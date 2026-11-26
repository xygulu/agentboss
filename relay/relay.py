#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""codeboss relay —— 中继服务器：账号/设备登记 + 盲隧道（规划-中继与多机账号.md v0.3 MVP）。

拓扑：手机 ──https──▶ relay ◀──wss(出站)── 开发机(host-link in server.py)
relay 只做三件事：账号/设备登记、寻址、盲转发。**零存储会话内容**：不落盘、
转发帧不缓存、日志只记地址与字节数。信任模型（MVP）：relay 能看到 TLS 终结后
的明文——端到端加密是 v0.5+（规划 §4）。

路由约定：relay 自己的接口全部在 /r/* 下；其余路径（/、/api/*、/static/*、/ext/*）
一律按 cookie cb_dev 选中的设备盲转发到 host（host-link 会在 host 侧注入本机
token 并剥掉 Set-Cookie，手机永远不接触 host 的 token.txt）。

事件提醒（业务逻辑/策略/规则在中继）：开发机把 Claude Code hook 的原始事件 POST 到
/r/dev/ev，中继按 notify_policy.json（管理接口热更新）算出该发哪些提醒并返回，
开发机负责推给手机。事件正文只在内存里过一遍，不落盘、不进日志。

运行：python3 relay.py [--host 0.0.0.0] [--port 8791] [--db relay.db]
      [--cert fullchain.pem --key privkey.pem]     # 直接 TLS；或前置 caddy/nginx
环境变量：RELAY_MAX_DEVICES=3   每账号设备上限（0=不限，自建 relay 建议 0）
         RELAY_SMTP_HOST/PORT/USER/PASS/FROM   邮箱验证码 SMTP（QQ/163 用授权码）。
         HOST=console 时验证码只打印到服务日志（journalctl 可取），供自建/测试。
         RELAY_ADMIN_TOKEN   设置后开放网页管理后台 /r/admin：手动上传更新文件
         （源码 relay.py / 二进制 codeboss-relay，按运行模式显示）、一键从更新源
         拉取（未设置则两者整体关闭）。
         RELAY_GH_REPO/TOKEN   自动更新源=GitHub 最新 release（私有库需 contents 只读
         PAT；二进制模式取 relay-v* 流，源码模式取其余 tag 的 relay.py）。
         Gitea 自建可用 RELAY_GH_API 覆盖。
         RELAY_UPDATE_URL/TOKEN   备选直连拉取源。国内 VPS 拉不动就走后台手动上传。
         RELAY_BACKUP_H/KEEP    relay.db 自动备份间隔（小时，默认 6）与保留份数（默认 14）。
                                只是首次播种默认值，之后在后台「计划任务」里改。
         RELAY_MON_MIN          资源监控采样间隔（分钟，默认 10）。
         RELAY_DISK_MAX_PCT / RELAY_DISK_MIN_FREE_MB / RELAY_MEM_MIN_MB
                                磁盘/内存告警线（默认 90%、2048MB、200MB）。
         RELAY_NET_MAX_MBPS     线路带宽（Mbps，用于「打满」告警和后台占线路百分比）。
                                必须填真实带宽：填大了告警永不触发、页面百分比也偏小；
                                按流量计费、峰值很高时填 0 关掉这条，改看 RELAY_DAY_GB_MAX。
         RELAY_DAY_GB_MAX       日出口流量告警线（GB，默认 0=不查）。
                                按流量计费时这是账单保护：中继正常一天约 1~2 GB。
         RELAY_NET_SAMPLE_SEC   后台「出口带宽」曲线采样间隔（秒，默认 60）。
                               对象存储（OSS）与大模型配置不走环境变量：
                                 /r/admin →「存储」/「模型」，存 meta 表，改完即时生效。
"""
import argparse
import asyncio
import base64
import pathlib as _pathlib
import sys as _sys
# 仓库按组件分目录（host/ 与 shared/ 是兄弟）；发布包是扁平的，品牌模块就在旁边。
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent / "shared"))
try:
    import branding
except ImportError:  # 单文件部署（/r/update 只带 relay.py）时的兜底，见 tests/test_relay_single_file.py
    # __BRANDING_FALLBACK_BEGIN__
    import types as _bt
    branding = _bt.ModuleType("branding")
    branding.RELAY_HOST_DEFAULT = "relay.example.com"
    branding.RELAY_URL_DEFAULT = "https://" + branding.RELAY_HOST_DEFAULT
    branding.SITE_URL = "https://example.com"
    branding.PRODUCT_CODEBOSS = "codeboss"
    branding.PRODUCT_AGENTBOSS = "agentboss"
    branding.PRODUCTS = (branding.PRODUCT_CODEBOSS, branding.PRODUCT_AGENTBOSS)
    branding.PRODUCT_DEFAULT = branding.PRODUCT_CODEBOSS
    branding.SERIES = "aiboss"
    branding.PRODUCT_NAMES = {
        branding.PRODUCT_CODEBOSS: {"label": "口袋工头", "app": "CodeBoss"},
        branding.PRODUCT_AGENTBOSS: {"label": "代理工头", "app": "AgentBoss"},
    }

    def _valid_product(s):
        """产品线标识清洗：不在枚举里（含空/None）一律回落默认，绝不猜。"""
        return s if s in branding.PRODUCTS else branding.PRODUCT_DEFAULT

    def _product_name(product):
        return branding.PRODUCT_NAMES[_valid_product(product)]

    branding.valid_product = _valid_product
    branding.product_name = _product_name
    # __BRANDING_FALLBACK_END__
import calendar
import collections
import contextvars
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from aiohttp import web, WSMsgType

MAX_DEVICES = int(os.environ.get("RELAY_MAX_DEVICES", "3"))
# 自建模式（私有化部署）：官方专属端点（计费/TTS/APK 分发/管理后台…）一律 404，
# 页面/host 按 /r/caps 降级。官方部署不设这个变量 = 现状零变化（设计稿 2026-11-23）。
SELFHOST = os.environ.get("RELAY_SELFHOST", "") == "1"
# TURN 兜底：STUN 打洞对对称 NAT / 企业网必失败，这类用户只能回中继隧道。
# 配了 RELAY_TURN_URLS（逗号分隔）就把凭据下发给浏览器；不配=只用 STUN（老行为）。
TURN_URLS = [u.strip() for u in os.environ.get("RELAY_TURN_URLS", "").split(",") if u.strip()]
TURN_USER = os.environ.get("RELAY_TURN_USER", "")
TURN_PASS = os.environ.get("RELAY_TURN_PASS", "")
ADMIN_TOKEN = os.environ.get("RELAY_ADMIN_TOKEN", "")

# 后台「日志上报」最长开多久：到点自动关，别让一台开发机一直往中继推日志。
LOG_MAX_S = 24 * 3600

# 上报日志在服务器上留几天：一天一份快照（当天反复推 = 覆盖成最新那份），
# 中继重启/内存丢了也能回看；超过这个天数自动删。
DEVLOG_KEEP_D = 7
DEVLOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devlogs")


def _devlog_save(dev, ver, txt):
    try:
        os.makedirs(DEVLOG_DIR, exist_ok=True)
        p = os.path.join(DEVLOG_DIR, "%s-%s.log" % (dev, time.strftime("%Y%m%d")))
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
            f.write("[%s] v%s（host 推来的 server.log 尾部）\n%s"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), ver,
                       str(txt or "")[-200000:]))
        os.replace(tmp, p)
    except OSError:
        pass


def _devlog_purge(now=None):
    """删掉 DEVLOG_KEEP_D 天前的日志快照。"""
    cut = (now or time.time()) - DEVLOG_KEEP_D * 86400
    try:
        for nm in os.listdir(DEVLOG_DIR):
            p = os.path.join(DEVLOG_DIR, nm)
            try:
                if os.path.getmtime(p) < cut:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass

# 套餐/升级暂时整体关闭：页面、账号中心链接、超额页的「开通」按钮全不出现，
# 直接访问 /r/plans 也只给一句「暂未开放」。要恢复时设 RELAY_PLANS_OPEN=1 重启。
PLANS_OPEN = os.environ.get("RELAY_PLANS_OPEN", "") == "1"
# 管理员手机号/密码：可写在环境里做初值，也可在后台「策略 → 管理员安全」里改（存 meta 表）。
ADMIN_PHONE_ENV = os.environ.get("RELAY_ADMIN_PHONE", "")
ADMIN_PASS_ENV = os.environ.get("RELAY_ADMIN_PASS", "")
ADM_TTL = 30 * 86400     # 后台登录有效期（滑动：每打开一次页面顺延，见 _adm_touch）
UPDATE_URL = os.environ.get("RELAY_UPDATE_URL", "")
UPDATE_TOKEN = os.environ.get("RELAY_UPDATE_TOKEN", "")
GH_REPO = os.environ.get("RELAY_GH_REPO", "")          # 如 example/codeboss
GH_TOKEN = os.environ.get("RELAY_GH_TOKEN", "")        # 私有库需 PAT（contents 只读）
# 客户端 APK 落盘（host 每次连上 ws 链路时推送）：/codeboss.apk 与 /api/apk_ver 直接本地答，
# 开发机离线也能首装/自更新；没存过才透传在线 host。APK 是公开产物（不含 token/会话数据）。
# boss 系列（D5/D12）：槽按产品线分文件（_apk_slot），codeboss 沿用现名——存量零迁移。
APK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-apk.bin")
APK_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-apk.json")
# 管理后台安卓壳 APK：只在后台「下载」页给管理员，不进官网、不进 OSS、不对外。
ADMIN_APK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "codeboss-admin-apk.bin")
# 按 code 留几个历史 APK + host 包：灰度/分版本推送时不同对象要拿到不同版本。
APK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apkpkgs")
# 灰度配置（后台「版本」页维护）：{"default": {ver, apk}, "rules": [{id, ver, apk, devs, clients, users}]}
# default = 没命中规则的对象拿到的「全员基线」；规则 = 指定版本推给指定开发机/终端/用户。
CANARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-canary.json")
# 客户端 iOS 包（.ipa）：/api/ios_ver + /ios-install + /codeboss.ipa 免鉴权直答。
# iOS 不能像安卓那样从网页直接装包，只能走 itms-services 清单：先开安装页，
# 再由系统按 /ios.plist 里的地址装 .ipa。host 每次连上 ws 链路把 .ipa 推上来落盘。
IOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-ios.bin")
IOS_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-ios.json")
IOS_BUNDLE = "com.example.codeboss.ios"
# iOS 公开内测（TestFlight）：苹果唯一允许「陌生设备」装的通道——不用登记 UDID、
# 不用第三方签名，点链接就装。公开用户只能走这条，所以 App 里的更新检查以它为准；
# 上面的开发签名通道（itms-services）留给已登记设备的开发者。
# 链接空了 = 还没开公开内测，页面退回开发签名安装页。
TF_URL = os.environ.get("CB_TF_URL", "https://testflight.apple.com/join/XXXXXXXX").strip()
# TestFlight 上「当前对外的 build」：App 的更新提示要比这个版本。
# 上传新 build 并过审后改这个文件（不用重启中继）。没有文件就退回开发签名的版本。
TF_META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-ios-tf.json")
# 开发机客户端包（源码 tar.gz）：/api/host_ver + /codeboss-host.tar.gz 免鉴权直答，
# Mac 菜单栏 App 的「检查更新」用它。发布方式=维护者用管理令牌 POST /r/admin/hostpkg，
# 人工控制上架时机（不像 APK 那样随构建自动推）。
HOSTPKG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-host.tar.gz")
HOSTPKG_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-host.json")
# 每个上架过的版本各留一份（近 6 个）：/codeboss-host-<ver>.tar.gz。灰度按版本分发靠它。
HOSTPKG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hostpkgs")
# Linux 发行包（tar.gz，自带 x86_64 Python）：/codeboss-linux.tar.gz 直链
LINUXPKG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-linux.tar.gz")
LINUXPKG_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-linux.json")
# Windows 安装包（NSIS exe）：官网下载 + 走 OSS 加速
EXE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CodeBoss-Setup.exe")
# Mac 菜单栏 App 本体（DMG）：/api/mac_ver + /bosshost-mac.dmg 免登录直答（和 APK 同款）。
# 老链接 /codeboss-mac.dmg、/CodeBoss-Setup.exe 继续能用（已发出去的页面/文档里还有）。
# 发布方式=维护者 POST /r/admin/macdmg?ver=x.y.z（Bearer RELAY_ADMIN_TOKEN，publish-dmg.sh）。
MAC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-mac.dmg")
MAC_META = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeboss-mac.json")
GH_API = os.environ.get("RELAY_GH_API", "https://api.github.com")   # 自建 Gitea 可覆盖
RELAY_VER = ""                                         # 版本号；CI 打包时把 tag 注入此处，源码运行为空


def _is_frozen():
    """PyInstaller 封装运行（发布形态：Linux 二进制，免源码）。"""
    return getattr(sys, "frozen", False)


def _self_path():
    return sys.executable if _is_frozen() else os.path.abspath(__file__)
PAIR_TTL = 15 * 60          # 一次性配对码有效期
TAKEOVER_TTL = 10 * 60      # 接管请求等确认的时间：过了就作废，重新扫码再来
CODE_TTL = 10 * 60          # 邮箱验证码有效期
REQ_TIMEOUT = 120           # 隧道单请求超时（host 侧 /api/update 等可能较慢）
STREAM_PATHS = ("/api/ext_push", "/api/bg_push")   # 走分块转发（SSE 等长连）的路径前缀
# 长连接不计入「远程使用时长」：推流/实时画面/消息流是通道，不是使用。
# 手机页面开着不动也会每 50 秒重连一次推流（ext.html 的 50s 自续），按普通请求
# 记账的话，一台摆桌上的手机一整天能烧掉 600 多分钟，用户根本没碰过手机。
# ext_snap 是「原样」视图 1 秒一次的终端帧：和实时画面看的是同一个终端、同一件事，
# 只因为它是短请求当初漏在豁免表外——两个视图「看」的账必须一样（用户拍板统一）。
METER_EXEMPT_PATHS = ("/api/ext_push", "/api/ext_msg/", "/api/ext_live/", "/api/ext_screen/",
                      "/api/ext_cells/",
                      "/api/ext_snap/",
                      # 终端页的输出拉取（WS 不通时的 HTTP 长轮询）：和 ext_snap 是同一份「看输出」。
                      "/api/ext_term_out/",
                      # 本机偏好（开关/样式）：改配置不算远程使用。
                      "/api/ext_pref",
                      # 标已读（开页/回前台/提醒看完自动发）：属于「看」的一部分。
                      # 以前靠客户端参数 a=0 豁免，2026-11-11 起 a=0 不再算数，必须进表。
                      # 这份表要和 server.py 的 USE_STREAM_PATHS 一字不差（有单测守）。
                      "/api/seen")


def _meter_exempt(path):
    """这条路径不记分钟（口径见 METER_EXEMPT_PATHS）。"""
    return path.startswith(METER_EXEMPT_PATHS)


# 「手机端到底做了什么」的动作归类：路径前缀 → (kind, 名字)。kind：
#   act = 用户主动操作（发消息/按键/开会话…），nav = 打开页面，read = 轮询/读数据。
# 只用于查账（用户问「我没怎么用，怎么扣了这么多分钟」时，能逐分钟看是哪来的）。
# 顺序敏感：前缀匹配，窄路径写在宽路径前面。
# 免费版「每天 N 条」计的就是发送类动作：发消息/按键/终端输入/跑命令/写文件/新建会话。
# 读（看输出/拉列表/轮询/看画面）不计——「看不计时、写计时」。
SEND_PATHS = ("/api/ext_send", "/api/ext_key", "/api/ext_term_in/",
              "/api/ext_cmdrun/", "/api/ext_write", "/api/ext_sess_new")
SEND_ACTS = ("发消息", "按键", "终端输入", "跑命令", "写文件", "新建会话")


def is_send_action(path):
    p = str(path or "")
    return any(p.startswith(x) for x in SEND_PATHS)


ACT_RULES = (
    ("/api/ext_write", "act", "写文件"),
    ("/api/ext_send", "act", "发消息"),
    ("/api/ext_key", "act", "按键"),
    ("/api/ext_term_in/", "act", "终端输入"),
    ("/api/ext_term_out/", "read", "看终端输出"),
    ("/api/ext_scan/", "act", "扫描"),
    ("/api/ext_cmddel/", "act", "删命令"),
    ("/api/ext_cmdrun/", "act", "跑命令"),
    ("/api/ext_cmdhist/", "read", "看命令历史"),
    ("/api/ext_cmdruns", "read", "看命令进度"),
    ("/api/ext_cmdout/", "read", "看命令输出"),
    ("/api/ext_open", "act", "打开文件"),
    ("/api/ext_migrate", "act", "迁移会话"),
    ("/api/ext_resume", "act", "恢复会话"),
    ("/api/ext_sess_new", "act", "新建会话"),
    ("/api/ext_sess_fork", "act", "fork 会话"),
    ("/api/ext_sess_close", "act", "关闭会话"),
    ("/api/ext_sess_del", "act", "删除会话"),
    ("/api/ext_model_switch", "act", "切模型"),
    ("/api/ext_model", "read", "看模型"),
    ("/api/ext_pref", "act", "改设置"),
    ("/api/ext_sessions", "read", "看会话列表"),
    ("/api/ext_snap/", "read", "看画面"),
    ("/api/ext_stream/", "read", "看画面"),
    ("/api/sessions", "read", "看会话列表"),
    ("/api/term_new", "act", "新开终端"),
    ("/api/term_close", "act", "关终端"),
    ("/api/term_mac", "act", "终端按键"),
    ("/new", "act", "开会话"),
    ("/kill/", "act", "结束会话"),
    ("/remove/", "act", "删除会话"),
    ("/respawn/", "act", "重开会话"),
    ("/api/ptt", "act", "语音"),
    ("/api/seen", "act", "标已读"),
    ("/api/pay_", "act", "支付"),
    ("/api/feedback", "act", "反馈"),
    ("/api/update", "act", "更新"),
    ("/api/say", "read", "取语音"),
    ("/api/host_stat", "read", "看机器状态"),
    ("/api/ping", "read", "探活"),
    ("/api/plan", "read", "看套餐"),
    ("/api/open", "act", "打开文件"),
    ("/api/upload", "act", "上传文件"),
)
ACT_NAV = (("/ext/", "打开会话页"), ("/s/", "打开终端页"), ("/ws/", "终端连接"))


def act_label(path, method="GET"):
    """把一次请求翻译成 (kind, 名字)。后台「动作记录」用；未知路径按读/写粗分。"""
    p = str(path or "")
    for pre, kind, name in ACT_RULES:
        if p.startswith(pre):
            return kind, name
    if method in ("GET", "HEAD"):
        for pre, name in ACT_NAV:
            if p.startswith(pre):
                return "nav", name
        if p in ("/", "/d", ""):
            return "nav", "打开设备页" if p == "/d" else "打开控制台"
        return "read", p[:60]
    return "act", (method + " " + p)[:60]


STREAM_IDLE = 90            # 流空闲超时：host 心跳 15s 必达，90s≈6 拍余量
STREAM_QMAX = 64            # 每流缓冲队列上限（64×64KB≈4MB）
# 语音识别（ASR）统一在中继：key 只放中继，各开发机不再各自持有。
# 代价只是每句话的音频过一趟中继（24–48KB，压缩音频），比让每台机器各配一把 key
# 更好管：一把 key、按设备每日秒数封顶、随时换模型、出问题一处可查。
ASR_KEY = os.environ.get("RELAY_DASHSCOPE_KEY", "")
ASR_MODEL = os.environ.get("RELAY_ASR_MODEL", "qwen3-asr-flash")
# 可配多个，按顺序尝试：某个模型不可用（下架/无权限/欠费）自动换下一个。
ASR_MODELS = [ASR_MODEL]
# 场景提示词：转写的是开发者在手机上对 Claude Code/终端的语音指令——中英混杂、
# 命令、路径、git/npm 等。带提示词准确率更高；可 env 覆盖。
# 2026-11 实测（固定音频 × 多种提示词）：
#   · DashScope 只接受**一条** system 文本消息——多轮 context 直接 400
#     （The dedicated task `asr` … does not support this input），别再加历史消息；
#   · 单个词塞进提示词对错读的修正很弱（WebSocket 照样错），所以「越用越懂」
#     不能只靠提示词：转写结果还要按词表做确定性纠正（_asr_fix_text）。
# 因此这里只留「场景 + 项目词表」两段，总长压在 400 字以内（超了尾部会被截掉）。
ASR_PROMPT_HEAD = (
    "场景：开发者用手机语音给电脑上的 Claude Code 编程助手和终端下指令，中英混杂。"
    "技术词按原样转写，不翻译、不改写，中文正常加标点。")
ASR_PROMPT = os.environ.get("RELAY_ASR_PROMPT", ASR_PROMPT_HEAD)
ASR_PROMPT_BUDGET = 400   # DashScope 上下文增强上限（字）
_ASR_PROMPT_OK = [True]   # 提示词格式被 API 拒过一次就永久关掉，不再白跑一次失败请求
ASR_DAILY_SEC = int(os.environ.get("RELAY_ASR_DAILY_SEC", "1800"))   # 兜底默认（套餐会覆盖）
ASR_MAX_BYTES = 20 * 1024 * 1024
# 语音合成（TTS）同样放中继：手机一个 TTS 引擎都没装，开发机本地 say 只有机械的婷婷；
# 提醒语音原本就是这个樱桃音色（预录音就是它）。开发机把整句发上来 → 中继合成 → 回 WAV。
TTS_MODEL = os.environ.get("RELAY_TTS_MODEL", "qwen3-tts-flash")
TTS_MODELS = [TTS_MODEL]
TTS_VOICE = os.environ.get("RELAY_TTS_VOICE", "Cherry")
# 播报风格换音色：开发机在 /r/tts 里传 voice，必须命中这张表（挡乱传/乱调参）。
# 表外的一律按默认 Cherry 合成，不报错——老开发机照旧能用。
# 只要标准普通话音色（方言音色一律不放进来：项目里明确不要方言播报）。
TTS_VOICES = {
    "Cherry": "芊悦", "Serena": "苏瑶", "Ethan": "晨煦", "Chelsie": "千雪",
}
TTS_MAX_CHARS = 300        # 一句提醒摘要的上限，超出截断（防被当免费合成接口刷）
TTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ttscache")
# 问题与建议：手机 App 设置页提交（文字 + 截图/视频），落中继磁盘，管理后台查看。
# 只收图片和视频，别的一律拒；大小/数量/字数都封顶，免得被当网盘。
FB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback")
FB_TEXT_MAX = 800
FB_FILES_MAX = 4
FB_ONE_MAX = 12 * 1024 * 1024
FB_TOTAL_MAX = 30 * 1024 * 1024
FB_MAGIC = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
    (b"\x1aE\xdf\xa3", "webm", "video/webm"),
)

# ---------- 套餐 / 功能墙（plans.json，改完 systemctl restart 生效） ----------
# 只把「用户感知得到」的东西收费：语音转写时长、开发机数、客户端数、
# 进度提醒、自动化。推送省流/P2P 是省中继成本的开关，永远免费。
_PLAN_PATH = os.environ.get("RELAY_PLAN_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "plans.json")
# 「看不计时、写计时」：远程时长只按写操作（发消息/按键/开会话/跑命令…）记格，
# 看消息/看列表/看输出/翻设置一律不记。免费版不看时长，只限每天 15 条消息 +
# 180 秒语音 + 20 次智能调用（sends_day / asr_sec_day / llm_day；0 = 不限）。
_PLANS_DEFAULT = {
    "free": {"name": "免费版",
             "limits": {"hosts": 1, "clients": 1,
                        "llm_day": 20, "use_min_day": 0,
                        "sends_day": 15, "asr_sec_day": 180,
                        "asr_sec_month": 5400},
             "features": {"terminal": False, "remind": False, "auto": False}},
    "pro": {"name": "Pro",
            "limits": {"hosts": 2, "clients": 2,
                       "llm_day": 300, "use_min_day": 0,
                       "sends_day": 200, "asr_sec_day": 1200,
                       "asr_sec_month": 0},
            "features": {"terminal": True, "remind": True, "auto": True}},
    "max": {"name": "Max",
            "limits": {"hosts": 5, "clients": 5,
                       "llm_day": 600, "use_min_day": 0,
                       "sends_day": 0, "asr_sec_day": 0,
                       "asr_sec_month": 0},
            "features": {"terminal": True, "remind": True, "auto": True}},
}
_PLAN_CACHE = [None, None]


def plans():
    """读 plans.json；文件缺失/损坏用内置默认档（不因为配置错误把服务拖死）。"""
    try:
        mt = os.path.getmtime(_PLAN_PATH)
    except OSError:
        return _PLANS_DEFAULT
    if _PLAN_CACHE[1] != mt:
        d = None
        try:
            with open(_PLAN_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print("[relay] plans.json 读取失败，用默认档位: %s" % type(e).__name__, flush=True)
        if not (isinstance(d, dict) and isinstance(d.get("free"), dict)
                and isinstance(d.get("pro"), dict)):
            d = _PLANS_DEFAULT
        else:
            # 老配置文件没有新档位（如 Max）时补默认值，不用等运维改文件
            for k, v in _PLANS_DEFAULT.items():
                if not isinstance(d.get(k), dict):
                    d[k] = v
        _PLAN_CACHE[0], _PLAN_CACHE[1] = d, mt
    return _PLAN_CACHE[0]


def _is_paid_plan(plan):
    """非免费、且 plans.json 里确实有这一档（含内部档 sell:false）。

    以前到处写死 ("pro","max")：内部套餐一分配就被当成过期，当场回落免费。"""
    return bool(plan) and plan != "free" and isinstance((plans() or {}).get(plan), dict)


def _plan_sold(key):
    """这一档是否对外售卖/展示。plans.json 里写 "sell": false 的是内部档：
    只有管理后台看得见、能人工分配，不进套餐页、不进升级建议。缺省=售卖。"""
    v = (plans() or {}).get(key)
    return isinstance(v, dict) and v.get("sell") is not False


def _next_tier(plan, key):
    """plans.json 里排在当前档之后、对外售卖的第一档 → (名字, 该项额度)，没有则 None。

    **档位高低用配置顺序**（free → pro → max → …，后台也按这个顺序列），不写死档位名：
    以后加档、改额度都自动跟上，最高档永远返回 None（不劝人升级/降级）。
    内部档（sell:false）整档跳过：套餐页看不到、也买不到。
    额度 0 在这个体系里表示「不限」。"""
    p = plans()
    keys = [k for k, v in p.items()
            if isinstance(v, dict) and k not in ("skus", "packs")]
    if plan not in keys:
        return None
    for k in keys[keys.index(plan) + 1:]:
        v = p[k]
        if v.get("sell") is False:
            continue
        return ((v.get("name") or k),
                int(((v.get("limits") or {}).get(key) or 0)))
    return None


def _cap_error(what, plan, cap, key, unit="个", note=""):
    """额度用尽的提示语：确实有更高档才劝升级，最高档直接说已达最高限额。
    note 补一句「怎么释放」（客户端数可以用退出登录/24 小时自动释放）。"""
    nx = _next_tier(plan, key)
    if nx:
        lim = "不限" if not nx[1] else ("%d %s" % (nx[1], unit))
        return "%s已达上限（%d），%s最多 %s%s" % (what, cap, nx[0], lim, note)
    return "%s已达上限（%d），已达最高限额%s" % (what, cap, note)


def _sku_rank(sku):
    """档位高低：Max 永远高于 Pro，同等级里时长越长越高。用于判断升级/降档。"""
    s = str(sku or "")
    if not s:
        return -1
    v = pay_skus().get(s) or ("", "0", 0)
    return (100000 if s.startswith("max_") else 0) + int(v[2] or 0)


def _sku_tier(sku):
    """订阅档位：max_* = Max，其余（trial/month/quarter/year）= Pro。"""
    return "max" if str(sku or "").startswith("max_") else "pro"


def _cur_sku_of(db, uid):
    """最近一次买过的订阅档位（老库没记 plan_sku 时兜底用）。时长包不算。"""
    row = db.execute("SELECT sku FROM orders WHERE uid=? AND status=1 "
                     "AND sku NOT LIKE 'pack%' ORDER BY paid DESC LIMIT 1",
                     (int(uid),)).fetchone()
    return (row[0] or "") if row else ""


def _sku_months(sku):
    """订阅档的自然周期（月数）：月付/体验月 1，季付 3，年付 12；时长包 0。"""
    s = str(sku or "")
    if s in ("trial", "month", "max_month"):
        return 1
    if s in ("quarter", "max_quarter"):
        return 3
    if s in ("year", "max_year"):
        return 12
    return 0


def _period_end(ts, months, base_is_expiry=False):
    """自然周期到期时刻（存「最后一天 23:59:59」，次日 0 点起失效）。

    例：11-08 买月付 → 12-07 23:59:59（12-08 过期）；
        01-31 买月付 → 02-27 23:59:59（2 月没有 31 号，按月末算）。
    base_is_expiry=True 表示 ts 已经是现套餐的过期时刻（那天是最后一天），
    新周期从次日接上——续费既不重叠也不丢一天。"""
    d = time.localtime(ts)
    y, m, day = d.tm_year, d.tm_mon, d.tm_mday
    if base_is_expiry:
        nx = time.localtime(time.mktime((y, m, day + 1, 12, 0, 0, 0, 0, -1)))
        y, m, day = nx.tm_year, nx.tm_mon, nx.tm_mday
    total = y * 12 + (m - 1) + int(months)
    ey, em = total // 12, total % 12 + 1
    ed = min(day, calendar.monthrange(ey, em)[1])
    return time.mktime((ey, em, ed - 1, 23, 59, 59, 0, 0, -1))


def _period_span(exp, sku):
    """现套餐这一自然周期的总秒数（把剩余时间折成钱时当分母）。"""
    n = _sku_months(sku)
    if n <= 0:
        v = pay_skus().get(sku) or ("", "0", 0)
        return max(86400.0, float(v[2] or 0) * 86400.0)
    return max(86400.0, float(exp) - _period_end(exp, -n, base_is_expiry=True))


def _sku_paid_price(db, uid, sku):
    """现档剩余价值的计价基准＝用户最近一次买这一档的实付价（不含补差价单）。

    价目表调过价时，按现价折算会让按旧价买的老用户凭空少一截抵扣；
    没有购买记录（老库/后台赠送）才回退到当前价。"""
    try:
        row = db.execute(
            "SELECT money FROM orders WHERE uid=? AND sku=? AND status=1 "
            "AND COALESCE(credit,0)<=0 ORDER BY COALESCE(paid,created) DESC LIMIT 1",
            (int(uid), str(sku))).fetchone()
        p = float((row[0] if row else 0) or 0)
    except (TypeError, ValueError, sqlite3.Error):
        p = 0.0
    return p if p > 0 else 0.0


def _queue_start(exp):
    """排队档的实际生效日（现套餐最后一天结束后的 0 点），页面展示用。"""
    d = time.localtime(float(exp or 0))
    return time.mktime((d.tm_year, d.tm_mon, d.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _queue_end(exp, nmon, ndays):
    """排队档的结束时刻：现套餐到期后接 N 个自然月，再加按天累加的部分。"""
    end = _period_end(exp, nmon, base_is_expiry=True) if int(nmon or 0) > 0 else float(exp)
    return end + int(ndays or 0) * 86400


def plan_state(db, uid):
    """(plan, expires, sku, next_sku, next_days)——把「排队等生效」的换档一起算进来。

    降档/缩短不立刻削权益：钱先收，档位从现套餐到期那天接上（起始日＝原到期日，
    不重叠、不浪费）。所以读套餐的地方一律走这里，别只读 users.plan/plan_expires。
    排队时长新单按「自然月数」存 plan_next_n，老单仍按 plan_next_days 兼容。"""
    row = db.execute("SELECT plan,plan_expires,plan_sku,plan_next,plan_next_days,"
                     "COALESCE(plan_next_n,0) FROM users WHERE id=?", (int(uid),)).fetchone()
    if not row:
        return "free", 0.0, "", "", 0
    plan, exp = (row[0] or ""), float(row[1] or 0)
    sku, nxt = (row[2] or ""), (row[3] or "")
    ndays, nmon = int(row[4] or 0), int(row[5] or 0)
    now = time.time()
    if _is_paid_plan(plan) and exp > now:
        if nxt and (ndays > 0 or nmon > 0) and exp > 0:
            ndays = int(round((_queue_end(exp, nmon, ndays) - exp) / 86400.0))
        return plan, exp, (sku or _cur_sku_of(db, uid)), nxt, ndays
    if nxt and (ndays > 0 or nmon > 0) and exp > 0:
        nplan = "max" if nxt.startswith("max_") else "pro"
        nend = _queue_end(exp, nmon, ndays)
        if nend > now:
            return nplan, nend, nxt, "", 0
    return "free", 0.0, "", "", 0


def _migrate_users_products(db):
    """老库补 end 权限列（幂等）：users.products CSV，NULL=按套餐。
    空串 = 一个端都不给（后台可据此停用某端）。"""
    try:
        db.execute("ALTER TABLE users ADD COLUMN products TEXT")
    except sqlite3.OperationalError:
        pass


def _parse_products(raw):
    """后台填的端权限 → CSV 或 None（None = 按套餐）。
    列表/CSV 都收；全脏值当「一个端都不给」（空串），别静默变成 null。"""
    if raw is None:
        return None
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    vals = {str(x).strip() for x in items if str(x).strip() in branding.PRODUCTS}
    return ",".join(p for p in branding.PRODUCTS if p in vals)   # 固定顺序，便于比对


def _allowed_products(db, uid):
    """这账号能用哪些端：(list, source)。中继是唯一硬墙，host 只做展示。

    - users.products 有值（含空串）→ 逐账号覆盖
    - pro / max 且未过期 → 双端
    - 其它 → 只给 users.product 那条线，缺省 codeboss
    绝不收回正在用的端：free 账号 product=agentboss 仍拿 agentboss。
    脏值（不是空串、但一个合法端都没有）按套餐走，别把人锁死。"""
    row = db.execute("SELECT product, products FROM users WHERE id=?",
                     (int(uid),)).fetchone()
    if not row:
        return ([branding.PRODUCT_DEFAULT], "plan")
    product = branding.valid_product(row[0])
    ov = row[1]
    if ov is not None:
        vals = [p.strip() for p in str(ov).split(",") if p.strip() in branding.PRODUCTS]
        if vals or not str(ov).strip():
            return (vals, "override")
    plan = plan_state(db, uid)[0]
    if plan in ("pro", "max"):
        return (list(branding.PRODUCTS), "plan")
    return ([product], "plan")


def _locked_page(product, allowed):
    """端没开通的 403 页：说清楚哪个端、现在能用哪个、去哪开通。
    2026-11-18 用户要「账号层面控制能用哪些端」——拦人要拦得明白。"""
    want = branding.product_name(product)
    have = "、".join(branding.product_name(p)["label"] for p in allowed) or "（暂无）"
    return _PAGE_LOCKED.replace("__CSS__", _CSS) \
        .replace("__LABEL__", want["label"]).replace("__APP__", want["app"]) \
        .replace("__HAVE__", have) \
        .replace("__PLANS_UP__", _LOCKED_NOPLANS if SELFHOST else _LOCKED_PLANS_A)


def _plan_roll(db, uid):
    """排队的档位已经到点 → 落回 users 正列。不落库的话，之后再换档的起始日
    会从早就过期的那一天算起（plan_state 是虚拟计算，只读没问题，写入前必须落地）。"""
    row = db.execute("SELECT plan,plan_expires,plan_sku,plan_next,plan_next_days,"
                     "COALESCE(plan_next_n,0) FROM users WHERE id=?", (int(uid),)).fetchone()
    if not row:
        return
    plan, exp = (row[0] or ""), float(row[1] or 0)
    nxt = (row[3] or "")
    ndays, nmon = int(row[4] or 0), int(row[5] or 0)
    now = time.time()
    if _is_paid_plan(plan) and exp > now:
        return
    if not (nxt and (ndays or nmon) and exp):
        return
    nend = _queue_end(exp, nmon, ndays)
    if nend <= now:
        return
    db.execute("UPDATE users SET plan=?,plan_expires=?,plan_sku=?,"
               "plan_next=NULL,plan_next_days=0,plan_next_n=0 WHERE id=?",
               ("max" if nxt.startswith("max_") else "pro", nend, nxt, uid))


def _upgrade_quote(db, uid, sku, now=None, st=None):
    """跨档升级的补差价（Pro → Max）。

    规矩（用户 2026-11-07 拍板）：
      · 同等级换更长的时长（Pro 月→季/年）＝**延续时间**，不补差；
      · 更低/更短的档＝**预约**，现套餐到期后才生效（_pay_apply 里原有逻辑）；
      · 只有跨等级升级才按「新档价 − 现档剩余价值」补差价，现档剩余时间折成钱抵掉，
        新档从**现在**起算（时间已经折算过，不能再叠加，否则等于抵扣两次）。

    剩余价值 = 现档实付价 × 剩余秒 / 当前自然周期总秒（调过价也不缩水）；
    抵扣超过新档价时，多出来的部分按新档单价折成天数一起给（不吞用户的钱）。
    返回 {pay, credit, days, overflow, months, from, left_days}；不适用时返回 None。
    `days` 只用于展示，`overflow` 才是到账时要额外加的天数（自然周期之外）。"""
    now = time.time() if now is None else now
    skus = pay_skus()
    v = skus.get(str(sku or ""))
    if not v or not v[2]:
        return None
    plan, exp, cur_sku = (st[0], st[1], st[2]) if st else plan_state(db, uid)[:3]
    if plan not in ("pro", "max"):
        return None
    cur_sku = cur_sku or _cur_sku_of(db, uid)
    if not cur_sku:
        return None
    if _sku_tier(sku) == _sku_tier(cur_sku):
        return None                      # 同档换更长的时长＝延续时间，不补差
    if _sku_rank(sku) <= _sku_rank(cur_sku):
        return None                      # 更低/更短＝预约，到期后才生效
    cv = skus.get(cur_sku)
    if not cv or not cv[2]:
        return None
    left = float(exp or 0) - now
    if left <= 60:                       # 快到期了就别折腾，按普通购买走
        return None
    cprice = _sku_paid_price(db, uid, cur_sku) or float(cv[1] or 0)
    credit = round(cprice * left / _period_span(exp, cur_sku), 2)
    tprice, tdays = float(v[1] or 0), int(v[2])
    months = _sku_months(sku)
    overflow, pay = 0, round(tprice - credit, 2)
    if pay < 0:                          # 剩的时间比新档还值钱：差额折成新档天数
        if tprice > 0:
            overflow = int((-pay) * tdays / tprice)
        pay = 0.01                       # 支付网关不接受 0 元单
    if months > 0:
        # 展示按「自然日」算：今天整天也算在内（11-08 买月付 = 30 天）
        d0 = time.localtime(now)
        t0 = time.mktime((d0.tm_year, d0.tm_mon, d0.tm_mday, 0, 0, 0, 0, 0, -1))
        days = int(round((_period_end(now, months) - t0) / 86400.0)) + overflow
    else:
        days = tdays + overflow
    return {"pay": round(pay, 2), "credit": credit, "days": int(days),
            "overflow": int(overflow), "months": int(months),
            "from": cur_sku, "left_days": int(left // 86400)}


def _upgrade_quotes(db, uid, now=None):
    """所有可补差价升级的档位一次算完（/api/plan 用）：现档状态只查一次。"""
    now = time.time() if now is None else now
    plan, exp, cur_sku, _n, _nd = plan_state(db, uid)
    if plan not in ("pro", "max"):
        return {}
    st = (plan, exp, cur_sku or _cur_sku_of(db, uid))
    out = {}
    for k in pay_skus():
        q = _upgrade_quote(db, uid, k, now, st)
        if q:
            out[k] = {"pay": q["pay"], "credit": q["credit"],
                      "days": q["days"], "left_days": q["left_days"]}
    return out


def _queue_conflict(db, uid, sku, st=None):
    """这一单会不会把已经付过钱的「排队换档」顶掉？会就返回提示语，否则返回 ''。

    降档/缩短不立刻生效，而是等现套餐到期后再接上（plan_next / plan_next_days）。
    如果用户又买另一个档，plan_next 会被换成新档，原来那笔排队的天数就整个丢了——
    钱已经收了，不能悄悄吞掉。所以下单这一步先挡住，让用户到期后再换，
    或者再买同一档把天数累加。"""
    if pack_min().get(sku, 0):
        return ""
    plan, exp, cur_sku, nxt, nd = st or plan_state(db, uid)
    if plan not in ("pro", "max") or float(exp or 0) <= time.time():
        return ""
    if not nxt or int(nd or 0) <= 0 or nxt == sku:
        return ""
    if _sku_rank(sku) >= _sku_rank(cur_sku or _cur_sku_of(db, uid)):
        return ""                       # 这单立即生效，不会动排队档
    name = (pay_skus().get(nxt) or (str(nxt),))[0]
    return ("已有一笔预约换档（%s · %d 天）在等生效；到期后再换，"
            "或再买同一档把天数累加" % (name, int(nd)))


def _acct_uid(uid):
    """用量主体 → 账号 id：'u12' / 12 → 12；匿名设备 ('d…')、空 → None。

    subject_of() 把账号也包成字符串 'u<id>'（用量函数统一收主体），但套餐/覆盖
    是账号级的。2026-11-19 Max 语音被判成免费额度，就是这里把 'u1' 当匿名设备。"""
    if isinstance(uid, str):
        return int(uid[1:]) if uid[:1] == "u" and uid[1:].isdigit() else None
    return int(uid) if uid else None


def user_plan(db, uid):
    """账号当前生效档位；付费档到期自动回落 free（不改库）。"""
    uid = _acct_uid(uid)
    if not uid:                    # 匿名设备主体（d<dev>）：永远 free
        return "free"
    return plan_state(db, uid)[0]


def _plan_ov_key(uid):
    return "planov:%d" % _acct_uid(uid)


def plan_override(db, uid):
    """这个账号的手动额度覆盖（meta planov:{uid} → {"limits": {只写覆盖的键}}）。
    键不存在 = 继承套餐档位；0 = 不限。后台「用户 → 管理 → 额度覆盖」改。"""
    uid = _acct_uid(uid)
    if not uid:
        return {}
    ov = _meta_json(db, _plan_ov_key(uid)).get("limits")
    return ov if isinstance(ov, dict) else {}


def _plan_override_save(db, uid, lim):
    """lim 为空 dict = 清除覆盖（删 meta 键）。"""
    k = _plan_ov_key(uid)
    if lim:
        _meta_json_save(db, k, {"limits": lim, "at": time.time()})
    else:
        db.execute("DELETE FROM meta WHERE k=?", (k,))
        db.commit()


def _plans_for(product):
    """产品线可见的档位表：档位 dict 里 "product" 键标记归属（缺省 codeboss），
    skus/packs 是夹带的价目表不算档位。product 值经 valid_product 清洗，
    脏值自然回落 codeboss，不用单独校验。"""
    product = branding.valid_product(product)
    return {k: v for k, v in (plans() or {}).items()
            if isinstance(v, dict) and k not in ("skus", "packs")
            and branding.valid_product(v.get("product")) == product}


def _user_product(db, uid):
    """用户的产品线（users.product，缺省 codeboss）。"""
    uid = _acct_uid(uid)
    if not uid:
        return branding.PRODUCT_DEFAULT
    row = db.execute("SELECT product FROM users WHERE id=?", (int(uid),)).fetchone()
    return branding.valid_product(row[0] if row else None)


def plan_limits(db, uid):
    if SELFHOST:
        # 自建没有套餐运营：不吃 _PLANS_DEFAULT 免费档（15 条/天就把核心链路卡死，
        # 而升级/覆盖的官方入口全 404，用户无处可去）。全表口径 0 = 不限；
        # 台数交 RELAY_MAX_DEVICES 管（0 = 不设限）。plan_override 是官方后台的
        # 手动覆盖，自建没有后台，跳过。
        return {"hosts": MAX_DEVICES, "clients": MAX_DEVICES, "llm_day": 0,
                "use_min_day": 0, "sends_day": 0, "asr_sec_day": 0,
                "asr_sec_month": 0}
    prod = _user_product(db, uid)
    pfor = _plans_for(prod)
    # 只认 plan_state（user_plan）：付费档一到期立即按 free 算，不等后台改 users.plan。
    # 以前这里优先读 users.plan 原值，过期账号还能吃到 pro 的额度（复查确认的绕过）。
    key = user_plan(db, uid)
    tier = pfor.get(key) or (plans() or {}).get(key) or {}
    lim = dict(tier.get("limits") or {})
    if MAX_DEVICES and (not lim.get("hosts") or int(lim["hosts"]) > MAX_DEVICES):
        lim["hosts"] = MAX_DEVICES   # RELAY_MAX_DEVICES 仍是硬天花板
    try:
        lim.update(plan_override(db, uid))   # 2026-11-18 起：按账号手动覆盖，最终决定权（可超 MAX_DEVICES）
    except Exception:
        pass
    return lim


def plan_features(db, uid):
    if SELFHOST:
        # 免费档 features 把 terminal/remind/auto 锁死（terminal 路径直接被拦）：
        # 自建功能不按档位锁。
        return {"terminal": True, "remind": True, "auto": True}
    return dict((plans().get(user_plan(db, uid)) or {}).get("features") or {})


def _quota_cta(plan, what="用"):
    """额度用完后那句「怎么办」。只有免费版才劝升级——付费档被提示
    「开通 Pro」是坏文案（2026-11-19 Max 用户实报）。"""
    return ("开通 Pro 可继续%s" % what) if plan == "free" else ""


def usage_get(db, k):
    row = db.execute("SELECT n FROM usage WHERE k=?", (k,)).fetchone()
    return int(row[0]) if row else 0


def usage_add(db, k, n):
    db.execute("INSERT INTO usage(k,n) VALUES(?,?) "
               "ON CONFLICT(k) DO UPDATE SET n=n+excluded.n", (k, int(n)))
    db.commit()


# ---------- 远程使用时长（免费不限、Pro 8 小时/天、Max 不限；免费按消息条数+语音秒数限） ----------
# 口径（2026-11-09「看不计时、写计时」）：某个「刻度窗口」（默认 3 秒，生产 10 秒，
# 后台可配）里 ≥1 个「写请求」（POST/PUT/DELETE；GET/HEAD/OPTIONS 是看，不记），
# 该窗口记 unit 秒。中继隧道的和 host 补报的直连/P2P 都算。
# (uid, 窗口起点) 是幂等键 —— 中继先记、host 后补报（或反之）都只算一次；
# 不用进程内状态，重启/多进程安全。日界按中继本机时区（生产为 CST）。

def _min_now():
    """当前分钟序号。只用于「查账」类聚合（动作记录、终端归属仍按分钟看）。"""
    return int(time.time() // 60)


def _day_of_ts(ts):
    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))


# ---------- 计费刻度（后台可配，默认 3 秒） ----------
# 口径：一个「刻度窗口」里出现 ≥1 个计费请求，就记 unit 秒。以前窗口固定 60 秒
# （自然分钟记 1 分钟），一次操作也要记满 60 秒；现在默认 3 秒，后台能改。
# **账目一律按秒存**。2026-11-08 从「按分钟」切到「按秒」：历史账不迁移
# （用户拍板「历史不管，只影响之后的」），所以切换当天可能出现分钟旧值与秒新值混在
# 同一个日计数器里——只有切换那半天如此，第二天起全按秒。
META_METER = "meter"
METER_UNIT_DEFAULT = 3
METER_UNIT_MIN, METER_UNIT_MAX = 1, 60
_METER_CACHE = {"unit": 0, "ts": 0.0}


def meter_unit(db):
    """当前计费刻度（秒）。meta 里没配/配坏了一律按默认 3 秒。"""
    now = time.time()
    if _METER_CACHE["unit"] and now - _METER_CACHE["ts"] < 5:
        return _METER_CACHE["unit"]
    u = METER_UNIT_DEFAULT
    try:
        u = int(_meta_json(db, META_METER).get("unit_sec") or METER_UNIT_DEFAULT)
    except Exception:
        u = METER_UNIT_DEFAULT
    u = max(METER_UNIT_MIN, min(METER_UNIT_MAX, u))
    _METER_CACHE["unit"], _METER_CACHE["ts"] = u, now
    return u


def meter_set(db, unit):
    """后台改刻度：立即生效，只影响之后的时间窗（历史账本来就按秒存）。"""
    u = max(METER_UNIT_MIN, min(METER_UNIT_MAX, int(unit or METER_UNIT_DEFAULT)))
    _meta_json_save(db, META_METER, {"unit_sec": u})
    _METER_CACHE["unit"], _METER_CACHE["ts"] = u, time.time()
    return u


def _bucket_of(ts, unit):
    """ts 落在哪个刻度窗口：返回窗口起点（epoch 秒），同时当幂等键用。"""
    return int(int(ts) // int(unit)) * int(unit)


def _clamp_bucket(bs, unit, now=None):
    """补报窗口：最多回溯 48h；比现在还超一个窗口的直接丢掉
    （时钟偏快时别把未来的窗口占掉——一格占错了，那个窗口之后就再也记不了账）。"""
    now = time.time() if now is None else now
    try:
        bs = int(bs)
    except (TypeError, ValueError):
        return None
    if bs < now - 48 * 3600:
        return None
    if bs > now + unit:
        return None
    return bs


def subject_of(db, uid=None, dev=""):
    """用量主体：账号 u<id>，匿名设备 d<dev>（免注册免费版）。
    所有用量函数都把它当 uid 收：账号照旧落 int（老数据/老查询不变），
    匿名设备落字符串 'd<dev>'（SQLite 动态类型，INTEGER 列存得下）。"""
    if isinstance(uid, str):
        return uid
    if uid:
        return "u%d" % int(uid)
    return "d%s" % str(dev or "")


def _subj_id(uid):
    return int(uid) if not isinstance(uid, str) else uid


def use_used(db, uid):
    """今天已用（秒）。"""
    return usage_get(db, "use:%s:d:%s" % (subject_of(db, uid), time.strftime("%Y-%m-%d")))


def _day_start_min():
    """今天 0 点对应的「分钟序号」（中继本机时区，和 _min_now 同口径）。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) // 60)


def sends_used(db, uid):
    """今天发出去的条数（免费版 daily 上限用）。

    数据源就是动作表 `use_act_log` 的「发消息」：中继亲眼看到的隧道发送逐次 +1，
    开发机补报的直连/P2P 发送按分钟计数——两边各记各的、不会重复（隧道转发到
    开发机的那一份被 host 的 _peer_is_local 跳过）。读/看不在里面，符合
    「看不计时、写计时」的口径。"""
    row = db.execute(
        "SELECT COALESCE(SUM(n),0) FROM use_act_log "
        "WHERE uid=? AND m>=? AND act IN (%s)" % ",".join("?" * len(SEND_ACTS)),
        (_subj_id(uid), _day_start_min()) + SEND_ACTS).fetchone()
    return int(row[0]) if row else 0


def use_cap(db, uid):
    """套餐日上限（秒）。2026-11-26 拍板：收费墙只按条数，时长墙整体摘除——
    恒返 0（0 = 不限）。plans.json 里的 use_min_day 从此只是展示/老数据字段，
    不再参与任何拦截（隧道 403 / 语音 429 / host 回落档全走这一个源头），
    时长包也不再扣费。"""
    return 0


def use_credit_of(db, uid):
    """使用时长包余额（秒）。匿名设备没有账号，买不了包。"""
    uid = _acct_uid(uid)          # 'u<id>' 是账号主体，不是匿名设备
    if not uid:
        return 0
    row = db.execute("SELECT sec FROM use_credits WHERE uid=?", (uid,)).fetchone()
    return int(row[0]) if row else 0


def use_credit_add(db, uid, n, src="", note=""):
    """改时长包余额（秒）。每一笔变动都记流水——包括每格刻度的扣费（src='charge'）。

    以前只有余额这一个数字，用户问「包明明只买了 6 小时，怎么还剩 600 多分钟」时
    无从查起（确实出现过一笔 +600 找不到来源）。现在加减都有 before/after 和来源。"""
    uid = _acct_uid(uid)
    if not uid:                   # 匿名设备没有账号，不积攒也不扣时长包
        return
    n = int(n)
    before = use_credit_of(db, uid)
    after = max(0, before + n)
    db.execute("INSERT INTO use_credits(uid,sec,updated) VALUES(?,?,?) "
               "ON CONFLICT(uid) DO UPDATE SET sec=MAX(0,sec+excluded.sec),"
               "updated=excluded.updated", (uid, n, time.time()))
    if n:
        db.execute("INSERT INTO use_credit_log(uid,delta,before,after,src,note,ts) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (uid, n, before, after, str(src or "")[:20],
                    str(note or "")[:200], time.time()))
    db.commit()


def use_cap_eff(db, uid):
    """有效上限（秒）= 套餐日上限 + 时长包余额（包不过期，是超出套餐之后的补量池）。

    已用超过套餐后，每记一格就从余额扣一格（unit 秒）、已用 +unit，两者此消彼长，
    所以这个有效上限在补量期间保持不变；余额用光后它落到「已用」上，于是立刻拦住。
    上限 0 = 不限。"""
    cap = use_cap(db, uid)
    if not cap:
        return 0
    return max(cap, use_used(db, uid)) + use_credit_of(db, uid)


def use_take(db, uid, m=None, src="relay"):
    """记一个刻度窗口（幂等）。超出套餐上限的那一格从时长包余额里扣 unit 秒。
    返回 (今天已用秒, 有效上限秒, 是否已超)。
    m：指定窗口起点（补报/测试用），默认取当前窗口。"""
    unit = meter_unit(db)
    now = time.time()
    bs = _clamp_bucket(_bucket_of(now if m is None else m, unit), unit, now)
    if bs is None:
        bs = _bucket_of(now, unit)
    used, cap = use_used(db, uid), use_cap(db, uid)
    over_now = bool(cap and used >= cap)
    if over_now and use_credit_of(db, uid) <= 0:
        return used, used, True          # 套餐用光、余额也没有：拦住
    cur = db.execute("INSERT OR IGNORE INTO usage_min(uid,m,src,ts) VALUES(?,?,?,?)",
                     (_subj_id(uid), bs, src, time.time()))
    if cur.rowcount:
        usage_add(db, "use:%s:d:%s" % (subject_of(db, uid), _day_of_ts(bs)), unit)
        if over_now:
            use_credit_add(db, uid, -unit, "charge", "超出套餐额度")   # 补量：扣一格
    db.commit()
    return used + (unit if cur.rowcount else 0), use_cap_eff(db, uid), False


def use_record(db, uid, marks, src="host", dev="", dev_name="", gran="m"):
    """host 补报的刻度批量落库（幂等、不因超额拒收——这些都是已经发生过的使用）。
    超套餐的部分同样从时长包余额里扣。返回 (已接受的窗口, 今天已用秒, 有效上限秒)。

    marks：窗口标识列表。gran="m" 是老的「分钟序号」（老 host 只会按分钟报），
    gran="s" 是新的「窗口起点（epoch 秒）」。老 host 一分钟最多记一格——和它自己
    的口径一致，换刻度不会把老客户端的行为放大。

    dev/dev_name：这批刻度是哪台开发机报上来的。记一行归属（k="d:<设备号>"），
    后台查账时能直接回答「是哪台设备用掉的」——以前只能看到一个笼统的「直连补报」。"""
    unit = meter_unit(db)
    now = time.time()
    cap = use_cap(db, uid)
    ok, total, charged = [], {}, 0
    touched = set()
    for raw in (marks or [])[:2000]:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            continue
        # 统一落到刻度窗口起点再当幂等键：host 报的是「第几秒」（或老 host 的分钟序号），
        # 同一窗口里的多个秒必须合成一格。以前这里只 clamp 不 bucket，host 报 3 个连续
        # 秒就会记 3 格——刻度 3 秒时等于把账放大三倍。
        bs = _clamp_bucket(_bucket_of(v * 60 if gran == "m" else v, unit), unit, now)
        if bs is None:
            continue
        ok.append(bs)
        cur = db.execute("INSERT OR IGNORE INTO usage_min(uid,m,src,ts) VALUES(?,?,?,?)",
                         (_subj_id(uid), bs, src, time.time()))
        if cur.rowcount:
            k = "use:%s:d:%s" % (subject_of(db, uid), _day_of_ts(bs))
            if k not in total:
                total[k] = usage_get(db, k)
            if cap and total[k] >= cap:
                charged += unit
            total[k] += unit
        # 归属到具体开发机（只影响查账，不影响计费）：同一分钟只写一次
        mk = bs // 60
        if dev and mk not in touched:
            touched.add(mk)
            use_client_touch(db, uid, mk, "d:" + str(dev)[:24], "host",
                             (dev_name or str(dev)[:12])[:60], "")
            # 这一分钟中继没见过任何客户端（纯直连/断链补报）：标成「开发机直连」，
            # 管理后台查账时至少知道不是凭空多出来的。
            if not use_client_has(db, uid, mk, clients_only=True):
                use_client_touch(db, uid, mk, "direct", "host",
                                 "开发机直连/离线补报（LAN·P2P）", "")
    for k, n in total.items():
        db.execute("INSERT INTO usage(k,n) VALUES(?,?) "
                   "ON CONFLICT(k) DO UPDATE SET n=excluded.n", (k, n))
    if charged:
        use_credit_add(db, uid, -charged, "charge", "超出套餐额度（开发机补报）")
    db.commit()
    return ok, use_used(db, uid), use_cap_eff(db, uid)


_PRUNE_AT = 0.0


def use_client_touch(db, uid, m, key, kind="", label="", ua=""):
    """记「这个分钟是哪台终端用掉的」。只用于查账，不影响计费（分钟仍按 uid 幂等）。
    同一分钟同一终端多次请求只累加 n。"""
    now = time.time()
    db.execute(
        "INSERT INTO usage_client(uid,m,k,kind,label,ua,n,first,last) "
        "VALUES(?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(uid,m,k) DO UPDATE SET n=n+1, last=excluded.last, "
        "label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE label END",
        (_subj_id(uid), int(m), key[:80], (kind or "")[:12], (label or "")[:60],
         # 120 会把 CodebossShell/x.y.z 截掉（标准 WebView UA 就 150+ 字符，版本号在最后），
         # 后台「终端」页因此看不到安卓机版本。留够长度，版本号在不在尾部都读得到。
         (ua or "")[:400], now, now))


# ---- 隧道流量计数 ----
# 每个经过中继转发的请求/回包都记一笔（直连不经过这里，所以这是「真吃 VPS 带宽」的量）。
# 先攒在内存里，每 30 秒落一次库：每个请求都写库会把 SQLite 打成瓶颈，而这是统计不是账单。
# 流量分类：product=用户使用产品（隧道 + 客户端/安装包下载）；ops=运维（发版上传/OSS 镜像/备份）
_TRF = {}       # (day, hour, kind) -> [请求数, 上行字节, 下行字节]

def traffic_add(kind, nin=0, nout=0, n=0):
    t = time.localtime()
    a = _TRF.setdefault((time.strftime("%Y-%m-%d", t), t.tm_hour, kind), [0, 0, 0])
    a[0] += n
    a[1] += int(nin or 0)
    a[2] += int(nout or 0)


_PBY = {}       # (day, hour, uid, cid, cls) -> [字节, 请求数]

def _pclass(path):
    p = str(path or "")
    if p.startswith("/static/"):
        return "静态资源"
    if p.startswith("/api/ext_live") or p.startswith("/api/ext_snap") or \
       p.startswith("/api/ext_screen") or p.startswith("/api/ext_stream/"):
        return "画面/实时"
    if p.startswith("/api/ext_push"):
        return "推送流"
    if p.startswith("/api/ext_msg"):
        return "消息历史"
    if p.startswith("/api/ext_send") or p.startswith("/api/ext_key"):
        return "发送/按键"
    if p.startswith("/api/ext_term_in"):
        return "终端输入"
    if p.startswith("/api/ext_model") or p.startswith("/api/ext_scan") or \
       p.startswith("/api/ext_cmd") or p == "/api/plan" or \
       p.startswith("/api/notify_hist"):
        return "模型/命令"
    if p.startswith("/api/ptt"):
        return "语音转写"
    if p.startswith("/r/tts"):
        return "语音合成"
    if p in ("/", "/d") or p.startswith("/ext/") or p.startswith("/s/"):
        return "页面"
    return "其他"


# 这些类别是「既能走直连、也能走中继」的接口（页面/静态/语音/终端输入只能走中继）
_ELIG_CLS = {"画面/实时", "消息历史", "发送/按键", "模型/命令", "推送流"}


def path_bytes_add(path, uid, cid, n, req=1):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return
    if n <= 0 and not req:
        return
    t = time.localtime()
    k = (time.strftime("%Y-%m-%d", t), t.tm_hour, int(uid or 0),
         str(cid or "")[:64], _pclass(path))
    a = _PBY.setdefault(k, [0, 0])
    a[0] += n
    a[1] += int(req or 0)


_TNL = {}       # (day, hour) -> [请求数, 上行字节, 下行字节]

def tunnel_add(nin=0, nout=0, n=0):
    t = time.localtime()
    a = _TNL.setdefault((time.strftime("%Y-%m-%d", t), t.tm_hour), [0, 0, 0])
    a[0] += n
    a[1] += int(nin or 0)
    a[2] += int(nout or 0)
    traffic_add("product", nin, nout, n)   # 隧道 = 用户使用产品的流量


def tunnel_flush(db):
    if not _TNL and not _TRF:
        return
    items = list(_TNL.items())
    _TNL.clear()
    try:
        day_sum = {}
        for (day, hour), (n, bi, bo) in items:
            db.execute(
                "INSERT INTO tunnel_hour(day,hour,req_n,bytes_in,bytes_out) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day,hour) DO UPDATE SET req_n=req_n+excluded.req_n,"
                " bytes_in=bytes_in+excluded.bytes_in, bytes_out=bytes_out+excluded.bytes_out",
                (day, hour, n, bi, bo))
            d = day_sum.setdefault(day, [0, 0, 0])
            d[0] += n
            d[1] += bi
            d[2] += bo
        for day, (n, bi, bo) in day_sum.items():
            db.execute(
                "INSERT INTO tunnel_stat(day,req_n,bytes_in,bytes_out) VALUES(?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET req_n=req_n+excluded.req_n,"
                " bytes_in=bytes_in+excluded.bytes_in, bytes_out=bytes_out+excluded.bytes_out",
                (day, n, bi, bo))
        pitems = list(_PBY.items())
        _PBY.clear()
        for (day, hour, uid, cid, cls), (nb, rq) in pitems:
            db.execute("INSERT INTO bytes_path(day,hour,uid,cid,cls,bytes,req)"
                       " VALUES(?,?,?,?,?,?,?) ON CONFLICT(day,hour,uid,cid,cls)"
                       " DO UPDATE SET bytes=bytes+excluded.bytes, req=req+excluded.req",
                       (day, hour, uid, cid, cls, nb, rq))
        titems = list(_TRF.items())
        _TRF.clear()
        for (day, hour, kind), (n, bi, bo) in titems:
            db.execute(
                "INSERT INTO traffic_hour(day,hour,kind,req_n,bytes_in,bytes_out)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(day,hour,kind) DO UPDATE SET"
                " req_n=req_n+excluded.req_n, bytes_in=bytes_in+excluded.bytes_in,"
                " bytes_out=bytes_out+excluded.bytes_out",
                (day, hour, kind, n, bi, bo))
        db.commit()
    except Exception as e:
        print("[relay] tunnel_stat 落库失败：%s" % str(e)[:120], flush=True)


async def _tunnel_shutdown(app):
    try:
        tunnel_flush(_db(app))
    except Exception:
        pass


async def _tunnel_flush_loop(app):
    while True:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            tunnel_flush(_db(app))
            raise
        try:
            tunnel_flush(_db(app))
            if _ACT_DIRTY[0]:          # 读请求的动作记录攒着，跟流量统计一起落盘
                _db(app).commit()
                _ACT_DIRTY[0] = False
        except Exception as e:
            print("[relay] tunnel flush: %s" % str(e)[:120], flush=True)


_ACT_DIRTY = [False]


def use_act(db, uid, m, src, dev="", cid="", ua="", kind="", act="", path="", n=1):
    """记一条「这一分钟做了什么」。同一个 (分钟, 来源, 设备, 客户端, 动作, 路径) 只一行。
    src=relay（中继亲眼看到）逐次 +1；src=host（开发机补报直连）用 MAX——
    开发机每 5 秒重发一次未确认的累计值，MAX 保证重发不会把次数越加越大。"""
    now = time.time()
    n = max(1, int(n or 1))
    if src == "host":
        db.execute(
            "INSERT INTO use_act_log(uid,m,src,dev,cid,ua,kind,act,path,n,first,last) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(uid,m,src,dev,cid,act,path) DO UPDATE SET "
            "n=MAX(n,excluded.n), last=excluded.last",
            (_subj_id(uid), int(m), src, (dev or "")[:24], (cid or "")[:64], (ua or "")[:120],
             (kind or "")[:8], (act or "")[:60], (path or "")[:80], n, now, now))
    else:
        db.execute(
            "INSERT INTO use_act_log(uid,m,src,dev,cid,ua,kind,act,path,n,first,last) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(uid,m,src,dev,cid,act,path) DO UPDATE SET "
            "n=n+excluded.n, last=excluded.last",
            (_subj_id(uid), int(m), src, (dev or "")[:24], (cid or "")[:64], (ua or "")[:120],
             (kind or "")[:8], (act or "")[:60], (path or "")[:80], n, now, now))


def use_client_has(db, uid, m, clients_only=False):
    """这一分钟中继见过终端吗？clients_only=True 时只看客户端行（kind<>'host'），
    开发机自己报上来的那几行不算——否则「直连补报」永远判不出来。"""
    if clients_only:
        return db.execute("SELECT 1 FROM usage_client WHERE uid=? AND m=? "
                          "AND kind<>'host' LIMIT 1", (_subj_id(uid), int(m))).fetchone() is not None
    return db.execute("SELECT 1 FROM usage_client WHERE uid=? AND m=? LIMIT 1",
                      (_subj_id(uid), int(m))).fetchone() is not None


# ---------- 功能使用埋点 ----------
# 页面每个功能入口 track('事件名', '明细')，攒批发到 /api/track；这里按「天×产品线×
# 事件×页面×明细×版本×平台×账号×设备」聚合成计数（feat_day）。
# pg = 当时在哪个页面（home/sess/set/term/sub:xxx），k = 点了哪一项/切到哪个值
# （界面上的名字，可以是中文）。不记内容、路径、会话标题——明细是「按钮名」不是「用户说什么」。
# 页面停留：ev=visit（进入次数）+ ev=dwell（n=秒），平均停留 = dwell/visit。
# 400 天足够看年度趋势；一天最多「事件数×细项数×人数」行，比逐条日志小几个量级。
_FEAT_EV = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FEAT_PG = re.compile(r"^[a-z][a-z0-9_:.-]{0,23}$")
# 细项：界面上给人看的中文/英文标签，挡掉控制字符和尖括号（别让 HTML 混进报表）
_FEAT_K = re.compile(r"^[^\x00-\x1f<>]{1,24}$")
_FEAT_MAX = 50
_FEAT_KEEP_DAYS = 400
_FEAT_DDL = ("CREATE TABLE IF NOT EXISTS feat_day("
             "day TEXT NOT NULL, prod TEXT NOT NULL DEFAULT '', "
             "ev TEXT NOT NULL, ver TEXT NOT NULL DEFAULT '', "
             "plat TEXT NOT NULL DEFAULT '', uid INTEGER NOT NULL DEFAULT 0, "
             "cid TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0, "
             "ts REAL NOT NULL DEFAULT 0, "
             "pg TEXT NOT NULL DEFAULT '', k TEXT NOT NULL DEFAULT '', "
             "PRIMARY KEY(day, prod, ev, pg, k, ver, plat, uid, cid))")


def _feat_day_migrate(db):
    """老库的 feat_day 只有事件名，没有页面/明细两维，主键也不含水。

    SQLite 改不了主键，只能重建：旧行原样搬过去，pg/k 留空（历史数据就当
    「没有页面维度的老事件」）。幂等：有 pg/k 就直接返回。"""
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(feat_day)").fetchall()}
    except sqlite3.OperationalError:
        return
    if "pg" in cols and "k" in cols:
        return
    db.execute("ALTER TABLE feat_day RENAME TO feat_day_old")
    db.execute(_FEAT_DDL)
    db.execute("INSERT INTO feat_day(day,prod,ev,ver,plat,uid,cid,n,ts) "
               "SELECT day,prod,ev,ver,plat,uid,cid,n,ts FROM feat_day_old")
    db.execute("DROP TABLE feat_day_old")


def use_prune(db):
    """每天清一次旧的窗口行（幂等表只在补报窗口内有意义）。"""
    global _PRUNE_AT
    now = time.time()
    if now - _PRUNE_AT < 3600:
        return
    _PRUNE_AT = now
    # usage_min.m 现在是「窗口起点（epoch 秒）」。旧的分钟序号数值小得多，一并被这条清掉。
    db.execute("DELETE FROM usage_min WHERE m < ?", (int(now) - 3 * 86400,))
    # usage_client 是查账明细（哪台客户端/开发机用掉的），后台要展示近 7 天和近 14 天，
    # 只留 3 天的话那两列会悄悄少算。行数不大（每分钟每终端一行），留 15 天。
    db.execute("DELETE FROM usage_client WHERE m < ?", (_min_now() - 15 * 24 * 60,))
    # 动作记录只留 3 天：查账看的是「今天/昨天到底做了什么」，留久了行数涨得快
    db.execute("DELETE FROM use_act_log WHERE m < ?", (_min_now() - 3 * 24 * 60,))
    # 时长包流水：扣费一分钟一行，一天最多几百行；留 120 天足够查账
    db.execute("DELETE FROM use_credit_log WHERE ts < ?", (now - 120 * 86400,))
    # 功能使用埋点：按天聚合，留 400 天
    db.execute("DELETE FROM feat_day WHERE day < ?",
               (time.strftime("%Y-%m-%d",
                              time.localtime(now - _FEAT_KEEP_DAYS * 86400)),))
    db.commit()


def _track_ua(ua):
    """UA → (壳版本, 平台)。库里只有三档平台：android / ios / web。"""
    ua = (ua or "")[:300]
    m = re.search(r"Shell/([0-9][0-9.]{0,15})", ua)
    ver = m.group(1).rstrip(".") if m else ""
    if re.search(r"iPhone|iPad|iPod", ua):
        plat = "ios"
    elif "Android" in ua:
        plat = "android"
    else:
        plat = "web"
    return ver, plat


def _track_add(app, uid, cid, prod, ver, plat, evs):
    """事件入表（/api/track 与设备转发共用）：白名单、截断、按天聚合累加。

    每条可以是 `"事件"`、`["事件", n]`（老格式）、`["事件", 明细, 页面, n]`。
    事件名不合白名单整条丢；明细/页面不合法只丢那一维，计数照记（老版本页面
    还在发老格式，新版本多两维，两边都要收）。埋点是观测，不许影响别的。"""
    add = {}
    for x in evs[:_FEAT_MAX]:
        if isinstance(x, str):
            name, k, pg, n = x, "", "", 1
        elif isinstance(x, list) and x:
            name, k, pg, n = x[0], "", "", 1
            if len(x) >= 4:
                k, pg, n = x[1], x[2], x[3]
            elif len(x) == 3:
                k, pg = x[1], x[2]
            elif len(x) == 2:
                n = x[1]
        else:
            continue
        if not isinstance(name, str) or not _FEAT_EV.match(name):
            continue
        k = k.strip() if isinstance(k, str) else ""
        if not _FEAT_K.match(k):
            k = ""
        if not isinstance(pg, str) or not _FEAT_PG.match(pg):
            pg = ""
        try:
            n = int(n)
        except Exception:
            n = 1
        key = (name, pg, k)
        add[key] = add.get(key, 0) + max(1, min(n, 1000))
    if not add:
        return 0
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    db = _db(app)
    db.executemany(
        "INSERT INTO feat_day(day,prod,ev,pg,k,ver,plat,uid,cid,n,ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day,prod,ev,pg,k,ver,plat,uid,cid) "
        "DO UPDATE SET n=n+excluded.n, ts=excluded.ts",
        [(day, prod, ev, pg, k, ver, plat, uid, cid, n, now)
         for (ev, pg, k), n in add.items()])
    db.commit()
    return len(add)


def _track_ctx(request):
    """埋点上下文：谁、哪条产品线、壳版本、什么平台。页面只报事件名。

    产品线/版本/平台一律从 cookie + UA 读，不让页面自报——外面随手传一个
    product 就能把报表刷花，那报表就没人信了。"""
    try:
        uid = int(uid_of(request) or 0)
    except Exception:
        uid = 0
    cid = _client_key(request)
    want = (request.headers.get("X-Product") or request.cookies.get("cb_product")
            or "").strip().lower()
    prod = branding.valid_product(want) if want else branding.PRODUCT_DEFAULT
    ver, plat = _track_ua(request.headers.get("User-Agent"))
    return uid, cid, prod, ver, plat


async def h_track(request):
    """功能使用埋点（POST /api/track）：{"e":[["send_msg",1],["key",3]]}。

    身份/产品线/版本/平台都从 cookie+UA 读；同一天同一人同一事件累加 n。"""
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    evs = d.get("e") if isinstance(d, dict) else None
    if not isinstance(evs, list) or not evs:
        return web.json_response({"ok": False}, status=400)
    uid, cid, prod, ver, plat = _track_ctx(request)
    return web.json_response(
        {"ok": True, "n": _track_add(request.app, uid, cid, prod, ver, plat, evs)})


async def r_dev_track(request):
    """开发机替局域网直连的页面转埋点（POST /r/dev_track）。

    页面源在 host 上（家里 WiFi 直连），/api/track 不在中继同源，发不到这里；
    host 用本机设备凭证把它转过来。身份只有设备：账号按 devices.owner 认，
    设备维度用 d:<dev>。直连时页面拿不到 relay 用户会话，同一台机在两条路上
    会各算一次设备——宁可多算一次，也不把游客算成账号。"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    if not rate_ok(request.remote or "?", "devtrk", 5000):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    evs = data.get("e")
    if not isinstance(evs, list) or not evs:
        return web.json_response({"ok": False}, status=400)
    row = db.execute("SELECT product FROM devices WHERE id=?", (dev,)).fetchone()
    want = str(data.get("product") or "").strip().lower()
    prod = branding.valid_product(want if want in branding.PRODUCTS
                                  else (row[0] if row else None))
    ver, plat = _track_ua(str(data.get("ua") or ""))
    return web.json_response({"ok": True, "n": _track_add(
        request.app, int(owner or 0), "d:" + dev, prod, ver, plat, evs)})


# 免费版不含命令终端：终端页面/接口全部挡掉。新建 agent 会话不挡（免费版也要能开工）。
# 2026-11-09 起「识别命令」（ext_scan）也归到这里：它要调大模型，免费版不具备。
_TERM_PATHS = ("/api/terms", "/api/term_new", "/api/term_close", "/api/term_mac",
               "/api/ext_term_out", "/api/ext_term_in", "/new",
               "/api/ext_sess_fork")


def is_term_path(path):
    return (path in _TERM_PATHS or path.startswith("/s/") or path.startswith("/respawn/")
            or path.startswith("/api/ext_cmdrun/") or path.startswith("/api/ext_scan/"))


# 客户端名额的活跃窗口：24 小时内连过才算占一个名额。
# 超过一天没连接的客户端自动释放——重装/换机/旧手机丢了不必再等 30 天或找客服；
# 同一台手机重新连上会立刻重新占位（clients_touch 写回 last）。
CLIENT_SEAT_SEC = 24 * 3600


def clients_used(db, uid, sec=CLIENT_SEAT_SEC):
    """占了多少个客户端名额：按「设备」去重，不按随机 cid
    （壳的 WebView cookie 会丢、每次重新生成 cb_cid；后台提醒流没有 cookie）。
    老行没有 dev 的仍按 cid 各算一个，等下次连上自动补。"""
    return db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT COALESCE(NULLIF(dev,''), cid) AS k"
        " FROM clients WHERE uid=? AND last>? AND NOT (" + _BG_ROW_SQL + "))",
        (_subj_id(uid), time.time() - sec)).fetchone()[0]


# 后台提醒通道的兜底行（BgService 没有 cookie，按 i:IP|UA 记下来）：不是客户端，
# 不计名额、不进列表。UA 很短，c:「i:127.0.0.1|CodebossShell/0.11.31 bg」整段都在。
_BG_ROW_SQL = "cid LIKE 'i:%|CodebossShell/% bg'"


def _ua_dev(ua):
    """设备指纹（机型级）：名额按它去重。版本号/随机 cid 会变，机型不会。
    读不出机型返回 ""（调用方保持旧的按 cid 记）。"""
    ua = ua or ""
    m = re.search(r"Android[^;)]*;\s*([^;)]+)", ua)
    if m:
        model = re.sub(r"\s+Build/.*$", "", m.group(1).strip()).strip()
        return "android|" + (model or "?")
    if "iPhone" in ua or "iPad" in ua:
        return "ios|iphone"
    if "Macintosh" in ua:
        return "mac"
    if "Windows" in ua:
        return "win"
    if "Linux" in ua:
        return "linux"
    return ""


_CID_MEM = {}


_CLI_GATE = {}   # (uid, cid, dev) → (放行?, 判定时刻, 当时已用数)


def clients_gate(db, uid, cid, dev=""):
    """客户端数上限：挡第 cap+1 个。返回 None=放行，超额返回 (used, cap)。

    口径和计数一致（24 小时内来过、按设备去重）。三个原则：
      · 已经算在额度里的客户端（同一台设备的新 cid 也算）永远放行 ——
        加规则不能把老客户端踢下线，也不能因为壳丢了 cookie 就把自己挡在外面；
      · 判定结果缓 60 秒 —— 用户开通 Pro 后不必等太久立刻生效。
    """
    cap = int(plan_limits(db, uid).get("clients") or 0)
    if not cap:
        return None
    k = (_subj_id(uid), cid, dev)
    now = time.time()
    hit = _CLI_GATE.get(k)
    if hit and now - hit[1] < 60:
        return None if hit[0] else (hit[2], cap)
    known = db.execute("SELECT 1 FROM clients WHERE uid=? AND cid=? AND last>?",
                       (_subj_id(uid), cid, now - CLIENT_SEAT_SEC)).fetchone() is not None
    if not known and dev:
        known = db.execute("SELECT 1 FROM clients WHERE uid=? AND dev=? AND last>?",
                           (_subj_id(uid), dev, now - CLIENT_SEAT_SEC)).fetchone() is not None
    if known:
        _CLI_GATE[k] = (True, now, 0)
        return None
    used = clients_used(db, uid)
    if used < cap:
        _CLI_GATE[k] = (True, now, 0)
        return None
    _CLI_GATE[k] = (False, now, used)
    return (used, cap)


def _owner_clients(db, uid, cur_cid=""):
    """账号自己的客户端列表（给手机 App 的「账号管理」用）。
    同一台设备（dev 相同）只出现一次，代表件取最近活跃的那行——
    壳丢 cookie 换了 cid 还是这台手机，列表里不该多一条、也不该多占名额。"""
    now = time.time()
    rows = db.execute("SELECT cid, dev, first, last FROM clients WHERE uid=?"
                      " AND NOT (" + _BG_ROW_SQL + ") ORDER BY last DESC",
                      (int(uid),)).fetchall()
    groups = {}
    for cid, dev, first, last in rows:
        gk = dev or cid
        g = groups.get(gk)
        if g is None:
            groups[gk] = {"cid": cid or "", "dev": dev or "", "first": first,
                          "last": last, "cur": str(cid) == cur_cid}
        elif str(cid) == cur_cid:
            g["cur"] = True
        elif first and (not g["first"] or float(first) < float(g["first"])):
            g["first"] = first
    out = []
    for cid, g in [(g["cid"], g) for g in groups.values()]:
        label, kind = "", ""
        try:
            r = db.execute("SELECT label, ua FROM usage_client WHERE uid=? AND k=?"
                           " ORDER BY last DESC LIMIT 1",
                           (int(uid), _use_key(cid))).fetchone()
            if r and (r[1] or ""):
                label, kind = _ua_label(r[1])
            elif r:
                label = r[0] or ""
        except Exception:
            pass
        out.append({"cid": cid, "label": label or (str(cid) or "")[:8],
                    "kind": kind or "app",
                    "first": float(g["first"] or 0), "last": float(g["last"] or 0),
                    "counted": bool(g["last"] and float(g["last"]) >= now - CLIENT_SEAT_SEC),
                    "current": g["cur"]})
    out.sort(key=lambda c: -(c["last"] or 0))
    return {"clients": out, "used": clients_used(db, uid),
            "cap": int(plan_limits(db, uid).get("clients") or 0)}


def usage_bytes_add(db, uid, m, kind, bin=0, bout=0, n=1, cid="", dev=""):
    """记一次请求的字节（用户+分钟+类型+客户端+开发机）。只用于统计，不参与计费。"""
    try:
        db.execute("INSERT INTO usage_bytes2(uid,m,k,cid,dev,bytes_in,bytes_out,req_n)"
                   " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uid,m,k,cid,dev) DO UPDATE SET"
                   " bytes_in=bytes_in+excluded.bytes_in,"
                   " bytes_out=bytes_out+excluded.bytes_out, req_n=req_n+excluded.req_n",
                   (_subj_id(uid), int(m), kind, str(cid or "")[:64], str(dev or "")[:64],
                    int(bin or 0), int(bout or 0), int(n or 0)))
        db.commit()
        if int(m) % 60 == 0:   # 每小时清一次 15 天前的
            db.execute("DELETE FROM usage_bytes2 WHERE m<?", (int(m) - 15 * 1440,))
            db.commit()
    except Exception:
        pass


def clients_touch(db, uid, cid, dev="", memo_sec=3600, kind=""):
    """记「哪个客户端来过」——客户端数收费靠它统计；同 cid 一小时只写一次库
    （浏览器要按 30 分钟闲置踢人，调用方传更短的 memo_sec 让 last 跟得上）。

    有设备指纹时一台设备只留一行：cid 刷新成这次见到的（壳丢 cookie/升版就换
    cid 不能多占名额）。行上的 cid 跟着设备走，踢下线也是踢这台设备。"""
    k = (_subj_id(uid), cid)
    now = time.time()
    if now - _CID_MEM.get(k, 0) < memo_sec:
        return
    _CID_MEM[k] = now
    if dev:
        row = db.execute("SELECT cid FROM clients WHERE uid=? AND dev=? AND last>?"
                         " ORDER BY last DESC LIMIT 1",
                         (_subj_id(uid), dev, now - CLIENT_SEAT_SEC)).fetchone()
        if row and row[0] != cid:
            db.execute("DELETE FROM clients WHERE uid=? AND cid=?", (_subj_id(uid), cid))
            db.execute("DELETE FROM clients WHERE uid=? AND dev=? AND cid<>?",
                       (_subj_id(uid), dev, row[0]))
            db.execute("UPDATE clients SET cid=?, last=?, kind=? WHERE uid=? AND cid=?",
                       (cid, now, kind, _subj_id(uid), row[0]))
            db.commit()
            return
    db.execute("INSERT INTO clients(uid,cid,dev,first,last,kind) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(uid,cid) DO UPDATE SET last=excluded.last, dev=excluded.dev,"
               " kind=excluded.kind",
               (_subj_id(uid), cid, dev, now, now, kind))
    db.commit()


def _delete_client(db, uid, cid):
    """踢一台客户端：知道设备指纹就把这台的所有 cid 行一起删（壳丢过 cookie 会
    留下兄弟行，只删一行等于名额没还），不知道就只删这一行。"""
    uid, cid = int(uid), str(cid or "")
    row = db.execute("SELECT dev FROM clients WHERE uid=? AND cid=?", (uid, cid)).fetchone()
    dev = (row[0] or "") if row else ""
    cids = ([r[0] for r in db.execute("SELECT cid FROM clients WHERE uid=? AND dev=?",
                                      (uid, dev))] if dev else [cid])
    now = time.time()
    for c in cids:
        db.execute("DELETE FROM clients WHERE uid=? AND cid=?", (uid, c))
        # 只删行不够：手机上的 r_sess 还活着，下一次请求又自动登记回来，
        # 用户看到的是「移除了没反应」。记一笔，带旧 cookie 来按未登录处理。
        db.execute("INSERT INTO client_kick(cid,ts) VALUES(?,?)"
                   " ON CONFLICT(cid) DO UPDATE SET ts=excluded.ts", (c, now))
        _CID_MEM.pop((uid, c), None)
    db.commit()
    return len(cids)


def _is_browser_ua(ua):
    """浏览器（桌面/手机浏览器，不是壳的 WebView/Dalvik 原生请求）。
    算客户端、占名额，但闲置 BROWSER_IDLE_SEC 就踢掉释放名额（见 _browser_sweep）。"""
    return _ua_kind(ua or "") == "web"


# 浏览器闲置多久踢下线：占着名额不用是浪费，但踢太勤又要天天重登（用户口径 30 分钟）。
BROWSER_IDLE_SEC = 30 * 60


def _cid_is_browser(db, uid, cid):
    """这一行客户端是不是浏览器：cid 自带 UA 的直接认，裸 cid 按 usage_client 记的 UA 认。"""
    c = str(cid or "")
    try:
        r = db.execute("SELECT kind FROM clients WHERE uid=? AND cid=?",
                       (_subj_id(uid), c)).fetchone()
        if r and r[0] in ("web", "app"):
            return r[0] == "web"
    except Exception:
        pass
    if c.startswith("i:"):
        return _is_browser_ua(c.split("|", 1)[-1])
    try:
        row = db.execute("SELECT ua FROM usage_client WHERE uid=? AND k=?"
                         " ORDER BY last DESC LIMIT 1",
                         (_subj_id(uid), _use_key(c))).fetchone()
    except Exception:
        return False
    return bool(row and _is_browser_ua(row[0] or ""))


def _browser_sweep(db, now=None):
    """踢掉闲置的浏览器客户端：名额当场放回去，并记下 cid 让它下次必须重新登录。
    返回踢掉的行数。App/壳的行不参与（它们按 CLIENT_SEAT_SEC 24 小时自然释放）。"""
    now = now or time.time()
    n = 0
    try:
        rows = db.execute("SELECT uid, cid FROM clients WHERE last < ?",
                          (now - BROWSER_IDLE_SEC,)).fetchall()
        for uid_, cid_ in rows:
            if not _cid_is_browser(db, uid_, cid_):
                continue
            db.execute("INSERT INTO client_kick(cid,ts) VALUES(?,?)"
                       " ON CONFLICT(cid) DO UPDATE SET ts=excluded.ts", (cid_, now))
            db.execute("DELETE FROM clients WHERE uid=? AND cid=?", (uid_, cid_))
            # 列表也别留半截：后台的终端页是按 usage_client 拼的，明细不删它还在
            db.execute("DELETE FROM usage_client WHERE uid=? AND k=?",
                       (uid_, _use_key(cid_)))
            _CID_MEM.pop((uid_, cid_), None)
            n += 1
        # 记录只留 7 天（最长登录态就是 7 天，再久的记录没意义）
        db.execute("DELETE FROM client_kick WHERE ts < ?", (now - 7 * 86400,))
        db.commit()
    except Exception:
        pass
    return n


def _seat_wall(request, db, suid, exempt=False):
    """客户端名额闸门：返回 None=放行，超额 (used, cap)。放行时顺手登记。

    后台提醒/播报流（exempt）不占名额：它们是同一台手机的后台通道，不是客户端，
    按 IP+UA 各记一条会把一台手机记成好几台（2026-11-21 中兴被自己的预览挡住）。
    浏览器照常登记占名额，闲置由 _browser_sweep 负责踢。"""
    if exempt:
        return None
    cid = (request.cookies.get("cb_cid") or "").strip()
    seat = cid if (cid and len(cid) <= 64) else _client_key(request)
    ua = request.headers.get("User-Agent") or ""
    browser = _is_browser_ua(ua)
    dev = _ua_dev(ua)
    gate = clients_gate(db, suid, seat, dev)
    if gate:
        return gate
    # 浏览器 30 分钟没动静就要踢：last 得跟着刷，不能等一小时的写库节流
    clients_touch(db, suid, seat, dev, memo_sec=(120 if browser else 3600),
                  kind=("web" if browser else "app"))
    return None


# ---------- 收款（Z-PAY / 易支付协议，文档：z-pay.cn/doc.html） ----------
ZPAY_API = (os.environ.get("ZPAY_API") or "https://zpayz.cn").rstrip("/")
ZPAY_PID = (os.environ.get("ZPAY_PID") or "").strip()
ZPAY_KEY = (os.environ.get("ZPAY_KEY") or "").strip()
# 价格（元）与时长（天，包=0 表示不续期而是充使用时长分钟）。改价走环境变量，不动代码。
PAY_SKUS = {
    "trial": ("AgentBoss Pro 体验月", os.environ.get("ZPAY_PRICE_TRIAL") or "6.90", 30),
    "month": ("AgentBoss Pro 月付", os.environ.get("ZPAY_PRICE_MONTH") or "19.00", 30),
    "quarter": ("AgentBoss Pro 季付", os.environ.get("ZPAY_PRICE_QUARTER") or "49.00", 91),
    "year": ("AgentBoss Pro 年付", os.environ.get("ZPAY_PRICE_YEAR") or "129.00", 365),
    "max_month": ("AgentBoss Max 月付", os.environ.get("ZPAY_PRICE_MAX_MONTH") or "39.00", 30),
    "max_quarter": ("AgentBoss Max 季付", os.environ.get("ZPAY_PRICE_MAX_QUARTER") or "99.00", 91),
    "max_year": ("AgentBoss Max 年付", os.environ.get("ZPAY_PRICE_MAX_YEAR") or "269.00", 365),
    # 按量付费：使用时长包（6/12/24/48/96/192 小时），不过期；当天套餐额度用完后接着用。
    # 单价刻意高于订阅折算（Pro 月付按用满 360 分钟/天算约 0.0014 元/分钟）：
    # 包是给偶尔补量/不想订阅的人的，别让包替代订阅。
    "pack_s": ("使用时长包 6 小时", os.environ.get("ZPAY_PRICE_PACK_S") or "5.00", 0),
    "pack_m": ("使用时长包 12 小时", os.environ.get("ZPAY_PRICE_PACK_M") or "8.80", 0),
    "pack_l": ("使用时长包 24 小时", os.environ.get("ZPAY_PRICE_PACK_L") or "15.80", 0),
    "pack_x": ("使用时长包 48 小时", os.environ.get("ZPAY_PRICE_PACK_X") or "27.80", 0),
    "pack_y": ("使用时长包 96 小时", os.environ.get("ZPAY_PRICE_PACK_Y") or "49.80", 0),
    "pack_z": ("使用时长包 192 小时", os.environ.get("ZPAY_PRICE_PACK_Z") or "88.80", 0),
}
# 时长包内置默认档（分钟）。plans.json 的 "packs" 可以整表替换它：后台能自由增删档位。
PACK_MIN = {"pack_s": 6 * 60, "pack_m": 12 * 60, "pack_l": 24 * 60,
            "pack_x": 48 * 60, "pack_y": 96 * 60, "pack_z": 192 * 60}


def _pack_ok(p):
    """一档时长包是否合法（后台填错就跳过这档，别把整张价格表带崩）。"""
    try:
        mins, money = int(p.get("min") or 0), float(p.get("money") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    return (isinstance(p, dict) and bool(re.match(r"^[a-z][a-z0-9_]{1,23}$", str(p.get("sku") or "")))
            and bool(str(p.get("name") or "").strip()) and mins > 0 and money >= 0)


def packs():
    """使用时长包档位表。plans.json 的 "packs" 说了算（可增删改），没配就用内置六档。
    每档 {sku,name,money,min}；**表里第一档就是「基础档」**，后面每档的折扣都以它为准，
    所以后台怎么增删，折扣都会自动跟着重算。"""
    ov = (plans() or {}).get("packs")
    out, seen = [], set()
    if isinstance(ov, list):
        for it in ov:
            # 内置包（pack_s/pack_m/…）允许在 plans.json 里改名/改价/改分钟数；
            # 只有撞上订阅商品（month/year/…）的 sku 才跳过
            if (not _pack_ok(it) or it["sku"] in seen
                    or (it["sku"] in PAY_SKUS and it["sku"] not in PACK_MIN)):
                continue
            seen.add(it["sku"])
            row = {"sku": it["sku"], "name": str(it["name"]).strip()[:40],
                   "money": "%.2f" % float(it["money"]), "min": int(it["min"])}
            if it.get("sell") is False:
                row["sell"] = False          # 内部包：后台可见可分配，前台不售卖
            out.append(row)
        if out:
            return out
    # 老配置兼容：没写 "packs" 时仍认 skus.pack_* 的名字/价格覆盖
    sku_ov = (plans() or {}).get("skus")
    sku_ov = sku_ov if isinstance(sku_ov, dict) else {}
    for k, mins in PACK_MIN.items():
        name, money, _ = PAY_SKUS[k]
        o = sku_ov.get(k)
        if isinstance(o, dict):
            name = str(o.get("name") or name)[:40]
            try:
                money = "%.2f" % float(o.get("money", money))
            except (TypeError, ValueError):
                money = "%.2f" % float(money)
        out.append({"sku": k, "name": name, "money": "%.2f" % float(money), "min": mins})
    return out


def pack_sold(sku):
    """这个时长包是否对外售卖。packs 里 "sell": false = 内部包：
    只在后台配置里看得到、能人工加时长，但不能下单买。没配 sell 视为售卖。"""
    for it in packs():
        if it["sku"] == sku:
            return it.get("sell") is not False
    return True


def pack_min():
    """{sku: 分钟数}，订单入账/额度都用它。"""
    return {p["sku"]: int(p["min"]) for p in packs()}


def pay_skus():
    """支付 SKU：环境变量给默认价，plans.json 里的 "skus" 可覆盖名称/价格/天数（管理页改价用）；
    时长包不在这里，统一由 packs() 出（后台可增删档位）。"""
    out = {k: v for k, v in PAY_SKUS.items() if k not in PACK_MIN}
    ov = (plans() or {}).get("skus") or {}
    if isinstance(ov, dict):
        for k, v in ov.items():
            if k not in out or not isinstance(v, dict):
                continue
            name, money, days = out[k]
            try:
                price = "%.2f" % float(v.get("money", money))
            except (TypeError, ValueError):
                price = money
            try:
                dy = int(v.get("days", days))
            except (TypeError, ValueError):
                dy = days
            out[k] = (str(v.get("name") or name)[:40], price, max(0, dy))
    for p in packs():                      # 时长包：天数恒为 0，分钟数在 pack_min() 里
        out[p["sku"]] = (p["name"], p["money"], 0)
    return out


def credits_of(db, uid):
    row = db.execute("SELECT sec FROM credits WHERE uid=?", (int(uid),)).fetchone()
    return int(row[0]) if row else 0


def credits_add(db, uid, sec):
    db.execute("INSERT INTO credits(uid,sec,updated) VALUES(?,?,?) "
               "ON CONFLICT(uid) DO UPDATE SET sec=MAX(0,sec+excluded.sec),"
               "updated=excluded.updated", (int(uid), int(sec), time.time()))
    db.commit()


def _zpay_sign(p):
    """ASCII 升序、去掉 sign/sign_type/空值，拼 a=b&c=d 后直接接商户 KEY 做小写 md5。"""
    items = sorted((str(k), str(v)) for k, v in p.items()
                   if str(k) not in ("sign", "sign_type") and v is not None and str(v) != "")
    return hashlib.md5(("&".join("%s=%s" % kv for kv in items) + ZPAY_KEY)
                       .encode("utf-8")).hexdigest()


def _zpay_post(path, params):
    import urllib.parse
    import urllib.request
    p = dict(params)
    p["sign"] = _zpay_sign(p)
    p["sign_type"] = "MD5"
    req = urllib.request.Request(ZPAY_API + path,
                                 data=urllib.parse.urlencode(p).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _zpay_query(otn):
    """订单二次确认（防假通知）。异常不打印 URL——里面带商户 key。"""
    import urllib.parse
    import urllib.request
    qs = urllib.parse.urlencode({"act": "order", "pid": ZPAY_PID,
                                 "key": ZPAY_KEY, "out_trade_no": otn})
    try:
        with urllib.request.urlopen(ZPAY_API + "/api.php?" + qs, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[pay] 订单二次确认失败: %s" % type(e).__name__, flush=True)
        return None

SMTP_HOST = os.environ.get("RELAY_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("RELAY_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("RELAY_SMTP_USER", "")
SMTP_PASS = os.environ.get("RELAY_SMTP_PASS", "")
SMTP_FROM = os.environ.get("RELAY_SMTP_FROM", "") or SMTP_USER
_MAIL_BG = set()      # 转到后台继续发的验证码任务（持引用，别被 GC 掉）

# 手机号验证码：阿里云「号码认证服务」短信认证（个人开发者免资质，免签名/模板申请）
# 控制台开通后取系统赠送的签名与模板编号；AccessKey 建议用 RAM 子账号。
ALI_AK_ID = os.environ.get("RELAY_ALI_AK_ID", "")
ALI_AK_SECRET = os.environ.get("RELAY_ALI_AK_SECRET", "")
ALI_SIGN_NAME = os.environ.get("RELAY_ALI_SIGN_NAME", "")
ALI_TEMPLATE = os.environ.get("RELAY_ALI_TEMPLATE_CODE", "")      # 登录/注册
ALI_TEMPLATE_RESET = os.environ.get("RELAY_ALI_TEMPLATE_RESET", "") or ALI_TEMPLATE
ALI_TEMPLATE_BIND = os.environ.get("RELAY_ALI_TEMPLATE_BIND", "") or ALI_TEMPLATE
# 用途 → 模板（控制台赠送模板：100001 登录/注册、100003 重置密码、100004 绑定新号）
ALI_ENDPOINT = os.environ.get("RELAY_ALI_ENDPOINT", "https://dypnsapi.aliyuncs.com")
SMS_CONSOLE = os.environ.get("RELAY_SMS_CONSOLE", "") == "1"   # 自建/测试：验证码打印到日志
SMS_ON = SMS_CONSOLE or bool(ALI_AK_ID and ALI_AK_SECRET and ALI_SIGN_NAME and ALI_TEMPLATE)


def caps_payload():
    """能力协商：页面/host 据此隐藏官方专属入口。键缺省=按官方（fail-open），
    所以只发 false 才有意义。**读调用时的全局**——ASR_KEY 会被 _model_apply 在
    启动时用后台 meta 覆盖（env 只是初值），这里现读现算，不许缓存。"""
    key = bool(ASR_KEY)
    return {
        "selfhost": bool(SELFHOST),
        # 注册通道：阿里云短信 or SMTP 邮箱任一可用即可完整注册/找回（builder 裁决 6）
        "register": bool(SMS_ON or SMTP_HOST),
        "llm": key, "asr": key,
        # TTS 是我们的品牌服务（Cherry）：自建一律不提供，key 存在也不亮（裁决 7）
        "tts": key and not SELFHOST,
        "plans": not SELFHOST, "promo": not SELFHOST,
        "apk": not SELFHOST, "ios": not SELFHOST, "admin": not SELFHOST,
    }


# 官方专属路径：自建模式一律 404。「藏起来但接口还在」是 spec 反目标，半点不含糊。
# devices/device 除外：自建机管理员的最低运维工具（RELAY_ADMIN_TOKEN 鉴权，无 token 即 401）。
_SELFHOST_KEEP = frozenset(("/r/admin_api/devices", "/r/admin_api/device"))
_OFFICIAL_ONLY = frozenset((
    "/r/plans", "/r/plans_ver", "/r/promo", "/r/tts",
    "/api/plan", "/api/pay_create", "/api/pay_status",
    "/r/pay/create", "/r/pay/notify", "/r/pay/status",
    "/api/apk_ver", "/api/ios_ver", "/api/host_ver", "/api/mac_ver",
    "/ios-install", "/ios.plist", "/codeboss.ipa",
    "/bosshost-mac.dmg", "/BossHost-Setup.exe", "/CodeBoss-Setup.exe"))


def _selfhost_gated(path):
    """自建模式下要 404 的路径。middleware 在所有路由（含 tunnel catch-all）之前跑，
    /codeboss.apk 这种 tunnel 内直答的一并盖住。"""
    if path in _SELFHOST_KEEP:
        return False
    if path.startswith("/r/admin"):     # 管理后台页 + admin_api 其余全部
        return True
    if path.startswith(("/codeboss-", "/codeboss.apk", "/agentboss.apk",
                        "/agentboss-", "/bosshost-", "/BossHost-", "/CodeBoss-")):
        return True                     # 分发直链：apk/ipa/dmg/linux/host 包（含 -{ver} 动态）
    return path in _OFFICIAL_ONLY


@web.middleware
async def _selfhost_gate(request, handler):
    if SELFHOST and _selfhost_gated(request.path):
        raise web.HTTPNotFound()
    return await handler(request)
SMS_TTL = 300          # 验证码有效期（秒）；阿里云侧与本地各记一份
SMS_COOLDOWN = int(os.environ.get("RELAY_SMS_COOLDOWN", "10"))   # 同号重发间隔（秒）
# 管理二次验证码的复用窗口：验证通过后这段时间内，同一个码可以继续做别的敏感操作。
# 阿里云同号短信有 60 秒频控，连着删几台设备时不该每台都去等一条新短信。
ADM_CODE_REUSE = int(os.environ.get("RELAY_ADM_SMS_REUSE", "300"))
# 一次短信验证 = 一小时通行证：验证通过后这段时间内做任何敏感操作都不再要码
# （删除/移除类除外，那些每次都要）。逐条验证（以前 5 分钟）在后台连着调版本/用户/
# 设备根本没法用。
ADM_STEPUP_TTL = int(os.environ.get("RELAY_ADM_STEPUP", "3600"))
# 管理员每做一次敏感操作都要重新验证，60 秒的间隔会把人卡在「等重发」上，所以默认 10 秒；
# 防刷靠下面的 rate_ok（同号 30 次/时、同 IP 30 次/时）和阿里云自己的频控。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOP_BY_HOP = {"transfer-encoding", "content-length", "connection", "keep-alive",
              "upgrade", "proxy-authorization", "te", "trailer"}


def _fwd_headers(request):
    """转发给开发机的头：跳掉逐跳头和 cookie，并把壳种的产品线 cookie 搬成 X-Product。

    产品线（codeboss/agentboss）决定能力面（哪些 agent tab 出现）。壳只能种 cookie
    （WebView 里的 XHR 发不了自定义头），而 cookie 不能整包转给开发机——以前直接剥掉，
    开发机只能回落到自己的产品线，codeboss 客户端于是看到 agentboss 的全量 tab
    （2026-11-18 macmini 实测）。只认两个合法值，别的当没带。"""
    h = {k: v for k, v in request.headers.items()
         if k.lower() not in HOP_BY_HOP | {"cookie", "host"}}
    if not any(k.lower() == "x-product" for k in h):
        pr = (request.cookies.get("cb_product") or "").strip().lower()
        if pr in ("codeboss", "agentboss"):
            h["X-Product"] = pr
    return h


# ---------- db ----------
def _db(app):
    return app["db"]


def db_init(path):
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
      pass_hash TEXT NOT NULL, salt TEXT NOT NULL, created REAL,
      sess_epoch INTEGER DEFAULT 0);   -- 改密码/踢下线时 +1，旧 cookie 立即作废
    CREATE TABLE IF NOT EXISTS devices(
      id TEXT PRIMARY KEY, name TEXT, secret TEXT NOT NULL,
      owner INTEGER, created REAL, last_seen REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pair_codes(
      code TEXT PRIMARY KEY, device_id TEXT NOT NULL,
      created REAL, expires REAL, used INTEGER DEFAULT 0);
    /* 接管请求：机器已在别的账号名下时，新账号不能拿码直接改归属——
       要原账号（手机 App）或机器本身（本机弹窗）确认才放行。 */
    CREATE TABLE IF NOT EXISTS takeover_req(
      id TEXT PRIMARY KEY, dev TEXT NOT NULL,
      owner_uid INTEGER NOT NULL, to_uid INTEGER NOT NULL,
      code TEXT NOT NULL, created REAL, expires REAL,
      status TEXT DEFAULT 'pending');   -- pending / approved / denied / expired
    CREATE TABLE IF NOT EXISTS email_codes(
      id INTEGER PRIMARY KEY, email TEXT NOT NULL, code TEXT NOT NULL,
      purpose TEXT NOT NULL, created REAL, expires REAL, used INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS orders(
      out_trade_no TEXT PRIMARY KEY, uid INTEGER NOT NULL, sku TEXT NOT NULL,
      money TEXT NOT NULL, status INTEGER DEFAULT 0, created REAL,
      paid REAL, trade_no TEXT, type TEXT);
    CREATE TABLE IF NOT EXISTS usage(k TEXT PRIMARY KEY, n INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS usage_min(       -- 使用时长幂等表：一个 uid 一个自然分钟只算一次
      uid INTEGER NOT NULL, m INTEGER NOT NULL,  -- m = epoch 分钟
      src TEXT NOT NULL, ts REAL,
      PRIMARY KEY(uid, m));
    /* 时长归属：同一分钟是哪台终端（客户端）用掉的——查账用，不影响计费。
       一个分钟里两台手机都在用就记两行；分钟的行数不改变已用分钟数。 */
    CREATE TABLE IF NOT EXISTS usage_client(
      uid INTEGER NOT NULL, m INTEGER NOT NULL, k TEXT NOT NULL,
      kind TEXT, label TEXT, ua TEXT, n INTEGER DEFAULT 0, first REAL, last REAL,
      PRIMARY KEY(uid, m, k));
    /* 隧道流量（按天）：中继替开发机转发/回传的字节数。直连不经过这里，所以
       「隧道流量」就是真金白银吃 VPS 带宽的那部分，和直连命中率一起看。 */
    CREATE TABLE IF NOT EXISTS tunnel_stat(
      day TEXT PRIMARY KEY,
      req_n INTEGER DEFAULT 0, bytes_in INTEGER DEFAULT 0, bytes_out INTEGER DEFAULT 0);
    /* 隧道流量按小时：监控屏「24 小时」窗口要精确到最近 24 小时，
       只按天存会把昨天整天也算进来（最多多算一倍）。 */
    /* 每种请求吃掉的字节（按用户/分钟）：直连命中率按「流量」算时用。
       k='relay' 中继转发；k='p2p' 开发机补报的 P2P/LAN 直连。 */
    /* 失败明细：服务调用失败 + HTTP 4xx/5xx。监控屏「失败明细」页用，保留最近 3 天。 */
    CREATE TABLE IF NOT EXISTS svc_fail(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT, err TEXT, ms INTEGER,
      uid INTEGER, dev TEXT, cid TEXT);
    CREATE TABLE IF NOT EXISTS req_fail(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, path TEXT, method TEXT, status INTEGER,
      uid INTEGER, dev TEXT, cid TEXT, ms INTEGER);
    /* 按路径类别拆的流量（诊断「中继流量都是什么」）：页面/静态/画面/消息/语音… */
    CREATE TABLE IF NOT EXISTS bytes_path(
      day TEXT, hour INTEGER, uid INTEGER, cid TEXT, cls TEXT,
      bytes INTEGER DEFAULT 0, req INTEGER DEFAULT 0,
      PRIMARY KEY(day, hour, uid, cid, cls));
    CREATE TABLE IF NOT EXISTS usage_bytes2(
      uid INTEGER NOT NULL, m INTEGER NOT NULL, k TEXT NOT NULL,
      cid TEXT DEFAULT '', dev TEXT DEFAULT '',
      bytes_in INTEGER DEFAULT 0, bytes_out INTEGER DEFAULT 0, req_n INTEGER DEFAULT 0,
      PRIMARY KEY(uid, m, k, cid, dev));
    CREATE TABLE IF NOT EXISTS traffic_hour(
      day TEXT NOT NULL, hour INTEGER NOT NULL, kind TEXT NOT NULL,
      req_n INTEGER DEFAULT 0, bytes_in INTEGER DEFAULT 0, bytes_out INTEGER DEFAULT 0,
      PRIMARY KEY(day, hour, kind));
    CREATE TABLE IF NOT EXISTS tunnel_hour(
      day TEXT NOT NULL, hour INTEGER NOT NULL,
      req_n INTEGER DEFAULT 0, bytes_in INTEGER DEFAULT 0, bytes_out INTEGER DEFAULT 0,
      PRIMARY KEY(day, hour));
    /* 动作记录：这一分钟里手机端做了什么（发消息/按键/开会话/翻文件/导航/轮询）。
       中继看到的请求（src=relay，带 cid）和开发机补报的直连请求（src=host，带 dev）
       都落这张表。同一分钟同一动作只一行、n 计数——查账时按分钟一行行看，
       能直接回答「这台手机半夜没碰过，为什么每分钟都在扣」。 */
    CREATE TABLE IF NOT EXISTS use_act_log(
      uid INTEGER NOT NULL, m INTEGER NOT NULL, src TEXT NOT NULL, dev TEXT,
      cid TEXT, ua TEXT, kind TEXT, act TEXT, path TEXT, n INTEGER DEFAULT 1,
      first REAL, last REAL,
      PRIMARY KEY(uid, m, src, dev, cid, act, path));
    /* 后台动作明细按时间查：没给 uid 时也走索引，别每次全表扫 */
    CREATE INDEX IF NOT EXISTS idx_use_act_m ON use_act_log(m);
    /* 免费版每天的消息条数 = SUM(n) WHERE uid=? AND m>=? AND act='发消息'：
       每发一条都要查一次，必须有 (uid, m) 索引。 */
    CREATE INDEX IF NOT EXISTS idx_use_act_um ON use_act_log(uid, m, act);
    /* 订单按用户查（套餐/升级报价/后台列表）：订单越攒越多，别全表扫 */
    CREATE INDEX IF NOT EXISTS idx_orders_uid ON orders(uid, status);
    /* 服务健康按天聚合：监控屏切「7 天 / 8 周 / 6 个月」用。事件只留 48h，
       每 5 分钟把今天/昨天两份按天重算一遍（幂等），更早的天不再动。 */
    CREATE TABLE IF NOT EXISTS svc_hour(
      day TEXT NOT NULL, hour INTEGER NOT NULL, key TEXT NOT NULL,
      ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, metric REAL DEFAULT 0,
      PRIMARY KEY(day, hour, key));
    CREATE TABLE IF NOT EXISTS svc_daily(
      day TEXT NOT NULL, key TEXT NOT NULL, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
      metric REAL DEFAULT 0, PRIMARY KEY(day, key));
    CREATE TABLE IF NOT EXISTS clients(
      uid INTEGER NOT NULL, cid TEXT NOT NULL, first REAL, last REAL,
      dev TEXT DEFAULT '',     -- 设备指纹（android|机型 等）：名额按它去重
      PRIMARY KEY(uid,cid));
    -- 被移除/被踢下线的终端（后台移除、账号页踢、浏览器闲置太久）：带着旧登录
    -- cookie 回来要按未登录处理，页面直接回 /r/ 登录；重新登录成功（新的 r_sess）
    -- 时删掉这条（2026-11-21）。
    CREATE TABLE IF NOT EXISTS client_kick(cid TEXT PRIMARY KEY, ts REAL);
    CREATE TABLE IF NOT EXISTS credits(uid INTEGER PRIMARY KEY, sec INTEGER DEFAULT 0,
      updated REAL);
    CREATE TABLE IF NOT EXISTS use_credits(uid INTEGER PRIMARY KEY, sec INTEGER DEFAULT 0,
      updated REAL);   -- 使用时长包余额（秒，不过期）
    CREATE TABLE IF NOT EXISTS use_credit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      uid INTEGER NOT NULL, delta INTEGER NOT NULL, before INTEGER, after INTEGER,
      src TEXT,          -- pay（到账）/ admin（后台手动）/ charge（每分钟扣费）/ fix（纠错）
      note TEXT, ts REAL);  -- 余额变动流水：后台对不上账时逐笔查
    CREATE TABLE IF NOT EXISTS feedback(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      uid INTEGER, dev TEXT, ts REAL, kind TEXT, text TEXT,
      media TEXT,            -- JSON：[{n:文件名,t:MIME,s:字节}]
      ver TEXT, plat TEXT, state INTEGER DEFAULT 0);
    /* 工单式回复：一条反馈下面可以来回聊（像阿里云工单——工单没关闭就一直能接）。
       state：0=待处理 1=处理中 2=已关闭（关闭后不再接受新回复）。 */
    CREATE TABLE IF NOT EXISTS feedback_replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fid INTEGER NOT NULL,   -- 反馈主帖 id
      ts REAL NOT NULL,
      who TEXT NOT NULL,      -- 'user'=用户回复 'admin'=官方回复
      text TEXT,
      media TEXT DEFAULT '[]');
    CREATE INDEX IF NOT EXISTS fb_rep_fid ON feedback_replies(fid);
    /* 计划任务：后台可开关/改间隔/立即执行。kind 是白名单里的处理类型。 */
    CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY, name TEXT, kind TEXT,
      every_s INTEGER DEFAULT 3600, enabled INTEGER DEFAULT 1,
      cron TEXT DEFAULT '',         -- crontab 表达式（分 时 日 月 周）；非空时优先于 every_s
      cfg TEXT DEFAULT '{}',        -- 任务参数（JSON）
      last_run REAL DEFAULT 0, last_ok INTEGER, last_msg TEXT,
      created REAL);
    /* 每次执行留一条记录（后台「历史」看最近 200 条）。 */
    CREATE TABLE IF NOT EXISTS task_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tid TEXT NOT NULL, started REAL NOT NULL, dur REAL DEFAULT 0,
      ok INTEGER DEFAULT 0, msg TEXT DEFAULT '',
      trigger TEXT DEFAULT 'auto');      -- auto=定时/间隔，manual=立即执行
    CREATE INDEX IF NOT EXISTS tr_tid ON task_runs(tid, id);
    /* 语音识别「越用越懂项目」：按 (账号, 开发机, 项目) 存。
       dev 也进主键：不同开发机上的同名项目不混词（用户确认过这个粒度）。
       proj 是开发机算出来的 git 根目录名（见 server.py _proj_key）。 */
    CREATE TABLE IF NOT EXISTS asr_gloss(
      uid INTEGER NOT NULL, dev TEXT NOT NULL, proj TEXT NOT NULL,
      term TEXT NOT NULL,            -- 正确写法（原样注入提示词、纠正结果）
      weight INTEGER NOT NULL DEFAULT 1,
      source TEXT NOT NULL DEFAULT 'fix',   -- fix=用户改稿学到 / manual / scan
      first REAL NOT NULL, last REAL NOT NULL,
      PRIMARY KEY(uid, dev, proj, term));
    CREATE TABLE IF NOT EXISTS asr_alias(
      uid INTEGER NOT NULL, dev TEXT NOT NULL, proj TEXT NOT NULL,
      wrong TEXT NOT NULL, right TEXT NOT NULL,
      hits INTEGER NOT NULL DEFAULT 1, last REAL NOT NULL,
      PRIMARY KEY(uid, dev, proj, wrong, right));
    CREATE TABLE IF NOT EXISTS asr_proj(
      uid INTEGER NOT NULL, dev TEXT NOT NULL, proj TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',   -- 项目一句话摘要（≤110 字，进提示词）
      updated REAL NOT NULL,
      PRIMARY KEY(uid, dev, proj));
    /* 桌宠中继兜底（板子连不上 host 时用）：按设备存最近对话和长期记忆。
       host 是唯一真相；这里仅离线那段自己记，不同步回去。 */
    CREATE TABLE IF NOT EXISTS pet_mem(
      dev TEXT PRIMARY KEY,
      memo TEXT NOT NULL DEFAULT '',
      chat TEXT NOT NULL DEFAULT '[]',    -- JSON [{role,text}]，最多 24 条
      ts REAL);
    """)
    for stmt in ("ALTER TABLE svc_daily ADD COLUMN metric REAL DEFAULT 0",
                 # 登记那一刻的端类型（web/app）：浏览器要按闲置踢人，
                 # 只靠 usage_client 的 UA 认不全（只刷页面不写操作的浏览器没有 UA 行）
                 "ALTER TABLE clients ADD COLUMN kind TEXT DEFAULT ''",
                 "ALTER TABLE users ADD COLUMN sess_epoch INTEGER DEFAULT 0",
                 "ALTER TABLE users ADD COLUMN email TEXT",
                 "ALTER TABLE users ADD COLUMN phone TEXT",
                 "ALTER TABLE devices ADD COLUMN frozen INTEGER DEFAULT 0",
                 # host 报上来的版本号落库：只在内存里存的话，中继一重启或开发机离线
                 # 后台那列就变 —，看不到「这台现在跑的是哪个版本」
                 "ALTER TABLE devices ADD COLUMN ver TEXT DEFAULT ''",
                 # 远程日志上报开关（后台按开发机打开）：打开后 host 每 15 秒推
                 # server.log 尾部过来，排查完就关掉。默认关。
                 "ALTER TABLE devices ADD COLUMN logs_on INTEGER DEFAULT 0",
                 # 日志上报到期时刻（unix 秒）：打开后最多 60 分钟自动关，
                 # 免得排查完忘了关、一直往中继推日志。0 = 没设过期。
                 "ALTER TABLE devices ADD COLUMN logs_until REAL DEFAULT 0",
                 "ALTER TABLE users ADD COLUMN plan TEXT",
                 "ALTER TABLE users ADD COLUMN plan_expires REAL",
                 # 当前生效的是哪一档（max_year 等）：页面据此判断升级/续费/降档
                 "ALTER TABLE users ADD COLUMN plan_sku TEXT",
                 # 已付款、等现套餐到期再生效的换档（降档/缩短）：sku + 天数
                 "ALTER TABLE users ADD COLUMN plan_next TEXT",
                 "ALTER TABLE users ADD COLUMN plan_next_days INTEGER DEFAULT 0",
                 # 排队档按自然月累加（month=1/quarter=3/year=12）；老库仍用 plan_next_days
                 "ALTER TABLE users ADD COLUMN plan_next_n INTEGER DEFAULT 0",
                 # 下单时把时长包的分钟数一起存下来：后台之后改价/删档，已下的单仍按原档入账
                 "ALTER TABLE orders ADD COLUMN pmin INTEGER DEFAULT 0",
                 # 跨档升级（Pro→Max）的补差价：下单时把抵扣额、实付额、给的时长、
                 # 报价时的档位一起冻结下来——付款可能隔几分钟，期间用户又下了别的单，
                 # 到账时按当时现算的抵扣会把钱算错。
                 "ALTER TABLE orders ADD COLUMN credit REAL DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN days_grant INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN from_sku TEXT DEFAULT ''",
                 # 下单时冻结「这一单给几天」和「下单那一刻的套餐到期日」：
                 # 付款可能隔几分钟，期间后台改天数、用户又续了费。到账按冻结值算，
                 # 付款前续出来的时间既不吞掉，也不会因为改价/改档多送或少送。
                 "ALTER TABLE orders ADD COLUMN days_frozen INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN base_exp REAL DEFAULT 0",
                 # 这一单的自然周期月数（月付 1/季付 3/年付 12，时长包 0）。
                 # 0 = 老订单，按 days_frozen 的固定天数入账，保持历史口径不变。
                 "ALTER TABLE orders ADD COLUMN period_n INTEGER DEFAULT 0",
                 # 计划任务支持 crontab 定时（老库补列，空 = 按 every_s 间隔）
                 "ALTER TABLE tasks ADD COLUMN cron TEXT DEFAULT ''",
                 # 注册默认密码=验证码：置 1 提醒用户登录后尽快改密码，改完清 0
                 "ALTER TABLE users ADD COLUMN must_change_pw INTEGER DEFAULT 0",
                 # 使用时长包余额改按秒存（2026-11-08：计费刻度最小单位从 1 分钟
                 # 改成可配、默认 3 秒）。老库补列；余额本身在下面一次性换算，
                 # 使用量历史按用户要求不迁移（只影响之后的账）。
                 "ALTER TABLE use_credits ADD COLUMN sec INTEGER DEFAULT 0",
                 # 产品线（boss 系列 D5/D12：codeboss=缺省，agentboss=伞形全量；
                 # 老库补列统一回落 codeboss，存量零感知）
                 "ALTER TABLE users ADD COLUMN product TEXT DEFAULT 'codeboss'",
                 "ALTER TABLE devices ADD COLUMN product TEXT DEFAULT 'codeboss'",
                 # 匿名免费版：开发机硬件指纹（SHA-256，见 spec 2026-11-19）。
                 # 重装/删 config 后重新登记时按它找回同一台设备，额度不重置。
                 "ALTER TABLE devices ADD COLUMN fp TEXT DEFAULT ''",
                 # 匿名设备（owner 为空但允许接入中继）。老设备一律 0，行为不变。
                 "ALTER TABLE devices ADD COLUMN anon INTEGER DEFAULT 0",
                 "CREATE INDEX IF NOT EXISTS idx_devices_fp ON devices(fp, product)",
                 # 指纹跨产品线唯一：一台机器只有一台设备（空指纹不占）。
                 "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_fp1 ON devices(fp) "
                 "WHERE fp<>''",
                 # 客户端名额按设备指纹去重（2026-11-21）：壳丢 cookie/后台通道
                 # 各占一个名额，一台手机能吃掉好几个。老行启动时按 UA 回填。
                 "ALTER TABLE clients ADD COLUMN dev TEXT DEFAULT ''"):
        try:  # 旧库迁移：补列（已存在则忽略）
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    _migrate_users_products(db)   # 端权限列（同样幂等）
    # 时长包余额一次性从「分钟」换算成「秒」：老库里 min 有值的行换算后清 0，
    # 所以只会跑一次。这是用户已经买到的余额，不能当成历史丢掉。
    try:
        db.execute("UPDATE use_credits SET sec=COALESCE(min,0)*60, min=0 "
                   "WHERE COALESCE(min,0)<>0")
    except sqlite3.OperationalError:
        pass
    db.execute("CREATE TABLE IF NOT EXISTS phone_codes("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, out_id TEXT NOT NULL, "
               "purpose TEXT NOT NULL, created REAL, expires REAL, used INTEGER DEFAULT 0)")
    # 后台人工改套餐/调额度的审计：谁、什么时候、改前改后。手工写库出的问题能追。
    db.execute("CREATE TABLE IF NOT EXISTS admin_audit("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, action TEXT, "
               "target_uid INTEGER, before TEXT, after TEXT, ip TEXT)")
    # 功能使用埋点：页面 /api/track 打点，后台「分析」页读。一行 = 某人/设备
    # 某天某个事件用了多少次；重复上报累加 n（见 h_track）。
    db.execute(_FEAT_DDL)
    _feat_day_migrate(db)   # 老表没有 pg/k：原地重建，旧行留在 '' 维度
    try:  # 一个手机号只能绑一个账号（历史库可能没这条索引）
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) "
                   "WHERE phone IS NOT NULL AND phone<>''")
    except sqlite3.OperationalError:
        pass
    _clients_dev_backfill(db)   # 老客户端行补设备指纹（幂等、几毫秒）
    db.commit()
    return db


def _clients_dev_backfill(db):
    """老 clients 行没有 dev：按 usage_client 里同一 cid 最近一次记下的 UA
    补上机型指纹。补完立刻按设备去重——以前同一台手机丢一次 cookie 就算一台。"""
    rows = db.execute("SELECT uid, cid FROM clients WHERE COALESCE(dev,'')=''").fetchall()
    for uid, cid in rows:
        try:
            r = db.execute("SELECT ua FROM usage_client WHERE uid=? AND k=?"
                           " AND COALESCE(ua,'')<>'' ORDER BY last DESC LIMIT 1",
                           (uid, _use_key(cid))).fetchone()
        except sqlite3.OperationalError:
            return                      # 老库还没 usage_client 表：算了
        if r:
            d = _ua_dev(r[0])
            if d:
                db.execute("UPDATE clients SET dev=? WHERE uid=? AND cid=?", (d, uid, cid))
    db.commit()


def _use_key(seat):
    """clients.cid → usage_client.k：有 cookie 的是裸 cid（usage 里记 "c:<cid>"），
    没 cookie 的兜底键本身已经带 "i:" 前缀。"""
    s = str(seat or "")
    return s if s.startswith("i:") else "c:" + s


def meta_key(db):
    row = db.execute("SELECT v FROM meta WHERE k='secret'").fetchone()
    if row:
        return row[0].encode()
    k = secrets.token_hex(32)
    db.execute("INSERT INTO meta(k,v) VALUES('secret',?)", (k,))
    db.commit()
    return k.encode()


# ---------- auth ----------
def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def make_sess(key, uid, epoch=0):
    body = "%d.%d" % (uid, epoch)
    return "%s.%s" % (body, hmac.new(key, body.encode(), hashlib.sha256).hexdigest())


def make_acct_tok(app, db, uid, ttl=1800):
    """限额页专用短票据：只授权账号管理接口，30 分钟有效，改密码/踢登录后失效。"""
    row = db.execute("SELECT sess_epoch FROM users WHERE id=?", (int(uid),)).fetchone()
    epoch = (row[0] if row else 0) or 0
    body = "acct.%d.%d.%d" % (int(uid), epoch, int(time.time()) + ttl)
    sig = hmac.new(app["key"], body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def check_acct_tok(app, db, tok):
    parts = (tok or "").split(".")
    if len(parts) != 5 or parts[0] != "acct":
        return None
    try:
        uid, epoch, exp = int(parts[1]), int(parts[2]), int(parts[3])
    except (TypeError, ValueError):
        return None
    body = ".".join(parts[:4])
    want = hmac.new(app["key"], body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[4], want):
        return None
    if time.time() > exp:
        return None
    row = db.execute("SELECT sess_epoch FROM users WHERE id=?", (uid,)).fetchone()
    if not row or (row[0] or 0) != epoch:
        return None
    return uid


def uid_of(request):
    tok = request.cookies.get("r_sess") or ""
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    uid, epoch, sig = parts
    body = "%s.%s" % (uid, epoch)
    want = hmac.new(request.app["key"], body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return None
    row = _db(request.app).execute("SELECT sess_epoch FROM users WHERE id=?", (int(uid),)).fetchone()
    if not row or row[0] != int(epoch):   # 改过密码/被踢：旧 cookie 作废
        return None
    return int(uid)


# ---- 设备会话（匿名免费版）----
# 免注册的免费用户没有账号，但手机端必须能代表「这台开发机」。cookie 里只放
# dev id + 到期时间 + HMAC，密钥沿用 meta 表的持久 secret（重启不失效）。
DEV_SESS_COOKIE = "cb_dev_sess"
DEV_SESS_TTL = 90 * 86400


def _dev_sess_key(db):
    k = os.environ.get("RELAY_SESS_KEY") or ""
    return k.encode() if k else meta_key(db)


def _dev_sess_make(db, dev, ttl=DEV_SESS_TTL):
    body = "ds.%s.%d" % (dev, int(time.time()) + int(ttl))
    sig = hmac.new(_dev_sess_key(db), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def _dev_sess_read(db, tok):
    parts = (tok or "").split(".")
    if len(parts) != 4 or parts[0] != "ds":
        return None
    dev, exp = parts[1], parts[2]
    try:
        if time.time() > int(exp):
            return None
        want = hmac.new(_dev_sess_key(db), ("ds.%s.%s" % (dev, exp)).encode(),
                        hashlib.sha256).hexdigest()
    except ValueError:
        return None
    if not hmac.compare_digest(parts[3], want):
        return None
    # 会话只属于「还没归属账号」的设备：设备一被认领（收银台/登录配对），
    # 旧 cookie 立刻失效。不失效的话，清掉 r_sess 就变回 d<dev> 主体——
    # 而认领时当日用量并入账号、d<dev> 已清零 = 白拿第二份免费额度。
    row = db.execute("SELECT frozen, owner FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or row[0] or row[1] is not None:
        return None
    return dev


def dev_of(request):
    return _dev_sess_read(_db(request.app), request.cookies.get(DEV_SESS_COOKIE))


def _tunnel_principal(request):
    """隧道身份：(uid, dev, anon)。账号会话优先，其次设备会话。"""
    uid = uid_of(request)
    if uid is not None:
        return uid, None, False
    dev = dev_of(request)
    return (None, dev, True) if dev else (None, None, False)


# ---- 后台提醒通道（安卓前台服务用）----
# 手机壳在黑屏/锁屏时靠一个前台服务维持一条「只收提醒」的 SSE。它不能算远程
# 使用时长，所以走单独的 /api/bg_push，并在计费处豁免。
# 认证用一把 30 天、只能读提醒流的 cookie：老 WebView（坚果 Chromium 62）
# 的 CookieManager 读不到 httponly 的 r_sess，而本 cookie 显式非 httponly。
BG_PATH = "/api/bg_push"
BG_COOKIE = "cb_bg"
BG_TTL = 30 * 86400
# 纯探针接口：页面/壳定时问「版本变了吗 / 有没有新包」。老页面（还没自更新）会在
# 息屏后被 WebView 节流成每分钟一次，按「有请求=在用」的口径会整夜烧时长。
PROBE_PATHS = ("/api/page_ver", "/api/version", "/api/apk_ver", "/api/ping")


def make_bg(key, uid, epoch):
    exp = int(time.time()) + BG_TTL
    body = "bg.%d.%d.%d" % (int(uid), int(epoch), exp)
    return body + "." + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def bg_uid(request):
    tok = request.cookies.get(BG_COOKIE) or ""
    parts = tok.split(".")
    if len(parts) != 5 or parts[0] != "bg":
        return None
    try:
        uid, epoch, exp = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    body = ".".join(parts[:4])
    want = hmac.new(request.app["key"], body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[4], want) or exp <= time.time():
        return None
    row = _db(request.app).execute("SELECT sess_epoch FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row[0] != epoch:   # 改过密码：一起作废
        return None
    return uid


def bg_cookie(resp, request, uid, epoch):
    """把后台提醒 cookie 种到响应上。path 限定在提醒通道，页面 JS 读不到，
    普通页面请求也不会带上它。"""
    resp.set_cookie(BG_COOKIE, make_bg(request.app["key"], uid, epoch),
                    max_age=BG_TTL, path=BG_PATH, httponly=False, samesite="Lax",
                    secure=(request.scheme == "https"))
    return resp


# ---- 设备版提醒凭据（2026-11-19：局域网直连的设备壳永远 401）----
# 账号版 cb_bg 只在「中继账号登录态」下种（uid_of 有值）。可手机常常是扫码配对的
# 设备会话：页面从没在中继登录过，壳就只能带一个 cb_cid 去撞 /api/bg_push，每 60
# 秒一次 401，语音播报也拿不到音频。这里按「设备」签一把只读提醒通道的钥匙：
# 非 httponly（壳要读）、path 限 /api/（页面 JS 在 / 和 /ext/ 下读不到）、
# 设备冻结/删号/换主人即失效（bgd_dev 每次回库核对）。
BGD_COOKIE = "cb_bgd"
BGD_PATH = "/api/"
BGD_TTL = 90 * 86400
SAY_PATH = "/api/say"


def make_bgd(key, dev):
    exp = int(time.time()) + BGD_TTL
    body = "bgd.%s.%d" % (dev, exp)
    return body + "." + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def _bgd_check(request, tok):
    parts = (tok or "").split(".")
    if len(parts) != 4 or parts[0] != "bgd":
        return None
    dev = parts[1]
    try:
        exp = int(parts[2])
    except ValueError:
        return None
    body = ".".join(parts[:3])
    want = hmac.new(request.app["key"], body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[3], want) or exp <= time.time():
        return None
    row = _db(request.app).execute(
        "SELECT owner, frozen FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or row[1]:
        return None
    return (row[0], dev)


def bgd_dev(request):
    """cb_bgd cookie → (owner_uid_or_None, dev)；签名/过期/冻结/不存在一律 None。
    返回 None 表示「没有有效设备凭据」，不是「匿名」——匿名设备也带 (None, dev)。"""
    return _bgd_check(request, request.cookies.get(BGD_COOKIE))


def bgd_hdr(request):
    """X-CB-Bgd 头（壳自己带）：页面把 /api/bg_ticket 签的票交给壳存下来，
    黑屏时页面被冻结、没人能换票，所以这把是 90 天的长期凭据。"""
    return _bgd_check(request, request.headers.get("X-CB-Bgd"))


def bgd_cookie(resp, request, dev):
    key = request.app.get("key")
    if not key or not dev:
        return resp
    resp.set_cookie(BGD_COOKIE, make_bgd(key, dev),
                    max_age=BGD_TTL, path=BGD_PATH, httponly=False, samesite="Lax",
                    secure=(request.scheme == "https"))
    return resp


def say_path(path):
    """/api/say/<id> 是壳取播报音频的真路径（host 路由带 id）；/api/say_demo 不算。"""
    return path == SAY_PATH or path.startswith(SAY_PATH + "/")


def dt_dev(request):
    """?dt= 直连票据当设备身份用（壳读不到 cookie，只能把票据放 URL 上）。
    只给 /api/say* 用：票据本来就是中继签给这台设备、10 分钟有效的短命凭据，
    壳把 URL 原样 GET —— 在家（Wi-Fi）和在外面（流量）都走中继，一样能认人。"""
    tok = (request.query.get("dt") or "").strip()
    if not tok:
        return None
    try:
        raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
    except Exception:
        return None
    msg, _, sig = raw.rpartition("|")
    parts = msg.split("|")
    if len(parts) < 3:
        return None
    dev = parts[0]
    try:
        exp = int(parts[1])
    except ValueError:
        return None
    if exp <= time.time():
        return None
    row = _db(request.app).execute(
        "SELECT secret, owner, frozen FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or row[2]:
        return None
    key = hmac.new(row[0].encode(), b"cb-direct-v1", hashlib.sha256).digest()
    want = hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want):
        return None
    return (row[1], dev)


def remind_auth(request):
    """提醒通道的身份：账号 cb_bg 优先，其次 dt 票据（只认取音频那条），
    最后设备 cb_bgd。别把它变成通用钥匙（/api/say 是壳下载播报音频，
    /api/bg_push 是提醒流）。"""
    if request.path != BG_PATH and not say_path(request.path):
        return None
    if request.path == BG_PATH:
        uid = bg_uid(request)
        if uid is not None:
            return (uid, None)
    else:
        d = dt_dev(request)
        if d is not None:
            return d
    d = bgd_hdr(request)     # 壳自己带的长期设备凭据（读不到 cookie 的机器）
    if d is not None:
        return d
    return bgd_dev(request)


def bg_ticket_dev(request):
    """谁在要提醒凭据：账号会话+选中设备，或匿名设备会话。"""
    uid = uid_of(request)
    if uid is not None:
        sel = _sel_dev(request)
        return sel[1][0] if (sel and sel[1]) else ""
    return dev_of(request) or ""


async def h_bg_ticket(request):
    """页面把一把 90 天的设备提醒凭据交给壳（壳读不到 WebView 的 cookie）。
    只给这台设备的 /api/bg_push 与 /api/say 用；冻结/删号即失效（每次回库核对）。"""
    dev = bg_ticket_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或未选设备"}, status=401)
    key = request.app.get("key")
    if not key:
        return web.json_response({"ok": False}, status=404)
    row = _db(request.app).execute(
        "SELECT frozen FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or row[0]:
        return web.json_response({"ok": False, "error": "设备不可用"}, status=403)
    return web.json_response({"ok": True, "tok": make_bgd(key, dev),
                              "exp": int(time.time()) + BGD_TTL})


def own_device(db, uid, dev_id):
    if not uid:          # 否则未绑定设备(owner=NULL)会认任何「无账号」调用者为主人
        return None
    row = db.execute("SELECT id,name,owner,last_seen,frozen FROM devices WHERE id=?", (dev_id,)).fetchone()
    return row if row and row[2] == uid else None


# ---------- 简单限流（内存滑窗，防注册/登记接口被扫） ----------
_RL = {}


def rate_ok(ip, action, limit, window=3600):
    now = time.time()
    arr = [t for t in _RL.get((ip, action), ()) if now - t < window]
    if len(arr) >= limit:
        _RL[(ip, action)] = arr
        return False
    arr.append(now)
    _RL[(ip, action)] = arr
    return True


def rate_wait(ip, action, limit, window=3600):
    """还要等多少秒才腾出名额（0=立刻可以）。429 时告诉用户等多久，别只写「稍后再试」。"""
    now = time.time()
    arr = sorted(t for t in _RL.get((ip, action), ()) if now - t < window)
    if len(arr) < limit:
        return 0
    return max(1, int(window - (now - arr[len(arr) - limit])) + 1)


# ---------- 邮箱验证码（注册二次验证 / 找回密码） ----------
def _mail_send(to, subject, body):
    """记录服务健康后再发信（监控屏用）。"""
    if not SMTP_HOST or SMTP_HOST == "console":
        return _mail_send_raw(to, subject, body)   # 自测/未配置：不算邮件服务的真实调用
    t0 = time.time()
    try:
        ok = _mail_send_raw(to, subject, body)
    except Exception as e:
        _svc_note("email", False, (time.time() - t0) * 1000,
                  "%s: %s" % (type(e).__name__, str(e)[:200]))
        raise
    _svc_note("email", bool(ok), (time.time() - t0) * 1000,
              "" if ok else "发信返回失败")
    return ok


def _mail_send_raw(to, subject, body):
    """同步发信（asyncio.to_thread 里调）。HOST=console 时打印到日志——自建/测试用。"""
    if SMTP_HOST == "console":
        print("[relay][console-mail] to=%s %s: %s" % (to, subject, body), flush=True)
        return True
    if not SMTP_HOST:
        return False
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    # ⚠️ 必须显式给 EHLO 名字。smtplib 在 local_hostname 缺省时会取
    # socket.getfqdn()；这台机器的 hostname 没有点（iZuf...），于是它退化成
    # EHLO [<本机 IP>]，而本机 IP 被 mihomo 的 fake-ip 解析成 198.18.0.9。
    # 163 的 Coremail 反垃圾收到 EHLO [198.18.0.9] 不报错，直接静默挂 20 秒
    # 再断连 —— 表现就是 SMTP_SSL 建连、TLS 握手、EHLO 都「成功」，然后
    # login() 卡 20s 抛 SMTPServerDisconnected。改成合法域名后 login <0.3s。
    lhn = branding.RELAY_HOST_DEFAULT
    # 偶发断连时重试一次就够（10s 超时 × 2 ≈ 最长 23s）；以前 3×15s 会让
    # 注册页的「发送中…」挂将近一分钟，用户以为卡死了。
    last = None
    for attempt in range(2):
        s = None
        try:
            if SMTP_PORT == 465:
                s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10,
                                     local_hostname=lhn)
            else:
                s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10,
                                 local_hostname=lhn)
                s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to], msg.as_string())
            return True
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
        finally:
            if s is not None:
                try:
                    s.quit()
                except Exception:
                    pass
    raise last


async def r_promo_info(request):
    """注册/登录页的活动信息（公开，不需要登录）。

    返回是否在活动期、送什么、多久、活动截止时间、说明文案。"""
    db = _db(request.app)
    cfg = promo_cfg(db)
    now = time.time()
    until = int(cfg.get("until") or 0)
    live = bool(cfg.get("enabled")) and int(cfg.get("days") or 0) > 0 and (not until or now <= until)
    plan = cfg.get("plan") or "pro"
    tier_name = {"pro": "Pro", "max": "Max"}.get(plan, "Pro")
    return web.json_response({
        "ok": True, "live": live, "plan": plan, "tier": tier_name,
        "days": int(cfg.get("days") or 0), "until": until,
        "note": cfg.get("note") or "", "banner": bool(cfg.get("banner", True)) and live,
        "text": ("注册即送 %d 天 %s，到期自动恢复免费版" % (int(cfg.get("days") or 0), tier_name))
        if live else "",
    })


async def r_send_sms(request):
    """发短信验证码。purpose=register/login/reset/bind。
    注册/登录/找回都直接告诉用户「这个号注没注册」——注册页本来就会说，
    藏着掖着只会让人以为验证码发了、傻等一条不会来的短信。"""
    data = await request.json()
    purpose = data.get("purpose") or "register"
    if purpose not in ("register", "login", "reset", "bind"):
        return web.json_response({"ok": False, "error": "未知用途"})
    phone = _norm_phone(data.get("phone"))
    if not _PHONE_RE.match(phone):
        return web.json_response({"ok": False, "error": "手机号格式不正确"})
    if purpose == "bind" and uid_of(request) is None:
        return web.json_response({"ok": False, "error": "请先登录"}, status=401)
    if not SMS_ON:
        return web.json_response(
            {"ok": False, "error": "中继没配短信通道（RELAY_ALI_AK_ID/AK_SECRET/SIGN_NAME/TEMPLATE_CODE）"},
            status=502)
    ip = request.remote or "?"
    db = _db(request.app)
    db.execute("DELETE FROM phone_codes WHERE expires<?", (time.time() - 3600,))
    db.commit()
    if not rate_ok(ip, "sms", 10) or not rate_ok(phone, "smsto", 5):
        return web.json_response({"ok": False, "error": "验证码请求过于频繁，稍后再试"}, status=429)
    has = db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone() is not None
    masked = _mask_phone(phone)
    if purpose == "register" and has:
        return web.json_response({"ok": False, "error": "该手机号已注册，直接登录"})
    if purpose == "bind" and has:
        # 换绑：别人账号占着这个号就别发码了；本人重复绑同一个号不算错
        row = db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row and row[0] != uid_of(request):
            return web.json_response({"ok": False, "error": "该手机号已被其他账号绑定"})
    if purpose in ("login", "reset") and not has:
        return web.json_response({"ok": False, "error": "该手机号未注册，请先注册"})
    last = db.execute("SELECT created FROM phone_codes WHERE phone=? ORDER BY id DESC LIMIT 1",
                      (phone,)).fetchone()
    if last and time.time() - last[0] < SMS_COOLDOWN:
        return web.json_response({"ok": False,
                                  "error": "发送太频繁，请 %d 秒后再试" % SMS_COOLDOWN})
    out_id = ("%06d" % secrets.randbelow(1000000)) if SMS_CONSOLE else secrets.token_hex(8)
    tmpl = {"reset": ALI_TEMPLATE_RESET, "bind": ALI_TEMPLATE_BIND}.get(purpose, ALI_TEMPLATE)
    try:
        await asyncio.to_thread(_sms_send_sync, phone, out_id, tmpl)
    except Exception as e:
        print("[relay] 短信发送失败 %s: %s" % (masked, str(e)[:200]), flush=True)
        return web.json_response({"ok": False, "error": "短信发送失败：%s" % str(e)[:140]},
                                 status=502)
    now = time.time()
    db.execute("INSERT INTO phone_codes(phone,out_id,purpose,created,expires) VALUES(?,?,?,?,?)",
               (phone, out_id, purpose, now, now + SMS_TTL))
    db.commit()
    print("[relay] 短信验证码已发 %s purpose=%s ip=%s" % (masked, purpose, ip), flush=True)
    return web.json_response({"ok": True, "sent": True, "msg": "验证码已发送至 " + masked})


# ---------- 手机号验证码（阿里云号码认证服务 短信认证） ----------
import re as _re
_PHONE_RE = _re.compile(r"^1[3-9]\d{9}$")


def _norm_phone(x):
    return _re.sub(r"\D", "", x or "")


def _mask_phone(p):
    return p[:3] + "****" + p[-4:] if len(p) == 11 else p


def _mask_email(e):
    """a***@example.com；只给账号中心/开发机显示用，不返回完整邮箱。"""
    if not e or "@" not in e:
        return e or ""
    a, d = e.split("@", 1)
    return (a[:1] + "***@" + d) if a else ("***@" + d)


def _ali_rpc(action, params):
    """阿里云 OpenAPI RPC 签名（HMAC-SHA1，SignatureVersion 1.0）。同步，放线程里调。
    只依赖标准库：发行包是 PyInstaller 单文件，不引入阿里云 SDK。"""
    import base64
    import hashlib
    import hmac
    import uuid
    import urllib.parse
    import urllib.request
    p = {"AccessKeyId": ALI_AK_ID, "Action": action, "Format": "JSON",
         "RegionId": "cn-hangzhou", "SignatureMethod": "HMAC-SHA1",
         "SignatureNonce": uuid.uuid4().hex, "SignatureVersion": "1.0",
         "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "Version": "2017-05-25"}
    p.update(params)

    def enc(v):
        return urllib.parse.quote(str(v), safe="~")

    query = "&".join("%s=%s" % (enc(k), enc(p[k])) for k in sorted(p))
    sts = "POST&%2F&" + enc(query)
    sig = base64.b64encode(hmac.new((ALI_AK_SECRET + "&").encode(),
                                    sts.encode(), hashlib.sha1).digest()).decode()
    body = (query + "&Signature=" + enc(sig)).encode()
    req = urllib.request.Request(ALI_ENDPOINT + "/", data=body,
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        import json as _json
        return _json.loads(r.read().decode("utf-8"))


def _sms_send_sync(phone, out_id, template=None):
    """记录服务健康后发送（监控屏用）。"""
    if SMS_CONSOLE:
        return _sms_send_raw(phone, out_id, template)   # 控制台模式：不是真实短信
    t0 = time.time()
    try:
        ok = _sms_send_raw(phone, out_id, template)
    except Exception as e:
        if SMS_ON:
            _svc_note("sms", False, (time.time() - t0) * 1000,
                      "%s: %s" % (type(e).__name__, str(e)[:200]))
        raise
    if SMS_ON:
        _svc_note("sms", bool(ok), (time.time() - t0) * 1000,
                  "" if ok else "发送返回失败")
    return ok


def _sms_send_raw(phone, out_id, template=None):
    """发一条验证码（系统生成，6 位数字，5 分钟有效，60 秒频控，重发覆盖旧码）。"""
    if SMS_CONSOLE:
        print("[relay][console-sms] to=%s code=%s" % (_mask_phone(phone), out_id), flush=True)
        return True
    import json as _json
    r = _ali_rpc("SendSmsVerifyCode", {
        "PhoneNumber": phone, "SignName": ALI_SIGN_NAME,
        "TemplateCode": template or ALI_TEMPLATE,
        "TemplateParam": _json.dumps({"code": "##code##", "min": str(SMS_TTL // 60)}),
        "OutId": out_id, "CodeLength": 6, "ValidTime": SMS_TTL,
        "Interval": 60, "CodeType": 1, "DuplicatePolicy": 1,
        "ReturnVerifyCode": "false"})
    if not r.get("Success") or r.get("Code") != "OK":
        raise RuntimeError("%s: %s" % (r.get("Code") or "ERR", r.get("Message") or "发送失败"))
    return True


def _sms_check_sync(phone, code, out_id):
    """记录服务健康后核验（监控屏用）。验证码错误不算服务故障，只记接口异常。"""
    if SMS_CONSOLE:
        return _sms_check_raw(phone, code, out_id)
    t0 = time.time()
    try:
        ok = _sms_check_raw(phone, code, out_id)
    except Exception as e:
        if SMS_ON:
            _svc_note("sms", False, (time.time() - t0) * 1000,
                      "核验 %s: %s" % (type(e).__name__, str(e)[:200]))
        raise
    return ok


def _sms_check_raw(phone, code, out_id):
    """向阿里云核验（一次性）。只有 Model.VerifyResult == PASS 才算通过。"""
    r = _ali_rpc("CheckSmsVerifyCode", {
        "PhoneNumber": phone, "VerifyCode": code, "OutId": out_id,
        "CountryCode": "86", "CaseAuthPolicy": 1})
    if not r.get("Success") or r.get("Code") != "OK":
        raise RuntimeError("%s: %s" % (r.get("Code") or "ERR", r.get("Message") or "核验失败"))
    return ((r.get("Model") or {}).get("VerifyResult") == "PASS")


async def _phone_verify(db, phone, code, purpose):
    """本地记录（有效期/一次性）+ 阿里云核验，两道都过才算数。"""
    code = (code or "").strip()
    if not code:
        return False
    row = db.execute("SELECT id,out_id,expires,used FROM phone_codes "
                     "WHERE phone=? AND purpose=? ORDER BY id DESC LIMIT 1",
                     (phone, purpose)).fetchone()
    if not row or row[3] or (row[2] or 0) < time.time():
        return False
    if SMS_CONSOLE:   # 自建/测试：验证码就是本地存的那串
        ok = (code == row[1])
    else:
        try:
            ok = await asyncio.to_thread(_sms_check_sync, phone, code, row[1])
        except Exception as e:
            print("[relay] 短信核验失败: %s" % str(e)[:200], flush=True)
            return False
    if not ok:
        return False
    db.execute("UPDATE phone_codes SET used=1 WHERE id=?", (row[0],))
    db.commit()
    return True


def _use_code(db, email, code, purpose):
    """核销一条验证码：匹配 + 未用 + 未过期 → 置 used。"""
    if not code:
        return False
    row = db.execute("SELECT id FROM email_codes WHERE email=? AND code=? AND purpose=? "
                     "AND used=0 AND expires>?", (email, code, purpose, time.time())).fetchone()
    if not row:
        return False
    db.execute("UPDATE email_codes SET used=1 WHERE id=?", (row[0],))
    db.commit()
    return True


async def r_send_code(request):
    """发验证码。purpose=register 发到请求里的 email；purpose=login 只给已注册邮箱发；
    purpose=reset 默认按请求里的 email（老页面传用户名也能兜底按用户名查）；
    purpose=bind 需登录，发到请求里的 email（换绑邮箱用）。
    注没注册一律明说——注册页本来就会暴露，含糊话术只会让用户白等验证码。"""
    data = await request.json()
    purpose = data.get("purpose") or "register"
    ip = request.remote or "?"
    db = _db(request.app)
    db.execute("DELETE FROM email_codes WHERE expires<?", (time.time() - 3600,))
    db.commit()
    if purpose not in ("register", "reset", "login", "bind"):
        return web.json_response({"ok": False, "error": "未知用途"})
    if purpose == "bind" and uid_of(request) is None:
        return web.json_response({"ok": False, "error": "请先登录"}, status=401)
    email = (data.get("email") or "").strip().lower()
    if purpose == "reset" and not email:
        name = (data.get("name") or "").strip()
        row = db.execute("SELECT email FROM users WHERE name=?", (name,)).fetchone()
        email = ((row[0] or "") if row else "").lower()
    if purpose in ("reset", "login"):
        if not email:
            return web.json_response({"ok": False, "error": "请填注册邮箱"})
        if not db.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
            return web.json_response({"ok": False, "error": "该邮箱未注册，请先注册"})
    if not _EMAIL_RE.match(email):
        return web.json_response({"ok": False, "error": "邮箱格式不正确"})
    if not rate_ok(ip, "mail", 10) or not rate_ok(email, "mailto", 5):
        return web.json_response({"ok": False, "error": "验证码请求过于频繁，稍后再试"}, status=429)
    last = db.execute("SELECT created FROM email_codes WHERE email=? ORDER BY id DESC LIMIT 1",
                      (email,)).fetchone()
    if last and time.time() - last[0] < 60:
        return web.json_response({"ok": False, "error": "发送太频繁，请 1 分钟后再试"})
    code = "%06d" % secrets.randbelow(1000000)
    now = time.time()
    db.execute("INSERT INTO email_codes(email,code,purpose,created,expires) VALUES(?,?,?,?,?)",
               (email, code, purpose, now, now + CODE_TTL))
    db.commit()
    why = ""
    if not SMTP_HOST:
        why = "中继没读到 RELAY_SMTP_HOST（检查 systemd 服务里的 Environment=）"
        db.execute("DELETE FROM email_codes WHERE email=? AND code=?", (email, code))
        db.commit()
        return web.json_response(
            {"ok": False, "error": "验证码发送失败：" + why}, status=502)

    async def _send():
        """真发信 + 失败清理。返回 (ok, why)。"""
        try:
            ok = await asyncio.to_thread(
                _mail_send, email, "CodeBoss 验证码",
                "验证码：%s（10 分钟内有效，非本人操作请忽略）" % code)
        except Exception as e:
            print("[relay] 邮件发送失败: %s" % str(e)[:300], flush=True)
            ok, w = False, "%s: %s" % (type(e).__name__, str(e)[:120])
        else:
            w = ""
        if not ok:      # 发不出去就把验证码作废，别留一条用户永远收不到的码
            try:
                db.execute("DELETE FROM email_codes WHERE email=? AND code=?", (email, code))
                db.commit()
            except Exception as e:
                print("[relay] 清理验证码失败: %s" % str(e)[:120], flush=True)
        return ok, w

    # ⚠️ 别让页面陪着 SMTP 一起等：邮件其实早就发到了，页面却还挂着「发送中…」。
    # 正常情况 1 秒内就回来；超过 6 秒就转后台继续发，先把「已发出」告诉用户。
    task = asyncio.create_task(_send())
    _MAIL_BG.add(task)
    task.add_done_callback(_MAIL_BG.discard)
    try:
        ok, why2 = await asyncio.wait_for(asyncio.shield(task), timeout=6)
    except asyncio.TimeoutError:
        print("[relay] 验证码发送超过 6s，转后台 %s purpose=%s" % (email, purpose), flush=True)
        return web.json_response(
            {"ok": True, "sent": True, "pending": True,
             "msg": "验证码已提交发送，若 1 分钟内没收到请重试"})
    if not ok:
        return web.json_response(
            {"ok": False, "error": "验证码发送失败：" + (why2 or "未知错误")},
            status=502)
    print("[relay] 验证码已发 %s purpose=%s ip=%s" % (email, purpose, ip), flush=True)
    return web.json_response({"ok": True, "sent": True, "msg": "验证码已发送至 %s" % email})


# ---------- 页面 ----------
# ---------- 站点图标（全端统一：mac/icon-1024.png，tools/make-icons.py 生成） ----------
# 中继是单文件部署，图标只能内联；/favicon.ico 与 /apple-touch-icon.png 两个路由
# 把这里解出来的 PNG 直接吐给浏览器，各页面 head 只引用路径。
_FAVICON_PNG_B64 = (
    "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"
)
_TOUCH_PNG_B64 = (
    "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"
)
_FAVICON_PNG = base64.b64decode(_FAVICON_PNG_B64)
_TOUCH_PNG = base64.b64decode(_TOUCH_PNG_B64)

# AgentBoss 线条目图标（金色信封+抽屉，180px）：登录/扫码页 ?product=agentboss 时
# 页头和 favicon 换成它。两条线各自一套，2026-11-19 用户实报「里面还是 CodeBoss 的图标」。
_AGENT_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAb0klEQVR42u2deZxcZZ3uv7/3nFq7u3pNSEgCCQlJwIiMG1yQAZEAGTcQF0RHR3QQUQZh9OodxsumLKOsLuNClCsuDA4Dw4woMDguBBE/EUHWJKhAVugkXdW1dFWd8/7uH6e6KXqydDrppJb3+VDhA59OUnXqOc953t8q7B2Y2iuo/5+dnTMH8KuHqpVXIfoKVRYAs4B+gS4gAQgOjQQFygrDwGZgnQhrUHlcjD5CEHsin98wOO73+ICtvaYUU00Wr3YBRj+Il8r0vcbDnGCV40T0lSAzRGTsStX94tDwkNF/ou9PFdCNqvIHI/w8xP5XKbdlJRDWCZvU/XfTENqrkVgBOnr3W4K1p6vqqYIcGhFYaxcA6n5W6t6TU+bGV+rRf49+dwYg+n4FVUXRJ0Tkdoy5pbB102N1362ZCmLLFFiLUYKS7hpYJqLngJwkIrGIv6q1DyJ1d6xDaxF9VKA8EBEBVa2C3q0qXysOD/5kW3xpJEK/7I5L9fS/1Vj5jIgc/dKjiKDOSzu0D0a9sz9mLVVXWKNXlYY2/8e2nuj7mtBmTJG7B/5CVD4vwl/V3vjom3RK7DCq3CIipvasvktF/7GYHXx4PJd2x+vuDvyaKic6uqddIsp3RGTxOCI7MjswzmJaQEVkIcqH4smOZLVcXFF7ivu7Q2rZXYuR7h54tSDfFOQ1EY8J98CN4tAeCAFPxKDoSkXPKmYHf7c7FsSbJJkFsOnugY+I8iMROUBVg/qTroPDBO2qgoYiMhvlA7FkelO1XFw5WR55k3sDaDoz7VojcjkQq91pvrMWDrsZUIgbkbf5iY6earn403Gc2+OWY9SwJzoyA98XMaep2qB2UzgiO+ypg2MoYnxVe1shN/g+oLwrh0XZJTLPnp3qyI7cIUZOjOKKxNx34DAFqIpITK3eU+hOnsLataWJktpMnPQLEh3DY2QOHJkdphAxVQ3EyIkdwyN3wILERAXYm6C/sR0Zc6uIvMUps8NePDBWRWRhLDFyaLVc/JeJnPm8icSZ05n+64yYDzkyO+xleDVSL/ETqd5qufSTncWpvZ38YWG6c+DDxpjLnc1w2IekDoyYo/xYem21UlxZV8U54UNhZDN6ph+Gtb+pEdll/Bz2ddq8ijFHFIZeeHR7h0Sz/cTJggTWfldEkq6c04F9H6tGRJJY+93aIVG2xUlve1Yj1SWXGGPeVbMavrumDg1wSAxEZH8vXpKgUrpvW9ZDtkXmjt7eJYTe71xxkUODWg+LF766sHXrY6Oc3WEcWgPvGhGJOavh0KDWI6aBd83OEitRVKNrYJkxslRVXdWcQ0NGPVQ1NEaWprsGlo2v7jTj5FyAi901c2gSXFzjrI4ntAfYdGbaUmPk9bUCfafODo2s0tYYeX06M21pzVd79YRWAFF7Pnu4adHBganrVaznrL6sVqOzs3+xGnkUV9fsQFNFPQKxelg+v/kpwIx1Yavw/lpkI3TXyaFJEIpITIX3j1ro0U4BX0XeqRMvKXVwaAQYjbpt3zlaSGcATWX6DxdkUW2AhiO0Q/NkD1VVkEWpTP/hgEajm6LJRji74dCktgNBTnrJXijH1R0SHRxoruyh1jgMkslk+gJiTwoyvS654uBAE0U6RNEXfKqHGGvjSwQzzdVtODR1fQdmmrXxJUaFw0Wmdmavg8PU+2hEhcMNhle46+FAa8Q8XmFUma/Objg0ue3QaJrpfAPMqtUqOUI7NK+Pjjg8ywADbqeJA62xIWPACHS6CIdDa0Q66DREq9McHFoBCdcA69BSSu0KkRxotVkHDg6O0A4OjtAODlOMth7xZQwYaa0zsVXFWkfotoJnBKvKcNFCtcWSSr7QkTJ4RgitOkK3PJk9ITccEo8Lx7yqk9cekma/vhhNL9QKm7ZW+d1TRR58rEihGJDp9NuO1H67WYxcNuDko7v53Edm8vpDOvCTrWU5wrKy8ukiX/j2Bu78xRBdnR7txGnpyAxoe5BZGB4O+N8fnMFV584CC/mSxbbYt21E6Ewb8ISLvr6eS2/cEJG6TVjtt43NyAZ8+JQBrrpgDoWhAGsVzxOM13qJ0uFiiACXnDuLLbmAr9zyAplunzBUp9BN/wEFKlVl1kCMh25aTGfKIwg0mkjSwrAKnoFKoBxx5tP8cV2ZZEJaPgJi2iGiUSlZPvDmfvr7YoyUbcuTObIeUK4oXRmfM9/aT3XEtlyIsi0JHVolljScfGQGSRuScWmLR28YKom4YFKGk47MkOowBG3wub14In1xyx8UfOG3jxdZvabE4YvTDPTHKJUsIkKriZZqdBNnMj5bcyGf/+f13HDrC2zJhW3RxtE2UY6RiiUcsRw8L8W1F8zmzcf2UC6ElKuK3yIHwzBUYjEh2eFx7wM5zrv6eZ5cXcQkDKmEcWG7VvOUnidRqC5QPnraAJedPYv+Ho9cLsQzzavWY6rc5TE0HHLJtzbwlVtfgFoILwy1bWLRbUPo+uQKwPBQyKL5Sa69YA7LjulmpBBSrUahvGZTZd8XUl0e9/06xyevfp7Hni7R2e0hQtvVdbQdoet9daEYogrnnDaNSz66P73dPrlc0BRqXa/Kubzl0uUbuOGWF7CqdKa9tjgAOkJvS60VhrMhhx6c4urzZ3PyG7oZyYdUg8ZV63pV/tmDOS64Zi2PPFlsW1VuS0KLRKq2TbX2hELJolb5xHumc8nfzqS7yyM33FjeekyVOz3yJctlyzdw7Q92rso7+uyO0DRn1sxaJRE3241B16v1kkUprjl/DkuP6qaUDwgaQK1Dq/hGSGU8fvHQMOdf8zwPP75zVfY8oVyJkirtkFBqeUJbCx2pKGz1/PoyXRkPlO2e+n1fyBcsgnLuu6dz0Vkz6e70yQ3vG29dr8qFkuWyb0eqHFilayeqbETI5QJmz4xTrir5YutnSU2r24zQKumk8NPrFnDmKQMM53Ycew4CJZ0SkknDtTdv5C/PWsXPfpsj0+tjDHs1yxhaRQQyvT73P5LnuLNXcdXyjcTjEtWkbOe9+J5QDZRcNuCMN/dz7w0H09MZ/XyrZ79NO3jnSgAD3T7LL53HDy6fx4xen+xQpLhGtq3qqtDdG+PxP5Y4+e9W86mr11INla4aMabSk6pGN1ZXh4cKXHjDOk74+Gp+93SR7l5/TLW39Vk9T8hmA/q7fL5z6Vy+/4V5zByIUQlan8xt0yRrBMqBUsoGvPfkfh749mLe95Z+csMBlR2pdaikU4ZEwnD1TZFa//x3w1Oq1mOq3OfzwKMFjvvoKi7/1gZivuzw4Fevyu86sZcV317E37xtgOJwyEhFt3njOkK3QKYwmw2Y3uPzvc/P4wdfmMe0Hp9sNohqo3ek1n0+f/hjiZPOXcNnr1tLoOxRtdbaDdTV4YERLvrqet708VWRKvf5KDtR5VxAb6fHTZfM5dYr5jNrILbDp5AjdIvA94RyVRnOBrx3WT8rli/m9JP7yGV3otaB0pEyxOPCVcs3cuxZq/jFw/lIrYXd6t0LrSJEXvmBPxQ4/uxVXPr1dXheTZWDnavyO47vZcXyxXzw7QMM50PKldapUXGEnqBa57IBM/t8fnj5Qdz8+XkM1Km17NBb+zyypsiJn1jNhTeswwp0dey6Wo+pctrDeMIl/7yeE85ZxUNPFujujUV7Qrajyn6dKi+/aC63/dNBHDA9Rm4owDO0lSq7QTPj1Xo44P1v7ueB5Yt414mRWleDHXvrzpRHPC5c/q0NHPfRVdz/SIFMrz8WWZm4Ksf4zRMFjj9nFRd/bT3Gk7FwnG7nPQehkh0KePtxPdx/42LOPHWAfD5kpNyequwIPV6tzahax7j1ynncdMlc+ru8Hap1aHVMrVc+XeSEj6/iwhvWQb1ab+fvDMIos2d84dKvr+P4j63iwccKdPf5E1LlTMrjG587kDu+OJ8D94tU2Zj2SJw4Qu+yWod88O0DrFi+mHct7SWXjTKFO1LrrrSHH4vU+o0fW8UDf6ip9ThyhjYieabXZ+VTRZaes4qLvroeMTVVDiamyiuWL+Ksd04jX7BOlR2hJ6DWQwGzpsW49cr5LL9oLj2dHtlcgL8DtaYWCfntk0XeVCMqhjH7MGpTfF/4wjc3cNzZq7j/0QLdvRNT5c6k4WsXHsjtX5zP3BnxmirjVNkReoJqXVGG8wFnnjrAA8sX847je8nuTK2DSK09T7j06+s5/uxVPPhEgUyPT6bH5+HVRU74+Gr+8cvrUIHMDqyJ5wlhCNmhgLcc08OKGxfzsXdPo+BU2c3lmKxaI5Faz5ke47Z/ms/yfx/kH766jhe2VOnO+GM+eluHve5en4eeLPKms1dx0Vn7E/eFz319PfkRS3dv9Hu3G1c2kSr3dft86bwD+Nhp07CBkhsKHJEdoXdfrUfKCuWAD79jgGNf3cmnrl/Lv/9siGSHIRH7n93U9eG4IFQ+8+W1AKRS3pgqsx1VrlaVfC5g2THdXP3J2RyyIEU+G469FwdnOfbM2N2at547I84dX5zPNz93IF2pnXtrYyDT6ZPp9PHMjr1ybjggFRe+/NkD+M9rF7BwTsJ5ZUfoqVfrfMHyt++cxoobF/GWY3rIDgUE4bYVdLSQaFv2ZMwr28grn3hkhvtvXMQnztiPYslSGnFe2RF6r6g15IYCDpoZ585r5vO1Cw+gMyk7VOsdqXLSF6779Bzuuu7gSJW3VDHiVNkRei+rdamsFAqWj71nOituXMyba2odhjvucPG8aMZcdmvAiUdk+NWNizjvr/djZKSmyr5TZUfofanWWwPmz4pz59Xz+fJnDyAVN+S2odb1qhzzhC99ag4/vv5gDjkwSW5rEHWZuG/EEboh1HpEKRYtnzhjOvffuIhlR3fXvHXkg/26uPKbXpfhl99cyN9/MFLlYsk6r+wI3bhqvXBOkv+8dgHXfGoOXckoEpLNRRGMK86bzd1fXsCSg1LktgbOK7s4dDOotUUEzn//fpx6bA+/fiyPWjjiFR3Mn5ukmA+x1kUwHKGbbdxYNqoJee+yfgCCsmV4tDvGqbIjNE24CqNSVUbKwdieF8+psiM0Td517kjsDoV7dCtUqWwpjlg80572Z/TzGzfGgKafZu/7kM2FrH5+BBMz2G3ln2nVEWiKHxOeWVdm81BAzBda/eO3waAZwYbKHb/MIn57Tea0FiQm3PmrLEFFMcYtDWr+LzVU0h0e/3LPFh59okimqz1mJweh0tnhsWrNCN+9azOpDq8t1iS3PKG1blzuWVc8R75k6Ux7VANtycevKlQDJZ00VALlrCufZSgXtoXdaKv50KNL6499dSc3XTSXuQcmCYohlaq2jA0xBuK+4KcNa9dX+Mhlz3L3r7NkMu2xRbbtJvh7npDLh+w/EOPv3jOdU4/rYc5+MVLx1nhQjVSVtS9UuPOXWa6/ZRPPbayS6fLahsxtuZLCM0K5aikXLb19PvNnJ5jW42OQpt7jp8DmbMCatWW2bK4STxmSCdNWZG7bHSujjaiVQClXLLZFvnRjhETcEI/JdjtkcJnCxiam2Y1tsHFfSMY9hNY5AKtGL28SITrVKHbdzDeC36yHHxGhUrWMlC0EbbvIaw/7MSGZEOJxg2pzHpb9plNkIxRKIWFZmT49xjGHpzhkbor9B2IkYpEPdpUTE1d0ASpVZcPmKk/+eYRHVhfZ+EIVE4tG+domsy5+M6lyGEI+F/CqQ9J89NQBlh3VzQH7xTFxR+E9QvCq8vymCnc/mOMbtw+y8vECqQ4P32ueDGtTHAo9ExUYxX3h/35kJh9/1zQ6Oj2qJUu5om1VnzHVhVyJmBBLG0qFkG/cPshF31hPsRyt5miGiEnDE9ozQnEkZEZfjB9efhBveG0XxWzUq9fMC+cbfeWy7wnpbp/fPpLn9H/4I89uqNDR0fgx7YYmtDFQqSj93T4/vWEBr1yYJjsU4PvifPJeQDVQurt9Vv15hJPOXc36wSqJhDS0/TCNrhYo3HTRXF65KCJzzJF5ryHmR0uWFs5LcvMlc8e8tLjipMk1m+ZzIeeePp2lb+gmuzUis8M+IPVQwBten+HTfz2DwnDY0B04DWk5RKLH3bQen99+ZzG9GZ+gTRZHNuqT0vOgOGJ53YeeZu2mCom40IjVqKZh6y2KljNO7GPGjDjlip00mUczZ+5Vs3CT3cZbVfoGYnzgr/qojNiGbRZoyDh0aJV40vDWv+zGVhUzCTZbBbVKLCaTSgO3IkKrVANFZNeXcRoRbFl5y9HdXPXdjQ3bJOE3ot0oV5QDZsQ55MAklUmos7WQTAh+wmPLliqFkm17u6IK6aShvy9GWLGUyrumsmKgXLEcPCfBvFkJnn62TDrReLbDb8RVENVAOXBGnN4uj2J51/ZUW6ukUx5/3lDmC9/ZyC8ezjtC19LcHQnhqMM6ufDMmSyck6BQDCdMaiHK1HZ1eMybmeCJZ0pI0qfR8uJ+QxZsWKW/x8P4Bh0JmCgbrYVE0vDUsyMsO28Nz60dIZ72MALtnksUYIvCzXdu5t7fDHPX9Qs4bEGSYslO2JKpKuIL03o8sDSkSPiNnCHc5YCnRFV451/zPM+tK9PXH6PiKvFehs5+n40vVjjvS89z71cPxpsEKxv5TOK3Ust+OmV4dHWRXz1SoDPjUa46Mo9Huap0ZTwefLzAw08VOWJJB4WibZlZe6Z1PKJiPGH9YECp4jzzjh9kQrVqWftiBfEEbSFD5mZgOuDmcjg4OEI7ODhCOzg4Qjs4QtNKKV6H9r1WppVCUWqVaT1e1P3tiL3DEKfnG6b3xsAqrdQyYVpqUv2I5bCFaf5iYZp8ISQec8Ho8YjFhHzBsuSgJK85JE2pZFtqgZFp5NoDJlN2GhO+dN5sMmmPrUOBU+pxFmNoKCQVF6755GzSKUMwiXI514K1q2VhENVg7CIbPSMUiyFHH9bBvV9dyNIjMyRr9iMac9Wer9HPn4gJb3xtJ3d/5WCOf10XxUK4a3UZEn0/5Vp9jLpajgky2ggvbgkIJlHcb4wwXAh53SFp7r7+YNasLTNcCN04JYXOtGH+7CSegeF8uMtFRiKCBsqmLUEkheoIzUQ6TeIx4U8bywxmA/o6farhrvUTeiaa2I/AvP3jrmOlrlZ8pGxR1V2+JqoQM7B1OOBP68vEGnQBk9+IPi8RN6zfVOXR1SWW/q8M5bzd5TLH0YNOuaKoM9JjlsHUSmwnJTQJw0NPFnl2Q9Qkq06hJ961EgbKbf89xIlv6N6tCyfSmIXozXegVCQm/NvPh6iMWFJJvyH7Ck2jNnOmOjx+dN9WVv9xhFTSYJ3I7lMbmEwYnltb5of3bCHZYRp2o5Zp1PBS3Be2DgVc9u0NxJIG6xi97wQmVOJpw+U3bWTTi1USMdOw4VDTyHv2ujI+N9+1hZvuGCTTH6PiOlD2OipVpbs/xq0/2cK3bh+kK+M39J7Hhh7WKPLSLLUfXTGPZW/sJTdYxXiCC1xMvc0Iw4jM9z2Q5bRPP0MlJJpvpy6xslsjqEKrvPv//Imbbx8k0+eTiAlBGK1McJq9Z3Na1kZPx0RMasq8mdM+/QwjVcX3afizTFMMPDcGghBKJcuH39bPhWfOZN4BCahGcdUgjE7hjtyTLzMQEXwvOvwRF55bW+HKmzbwjdsHScRNRGbrJvjvUfshAsO5kOn9MU4/qY93Ht/DkoNS9HR5iJtMupuHFmUoH/LEn0a47b+H+MHdm9n4QpWuLm9suxZuJQVTsg22UrWMFCyxpGHe/nHmz04woz9GImZcQfQklKJStWzcEvDM2jJ/Wl+mUrIk0oZk3DT0AbBlFm+OLs4MrVKuKNXqaBWO4+ekPYcBP2ZIxmXs2jajNvjNWgY5qhyJuJBKGJcN3APXVFWxddcWt3hzHy25cTbDwTXJOrQqoZ28ObSMczJA2V0HhxZB2SjkX9785ODQlElOFPIGGHT9SQ60ROyRQQOsq/HZKbRD8yp0xOF1RoRnxFkOhyYndFSPwjMGy+Pueji0BCyPG1F+XytU89wVcWhSeKqoKL83xlQeU+yLLtLh0NwRDvuiMZXHTC6X24LKo7XWduuuj0OzGQ0RAZVHc7ncFlOLePy8FvZwCu3QhAotNQ7XajkUvbs2jMX5aIcm9M+KonePElpKuc2/V/Tp2kgdZzscmieuISKKPl3Kbf59bSU5HhCI6r/K6A85ODSLfwZE9V+BAPDMKIFF+Z6qVp3tcGgyu1EV5XujBB8ltMnnNz+F6n0S2Y7AXSuHBkcgIoLqffn85qdq9tma+soOFXMtrvDfgeZpTqnjrDBuu4ABNN018KAx8npVDZ39cGhQhCLiWasPFYcHj6zx2I5X4trCAS5218uhSXBxjbOyvf0vHhCmuwbuMUaWOpV2aGB1vrc4PHjiKGd32CQrfnhBLeLhsocONFzdhmpV/PCCbf2At43f4FVHRjb58VTCGHNsjf3ukOjQKOrsW9Uri9ktt4xX5+2tnKvN0Vngd2SGfiMir3LWw6FRrIaqPlLI9RwBa4LaQVB3NpdDo9eaMsZ8QFVHnPVwaBCrMYIxH4A15Zd4umPLMc56FDbGYumNxsgpzno47GuroZazi7kXf1rjrZ2Ihx5Par9aKa70E6leI+YowKXGHfY2qiISs2qvLw5vvqI2vi5kkmuba36asCMzcJuIvKMW/Yi56+ywt8isqv9WyA2eVqfMOtnZdjpa61HI9Zyh6D0iEsPVejiwV2o1YoreU8j1nDFaq7Gzs5yZ+Dr5NeVCV/IUtXqPiPg1++HgMFXK7KvVewpdyVNqh8AJBSZkF4tBLJDoyAx8X8ScpmqD2mPAjV5y2FPRjFDE+Kr2tkJu8H1EsxfNROv0vV38ywwQVMvFW/1EuseIHFUjs4uAOOx2JAMwImKscl0xN/jhOl5NuOnEm8QdJICplos/9ZPpdQJLRSRe89Xi1NphcqosPlBS4ZxibvCKOoHcpfzH7oTgvGq5uDKWTP8E5LUiZnZtIpNTa4ddVGVjFFaqcGoxO/jjGi91Msk8bzfvLL9aLq6rlov/L5ZMC3BkLQpix5f1OThQ39wKKiIeUEG4spB98YPVcnHtzuLMU0no0Tc26qt/Fkumf4zKHBFZKC91kFtnRRzqQsCIiBERUeUuFc4oZgd/UPdkD9ndobrsscVg0ZtJ9fS/1Vj5jIgcXcvBU/PYxtmRtlRjC/i16Vyo6gpr9KrS0Ob/qBNWyx6oF5IpWkJkAdJdA8tE9ByQk6KMz+jeKsK6m8Apd2sqcW0AaPSsjjLMereqfK04PPiTbfFlj409nwK87I7r6N1vCdaerqqnCnJodKfqqHIzznPLFL83hz1YAVd3eBsVqNq8IqntX9cnROR2jLmlsHXTY9t6orOn5/hPIbx67wR4qUzfazzMCVY5TkRfCTJj7FH08l8caII1EPISiSKB0o2q8gcj/DzE/lcpt2VlHXFNXd5i6hZTsHdazs34GpDOzpkD+NVD1cqrEH2FKguAWUC/QBeQcErdkMpcVhgGNgPrRFiDyuNi9BGC2BP5/IZB/ueCV7s3pnL9fy7lk/7/Lv6HAAAAAElFTkSuQmCC"
)
_AGENT_ICON_PNG = base64.b64decode(_AGENT_ICON_PNG_B64)

async def r_favicon(request):
    return web.Response(body=_FAVICON_PNG, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

async def r_touch_icon(request):
    return web.Response(body=_TOUCH_PNG, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

async def r_agent_icon(request):
    return web.Response(body=_AGENT_ICON_PNG, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


_CSS = """<link rel="icon" type="image/png" href="/favicon.ico"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><style>
:root{color-scheme:dark;--bg:#0B0E14;--card:#12171F;--ctl:#1B2230;--line:#30363D;--txt:#E6EDF3;
      --mut:#8B949E;--dim:#6E7681;--gy:#2EA043;--gyH:#3FB950;--boss:#FFC53D;--red:#F85149;
      --yel:#E3B341;--blue:#58A6FF;--inp:#0B0E14}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:16px;padding-bottom:calc(16px + env(safe-area-inset-bottom));
     font:15px/1.55 -apple-system,'PingFang SC','Helvetica Neue',sans-serif;color:var(--txt);
     background:var(--bg)}
h1{font-size:19px;margin:8px 0 16px;display:flex;align-items:center;gap:8px}
h1,.card,.meta,body>p{max-width:420px;margin-left:auto;margin-right:auto}
.lg{width:64px;height:64px;margin:0 auto 12px;display:flex;align-items:center;justify-content:center;
    background:var(--ctl);border:1px solid var(--line);border-radius:16px}
/* 品牌头（登录页用） */
.hero{text-align:center;padding:28px 0 18px;max-width:420px;margin:0 auto}
.hero h1{border:0;float:none;font-size:21px;margin:0 0 4px;letter-spacing:.5px;justify-content:center}
.hero .sub{color:var(--mut);font-size:13px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}
input{width:100%;background:var(--inp);border:1px solid var(--line);border-radius:8px;color:var(--txt);
      padding:11px 12px;margin-bottom:10px;font-size:16px;outline:none;
      transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--gy);box-shadow:0 0 0 3px rgba(46,160,67,.22)}
input::placeholder{color:var(--dim)}
button{border:0;border-radius:8px;padding:11px 16px;font-size:15px;background:var(--gy);color:#fff;
       font-weight:600;cursor:pointer;transition:background .15s,transform .15s}
button:hover{background:var(--gyH)}
button:active{transform:scale(.98)}
button.gray{background:var(--ctl);color:#C9D1D9;border:1px solid var(--line);font-weight:500}
button.gray:hover{background:#232C3A}
button:disabled{opacity:.5;transform:none}
/* 分段 tab（登录/注册） */
.tabs{display:flex;background:var(--ctl);border:1px solid var(--line);border-radius:8px;padding:3px;margin-bottom:14px}
.tabs .tab{flex:1;background:none;border:0;border-radius:6px;padding:8px 0;color:var(--mut);font-weight:500}
.tabs .tab.on{background:#222B3A;color:var(--txt)}
.row{display:flex;align-items:center;gap:10px}
.meta{color:var(--mut);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.dot.on{background:#3FB950}
.err{color:var(--red);font-size:13.5px;min-height:20px;margin:4px 0}
a{color:var(--blue)}
.center{text-align:center}
.badge{display:inline-block;font-size:11px;color:var(--mut);background:var(--ctl);border:1px solid var(--line);
       border-radius:10px;padding:2px 8px;margin-left:6px;font-weight:500;vertical-align:middle}
.ic{display:inline-flex;align-items:center;justify-content:center}
.pw{position:relative}
.pw input{padding-right:48px;margin-bottom:0}
.pw .pe{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:36px;height:32px;
        background:none;border:0;padding:0;color:var(--mut);font-weight:400;
        display:flex;align-items:center;justify-content:center}
.pw .pe:active{transform:translateY(-50%) scale(.95)}
</style>"""

_PAGE_LOGIN = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
<title>CodeBoss · 登录</title>__CSS__</head><body>
<!-- App 壳吞掉系统返回键：没有这个链接，从扫码页进来就出不去 -->
<a href="/" id="pgback" style="position:fixed;top:calc(env(safe-area-inset-top) + 10px);left:12px;z-index:9;color:#58A6FF;text-decoration:none;font-size:14px;-webkit-tap-highlight-color:transparent">‹ 返回</a>
<div class="hero">
  <div class="lg"><img src="/apple-touch-icon.png" width="40" height="40" alt="CodeBoss" style="border-radius:9px"></div>
  <h1>CodeBoss</h1>
  <p class="sub">手机派活，电脑干活</p>
</div>
<div class="card" id="promoBar" style="display:none">
  <div style="display:flex;align-items:center;gap:10px">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#FFB300" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2l2.4 7.1H22l-6 4.5 2.3 7.2L12 16.4l-6.3 4.4L8 13.6l-6-4.5h7.6z"/></svg>
    <div style="flex:1;min-width:0">
      <div id="promoTxt" style="font-size:14px;font-weight:600;line-height:1.5"></div>
      <div class="meta" id="promoSub" style="margin:2px 0 0"></div>
    </div>
  </div>
</div>
<div class="card" id="v-login">
  <div class="tabs">
    <button type="button" class="tab on" id="tab-login" onclick="show('login')">登录</button>
    <button type="button" class="tab" id="tab-reg" onclick="show('reg')">注册</button>
  </div>
  <input id="u" placeholder="手机号 / 邮箱 / 用户名" autocomplete="username">
  <div class="pw" id="pwrap">
    <input id="p" type="password" placeholder="密码" autocomplete="current-password">
    <button type="button" class="pe" id="pe" onclick="togglePw(event)" title="显示/隐藏密码"
            aria-label="显示/隐藏密码"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg></button>
  </div>
  <div id="codewrap" style="display:none">
    <div class="row"><input id="lc" placeholder="验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0">
      <button type="button" class="gray" id="lcb" style="flex:none" onclick="sendLoginCode()">获取验证码</button></div>
  </div>
  <p class="meta" style="margin:8px 0 0"><a href="javascript:void(0)" id="lmode"
     onclick="toggleLoginMode()">用验证码登录</a></p>
  <div class="err" id="e">__ERR__</div>
  <button style="width:100%" onclick="login()">登录</button>
  <p class="meta center" style="margin:12px 0 0"><a href="javascript:void(0)" onclick="show('forgot')">忘记密码？</a></p>
</div>
<div class="card" id="v-reg" style="display:none">__NOCHAN_REG__
  <div class="tabs">
    <button type="button" class="tab" id="tab-login2" onclick="show('login')">登录</button>
    <button type="button" class="tab on" id="tab-reg2" onclick="show('reg')">注册</button>
  </div>
  <div class="tabs">
    <button type="button" class="tab on" id="rm-phone" onclick="regMode('phone')">手机号注册</button>
    <button type="button" class="tab" id="rm-mail" onclick="regMode('mail')">邮箱注册</button>
  </div>
  <div id="reg-mail" style="display:none">
    <input id="rem" type="email" placeholder="邮箱" autocomplete="email"
           inputmode="email">
    <div class="row"><input id="rc" placeholder="邮箱验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0">
      <button type="button" class="gray" id="rcb" style="flex:none" onclick="sendCode('rcb','rem','register','re')">获取验证码</button></div>
  </div>
  <div id="reg-phone">
    <input id="rph" type="tel" placeholder="手机号（中国大陆）" inputmode="numeric" maxlength="11">
    <div class="row"><input id="rsc" placeholder="短信验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0">
      <button type="button" class="gray" id="rscb" style="flex:none" onclick="sendSms('rscb','rph','register','re')">获取验证码</button></div>
  </div>
  <div class="pw" style="margin-top:10px">
    <input id="rpw" type="password" placeholder="设置密码（可选，≥6 位；不填则用验证码）"
           autocomplete="new-password">
  </div>
  <p class="meta" style="margin:10px 0 0;line-height:1.7">不设密码时，初始密码就是注册收到的验证码，登录后可在用户设置中修改；忘记密码可以用手机号重置。</p>
  <div class="err" id="re"></div>
  <div class="row">
    <button style="flex:1" onclick="reg()">注册</button>
    <button type="button" class="gray" style="flex:1" onclick="show('login')">返回登录</button>
  </div>
  <p class="meta center" style="margin:12px 0 0;line-height:1.7">注册即表示同意
    <a href="/r/terms">《服务条款》</a>和
    <a href="/r/privacy">《隐私政策》</a></p>
</div>
<div class="card" id="v-forgot" style="display:none">__NOCHAN_FG__
  <div class="tabs">
    <button type="button" class="tab on" id="fm-mail" onclick="fgMode('mail')">邮箱找回</button>
    <button type="button" class="tab" id="fm-phone" onclick="fgMode('phone')">手机号找回</button>
  </div>
  <div id="fg-mail">
    <input id="fu" type="email" placeholder="注册邮箱" autocomplete="email" inputmode="email">
    <div class="row"><input id="fc" placeholder="邮箱验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0">
      <button type="button" class="gray" id="fcb" style="flex:none" onclick="sendCode('fcb','fu','reset','fe')">获取验证码</button></div>
    <div class="pw" style="margin-top:10px">
      <input id="fp" type="password" placeholder="新密码（≥6 位）" autocomplete="new-password">
    </div>
  </div>
  <div id="fg-phone" style="display:none">
    <input id="fph" placeholder="手机号" inputmode="numeric" maxlength="11">
    <div class="row"><input id="fsc" placeholder="短信验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0">
      <button type="button" class="gray" id="fscb" style="flex:none" onclick="sendSms('fscb','fph','reset','fe')">获取验证码</button></div>
    <div class="pw" style="margin-top:10px">
      <input id="fpp" type="password" placeholder="新密码（≥6 位）" autocomplete="new-password">
    </div>
  </div>
  <div class="err" id="fe"></div>
  <div class="row">
    <button style="flex:1" onclick="forgot()">重置密码</button>
    <button type="button" class="gray" style="flex:1" onclick="show('login')">返回登录</button>
  </div>
</div>
<p class="meta center">登录后扫码/输入配对码，把你的开发机挂到账号下。</p>
<script>
var pair=new URLSearchParams(location.search).get('p')||'';
var PRD=new URLSearchParams(location.search).get('product')||'';
/* boss 系列品牌位：?product=agentboss 时换 title/h1/图标/下载链接（布局与逻辑不动）。 */
if(PRD==='agentboss'){document.title='AgentBoss · 登录';
  var h1=document.querySelector('.hero h1');if(h1)h1.textContent='AgentBoss';
  var sub=document.querySelector('.hero .sub');if(sub)sub.textContent='手机派活，电脑干活';
  Array.from(document.querySelectorAll('a[href="/codeboss.apk"]')).forEach(function(a){a.href='/agentboss.apk';});
  Array.from(document.querySelectorAll('.lg img')).forEach(function(i){i.src='/agentboss-icon.png';});
  var lk=document.querySelector('link[rel=icon]');if(lk)lk.href='/agentboss-icon.png';}
function $(id){return document.getElementById(id)}
function show(v){['login','reg','forgot'].forEach(function(x){$('v-'+x).style.display=(x===v?'':'none')});
  $('tab-login').className='tab'+(v==='login'?' on':'');
  $('tab-reg').className='tab'+(v==='reg'?' on':'');
  /* 注册卡片里已经有「注册即表示同意《服务条款》和《隐私政策》」，
     页脚那份在注册视图里藏掉，不然同一屏出现两遍。 */
  var f=$('pfoot'); if(f) f.style.display=(v==='reg'?'none':'');}
function togglePw(ev){ev.preventDefault();var p=$('p'),on=p.type==='password';p.type=on?'text':'password';
  $('pe').innerHTML=on?'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 12s3.5-6.5 10-6.5c2.2 0 4.1.7 5.7 1.7M22 12s-3.5 6.5-10 6.5c-2.2 0-4.1-.7-5.7-1.7"/><path d="M3 3l18 18"/></svg>':'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg>';try{p.focus()}catch(e){}}
/* 老安卓 WebView（≤Chromium 66）的 fetch 不落地 Set-Cookie，登录态会丢；一律走 XHR */
function post(ep,body,cb){var x=new XMLHttpRequest();x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');
  var done=false,timedOut=false;
  function fin(d){if(done)return;done=true;cb(d)}
  /* 不能永远停在「发送中…」：15 秒没回音就明说，按钮放开让用户重试。
     （中继那边已经改成最多等 6 秒、发信转后台；这里是网络层兜底。） */
  x.timeout=15000;
  x.ontimeout=function(){timedOut=true};
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};
    if(timedOut)d={ok:false,error:'等待超时：若已收到验证码可以直接填；没收到就再点一次'};
    else if(!x.status)d={ok:false,error:'网络不通，请检查连接后重试'};
    else{try{d=JSON.parse(x.responseText)}catch(e){}}
    fin(d)};
  x.send(JSON.stringify(body))}
function go(ep,body,errId){post(ep,body,function(d){if(d.ok)location.href=d.to||'/r/';else $(errId).textContent=d.error||'失败'})}
function isPhone(s){return /^1[3-9]\d{9}$/.test(String(s||'').replace(/\D/g,''))}
var LGM='pass';
function toggleLoginMode(){LGM=(LGM==='pass'?'code':'pass');
  $('pwrap').style.display=(LGM==='pass'?'':'none');
  $('codewrap').style.display=(LGM==='code'?'':'none');
  $('lmode').textContent=(LGM==='pass'?'用验证码登录':'用密码登录');}
function sendLoginCode(){   /* 手机号发短信码，邮箱发邮箱码 */
  var id=$('u').value.trim();
  if(isPhone(id))sendSms('lcb','u','login','e');
  else if(id.indexOf('@')>0)sendCode('lcb','u','login','e');
  else $('e').textContent='请先填手机号或邮箱';
}
function login(){var ident=$('u').value.trim();
  if(LGM==='code')go('/r/login',{ident:ident,code:$('lc').value.trim(),pair:pair,product:PRD},'e');
  else go('/r/login',{ident:ident,pass:$('p').value,pair:pair,product:PRD},'e');}
function cool(btn){btn.disabled=true;var t=60;btn.textContent=t+'s';
  var iv=setInterval(function(){t--;btn.textContent=t+'s';
    if(t<=0){clearInterval(iv);btn.disabled=false;btn.textContent='获取验证码'}},1000);}
function sendCode(btnId,inputId,purpose,errId){
  var body={purpose:purpose,email:$(inputId).value.trim()};
  var btn=$(btnId),err=$(errId);btn.disabled=true;err.textContent='发送中…';
  post('/r/send_code',body,function(d){err.textContent=d.msg||d.error||'';
    if(d.ok)cool(btn);else btn.disabled=false;});
}
function sendSms(btnId,inputId,purpose,errId){
  var btn=$(btnId),err=$(errId);btn.disabled=true;err.textContent='发送中…';
  post('/r/send_sms',{purpose:purpose,phone:$(inputId).value.trim()},function(d){
    err.textContent=d.msg||d.error||'';if(d.ok)cool(btn);else btn.disabled=false;});
}
var REGM='phone';
function regMode(m){REGM=m;
  $('reg-mail').style.display=(m==='mail'?'':'none');
  $('reg-phone').style.display=(m==='phone'?'':'none');
  $('rm-mail').className='tab'+(m==='mail'?' on':'');
  $('rm-phone').className='tab'+(m==='phone'?' on':'');}
function reg(){
  var pw=$('rpw').value.trim();          /* 留空 = 老行为：默认密码=验证码 */
  if(pw&&pw.length<6){$('re').textContent='密码至少 6 位';return}
  if(REGM==='phone')go('/r/register',{phone:$('rph').value.trim(),
    code:$('rsc').value.trim(),pass:pw,pair:pair,product:PRD},'re');
  else go('/r/register',{email:$('rem').value.trim(),
    code:$('rc').value.trim(),pass:pw,pair:pair,product:PRD},'re');
}
var FGM='mail';
function fgMode(m){FGM=m;
  $('fg-mail').style.display=(m==='mail'?'':'none');
  $('fg-phone').style.display=(m==='phone'?'':'none');
  $('fm-mail').className='tab'+(m==='mail'?' on':'');
  $('fm-phone').className='tab'+(m==='phone'?' on':'');}
function forgot(){
  var body=FGM==='phone'?{phone:$('fph').value.trim(),code:$('fsc').value.trim(),pass:$('fpp').value}
                        :{email:$('fu').value.trim(),code:$('fc').value.trim(),pass:$('fp').value};
  post('/r/reset',body,function(d){if(d.ok){show('login');$('e').textContent='密码已重置，请用新密码登录'}
    else $('fe').textContent=d.error||'失败'});
}
$('p').addEventListener('keydown',function(ev){if(ev.key==='Enter')login()});
$('u').addEventListener('keydown',function(ev){if(ev.key==='Enter')login()});
/* 活动横幅：后台开了「注册赠送」+「显示横幅」且在活动期内才出现（XHR，老 WebView 一律不用 fetch） */
(function(){
  try{
    var x=new XMLHttpRequest();
    x.open('GET','/r/promo',true);
    x.onreadystatechange=function(){
      if(x.readyState!==4)return;
      var d={};try{d=JSON.parse(x.responseText)}catch(e){return}
      if(!d.ok||!d.banner)return;
      var b=$('promoBar'),t=$('promoTxt'),s=$('promoSub');if(!b||!t)return;
      t.textContent=d.note||d.text||'';
      if(d.until){
        var dt=new Date(d.until*1000);function z(n){return (n<10?'0':'')+n}
        s.textContent='活动至 '+dt.getFullYear()+'-'+z(dt.getMonth()+1)+'-'+z(dt.getDate());
      }
      b.style.display='';
    };
    x.send();
  }catch(e){}
})();
document.addEventListener('DOMContentLoaded',function(){
  var b=document.body, d=document.createElement('div');
  d.id='pfoot'; d.className='meta center'; d.style.marginTop='18px';
  d.innerHTML='<a href="/r/terms" style="color:var(--mut)">服务条款</a>'
    +'<span style="color:var(--line)"> · </span>'
    +'<a href="/r/privacy" style="color:var(--mut)">隐私政策</a>';
  b.appendChild(d);
});
</script></body></html>"""

# 验证码通道一个都没配（自建常见）：注册/找回卡只剩提示条，不留死按钮。
# 官方或任一通道可用时为空串，页面原样。
_NOCHAN_CSS = '<style>%s > *:not(.nochan-hint){display:none}</style>'
_NOCHAN_REG = ("" if (SMS_ON or SMTP_HOST) else
               (_NOCHAN_CSS % "#v-reg"
                + '<div class="err nochan-hint" style="display:block">'
                  '运营者未配置验证码通道，暂时无法注册，请联系服务器管理员。</div>'))
_NOCHAN_FG = ("" if (SMS_ON or SMTP_HOST) else
              (_NOCHAN_CSS % "#v-forgot"
               + '<div class="err nochan-hint" style="display:block">'
                 '运营者未配置验证码通道，暂时无法找回密码，请联系服务器管理员。</div>'))
_PAGE_LOGIN = (_PAGE_LOGIN.replace("__NOCHAN_REG__", _NOCHAN_REG)
               .replace("__NOCHAN_FG__", _NOCHAN_FG))

_PAGE_DEV = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>aiboss 系列 · 接入开发机</title>__CSS__</head><body>
<h1><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#C9D1D9" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M1 20h22"/></svg> 接入开发机 <span class="badge" id="quota"></span><a href="/r/account" style="margin-left:auto;font-size:14px;text-decoration:none">账号中心</a></h1>
<div id="devs"></div>
<div class="card">
  <div class="meta" id="nodev" style="display:none;margin-bottom:8px;line-height:1.7">还没有开发机？在你的开发电脑上安装 aiboss 系列的开发机客户端（口袋工头 CodeBoss / 代理工头 AgentBoss），然后扫码接入。</div>
  <div class="meta" style="margin-bottom:6px;line-height:1.7">在电脑上：Mac 点菜单栏图标 · Windows 右键托盘图标（可能在 ^ 里）· Linux 终端输入 codeboss，选「显示配对二维码」；也可以把配对码粘到下面。码 15 分钟内有效。</div>
  <button id="scanBtn" style="display:none;width:100%;margin-bottom:10px" onclick="scanPair()">📷 扫码配对</button>
  <div class="row"><input id="pc" placeholder="或粘贴配对码" style="flex:1;margin:0">
  <button onclick="pairDev()">配对</button></div>
  <div class="err" id="e"></div>
</div>
<div class="row"><button class="gray" style="flex:1" onclick="location='/r/logout'">退出登录</button></div>
<div class="meta center" style="margin-top:14px">
  <a href="http://__DLHOST__:8088/#dl" onclick="return cbDl(this)">下载开发机客户端</a>
</div>
<div class="meta center" style="margin-top:6px">
  <a href="/r/terms" style="color:var(--mut)">服务条款</a>
  <span style="color:var(--line)"> · </span>
  <a href="/r/privacy" style="color:var(--mut)">隐私政策</a>
</div>
<script>
function get(ep,cb){var x=new XMLHttpRequest();x.open('GET',ep,true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};try{d=JSON.parse(x.responseText)}catch(e){}cb(d)};
  x.send(null)}
function post(ep,body,cb){var x=new XMLHttpRequest();x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};try{d=JSON.parse(x.responseText)}catch(e){}cb(d)};
  x.send(JSON.stringify(body))}
function acct(){
  get('/r/account',function(d){if(d.ok){uname.textContent=d.name;uemail.textContent=d.email||'未绑定（建议绑定，用于找回密码）';}})}
var bcd=0;
function sendBindCode(){ if(bcd>0)return;
  post('/r/send_code',{purpose:'bind',email:be.value.trim()},function(d){
    ae.textContent=d.ok?(d.msg||'验证码已发送'):(d.error||'发送失败');
    if(!d.ok)return;
    bcd=60;bcb.disabled=true;bcb.textContent='60s';
    var t=setInterval(function(){bcd--;bcb.textContent=bcd>0?bcd+'s':'发验证码';
      if(bcd<=0){clearInterval(t);bcb.disabled=false;}},1000);
  });
}
function bindEmail(){
  post('/r/bind_email',{email:be.value.trim(),code:bc.value.trim()},function(d){
    if(d.ok){ae.textContent='邮箱已绑定：'+d.email;bc.value='';acct();}
    else ae.textContent=d.error||'绑定失败';
  });
}
function changePass(){
  post('/r/changepass',{old:op.value,new:np.value},function(d){
    if(d.ok){ae.textContent='密码已修改，其他设备已下线';op.value=np.value='';}
    else ae.textContent=d.error||'修改失败';
  });
}
function load(){
  get('/r/devices',function(d){
    quota.textContent=d.quota||'';if(!d.quota)quota.style.display='none';
    devs.innerHTML=(d.devices||[]).map(function(x){
      var st=x.frozen?'已冻结 · 只读（可看历史，禁止操作）':(x.online?'在线':('离线 · 最后心跳 '+(x.last_seen||'—')));
      return '<div class="card row"><span class="dot'+(x.online&&!x.frozen?' on':'')+'"></span>'+
      '<div style="flex:1;min-width:0"><div>'+esc(x.name||x.id.slice(0,8))+'</div>'+
      '<div class="meta">'+st+'</div></div>'+
      (x.online?'<button style="flex:none" onclick="enter(\\''+x.id+'\\')">进入</button>':'')+
      '<button class="gray" style="flex:none" onclick="delDev(\\''+x.id+'\\',\\''+esc(x.name||'')+'\\')">删除</button>'+
      '</div>'}).join('');
    var nd=document.getElementById('nodev');
    if(nd)nd.style.display=(d.devices||[]).length?'none':'block';
  });
}
function delDev(id,name){
  if(!confirm('删除「'+(name||id.slice(0,8))+'」？将清除它在 relay 侧的全部记录，并撤销该开发机的接入注册'))return;
  post('/r/dev_del',{id:id},function(d){if(d.ok)load();else e.textContent=d.error||'删除失败'});
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function pairDev(){
  post('/r/pair',{code:pc.value.trim()},function(d){
    if(d.ok){pc.value='';e.textContent='';location.replace('/');}else e.textContent=d.error||'配对失败';
  });
}
/* 下载开发机客户端：App 里 WebView 打不开官网 http 页、也下不了桌面安装包
   （壳会把这次导航当成断网顶掉整页），交系统浏览器去开（iOS 壳叫 openExternal，
   安卓 0.11.8 起叫 openUrl）。桌面浏览器没有 CB，照常跳转。
   老壳两样都没有：给一句提示，别让它把页面顶成断网。 */
function cbDl(a){
  try{
    if(window.CB){
      var f=CB.openExternal||CB.openUrl;
      var u=(a&&a.href)||'http://__DLHOST__:8088/#dl';
      if(f){f.call(CB,u);return false;}
      if(e)e.textContent='客户端版本较旧，请先更新；也可以在电脑上打开 __DLHOST__:8088 下载';
      return false;
    }
  }catch(x){}
  return true;
}
/* App 内扫码（安卓/iOS 壳都提供 CB.scanQr；桌面浏览器没有就只留输入框） */
function scanPair(){
  if(!(window.CB&&CB.scanQr)){e.textContent='当前客户端不支持扫码，请输入配对码';return;}
  e.textContent='';try{CB.scanQr();}catch(x){e.textContent='相机打不开：'+x;}
}
window.__cbQr=function(txt){
  var s=String(txt||'').trim();
  var m=/[?&]p=([0-9a-zA-Z_-]+)/.exec(s);
  var code=m?m[1]:s;
  if(!/^[0-9a-fA-F]{12}$/.test(code)){e.textContent='没读到配对码，请对准屏幕上的二维码';return;}
  pc.value=code;pairDev();
};
window.__cbQrDenied=function(){e.textContent='没有相机权限：系统设置里允许相机后重试';};
if(window.CB&&CB.scanQr)scanBtn.style.display='';
function enter(id){document.cookie='cb_dev='+id+';path=/;max-age=2592000;samesite=lax';location='/d';}
load();setInterval(load,15000);
</script></body></html>"""
# 下载域名占位符就地落值（值唯一来源 branding.py；服务点零改动）
_PAGE_DEV = _PAGE_DEV.replace("__DLHOST__", branding.RELAY_HOST_DEFAULT)

# ---------- 匿名态页面（2026-11-19 免注册免费版） ----------
# 首页未登录先给「扫码/输码」，登录/注册只从「开通 Pro」「已有账号」进。
_PAGE_ANON = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
<title>CodeBoss · 扫码开始</title>__CSS__</head><body>
<div class="hero">
  <div class="lg"><img src="/apple-touch-icon.png" width="40" height="40" alt="CodeBoss" style="border-radius:9px"></div>
  <h1>CodeBoss</h1>
  <p class="sub">手机派活，电脑干活 · 免费直接用，不用注册</p>
</div>
<div class="card">
  <div class="meta" style="line-height:1.7;margin-bottom:8px">在开发电脑上打开配对二维码，手机扫一下就能用；也可以把配对码粘到下面。<br>
  Mac 点菜单栏图标 · Windows 右键托盘图标 · Linux 终端输入 codeboss，选「显示配对二维码」。码 15 分钟内有效。</div>
  <button id="scanBtn" style="display:none;width:100%;margin-bottom:10px" onclick="scanPair()">📷 扫码开始</button>
  <div class="row"><input id="pc" placeholder="或粘贴配对码" style="flex:1;margin:0">
    <button onclick="pairDev()">开始使用</button></div>
  <div class="err" id="e"></div>
  <p class="meta center" style="margin:12px 0 0">免费版：__FREE__，仅 1 台开发机</p>
</div>
<div class="card">
  __PLANS_A__
  <a href="/r/" style="display:block;text-align:center;color:#8b949e;text-decoration:none;
     margin-top:10px;font-size:14px">已有账号，登录</a>
</div>
<div class="meta center" style="margin-top:14px">
  <a href="http://__DLHOST__:8088/#dl" onclick="return cbDl(this)">下载开发机客户端</a>
</div>
<div class="meta center" style="margin-top:6px">
  <a href="javascript:void(0)" id="sb" style="display:none;color:var(--mut)" onclick="showSelf(true)">使用自建服务器</a>
</div>
<div class="card" id="sv" style="display:none;margin-top:12px">
  <h2 style="margin:0 0 8px;font-size:17px">使用自建服务器</h2>
  <p class="meta" style="line-height:1.7">填你自己部署的 aiboss 中继地址（http(s) 开头），注册和账号都落在那台服务器上，与本页服务器互不相通。</p>
  <div class="row"><input id="su" placeholder="https://relay.example.com" style="flex:1;margin:0">
    <button type="button" class="gray" id="sscan" style="flex:none;display:none" onclick="scanSelf()">📷 扫码</button></div>
  <div class="err" id="se"></div>
  <div class="row">
    <button style="flex:1" onclick="goSelf()">切换</button>
    <button type="button" class="gray" style="flex:1" onclick="showSelf(false)">取消</button>
  </div>
</div>
<script>
var PRD=new URLSearchParams(location.search).get('product')||'';
if(PRD==='agentboss'){document.title='AgentBoss · 扫码开始';
  var h1=document.querySelector('.hero h1');if(h1)h1.textContent='AgentBoss';
  Array.from(document.querySelectorAll('a[href="/codeboss.apk"]')).forEach(function(a){a.href='/agentboss.apk';});
  Array.from(document.querySelectorAll('.lg img')).forEach(function(i){i.src='/agentboss-icon.png';});
  var lk=document.querySelector('link[rel=icon]');if(lk)lk.href='/agentboss-icon.png';}
function $(id){return document.getElementById(id)}
var e=$('e');
function post(ep,body,cb){var x=new XMLHttpRequest();x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');x.timeout=15000;
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};
    if(!x.status)d={ok:false,error:'网络不通，请检查连接后重试'};
    else{try{d=JSON.parse(x.responseText)}catch(x){}}cb(d)};
  x.send(JSON.stringify(body))}
function pairDev(){
  var c=$('pc').value.trim();
  if(!c){e.textContent='请先扫码或填入配对码';return}
  post('/r/pair',{code:c},function(d){
    if(d.ok){e.textContent='';location.replace('/d');}else e.textContent=d.error||'配对失败';
  });
}
function scanPair(){
  if(!(window.CB&&CB.scanQr)){e.textContent='当前客户端不支持扫码，请输入配对码';return;}
  e.textContent='';try{CB.scanQr();}catch(x){e.textContent='相机打不开：'+x;}
}
window.__cbQr=function(txt){
  var s=String(txt||'').trim();
  var m=/[?&]p=([0-9a-zA-Z_-]+)/.exec(s);
  var code=m?m[1]:s;
  if(!/^[0-9a-fA-F]{12}$/.test(code)){e.textContent='没读到配对码，请对准屏幕上的二维码';return;}
  $('pc').value=code;pairDev();
};
window.__cbQrDenied=function(){e.textContent='没有相机权限：系统设置里允许相机后重试';};
if(window.CB&&CB.scanQr)$('scanBtn').style.display='';
function cbDl(a){
  try{ if(window.CB){ var f=CB.openExternal||CB.openUrl;
    var u=(a&&a.href)||'http://__DLHOST__:8088/#dl';
    if(f){f.call(CB,u);return false;}
    if(e)e.textContent='客户端版本较旧，请先更新；也可以在电脑上打开 __DLHOST__:8088 下载';
    return false; } }catch(x){}
  return true;
}
/* 自建服务器入口：只有带 CB.setBase 桥的壳才显示（桌面浏览器/老壳没有，别误导）。
   扫码临时接管 __cbQr：扫到的文本像地址就填进输入框，否则回落配对码流程。 */
var _qrBack=null,_oldQr=window.__cbQr;
window.__cbQr=function(txt){
  if(_qrBack){var f=_qrBack;_qrBack=null;f(String(txt||'').trim());return;}
  _oldQr(String(txt||''));
};
function showSelf(on){
  $('sv').style.display=on?'':'none';
  if(on){try{$('su').focus();}catch(x){}}else{_qrBack=null;$('se').textContent='';}
}
function scanSelf(){
  if(!(window.CB&&CB.scanQr)){$('se').textContent='当前客户端不支持扫码，请手动输入地址';return;}
  _qrBack=function(t){
    if(/^https?:\\/\\//.test(t)){$('su').value=t;$('se').textContent='';}
    else $('se').textContent='二维码里没有服务器地址（要 http(s):// 开头）';
  };
  try{CB.scanQr();}catch(x){_qrBack=null;$('se').textContent='相机打不开：'+x;}
}
function goSelf(){
  var u=($('su').value||'').trim().replace(/\\/+$/,'');
  /* scheme 校验区分大小写：壳的 startsWith 是严格的，这里放宽会两头不一致
     （页面放行、壳静默无视 = 静默吞） */
  if(!/^https?:\\/\\//.test(u)){$('se').textContent='要以 http:// 或 https:// 开头';return;}
  if(!(window.CB&&CB.setBase)){$('se').textContent='当前客户端版本较旧，不支持切换服务器，请更新 App';return;}
  CB.setBase(u);   /* 壳会弹原生确认框，用户点了才真的切 */
}
if(window.CB&&CB.setBase){$('sb').style.display='';if(window.CB&&CB.scanQr)$('sscan').style.display='';}
</script></body></html>"""
_PAGE_ANON = _PAGE_ANON.replace("__DLHOST__", branding.RELAY_HOST_DEFAULT)

# 官方专属入口的按模式渲染：官方原样、自建空串（spec 反目标：不许藏一半）。
# 替换在渲染时做（读当时的 SELFHOST），测试可以 monkeypatch 模式。
_ANON_PLANS_A = """<a href="/r/plans" style="display:block;text-align:center;background:#2EA043;color:#fff;
     text-decoration:none;border-radius:8px;padding:10px">开通 Pro / Max</a>"""
_ANONDEV_PLANS_A = """<a href="/r/plans" style="display:block;text-align:center;background:#2EA043;color:#fff;
     text-decoration:none;border-radius:8px;padding:10px">开通 Pro（绑定账号，解除额度限制）</a>"""
_LOCKED_PLANS_A = """<a href="/r/account" style="display:block;text-align:center;background:#2EA043;color:#fff;
     text-decoration:none;border-radius:8px;padding:10px;margin-top:12px">去开通 / 升级套餐</a>"""
_LOCKED_NOPLANS = ('<p class="meta" style="margin-top:12px;line-height:1.7">'
                   '自建服务器未开通此端：请联系服务器管理员开通权限。</p>')

# 设备页（匿名态）：本机名 + 今日用量/剩余 + 开通 Pro（原账号中心位置）
_PAGE_ANON_DEV = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__NAME__ · 免费版</title>__CSS__</head><body>
<h1><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#C9D1D9" stroke-width="1.6"
  stroke-linecap="round" aria-hidden="true"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M1 20h22"/></svg>
  <span id="nm">__NAME__</span></h1>
<div class="card">
  <div class="meta" style="font-size:14px;line-height:2">
    今日发送：<b>__SENDS__</b>（剩 __SREM__）<br>
    今日语音：<b>__ASR__</b>（剩 __AREM__）<br>
    今日智能调用：<b>__LLM__</b>（剩 __LREM__）
  </div>
  <p class="meta" style="margin:10px 0 0;line-height:1.7">免费版按这台开发机计额度，每天 0 点恢复。额度用完后只能查看，第二天自动恢复。</p>
</div>
<div class="card">
  __PLANS_A__
  <a href="/r/" style="display:block;text-align:center;color:#8b949e;text-decoration:none;
     margin-top:10px;font-size:14px">已有账号，登录后把本机挂到账号下</a>
</div>
<div class="meta center" style="margin-top:14px">
  <a href="/r/terms" style="color:var(--mut)">服务条款</a><span style="color:var(--line)"> · </span><a href="/r/privacy" style="color:var(--mut)">隐私政策</a>
</div>
</body></html>"""


def _free_txt():
    """免费档一句话（数值只在 plans.json 里写一次）。"""
    lim = (plans().get("free") or {}).get("limits") or {}
    return "每天 %s 条发送、%s 秒语音、%s 次智能调用" % (
        lim.get("sends_day") or 0, lim.get("asr_sec_day") or 0, lim.get("llm_day") or 0)


def _anon_landing_html():
    return (_PAGE_ANON.replace("__CSS__", _CSS).replace("__FREE__", _free_txt())
            .replace("__PLANS_A__", "" if SELFHOST else _ANON_PLANS_A))


def _q(used, cap):
    """(已用/上限, 剩余)：cap 0 = 不限。"""
    used, cap = int(used or 0), int(cap or 0)
    if not cap:
        return "%d（不限）" % used, "不限"
    return "%d/%d" % (used, cap), "%d" % max(0, cap - used)


def _anon_dev_page_html(db, dev):
    row = db.execute("SELECT name FROM devices WHERE id=?", (dev,)).fetchone()
    name = (row[0] if row else "") or dev[:8]
    name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    subj = subject_of(db, None, dev)
    lim = plan_limits(db, subj)
    s, sr = _q(sends_used(db, subj), lim.get("sends_day") or 0)
    a, ar = _q(_asr_quota(db, dev)[0], lim.get("asr_sec_day") or 0)
    l, lr = _q(usage_get(db, _llm_key(db, None, dev)), lim.get("llm_day") or 0)
    return (_PAGE_ANON_DEV.replace("__CSS__", _CSS).replace("__NAME__", name)
            .replace("__SENDS__", s).replace("__SREM__", sr)
            .replace("__ASR__", a).replace("__AREM__", ar)
            .replace("__LLM__", l).replace("__LREM__", lr)
            .replace("__PLANS_A__", "" if SELFHOST else _ANONDEV_PLANS_A))

# 端未开通（账号层面权限，2026-11-18）：给端名、现在能用的端、购买入口。
_PAGE_LOCKED = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>未开通 __LABEL__</title>__CSS__</head><body>
<div class="card" style="margin-top:24px">
  <h2 style="margin:0 0 8px">未开通 __LABEL__</h2>
  <p class="meta" style="line-height:1.8">这个账号暂未开通「__LABEL__（__APP__）」。<br>
  aiboss 系列账号按套餐区分可用端，当前可用：<b>__HAVE__</b>。</p>
  __PLANS_UP__
  <a href="/" style="display:block;text-align:center;color:#8b949e;text-decoration:none;
     margin-top:10px;font-size:14px">返回开发机列表</a>
</div></body></html>"""


# ---------- /r/* 处理器 ----------
async def r_index(request):
    if uid_of(request) is None:
        html = _PAGE_LOGIN.replace("__CSS__", _CSS).replace("__ERR__", "")
        # no-store：页面 JS 就是交互逻辑（发码按钮/超时提示），改了必须立刻生效
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    # 已登录：直接进会话列表（/ 会选设备跳 /d）；「我的开发机」页不再作为入口
    raise web.HTTPFound("/")


def _find_user(db, ident):
    """账号标识 → 用户行：手机号（11 位）/ 邮箱（含 @）/ 用户名。"""
    ident = (ident or "").strip()
    if not ident:
        return None
    phone = _norm_phone(ident)
    cols = "id,pass_hash,salt,sess_epoch,name,email,phone,must_change_pw"
    if _PHONE_RE.match(phone):
        return db.execute("SELECT %s FROM users WHERE phone=?" % cols, (phone,)).fetchone()
    if "@" in ident:
        return db.execute("SELECT %s FROM users WHERE lower(email)=?" % cols,
                          (ident.lower(),)).fetchone()
    return db.execute("SELECT %s FROM users WHERE name=?" % cols, (ident,)).fetchone()


def _login_resp(request, db, uid, epoch, data, must_change=False):
    """登录/注册成功的统一收尾：种 cookie + 顺手完成扫码配对。
    登录后直接进会话列表：/ 会自动选中名下的设备并跳到 /d（没设备时才出配对页）。
    must_change（注册后还在用默认密码）→ 同样直接进（提示在注册页上讲过了），
    改密码入口在账号中心，首次改不用旧密码。"""
    resp = {"ok": True, "to": "/",
            "must_change_pw": bool(must_change),
            "products": _allowed_products(db, uid)[0]}   # 壳可在登录后立刻提示
    pair = (data.get("pair") or "").strip()
    if pair:  # 扫码登录：登录成功即顺手完成配对（别人名下的机器同样要走确认）
        r, extra = _do_pair(db, uid, pair, bool(data.get("takeover")))
        resp["paired"] = r == "ok"
        if r == "need":
            resp["need_confirm"] = True
            resp["req"] = extra
            resp["pair_error"] = "这台电脑已绑定其他账号，需要原账号确认"
            try:   # 顺手推给目标机器弹本机确认框（这里在协程里，不能 await 就排进事件循环）
                asyncio.get_running_loop().create_task(_tk_notify(request.app, db, extra))
            except Exception:
                pass
    out = web.json_response(resp)
    out.set_cookie("r_sess", make_sess(request.app["key"], uid, epoch),
                   max_age=86400 * 7, httponly=True, samesite="Lax",
                   secure=(request.scheme == "https"))
    bg_cookie(out, request, uid, epoch)   # 壳的后台提醒服务要读（非 httponly）
    return out


def _auto_name(db, email, phone):
    """注册不再让用户填用户名：手机号/邮箱本地部分直接当账号名，撞了就加数字后缀。
    账号名只用于展示和后台排查，登录用手机号/邮箱，用户不必记住它。"""
    base = (phone or (email or "").split("@")[0] or "user").strip()
    base = _re.sub(r"[^\w.@-]", "", base)[:32] or "user"
    name, i = base, 1
    while db.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        i += 1
        name = "%s%d" % (base, i)
    return name


async def r_register(request):
    """注册：手机号或邮箱 + 验证码，用户名自动生成。
    可以带自设密码（data.pass，≥6 位）——不再默认拿验证码当密码；
    老客户端不带 pass 时仍退回「默认密码=验证码」+ 强制改密，保持兼容。"""
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    phone = _norm_phone(data.get("phone"))
    code = (data.get("code") or "").strip()
    ip = request.remote or "?"
    if not rate_ok(ip, "auth", 30):
        return web.json_response({"ok": False, "error": "尝试过多，1 小时后再试"})
    if not code:
        return web.json_response({"ok": False, "error": "请填验证码"})
    db = _db(request.app)
    if phone:
        if not _PHONE_RE.match(phone):
            return web.json_response({"ok": False, "error": "手机号格式不正确"})
        if db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            return web.json_response({"ok": False, "error": "该手机号已注册，直接登录"})
        if not await _phone_verify(db, phone, code, "register"):
            return web.json_response({"ok": False, "error": "短信验证码错误或已过期"})
        email = ""
    elif email:
        if not _EMAIL_RE.match(email):
            return web.json_response({"ok": False, "error": "邮箱格式不正确"})
        if db.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
            return web.json_response({"ok": False, "error": "该邮箱已注册，直接登录"})
        if not _use_code(db, email, code, "register"):
            return web.json_response({"ok": False, "error": "邮箱验证码错误或已过期"})
        phone = ""
    else:
        return web.json_response({"ok": False, "error": "注册需要手机号或邮箱验证码"})
    name = _auto_name(db, email, phone)
    salt = secrets.token_hex(8)
    # 注册页可以带自设密码；没带才退回「默认密码=本次验证码」并强制改密。
    # （6 位数字验证码 + 手机号可枚举，能自设就别用验证码当密码。）
    pw0 = str(data.get("pass") or data.get("password") or "").strip()
    use_pw = pw0 if len(pw0) >= 6 else code
    must = 0 if use_pw != code else 1
    prod = branding.valid_product(str(data.get("product") or ""))
    try:
        cur = db.execute(
            "INSERT INTO users(name,pass_hash,salt,created,sess_epoch,email,phone,must_change_pw,product) "
            "VALUES(?,?,?,?,0,?,?,?,?)",
            (name, hash_pw(use_pw, salt), salt, time.time(), email, phone, must, prod))
    except sqlite3.IntegrityError:
        return web.json_response({"ok": False, "error": "该手机号或邮箱已被占用"})
    db.commit()
    print("[relay] 新账号 %s（%s）ip=%s %s"
          % (name, "手机" if phone else "邮箱", ip,
             "自设密码" if must == 0 else "默认密码=注册验证码"), flush=True)
    _gifted, _why = promo_grant(db, cur.lastrowid)
    if _alert_cfg(db)["reg"]:
        # 有人注册了给运营发一条（走「提醒」里配好的渠道）；失败只写日志，不影响注册
        asyncio.create_task(_alert_send(
            request.app, "🆕 新用户注册",
            "账号：%s\n方式：%s %s\nIP：%s" % (name, "手机" if phone else "邮箱",
                                            phone or email, ip)))
    return _login_resp(request, db, cur.lastrowid, 0, data, must_change=bool(must))


async def r_login(request):
    """登录：ident 可以是用户名 / 邮箱 / 手机号。
    验证码登录（只传 code 不传 pass）：手机号走短信码，邮箱走邮箱码。"""
    data = await request.json()
    ident = (data.get("ident") or data.get("account") or data.get("name") or
             data.get("phone") or data.get("email") or "").strip()
    ip = request.remote or "?"
    if not rate_ok(ip, "auth", 30):
        return web.json_response({"ok": False, "error": "尝试过多，1 小时后再试"})
    db = _db(request.app)
    phone = _norm_phone(ident)
    code = (data.get("code") or "").strip()
    if code and not data.get("pass"):
        if _PHONE_RE.match(phone):
            if not await _phone_verify(db, phone, code, "login"):
                return web.json_response({"ok": False, "error": "验证码错误或已过期"})
            row = db.execute("SELECT id,sess_epoch,must_change_pw FROM users WHERE phone=?",
                             (phone,)).fetchone()
            if not row:
                return web.json_response({"ok": False, "error": "该手机号未注册"})
        elif "@" in ident:
            email = ident.lower()
            if not _use_code(db, email, code, "login"):
                return web.json_response({"ok": False, "error": "验证码错误或已过期"})
            row = db.execute("SELECT id,sess_epoch,must_change_pw FROM users "
                             "WHERE lower(email)=?", (email,)).fetchone()
            if not row:
                return web.json_response({"ok": False, "error": "该邮箱未注册"})
        else:
            return web.json_response({"ok": False, "error": "验证码登录请填手机号或邮箱"})
        return _login_resp(request, db, row[0], row[1] or 0, data, must_change=bool(row[2]))
    pw = data.get("pass") or ""
    row = _find_user(db, ident)
    if not row or not hmac.compare_digest(row[1], hash_pw(pw, row[2])):
        return web.json_response({"ok": False, "error": "账号或密码错误"})
    return _login_resp(request, db, row[0], row[3] or 0, data, must_change=bool(row[7]))


async def r_reset(request):
    """忘记密码：邮箱（注册邮箱 + 邮箱验证码）或手机号（手机号 + 短信验证码）+ 新密码。
    成功后登录态全部作废（epoch+1），并且不再算「默认密码」——用户刚亲手设了新密码。"""
    data = await request.json()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = _norm_phone(data.get("phone"))
    pw = data.get("pass") or ""
    code = (data.get("code") or "").strip()
    ip = request.remote or "?"
    if not rate_ok(ip, "auth", 30):
        return web.json_response({"ok": False, "error": "尝试过多，1 小时后再试"})
    if len(pw) < 6:
        return web.json_response({"ok": False, "error": "密码至少 6 位"})
    db = _db(request.app)
    if phone:
        row = db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if not row or not await _phone_verify(db, phone, code, "reset"):
            return web.json_response({"ok": False, "error": "验证码错误，或该手机号未注册"})
        uid = row[0]
    else:
        # 兼容老页面传来的用户名：按用户名找到绑定邮箱后再核销验证码
        if not email and name:
            r0 = db.execute("SELECT email FROM users WHERE name=?", (name,)).fetchone()
            email = (r0[0] or "").lower() if r0 else ""
        row = db.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not row or not email or not _use_code(db, email, code, "reset"):
            return web.json_response({"ok": False, "error": "验证码错误，或该邮箱未注册"})
        uid = row[0]
    salt = secrets.token_hex(8)
    db.execute("UPDATE users SET pass_hash=?,salt=?,sess_epoch=sess_epoch+1,must_change_pw=0 "
               "WHERE id=?", (hash_pw(pw, salt), salt, uid))
    db.commit()
    print("[relay] 密码已重置 %s ip=%s" % (_mask_phone(phone) if phone else "email", ip),
          flush=True)
    return web.json_response({"ok": True, "to": "/r/"})


async def r_logout(request):
    """退出登录 = 这台客户端不再用这个账号：把它占的客户端名额当场还回去。

    客户端数按「最近 24 小时来过的 cb_cid 去重」算（clients_used），以前退出只清
    cookie、名额还挂着一整段窗口——用户换手机/把旧手机给家人用时，明明退出了却提示
    「客户端已满」。这里删掉本机 cid 那一行；同一台手机重新登录会立刻重新登记，
    净效果就是「退出一个，进来一个」。
    只按 (uid, cid) 删：删不到就什么都不做，绝不误伤同账号其它设备。"""
    try:
        uid = uid_of(request)
        cid = (request.cookies.get("cb_cid") or "").strip()
        if uid and cid and len(cid) <= 64:
            db = _db(request.app)
            _delete_client(db, int(uid), cid)
            # 名额刚空出来：被挡住的其它客户端可能还缓存着「已满」。
            # 只清这个账号的判定缓存，下一次请求立刻重新算（最多等 60 秒太反直觉）。
            for k in [k for k in _CLI_GATE if k[0] == int(uid)]:
                _CLI_GATE.pop(k, None)
    except Exception:
        pass   # 退出登录绝不能因为记账失败而退不掉
    out = web.HTTPFound("/r/")
    out.del_cookie("r_sess")
    out.del_cookie("cb_dev")
    out.del_cookie(BG_COOKIE, path=BG_PATH)   # 后台提醒通道一起退掉
    raise out


async def r_update(request):
    """上传新 relay.py 自更新（网页管理后台 /r/admin 或 curl 均可）。Bearer RELAY_ADMIN_TOKEN
    鉴权（未设置则功能整体关闭）；py_compile 校验通过才原子替换，1s 后原地 execv 重启
    （PID 不变，systemd 无感）。接受 multipart 字段 f= 或裸 body。"""
    if not ADMIN_TOKEN:
        return web.json_response({"ok": False, "error": "未启用：服务端需设置 RELAY_ADMIN_TOKEN"}, status=403)
    if request.headers.get("authorization", "") != "Bearer %s" % ADMIN_TOKEN:
        return web.json_response({"ok": False, "error": "无权更新"}, status=401)
    if request.content_type == "multipart/form-data":
        data, reader = b"", await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "f":
                data = await part.read(decode=False)
                break
    else:
        data = await request.read()
    data = _unwrap_update(data)
    if not _looks_like_update(data):
        return web.json_response({"ok": False, "error": "更新物校验未通过（源码特征/平台魔数不符）"}, status=400)
    ok, err = await _apply_update(request.app, data)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    return web.json_response({"ok": True, "bytes": len(data), "restarted": True})


def _unwrap_update(data):
    """上传物预处理：release 压缩包（tar.gz，内含 codeboss-relay 或 relay.py）自动解出
    载荷，裸二进制/源码原样返回；解不出也原样交给 _looks_like_update 报错。"""
    if data[:2] != b"\x1f\x8b":
        return data
    try:
        import io, tarfile
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        for m in tf.getmembers():
            base = os.path.basename(m.name)
            if base in ("codeboss-relay", "relay.py"):
                f = tf.extractfile(m)
                if f:
                    return f.read()
    except Exception:
        pass
    return data


def _looks_like_update(data):
    """更新物粗检：源码形态查特征串；二进制形态查与当前同平台的可执行魔数。"""
    if not (12000 < len(data) < 256 * 1024 * 1024):
        return False
    if _is_frozen():
        try:
            cur = open(_self_path(), "rb").read(4)
        except OSError:
            cur = b""
        magic = data[:4]
        return magic == cur or magic in (b"\x7fELF", b"\xcf\xfa\xed\xfe")
    return b"async def tunnel" in data


async def _apply_update(app, data):
    """校验（源码=py_compile / 二进制=同平台魔数）→ 原子替换自身 → 排程 1s 后 execv
    原地重启（封装运行=换二进制自身，PID 不变，systemd 无感）。返回 (ok, err)。"""
    me = _self_path()
    tmp = me + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    if _is_frozen():
        os.chmod(tmp, 0o755)
    else:
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "py_compile", tmp,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0:
            os.unlink(tmp)
            return False, "编译失败：%s" % (err or b"?").decode("utf-8", "replace")[:200]
    os.replace(tmp, me)
    print("[relay] 收到新版本（%d bytes），1s 后重启生效" % len(data), flush=True)
    if not app.get("updating"):
        app["updating"] = True
        asyncio.get_event_loop().call_later(1.0, _exec_restart)
    return True, ""


def _exec_restart():
    if _is_frozen():
        os.execv(_self_path(), [_self_path()] + sys.argv[1:])
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _adm_sess(app, exp):
    body = "adm.%d" % exp
    return body + "." + hmac.new(app["key"], body.encode(), hashlib.sha256).hexdigest()


def _adm_sess_ok(app, tok):
    parts = (tok or "").split(".")
    if len(parts) != 3 or parts[0] != "adm":
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    body = "adm.%d" % exp
    want = hmac.new(app["key"], body.encode(), hashlib.sha256).hexdigest()
    return exp > time.time() and hmac.compare_digest(parts[2], want)


async def _adm_auth(request):
    if not ADMIN_TOKEN:
        return False
    ah = request.headers.get("authorization", "")
    if hmac.compare_digest(ah, "Bearer %s" % ADMIN_TOKEN):
        return True
    return _adm_sess_ok(request.app, request.cookies.get("cb_adm") or "")


def _admin_phone(db):
    # 环境变量优先：VPS 运维可用它覆盖/找回被写错的手机号（不用改库）。
    env = _norm_phone(ADMIN_PHONE_ENV)
    if env:
        return env
    row = db.execute("SELECT v FROM meta WHERE k='admin_phone'").fetchone()
    return _norm_phone(row[0] if row else "")


def _admin_creds(db):
    h = db.execute("SELECT v FROM meta WHERE k='admin_hash'").fetchone()
    s = db.execute("SELECT v FROM meta WHERE k='admin_salt'").fetchone()
    return ((h[0] if h else "") or ""), ((s[0] if s else "") or "")


def _admin_pass_ok(db, phone, pw):
    if not phone or phone != _admin_phone(db) or not pw:
        return False
    if ADMIN_PASS_ENV:      # 环境变量优先：同上，运维找回通道
        return hmac.compare_digest(pw, ADMIN_PASS_ENV)
    h, salt = _admin_creds(db)
    if h and salt:
        return hmac.compare_digest(hash_pw(pw, salt), h)
    return False


_ADM_CODE_OK = {"code": "", "exp": 0.0}    # 最近一次验证通过的管理码（复用窗口）
_ADM_OK_UNTIL = 0.0                        # 免验证截止时刻（一小时通行证）


def _adm_stepup_on():
    """验过手机就给一小时通行证。管理员只有一个（库里就一个手机号），通行证全站共享：
    浏览器、手机壳、Bearer 令牌换着调也都是同一个人，别再各要一遍短信。"""
    global _ADM_OK_UNTIL
    _ADM_OK_UNTIL = time.time() + ADM_STEPUP_TTL


async def _adm_sensitive(request, hard=False):
    """敏感操作：一小时内验证过手机就直接放行，不用再带码。

    hard=True 是删除/移除类动作——不吃通行证，每次都要当次验证码（同一个码在
    ADM_CODE_REUSE 秒内仍可复用：阿里云同号短信有 60 秒频控，连着删几台设备
    不可能每台都等一条新短信）。
    """
    global _ADM_OK_UNTIL
    code = (request.headers.get("x-adm-code") or "").strip()
    cached = bool(code) and code == _ADM_CODE_OK["code"] \
        and time.time() < _ADM_CODE_OK["exp"]
    if not cached:
        if not hard and time.time() < _ADM_OK_UNTIL:
            return True
        if not code:
            return False
        db = _db(request.app)
        phone = _admin_phone(db)
        if not _PHONE_RE.match(phone):
            return False
        if not await _phone_verify(db, phone, code, "adminop"):
            return False
        _ADM_CODE_OK["code"] = code
        _ADM_CODE_OK["exp"] = time.time() + ADM_CODE_REUSE
    _adm_stepup_on()
    return True


def _adm_need_sms(db):
    return web.json_response({"ok": False, "need_sms": True,
                              "phone": _mask_phone(_admin_phone(db)),
                              "error": "该操作需要手机二次验证"}, status=403)


def _adm_login_resp(app, request, data):
    exp = int(time.time() + ADM_TTL)
    tok = _adm_sess(app, exp)
    resp = web.json_response(data)
    resp.set_cookie("cb_adm", tok, max_age=ADM_TTL, httponly=True,
                    samesite="Lax", secure=(request.scheme == "https"))
    if data.get("by") == "sms":
        # 短信码登录本身就是一次手机验证，同样给一小时通行证
        _adm_stepup_on()
    return resp


def _adm_touch(request, resp):
    """滑动续期：cookie 会话剩不到一半就重签一张新的。

    否则 12 小时一到（手机壳每次回前台都重新加载页面）就得再登录一遍。
    页面打开时都会打 /r/admin_info，所以挂在它上面就够。"""
    tok = request.cookies.get("cb_adm") or ""
    if not _adm_sess_ok(request.app, tok):
        return resp
    try:
        exp = int(tok.split(".")[1])
    except (IndexError, ValueError):
        return resp
    if exp - time.time() < ADM_TTL / 2:
        resp.set_cookie("cb_adm", _adm_sess(request.app, int(time.time() + ADM_TTL)),
                        max_age=ADM_TTL, httponly=True, samesite="Lax",
                        secure=(request.scheme == "https"))
    return resp


async def r_admin_login(request):
    """后台登录：手机号+密码 / 手机号+短信验证码（管理令牌走旧的 sessionStorage 方式）。"""
    if not ADMIN_TOKEN:
        return web.json_response({"ok": False, "error": "管理后台未启用"}, status=403)
    try:
        d = await request.json()
    except Exception:
        d = {}
    ip = request.remote or "?"
    if not rate_ok(ip, "adm_login", 8):
        return web.json_response({"ok": False, "error": "尝试太频繁，稍后再试"}, status=429)
    db = _db(request.app)
    phone = _admin_phone(db)
    if not _PHONE_RE.match(phone):
        return web.json_response({"ok": False, "error": "中继还未配置管理员手机号"}, status=400)
    mode = str(d.get("mode") or "pass")
    if _norm_phone(d.get("phone")) != phone and mode in ("pass", "sms"):
        return web.json_response({"ok": False, "error": "手机号或凭证不正确"}, status=403)
    if mode == "pass":
        if not _admin_pass_ok(db, phone, str(d.get("password") or "")):
            return web.json_response({"ok": False, "error": "手机号或密码不正确"}, status=403)
    elif mode == "sms":
        if not await _phone_verify(db, phone, str(d.get("code") or ""), "adminlogin"):
            return web.json_response({"ok": False, "error": "验证码不正确或已过期"}, status=403)
    else:
        return web.json_response({"ok": False, "error": "未知登录方式"})
    print("[admin] 后台登录 %s via %s ip=%s" % (_mask_phone(phone), mode, ip), flush=True)
    return _adm_login_resp(request.app, request, {"ok": True, "by": mode,
                                                    "phone": _mask_phone(phone)})


async def r_admin_logout(request):
    global _ADM_OK_UNTIL
    _ADM_OK_UNTIL = 0.0
    resp = web.json_response({"ok": True})
    resp.del_cookie("cb_adm")
    return resp


async def r_admin_sms(request):
    """管理员验证码：adminlogin（登录）/ adminop（敏感操作二次验证）。只发给配置的管理员手机号。"""
    try:
        d = await request.json()
    except Exception:
        d = {}
    purpose = str(d.get("purpose") or "adminlogin")
    if purpose not in ("adminlogin", "adminop"):
        return web.json_response({"ok": False, "error": "未知用途"})
    if purpose == "adminop" and not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    phone = _admin_phone(db)
    if not _PHONE_RE.match(phone):
        return web.json_response({"ok": False, "error": "中继还未配置管理员手机号"}, status=400)
    if not SMS_ON:
        return web.json_response(
            {"ok": False, "error": "中继没配短信通道（RELAY_ALI_AK_ID/AK_SECRET/SIGN_NAME/TEMPLATE_CODE）"},
            status=502)
    ip = request.remote or "?"
    # 限额只管「防炸」：管理员已登录、手机号也是固定的自己人。以前 8 条/小时太紧，
    # 反复调试敏感操作（每次弹窗都发一条）一小时就打满，之后全是 429。
    if not rate_ok(ip, "adm_sms", 30) or not rate_ok(phone, "adm_smsto", 30):
        wait = max(rate_wait(phone, "adm_smsto", 30), rate_wait(ip, "adm_sms", 30))
        return web.json_response(
            {"ok": False, "error": "验证码请求过于频繁，%d 分钟后再试" % max(1, wait // 60)},
            status=429)
    masked = _mask_phone(phone)
    db.execute("DELETE FROM phone_codes WHERE expires<?", (time.time() - 3600,))
    last = db.execute("SELECT created FROM phone_codes WHERE phone=? ORDER BY id DESC LIMIT 1",
                      (phone,)).fetchone()
    if last and time.time() - last[0] < SMS_COOLDOWN:
        return web.json_response({"ok": False,
                                  "error": "发送太频繁，请 %d 秒后再试" % SMS_COOLDOWN})
    out_id = ("%06d" % secrets.randbelow(1000000)) if SMS_CONSOLE else secrets.token_hex(8)
    try:
        await asyncio.to_thread(_sms_send_sync, phone, out_id, ALI_TEMPLATE)
    except Exception as e:
        print("[relay] 管理员短信发送失败 %s: %s" % (masked, str(e)[:200]), flush=True)
        return web.json_response({"ok": False, "error": "短信发送失败：%s" % str(e)[:140]},
                                 status=502)
    now = time.time()
    db.execute("INSERT INTO phone_codes(phone,out_id,purpose,created,expires) VALUES(?,?,?,?,?)",
               (phone, out_id, purpose, now, now + SMS_TTL))
    db.commit()
    print("[relay] 管理员验证码已发 %s purpose=%s ip=%s" % (masked, purpose, ip), flush=True)
    return web.json_response({"ok": True, "sent": True, "phone": masked,
                              "msg": "验证码已发送至 " + masked})


async def r_admin_security(request):
    """管理员手机号 / 登录密码。GET 读状态；POST 改（已配置手机号时需手机二次验证）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    phone = _admin_phone(db)
    if request.method == "GET":
        h, _ = _admin_creds(db)
        return web.json_response({"ok": True, "phone": _mask_phone(phone),
                                  "configured": bool(phone),
                                  "pass_set": bool(h) or bool(ADMIN_PASS_ENV),
                                  "sms": SMS_ON})
    if phone and not await _adm_sensitive(request):
        return _adm_need_sms(db)
    try:
        d = await request.json()
    except Exception:
        d = {}
    newp = _norm_phone(d.get("phone") or "")
    pw = str(d.get("password") or "")
    if newp and not _PHONE_RE.match(newp):
        return web.json_response({"ok": False, "error": "手机号格式不正确"})
    if pw and len(pw) < 6:
        return web.json_response({"ok": False, "error": "密码至少 6 位"})
    if not newp and not pw:
        return web.json_response({"ok": False, "error": "没有改动"})
    if newp:
        db.execute("INSERT INTO meta(k,v) VALUES('admin_phone',?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (newp,))
    if pw:
        salt = secrets.token_hex(8)
        db.execute("INSERT INTO meta(k,v) VALUES('admin_salt',?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (salt,))
        db.execute("INSERT INTO meta(k,v) VALUES('admin_hash',?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (hash_pw(pw, salt),))
    db.commit()
    if newp and not _PHONE_RE.match(phone):
        # 首次配置：清掉旧会话之外没有副作用；提示用新手机号登录
        pass
    print("[admin] 管理员安全配置已更新 phone=%s pass=%s"
          % (_mask_phone(newp or phone), "yes" if pw else "no"), flush=True)
    return web.json_response({"ok": True, "phone": _mask_phone(newp or phone),
                              "msg": "已保存"})


async def r_admin_info(request):
    """管理后台状态：relay.py 大小/修改时间、自动更新源是否配置。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权访问"}, status=401)
    st = os.stat(_self_path())
    src = "release" if GH_REPO else ("url" if UPDATE_URL else "")
    return _adm_touch(request, web.json_response({"ok": True, "bytes": st.st_size,
                              "mtime": int(st.st_mtime), "ver": RELAY_VER,
                              "source": src, "repo": GH_REPO,
                              "smtp": {"on": bool(SMTP_HOST), "host": SMTP_HOST,
                                       "port": SMTP_PORT, "user": SMTP_USER,
                                       "from": SMTP_FROM,
                                       "pass": "已配置" if SMTP_PASS else "未配置"},
                              "sms": {"on": SMS_ON, "sign": ALI_SIGN_NAME,
                                      "template": ALI_TEMPLATE,
                                      "ak": "已配置" if ALI_AK_ID else "未配置"}}))


# ---------- 管理后台 API（Bearer RELAY_ADMIN_TOKEN） ----------
def _day0(ts=None):
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _month0(ts=None):
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def _sum_usage(db, pat):
    row = db.execute("SELECT COALESCE(SUM(n),0) FROM usage WHERE k LIKE ?", (pat,)).fetchone()
    return int(row[0] if row else 0)


def _mask_secret(v):
    v = str(v or "")
    if not v:
        return ""
    return "***" + v[-4:] if len(v) > 4 else "***"


def _adm_unauth():
    return web.json_response({"ok": False, "error": "无权访问"}, status=401)


async def r_admin_net(request):
    """出口带宽：当前瞬时速率 + 近 24 小时曲线（5 分钟一个点）+ 近 7 天日出/入流量。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    now = time.time()
    cutoff = now - 24 * 3600
    series = []
    for b in _NET_HIST:
        if b[0] < cutoff - _NET_BUCKET:
            continue
        dt = b[3] if b[3] > 0 else 1.0
        series.append([int(b[0]), round(b[1] / dt, 1), round(b[2] / dt, 1)])
    peak = max(series, key=lambda x: x[1]) if series else None
    peak_dn = max(series, key=lambda x: x[2]) if series else None
    today = _NET_DAILY.get(time.strftime("%Y-%m-%d"), [0, 0])
    cap_kbs = round(_MON_NET_MBPS * 1024 / 8, 1) if _MON_NET_MBPS > 0 else 0
    return web.json_response({
        "ok": True, "now": now,
        "cur": {"ts": round(_NET_CUR["ts"], 1),
                "up": round(_NET_CUR["up"], 1), "dn": round(_NET_CUR["dn"], 1)},
        "max_mbps": _MON_NET_MBPS, "alert_kbs": round(cap_kbs * 0.8, 1), "cap_kbs": cap_kbs,
        "sample_sec": _NET_EVERY, "bucket_sec": _NET_BUCKET,
        "series": series,
        "peak": {"up": peak[1], "at": peak[0]} if peak else None,
        "peak_down": {"dn": peak_dn[2], "at": peak_dn[0]} if peak_dn else None,
        "today": {"up_mb": round(today[0] / 1048576.0, 1),
                  "down_mb": round(today[1] / 1048576.0, 1)},
        "days": [{"date": k, "up_mb": round(v[0] / 1048576.0, 1),
                  "down_mb": round(v[1] / 1048576.0, 1)}
                 for k, v in sorted(_NET_DAILY.items())[-7:]],
    })


async def r_admin_direct(request):
    """直连命中率 + 隧道流量（近 14 天）。

    命中率：usage_client 里「纯直连分钟」（k=direct，开发机补报）占「有使用的分钟」
    的比例——直连不经过中继，所以它越高，中继带宽压力越小。
    隧道流量：tunnel_stat 按天累计的请求数/上下行字节（回包是压缩前的原始字节）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    since_m = int(time.time() // 60) - 14 * 1440
    by = {}
    for m, k in db.execute("SELECT m,k FROM usage_client WHERE m>=?", (since_m,)):
        day = time.strftime("%Y-%m-%d", time.localtime(int(m) * 60))
        e = by.setdefault(day, [set(), set()])
        if k == "direct":
            e[0].add(m)
        elif str(k).startswith("c:"):
            e[1].add(m)
    days = []
    for day in sorted(by.keys()):
        dset, rset = by[day]
        union = dset | rset
        days.append({"date": day, "direct_min": len(dset), "relay_min": len(rset),
                     "all_min": len(union),
                     "rate": round(100.0 * len(dset) / len(union), 1) if union else 0.0})
    day0 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
    tmap = {}
    try:
        for day, n, bi, bo in db.execute(
                "SELECT day,req_n,bytes_in,bytes_out FROM tunnel_stat WHERE day>=?", (day0,)):
            tmap[day] = (int(n or 0), int(bi or 0), int(bo or 0))
    except Exception:
        pass
    for d in days:
        n, bi, bo = tmap.get(d["date"], (0, 0, 0))
        d.update(req_n=n, bytes_in=bi, bytes_out=bo)
    # 只有流量没有使用时长的日子也列出来（半夜推送/自更新也会走隧道）
    for day in sorted(tmap.keys()):
        if day not in by:
            n, bi, bo = tmap[day]
            days.append({"date": day, "direct_min": 0, "relay_min": 0, "all_min": 0,
                         "rate": 0.0, "req_n": n, "bytes_in": bi, "bytes_out": bo})
    days.sort(key=lambda x: x["date"])
    today = time.strftime("%Y-%m-%d")
    td = next((d for d in days if d["date"] == today),
              {"date": today, "direct_min": 0, "relay_min": 0, "all_min": 0,
               "rate": 0.0, "req_n": 0, "bytes_in": 0, "bytes_out": 0})
    return web.json_response({
        "ok": True, "now": time.time(), "today": td,
        "totals": {"direct_min": sum(d["direct_min"] for d in days),
                   "all_min": sum(d["all_min"] for d in days),
                   "req_n": sum(d.get("req_n", 0) for d in days),
                   "bytes_in": sum(d.get("bytes_in", 0) for d in days),
                   "bytes_out": sum(d.get("bytes_out", 0) for d in days)},
        "days": days,
    })


async def r_admin_services(request):
    """服务监控：短信/邮件/提醒推送/语音转写/语音合成/文本模型 + 机器与在线概览。
    监控屏（/r/admin/board）也读这一个接口，10 秒一轮。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    app = request.app
    now = time.time()
    q = request.query
    win = (q.get("window") or "").strip().lower()
    gran = (q.get("gran") or "").strip().lower()
    try:
        span = int(q.get("span") or 0)
    except (TypeError, ValueError):
        span = 0
    if not span:      # 老参数 window 兼容（后台页/旧链接）
        gran, span = {"hour": ("hour", 86400), "day": ("day", 7 * 86400),
                      "week": ("day", 56 * 86400), "month": ("day", 180 * 86400)}.get(
                          win, ("hour", 86400))
    if gran not in _SVC_GRANS:
        gran = "hour"
    span = min(max(span, 60), 400 * 86400)
    try:
        f_uid = int(q.get("uid") or 0)
    except (TypeError, ValueError):
        f_uid = 0
    f_dev = (q.get("dev") or "").strip()[:64]
    f_cid = (q.get("cid") or "").strip()[:64]
    base, nb = _svc_bucket(gran, span)
    svcs = _svc_view(app, gran, span, f_uid, f_dev, f_cid)
    try:
        sysst = _sys_sample(app["dbp"])
    except Exception:
        sysst = {}
    try:
        online = len(app.get("links") or {})
        clients = int(sum((app.get("presence_n") or {}).values()))
    except Exception:
        online = clients = 0
    bal = dict(_BAL_LAST)
    return web.json_response({
        "ok": True, "now": now, "ver": RELAY_VER or "",
        "uptime": max(0, int(now - float(app.get("started") or now))),
        "gran": gran, "span": span, "bucket_sec": base,
        "bucket_text": _bucket_text(base), "span_text": _span_text(span),
        "filter": {"uid": f_uid, "dev": f_dev, "cid": f_cid},
        "services": svcs,
        "direct": _direct_view(_db(app), span, f_uid, f_dev, f_cid),
        "traffic": _traffic_view(_db(app), span, f_uid, f_dev, f_cid),
        "summary": {
            "total": len(svcs),
            "ok": sum(1 for s in svcs if s["state"] == "ok"),
            "warn": sum(1 for s in svcs if s["state"] == "warn"),
            "fail": sum(1 for s in svcs if s["state"] == "fail"),
            "unknown": sum(1 for s in svcs if s["state"] == "unknown"),
            "off": sum(1 for s in svcs if s["state"] == "off"),
            "bad": [s["name"] for s in svcs if s["state"] in ("fail", "warn")],
        },
        "sys": {"disk_pct": round(float(sysst.get("disk_pct") or 0), 1),
                "disk_free_mb": int(sysst.get("disk_free_mb") or 0),
                "mem_avail_mb": int(sysst.get("mem_avail_mb") or 0),
                "load": round(float(sysst.get("load") or 0), 2),
                "db_mb": round(float(sysst.get("db_mb") or 0), 1)},
        "online": online, "clients": clients,
        "balance": {"amt": bal.get("amt"), "cur": bal.get("cur") or "",
                    "ts": bal.get("ts") or 0, "err": bal.get("err") or "",
                    "min": _BAL_MIN},
    })


async def r_admin_overview(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    now = time.time()
    d0, m0 = _day0(), _month0()
    today = time.strftime("%Y-%m-%d")

    def one(sql, args=()):
        row = db.execute(sql, args).fetchone()
        return row[0] if row else 0

    users_total = one("SELECT COUNT(*) FROM users")
    users_paid = one("SELECT COUNT(*) FROM users WHERE plan IN ('pro','max') "
                     "AND COALESCE(plan_expires,0)>?", (now,))
    users_new = one("SELECT COUNT(*) FROM users WHERE created>=?", (d0,))
    dev_total = one("SELECT COUNT(*) FROM devices")
    dev_bound = one("SELECT COUNT(*) FROM devices WHERE owner IS NOT NULL")
    dev_frozen = one("SELECT COUNT(*) FROM devices WHERE frozen=1")
    pay_today = db.execute("SELECT COUNT(*),COALESCE(SUM(CAST(money AS REAL)),0) FROM orders "
                           "WHERE status=1 AND paid>=?", (d0,)).fetchone()
    pay_month = db.execute("SELECT COUNT(*),COALESCE(SUM(CAST(money AS REAL)),0) FROM orders "
                           "WHERE status=1 AND paid>=?", (m0,)).fetchone()
    pay_all = db.execute("SELECT COUNT(*),COALESCE(SUM(CAST(money AS REAL)),0) FROM orders "
                         "WHERE status=1").fetchone()
    pending = one("SELECT COUNT(*) FROM orders WHERE status=0")
    recent = db.execute(
        "SELECT o.out_trade_no,o.sku,o.money,o.status,o.created,o.paid,COALESCE(u.name,'(已删)') "
        "FROM orders o LEFT JOIN users u ON u.id=o.uid ORDER BY o.created DESC LIMIT 8").fetchall()
    try:
        svcs = _svc_view(request.app)
    except Exception as e:      # 概览不能被监控模块拖垮（以前 plans 就整页 500 过）
        print("[relay] overview svc view failed: %s" % str(e)[:160], flush=True)
        svcs = []
    return web.json_response({
        "ok": True, "now": now,
        "users": {"total": users_total, "paid": users_paid, "new_today": users_new},
        "devices": {"total": dev_total, "bound": dev_bound, "frozen": dev_frozen,
                    "online": len(request.app.get("links") or {}),
                    "clients": sum((request.app.get("presence_n") or {}).values())},
        "pay": {"today_n": pay_today[0], "today_sum": round(pay_today[1], 2),
                "month_n": pay_month[0], "month_sum": round(pay_month[1], 2),
                "all_n": pay_all[0], "all_sum": round(pay_all[1], 2),
                "pending": pending},
        # 时长按秒存（刻度可配），后台页面按秒显示；use_min 是给老页面的兼容字段。
        "usage": {"use_sec": _sum_usage(db, "use:u%%:d:%s" % today),
                  "use_min": _sum_usage(db, "use:u%%:d:%s" % today) // 60,
                  "asr_sec": _sum_usage(db, "asr:%%:d:%s" % today),
                  "llm": _sum_usage(db, "llm:u%%:d:%s" % today),
                  "credits_sec": one("SELECT COALESCE(SUM(sec),0) FROM credits"),
                  "use_credit_sec": one("SELECT COALESCE(SUM(sec),0) FROM use_credits"),
                  "use_credit_min": one("SELECT COALESCE(SUM(sec),0) FROM use_credits") // 60},
        "recent": [{"otn": r[0], "sku": r[1],
                    "sku_name": (pay_skus().get(r[1]) or (r[1],))[0],
                    "money": r[2], "status": r[3],
                    "created": r[4], "paid": r[5], "user": r[6]} for r in recent],
        # plans() 里除了档位还有 skus/packs（packs 是 list），只取档位那种 dict，
        # 否则概览整页 500（后台「概览」一片空白的直接原因）。
        "plans": {k: {"name": v.get("name") or k}
                  for k, v in (plans() or {}).items()
                  if isinstance(v, dict) and k not in ("skus", "packs")},
        # 概览页也要一眼看到这两项：计时单位时长、外部服务有没有挂
        "meter": meter_unit(db),
        "svc": {"fail": sum(1 for s in svcs if s["state"] == "fail"),
                "warn": sum(1 for s in svcs if s["state"] == "warn"),
                "bad": [s["name"] for s in svcs
                        if s["state"] in ("fail", "warn")][:4]},
    })


async def r_admin_users(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    q = (request.query.get("q") or "").strip()
    try:
        limit = min(500, max(1, int(request.query.get("limit") or 200)))
    except ValueError:
        limit = 200
    where, args = "1=1", []
    if q:
        like = "%" + q + "%"
        where = "(u.name LIKE ? OR u.email LIKE ? OR u.phone LIKE ? OR CAST(u.id AS TEXT)=?)"
        args = [like, like, like, q]
    pf = branding.valid_product(request.query.get("product") or "")
    if request.query.get("product"):        # 显式给了才过滤（合法值已回落默认）
        where += " AND u.product=?"
        args.append(pf)
    today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    rows = db.execute(
        "SELECT u.id,u.name,u.email,u.phone,u.plan,u.plan_expires,u.created,u.sess_epoch,"
        "(SELECT COUNT(*) FROM devices d WHERE d.owner=u.id),"
        "(SELECT MAX(d.last_seen) FROM devices d WHERE d.owner=u.id),u.product,u.products "
        "FROM users u WHERE " + where + " ORDER BY u.created DESC LIMIT ?",
        args + [limit]).fetchall()
    out = []
    now = time.time()
    for r in rows:
        uid = r[0]
        effective = "free"
        if _is_paid_plan(r[4] or "") and float(r[5] or 0) > now:
            effective = r[4]
        lim = plan_limits(db, uid)
        out.append({
            "id": uid, "name": r[1], "email": r[2] or "", "phone": r[3] or "",
            "plan": r[4] or "", "plan_expires": float(r[5] or 0),
            "effective_plan": effective, "created": r[6] or 0,
            "sess_epoch": r[7] or 0, "devices": r[8] or 0,
            "last_seen": float(r[9] or 0),
            "product": r[10] or branding.PRODUCT_DEFAULT,
            # 端权限覆盖（NULL=按套餐，空串=两线都关，CSV=逐账号指定），后台卡片用
            "products": r[11],
            "credits_sec": credits_of(db, uid),
            "use_credit_sec": use_credit_of(db, uid),
            "use_credit_min": use_credit_of(db, uid) // 60,
            "use_cap": int(lim.get("use_min_day") or 0),
            "asr_day_cap": int(lim.get("asr_sec_day") or 0),
            "asr_month_cap": int(lim.get("asr_sec_month") or 0),
            "llm_cap": int(lim.get("llm_day") or 0),
            "hosts_cap": int(lim.get("hosts") or 0),
            # 客户端数：按 cb_cid 统计最近 24 小时来过的去重个数（见 clients_used）
            "clients": clients_used(db, uid),
            "clients_cap": int(lim.get("clients") or 0),
            "quota_ov": plan_override(db, uid),
            "use_min_today": usage_get(db, "use:u%d:d:%s" % (uid, today)) // 60,
            # 时长按秒存（刻度可配）；后台用这一组显示，分钟那两个字段只留给老页面
            "use_sec_today": usage_get(db, "use:u%d:d:%s" % (uid, today)),
            "use_cap_sec": use_cap(db, uid),
            "asr_sec_today": usage_get(db, "asr:u%d:d:%s" % (uid, today)),
            "asr_sec_month": usage_get(db, "asr:u%d:m:%s" % (uid, month)),
            "llm_today": usage_get(db, "llm:u%d:d:%s" % (uid, today)),
        })
    return web.json_response({
        "ok": True, "users": out,
        # 后台分配套餐用的档位表：含内部档（sell:false），前台永远看不到这些
        "plan_opts": [{"key": k, "name": (v.get("name") or k),
                       "sell": v.get("sell") is not False}
                      for k, v in (plans() or {}).items()
                      if isinstance(v, dict) and k not in ("skus", "packs")],
    })


def _adm_audit(db, request, action, target_uid, before=None, after=None):
    """后台人工操作的审计落库（谁、何时、改前改后、来源 IP）。

    手工改套餐最容易写出「多送一年」这种账，出了问题必须能倒查是哪一次、改了什么。"""
    try:
        db.execute("INSERT INTO admin_audit(ts,action,target_uid,before,after,ip) "
                   "VALUES(?,?,?,?,?,?)",
                   (time.time(), str(action)[:40], int(target_uid or 0),
                    json.dumps(before or {}, ensure_ascii=False)[:800],
                    json.dumps(after or {}, ensure_ascii=False)[:800],
                    str(request.remote or "?")[:64]))
    except Exception as e:          # 审计写不进去不能挡住正常操作，但要在日志里喊一声
        print("[admin] 审计写入失败 action=%s uid=%s: %s"
              % (action, target_uid, str(e)[:120]), flush=True)


async def r_admin_user(request):
    """用户管理：改套餐/调使用时长包/重置密码/强制下线/删除账号。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        d = await request.json()
    except Exception:
        d = {}
    # 删除/移除类每次都要手机码，不吃一小时通行证
    if not await _adm_sensitive(request, hard=str(d.get("action") or "") in
                                ("delete", "del_client", "clear_clients")):
        return _adm_need_sms(db)
    try:
        uid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    row = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "用户不存在"}, status=404)
    name = row[0]
    act = str(d.get("action") or "")
    now = time.time()
    if act == "set_plan":
        plan = str(d.get("plan") or "free")
        _old = db.execute("SELECT plan,plan_expires,plan_sku,plan_next,plan_next_days,"
                          "COALESCE(plan_next_n,0) FROM users WHERE id=?",
                          (uid,)).fetchone()
        before = {"plan": _old[0], "expires": _old[1], "sku": _old[2],
                  "next": _old[3], "next_days": _old[4],
                  "next_n": _old[5]} if _old else {}
        # 允许 plans.json 里的任意档位（含内部档 sell:false）——后台人工分配就是要它
        known = [k for k, v in (plans() or {}).items()
                 if isinstance(v, dict) and k not in ("skus", "packs")]
        if plan not in known:
            return web.json_response({"ok": False,
                                      "error": "档位只能是：" + "/".join(known)})
        if plan == "free":
            # 后台手动改档：连排队中的换档一起清掉，别让旧预约悄悄生效
            db.execute("UPDATE users SET plan='free',plan_expires=0,plan_sku='',"
                       "plan_next=NULL,plan_next_days=0,plan_next_n=0 WHERE id=?", (uid,))
            msg = "已改为免费版"
        else:
            if d.get("expires"):
                try:
                    exp = float(d["expires"])
                except (TypeError, ValueError):
                    return web.json_response({"ok": False, "error": "到期时间格式不对"})
            else:
                try:
                    days = int(d.get("days") or 30)
                except (TypeError, ValueError):
                    days = 30
                cur = db.execute("SELECT plan_expires FROM users WHERE id=?", (uid,)).fetchone()
                base = max(now, float((cur[0] if cur else 0) or 0))
                exp = base + max(1, days) * 86400
            db.execute("UPDATE users SET plan=?,plan_expires=?,plan_sku='',"
                       "plan_next=NULL,plan_next_days=0,plan_next_n=0 WHERE id=?",
                       (plan, exp, uid))
            msg = "已改为 %s，有效期到 %s" % (
                ((plans().get(plan) or {}).get("name") or plan),
                time.strftime("%Y-%m-%d", time.localtime(exp)))
        _new = db.execute("SELECT plan,plan_expires,plan_sku,plan_next,plan_next_days,"
                          "COALESCE(plan_next_n,0) FROM users WHERE id=?",
                          (uid,)).fetchone()
        _adm_audit(db, request, "set_plan", uid, before,
                   {"plan": _new[0] if _new else "", "expires": _new[1] if _new else 0,
                    "sku": _new[2] if _new else "", "next": _new[3] if _new else "",
                    "next_days": _new[4] if _new else 0,
                    "next_n": _new[5] if _new else 0,
                    "req": {"plan": plan, "days": d.get("days"),
                            "expires": d.get("expires")}})
        db.commit()
        asyncio.create_task(_push_use_ack_uid(request.app, uid))
    elif act == "set_products":
        # 端权限逐账号覆盖：products=None → 按套餐；[]/"" → 一个端都不给。
        raw = d.get("products")
        val = _parse_products(raw)
        before = db.execute("SELECT products FROM users WHERE id=?", (uid,)).fetchone()
        db.execute("UPDATE users SET products=? WHERE id=?", (val, uid))
        _adm_audit(db, request, "set_products", uid,
                   {"products": before[0] if before else None},
                   {"products": val, "req": raw})
        db.commit()
        allowed, src = _allowed_products(db, uid)
        msg = ("已恢复按套餐（%s）" % "、".join(branding.product_name(x)["label"] for x in allowed)
               if val is None else
               ("已关闭该账号全部端" if not val else
                "已设为：" + "、".join(branding.product_name(x)["label"]
                                      for x in val.split(","))))
        return web.json_response({"ok": True, "msg": msg, "products": val,
                                  "allowed": allowed, "source": src})
    elif act in ("use_credit", "credits"):
        try:
            mins = int(d.get("min") if d.get("min") is not None else d.get("sec") or 0)
        except (TypeError, ValueError):
            mins = 0
        if not mins:
            return web.json_response({"ok": False, "error": "分钟数不能为 0"})
        use_credit_add(db, uid, mins, "admin",
                       "后台手动调整 ip=%s" % (request.remote or "?"))
        msg = "使用时长包已 %s %d 分钟（余 %d 分钟）" % (
            "增加" if mins > 0 else "扣减", abs(mins), use_credit_of(db, uid))
        asyncio.create_task(_push_use_ack_uid(request.app, uid))
    elif act == "reset_pass":
        pw = d.get("password") or secrets.token_urlsafe(9)
        if len(str(pw)) < 6:
            return web.json_response({"ok": False, "error": "密码至少 6 位"})
        salt = secrets.token_hex(8)
        db.execute("UPDATE users SET pass_hash=?,salt=?,sess_epoch=sess_epoch+1 WHERE id=?",
                   (hash_pw(str(pw), salt), salt, uid))
        db.commit()
        print("[admin] 重置密码 uid=%d" % uid, flush=True)
        return web.json_response({"ok": True, "password": str(pw),
                                  "msg": "已重置密码，所有登录态失效"})
    elif act == "logout":
        db.execute("UPDATE users SET sess_epoch=sess_epoch+1 WHERE id=?", (uid,))
        db.commit()
        msg = "已强制下线（旧登录态失效）"
    elif act == "clear_clients":
        # 换手机/重装后 cb_cid 会变，旧记录还占着名额（不在线 24 小时后自动释放）；
        # 这里给客服一个立即「清空计数」的口子
        db.execute("DELETE FROM clients WHERE uid=?", (uid,))
        db.commit()
        for k in [k for k in _CLI_GATE if k[0] == uid]:
            _CLI_GATE.pop(k, None)
        msg = "已清空客户端计数（谁来过重新开始算）"
    elif act == "del_client":
        # 只删一个客户端：用户手机上装过好几个 App/换过设备，cb_cid 各不相同，
        # 老的占着名额清不掉（上限 5 个、实际来了 8 个）。
        cid = str(d.get("cid") or "").strip()[:64]
        if not cid:
            return web.json_response({"ok": False, "error": "缺 cid"})
        _delete_client(db, uid, cid)
        _CLI_GATE.pop((int(uid), cid), None)
        msg = "已移除该客户端（%s），名额已释放" % cid
    elif act == "delete":
        if str(d.get("confirm") or "") != name:
            return web.json_response({"ok": False, "error": "删除需输入用户名确认"})
        devs = [r[0] for r in db.execute(
            "SELECT id FROM devices WHERE owner=?", (uid,)).fetchall()]
        # 名下开发机回到匿名免费档（anon=1）：只清 owner 的话 host 重连拿 409，
        # 那台机器就永远连不上、也没人能用（删账号 = 设备解绑，不是设备报废）。
        db.execute("UPDATE devices SET owner=NULL,anon=1,frozen=0 WHERE owner=?", (uid,))
        db.execute("DELETE FROM clients WHERE uid=?", (uid,))
        db.execute("DELETE FROM credits WHERE uid=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
        for dev in devs:          # 立刻断链，让 host 按匿名免费档重连（别等巡检）
            ws = (request.app.get("links") or {}).pop(dev, None)
            (request.app.get("direct") or {}).pop(dev, None)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
        print("[admin] 删除账号 uid=%d name=%s" % (uid, name), flush=True)
        return web.json_response({"ok": True, "msg": "账号已删除，开发机已回到免费档（订单保留）"})
    elif act == "set_quota":
        # 按账号手动覆盖额度（2026-11-18）：limits 里 7 个键全量提交，
        # 值为空/None = 该项继承套餐档位；全部为空 = 清掉覆盖。0 = 不限。
        ov = d.get("limits")
        if not isinstance(ov, dict):
            return web.json_response({"ok": False, "error": "limits 要是对象"})
        allow = ("sends_day", "asr_sec_day", "asr_sec_month", "llm_day",
                 "hosts", "clients", "use_min_day")
        before = plan_override(db, uid)
        clean, bad = {}, []
        for k, v in ov.items():
            if k not in allow or v is None or v == "":
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                bad.append(k)
                continue
            if v < 0:
                return web.json_response({"ok": False, "error": k + " 不能为负"})
            clean[k] = v
        if bad:
            return web.json_response({"ok": False, "error": "这些项要是整数：" + "、".join(bad)})
        _plan_override_save(db, uid, clean)
        msg = ("额度覆盖已保存（%d 项，其余按套餐）" % len(clean)) if clean \
            else "额度覆盖已清除，恢复套餐默认"
        _adm_audit(db, request, "set_quota", uid, before, clean)
        db.commit()
        asyncio.create_task(_push_use_ack_uid(request.app, uid))
    else:
        return web.json_response({"ok": False, "error": "未知操作"})
    print("[admin] 用户 %s（uid %d）：%s" % (name, uid, msg), flush=True)
    return web.json_response({"ok": True, "msg": msg})


async def r_admin_devices(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    q = (request.query.get("q") or "").strip()
    where, args = "1=1", []
    if q:
        like = "%" + q + "%"
        where = "(d.id LIKE ? OR d.name LIKE ? OR u.name LIKE ?)"
        args = [like, like, like]
    if request.query.get("product"):        # 产品线筛选（合法值经 valid_product 回落）
        where += " AND d.product=?"
        args.append(branding.valid_product(request.query.get("product")))
    rows = db.execute(
        "SELECT d.id,d.name,d.owner,d.created,d.last_seen,d.frozen,u.name,d.ver,d.logs_on,"
        "d.logs_until,d.product "
        "FROM devices d LEFT JOIN users u ON u.id=d.owner WHERE " + where +
        " ORDER BY d.last_seen DESC LIMIT 500", args).fetchall()
    out = []
    now = time.time()
    links = request.app.get("links") or {}
    direct = request.app.get("direct") or {}
    # 盘上日志快照的最新时间：中继重启后内存里的 at 没了，按钮还要在
    disk_at = {}
    try:
        for nm in os.listdir(DEVLOG_DIR):
            if "-" not in nm:
                continue
            did = nm.rsplit("-", 1)[0]
            try:
                mt = os.path.getmtime(os.path.join(DEVLOG_DIR, nm))
            except OSError:
                continue
            if mt > disk_at.get(did, 0):
                disk_at[did] = mt
    except OSError:
        pass
    for r in rows:
        dr = direct.get(r[0]) or {}
        out.append({
            "id": r[0], "name": r[1] or "", "owner": r[2], "owner_name": r[6] or "",
            "created": r[3] or 0, "last_seen": float(r[4] or 0),
            "frozen": bool(r[5]), "online": r[0] in links,
            "logs_on": bool(r[8]),
            "product": r[10] or branding.PRODUCT_DEFAULT,
            # 还剩几分钟自动关（0 = 没设过期，只在老配置下出现）
            "log_left": int(max(0, (r[9] or 0) - now)) if r[8] and r[9] else 0,
            "log_at": (((request.app.get("dev_logs") or {}).get(r[0]) or {}).get("at")
                       or disk_at.get(r[0]) or 0),
            "ver": dr.get("ver") or r[7] or "",
            "lan": dr.get("lan") or [], "rtc": bool(dr.get("rtc")),
            "direct_at": dr.get("ts") or 0,
            "clients": (request.app.get("presence_n") or {}).get(r[0]) or 0,
        })
    return web.json_response({"ok": True, "devices": out, "now": now})


async def r_admin_device_logs(request):
    """后台「看日志」：这台开发机最近一次推上来的 server.log 尾部。

    只读、只给管理员：开发机侧没有任何开关/界面（用户不可见），后台点了「开日志」
    它才开始推，点关就停。内存里只留每个设备最后一份。
    """
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    dev = (request.query.get("id") or "").strip()
    row = db.execute("SELECT name, logs_on FROM devices WHERE id=?", (dev,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "开发机不存在"}, status=404)
    d = (request.app.get("dev_logs") or {}).get(dev) or {}
    if not d.get("txt"):      # 中继重启过（内存丢了）：读盘上留的那份快照
        try:
            fs = sorted(x for x in os.listdir(DEVLOG_DIR) if x.startswith(dev + "-"))
        except OSError:
            fs = []
        if fs:
            p = os.path.join(DEVLOG_DIR, fs[-1])
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read()[-200000:]
                d = {"at": os.path.getmtime(p), "ver": "", "txt": txt}
            except OSError:
                pass
    if request.query.get("dl"):
        # 下载：设备名可能是中文，文件名只留 ASCII，否则响应头非法
        nm = re.sub(r"[^A-Za-z0-9._-]", "", (row[0] or ""))[:24] or "device"
        fn = "codeboss-%s-%s.log" % (nm, dev[:8])
        return web.Response(body=(d.get("txt") or "").encode("utf-8", "replace"),
                            content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": 'attachment; filename="%s"' % fn})
    return web.json_response({"ok": True, "on": bool(row[1]), "at": d.get("at") or 0,
                              "ver": d.get("ver") or "", "txt": d.get("txt") or ""})


async def r_admin_clients(request):
    """终端管理：所有登记过的手机/浏览器客户端（clients 表）。

    一行 = 一台终端（cb_cid）：机型、所属账号、近 30 天用了多少分钟、首次/最后出现。
    最近 24 小时来过才占客户端名额（和计费口径一致）；从这一页可以直接跳到动作明细。
    """
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    now = time.time()
    q = (request.query.get("q") or "").strip()
    state = (request.query.get("state") or "all").strip()
    where, args = "1=1", []
    if q:
        like = "%" + q + "%"
        where += " AND (c.cid LIKE ? OR u.name LIKE ? OR CAST(c.uid AS TEXT) LIKE ?)"
        args += [like, like, like]
    if state == "active":
        where += " AND c.last>=?"
        args.append(now - CLIENT_SEAT_SEC)
    elif state == "stale":
        where += " AND c.last<?"
        args.append(now - CLIENT_SEAT_SEC)
    rows = db.execute(
        "SELECT c.uid,c.cid,c.dev,c.first,c.last,u.name FROM clients c "
        "LEFT JOIN users u ON u.id=c.uid WHERE " + where +
        " AND NOT (c." + _BG_ROW_SQL + ") ORDER BY c.last DESC LIMIT 800",
        args).fetchall()
    # 同一台设备（dev 相同）合成一行：壳丢 cookie 换的 cid 不是新终端
    _grp = {}
    for uid_, cid, dev, first, last, name in rows:
        g = _grp.setdefault((uid_, dev or cid),
                            {"uid": int(uid_), "cid": cid or "", "first": first,
                             "last": last, "user": name or ""})
        if first and (not g["first"] or float(first) < float(g["first"])):
            g["first"] = first
        if last and (not g["last"] or float(last) > float(g["last"])):
            g["last"], g["cid"] = last, cid or ""
    rows = sorted(_grp.values(), key=lambda g: -(g["last"] or 0))
    # 机型/标签与分钟数各扫一遍 usage_client（表只留 15 天，量很小），避免每行子查询
    mcut = int(now // 60) - 30 * 24 * 60
    labs, mins = {}, {}
    try:
        for uid_, k, label, ua, last in db.execute(
                "SELECT uid,k,label,ua,MAX(last) FROM usage_client "
                "WHERE k LIKE 'c:%' OR k LIKE 'i:%' GROUP BY uid,k").fetchall():
            labs[(int(uid_), k)] = (label or "", ua or "", float(last or 0))
        for uid_, k, n in db.execute(
                "SELECT uid,k,COUNT(DISTINCT m) FROM usage_client "
                "WHERE (k LIKE 'c:%' OR k LIKE 'i:%') AND m>=? GROUP BY uid,k", (mcut,)).fetchall():
            mins[(int(uid_), k)] = int(n or 0)
    except Exception as e:
        print("[relay] 终端列表读取失败: %s" % str(e)[:120], flush=True)
    out = []
    for g in rows:
        uid_, cid, first, last, name = g["uid"], g["cid"], g["first"], g["last"], g["user"]
        uk = _use_key(cid)
        label, ua, _ = labs.get((uid_, uk), ("", "", 0))
        if ua:
            label, kind = _ua_label(ua)   # 历史行的 label 可能是老逻辑写的，按 UA 重算
        else:
            kind = "web"
        out.append({
            "uid": uid_, "cid": cid or "", "user": name or "",
            "first": float(first or 0), "last": float(last or 0),
            "label": label, "ua": ua[:100], "ver": _ua_ver(ua),
            "min30": mins.get((uid_, uk), 0),
            "counted": bool(last and float(last) >= now - CLIENT_SEAT_SEC),
            "kind": kind,
        })
    return web.json_response({"ok": True, "clients": out, "now": now})


def _uid_expr():
    """use_act_log.uid 的统一读法：账号是 'u<id>'、匿名设备是 'd<dev>'
    （subject_of 的编码，隧道每条都这么写），老数据是整数 id，历史行还有 NULL。
    明细页/筛选/名字 JOIN 都按这个表达式看，别各算各的。"""
    c = "a.uid"
    return ("COALESCE(CASE WHEN typeof(%s)='text' AND %s LIKE 'u%%' "
            "THEN CAST(substr(%s,2) AS INTEGER) ELSE %s END,0)" % (c, c, c, c))


def _uid_json(v):
    if isinstance(v, int):
        return v
    return 0 if v is None else str(v)     # 匿名主体 'd<dev>' 原样给人看


async def r_admin_actlog(request):
    """动作明细：按 用户 / 开发机 / 终端 / 时间 / 动作 筛选 use_act_log（保留 3 天）。

    回答「这一分钟到底是谁、用什么、做了什么、做了几次」。
    时间按本地自然日筛选（from/to 都是 YYYY-MM-DD，包含首尾两天）。
    """
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    where, args = [], []

    try:
        uid = int(request.query.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid:
        where.append(_uid_expr() + "=?")
        args.append(uid)
    user = (request.query.get("user") or "").strip()[:40]
    if user:
        # 一个框收「用户名」也收 uid：手机号注册的用户名就是一串数字，
        # 只按名字匹配的话，输入 132****7605 会被当成 uid 查空。
        # 页面上抄下来的「uid 7」「用户 7」也认。
        m = re.match(r"^(?:uid|用户)\s*[:：]?\s*(\d+)$", user, re.I)
        if m:
            where.append(_uid_expr() + "=?")
            args.append(int(m.group(1)))
        else:
            where.append("(u.name LIKE ? OR CAST(" + _uid_expr() + " AS TEXT)=?)")
            args += ["%" + user + "%", user]
    dev = (request.query.get("dev") or "").strip()[:24]
    if dev:
        # ID 前缀或设备名都认：设备列表上显眼的是名字（macmini4），ID 只是小字，
        # 照着名字填「开发机 ID」是很自然的事。
        where.append("(a.dev LIKE ? OR dv.name LIKE ?)")
        args += [dev + "%", "%" + dev + "%"]
    cid = (request.query.get("cid") or "").strip()[:64]
    if cid:
        where.append("a.cid LIKE ?")
        args.append(cid + "%")
    kind = (request.query.get("kind") or "").strip()
    if kind in ("act", "nav", "read"):
        where.append("a.kind=?")
        args.append(kind)
    act = (request.query.get("act") or "").strip()[:40]
    if act:
        where.append("(a.act LIKE ? OR a.path LIKE ?)")
        args += ["%" + act + "%", "%" + act + "%"]
    q = (request.query.get("q") or "").strip()[:40]
    if q:
        like = "%" + q + "%"
        where.append("(a.act LIKE ? OR a.path LIKE ? OR a.ua LIKE ? OR a.cid LIKE ? "
                     "OR u.name LIKE ?)")
        args += [like, like, like, like, like]

    def day_min(ds):
        return int(time.mktime(time.strptime(ds, "%Y-%m-%d")) // 60)

    today = time.strftime("%Y-%m-%d")
    dfrom = (request.query.get("from") or today).strip()
    dto = (request.query.get("to") or dfrom).strip()
    try:
        m0 = day_min(dfrom)
        m1 = day_min(dto) + 24 * 60
    except Exception:
        return web.json_response({"ok": False, "error": "日期格式 YYYY-MM-DD"}, status=400)
    if m1 < m0:
        m0, m1 = m1 - 24 * 60, m0 + 24 * 60
    where.append("a.m>=? AND a.m<?")
    args += [m0, m1]
    try:
        limit = min(3000, max(1, int(request.query.get("limit") or 600)))
    except (TypeError, ValueError):
        limit = 600
    wsql = (" AND ".join(where)) or "1=1"

    rows = db.execute(
        "SELECT " + _uid_expr() + ",a.m,a.src,a.dev,a.cid,a.ua,a.kind,a.act,SUM(a.n),"
        "u.name,dv.name,MAX(a.path) "
        "FROM use_act_log a LEFT JOIN users u ON u.id=" + _uid_expr() + " "
        "LEFT JOIN devices dv ON dv.id=a.dev "
        "WHERE " + wsql + " GROUP BY a.uid,a.m,a.src,a.dev,a.cid,a.kind,a.act,"
        "u.name,dv.name ORDER BY a.m DESC, SUM(a.n) DESC LIMIT ?",
        args + [limit]).fetchall()
    out = []
    for uid_, m, src, dev_, cid_, ua_, kind_, act_, n, name, dname, path_ in rows:
        out.append({"uid": _uid_json(uid_), "m": int(m), "src": src or "",
                    "dev": dev_ or "", "dev_name": dname or "",
                    "cid": cid_ or "", "ua": (ua_ or "")[:80],
                    "kind": kind_ or "", "act": act_ or "", "path": path_ or "",
                    "n": int(n or 0), "user": name or ""})
    # 汇总要跟明细用同一套 FROM：按开发机名筛选时 wsql 里引用了 dv.name，
    # 只 JOIN users 会让两条汇总 SQL 报错，被 except 吞成 0（列表有行、头上却是 0 分钟）。
    frm = ("FROM use_act_log a LEFT JOIN users u ON u.id=" + _uid_expr() + " "
           "LEFT JOIN devices dv ON dv.id=a.dev WHERE " + wsql)
    try:
        minutes = int(db.execute(
            "SELECT COUNT(DISTINCT a.m) " + frm, args).fetchone()[0] or 0)
        total_n = int(db.execute(
            "SELECT COALESCE(SUM(a.n),0) " + frm, args).fetchone()[0] or 0)
    except Exception:
        minutes = total_n = 0
    return web.json_response({"ok": True, "rows": out, "from": dfrom, "to": dto,
                              "limit": limit, "count": len(out),
                              "minutes": minutes, "total_n": total_n})


async def r_admin_device(request):
    """开发机管理：改名/冻结（同时断链）/解冻/删除/重发配对码。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        d = await request.json()
    except Exception:
        d = {}
    act = str(d.get("action") or "")
    # 关日志是「收手」动作，不要求二次验证：排查完想提前关还得等短信太别扭。
    # 其它动作（改名/冻结/删除/重发配对码/开日志）照旧要当次短信码。
    if not (act == "logs" and not d.get("on")):
        # 删开发机是移除类动作：每次都要手机码，不吃一小时通行证
        if not await _adm_sensitive(request, hard=(act == "delete")):
            return _adm_need_sms(db)
    dev = str(d.get("id") or "")
    row = db.execute("SELECT name FROM devices WHERE id=?", (dev,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "开发机不存在"}, status=404)

    async def drop_link():
        ws = (request.app.get("links") or {}).pop(dev, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        (request.app.get("direct") or {}).pop(dev, None)

    if act == "rename":
        name = str(d.get("name") or "").strip()[:40]
        if not name:
            return web.json_response({"ok": False, "error": "名字不能为空"})
        db.execute("UPDATE devices SET name=? WHERE id=?", (name, dev))
        db.commit()
        msg = "已改名"
    elif act == "freeze":
        db.execute("UPDATE devices SET frozen=1 WHERE id=?", (dev,))
        db.commit()
        await drop_link()
        msg = "已冻结并断开中继连接（解冻后开发机会自动重连）"
    elif act == "unfreeze":
        db.execute("UPDATE devices SET frozen=0 WHERE id=?", (dev,))
        db.commit()
        msg = "已解冻"
    elif act == "set_product":
        # 存量设备翻产品线（boss 系列）：enroll 只在新行写 product，改存量走这里。
        # 不 drop_link——产品线变更不影响既有链路；该机下次推 APK 落新槽。
        prod = branding.valid_product(str(d.get("product") or ""))
        db.execute("UPDATE devices SET product=? WHERE id=?", (prod, dev))
        db.commit()
        msg = "产品线已切到 %s（该机下次推 APK 落新槽）" % prod
    elif act == "unbind":
        # 人工解绑（指纹丢了/换了机器的通道）：设备回到匿名免费档，同一指纹可重新登记。
        # 不删行——会话、配对码、用量历史都还在；只是归属清空、档位回 free。
        owner = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()[0]
        db.execute("UPDATE devices SET owner=NULL, anon=1 WHERE id=?", (dev,))
        db.commit()
        _adm_audit(db, request, "device_unbind", owner or 0,
                   {"owner": owner}, {"owner": None})
        await drop_link()      # 让 host 重连后按匿名免费档重新握手
        msg = ("已解绑：开发机回到免费档（原账号 %s）；同一指纹可重新登记"
               % owner if owner else "这台设备本来就没绑账号")
    elif act == "update":
        # 让这台开发机立刻走一遍「检查更新 → 有新版就套用并重启」，跟 App 里那个按钮
        # 同一条链路（host 的 _admin_update）。不然只能等它自己 30 分钟一次的轮询。
        ws = (request.app.get("links") or {}).get(dev)
        if ws is None or ws.closed:
            return web.json_response({"ok": False, "error": "开发机离线，连上后再点"})
        try:
            async with (getattr(ws, "_cb_lock", None) or asyncio.Lock()):
                await ws.send_json({"t": "update"})
        except Exception as e:
            return web.json_response({"ok": False, "error": "通知失败：%s" % str(e)[:80]})
        msg = "已通知开发机检查更新：有新版本会自己装好并重启（约 30 秒），结果看日志里的 [update]"
    elif act == "logs":
        # 远程日志上报：写库（中继重启/重连后仍然生效），同时马上通知在线的那条链路。
        # 打开后最多 24 小时自动关（LOG_MAX_S），到期由 _logs_expire_loop 翻转并通知 host。
        on = 1 if d.get("on") else 0
        until = time.time() + LOG_MAX_S if on else 0
        db.execute("UPDATE devices SET logs_on=?, logs_until=? WHERE id=?", (on, until, dev))
        db.commit()
        ws = (request.app.get("links") or {}).get(dev)
        if ws is not None and not ws.closed:
            try:
                async with (getattr(ws, "_cb_lock", None) or asyncio.Lock()):
                    await ws.send_json({"t": "logs_on", "on": on})
            except Exception:
                pass
        msg = ("已打开日志上报：这台开发机每 15 秒传一次 server.log 尾部，%d 小时后自动关闭"
               "（中继上留 %d 天）" % (LOG_MAX_S // 3600, DEVLOG_KEEP_D)
               if on else "已关闭日志上报")
        if on and ws is None:
            msg += "（设备当前离线，连上后自动开始）"
    elif act == "delete":
        if str(d.get("confirm") or "") != dev:
            return web.json_response({"ok": False, "error": "删除需输入设备 ID 确认"})
        await drop_link()
        db.execute("DELETE FROM pair_codes WHERE device_id=?", (dev,))
        db.execute("DELETE FROM devices WHERE id=?", (dev,))
        db.commit()
        print("[admin] 删除开发机 %s" % dev, flush=True)
        return web.json_response({"ok": True, "msg": "开发机已删除"})
    elif act == "paircode":
        now = time.time()
        code = secrets.token_hex(6)
        db.execute("DELETE FROM pair_codes WHERE device_id=? AND used=0", (dev,))
        db.execute("INSERT INTO pair_codes(code,device_id,created,expires) VALUES(?,?,?,?)",
                   (code, dev, now, now + PAIR_TTL))
        db.commit()
        return web.json_response({"ok": True, "pair_code": code,
                                  "pair_url": "/r/?p=%s" % code,
                                  "expires": now + PAIR_TTL, "msg": "新配对码 15 分钟有效"})
    else:
        return web.json_response({"ok": False, "error": "未知操作"})
    print("[admin] 开发机 %s：%s" % (dev[:8], msg), flush=True)
    return web.json_response({"ok": True, "msg": msg})


def _plans_valid(d):
    if not isinstance(d.get("free"), dict) or not isinstance(d.get("pro"), dict):
        return "免费版和 Pro 档位必须都有"
    for k, v in d.items():
        if k in ("skus", "packs"):
            continue
        if not isinstance(v, dict):
            return "档位 %s 格式不对" % k
        if not isinstance(v.get("name"), str) or not v["name"].strip():
            return "档位 %s 缺名称" % k
        if "sell" in v and not isinstance(v["sell"], bool):
            return "档位 %s.sell 只能是 true/false（false=内部档，不对外售卖）" % k
        lim, feats = v.get("limits"), v.get("features")
        if not isinstance(lim, dict) or not isinstance(feats, dict):
            return "档位 %s 缺 limits/features" % k
        for lk, lv in lim.items():
            if isinstance(lv, bool) or not isinstance(lv, (int, float)) or lv < 0:
                return "%s.%s 必须是非负数字" % (k, lk)
        for fk, fv in feats.items():
            if not isinstance(fv, bool):
                return "%s.%s 必须是 true/false" % (k, fk)
    pk = d.get("packs")
    if pk is not None:
        if not isinstance(pk, list):
            return "packs 要是数组"
        seen = set()
        for i, it in enumerate(pk):
            if not isinstance(it, dict):
                return "第 %d 档时长包格式不对" % (i + 1)
            sku = str(it.get("sku") or "").strip()
            if not re.match(r"^[a-z][a-z0-9_]{1,23}$", sku):
                return "第 %d 档 sku 只能用小写字母/数字/下划线" % (i + 1)
            # 内置包 sku 允许覆盖（改价/改额度）；subscription sku 撞了才算冲突
            if (sku in PAY_SKUS and sku not in PACK_MIN) or sku in seen:
                return "第 %d 档 sku %s 重复或与订阅商品冲突" % (i + 1, sku)
            seen.add(sku)
            if not str(it.get("name") or "").strip():
                return "第 %d 档缺名称" % (i + 1)
            try:
                if float(it.get("money") or -1) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "第 %d 档价格不对" % (i + 1)
            try:
                if int(it.get("min") or 0) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "第 %d 档分钟数要大于 0" % (i + 1)
    sk = d.get("skus")
    if sk is not None:
        if not isinstance(sk, dict):
            return "skus 格式不对"
        for k, v in sk.items():
            if k not in PAY_SKUS:
                return "未知商品 %s" % k
            if not isinstance(v, dict):
                return "商品 %s 格式不对" % k
            try:
                float(v.get("money"))
            except (TypeError, ValueError):
                return "商品 %s 价格不是数字" % k
            try:
                int(v.get("days", 0))
            except (TypeError, ValueError):
                return "商品 %s 天数不是整数" % k
    return ""


async def r_admin_plans(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    if request.method == "GET":
        pm = pack_min()
        return web.json_response({"ok": True, "plans": plans(),
                                  "skus": {k: {"name": v[0], "money": v[1], "days": v[2]}
                                           for k, v in pay_skus().items() if k not in pm},
                                  # 计费刻度（秒）：改完只影响之后的窗口，不动历史账
                                  "meter": {"unit_sec": meter_unit(_db(request.app)),
                                            "min": METER_UNIT_MIN, "max": METER_UNIT_MAX,
                                            "default": METER_UNIT_DEFAULT},
                                  "packs": packs(), "file": _PLAN_PATH})
    if not await _adm_sensitive(request):
        return _adm_need_sms(_db(request.app))
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return web.json_response({"ok": False, "error": "body 要是 JSON 对象"}, status=400)
    cur = dict(plans())
    incoming = raw.get("plans") if isinstance(raw.get("plans"), dict) else raw
    for k in list(incoming.keys()):
        if k in ("skus", "plans", "packs", "meter"):
            continue
        cur[k] = incoming[k]
    if isinstance(incoming.get("skus"), dict) or isinstance(raw.get("skus"), dict):
        sk = dict(cur.get("skus") or {})
        ov = incoming.get("skus") if isinstance(incoming.get("skus"), dict) else raw["skus"]
        for k, v in ov.items():
            sk[k] = v
        cur["skus"] = sk
    # 时长包档位整表替换（可增删改）。老缓存的后台页会把 "packs" 当成一个档位
    # 塞成对象——不是 list 就原样保留，别让一次保存把整张包表清掉（旧库里踩过的坑）
    if isinstance(incoming.get("packs"), list):
        cur["packs"] = incoming["packs"]
    elif isinstance(raw.get("packs"), list):
        cur["packs"] = raw["packs"]
    err = _plans_valid(cur)
    if err:
        return web.json_response({"ok": False, "error": err}, status=400)
    tmp = _PLAN_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _PLAN_PATH)
    except OSError as e:
        return web.json_response({"ok": False, "error": "写 plans.json 失败：%s" % str(e)[:120]},
                                 status=500)
    print("[admin] 套餐配置已更新", flush=True)
    # 计费刻度单独存 meta（不写 plans.json）：秒，1~60
    mm = raw.get("meter")
    if isinstance(mm, dict) and mm.get("unit_sec") is not None:
        try:
            u = meter_set(_db(request.app), int(mm.get("unit_sec")))
            print("[admin] 计费刻度改为 %ds" % u, flush=True)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "计费刻度要是整数秒"}, status=400)
    pm = pack_min()
    return web.json_response({"ok": True, "plans": plans(),
                              "meter": {"unit_sec": meter_unit(_db(request.app)),
                                        "min": METER_UNIT_MIN, "max": METER_UNIT_MAX,
                                        "default": METER_UNIT_DEFAULT},
                              "skus": {k: {"name": v[0], "money": v[1], "days": v[2]}
                                       for k, v in pay_skus().items() if k not in pm},
                              "packs": packs()})


async def r_admin_promo(request):
    """注册赠送策略：GET 读，POST 改（改配置算敏感操作，走短信二次验证）。

    存 meta 表（不是 plans.json），改完立即生效——只影响之后的新注册。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    if request.method == "GET":
        return web.json_response({"ok": True, "promo": promo_cfg(db)})
    if not await _adm_sensitive(request):
        return _adm_need_sms(db)
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return web.json_response({"ok": False, "error": "body 要是 JSON 对象"}, status=400)
    cfg = promo_cfg(db)   # 以现值为底，只覆盖这次发来的键
    cfg["enabled"] = bool(raw.get("enabled", cfg.get("enabled")))
    cfg["banner"] = bool(raw.get("banner", cfg.get("banner")))
    plan = str(raw.get("plan") or cfg.get("plan") or "pro")
    cfg["plan"] = plan if plan in ("pro", "max") else "pro"
    try:
        days = int(raw.get("days", cfg.get("days")))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "赠送天数要是整数"}, status=400)
    if days < 0 or days > 3650:
        return web.json_response({"ok": False, "error": "赠送天数要在 0~3650 之间"}, status=400)
    if cfg["enabled"] and days <= 0:
        return web.json_response({"ok": False, "error": "开了赠送天数就要大于 0"}, status=400)
    cfg["days"] = days
    cfg["sku"] = str(raw.get("sku", cfg.get("sku")) or "")[:32]
    cfg["note"] = str(raw.get("note", cfg.get("note")) or "")[:200]
    until = raw.get("until", cfg.get("until"))
    until = 0 if until in ("", None) else until
    try:
        until = int(until)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "活动截止时间不合法"}, status=400)
    cfg["until"] = max(0, int(until))
    _meta_json_save(db, _META_PROMO, cfg)
    print("[admin] 注册赠送策略已更新 enabled=%s %s %d 天 截止=%s"
          % (cfg["enabled"], cfg["plan"], cfg["days"],
             time.strftime("%Y-%m-%d %H:%M", time.localtime(cfg["until"])) if cfg["until"] else "长期"),
          flush=True)
    return web.json_response({"ok": True, "promo": cfg})


async def r_admin_orders(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    st = (request.query.get("status") or "all").strip()
    q = (request.query.get("q") or "").strip()
    cond, args = [], []
    if st == "paid":
        cond.append("o.status=1")
    elif st == "pending":
        cond.append("o.status=0")
    elif st == "failed":
        cond.append("o.status<0")
    if q:
        cond.append("(o.out_trade_no LIKE ? OR u.name LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    rows = db.execute(
        "SELECT o.out_trade_no,o.uid,COALESCE(u.name,'(已删)'),o.sku,o.money,o.status,"
        "o.created,o.paid,o.trade_no,o.type FROM orders o LEFT JOIN users u ON u.id=o.uid"
        + where + " ORDER BY o.created DESC LIMIT 300", args).fetchall()
    names = {k: (v[0] if v else k) for k, v in (pay_skus() or {}).items()}
    return web.json_response({"ok": True, "orders": [{
        "otn": r[0], "uid": r[1], "user": r[2], "sku": r[3],
        "sku_name": names.get(r[3], r[3]), "money": r[4], "status": r[5],
        "created": r[6] or 0, "paid": r[7] or 0, "trade_no": r[8] or "",
        "type": r[9] or ""} for r in rows]})


async def r_admin_order(request):
    """订单操作：requery（去支付平台二次确认后入账）/ mark（人工确认入账，需订单号确认）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    if not await _adm_sensitive(request):
        return _adm_need_sms(db)
    try:
        d = await request.json()
    except Exception:
        d = {}
    otn = str(d.get("otn") or "")
    act = str(d.get("action") or "")
    row = db.execute("SELECT status,money FROM orders WHERE out_trade_no=?", (otn,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "订单不存在"}, status=404)
    if act == "requery":
        if not (ZPAY_PID and ZPAY_KEY):
            return web.json_response({"ok": False, "error": "中继未配置收款"})
        q2 = await asyncio.get_event_loop().run_in_executor(None, _zpay_query, otn)
        if not isinstance(q2, dict):
            return web.json_response({"ok": False, "error": "支付平台查不到，稍后再试"})
        paid = int(q2.get("code") or 0) == 1 and int(q2.get("status") or 0) == 1
        if not paid:
            return web.json_response({"ok": False, "error": "支付平台显示未支付",
                                      "platform": {k: q2.get(k) for k in ("code", "status")}})
        from decimal import Decimal
        if q2.get("money") is not None:
            try:
                if Decimal(str(q2.get("money"))) != Decimal(str(row[1])):
                    return web.json_response({"ok": False, "error": "金额不符，拒绝入账"})
            except Exception:
                return web.json_response({"ok": False, "error": "金额格式异常，拒绝入账"})
        ok, why, info = _pay_apply(db, otn, str(q2.get("trade_no") or ""),
                                   str(q2.get("type") or ""))
        if ok and info and info.get("new"):
            _alert_pay(request.app, db, info)
            asyncio.create_task(_push_use_ack_uid(request.app, info.get("uid")))
        return web.json_response({"ok": ok, "msg": why})
    if act == "mark":
        if str(d.get("confirm") or "") != otn:
            return web.json_response({"ok": False, "error": "人工入账需输入订单号确认"})
        print("[admin] 人工确认到账 otn=%s" % otn, flush=True)
        ok, why, info = _pay_apply(db, otn, str(d.get("trade_no") or "manual"), "manual")
        if ok and info and info.get("new"):
            _alert_pay(request.app, db, info)
            asyncio.create_task(_push_use_ack_uid(request.app, info.get("uid")))
        return web.json_response({"ok": ok, "msg": why})
    return web.json_response({"ok": False, "error": "未知操作"})


async def r_admin_usage(request):
    """用量曲线 + 今日用户排行：远程分钟、语音秒数、模型调用。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        days = min(60, max(1, int(request.query.get("days") or 14)))
    except ValueError:
        days = 14
    out = {}
    order = []
    now = time.time()
    for i in range(days):
        ds = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        order.append(ds)
        out[ds] = {"use_sec": 0, "use_min": 0, "asr_sec": 0, "llm": 0}
    rows = db.execute("SELECT k,n FROM usage WHERE k LIKE 'use:u%' OR k LIKE 'asr:%' "
                      "OR k LIKE 'llm:u%'").fetchall()
    today = time.strftime("%Y-%m-%d")
    per_user = {}
    for k, n in rows:
        parts = k.split(":")
        if len(parts) < 4:
            continue
        kind, date = parts[0], parts[-1]
        if date not in out:
            continue
        if kind == "use":
            out[date]["use_sec"] += int(n)
            if date == today:
                uid = parts[1][1:] if parts[1].startswith("u") else ""
                if uid.isdigit():
                    pu = per_user.setdefault(int(uid), {"use_sec": 0, "use_min": 0, "asr_sec": 0, "llm": 0})
                    pu["use_sec"] += int(n)
        elif kind == "asr":
            out[date]["asr_sec"] += int(n)
            if date == today and parts[1].startswith("u"):
                uid = parts[1][1:]
                if uid.isdigit():
                    pu = per_user.setdefault(int(uid), {"use_min": 0, "asr_sec": 0, "llm": 0})
                    pu["asr_sec"] += int(n)
        elif kind == "llm":
            out[date]["llm"] += int(n)
            if date == today:
                uid = parts[1][1:] if parts[1].startswith("u") else ""
                if uid.isdigit():
                    pu = per_user.setdefault(int(uid), {"use_min": 0, "asr_sec": 0, "llm": 0})
                    pu["llm"] += int(n)
    users = []
    for uid, v in sorted(per_user.items(), key=lambda kv: -sum(kv[1].values()))[:50]:
        r = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
        users.append({"id": uid, "name": (r[0] if r else "(已删)"), **v})
    return web.json_response({"ok": True, "days": order,
                              "series": [{"date": d, **out[d]} for d in order],
                              "today_users": users,
                              # 用量页也放一份计费刻度：改计时单位不用再翻到套餐页
                              "meter": {"unit_sec": meter_unit(db),
                                        "min": METER_UNIT_MIN, "max": METER_UNIT_MAX,
                                        "default": METER_UNIT_DEFAULT}})


async def r_admin_use_clients(request):
    """某账号某天的「分钟 × 终端」明细——用户对使用时长有疑问时照着查。
    计费仍按账号每分钟一分钟；这里只回答「是哪个终端在用」。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        uid = int(request.query.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return web.json_response({"ok": False, "error": "缺 uid"}, status=400)
    day = (request.query.get("date") or time.strftime("%Y-%m-%d")).strip()
    try:
        m0 = int(time.mktime(time.strptime(day, "%Y-%m-%d")) // 60)
    except Exception:
        return web.json_response({"ok": False, "error": "日期格式 YYYY-MM-DD"}, status=400)
    m1 = m0 + 24 * 60
    try:
        limit = min(3000, max(1, int(request.query.get("limit") or 1200)))
    except ValueError:
        limit = 1200
    rows = db.execute(
        "SELECT m,k,kind,label,ua,n,last FROM usage_client "
        "WHERE uid=? AND m>=? AND m<? ORDER BY m DESC, last DESC LIMIT ?",
        (uid, m0, m1, limit)).fetchall()
    clients = [{"key": r[0], "label": r[1] or r[0], "kind": r[2] or "",
                "minutes": int(r[3] or 0)}
               for r in db.execute(
                   "SELECT k,label,kind,COUNT(*) FROM usage_client "
                   "WHERE uid=? AND m>=? AND m<? GROUP BY k ORDER BY COUNT(*) DESC",
                   (uid, m0, m1)).fetchall()]
    out = []
    for m, k, kind, label, ua, n, last in rows:
        out.append({"m": int(m), "time": time.strftime("%H:%M", time.localtime(int(m) * 60)),
                    "key": k, "kind": kind or "", "label": label or k,
                    "ua": ua or "", "n": int(n or 0),
                    "last": float(last or 0)})
    total = db.execute("SELECT COUNT(DISTINCT m) FROM usage_client WHERE uid=? AND m>=? AND m<?",
                       (uid, m0, m1)).fetchone()[0] or 0
    return web.json_response({"ok": True, "uid": uid, "day": day,
                              "minutes": int(total), "clients": clients, "rows": out})


async def r_admin_user_detail(request):
    """用户的使用详情：每个客户端、每台开发机分别用掉了多少分钟。

    「下午没怎么用，时长怎么涨这么多？是哪台设备用掉的？」——后台照着这个答：
      · 客户端：cb_cid + 机型/版本标签 + 首次/最后出现 + 当天/近 7 天分钟 + 可移除；
      · 开发机：设备名/编号 + 在线状态 + 最后在线 + 当天/近 7 天分钟；
      · 每天的分钟数（近 14 天）。
    计费仍按账号每分钟一分钟，这里只回答「是谁用掉的」。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        uid = int(request.query.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return web.json_response({"ok": False, "error": "缺 uid"}, status=400)
    day = (request.query.get("date") or time.strftime("%Y-%m-%d")).strip()
    try:
        m0 = int(time.mktime(time.strptime(day, "%Y-%m-%d")) // 60)
    except Exception:
        return web.json_response({"ok": False, "error": "日期格式 YYYY-MM-DD"}, status=400)
    m1 = m0 + 24 * 60
    w0 = m0 - 6 * 24 * 60          # 近 7 天（含当天）
    now = time.time()

    def mins_of(k, a, b):
        return int(db.execute("SELECT COUNT(DISTINCT m) FROM usage_client "
                               "WHERE uid=? AND k=? AND m>=? AND m<?",
                               (uid, k, a, b)).fetchone()[0] or 0)

    # 客户端：登记表（cb_cid）+ 它在 usage_client 里的标签/机型
    lab = {}
    for k, lbl, kind, last in db.execute(
            "SELECT k,label,kind,last FROM usage_client WHERE uid=? AND k LIKE 'c:%' "
            "ORDER BY last DESC", (uid,)):
        if k not in lab:
            lab[k] = (lbl or "", kind or "")
    clients = []
    for cid, first, last in db.execute(
            "SELECT cid,first,last FROM clients WHERE uid=? ORDER BY last DESC",
            (uid,)).fetchall():
        k = "c:" + cid
        lbl, kind = lab.get(k, ("", ""))
        clients.append({
            "cid": cid, "label": lbl, "kind": kind,
            "first": float(first or 0), "last": float(last or 0),
            "min_day": mins_of(k, m0, m1), "min_week": mins_of(k, w0, m1),
        })

    # 开发机：这台机器报上来的分钟（k="d:<设备号>"）+ 设备表的状态
    links = request.app.get("links") or {}
    devices = []
    for did, name, created, last_seen, frozen in db.execute(
            "SELECT id,name,created,last_seen,frozen FROM devices WHERE owner=? "
            "ORDER BY last_seen DESC", (uid,)).fetchall():
        k = "d:" + str(did)[:24]
        devices.append({
            "id": did, "name": name or "", "created": float(created or 0),
            "last_seen": float(last_seen or 0), "frozen": bool(frozen),
            "online": did in links,
            "min_day": mins_of(k, m0, m1), "min_week": mins_of(k, w0, m1),
        })

    # 中继这一分钟没看到任何客户端（开发机直连/断链补报）：没细分到设备，
    # 老数据（细分之前）也全在这一栏里。
    direct_day = int(db.execute(
        "SELECT COUNT(DISTINCT m) FROM usage_client WHERE uid=? AND k='direct' "
        "AND m>=? AND m<?", (uid, m0, m1)).fetchone()[0] or 0)
    direct_week = int(db.execute(
        "SELECT COUNT(DISTINCT m) FROM usage_client WHERE uid=? AND k='direct' "
        "AND m>=?", (uid, w0)).fetchone()[0] or 0)

    days = []
    for i in range(13, -1, -1):
        ds = time.strftime("%Y-%m-%d", time.localtime((m0 - i * 1440) * 60))   # m0 是分钟
        days.append({"day": ds, "min": usage_get(db, "use:u%d:d:%s" % (uid, ds))})

    # 时长包流水：非扣费的大额变动逐笔列出来（扣费一分钟一条，只汇总今天的）
    lt = time.localtime()
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    charged_today = -int(db.execute(
        "SELECT COALESCE(SUM(delta),0) FROM use_credit_log "
        "WHERE uid=? AND src='charge' AND ts>=?", (uid, day0)).fetchone()[0] or 0)
    credit_log = []
    for dl, be, af, sr, no, ts in db.execute(
            "SELECT delta,before,after,src,note,ts FROM use_credit_log "
            "WHERE uid=? AND src!='charge' ORDER BY id DESC LIMIT 20", (uid,)).fetchall():
        credit_log.append({"delta": int(dl), "before": int(be or 0), "after": int(af or 0),
                           "src": sr or "", "note": no or "", "ts": float(ts or 0)})

    lim = plan_limits(db, uid)
    # 动作记录：这一天每分钟做了什么（中继亲眼看到的 + 开发机补报的直连动作）。
    # 按 (分钟, 来源, 设备, 客户端, 动作) 合并计数，最新的排在前面。
    acts = []
    try:
        for m, src, dev_, cid_, ua_, kd, ac, nn in db.execute(
                "SELECT m,src,dev,cid,ua,kind,act,SUM(n) FROM use_act_log "
                "WHERE uid=? AND m>=? AND m<? GROUP BY m,src,dev,cid,kind,act "
                "ORDER BY m DESC, Sum(n) DESC LIMIT 400",
                (uid, m0, m1)).fetchall():
            acts.append({"m": int(m), "src": src or "", "dev": dev_ or "",
                         "cid": cid_ or "", "ua": (ua_ or "")[:60],
                         "kind": kd or "", "act": ac or "", "n": int(nn or 0)})
    except Exception as e:
        print("[relay] 动作记录读取失败: %s" % str(e)[:120], flush=True)
    return web.json_response({
        "ok": True, "uid": uid, "day": day, "now": now,
        "credit_log": credit_log, "charged_today": charged_today,
        "acts": acts,
        "clients": clients, "devices": devices,
        "direct_day": direct_day, "direct_week": direct_week,
        "days": days,
        "used_day": usage_get(db, "use:u%d:d:%s" % (uid, day)),
        "cap": use_cap(db, uid),          # 秒（套餐日上限）
        "credit": use_credit_of(db, uid),  # 秒（时长包余额）
        "clients_cap": int(lim.get("clients") or 0),
        "clients_used": clients_used(db, uid),
    })


async def r_admin_alerts(request):
    """收款提醒渠道：邮件 / 钉钉 / 企业微信 / Server酱 / PushPlus。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    if request.method == "GET":
        c = _alert_cfg(db)
        return web.json_response({"ok": True, "cfg": {
            "email": "\n".join(c["email"]), "pay": c["pay"], "reg": c["reg"],
            "dingtalk": "\n".join(_mask_secret(x) for x in c["dingtalk"]),
            "wecom": "\n".join(_mask_secret(x) for x in c["wecom"]),
            "serverchan": "\n".join(_mask_secret(x) for x in c["serverchan"]),
            "pushplus": "\n".join(_mask_secret(x) for x in c["pushplus"]),
        }, "smtp": {"on": bool(SMTP_HOST), "user": SMTP_USER, "from": SMTP_FROM}})
    # 提醒渠道（含发送测试）不做手机二次验证；其余增删改仍校验。
    try:
        d = await request.json()
    except Exception:
        d = {}
    c = _alert_cfg(db)
    for k, cap in (("email", 120), ("dingtalk", 400), ("wecom", 400),
                   ("serverchan", 200), ("pushplus", 200)):
        if k in d:
            c[k] = _merge_multi(c.get(k), d.get(k), cap)
    if "pay" in d:
        c["pay"] = bool(d.get("pay"))
    if "reg" in d:
        c["reg"] = bool(d.get("reg"))
    _alert_save(db, c)
    if str(d.get("action") or "") == "test":
        await _alert_send(request.app, "🔔 CodeBoss 提醒测试", "这是一条测试消息，收到说明渠道配置正确。")
        return web.json_response({"ok": True, "msg": "已发送测试消息（各渠道失败会写在中继日志）"})
    print("[admin] 收款提醒配置已更新", flush=True)
    return web.json_response({"ok": True, "msg": "已保存"})


async def r_admin_storage(request):
    """运行配置：对象存储（OSS）。密钥只回打码值，回传打码值=保持原值。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    c = _oss_cfg(db)
    if request.method == "GET":
        return web.json_response({"ok": True, "cfg": {
            "on": c["on"], "endpoint": c["endpoint"], "bucket": c["bucket"],
            "ak": c["ak"], "sk": _mask_secret(c["sk"]), "prefix": c["prefix"],
        }, "ready": _oss_ready(c)})
    try:
        d = await request.json()
    except Exception:
        d = {}
    if "on" in d:
        c["on"] = bool(d.get("on"))
    for k in ("endpoint", "bucket", "ak", "prefix"):
        if k in d:
            c[k] = str(d.get(k) or "").strip()[:400]
    c["prefix"] = _norm_prefix(c["prefix"]) or "codeboss/"
    if "sk" in d:
        v = str(d.get("sk") or "").strip()
        if not v.startswith("***"):
            c["sk"] = v[:200]
    _meta_json_save(db, _META_OSS, c)
    print("[admin] 对象存储配置已更新 on=%s bucket=%s" % (c["on"], c["bucket"]), flush=True)
    if str(d.get("action") or "") == "test":
        if not _oss_ready(c):
            return web.json_response({"ok": False, "error": "配置不完整（endpoint/bucket/AK/SK/开关）"})
        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(None, _oss_test, c)
        return web.json_response({"ok": ok, "msg": msg, "error": "" if ok else msg})
    return web.json_response({"ok": True, "msg": "已保存", "ready": _oss_ready(c)})


# ---------- 文件浏览（OSS / 本机）----------
# 目的：备份到底传没传到 OSS，打开「存储」页就能看见对象的 Key/大小/时间，
# 不用登阿里云控制台。只读：不允许删除/覆盖。
_LOCAL_FILE_DIRS = ("backup", "dist", "feedback")
_DIST_NAMES = ("codeboss-apk.bin", "codeboss-ios.bin", "codeboss-host.tar.gz",
               "codeboss-mac.dmg", "bosshost-mac.dmg", "BossHost-Setup.exe")
_TEXT_EXT = (".txt", ".log", ".json", ".md", ".csv")
_FILE_CAP = 128 * 1048576


def _local_file_list(app, d):
    base = os.path.dirname(os.path.abspath(app["dbp"]))
    out = []
    if d == "backup":
        for p in _db_backup_list(app["dbp"]):
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": os.path.basename(p), "size": st.st_size,
                        "mtime": st.st_mtime, "storage": ""})
    elif d == "dist":
        for n in _DIST_NAMES:
            p = os.path.join(base, n)
            if os.path.isfile(p):
                st = os.stat(p)
                out.append({"name": n, "size": st.st_size,
                            "mtime": st.st_mtime, "storage": ""})
        out.sort(key=lambda x: -x["mtime"])
    elif d == "feedback":
        root = os.path.join(base, "feedback")
        try:
            fids = os.listdir(root)
        except OSError:
            fids = []
        for fid in fids:
            fd = os.path.join(root, fid)
            if not os.path.isdir(fd):
                continue
            try:
                names = os.listdir(fd)
            except OSError:
                continue
            for n in names:
                p = os.path.join(fd, n)
                if not os.path.isfile(p):
                    continue
                st = os.stat(p)
                out.append({"name": fid + "/" + n, "size": st.st_size,
                            "mtime": st.st_mtime, "storage": ""})
        out.sort(key=lambda x: -x["mtime"])
    return out


def _local_file_path(app, d, name):
    """只允许访问已知清单里的文件（防目录穿越）。"""
    if d == "backup":
        for p in _db_backup_list(app["dbp"]):
            if os.path.basename(p) == name:
                return p
        return None
    base = os.path.dirname(os.path.abspath(app["dbp"]))
    if d == "dist":
        return os.path.join(base, name) if name in _DIST_NAMES else None
    if d == "feedback":
        parts = str(name).split("/")
        if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9._-]+", x) for x in parts):
            p = os.path.join(base, "feedback", parts[0], parts[1])
            return p if os.path.isfile(p) else None
    return None


async def r_admin_files(request):
    """列文件：OSS 对象（可按前缀）或本机备份/分发产物/反馈附件。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    where = str(request.query.get("where") or "oss")
    if where == "local":
        d = str(request.query.get("dir") or "backup")
        if d not in _LOCAL_FILE_DIRS:
            return web.json_response({"ok": False, "error": "不支持的目录"})
        return web.json_response({"ok": True, "where": "local", "dir": d,
                                  "files": _local_file_list(request.app, d)})
    c = _oss_cfg(db)
    if not _oss_ready(c):
        return web.json_response({"ok": False, "error": "OSS 未配置或未启用"})
    base = c["prefix"] or ""
    prefix = str(request.query.get("prefix") or base)
    if base and not prefix.startswith(base):   # 只能看自己前缀下的东西
        prefix = base
    marker = str(request.query.get("marker") or "")
    try:
        files, nxt = await asyncio.get_event_loop().run_in_executor(
            None, _oss_list, c, prefix, marker, 300)
    except RuntimeError as e:
        return web.json_response({"ok": False, "error": str(e)})
    for f in files:
        f["name"] = f["key"][len(prefix):] if f["key"].startswith(prefix) else f["key"]
    return web.json_response({"ok": True, "where": "oss", "prefix": prefix,
                              "files": files, "next": nxt})


async def r_admin_file(request):
    """取单个文件：文本在页面里预览，其它按附件下载。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    where = str(request.query.get("where") or "oss")
    name = ""
    if where == "oss":
        c = _oss_cfg(db)
        if not _oss_ready(c):
            return web.json_response({"ok": False, "error": "OSS 未配置或未启用"}, status=400)
        key = str(request.query.get("key") or "")
        pf = c["prefix"] or ""
        if pf and not key.startswith(pf):
            return web.json_response({"ok": False, "error": "只能读取配置前缀下的文件"},
                                     status=400)
        try:
            blob, err = await asyncio.get_event_loop().run_in_executor(
                None, _oss_get, c, key)
        except RuntimeError as e:
            blob, err = None, str(e)
        if blob is None:
            return web.json_response({"ok": False, "error": err or "读取失败"}, status=502)
        name = key.rsplit("/", 1)[-1]
    else:
        d = str(request.query.get("dir") or "backup")
        name = str(request.query.get("name") or "")
        if d not in _LOCAL_FILE_DIRS:
            return web.json_response({"ok": False, "error": "不支持的目录"}, status=400)
        p = _local_file_path(request.app, d, name)
        if not p:
            return web.json_response({"ok": False, "error": "文件不存在"}, status=404)
        try:
            with open(p, "rb") as f:
                blob = f.read()
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)[:120]}, status=500)
        name = os.path.basename(p)
    if len(blob) > _FILE_CAP:
        return web.json_response({"ok": False, "error": "文件太大（>128 MB）"}, status=400)
    inline = name.lower().endswith(_TEXT_EXT)
    hdrs = {"Content-Disposition": ("inline" if inline else
                                    'attachment; filename="%s"' % urllib.parse.quote(name)),
            "Cache-Control": "no-store"}
    if inline:
        return web.Response(body=blob, content_type="text/plain", charset="utf-8",
                            headers=hdrs)
    return web.Response(body=blob, content_type="application/octet-stream",
                        headers=hdrs)


async def r_admin_models(request):
    """运行配置：语音识别 / 语音合成 / 文本模型（DashScope 兼容接口）。"""
    global ASR_KEY
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    if request.method == "GET":
        c = _model_cfg(db)
        return web.json_response({"ok": True, "cfg": {
            "key": _mask_secret(c["key"]),
            "asr": "\n".join(c["asr"]), "tts": "\n".join(c["tts"]),
            "voice": c["voice"], "llm": "\n".join(c["llm"]),
            "fix": "\n".join(c["fix"]), "llm_base": c["llm_base"],
        }, "key_ready": bool(c["key"]), "def": dict(_M_DEF)})
    try:
        d = await request.json()
    except Exception:
        d = {}
    cur = _meta_json(db, _META_MODELS)
    for k, dv in (("asr", ASR_MODEL), ("tts", TTS_MODEL), ("llm", LLM_MODEL)):
        if k in d:
            cur[k] = _model_name_list(d.get(k), dv)
    if "fix" in d:
        fr = str(d.get("fix") or "").strip()
        cur["fix"] = _model_name_list(fr, "") if fr else ""   # 空 = 跟文本模型
    for k in ("voice", "llm_base"):
        if k in d:
            cur[k] = str(d.get(k) or "").strip()[:300]
    if "key" in d:
        v = str(d.get("key") or "").strip()
        if not v.startswith("***"):
            cur["key"] = v[:300]
    _meta_json_save(db, _META_MODELS, cur)
    _model_apply(db)
    if str(d.get("action") or "") == "test":
        ok, msg = await _model_test()
        return web.json_response({"ok": ok, "msg": msg, "error": "" if ok else msg})
    return web.json_response({"ok": True, "msg": "已保存并生效"})


async def _model_test():
    """用当前配置真跑一次最小请求：确认密钥、模型名与接口地址都通。"""
    if not ASR_KEY:
        return False, "还没配置服务密钥（DASHSCOPE / 阿里云百炼 API Key）"
    loop = asyncio.get_event_loop()

    def _llm():
        last = ""
        for m in (LLM_MODELS or [LLM_MODEL]):
            try:
                body = json.dumps({"model": m, "max_tokens": 8,
                                   "messages": [{"role": "user", "content": "ping"}]}).encode()
                req = urllib.request.Request(
                    LLM_BASE, data=body,
                    headers={"Authorization": "Bearer " + ASR_KEY,
                             "Content-Type": "application/json"})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=20) as r:
                    d = json.loads(r.read().decode("utf-8"))
                txt = (((d.get("choices") or [{}])[0].get("message") or {})
                       .get("content") or "")
                return "模型 %s 可用（回复 %r）" % (m, str(txt)[:40])
            except urllib.error.HTTPError as e:
                last = "模型 %s HTTP %s：%s" % (
                    m, e.code, e.read()[:200].decode("utf-8", "replace"))
            except Exception as e:
                last = "模型 %s：%s" % (m, str(e)[:160])
            print("[relay] 测试模型失败 %s" % last[:200], flush=True)
        raise RuntimeError(last or "没有配置文本模型")

    try:
        return True, await loop.run_in_executor(None, _llm)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s：%s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
    except Exception as e:
        return False, str(e)[:300]


async def r_admin_tasks(request):
    """计划任务：列表 / 新建 / 改 / 开关 / 立即执行 / 删除。
    kind 只能取白名单（TASK_KINDS），不开放任意命令。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    _task_seed(db)
    if request.method == "GET":
        rid = str(request.query.get("runs") or "")
        if rid:
            rows = db.execute(
                "SELECT started,dur,ok,msg,trigger FROM task_runs WHERE tid=? "
                "ORDER BY id DESC LIMIT 100", (rid,)).fetchall()
            return web.json_response({"ok": True, "runs": [
                {"started": float(x[0] or 0), "dur": float(x[1] or 0),
                 "ok": bool(x[2]), "msg": x[3] or "", "trigger": x[4] or "auto"}
                for x in rows]})
        return web.json_response({"ok": True, "tasks": _task_list(db),
                                  "kinds": [{"k": k, "label": v}
                                            for k, v in TASK_KINDS.items()]})
    try:
        d = await request.json()
    except Exception:
        d = {}
    act = str(d.get("action") or "save")
    tid = str(d.get("id") or "")[:40]
    if act == "create":
        kind = str(d.get("kind") or "")
        if kind not in TASK_KINDS:
            return web.json_response({"ok": False, "error": "不支持的任务类型"})
        name = str(d.get("name") or "").strip()[:40] or TASK_KINDS[kind][:20]
        tid = secrets.token_hex(6)
        cfg = d.get("cfg") if isinstance(d.get("cfg"), dict) else {}
        nv = d.get("notify")
        if isinstance(nv, list):
            cfg["notify"] = [str(x) for x in nv if str(x) in TASK_CHANNELS]
        cron = str(d.get("cron") or "").strip()[:120]
        if cron and not _cron_valid(cron):
            return web.json_response({"ok": False, "error": "定时表达式不对（例：0 3 * * * 每天 03:00）"})
        db.execute("INSERT INTO tasks(id,name,kind,every_s,enabled,cron,cfg,created) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (tid, name, kind, max(30, int(d.get("every_s") or 3600)),
                    1 if d.get("enabled", True) else 0, cron,
                    json.dumps(cfg, ensure_ascii=False), time.time()))
        db.commit()
        print("[admin] 新建计划任务 %s %s%s" % (name, kind,
                                              (" 定时 " + cron) if cron else ""), flush=True)
        return web.json_response({"ok": True, "msg": "已创建", "id": tid})
    row = db.execute("SELECT id,name,kind,every_s,enabled,cron,cfg,last_run,last_ok,last_msg "
                     "FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "任务不存在"})
    t = _task_row(row)
    if act == "delete":
        db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        db.commit()
        print("[admin] 删除计划任务 %s" % t["name"], flush=True)
        return web.json_response({"ok": True, "msg": "已删除"})
    if act == "toggle":
        en = 0 if t["enabled"] else 1
        db.execute("UPDATE tasks SET enabled=? WHERE id=?", (en, tid))
        db.commit()
        return web.json_response({"ok": True, "msg": "已开启" if en else "已暂停", "enabled": bool(en)})
    if act == "run":
        cfg = t["cfg"]
        try:
            cfg = json.loads(cfg or "{}")
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        now = time.time()
        db.execute("UPDATE tasks SET last_run=? WHERE id=?", (now, tid))
        db.commit()
        loop = asyncio.get_event_loop()
        try:
            ok, msg = await loop.run_in_executor(
                None, _task_run_sync, request.app["dbp"], t["kind"], cfg, _oss_cfg(db))
        except Exception as e:
            ok, msg = False, "异常：%s" % str(e)[:200]
        db.execute("UPDATE tasks SET last_ok=?,last_msg=? WHERE id=?",
                   (1 if ok else 0, str(msg)[:400], tid))
        db.commit()
        _task_log(db, tid, now, time.time() - now, ok, msg, "manual")
        print("[admin] 立即执行 %s ok=%s %s" % (t["name"], ok, msg), flush=True)
        return web.json_response({"ok": True, "ran_ok": ok, "msg": msg})
    # save
    if "name" in d:
        db.execute("UPDATE tasks SET name=? WHERE id=?",
                   (str(d.get("name") or "").strip()[:40] or t["name"], tid))
    if isinstance(d.get("cfg"), dict):
        db.execute("UPDATE tasks SET cfg=? WHERE id=?",
                   (json.dumps(d["cfg"], ensure_ascii=False), tid))
    if "cron" in d:
        cron = str(d.get("cron") or "").strip()[:120]
        if cron and not _cron_valid(cron):
            return web.json_response({"ok": False, "error": "定时表达式不对（例：0 3 * * * 每天 03:00）"})
        db.execute("UPDATE tasks SET cron=? WHERE id=?", (cron, tid))
        print("[admin] 计划任务 %s 定时改成 %s" % (t["name"], cron or "（按间隔）"), flush=True)
    if "notify" in d:
        cfg = _cfg_dict(t["cfg"])
        nv = d.get("notify")
        if isinstance(nv, str):
            nv = [x for x in re.split(r"[,;\s]+", nv) if x]
        cfg["notify"] = [str(x) for x in (nv or []) if str(x) in TASK_CHANNELS]
        db.execute("UPDATE tasks SET cfg=? WHERE id=?",
                   (json.dumps(cfg, ensure_ascii=False), tid))
        print("[admin] 计划任务 %s 失败通知=%s"
              % (t["name"], ",".join(cfg["notify"]) or "全部已配置渠道"), flush=True)
    if "every_s" in d:
        db.execute("UPDATE tasks SET every_s=? WHERE id=?",
                   (max(30, int(d.get("every_s") or t["every_s"])), tid))
    if "enabled" in d:
        db.execute("UPDATE tasks SET enabled=? WHERE id=?",
                   (1 if d.get("enabled") else 0, tid))
    db.commit()
    return web.json_response({"ok": True, "msg": "已保存"})


async def r_admin_feed(request):
    """管理页轮询：最近到账订单（用于实时提醒/角标）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        after = float(request.query.get("after") or 0)
    except ValueError:
        after = 0
    now = time.time()
    rows = db.execute(
        "SELECT o.out_trade_no,o.money,o.sku,o.paid,COALESCE(u.name,'(已删)') "
        "FROM orders o LEFT JOIN users u ON u.id=o.uid "
        "WHERE o.status=1 AND o.paid>? ORDER BY o.paid ASC LIMIT 30", (after,)).fetchall()
    names = {k: (v[0] if v else k) for k, v in (pay_skus() or {}).items()}
    return web.json_response({"ok": True, "now": now, "orders": [{
        "otn": r[0], "money": r[1], "sku": r[2], "sku_name": names.get(r[2], r[2]),
        "paid": r[3], "user": r[4]} for r in rows]})


async def _gh_release_fetch(tag_only=False):
    """从 GitHub release 取更新物。封装运行（Linux 二进制）→ 取 relay-v* 流的最新包里的
    codeboss-relay；源码运行 → 取 v* 流的 relay.py。返回 (data, tag, err)。
    tag_only=True 只查最新 tag 不下包（配合 RELAY_VER 短路「已是最新」）。"""
    from aiohttp import ClientSession, ClientTimeout
    H = {"Accept": "application/vnd.github+json", "User-Agent": "codeboss-relay"}
    if GH_TOKEN:    # 没配令牌就不带凭证头：空 Bearer 会被 GitHub 一律打 401
        H["Authorization"] = "Bearer %s" % GH_TOKEN
    frozen = _is_frozen()
    want = "relay-v" if frozen else None
    try:
        async with ClientSession(timeout=ClientTimeout(total=120)) as s:
            async with s.get("%s/repos/%s/releases?per_page=20" % (GH_API, GH_REPO),
                             headers=H) as r:
                if r.status == 404:
                    if GH_TOKEN:
                        return None, "", "仓库 %s 不存在，或令牌没授权它（查 RELAY_GH_REPO 拼写；PAT 需 Contents 只读且圈中此仓库）" % GH_REPO
                    return None, "", "仓库 %s 拉不到：私有仓库需配 RELAY_GH_TOKEN（PAT，Contents 只读）" % GH_REPO
                if r.status == 401:
                    return None, "", "令牌无效或已过期（查 RELAY_GH_TOKEN）"
                if r.status == 403:
                    return None, "", "令牌权限不足或触发 GitHub 限流（RELAY_GH_TOKEN）"
                if r.status != 200:
                    return None, "", "GitHub API HTTP %s" % r.status
                rels = await r.json()
            rel = None
            for x in rels or []:
                t = x.get("tag_name") or ""
                if want:
                    if t.startswith("relay-v"):
                        rel = x
                        break
                elif not t.startswith("relay-v"):
                    rel = x
                    break
            if rel is None:
                return None, "", "没有可用的 %s release" % ("relay-v*（二进制发布流）" if frozen else "v*")
            tag = rel.get("tag_name") or "?"
            if tag_only:
                return None, tag, ""
            assets = rel.get("assets") or []

            async def _dl(a):
                async with s.get(a["url"], headers=dict(H, Accept="application/octet-stream"),
                                 timeout=ClientTimeout(total=300)) as r2:
                    if r2.status != 200:
                        return None
                    return await r2.read()

            if frozen:
                tg = next((a for a in assets if a.get("name", "").endswith((".tar.gz", ".tgz"))), None)
                if not tg:
                    return None, tag, "relay release 没有 tar.gz 资产"
                blob = await _dl(tg)
                if blob is None:
                    return None, tag, "发行包下载失败"
                import tarfile, io
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
                    for m in t.getmembers():
                        if os.path.basename(m.name) == "codeboss-relay":
                            return t.extractfile(m).read(), tag, ""
                return None, tag, "发行包里没有 codeboss-relay"
            py = next((a for a in assets if a.get("name") == "relay.py"), None)
            if py:
                data = await _dl(py)
                return (data, tag, "") if data is not None else (None, tag, "relay.py 资产下载失败")
            tg = next((a for a in assets if a.get("name", "").endswith((".tar.gz", ".tgz"))), None)
            if tg:
                blob = await _dl(tg)
                if blob is None:
                    return None, tag, "发行包下载失败"
                import tarfile, io
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
                    for m in t.getmembers():
                        if os.path.basename(m.name) == "relay.py":
                            return t.extractfile(m).read(), tag, ""
                return None, tag, "发行包里没有 relay.py"
            return None, tag, "release 没有 relay.py 或 tar.gz 资产"
    except asyncio.TimeoutError:
        return None, "", "拉取超时（到 GitHub 的连接太慢），稍后再点一次，或走手动上传兜底"
    except Exception as e:
        return None, "", "拉取异常：%s" % (str(e) or type(e).__name__)[:150]


async def r_update_pull(request):
    """自动更新：优先 GitHub 最新 release（RELAY_GH_REPO），否则 RELAY_UPDATE_URL 直拉。
    内容有变且编译通过才替换重启。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权访问"}, status=401)
    tag = ""
    if GH_REPO:
        if RELAY_VER:   # 版本短路：tag 没变就不下 12MB 的包，国内拉 GitHub 太慢
            _, rtag, err = await _gh_release_fetch(tag_only=True)
            if err:
                return web.json_response({"ok": False, "error": "%s" % err}, status=502)
            if rtag == "relay-v" + RELAY_VER:
                return web.json_response({"ok": True, "changed": False, "tag": rtag})
        data, tag, err = await _gh_release_fetch()
        if data is None:
            return web.json_response({"ok": False, "error": "%s" % err}, status=502)
    elif UPDATE_URL:
        from aiohttp import ClientSession, ClientTimeout
        headers = {"Authorization": "Bearer %s" % UPDATE_TOKEN} if UPDATE_TOKEN else {}
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as s:
                async with s.get(UPDATE_URL, headers=headers) as r:
                    if r.status != 200:
                        return web.json_response({"ok": False, "error": "拉取失败 HTTP %s" % r.status}, status=502)
                    data = await r.read()
        except Exception as e:
            return web.json_response({"ok": False, "error": "拉取异常：%s" % str(e)[:150]}, status=502)
    else:
        return web.json_response(
            {"ok": False, "error": "未配置更新源：RELAY_GH_REPO（release）或 RELAY_UPDATE_URL（直连）"}, status=400)
    if not _looks_like_update(data):
        return web.json_response({"ok": False, "error": "拉到的内容不像 relay.py，拒绝"}, status=400)
    try:
        cur = open(_self_path(), "rb").read()
    except OSError:
        cur = b""
    if data == cur:
        return web.json_response({"ok": True, "changed": False, "bytes": len(data), "tag": tag})
    ok, err = await _apply_update(request.app, data)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    return web.json_response({"ok": True, "changed": True, "bytes": len(data), "tag": tag, "restarted": True})


_PAGE_ADMIN = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>CodeBoss 中继 · 管理后台</title>__CSS__<style>
.wrap{max-width:780px;margin:0 auto}
h1,.card{max-width:none}
.meta{max-width:none;margin-left:0;margin-right:0}
.nav{display:flex;gap:6px;overflow-x:auto;padding:0 0 12px;margin:0 auto 2px;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nv{background:var(--ctl);color:var(--mut);border:1px solid var(--line);padding:7px 12px;
    font-size:13px;white-space:nowrap;font-weight:500;border-radius:999px}
.nv:hover{background:#232C3A;color:var(--txt)}
.nv.on{background:var(--gy);color:#fff;border-color:var(--gy)}
/* 菜单分组：手机横向胶囊不分层（display:contents 把子项摊平），桌面左侧栏可折叠 */
.ngrp{display:contents}
.ngh{display:none}
.pane{display:none}.pane.on{display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:8px}
.stile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stile b{font-size:19px;display:block;line-height:1.3}
.stile span{display:block;color:var(--mut);font-size:12px;margin-top:2px}
.netsv{width:100%;height:132px;display:block;margin:8px 0 6px;background:var(--bg);
  border:1px solid var(--line);border-radius:10px}
.netrow{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
.netbig{font-size:20px;font-weight:600}
.netbig small{font-size:12px;color:var(--mut);font-weight:400;margin-left:4px}
@media(min-width:720px){#t-ov #netCard{grid-column:1/-1}}
.lrow{display:flex;justify-content:space-between;gap:8px;padding:9px 0;
      border-bottom:1px solid #1e2531;align-items:flex-start;cursor:pointer}
.lrow:last-child{border-bottom:0}
.small{font-size:12px;color:var(--mut)}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
     border:1px solid var(--line);color:var(--mut);vertical-align:1px}
.tag.g{color:var(--gyH);border-color:#2ea04355}
.tag.y{color:var(--yel);border-color:#e3b34155}
.tag.r{color:var(--red);border-color:#f8514955}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px;background:#555}
.dot.on{background:var(--gyH)}.dot.off{background:#3b4350}
.dot.warn{background:var(--yel)}.dot.bad{background:var(--red)}
.svcrow{display:flex;align-items:flex-start;gap:10px;padding:11px 0;border-bottom:1px solid #1e2531}
.svcrow:last-child{border-bottom:0}
.svcrow .sbody{flex:1;min-width:0}
.svcrow .sname{font-weight:600;font-size:14px}
.svcrow .sblurb{font-size:12px;color:var(--mut);margin-top:2px;overflow:hidden;
                text-overflow:ellipsis;white-space:nowrap}
.svcrow .serr{font-size:12px;color:#ff9d97;margin-top:2px;overflow:hidden;
              text-overflow:ellipsis;white-space:nowrap}
.svcrow .serr.off{color:var(--mut)}
.svcspark{display:flex;align-items:flex-end;gap:2px;height:24px;width:132px;flex:none;margin-top:2px}
.svcspark i{flex:1;background:#2a3441;border-radius:1px 1px 0 0;height:2px}
.svcspark i.g{background:#2ea04399}.svcspark i.r{background:#f85149cc}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.actions button{padding:7px 10px;font-size:12px}
.det{display:none;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
.det.on{display:block}
input.mini,select.mini{padding:7px 8px;font-size:13px;width:auto;margin:0;border-radius:8px}
select{background:var(--inp);border:1px solid var(--line);border-radius:8px;color:var(--txt);
       padding:9px;font-size:14px;outline:none}
.chips{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.chip{padding:6px 11px;border-radius:999px;background:var(--ctl);border:1px solid var(--line);
      color:var(--mut);font-size:12px;cursor:pointer}
.chip.on{color:#fff;background:var(--gy);border-color:var(--gy)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1d2634;
       border:1px solid var(--line);color:var(--txt);padding:10px 16px;border-radius:10px;
       font-size:14px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90vw;
       z-index:99;text-align:center}
#toast.on{opacity:1}
#toast.err{border-color:var(--red);color:#ffb3ad}
.bar{height:6px;background:var(--gy);border-radius:3px;margin-top:4px;min-width:2px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl td,.tbl th{padding:6px 4px;border-bottom:1px solid #1e2531;text-align:left}
.tbl th{color:var(--mut);font-weight:500}
.tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl.users{min-width:760px}.tbl.devs{min-width:820px}.tbl.orders{min-width:760px}
.tbl tbody tr:hover{background:#161d28}
.tbl td{vertical-align:middle}
.tbl .btnmini{padding:6px 10px;font-size:12px;font-weight:500}
.tbl .detrow{display:none}
.tbl .detrow.on{display:table-row}
.tbl .detrow>td{background:#0f141c;padding:10px 8px}
.tbl td.ac{white-space:nowrap}
.ph2{color:var(--dim);font-size:12px;margin:2px 0 8px}
.grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
input[type=checkbox]{width:auto;margin:0;flex:none;accent-color:var(--gy)}
#gate .tab{flex:1;background:#161d28;border:1px solid #212936;border-radius:8px;padding:8px 0;
           color:var(--mut);font-weight:500;cursor:pointer;font-size:13px}
#gate .tab.on{background:#238636;border-color:#238636;color:#fff}
#gate input{width:100%;margin:0 0 8px}
#t-policy .row{flex-wrap:wrap}
/* 手机（<900px）：底栏导航 + 子导航 chips；宽屏以下样式一律不生效 */
#mtabs,#msub{display:none}
@media (max-width:899px){
  body{padding-bottom:calc(58px + env(safe-area-inset-bottom,0px))}
  #panel .nav{display:none}
  #mtabs{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:60;
         background:#0d1117;border-top:1px solid #1c2430;
         padding:6px 4px calc(6px + env(safe-area-inset-bottom,0px))}
  #mtabs .mtb{flex:1;background:none;border:0;color:var(--mut);font-size:11px;
              padding:6px 0;border-radius:8px;min-height:44px}
  #mtabs .mtb.on{color:var(--txt);background:#161d28}
  #msub{display:flex;gap:6px;overflow-x:auto;padding:0 0 10px;scrollbar-width:none}
  #msub .chip{flex:none}
  #mmore{position:fixed;top:0;left:0;right:0;bottom:0;z-index:70;background:rgba(0,0,0,.55);display:none}
  #mmore.on{display:block}
  #mmore .sheet{position:absolute;left:0;right:0;bottom:0;max-height:70vh;overflow:auto;
    background:#0d1117;border-radius:16px 16px 0 0;padding:14px 14px calc(14px + env(safe-area-inset-bottom,0px))}
  #mmore h4{margin:12px 0 6px;font-size:12px;color:var(--mut);letter-spacing:.4px}
  #mmore .mmbtn{display:block;width:100%;text-align:left;background:#131a24;border:0;color:var(--txt);
    padding:14px;border-radius:10px;margin-bottom:8px;font-size:15px}
  /* 中继自更新的上传/重启按钮手机上藏掉：误点一次停服务，桌面端才有 */
  .pconly{display:none}
  /* 使用详情一排四个筛选框，窄屏必须能折行，否则整页横向滚动 */
  #t-actlog .row{flex-wrap:wrap}
  #t-actlog .row input,#t-actlog .row select{min-width:0}
  /* 表格在窄屏变卡片：td::before 取表头文字当标签，宽屏不受影响 */
  .tblwrap{overflow:visible}
  .tbl{min-width:0 !important;width:100%;border:0;background:none}
  .tbl thead{display:none}
  .tbl,.tbl tbody,.tbl tr,.tbl td{display:block;width:auto}
  .tbl tr{background:#131a24;border:1px solid #1c2430;border-radius:12px;
          padding:6px 12px;margin-bottom:10px}
  .tbl td{border:0;padding:5px 0;display:flex;gap:10px;align-items:baseline;
          justify-content:space-between;text-align:right;word-break:break-all}
  .tbl td::before{content:attr(data-label);flex:none;color:var(--mut);font-size:12px;
                  text-align:left;max-width:45%}
  .tbl tr.hdr{display:none}
  .tbl td[colspan]{display:block;text-align:center}
  .tbl td[colspan]::before{content:''}
  .tbl td.ac{white-space:normal;justify-content:flex-end;flex-wrap:wrap;gap:6px}
  .tbl td.ac .btnmini{min-height:38px;padding:8px 12px}
  .tbl .detrow.on{display:block}
  .tbl .detrow>td{background:none;padding:0}
}
/* 电脑端：宽屏自动切成「左侧导航 + 右侧内容」，列表并排成多列；
   窄屏（手机/平板竖屏）保持单列卡片。纯 CSS 断点，自动适应窗口宽度。 */
@media (min-width:900px){
  body{padding:24px}
  .wrap{max-width:1280px;padding:0}
  h1{font-size:22px;margin:0 0 20px}
  #panel{display:grid;grid-template-columns:200px minmax(0,1fr);gap:24px;align-items:start}
  #panel .nav{grid-column:1;grid-row:1/span 40;flex-direction:column;gap:4px;
              padding:0;margin:0;position:sticky;top:24px}
  #panel .nv{text-align:left;border-radius:10px;padding:10px 14px;font-size:14px}
  #panel .ngrp{display:flex;flex-direction:column;gap:4px}
  #panel .ngrp+.ngrp{margin-top:10px;padding-top:10px;border-top:1px solid #1c2430}
  #panel .ngrp:not(.on) .nv{display:none}
  #panel .ngh{display:flex;align-items:center;justify-content:space-between;width:100%;
              background:none;border:0;border-radius:8px;color:var(--mut);
              font-size:12px;font-weight:600;letter-spacing:.4px;padding:4px 12px 6px;
              cursor:pointer}
  #panel .ngh:hover{color:var(--txt)}
  #panel .ngh i{font-style:normal;font-size:9px;opacity:.7;transition:transform .15s}
  #panel .ngrp.on .ngh{color:var(--txt)}
  #panel .ngrp.hdr .nv{margin-left:8px;width:calc(100% - 8px);box-sizing:border-box}
  #panel .pane{grid-column:2;min-width:0}
  #gate{max-width:560px}
  .stats{grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
  #t-ov.pane.on{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);
                gap:12px;align-items:start}
  #t-ov #ovStats{grid-column:1/-1;margin:0}
  #t-ov>.card{margin:0}
  .tbl{font-size:13px}
  .tbl td,.tbl th{padding:8px 6px}
  .actions button{padding:8px 12px;font-size:13px}
}
</style></head><body>
<div class="wrap">
<h1>管理后台 <span class="badge" id="newBadge" style="display:none"></span>
  <span style="flex:1"></span>
  <button class="gray" style="font-size:12px;padding:7px 10px" onclick="admOut()">退出</button></h1>
<div class="card" id="gate">
  <div class="row" style="gap:6px;margin-bottom:10px">
    <button class="tab on" id="gm-pass" onclick="gateMode('pass')">手机号+密码</button>
    <button class="tab" id="gm-sms" onclick="gateMode('sms')">手机验证码</button>
    <button class="tab" id="gm-tk" onclick="gateMode('tk')">管理令牌</button>
  </div>
  <div id="gp-pass">
    <input id="ap" placeholder="管理员手机号" inputmode="numeric" maxlength="11" autocomplete="username">
    <input id="apw" type="password" placeholder="密码" autocomplete="current-password"
      onkeydown="if(event.key==='Enter')admLoginPass()">
    <button style="width:100%" onclick="admLoginPass()">进入</button>
  </div>
  <div id="gp-sms" style="display:none">
    <input id="as" placeholder="管理员手机号" inputmode="numeric" maxlength="11">
    <div class="row"><input id="asc" placeholder="短信验证码" inputmode="numeric" maxlength="6" autocomplete="one-time-code" style="flex:1;margin:0">
      <button type="button" class="gray" id="asb" style="flex:none" onclick="sendAdmCode('asb','adminlogin')">获取验证码</button></div>
    <button style="width:100%" onclick="admLoginSms()">进入</button>
  </div>
  <div id="gp-tk" style="display:none">
    <div class="row"><input id="tk" type="password" placeholder="管理令牌（RELAY_ADMIN_TOKEN）" style="flex:1;margin:0">
    <button onclick="adm()">进入</button></div>
    <div class="ph2">令牌只保存在本页面的 sessionStorage，关闭页面即失效。</div>
  </div>
  <div class="err" id="ge"></div>
  <div class="ph2">登录后保持 30 天（常打开会自动顺延）；改套餐、删除、改配置等敏感操作每次都要再收一条短信验证码。</div>
</div>
<div id="panel" style="display:none">
<div class="nav" id="nav"></div>
<div id="msub"></div>
<div id="mtabs"></div>
<div id="mmore" onclick="if(event.target===this)mMore(0)"><div class="sheet" id="mmsheet"></div></div>

<div class="pane" id="t-ov">
  <div class="stats" id="ovStats"></div>
  <div class="card" id="netCard" style="margin-top:12px"><div class="meta" style="margin-bottom:6px">出口带宽
      <span class="small" id="netMeta"></span></div>
    <div class="netrow" id="netNow"></div>
    <div id="netBox"></div>
    <div class="small" id="netDays"></div>
  </div>
  <div class="card" style="margin-top:12px"><div class="meta" style="margin-bottom:6px">最近订单</div>
    <div id="ovRecent"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:6px">实时到账提醒</div>
    <div class="small" id="feedState">每 20 秒检查一次；停留在页面上即可收到提示音与系统通知。</div>
    <div class="actions"><button class="gray" onclick="notifyOn()">开启桌面通知</button></div></div>
</div>

<div class="pane" id="t-svc">
  <div class="card">
    <div class="row" style="align-items:center">
      <div class="meta" style="flex:1;margin:0">外部服务健康：真实调用逐笔记录，模型/语音每 15~30 分钟主动探活一次</div>
      <button class="gray" style="flex:none" onclick="openBoard()">打开监控屏</button>
    </div>
    <div class="stats" id="svcStats" style="margin-top:10px"></div>
    <div class="small" id="svcHint" style="margin-top:6px">每 10 秒自动刷新；监控屏给大屏/平板常开用。</div>
  </div>
  <div id="svcList"></div>
</div>

<div class="pane" id="t-p2p">
  <div class="card">
    <div class="meta" style="margin:0 0 8px">直连不经过中继，命中率越高越省 VPS 带宽。口径是「纯直连分钟」：那一分钟中继一个请求都没收到（P2P 连接时心跳/推送仍可能走中继，所以这是下限）。隧道流量只统计经过中继的请求，回包为压缩前原始字节。</div>
    <div class="stats" id="p2pStats"></div>
    <div class="small" id="p2pHint" style="margin-top:6px">近 14 天 · 进入本页时刷新</div>
  </div>
  <div class="card"><div class="tblwrap"><table class="tbl" id="p2pTable"></table></div></div>
</div>

<div class="pane" id="t-users">
  <div class="card"><div class="row"><input id="uq" placeholder="搜用户名 / 邮箱 / 手机号 / uid" style="flex:1;margin:0"
     onkeydown="if(event.key==='Enter')loadUsers()"><button class="gray" onclick="loadUsers()">搜索</button></div>
    <div class="small" id="ucount"></div></div>
  <div id="userList"></div>
</div>

<div class="pane" id="t-devs">
  <div class="card"><div class="row"><input id="dq" placeholder="搜设备名 / ID / 账号" style="flex:1;margin:0"
     onkeydown="if(event.key==='Enter')loadDevs()"><button class="gray" onclick="loadDevs()">搜索</button></div>
    <div class="small" id="dcount"></div></div>
  <div id="devList"></div>
</div>

<div class="pane" id="t-clients">
  <div class="card"><div class="row"><input id="cq" placeholder="搜终端 ID / 机型 / 账号 / uid" style="flex:1;margin:0"
     onkeydown="if(event.key==='Enter')loadClients()"><button class="gray" onclick="loadClients()">搜索</button></div>
    <div class="chips" id="cFilters"></div>
    <div class="small" id="ccount"></div></div>
  <div id="clientList"></div>
</div>

<div class="pane" id="t-actlog">
  <div class="card">
    <div class="row">
      <input id="al_uid" placeholder="用户（uid 或用户名）" style="flex:1;margin:0">
      <input id="al_cid" placeholder="终端 ID" style="flex:1;margin:0">
    </div>
    <div class="row">
      <input id="al_dev" placeholder="开发机（设备名或 ID）" style="flex:1;margin:0">
      <input id="al_act" placeholder="动作（如 发消息）" style="flex:1;margin:0">
    </div>
    <div class="row">
      <input id="al_from" type="date" style="flex:1;margin:0">
      <span class="meta" style="flex:none">至</span>
      <input id="al_to" type="date" style="flex:1;margin:0">
      <select id="al_kind" style="flex:none">
        <option value="">全部</option>
        <option value="act">动手</option>
        <option value="nav">导航</option>
        <option value="read">轮询</option>
      </select>
    </div>
    <div class="row">
      <button style="flex:1" onclick="loadAct()">查询</button>
      <button class="gray" onclick="actReset()">清空筛选</button>
      <select id="al_limit" style="flex:none">
        <option value="300">300 条</option>
        <option value="600" selected>600 条</option>
        <option value="2000">2000 条</option>
      </select>
    </div>
    <div class="small" id="alcount"></div>
  </div>
  <div id="actList"></div>
</div>

<div class="pane" id="t-fb">
  <div class="card"><div class="row"><span class="meta" style="flex:1">问题与建议</span>
    <button class="gray" onclick="loadFb()">刷新</button></div>
    <div class="small" id="fbcount"></div></div>
  <div id="fbList"></div>
</div>

<div class="pane" id="t-orders">
  <div class="card"><div class="chips" id="oFilters"></div>
    <div class="row"><input id="oq" placeholder="搜订单号 / 用户名" style="flex:1;margin:0"
      onkeydown="if(event.key==='Enter')loadOrders()"><button class="gray" onclick="loadOrders()">搜索</button></div>
    <div class="small" id="ocount"></div></div>
  <div id="orderList"></div>
</div>

<div class="pane" id="t-plans">
  <div id="planForm"></div>
</div>

<div class="pane" id="t-usage">
  <div id="usageMeter"></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">近 14 天用量</div><div id="usageSeries"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">今日用量排行</div><div id="usageUsers"></div></div>
</div>

<div class="pane" id="t-analytics">
  <div class="card">
    <div class="row" style="margin-bottom:8px"><b>功能使用</b>
      <span class="small" id="anHint" style="margin-left:auto"></span></div>
    <div class="row">
      <button class="gray" id="an7" onclick="anSet(7)">7 天</button>
      <button class="gray" id="an30" onclick="anSet(30)">30 天</button>
      <button class="gray" id="an90" onclick="anSet(90)">90 天</button>
      <select id="anProd" onchange="anSet()" style="flex:none;margin:0 0 0 auto">
        <option value="">全部产品线</option>
        <option value="codeboss">口袋工头</option>
        <option value="agentboss">代理工头</option>
      </select>
    </div>
    <div class="small" style="margin-top:6px">只记功能使用次数、按钮名和页面停留时长，不记聊天内容、会话标题、文件和路径；没登录的设备计入设备数。</div>
  </div>
  <div class="card"><div class="meta" style="margin-bottom:8px">今日</div><div id="anTop"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">每日活跃</div><div id="anDaily"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">页面停留</div><div id="anPages"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">页面上的动作</div><div id="anDet"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">功能排行</div><div id="anEv"></div></div>
  <div class="card"><div class="meta" style="margin-bottom:8px">产品线 · 版本 · 平台</div><div id="anDim"></div></div>
</div>

<div class="pane" id="t-alerts">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">收款到账 / 新用户注册提醒（可同时开多个渠道）</div>
    <div class="row"><label class="meta" style="flex:1">收付款提醒总开关</label><input type="checkbox" id="al_pay"></div>
    <div class="row"><label class="meta" style="flex:1">新用户注册提醒</label><input type="checkbox" id="al_reg"></div>
    <div class="ph2">邮件（复用中继 SMTP，可填多个邮箱，逗号/换行分隔）</div>
    <textarea id="al_email" rows="2" placeholder="me@example.com&#10;a@example.com" style="width:100%"></textarea>
    <div class="ph2">钉钉群机器人 Webhook（可填多个，一行一个）</div>
    <textarea id="al_ding" rows="2" placeholder="https://oapi.dingtalk.com/robot/send?access_token=…" style="width:100%"></textarea>
    <div class="ph2">企业微信群机器人 Webhook（可经微信插件收到微信，可填多个）</div>
    <textarea id="al_wecom" rows="2" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…" style="width:100%"></textarea>
    <div class="ph2">Server酱（推送到微信，可填多个 SendKey）</div>
    <textarea id="al_sc" rows="2" placeholder="SCT… 开头的 SendKey" style="width:100%"></textarea>
    <div class="ph2">PushPlus（推送到微信，可填多个 token）</div>
    <textarea id="al_pp" rows="2" placeholder="PushPlus token" style="width:100%"></textarea>
    <div class="actions">
      <button onclick="saveAlerts(false)">保存</button>
      <button class="gray" onclick="notifyOn()">开启桌面通知</button>
      <button class="gray" onclick="saveAlerts(true)">保存并发送测试</button>
    </div>
    <div class="small" id="al_state" style="margin-top:8px"></div>
  </div>
</div>

<div class="pane" id="t-store">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">对象存储（阿里云 OSS）——数据库备份、反馈附件、安装包都往这里放；不配置则全部留在本机磁盘</div>
    <div class="row"><label class="meta" style="flex:1">启用对象存储</label><input type="checkbox" id="os_on"></div>
    <div class="ph2">地域节点 Endpoint</div>
    <input id="os_ep" placeholder="oss-cn-hangzhou.aliyuncs.com">
    <div class="ph2">Bucket 名称</div>
    <input id="os_bk" placeholder="my-codeboss">
    <div class="ph2">AccessKey ID</div>
    <input id="os_ak" placeholder="LTAI…" autocomplete="off">
    <div class="ph2">AccessKey Secret（保存后只显示尾 4 位，回传打码值表示不改）</div>
    <input id="os_sk" type="password" autocomplete="new-password" placeholder="填写新的 Secret">
    <div class="ph2">对象前缀（归档目录）</div>
    <input id="os_pf" placeholder="codeboss/">
    <div class="actions">
      <button onclick="osSave(false)">保存</button>
      <button class="gray" onclick="osSave(true)">保存并自检（写/读/删）</button>
    </div>
    <div class="small" id="os_state" style="margin-top:8px"></div>
  </div>
  <div class="card">
    <div class="meta" style="margin-bottom:6px">文件浏览（只读）——备份有没有真的传到 OSS，看这里就知道</div>
    <div class="row">
      <select id="fl_where" onchange="flLoad()" style="flex:1">
        <option value="oss:backup">OSS · 数据库备份</option>
        <option value="oss:dist">OSS · 分发产物</option>
        <option value="oss:feedback">OSS · 反馈附件</option>
        <option value="oss:all">OSS · 当前前缀全部</option>
        <option value="local:backup">本机 · 数据库备份</option>
        <option value="local:dist">本机 · 分发产物</option>
        <option value="local:feedback">本机 · 反馈附件</option>
      </select>
      <button class="gray" style="flex:none" onclick="flLoad()">刷新</button>
    </div>
    <div class="small" id="fl_state" style="margin:6px 0"></div>
    <div id="fl_list"></div>
    <pre class="small" id="fl_preview" style="display:none;max-height:320px;overflow:auto;white-space:pre-wrap;background:#111;padding:8px;border-radius:6px"></pre>
  </div>
</div>

<div class="pane" id="t-models">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">模型服务（阿里云百炼 / DashScope 兼容接口）——语音转写、语音播报、回复概括共用这个 Key</div>
    <div class="ph2">API Key（保存后只显示尾 4 位，回传打码值表示不改）</div>
    <input id="md_key" type="password" autocomplete="new-password" placeholder="sk-…">
    <div class="ph2">语音识别模型（可填多个，一行一个：第一个为主，不可用自动换下一个）</div>
    <textarea id="md_asr" rows="2" placeholder="qwen3-asr-flash" style="width:100%"></textarea>
    <div class="ph2">语音合成模型（可填多个，一行一个）</div>
    <textarea id="md_tts" rows="2" placeholder="qwen3-tts-flash" style="width:100%"></textarea>
    <div class="ph2">语音音色</div>
    <input id="md_voice" placeholder="Cherry">
    <div class="ph2">文本模型（概括回复 / 纠错兜底，可填多个，一行一个）</div>
    <textarea id="md_llm" rows="2" placeholder="qwen-flash" style="width:100%"></textarea>
    <div class="ph2">纠错模型（留空用文本模型列表；也可填多个）</div>
    <textarea id="md_fix" rows="2" placeholder="qwen-flash" style="width:100%"></textarea>
    <div class="ph2">兼容接口地址</div>
    <input id="md_base" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions">
    <div class="actions">
      <button onclick="mdSave(false)">保存并生效</button>
      <button class="gray" onclick="mdSave(true)">保存并测试</button>
    </div>
    <div class="small" id="md_state" style="margin-top:8px"></div>
  </div>
</div>

<div class="pane" id="t-tasks">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">计划任务（中继自己执行，30 秒内生效；失败会走提醒渠道告警）</div>
    <div id="taskList"></div>
    <div class="small" id="tk_state"></div>
    <div class="small" id="tk_hist"></div>
  </div>
  <div class="card">
    <div class="meta" style="margin-bottom:6px">新建任务</div>
    <div class="row"><input id="tk_name" placeholder="任务名（可留空）" style="flex:1;margin:0">
      <select id="tk_kind" style="flex:none"></select></div>
    <div class="row"><label class="meta" style="flex:none">间隔（分钟）</label>
      <input id="tk_every" type="number" min="1" value="360" style="flex:none;width:110px;margin:0"></div>
    <div class="row"><label class="meta" style="flex:none">定时</label>
      <input id="tk_cron" placeholder="crontab 5 段，留空=按间隔；如 0 3 * * * 每天 03:00" style="flex:1;margin:0">
      <button style="flex:1" onclick="tkCreate()">新建</button></div>
    <div class="small" id="tk_new_state" style="margin-top:8px"></div>
  </div>
</div>

<div class="pane" id="t-policy">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">管理员安全（手机号 / 登录密码 / 敏感操作二次验证）</div>
    <div class="row"><label class="meta" style="flex:1">管理员手机号</label>
      <input id="ad_phone" inputmode="numeric" maxlength="11" placeholder="未配置" style="width:160px;margin:0"></div>
    <div class="row"><label class="meta" style="flex:1">新登录密码（留空不改）</label>
      <input id="ad_pass" type="password" autocomplete="new-password" placeholder="至少 6 位" style="width:160px;margin:0"></div>
    <div class="small" id="ad_state" style="margin:6px 0"></div>
    <div class="actions"><button onclick="adSecSave()">保存管理员安全设置</button></div>
  </div>

  <div class="card">
    <div class="meta" style="margin-bottom:6px">注册赠送（新注册自动送套餐，到期自动恢复免费版；改完立即生效，只影响之后的新注册）</div>
    <div class="row"><label class="meta" style="flex:1">开启注册赠送</label>
      <input type="checkbox" id="pr_en"></div>
    <div class="row"><label class="meta" style="flex:none">赠送档位</label>
      <select id="pr_plan" style="flex:none"><option value="pro">Pro</option><option value="max">Max</option></select>
      <label class="meta" style="flex:none;margin-left:12px">天数</label>
      <input id="pr_days" type="number" min="1" max="3650" style="flex:none;width:76px">
      <span class="meta" style="flex:1">到期日=注册日+天数，过期自动回免费版</span></div>
    <div class="row"><label class="meta" style="flex:none">活动截止</label>
      <input id="pr_until" type="datetime-local" style="flex:none;width:200px;margin:0">
      <span class="meta" style="flex:1">到点不再送（留空=长期有效）</span></div>
    <div class="row"><label class="meta" style="flex:1">注册/登录页显示活动横幅</label>
      <input type="checkbox" id="pr_banner"></div>
    <div class="row"><label class="meta" style="flex:none">横幅文案</label>
      <input id="pr_note" style="flex:1;margin:0" placeholder="新用户注册即送 1 个月 Pro，到期自动恢复免费版"></div>
    <div class="small" id="pr_state" style="margin:6px 0"></div>
    <div class="actions"><button onclick="promoSave()">保存注册赠送</button></div>
    <div class="err" id="pr_e"></div>
  </div>

  <div class="card">
    <div class="meta" style="margin-bottom:6px">提醒策略（改完立即生效：开发机让中继算，兜底策略最多 5 分钟拉到）</div>
    <div class="row"><label class="meta" style="flex:1">提醒总开关</label><input type="checkbox" id="p_en"></div>
    <div class="row"><label class="meta" style="flex:1">完成提醒（每一轮回答结束都会触发，建议关，避免太吵）</label>
      <input type="checkbox" id="p_k_done" style="flex:none">
      <select id="p_s_done" style="flex:none"><option value="done">提示音</option><option value="alert">警示音</option><option value="ping">轻提示</option></select>
      <input type="checkbox" id="p_v_done" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:1">需要确认（重要：默认语音播报）</label>
      <input type="checkbox" id="p_k_perm" style="flex:none">
      <select id="p_s_perm" style="flex:none"><option value="alert">警示音</option><option value="done">提示音</option><option value="ping">轻提示</option></select>
      <input type="checkbox" id="p_v_perm" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:1">等待回复（答完后一直没动）</label>
      <input type="checkbox" id="p_k_input" style="flex:none">
      <select id="p_s_input" style="flex:none"><option value="ping">轻提示</option><option value="done">提示音</option><option value="alert">警示音</option></select>
      <input type="checkbox" id="p_v_input" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:1">需要你输入/执行（对话框、命令）</label>
      <input type="checkbox" id="p_k_ask" style="flex:none">
      <select id="p_s_ask" style="flex:none"><option value="ping">轻提示</option><option value="done">提示音</option><option value="alert">警示音</option></select>
      <input type="checkbox" id="p_v_ask" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:1">模型额度用完（429 / 额度、账单）</label>
      <input type="checkbox" id="p_k_limit" style="flex:none">
      <select id="p_s_limit" style="flex:none"><option value="alert">警示音</option><option value="done">提示音</option><option value="ping">轻提示</option></select>
      <input type="checkbox" id="p_v_limit" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:1">可能卡住（跑着但长时间没输出，只报一次）</label>
      <input type="checkbox" id="p_k_stuck" style="flex:none">
      <select id="p_s_stuck" style="flex:none"><option value="ping">轻提示</option><option value="done">提示音</option><option value="alert">警示音</option></select>
      <input type="checkbox" id="p_v_stuck" style="flex:none" title="语音播报"></div>
    <div class="row"><label class="meta" style="flex:none">确认合并窗口(秒)</label><input id="p_pt" type="number" min="0" style="flex:none;width:64px">
      <label class="meta" style="flex:none">等待阈值(秒)</label><input id="p_ia" type="number" min="0" style="flex:none;width:64px">
      <label class="meta" style="flex:none">正文上限(字)</label><input id="p_mt" type="number" min="20" style="flex:none;width:64px"></div>
    <div class="row"><label class="meta" style="flex:none">卡住阈值(分)</label><input id="p_sm" type="number" min="0" style="flex:none;width:64px">
      <span class="meta" style="flex:1">0=关；会话跑着、转录这么久没新增才提醒</span></div>
    <div class="row"><label class="meta" style="flex:none">完成文案</label><input id="p_dt" style="flex:1;margin:0" placeholder="{msg}"></div>
    <div class="row"><label class="meta" style="flex:none">确认文案</label><input id="p_ptext" style="flex:1;margin:0" placeholder="{tool} 需要确认：{brief}"></div>
    <div class="row"><label class="meta" style="flex:none">等待文案</label><input id="p_it" style="flex:1;margin:0" placeholder="等待你的回复"></div>
    <div class="row"><label class="meta" style="flex:none">输入文案</label><input id="p_at" style="flex:1;margin:0" placeholder="{msg}"></div>
    <div class="row"><label class="meta" style="flex:none">额度文案</label><input id="p_lt" style="flex:1;margin:0" placeholder="{msg}"></div>
    <div class="row"><label class="meta" style="flex:none">卡住文案</label><input id="p_st" style="flex:1;margin:0" placeholder="{title} 已 {min} 分钟没有输出，可能卡住了"></div>
    <div class="row"><button style="flex:1" onclick="psave()">保存策略</button></div>
    <div class="err" id="pe"></div>
  </div>
</div>

<div class="pane" id="t-canary">
  <div class="card">
    <div class="row" style="margin-bottom:6px">
      <b>版本推送</b><span class="small" id="cst" style="margin-left:auto"></span>
      <button class="gray btnmini" onclick="loadCanary()">刷新</button>
      <button class="btnmini" onclick="cPromote()">全量发布</button>
    </div>
    <div class="small" style="line-height:1.8">
      上新版本后先只给灰度名单里的对象更新，其它开发机/客户端停在「全员基线」；
      自己机器测好再点「全量发布」推给所有人。<br>
      名单按 <b>开发机</b>（设备 ID）/ <b>终端</b>（客户端 ID）/ <b>用户</b>（uid）匹配：
      在「开发机」「终端」「用户」页勾选后点「加入灰度」。不同对象可以分到不同版本。<br>
      每条规则同时带一个 <b>安卓 code</b>：命中规则的对象连客户端也换成那一版（留空=新建时中继上的最新包）。
    </div>
    <div class="small" id="cpkgs" style="margin-top:6px"></div>
  </div>
  <div class="card">
    <div class="row" style="margin-bottom:6px">
      <b>全员基线</b><span class="small" style="margin-left:auto">没命中灰度规则的对象拿这个版本；留空=最新</span>
    </div>
    <div class="row" style="margin-top:6px">
      <input id="cbVer" placeholder="host 版本" style="flex:1;margin:0">
      <input id="cbApk" placeholder="安卓 code" style="flex:1;margin:0">
      <button style="flex:none" onclick="cBaseline()">保存</button>
    </div>
  </div>
  <div class="card">
    <div class="row" style="margin-bottom:6px">
      <b>iOS TestFlight</b><span class="small" id="tfst" style="margin-left:auto"></span>
    </div>
    <div class="small" style="line-height:1.8">
      每次传完 TestFlight 在这填 build 号：App 里的「发现新版本」才会提示去 TestFlight 更新。<br>
      填 TestFlight 上的 build（Xcode 的 Build 号，不是版本号）。
    </div>
    <div class="row" style="margin-top:6px">
      <input id="tfCode" placeholder="build，如 27" style="flex:1;margin:0">
      <input id="tfVer" placeholder="版本，如 0.2.24" style="flex:1;margin:0">
      <button style="flex:none" onclick="cTfSave()">登记</button>
    </div>
  </div>
  <div class="card">
    <div class="row" style="margin-bottom:6px">
      <span class="small" style="flex:none">灰度规则</span>
      <select id="cRule" style="flex:1;margin:0" onchange="cRender()"></select>
      <button class="gray btnmini" onclick="cNewRule()">新建</button>
      <button class="gray btnmini" style="color:#ff7b72" onclick="cDelRule()">删除</button>
    </div>
    <div class="row" style="margin-top:6px">
      <span class="small" style="flex:none">这条规则的安卓 code</span>
      <input id="cRuleApk" placeholder="0=最新" style="flex:1;margin:0">
      <button class="gray btnmini" onclick="cRuleApkSave()">保存</button>
    </div>
    <div id="cMembers" class="small" style="line-height:2"></div>
  </div>
</div>

<div class="pane" id="t-upd">
  <div class="card">
    <div class="meta" style="margin-bottom:6px">当前程序：<span id="st"></span></div>
    <div class="meta">更新源：<span id="usrc"></span></div>
  </div>
  <div class="card pconly">
    <div class="meta" style="margin-bottom:6px">手动上传更新（选 __FMT__ 或 release 压缩包 → 上传，压缩包自动解压，校验通过自动重启生效）</div>
    <div class="row"><input id="uf" type="file" style="flex:1;margin:0">
      <button style="flex:none" onclick="upl()">上传并更新</button></div>
  </div>
  <div class="card pconly">
    <div class="meta" style="margin-bottom:6px">自动更新（从更新源拉取，内容有变才更新）</div>
    <div class="row"><button style="flex:1" onclick="pull()">检查并更新</button></div>
  </div>
  <div class="card">
    <div class="meta" style="margin-bottom:6px">试发邮件 / 短信（用运行中进程的环境变量，直接看真实错误）</div>
    <div class="row"><input id="mt" placeholder="收件邮箱" style="flex:1;margin:0"><button class="gray" onclick="mailtest()">试发邮件</button></div>
    <div class="row"><input id="mtel" placeholder="收短信手机号" style="flex:1;margin:0"><button class="gray" onclick="smstest()">试发短信</button></div>
    <div class="small" id="mres"></div>
  </div>
  <div class="err" id="ae"></div>
  <div class="meta">更新成功后服务原地重启，页面 5 秒后自动刷新重连。</div>
</div>

<div class="pane" id="t-dl">
  <div class="card">
    <div class="row" style="margin-bottom:6px"><b>管理后台 App</b>
      <span class="small" id="dlst" style="margin-left:auto"></span></div>
    <div class="small" style="line-height:1.8">
      安卓：点下面下载安装（管理员自用，不对外发、不上官网）。<br>
      iOS：TestFlight「内部测试」组里装「Codeboss 管理」，没有公开链接。
    </div>
    <div class="row" style="margin-top:8px">
      <button style="flex:1" onclick="apkGo()">下载安卓管理 App</button>
    </div>
  </div>
  <div class="card pconly">
    <div class="meta" style="margin-bottom:6px">上架新版管理 App（选 android-admin/build/codeboss-admin.apk）</div>
    <div class="row"><input id="dlf" type="file" accept=".apk" style="flex:1;margin:0">
      <button style="flex:none" onclick="dlUp()">上传</button></div>
    <div class="small" id="dlm"></div>
  </div>
  <div class="card">
    <div class="meta" style="margin-bottom:6px">客户端 / 开发机安装包</div>
    <div class="small" style="line-height:2">
      <a href="/codeboss.apk">安卓客户端 APK</a><br>
      <a href="/codeboss.ipa">iOS 安装包</a><br>
      <a href="/bosshost-mac.dmg">Mac DMG</a><br>
      <a href="/BossHost-Setup.exe">Windows 安装包</a><br>
      <a href="/codeboss-host.tar.gz">开发机更新包</a><br>
      <a href="/codeboss-linux.tar.gz">Linux 包</a>
    </div>
  </div>
</div>

</div>
</div>
<div id="toast"></div>
<div id="smsdlg" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:90;background:rgba(0,0,0,.6)">
 <div class="card" style="position:absolute;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));margin:0;max-width:520px;margin-left:auto;margin-right:auto">
  <div class="meta" id="smsmsg" style="margin-bottom:8px"></div>
  <input id="smscode" placeholder="短信验证码" inputmode="numeric" pattern="[0-9]*"
    autocomplete="one-time-code" maxlength="6" style="width:100%;font-size:16px">
  <div class="row" style="margin-top:8px">
    <button class="gray" style="flex:1" onclick="smsDlgDone('')">取消</button>
    <button class="gray" style="flex:1" onclick="smsResend()">重发</button>
    <button style="flex:2" onclick="smsDlgDone($('smscode').value)">确认</button>
  </div>
 </div>
</div>
<div id="logv" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:85;background:#0d1117">
 <div style="display:flex;align-items:center;gap:8px;padding:calc(8px + env(safe-area-inset-top,0px)) 12px 8px;border-bottom:1px solid #1c2430">
  <b id="logvt" style="flex:1;font-size:14px"></b>
  <button class="gray btnmini" onclick="logvCopy()">复制</button>
  <button class="gray btnmini" onclick="document.getElementById('logv').style.display='none'">关闭</button>
 </div>
 <pre id="logvpre" style="margin:0;padding:12px;white-space:pre-wrap;word-break:break-all;font:12px/1.55 ui-monospace,Menlo,monospace;overflow:auto;height:calc(100% - 54px)"></pre>
</div>
<script>
const FMT='__FMT__';
/* 菜单分组（桌面左侧栏按组折叠，手机横向胶囊仍摊平）。空组名 = 不显示组头。 */
const GROUPS=[['',[['ov','概览']]],
  ['用户与订单',[['users','用户'],['devs','开发机'],['clients','终端'],['actlog','使用详情'],
                ['orders','订单'],['plans','套餐'],['usage','用量']]],
  ['监控',[['svc','服务监控'],['p2p','直连/流量']]],
  ['运营',[['analytics','分析'],['fb','反馈'],['alerts','提醒']]],
  ['发布',[['canary','版本'],['upd','更新'],['policy','策略']]],
  ['系统',[['dl','下载'],['store','存储'],['models','模型'],['tasks','任务']]]];
const NAV=GROUPS.reduce(function(a,g){return a.concat(g[1]);},[]);
let CT='ov', USERS=[], DEVS=[], ORDERS=[], FBS=[], OST='all', PLANS=null, SKUS=null;
let feedTimer=null, svcTimer=null, lastPaid=0, unread=0, BOOT=null;
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function money(v){return '¥'+Number(v||0).toFixed(2);}
function dur(sec){sec=Number(sec||0);var m=Math.floor(sec/60),h=Math.floor(m/60);
  if(h>0)return h+' 小时'+(m%60?(' '+(m%60)+' 分'):'');
  if(m>0)return m+' 分钟';
  return sec+' 秒';}
function fmtDate(ts){if(!ts)return '—';var d=new Date(ts*1000);
  return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);}
function ago(ts){if(!ts)return '—';var s=Date.now()/1000-ts;
  if(s<0)return '刚刚';
  if(s<60)return '刚刚';
  if(s<3600)return Math.floor(s/60)+' 分钟前';
  if(s<86400)return Math.floor(s/3600)+' 小时前';
  var d=new Date(ts*1000);
  return (d.getMonth()+1)+'-'+d.getDate()+' '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
const tkget=()=>sessionStorage.getItem('cb_admin')||'';
function H(){return {Authorization:'Bearer '+tkget()};}
/* 管理二次验证码的本地复用窗口（服务端 ADM_CODE_REUSE 同口径）。
   一次验证后几分钟内做别的敏感操作不再发新短信——阿里云同号短信有 60 秒频控，
   连着删几台设备会被卡在「发送太频繁」，然后只看到一句「手机验证未完成」。 */
const ADM_CODE_TTL=300;
var ADM_CODE=null,STEP=null;
function admCodeGet(){
  if(ADM_CODE&&ADM_CODE.exp>Date.now())return ADM_CODE.code;
  ADM_CODE=null;return '';}
function admCodeSet(c){if(c)ADM_CODE={code:String(c),exp:Date.now()+ADM_CODE_TTL*1000};}
function admCodeClear(){ADM_CODE=null;}
function withAdmCode(opt,code){
  return Object.assign({},opt,{headers:Object.assign({},opt.headers,{'X-Adm-Code':code})});}
function api(path,opt,retry){opt=opt||{};
  opt.headers=Object.assign({'Content-Type':'application/json'},H(),opt.headers||{});
  return fetch(path,opt).then(function(r){
    if(r.status===401){
      var vis=$('panel').style.display!=='none';
      showGate(vis?'登录已失效，请重新登录':'');
      throw new Error('401');
    }
    return r.json();
  }).then(function(d){
    if(!(d&&d.need_sms)||retry)return d;
    return adminStepUp(d).then(function(st){
      if(!st.code)return {ok:false,need_sms:true,
        error:st.error||'手机验证未完成，操作已取消'};
      return api(path,withAdmCode(opt,st.code),true).then(function(d2){
        if(d2&&d2.ok){admCodeSet(st.code);return d2;}
        if(!(d2&&d2.need_sms))return d2;      // 业务错误原样交给调用方
        admCodeClear();
        if(st.cached)return api(path,opt,false);  // 窗口内的旧码失效 → 正常发新码
        return {ok:false,need_sms:true,error:'验证码不正确或已过期，请再操作一次'};
      });
    });
  });}
/* 先用手上还没过期的码（不弹窗、不发短信），没有再发一条新码并弹窗 */
function adminStepUp(d){
  var hit=admCodeGet();
  if(hit)return Promise.resolve({code:hit,cached:true});
  if(STEP)return STEP;
  var phone=(d&&d.phone)||'';
  if(!phone)return Promise.resolve({code:'',error:'请先在「策略 → 管理员安全」里配置管理员手机号'});
  STEP=api('/r/admin/sms',{method:'POST',body:JSON.stringify({purpose:'adminop'})},true).then(function(r){
    if(!r.ok){STEP=null;return {code:'',error:r.error||'验证码发送失败'};}
    return smsPrompt(phone).then(function(st){STEP=null;return st;});
  }).catch(function(){STEP=null;return {code:'',error:'验证码请求失败，稍后再试'};});
  return STEP;
}
/* 验证码只留数字、去重、最多 6 位。系统 OTP 自动填充偶尔会把同一个码写两遍（123456123456），
   这里按「前后两段完全相同」折回 6 位；用户手删时不会被吃掉。 */
function otpClean(v){
  v=String(v==null?'':v).replace(/\D/g,'');
  if(v.length>6&&v.slice(0,6)===v.slice(6,12))v=v.slice(0,6);
  return v.slice(0,6);
}
document.addEventListener('input',function(e){
  if(e.target&&e.target.id==='smscode'){
    var c=otpClean(e.target.value);
    if(c!==e.target.value)e.target.value=c;
  }
},true);
var SMSWAIT=null;
function smsPrompt(phone){
  return new Promise(function(res){
    SMSWAIT=res;
    $('smsmsg').textContent='该操作需要手机二次验证，验证码已发送至 '+phone+
      '（验证一次后 1 小时内其它操作不用再验证，删除/移除类除外）';
    $('smscode').value='';$('smsdlg').style.display='block';
    setTimeout(function(){try{$('smscode').focus();}catch(e){}},120);
  });
}
/* 总是先关弹窗再交回结果：取消/确认/超时都只回调一次，不会叠出第二个弹窗 */
function smsDlgDone(v){
  var r=SMSWAIT;SMSWAIT=null;
  $('smsdlg').style.display='none';
  if(r)r({code:otpClean(v)});
}
function smsResend(){
  api('/r/admin/sms',{method:'POST',body:JSON.stringify({purpose:'adminop'})},true)
    .then(function(d){toast(d&&d.ok?'验证码已重新发送':((d&&d.error)||'发送失败'),!(d&&d.ok));});
}
function sendAdmCode(id,purpose){
  var b=$(id);if(!b||b.disabled)return;
  b.disabled=true;
  api('/r/admin/sms',{method:'POST',body:JSON.stringify({purpose:purpose||'adminlogin'})},true)
    .then(function(d){
      toast(d.ok?(d.msg||'验证码已发送'):(d.error||'发送失败'),!d.ok);
      if(!d.ok){b.disabled=false;return;}
      var n=10;b.textContent=n+'s';
      var t=setInterval(function(){n--;b.textContent=n>0?(n+'s'):'获取验证码';
        if(n<=0){clearInterval(t);b.disabled=false;}},1000);
    });
}
function gateMode(m){
  ['pass','sms','tk'].forEach(function(k){
    $('gm-'+k).className='tab'+(k===m?' on':'');
    $('gp-'+k).style.display=(k===m?'':'none');
  });
}
async function admLoginPass(){
  var p=($('ap').value||'').trim(),w=$('apw').value||'';
  if(!p||!w){$('ge').textContent='请输入管理员手机号和密码';return;}
  $('ge').textContent='';
  var d=await api('/r/admin/login',{method:'POST',body:JSON.stringify({mode:'pass',phone:p,password:w})},true);
  if(!d.ok){$('ge').textContent=d.error||'登录失败';return;}
  sessionStorage.removeItem('cb_admin');boot();
}
async function admLoginSms(){
  var p=($('as').value||'').trim(),c=($('asc').value||'').trim();
  if(!p||!c){$('ge').textContent='请输入管理员手机号和验证码';return;}
  $('ge').textContent='';
  var d=await api('/r/admin/login',{method:'POST',body:JSON.stringify({mode:'sms',phone:p,code:c})},true);
  if(!d.ok){$('ge').textContent=d.error||'登录失败';return;}
  sessionStorage.removeItem('cb_admin');boot();
}
var TT=null;
function toast(m,err){var t=$('toast');t.textContent=m;t.className='on'+(err?' err':'');
  clearTimeout(TT);TT=setTimeout(function(){t.className='';},2800);}
function beep(){try{var A=window.AudioContext||window.webkitAudioContext;if(!A)return;
  var a=new A(),t=a.currentTime;
  [[880,0,.1],[1320,.13,.14]].forEach(function(x){
    var o=a.createOscillator(),g=a.createGain();
    o.type='sine';o.frequency.value=x[0];
    g.gain.setValueAtTime(.0001,t+x[1]);
    g.gain.exponentialRampToValueAtTime(.25,t+x[1]+.02);
    g.gain.exponentialRampToValueAtTime(.0001,t+x[1]+x[2]);
    o.connect(g);g.connect(a.destination);o.start(t+x[1]);o.stop(t+x[1]+x[2]+.02);});
  setTimeout(function(){try{a.close();}catch(e){}},900);}catch(e){}}
function popNotify(title,body){try{
  if(window.Notification&&Notification.permission==='granted'){new Notification(title,{body:body});}
}catch(e){}}
function notifyOn(){try{
  if(window.Notification&&Notification.permission!=='granted'){
    Notification.requestPermission().then(function(p){
      toast(p==='granted'?'桌面通知已开启':'未授权桌面通知',p!=='granted');});
  }else{toast('桌面通知已开启');}
}catch(e){toast('当前浏览器不支持桌面通知',true);}}
function showGate(m){$('ge').textContent=m||'';$('gate').style.display='block';
  $('panel').style.display='none';}
let NOpen=JSON.parse(sessionStorage.getItem('cb_nav_open')||'{"1":true}');  // 默认展开「用户与订单」
function navToggle(i){NOpen[i]=!NOpen[i];
  sessionStorage.setItem('cb_nav_open',JSON.stringify(NOpen));navRender();}
function navRender(){
  $('nav').innerHTML=GROUPS.map(function(g,i){
    var on=!g[0]||!!NOpen[i];   // 无组头（概览）常显
    return '<div class="ngrp'+(on?' on':'')+(g[0]?' hdr':'')+'">'+
      (g[0]?'<button class="ngh" onclick="navToggle('+i+')">'+g[0]+'<i>'+(on?'▾':'▸')+'</i></button>':'')+
      g[1].map(function(n){
        return '<button class="nv'+(n[0]===CT?' on':'')+'" onclick="go(\''+n[0]+'\')">'+n[1]+'</button>';
      }).join('')+'</div>';
  }).join('');}
const MTABS=[['ov','概览',['ov']],
  ['users','用户',['users','devs','clients','actlog','orders','plans','usage']],
  ['pub','发布',['canary','upd','policy','dl']],
  ['mon','监控',['svc','p2p','alerts']],
  ['more','更多',null]];
function mTabOf(t){var k='more';MTABS.forEach(function(m){if(m[2]&&m[2].indexOf(t)>=0)k=m[0];});return k;}
function mTabSync(){
  $('mtabs').innerHTML=MTABS.map(function(m){var on=m[0]===mTabOf(CT);
    return '<button class="mtb'+(on?' on':'')+'" onclick="mTabGo(\''+m[0]+'\')">'+m[1]+'</button>';}).join('');
  var g=MTABS.filter(function(m){return m[0]===mTabOf(CT);})[0];
  var sub=(g&&g[2])||[];
  $('msub').innerHTML=sub.length>1?sub.map(function(t){
    return '<span class="chip'+(t===CT?' on':'')+'" onclick="go(\''+t+'\')">'+
      esc((NAV.filter(function(n){return n[0]===t;})[0]||[t,t])[1])+'</span>';}).join(''):'';
}
function tagCells(root){
  var ts=(root||document).querySelectorAll('table.tbl');
  for(var i=0;i<ts.length;i++){
    var tb=ts[i], hs=[].map.call(tb.querySelectorAll('thead th'),function(th){return (th.textContent||'').trim();});
    if(!hs.length){          /* 用量/直连这类表头是裸 <tr><th>：拿第一行 th 当列名，窄屏隐藏这行 */
      var hr=tb.querySelector('tr');
      while(hr&&!hr.querySelector('th'))hr=hr.nextElementSibling;
      if(hr){hs=[].map.call(hr.children,function(th){return (th.textContent||'').trim();});
        if(hr.className.indexOf('hdr')<0)hr.className=(hr.className?hr.className+' ':'')+'hdr';}
    }
    if(!hs.length)continue;
    var trs=tb.querySelectorAll('tbody tr');
    for(var r=0;r<trs.length;r++){var tds=trs[r].children;
      for(var c=0;c<tds.length;c++){if(tds[c].tagName==='TD'&&!tds[c].getAttribute('data-label'))
        tds[c].setAttribute('data-label',hs[c]||'');}}
  }
}
var TAGQ=0;
function tagSoon(){
  if(TAGQ)return;
  TAGQ=setTimeout(function(){TAGQ=0;tagCells(document.getElementById('panel'));},80);
}
new MutationObserver(tagSoon).observe(document.body,{childList:true,subtree:true});
function mTabGo(k){
  if(k==='more'){mMore(1);return;}
  var g=MTABS.filter(function(m){return m[0]===k;})[0];
  go((g&&g[2]&&g[2][0])||'ov');}
function mMore(on){
  if(!on){$('mmore').className='';return;}
  $('mmsheet').innerHTML=GROUPS.map(function(g){
    return '<h4>'+esc(g[0]||'概览')+'</h4>'+g[1].map(function(n){
      return '<button class="mmbtn" onclick="mMore(0);go(\''+n[0]+'\')">'+n[1]+'</button>';}).join('');
  }).join('')+'<button class="mmbtn" style="text-align:center;background:#21262d" onclick="mMore(0)">关闭</button>';
  $('mmore').className='on';}
function go(t){CT=t;
  mTabSync();
  /* 只在这里展开目标页所在的组：放在 navRender 里会导致当前组永远收不起来 */
  GROUPS.forEach(function(g,i){g[1].forEach(function(n){
    if(n[0]===t)NOpen[i]=true;});});
  sessionStorage.setItem('cb_nav_open',JSON.stringify(NOpen));
  NAV.forEach(function(n){var e=$('t-'+n[0]);if(e)e.className='pane'+(n[0]===t?' on':'');});
  navRender();
  if(svcTimer){clearInterval(svcTimer);svcTimer=null;}
  if(t==='svc'){loadSvc();svcTimer=setInterval(loadSvc,10000);return;}
  (t==='ov'?loadOv:t==='p2p'?loadP2p:t==='users'?loadUsers:t==='devs'?loadDevs:t==='clients'?loadClients:
   t==='actlog'?loadAct:t==='orders'?loadOrders:t==='plans'?loadPlans:t==='usage'?loadUsage:
   t==='analytics'?loadAn:t==='alerts'?loadAlerts:t==='fb'?loadFb:t==='store'?function(){loadStore();flLoad();}:t==='models'?loadModels:
   t==='tasks'?loadTasks:t==='policy'?polLoad:t==='canary'?loadCanary:
   t==='dl'?loadDl:function(){})();}
var APKURL='';
function apkGo(){location.href=APKURL||('/r/admin/app.apk'+(TOK?('?t='+encodeURIComponent(TOK)):''));}
function loadDl(){
  fetch('/r/admin/app.apk',{method:'HEAD',headers:H()}).then(function(r){
    $('dlst').textContent=r.ok?'可下载':(r.status===401?'登录后可下载':'中继上还没有，先在电脑上传');
  }).catch(function(){});
  /* 管理壳点下载会交给系统浏览器（没有登录 cookie）：换成 10 分钟有效的签名直链 */
  fetch('/r/admin/apk_ticket',{headers:H()}).then(function(r){return r.json();}).then(function(d){
    if(d&&d.ok&&d.url)APKURL=d.url;
  }).catch(function(){});
}
async function dlUp(){
  var f=$('dlf').files[0];if(!f){$('dlm').textContent='先选 APK';return;}
  $('dlm').textContent='上传中…';
  try{
    var r=await fetch('/r/admin/adminapk',{method:'POST',headers:H(),body:f});
    var d=await r.json();
    $('dlm').textContent=d.ok?('已上架（'+Math.round(d.size/1024)+' KB）'):(d.error||'上传失败');
    if(d.ok)loadDl();
  }catch(e){$('dlm').textContent='上传失败：'+e.message;}
}
function adm(){sessionStorage.setItem('cb_admin',$('tk').value.trim());boot();}
function admOut(){sessionStorage.removeItem('cb_admin');
  fetch('/r/admin/logout',{method:'POST'}).catch(function(){}).then(function(){location.reload();});}
function boot(){
  api('/r/admin_info').then(function(d){
    if(!d.ok){showGate(d.error||'令牌无效');return;}
    $('ge').textContent='';$('gate').style.display='none';$('panel').style.display='';
    BOOT=d;updStat(d);navRender();go('ov');feedInit();
    if(feedTimer)clearInterval(feedTimer);
    feedTimer=setInterval(feedTick,20000);
  }).catch(function(){});
}
function updStat(d){if(!d)return;
  $('st').textContent=(d.ver?('v'+d.ver+' · '):'')+(d.bytes/1024).toFixed(1)+' KB · '+
    new Date(d.mtime*1000).toLocaleString();
  $('usrc').textContent=d.source==='release'?('GitHub Release（'+d.repo+'）'):
    (d.source==='url'?'直连 URL（RELAY_UPDATE_URL）':'未配置（服务端设 RELAY_GH_REPO 或 RELAY_UPDATE_URL）');}
function tile(l,v,s){return '<div class="stile"><b>'+esc(v)+'</b><span>'+esc(l)+
  (s?' · '+esc(s):'')+'</span></div>';}
function netKB(v){v=Number(v||0);return v>=1024?((v/1024).toFixed(2)+' MB/s'):(Math.round(v)+' KB/s');}
function netTime(ts){var d=new Date(ts*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
function netSvg(series,alertK){
  if(!series||!series.length)
    return '<div class="small" style="padding:18px 6px">采样刚启动，大约一分钟出第一个点。</div>';
  var W=600,H=132,P=6,t1=Math.floor(Date.now()/1000),t0=t1-24*3600;
  var dmax=0;series.forEach(function(p){dmax=Math.max(dmax,p[1],p[2]);});
  var alert=Number(alertK||0);
  var showTh=alert>0&&alert<=dmax*1.5;    // 告警线离数据太远就不画，否则平时的曲线被压成一条直线
  var mx=Math.max(dmax*1.15,showTh?alert*1.1:0,4);
  function X(t){return (P+(W-2*P)*(t-t0)/(t1-t0)).toFixed(1);}
  function Y(v){return (H-P-(H-2*P)*Math.min(v,mx)/mx).toFixed(1);}
  var pu=[],pd=[],bu=[],bd=[],last=0;
  function flush(){if(bu.length){pu.push('M'+bu.join('L'));pd.push('M'+bd.join('L'));bu=[];bd=[];}}
  series.forEach(function(p){
    if(last&&p[0]-last>900)flush();          // 中继重启/停机 > 15 分钟：断开不连线
    bu.push(X(p[0])+' '+Y(p[1]));bd.push(X(p[0])+' '+Y(p[2]));last=p[0];});
  flush();
  var th=showTh?('<line x1="'+P+'" x2="'+(W-P)+'" y1="'+Y(alert)+'" y2="'+Y(alert)+
    '" stroke="#F85149" stroke-dasharray="5 4" opacity=".5"/>'):'';
  return '<svg class="netsv" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+th+
    '<path d="'+pu.join('')+'" fill="none" stroke="#3FB950" stroke-width="1.6" vector-effect="non-scaling-stroke"/>'+
    '<path d="'+pd.join('')+'" fill="none" stroke="#58A6FF" stroke-width="1.6" vector-effect="non-scaling-stroke" opacity=".75"/>'+
    '</svg>';
}
async function loadNet(){
  try{var d=await api('/r/admin_api/net');if(!d.ok)return;
    var c=d.cur||{},live=(Date.now()/1000-(c.ts||0))<600;
    var cap=Number(d.cap_kbs||0),pct=cap?Math.min(999,Math.round(100*(c.up||0)/cap)):0;
    var dmax=0;(d.series||[]).forEach(function(p){dmax=Math.max(dmax,p[1],p[2]);});
    var showTh=d.alert_kbs>0&&d.alert_kbs<=dmax*1.5;
    $('netMeta').textContent='每 '+(d.sample_sec||60)+' 秒采样 · 线路 '+(d.max_mbps||0)+' Mbps'+
      (d.alert_kbs?(' · 告警线 '+netKB(d.alert_kbs)+(showTh?'（红虚线）':'')):'');
    $('netNow').innerHTML=
      '<div class="netbig" style="color:var(--gyH)">↑ '+(live?netKB(c.up):'采样中…')+'<small>出口</small></div>'+
      '<div class="netbig" style="color:var(--blue)">↓ '+(live?netKB(c.dn):'采样中…')+'<small>入口</small></div>'+
      (cap?('<div class="small">占线路 '+pct+'%'+(pct>=80?' <b style="color:var(--red)">接近打满</b>':'')+'</div>'):'')+
      '<div class="small">今日 出 '+((d.today||{}).up_mb||0)+' MB · 入 '+((d.today||{}).down_mb||0)+' MB</div>'+
      (d.peak?('<div class="small">24h 峰值 ↑ '+netKB(d.peak.up)+' · '+netTime(d.peak.at)+
        (d.peak_down?(' / ↓ '+netKB(d.peak_down.dn)+' · '+netTime(d.peak_down.at)):'')+'</div>'):'');
    $('netBox').innerHTML=netSvg(d.series,d.alert_kbs);
    var ds=(d.days||[]).slice().reverse();
    $('netDays').innerHTML=ds.length?('近 '+ds.length+' 天：'+ds.map(function(x){
      return '<span style="white-space:nowrap">'+esc(x.date.slice(5))+' ↑'+x.up_mb+' / ↓'+x.down_mb+' MB</span>';
    }).join(' · ')):'';
  }catch(e){}
}
/* ---------- 直连/流量 ---------- */
async function loadP2p(){
  var d=await api('/r/admin_api/direct');if(!d.ok)return;
  var t=d.today||{},tot=d.totals||{};
  function mb(b){return ((b||0)/1048576).toFixed(1);}
  $('p2pStats').innerHTML=
    tile('今日直连命中率',t.all_min?(t.rate+'%'):'—',
         '直连 '+t.direct_min+' / 中继 '+t.relay_min+' 分钟')+
    tile('今日隧道流量','↑'+mb(t.bytes_in)+' / ↓'+mb(t.bytes_out)+' MB',
         (t.req_n||0)+' 个请求')+
    tile('近 14 天命中率',tot.all_min?((100*tot.direct_min/tot.all_min).toFixed(1)+'%'):'—',
         '直连 '+tot.direct_min+' / 共 '+tot.all_min+' 分钟')+
    tile('近 14 天隧道','↑'+mb(tot.bytes_in)+' / ↓'+mb(tot.bytes_out)+' MB',
         (tot.req_n||0)+' 个请求');
  var ds=(d.days||[]).slice().reverse();
  var h='<tr><th>日期</th><th>直连/总分钟</th><th>命中率</th><th>隧道 ↑/↓</th></tr>';
  h+=ds.length?ds.map(function(x){
    return '<tr><td>'+esc(x.date.slice(5))+'</td><td>'+x.direct_min+' / '+x.all_min+
      '</td><td>'+(x.all_min?x.rate+'%':'—')+'</td><td>↑'+mb(x.bytes_in)+' / ↓'+
      mb(x.bytes_out)+' MB</td></tr>';
  }).join(''):'<tr><td colspan="4" class="small">还没有数据</td></tr>';
  $('p2pTable').innerHTML=h;
}
async function loadOv(){
  var d=await api('/r/admin_api/overview');if(!d.ok)return;
  var u=d.users,dev=d.devices,p=d.pay,us=d.usage;
  $('ovStats').innerHTML=
    tile('用户',u.total,'付费 '+u.paid+' · 今日新增 '+u.new_today)+
    tile('开发机',dev.total,'在线 '+dev.online+' · 绑定 '+dev.bound+' · 冻结 '+dev.frozen)+
    tile('今日收入',money(p.today_sum),p.today_n+' 笔')+
    tile('本月收入',money(p.month_sum),p.month_n+' 笔 · 累计 '+money(p.all_sum))+
    tile('待支付订单',p.pending,'')+
    tile('今日远程时长',dur(us.use_sec||us.use_min*60),'语音 '+dur(us.asr_sec)+' · 模型 '+us.llm+' 次')+
    tile('时长包余额',us.use_credit_min?(us.use_credit_min+' 分钟'):'—','全站合计')+
    tile('计费刻度',(d.meter||3)+' 秒/单位','计时单位时长 · 在「用量」或「套餐」里改')+
    tile('外部服务',(d.svc&&(d.svc.fail||d.svc.warn))?((d.svc.fail?d.svc.fail+' 故障':'')+
      (d.svc.fail&&d.svc.warn?' · ':'')+(d.svc.warn?d.svc.warn+' 异常':'')):'正常',
      (d.svc&&d.svc.bad&&d.svc.bad.length)?d.svc.bad.join('、'):'点「服务监控」看详情');
  $('ovRecent').innerHTML=(d.recent||[]).map(function(o){
    return '<div class="lrow" style="cursor:default"><div><b>'+money(o.money)+'</b> '+esc(o.sku_name||o.sku)+
      '<div class="small">'+esc(o.user)+' · '+esc(o.otn)+'</div></div>'+
      '<div style="text-align:right"><span class="tag'+(o.status===1?' g':(o.status<0?' r':''))+'">'+
      (o.status===1?'已付':(o.status<0?'失败':'待付'))+'</span><div class="small">'+ago(o.created)+
      '</div></div></div>';
  }).join('')||'<div class="small">暂无订单</div>';
  loadNet();
}
async function feedInit(){
  try{var d=await api('/r/admin_api/feed?after='+(Date.now()/1000-120));
    lastPaid=Number(d.now||Date.now()/1000);
    $('feedState').textContent='每 20 秒检查一次；上次检查 '+new Date().toLocaleTimeString();
  }catch(e){lastPaid=Date.now()/1000;}
}
async function feedTick(){
  if(document.hidden)return;
  try{var d=await api('/r/admin_api/feed?after='+lastPaid);
    var os=d.orders||[];lastPaid=Number(d.now||lastPaid);
    $('feedState').textContent='每 20 秒检查一次；上次检查 '+new Date().toLocaleTimeString();
    if(CT==='ov')loadNet();
    if(os.length){os.forEach(function(o){
      toast('💳 到账 '+money(o.money)+' · '+o.user+' · '+o.sku_name);
      popNotify('到账 '+money(o.money),o.user+' 购买了 '+o.sku_name);});
      beep();unread+=os.length;
      var b=$('newBadge');b.style.display='';b.textContent=String(unread);
      if(CT==='ov')loadOv();if(CT==='orders')loadOrders();}
  }catch(e){}
}
/* ---------- 服务监控 ---------- */
var SVCN={ok:'正常',warn:'有异常',fail:'故障',unknown:'待观察',off:'未配置'};
function svcAgo(ts){
  if(!ts)return '—';var d=Math.max(0,Date.now()/1000-ts);
  if(d<60)return Math.round(d)+' 秒前';
  if(d<3600)return Math.round(d/60)+' 分钟前';
  if(d<86400)return (d/3600).toFixed(1)+' 小时前';
  return Math.round(d/86400)+' 天前';
}
function svcMs(v){v=Number(v||0);return v>=1000?(v/1000).toFixed(1)+' s':Math.round(v)+' ms';}
function svcOrder(s){return {fail:0,warn:1,unknown:2,ok:3,off:4}[s.state];}
function svcSpark(series){
  var mx=1;(series||[]).forEach(function(b){mx=Math.max(mx,b[0]+b[1]);});
  return '<div class="svcspark">'+(series||[]).map(function(b){
    var n=b[0]+b[1],h=n?Math.max(2,Math.round(24*n/mx)):2;
    return '<i class="'+(n?(b[1]?'r':'g'):'')+'" style="height:'+h+'px" title="'+
      n+' 次（失败 '+b[1]+'）"></i>';}).join('')+'</div>';
}
function svcRow(s){
  var cls={ok:'on',warn:'warn',fail:'bad',unknown:'off',off:'off'}[s.state]||'off';
  var h=s.h24||{};
  var bits=['24h '+h.ok+' 成功'];
  if(h.fail)bits.push(h.fail+' 失败');
  if(h.rate!=null)bits.push('成功率 '+h.rate.toFixed(1)+'%');
  if(s.last_ok)bits.push('最近成功 '+svcAgo(s.last_ok));
  if(s.kind==='active'&&s.probe_ts)
    bits.push('探活 '+svcAgo(s.probe_ts)+' · '+(s.probe_ok?'通过':'失败'));
  if(s.avg_ms)bits.push('平均 '+svcMs(s.avg_ms));
  var err=(s.state!=='ok'&&s.state!=='off')?(s.err||s.probe_err||''):'';
  if(s.state==='off')err='未配置（只是这项功能没开，不影响其它服务）';
  return '<div class="svcrow"><span class="dot '+cls+'" style="margin-top:5px"></span>'+
    '<div class="sbody"><div class="sname">'+esc(s.name)+
      ' <span class="tag '+(s.state==='ok'?'g':s.state==='warn'?'y':s.state==='fail'?'r':'')+
      '">'+(SVCN[s.state]||'未知')+'</span></div>'+
    '<div class="sblurb">'+esc(s.desc||'')+'</div>'+
    '<div class="small">'+esc(bits.join(' · '))+'</div>'+
    (err?'<div class="serr'+(s.state==='off'?' off':'')+'" title="'+esc(err)+'">'+
      esc(err)+'</div>':'')+'</div>'+
    svcSpark(s.series||[])+'</div>';
}
function openBoard(){
  /* 管理员壳里没有新窗口（安卓壳连 onCreateWindow 都没有）：同窗口打开，返回键能回 */
  if(/CodebossAdmin\//.test(navigator.userAgent)){location.href='/r/admin/board';return;}
  var w=window.open('/r/admin/board','_blank');
  if(!w)toast('浏览器拦了新窗口，允许弹窗后重试',true);
}
async function loadSvc(){
  try{
    var d=await api('/r/admin_api/services');
    if(!d.ok){$('svcHint').textContent=d.error||'读取失败';return;}
    var s=d.summary||{};
    $('svcStats').innerHTML=tile('正常',s.ok||0)+tile('故障',s.fail||0)+
      tile('有异常',s.warn||0)+tile('待观察',s.unknown||0)+tile('未配置',s.off||0);
    var hh=Math.floor((d.uptime||0)/3600),mm=Math.floor((d.uptime||0)%3600/60);
    $('svcHint').textContent='每 10 秒自动刷新 · 运行 '+(hh?hh+' 小时 ':'')+mm+
      ' 分钟 · 在线开发机 '+(d.online||0)+' · 在线客户端 '+(d.clients||0)+
      ' · 更新于 '+new Date().toLocaleTimeString();
    var arr=(d.services||[]).slice().sort(function(a,b){return svcOrder(a)-svcOrder(b);});
    $('svcList').innerHTML='<div class="card">'+arr.map(svcRow).join('')+'</div>';
  }catch(e){
    $('svcHint').textContent='刷新失败：'+((e&&e.message)||e)+'（'+
      new Date().toLocaleTimeString()+'）';
  }
}
async function loadUsers(){
  var d=await api('/r/admin_api/users?q='+encodeURIComponent(($('uq')||{}).value||''));
  USERS=d.users||[];if(d.plan_opts&&d.plan_opts.length)PLANOPTS=d.plan_opts;
  $('ucount').textContent='共 '+USERS.length+' 个用户';renderUsers();
}
/* 后台分配套餐用的档位表（含内部档）；用户页不用等「套餐」页加载 */
var PLANOPTS=[{key:'free',name:'免费版',sell:true},{key:'pro',name:'Pro',sell:true},
              {key:'max',name:'Max',sell:true}];
function planOptsHtml(cur){
  var list=PLANOPTS.slice();
  if(cur&&!list.some(function(x){return x.key===cur;}))list.push({key:cur,name:cur,sell:true});
  return list.map(function(o){
    return '<option value="'+esc(o.key)+'"'+(o.key===(cur||'free')?' selected':'')+'>'+
      esc(o.name)+(o.sell===false?'（内部）':'')+'</option>';
  }).join('');
}
function planTag(u){
  var k=(u&&u.effective_plan)||'free';
  var o=PLANOPTS.filter(function(x){return x.key===k;})[0]||{key:k,name:k};
  return '<span class="tag'+(k==='free'?'':' g')+'" title="'+esc(k)+'">'+
    esc(o.name)+(k!=='free'&&o.sell===false?' · 内部':'')+'</span>';
}
/* 额度展示：used/cap，并给剩余。kind：min=分钟、sec=秒、cnt=次 */
function qv(v,kind){
  if(kind==='min')return Math.round(v)+' 分';
  if(kind==='sec')return v>=120?Math.floor(v/60)+' 分':Math.round(v)+' 秒';
  return Math.round(v)+' 次';
}
function qbar(used,cap){
  if(!cap)return '';
  var p=Math.max(0,Math.min(100,Math.round(used*100/cap)));
  var col=p>=100?'background:#f85149':(p>=80?'background:#e3b341':'');
  return '<div class="bar" style="width:'+p+'%'+(col?';'+col:'')+'" title="已用 '+p+'%"></div>';
}
function qline(label,used,cap,kind){
  var pre=label?label+' ':'';
  if(!cap)return '<div class="small">'+pre+qv(used,kind)+' · 不限</div>';
  var left=Math.max(0,cap-used);
  var pct=Math.min(999,Math.round(used*100/cap));
  return '<div class="small">'+pre+qv(used,kind)+'/'+qv(cap,kind)+' · '+pct+'%'+
    (left?(' · 余 '+qv(left,kind)):' · 已用完')+'</div>'+qbar(used,cap);
}
function renderUsers(){
  if(!USERS.length){$('userList').innerHTML='<div class="card small">没有匹配用户</div>';return;}
  var h='<div class="row" style="margin:0 0 6px"><button class="gray btnmini" '+
    'onclick="cAddSel(\'cku\',\'users\')">把勾选的用户加入灰度</button></div>'+
    '<div class="card" style="padding:0"><div class="tblwrap"><table class="tbl users">'+
    '<thead><tr><th><input type="checkbox" onclick="ckAll(this,\'cku\')"></th>'+
    '<th>用户</th><th>账号</th><th>套餐</th><th>到期</th><th>远程（今日）</th>'+
    '<th>语音（今日/本月）</th><th>模型（今日）</th><th>开发机</th><th>客户端</th><th>包余</th><th>最近</th><th></th></tr></thead><tbody>';
  h+=USERS.map(function(u){
    var hosts='<div>'+u.devices+' 台'+(u.hosts_cap?(' / 上限 '+u.hosts_cap+' 台'):'')+'</div>'+
      qbar(u.devices,u.hosts_cap);
    var cli='<div>'+(u.clients||0)+' 个'+(u.clients_cap?(' / 上限 '+u.clients_cap+' 个'):'')+'</div>'+
      qbar(u.clients||0,u.clients_cap);
    return '<tr><td><input type="checkbox" class="cku" value="'+u.id+'"></td>'+
      '<td><b>'+esc(u.name)+'</b><div class="small">uid '+u.id+'</div></td>'+
      '<td class="small">'+esc(u.email||u.phone||'未绑定')+'</td>'+
      '<td>'+planTag(u)+((u.quota_ov&&Object.keys(u.quota_ov).length)?' <span class="tag y" title="该账号有手动额度覆盖，展开「管理」可改">定</span>':'')+'</td>'+
      '<td class="small">'+fmtDate(u.plan_expires)+'</td>'+
      '<td>'+qline('',u.use_sec_today,u.use_cap_sec,'sec')+'</td>'+
      '<td>'+qline('今',u.asr_sec_today,u.asr_day_cap,'sec')+
        qline('月',u.asr_sec_month,u.asr_month_cap,'sec')+'</td>'+
      '<td class="small">'+qline('',u.llm_today,u.llm_cap,'cnt')+'</td>'+
      '<td class="small">'+hosts+'</td>'+
      '<td class="small">'+cli+'</td>'+
      '<td class="small">'+(u.use_credit_sec?dur(u.use_credit_sec):'—')+'</td>'+
      '<td class="small">'+ago(u.last_seen)+'</td>'+
      '<td><div class="actions" style="margin:0;flex-wrap:nowrap">'+
      '<button class="gray btnmini" onclick="openAct({uid:'+u.id+'})">使用详情</button>'+
      '<button class="gray btnmini" onclick="uToggle('+u.id+')">管理</button></div></td></tr>'+
      '<tr class="detrow" id="ud-'+u.id+'"><td colspan="13">'+userDetail(u)+'</td></tr>';
  }).join('');
  $('userList').innerHTML=h+'</tbody></table></div></div>';
}
function uToggle(id){var e=$('ud-'+id);if(e)e.className=(e.className.indexOf('on')<0)?'detrow on':'detrow';}
function todayStr(){var d=new Date();return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+
  '-'+('0'+d.getDate()).slice(-2);}
/* 使用详情：这个账号这天每一分钟是哪台终端在用掉的（用户质疑时长时照着解释）。
   分三块：账号合计 → 每个客户端/每台开发机分别用掉多少分钟 → 逐分钟明细。 */
function cidSafe(s){return String(s||'').replace(/[^A-Za-z0-9_.:-]/g,'');}
async function uClients(id){
  var el=$('ucl-'+id);if(!el)return;
  var day=(($('uday-'+id)||{}).value||'');
  var q='uid='+id+(day?('&date='+encodeURIComponent(day)):'');
  el.innerHTML='<div class="small">加载中…</div>';
  var d=await api('/r/admin_api/user_detail?'+q);
  var rd=await api('/r/admin_api/use_clients?'+q);
  if(!d||!d.ok){el.innerHTML='<div class="small">'+esc((d&&d.error)||'取不到')+'</div>';return;}
  /* 「已用」和「包余」不是一个口径，不能相加：套餐内的时长用完了才动包。
     直接给出「今天还能用多少」，免得两个数字并排看着像能加起来。
     2026-11-08 起内部按秒记账（刻度默认 3 秒），这里统一用 dur() 显示。 */
  var capN=d.cap||0, usedN=d.used_day||0, credN=d.credit||0;
  var leftN=(capN?Math.max(0,capN-usedN):0)+credN;
  var h='<div class="small" style="margin:6px 0"><b>'+esc(d.day)+'</b>：已用 <b>'+dur(usedN)+
    '</b> · 套餐 '+(capN?dur(capN):'不限')+(credN?(' ＋ 时长包余 '+dur(credN)):'')+
    (capN?(' → 今天还能用 <b>'+dur(leftN)+'</b>'):'')+'</div>';
  /* 时长包流水：大额变动逐笔列（扣费一格一条，只给今天合计） */
  if((d.credit_log||[]).length||d.charged_today){
    h+='<div class="small" style="margin:8px 0 3px"><b>时长包流水</b>'+
      (d.charged_today?(' · 今天扣费 -'+dur(d.charged_today)):'')+'</div>';
    if((d.credit_log||[]).length){
      h+='<div class="tblwrap"><table class="tbl"><thead><tr><th>时间</th><th>变动</th>'+
        '<th>来源</th><th>变动后余额</th></tr></thead><tbody>';
      h+=(d.credit_log||[]).map(function(r){
        var t=new Date(r.ts*1000);
        var when=(t.getMonth()+1)+'-'+t.getDate()+' '+('0'+t.getHours()).slice(-2)+
          ':'+('0'+t.getMinutes()).slice(-2);
        return '<tr><td class="small">'+when+'</td>'+
          '<td class="small"><b style="color:'+(r.delta>0?'#3fb950':'#f85149')+'">'+
          (r.delta>0?'+':'')+dur(Math.abs(r.delta))+'</b></td>'+
          '<td class="small">'+esc(r.note||r.src||'')+'</td>'+
          '<td class="small">'+dur(r.after)+'</td></tr>';
      }).join('');
      h+='</tbody></table></div>';
    }
  }
  /* 客户端 */
  h+='<div class="small" style="margin:6px 0 3px"><b>客户端</b>（'+(d.clients_used||0)+
    ' 个'+(d.clients_cap?(' / 上限 '+d.clients_cap+' 个'):'')+'）</div>';
  if((d.clients||[]).length){
    h+='<div class="tblwrap"><table class="tbl"><thead><tr><th>终端</th><th>客户端号</th>'+
      '<th>当天</th><th>近 7 天</th><th>最后出现</th><th></th></tr></thead><tbody>'+
      d.clients.map(function(c){
        return '<tr><td class="small">'+esc(c.label||'（无机型记录）')+'</td>'+
          '<td class="small">'+esc(cidSafe(c.cid))+'</td>'+
          '<td class="small">'+(c.min_day||0)+'</td>'+
          '<td class="small">'+(c.min_week||0)+'</td>'+
          '<td class="small">'+ago(c.last)+'</td>'+
          '<td><div class="actions" style="margin:0;flex-wrap:nowrap">'+
          '<button class="gray btnmini" onclick="openAct({uid:'+id+',cid:\''+cidSafe(c.cid)+
          '\'})">使用详情</button>'+
          '<button class="gray btnmini" onclick="uDelClient('+id+',\''+cidSafe(c.cid)+
          '\')">移除</button></div></td></tr>';}).join('')+'</tbody></table></div>';
  }else{h+='<div class="small">（没有登记过客户端）</div>';}
  /* 开发机 */
  h+='<div class="small" style="margin:10px 0 3px"><b>开发机</b>（'+(d.devices||[]).length+' 台）</div>';
  if((d.devices||[]).length){
    h+='<div class="tblwrap"><table class="tbl"><thead><tr><th>设备</th><th>状态</th>'+
      '<th>当天</th><th>近 7 天</th><th>最后在线</th><th></th></tr></thead><tbody>'+
      d.devices.map(function(x){
        return '<tr><td class="small"><b>'+esc(x.name||x.id.slice(0,8))+'</b>'+
          (x.frozen?' <span class="tag r">已冻结</span>':'')+
          '<div class="small">'+esc(x.id)+'</div></td>'+
          '<td class="small">'+(x.online?'<span class="dot on"></span>在线':'<span class="dot off"></span>离线')+'</td>'+
          '<td class="small">'+(x.min_day||0)+'</td>'+
          '<td class="small">'+(x.min_week||0)+'</td>'+
          '<td class="small">'+ago(x.last_seen)+'</td>'+
          '<td><div class="actions" style="margin:0;flex-wrap:nowrap">'+
          '<button class="gray btnmini" onclick="uDevRename('+id+',\''+cidSafe(x.id)+
          '\')">改名</button>'+
          (x.frozen?'<button class="gray btnmini" onclick="uDevAct('+id+',\''+cidSafe(x.id)+
            '\',\'unfreeze\')">解冻</button>':
            '<button class="gray btnmini" onclick="uDevAct('+id+',\''+cidSafe(x.id)+
            '\',\'freeze\')">冻结</button>')+
          '<button class="gray btnmini" style="color:#ff7b72" onclick="uDevDel('+id+',\''+
          cidSafe(x.id)+'\')">删除</button></div></td></tr>';}).join('')+'</tbody></table></div>';
  }else{h+='<div class="small">（没有绑定开发机）</div>';}
  if(d.direct_day){
    h+='<div class="small" style="margin-top:4px">另有 <b>'+d.direct_day+'</b> 分钟记在「开发机直连/离线补报」'+
      '（中继没看到终端；升级前的老数据都归在这一栏，升级后能细分到具体开发机）</div>';
  }
  /* 近 14 天 */
  h+='<div class="small" style="margin:10px 0 3px"><b>近 14 天</b></div><div class="small">'+
    (d.days||[]).map(function(x){return esc(x.day.slice(5))+'：'+x.min+' 分';}).join(' · ')+'</div>';
  /* 动作记录：这一分钟里手机端到底做了什么（发消息/按键/开会话/翻文件/导航/轮询）。
     「我没怎么用，怎么扣这么多」→ 照着这张表逐分钟解释：有 bold 的才是人做的动作，
     只有灰色「轮询」的分钟说明手机挂着没人碰（该查轮询为什么没带 a=0）。 */
  if((d.acts||[]).length){
    var am={};(d.acts||[]).forEach(function(r){(am[r.m]=am[r.m]||[]).push(r);});
    var aks=Object.keys(am).map(Number).sort(function(a,b){return b-a;}).slice(0,90);
    h+='<div class="small" style="margin:10px 0 3px"><b>动作记录</b>（最近 '+aks.length+
      ' 分钟，按分钟合并）</div>'+
      '<div class="tblwrap" style="max-height:340px;overflow:auto"><table class="tbl">'+
      '<thead><tr><th>时间</th><th>来源</th><th>这一分钟做了什么</th></tr></thead><tbody>';
    aks.forEach(function(m){
      var t=new Date(m*60000);
      var when=('0'+(t.getMonth()+1)).slice(-2)+'-'+('0'+t.getDate()).slice(-2)+' '+
        ('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2);
      var rows=am[m].slice().sort(function(a,b){
        var o={act:0,nav:1,read:2};return (o[a.kind]||3)-(o[b.kind]||3);});
      var srcs=[];var parts=[];
      rows.forEach(function(r){
        var ua=String(r.ua||'').replace(/^Mozilla\/5\.0 \(Linux; Android [0-9.]+; /,'')
          .replace(/ Build\/[^;)]*/,'').replace(/\).*$/,'').slice(0,22);
        var who=(r.src==='host')?('直连'+(r.dev?(' '+esc(r.dev.slice(0,8))):'')):(ua||'中继');
        if(who&&srcs.indexOf(who)<0)srcs.push(who);
        var nm='<span title="'+esc(r.path||'')+'">'+esc(r.act||r.path||'')+
          (r.n>1?('×'+r.n):'')+'</span>';
        if(r.kind==='act')parts.push('<b>'+nm+'</b>');
        else if(r.kind==='nav')parts.push(nm);
        else parts.push('<span style="color:#8b949e">'+nm+'</span>');
      });
      var onlyRead=rows.every(function(r){return r.kind!=='act'&&r.kind!=='nav';});
      h+='<tr><td class="small" style="white-space:nowrap">'+when+'</td>'+
        '<td class="small">'+esc(srcs.join(' / '))+'</td>'+
        '<td class="small">'+(onlyRead?'<span style="color:#e3b341">无人操作，只有轮询：</span> ':'')+
        parts.join('、')+'</td></tr>';
    });
    h+='</tbody></table></div>';
  }
  /* 逐分钟明细 */
  if(rd&&rd.ok&&(rd.rows||[]).length){
    h+='<div class="small" style="margin:10px 0 3px"><b>逐分钟明细</b>（'+rd.minutes+' 分钟）</div>'+
      '<div class="tblwrap" style="max-height:280px;overflow:auto"><table class="tbl">'+
      '<thead><tr><th>时间</th><th>终端</th><th>请求数</th></tr></thead><tbody>'+
      rd.rows.map(function(r){
        return '<tr><td class="small">'+esc(r.time)+'</td><td class="small">'+
          esc(r.label||r.key)+'</td><td class="small">'+r.n+'</td></tr>';}).join('')+
      '</tbody></table></div>';
  }
  el.innerHTML=h;
}
/* ── 终端管理 ── */
let CTI='all';
const CSTATE=[['all','全部'],['active','24 小时内活跃（占名额）'],['stale','超 24 小时（已释放）']];
function cliChips(){$('cFilters').innerHTML=CSTATE.map(function(s){
  return '<span class="chip'+(CTI===s[0]?' on':'')+'" onclick="setCli(\''+s[0]+'\')">'+s[1]+'</span>';
}).join('');}
function setCli(s){CTI=s;loadClients();}
async function loadClients(){
  cliChips();
  var d=await api('/r/admin_api/clients?state='+encodeURIComponent(CTI)+
    '&q='+encodeURIComponent(($('cq')||{}).value||''));
  if(!d||!d.ok){toast((d&&d.error)||'取不到',true);return;}
  var CL=d.clients||[];
  $('ccount').textContent='共 '+CL.length+' 个终端（最近 24 小时连过才算占名额，超时自动释放；'+
    '分钟数只统计中继看到的请求，直连的记在开发机侧）';
  if(!CL.length){$('clientList').innerHTML='<div class="card small">没有匹配终端</div>';return;}
  var h='<div class="row" style="margin:0 0 6px"><button class="gray btnmini" '+
    'onclick="cAddSel(\'ckc\',\'clients\')">把勾选的终端加入灰度</button></div>'+
    '<div class="card" style="padding:0"><div class="tblwrap"><table class="tbl">'+
    '<thead><tr><th><input type="checkbox" onclick="ckAll(this,\'ckc\')"></th>'+
    '<th>机型 / 类型</th><th>版本</th><th>终端 ID</th><th>账号</th><th>近 30 天</th>'+
    '<th>首次</th><th>最后</th><th></th></tr></thead><tbody>';
  h+=CL.map(function(x){
    var kind=(x.kind==='app')?'<span class="tag g">App</span>':'<span class="tag y">浏览器</span>';
    /* label 里本来带着「 · App 0.2.18」，版本单列一列，这里去掉免得同屏两份 */
    var lab=(x.label||'').replace(/ · App( [0-9.]+)?$/,'')||'（无机型记录）';
    return '<tr><td><input type="checkbox" class="ckc" value="'+esc(x.cid)+'"></td>'+
      '<td class="small"><b>'+esc(lab)+'</b> '+kind+
      (x.counted?'':' <span class="tag y">不占名额</span>')+
      (x.ua?'<div class="small" style="color:#8b949e" title="'+esc(x.ua)+'">'+
        esc(x.ua.slice(0,46))+'</div>':'')+'</td>'+
      '<td class="small">'+(x.ver?('v'+esc(x.ver)):'—')+'</td>'+
      '<td class="small" title="'+esc(x.cid)+'">'+esc(x.cid.slice(0,18))+'</td>'+
      '<td class="small">'+(x.user?('<b>'+esc(x.user)+'</b>'):'—')+
        '<div class="small">uid '+x.uid+'</div></td>'+
      '<td class="small">'+x.min30+' 分钟</td>'+
      '<td class="small">'+fmtDate(x.first)+'</td>'+
      '<td class="small">'+ago(x.last)+'</td>'+
      '<td><div class="actions" style="margin:0;flex-wrap:nowrap">'+
      '<button class="gray btnmini" onclick="openAct({uid:'+x.uid+',cid:\''+
        cidSafe(x.cid)+'\'})">使用详情</button>'+
      '<button class="gray btnmini" style="color:#ff7b72" onclick="cliDel('+x.uid+',\''+
        cidSafe(x.cid)+'\')">移除</button></div></td></tr>';}).join('');
  $('clientList').innerHTML=h+'</tbody></table></div></div>';
}
async function cliDel(uid,cid){
  if(!confirm('移除终端 '+cid+'？\n名额立刻释放；这个终端再连会重新占一个。'))return;
  var d=await api('/r/admin_api/user',{method:'POST',
    body:JSON.stringify({id:uid,action:'del_client',cid:cid})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok)loadClients();
}
/* ── 使用详情（动作明细）── */
function actInit(){
  if(!$('al_from').value)$('al_from').value=todayStr();
  if(!$('al_to').value)$('al_to').value=todayStr();
  /* 手机上填完筛选直接按「前往/回车」就查：以前只有点按钮才生效，
     打了字按回车什么都没发生，看起来就像「查不出」。 */
  ['al_uid','al_cid','al_dev','al_act'].forEach(function(k){
    var el=$(k); if(el)el.onkeydown=function(e){
      if(e.key==='Enter'){e.preventDefault();loadAct();}};
  });
}
/* 从用户 / 开发机 / 终端页跳过来：把对应的筛选条件带过来 */
function openAct(o){
  o=o||{};
  $('al_uid').value=(o.uid||'');
  $('al_cid').value=(o.cid||'');
  $('al_dev').value=(o.dev||'');
  $('al_act').value=(o.act||'');
  $('al_kind').value=(o.kind||'');
  $('al_from').value=o.from||todayStr();
  $('al_to').value=o.to||todayStr();
  $('alcount').textContent='';
  go('actlog');
}
async function loadAct(){
  actInit();
  var uv=($('al_uid').value||'').trim();
  var q='from='+encodeURIComponent($('al_from').value||todayStr())+
    '&to='+encodeURIComponent($('al_to').value||todayStr())+
    '&kind='+encodeURIComponent($('al_kind').value||'')+
    '&act='+encodeURIComponent(($('al_act').value||'').trim())+
    '&cid='+encodeURIComponent(($('al_cid').value||'').trim())+
    '&dev='+encodeURIComponent(($('al_dev').value||'').trim())+
    '&limit='+encodeURIComponent($('al_limit').value||'600');
  /* 一律按「用户名或 uid」交给后端判断：手机号注册的账号用户名是一串数字，
     以前在前端当成 uid（uid=132****7605），查出来当然是空的。 */
  if(uv)q+='&user='+encodeURIComponent(uv);
  var d=await api('/r/admin_api/actlog?'+q);
  if(!d||!d.ok){toast((d&&d.error)||'取不到',true);return;}
  var rows=d.rows||[];
  $('alcount').textContent='共 '+d.count+' 条 · 动作 '+d.total_n+' 次 · 覆盖 '+
    d.minutes+' 分钟 · '+(d.from===d.to?d.from:(d.from+' ~ '+d.to))+
    ' · 动作记录只留 3 天';
  if(!rows.length){
    $('actList').innerHTML='<div class="card small">没有匹配的动作记录（只保留 3 天）'+
      '<div style="margin-top:6px;color:#8b949e">注意：开发机列表里的「最近在线」只是它连着中继，'+
      '不代表有人在用；只有手机/浏览器真的发过请求（看消息、点按键、开终端）才会记在这里。'+
      '开发机可填设备名或 ID，用户可填用户名或 uid。</div></div>';
    return;}
  var K={act:['动手','#3fb950'],nav:['导航','#e6edf3'],read:['轮询','#8b949e']};
  var h='<div class="card" style="padding:0"><div class="tblwrap" style="max-height:620px;overflow:auto">'+
    '<table class="tbl"><thead><tr><th>时间</th><th>用户</th><th>开发机</th>'+
    '<th>终端</th><th>动作</th><th>次数</th></tr></thead><tbody>';
  h+=rows.map(function(r){
    var t=new Date(r.m*60000);
    var when=('0'+(t.getMonth()+1)).slice(-2)+'-'+('0'+t.getDate()).slice(-2)+' '+
      ('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2);
    var ua=String(r.ua||'').replace(/^Mozilla\/5\.0 \(Linux; Android [0-9.]+; /,'')
      .replace(/^Dalvik\/[0-9.]+ \(Linux; U; Android [0-9.]+; /,'')
      .replace(/ Build\/[^;)]*/,'').replace(/\).*$/,'').slice(0,20);
    var kk=K[r.kind]||['其他','#8b949e'];
    var dn=r.dev_name?('<b>'+esc(r.dev_name)+'</b><div class="small">'+
        esc(r.dev.slice(0,8))+'</div>'):(r.src==='host'?('直连'+
        (r.dev?(' '+esc(r.dev.slice(0,8))):'')):'中继');
    return '<tr><td class="small" style="white-space:nowrap">'+when+'</td>'+
      '<td class="small">'+(r.user?('<b>'+esc(r.user)+'</b>'):'—')+
        '<div class="small">uid '+r.uid+'</div></td>'+
      '<td class="small">'+dn+'</td>'+
      '<td class="small">'+(r.cid?esc(r.cid.slice(0,14)):'—')+
        (ua?'<div class="small" style="color:#8b949e">'+esc(ua)+'</div>':'')+'</td>'+
      '<td class="small"><span style="color:'+kk[1]+'">'+
        (r.kind==='act'?'<b>':'')+esc(r.act)+(r.kind==='act'?'</b>':'')+'</span>'+
        (r.path&&r.path!==r.act?'<div class="small" style="color:#8b949e">'+
          esc(r.path)+'</div>':'')+'</td>'+
      '<td class="small">'+r.n+'</td></tr>';}).join('');
  $('actList').innerHTML=h+'</tbody></table></div></div>';
}
function actReset(){
  $('al_uid').value='';$('al_cid').value='';$('al_dev').value='';$('al_act').value='';
  $('al_kind').value='';
  $('al_from').value=todayStr();$('al_to').value=todayStr();$('al_limit').value='600';
  loadAct();
}
/* 移除单个客户端：老客户端（换手机/重装）一直占着名额，后台点一下就释放 */
async function uDelClient(id,cid){
  if(!confirm('移除客户端 '+cid+'？\n名额立刻释放；这个终端再连会重新占一个。'))return;
  var d=await api('/r/admin_api/user',{method:'POST',
    body:JSON.stringify({id:id,action:'del_client',cid:cid})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){loadUsers();uClients(id);}
}
async function uDevAct(id,dev,act){
  var d=await api('/r/admin_api/device',{method:'POST',
    body:JSON.stringify({id:dev,action:act})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){loadUsers();uClients(id);}
}
function uDevRename(id,dev){
  var n=prompt('新的设备名');if(!n)return;
  api('/r/admin_api/device',{method:'POST',
    body:JSON.stringify({id:dev,action:'rename',name:n})}).then(function(d){
      toast(d.msg||d.error||'完成',!d.ok);if(d.ok){loadUsers();uClients(id);}});
}
function uDevDel(id,dev){
  var v=prompt('删除开发机需输入设备 ID 确认：\n'+dev);
  if(v!==dev){if(v!==null)toast('输入不一致，已取消',true);return;}
  api('/r/admin_api/device',{method:'POST',
    body:JSON.stringify({id:dev,action:'delete',confirm:v})}).then(function(d){
      toast(d.msg||d.error||'完成',!d.ok);if(d.ok){loadUsers();uClients(id);}});
}
function userDetail(u){
  var hosts=u.devices+' 台'+(u.hosts_cap?('（上限 '+u.hosts_cap+' 台）'):'（不限）');
  var q='<div class="small" style="line-height:1.9;margin-bottom:8px;padding:6px 8px;'+
    'border:1px solid var(--line);border-radius:8px">'+
    '<div class="small" style="margin-bottom:2px"><b>额度使用</b></div>'+
    qline('远程今日',u.use_sec_today,u.use_cap_sec,'sec')+
    qline('语音今日',u.asr_sec_today,u.asr_day_cap,'sec')+
    qline('语音本月',u.asr_sec_month,u.asr_month_cap,'sec')+
    qline('模型今日',u.llm_today,u.llm_cap,'cnt')+
    '<div class="small">开发机 '+hosts+' · 客户端 '+(u.clients||0)+' 个'+
    (u.clients_cap?('（上限 '+u.clients_cap+' 个）'):'（不限）')+' · 时长包余 '+
    (u.use_credit_sec?dur(u.use_credit_sec):'—')+'</div></div>';
  var uc='<div class="row" style="margin-bottom:8px">'+
    '<input class="mini" id="uday-'+u.id+'" type="date" value="'+todayStr()+'" style="flex:1">'+
    '<button class="gray" onclick="uClients('+u.id+')">使用详情（客户端/开发机）</button></div>'+
    '<div id="ucl-'+u.id+'" class="small" style="margin-bottom:8px"></div>';
  var OV=[['sends_day','消息/天'],['asr_sec_day','语音秒/天'],['asr_sec_month','语音秒/月'],
    ['llm_day','模型/天'],['hosts','开发机'],['clients','客户端'],['use_min_day','时长分/天']];
  var ov=u.quota_ov||{},nOv=0;
  for(var k in ov)if(ov[k]!==null&&ov[k]!==''&&ov[k]!==undefined)nOv++;
  var uq='<div class="small" style="line-height:1.7;margin:8px 0;padding:6px 8px;'+
    'border:1px solid var(--line);border-radius:8px">'+
    '<div class="small" style="margin-bottom:4px"><b>额度覆盖</b>'+
    (nOv?' <span class="tag y" title="该账号有手动额度覆盖，留空项仍按套餐">已覆盖 '+nOv+' 项</span>':'')+
    ' <span class="small" style="color:var(--mut)">留空=按套餐，0=不限</span></div>'+
    OV.map(function(p){var v=ov[p[0]];v=(v===0||v)?String(v):'';
      return '<div class="row" style="margin-bottom:3px"><span style="width:86px">'+p[1]+'</span>'+
      '<input class="mini" id="uq-'+u.id+'-'+p[0]+'" type="number" min="0" value="'+v+'" style="width:90px"></div>';
    }).join('')+
    '<div class="row"><button class="gray btnmini" onclick="uSetQuota('+u.id+')">保存覆盖</button></div></div>';
  /* 端权限：这账号能用哪条线（agentboss=老板/agent 面，codeboss=工头/code 面）。
     规则见 _allowed_products：NULL=按套餐、空串=两线都关、CSV=逐账号指定。 */
  var up='<div class="row" style="margin:8px 0"><span class="small" style="width:64px">可用端</span>'+
    '<select class="mini" id="uprod-'+u.id+'" style="flex:1">'+prodOpts(u.products)+'</select>'+
    '<button class="gray" onclick="uSetProducts('+u.id+')">保存</button></div>';
  return q+uc+'<div class="row"><select class="mini" id="uplan-'+u.id+'">'+
    planOptsHtml(u.plan)+'</select>'+
    '<input class="mini" id="udays-'+u.id+'" type="number" value="30" min="1" style="width:72px">'+
    '<button class="gray" onclick="uSetPlan('+u.id+')">设套餐/续期</button></div>'+up+uq+
    '<div class="row"><input class="mini" id="usec-'+u.id+'" type="number" placeholder="分钟数（负=扣）" style="flex:1">'+
    '<button class="gray" onclick="uCredits('+u.id+',60)">+60分</button>'+
    '<button class="gray" onclick="uCredits('+u.id+',300)">+5小时</button>'+
    '<button class="gray" onclick="uCreditsFrom('+u.id+')">按输入</button></div>'+
    '<div class="actions"><button class="gray" onclick="uReset('+u.id+')">重置密码</button>'+
    '<button class="gray" onclick="uLogout('+u.id+')">强制下线</button>'+
    '<button class="gray" onclick="uClientsClear('+u.id+')">清空客户端计数</button></div>'+
    '<div class="row" style="margin-top:8px"><input class="mini" id="udel-'+u.id+
    '" placeholder="输入用户名确认删除" style="flex:1">'+
    '<button class="gray" style="color:#ff7b72" onclick="uDel('+u.id+')">删除账号</button></div>';
}
/* ── 问题与建议 ── */
async function loadFb(){
  var d=await api('/r/admin_api/feedback?limit=100');
  FBS=d.items||[];
  $('fbcount').textContent='共 '+FBS.length+' 条';
  renderFb();
}
function fbSize(n){
  n=Number(n||0);
  return n>=1048576?(n/1048576).toFixed(1)+'MB':Math.round(n/1024)+'KB';
}
function renderFb(){
  if(!FBS.length){$('fbList').innerHTML='<div class="card small">还没有反馈</div>';return;}
  var h='';
  FBS.forEach(function(f){
    var media='';
    if(f.media&&f.media.length){
      media='<div class="grid2" style="margin-top:8px">'+f.media.map(function(m){
        if(/^video\//.test(m.t||'')){
          return '<div><video src="'+m.u+'" controls preload="metadata" '+
            'style="width:100%;border-radius:8px;background:#000"></video>'+
            '<div class="small">'+fbSize(m.s)+'</div></div>';
        }
        return '<a href="'+m.u+'" target="_blank" rel="noreferrer">'+
          '<img src="'+m.u+'" loading="lazy" style="width:100%;border-radius:8px">'+
          '<div class="small">'+fbSize(m.s)+'</div></a>';
      }).join('')+'</div>';
    }
    h+='<div class="card" style="margin-top:8px">'+
      '<div class="row" style="align-items:flex-start">'+
        '<div style="flex:1">'+
          '<b class="fbuser" style="cursor:pointer;text-decoration:underline dotted" '+
            'onclick="fbUser('+f.id+','+JSON.stringify(f.uid)+')" title="查看用户信息">'+esc(f.user)+'</b> '+
          '<span class="tag'+(f.kind==='建议'?' y':'')+'">'+esc(f.kind||'问题')+'</span> '+
          (f.state===2?'<span class="tag">已关闭</span>':
            (f.state?'<span class="tag g">处理中</span>':'<span class="tag r">待处理</span>'))+
          (f.n_reply?(' <span class="tag y">'+f.n_reply+' 条回复</span>'):'')+
          '<div class="small">'+esc(f.devname||f.dev||'')+' · '+esc(f.ver||'')+
            (f.plat?' · '+esc(f.plat):'')+' · '+
            new Date((f.ts||0)*1000).toLocaleString()+'</div>'+
        '</div>'+
        '<button class="gray btnmini" onclick="fbReplies('+f.id+')">回复/查看</button>'+
      '</div>'+
      (f.text?'<div style="white-space:pre-wrap;margin-top:6px;line-height:1.6">'+esc(f.text)+'</div>':'')+
      media+
      '<div class="det" id="fbr-'+f.id+'"></div>'+
      '<div class="det" id="fbu-'+f.id+'"></div>'+
    '</div>';
  });
  $('fbList').innerHTML=h;
}
/* 工单对话：官方回复后用户还能接着回（像阿里云工单），关闭工单才停。 */
async function fbReplies(id){
  var el=$('fbr-'+id);
  if(!el)return;
  if(el.className==='det on'){el.className='det';return;}
  el.className='det on';el.innerHTML='<div class="small">加载中…</div>';
  var d=await api('/r/admin_api/feedback_detail?id='+id);
  if(!d||!d.ok){el.innerHTML='<div class="small">取不到</div>';return;}
  var it=d.item||{}, rs=d.replies||[];
  var h='<div style="border-top:1px solid var(--line);margin-top:8px;padding-top:8px">'+
    '<div class="small"><b>对话记录</b> · 工单没关闭前用户可以接着回</div>';
  rs.forEach(function(r){
    h+='<div class="small" style="margin-top:6px;padding:6px 8px;border-radius:8px;'+
      'background:'+(r.who==='admin'?'rgba(63,185,80,.12)':'var(--ctl)')+'">'+
      '<b>'+(r.who==='admin'?'官方回复':'用户回复')+'</b> · '+
      new Date((r.ts||0)*1000).toLocaleString()+
      '<div style="white-space:pre-wrap;margin-top:4px;line-height:1.6">'+esc(r.text)+'</div></div>';
  });
  if(!rs.length) h+='<div class="small">还没有回复</div>';
  h+='<div class="row" style="margin-top:8px"><input class="mini" id="fbrt-'+id+
    '" placeholder="回复用户…" style="flex:1">'+
    '<button class="gray btnmini" onclick="fbSendReply('+id+')">发送</button></div>';
  h+='<div class="row" style="margin-top:6px">'+
    (it.state===2?'<button class="gray btnmini" onclick="fbClose('+id+',0)">重新打开</button>'
                :'<button class="gray btnmini" onclick="fbClose('+id+',1)">关闭工单</button>')+
    '</div></div>';
  el.innerHTML=h;
}
async function fbSendReply(id){
  var box=$('fbrt-'+id);var t=String((box&&box.value)||'').trim();
  if(!t){toast('写点内容再发',true);return;}
  var d=await api('/r/admin_api/feedback_reply',{method:'POST',
    body:JSON.stringify({id:id,text:t})});
  toast(d.ok?'已回复，用户能在 App 里看到':(d.error||'失败'),!d.ok);
  if(d.ok){var el=$('fbr-'+id);if(el)el.className='det';loadFb();}
}
async function fbClose(id,close){
  var d=await api('/r/admin_api/feedback',{method:'POST',
    body:JSON.stringify({id:id,close:!!close})});
  toast(d.ok?(close?'工单已关闭，用户不能再回复':'工单已重新打开'):(d.error||'失败'),!d.ok);
  if(d.ok)loadFb();
}
async function fbUser(fid, uid){
  var el=$('fbu-'+fid);
  if(!uid){el.className='det on';el.innerHTML='<div class="small">这个账号已删除</div>';return;}
  if(el.className==='det on'){el.className='det';return;}
  el.className='det on';el.innerHTML='<div class="small">加载中…</div>';
  var d=await api('/r/admin_api/users?q='+encodeURIComponent(String(uid)));
  var u=(d.users||[]).filter(function(x){return x.id===uid;})[0];
  if(!u){el.innerHTML='<div class="small">用户不存在</div>';return;}
  el.innerHTML='<div class="small" style="line-height:1.9">'+
    '用户名 <b>'+esc(u.name)+'</b> · uid '+u.id+'<br>'+
    '套餐 '+esc(u.effective_plan||'free')+' · 到期 '+fmtDate(u.plan_expires)+'<br>'+
    '邮箱 '+esc(u.email||'—')+' · 手机 '+esc(u.phone||'—')+'<br>'+
    '开发机 '+u.devices+' 台 · 包余 '+(u.use_credit_sec?dur(u.use_credit_sec):'—')+'</div>'+
    qline('今日远程',u.use_sec_today,u.use_cap_sec,'sec')+
    qline('今日语音',u.asr_sec_today,u.asr_day_cap,'sec')+
    qline('今日模型',u.llm_today,u.llm_cap,'cnt')+
    '<div class="small" style="line-height:1.9">'+
    '注册 '+fmtDate(u.created)+' · 最近 '+ago(u.last_seen)+
    '</div><div class="actions">'+
    '<button class="gray btnmini" onclick="fbOpenUser('+u.id+')">在用户页打开</button></div>';
}
function fbOpenUser(id){go('users');$('uq').value=String(id);loadUsers();}
async function fbState(id,st){
  var d=await api('/r/admin_api/feedback',{method:'POST',body:JSON.stringify({id:id,state:st})});
  toast(d.ok?'已更新':(d.error||'失败'),!d.ok);
  if(d.ok)loadFb();
}
async function uAct(id,body,after){
  body.id=id;var d=await api('/r/admin_api/user',{method:'POST',body:JSON.stringify(body)});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){if(after)after(d);loadUsers();if(CT==='ov')loadOv();}
}
function uSetPlan(id){uAct(id,{action:'set_plan',plan:$('uplan-'+id).value,
  days:Number($('udays-'+id).value||30)});}
/* 端权限三态：auto=按套餐（NULL）、none=两条线都关（空串）、其余=CSV 指定的线 */
function prodOpts(v){
  var cur=(v===null||v===undefined)?'auto':(String(v)===''?'none':String(v));
  return [['auto','按套餐（跟随用户产品线）'],['codeboss','仅 CodeBoss（工头）'],
          ['agentboss','仅 AgentBoss（老板）'],['codeboss,agentboss','两条线都开'],
          ['none','两条线都关（停用）']].map(function(o){
    return '<option value="'+o[0]+'"'+(o[0]===cur?' selected':'')+'>'+o[1]+'</option>';
  }).join('');
}
function uSetProducts(id){
  var v=$('uprod-'+id).value;
  uAct(id,{action:'set_products',products:(v==='auto'?null:(v==='none'?'':v))});
}
function uSetQuota(id){
  var K=['sends_day','asr_sec_day','asr_sec_month','llm_day','hosts','clients','use_min_day'];
  var lim={};
  K.forEach(function(k){var el=$('uq-'+id+'-'+k);var v=el?String(el.value).trim():'';
    lim[k]=(v===''?null:Number(v));});
  uAct(id,{action:'set_quota',limits:lim});
}
function uCredits(id,min){uAct(id,{action:'use_credit',min:min});}
function uCreditsFrom(id){var v=Number($('usec-'+id).value||0);
  if(!v){toast('先填分钟数',true);return;}uAct(id,{action:'use_credit',min:v});}
function uReset(id){uAct(id,{action:'reset_pass'},function(d){
  if(d.password)alert('新密码：'+d.password+'\n只显示这一次，请立即保存。');});}
function uLogout(id){uAct(id,{action:'logout'});}
function uClientsClear(id){if(!confirm('清空这个账号的客户端计数？（换手机/重装后旧记录占名额时用）'))return;
  uAct(id,{action:'clear_clients'});}
function uDel(id){var v=($('udel-'+id).value||'').trim();
  if(!v){toast('输入用户名才能删除',true);return;}
  uAct(id,{action:'delete',confirm:v});}
/* ── 版本推送（灰度）：谁是「对象」由中继按库里的归属算——
   开发机按设备 ID、终端按客户端 ID、用户按 uid；不同对象可以分到不同版本。 ── */
let CAN={default:{},rules:[],pkgs:[],apk:null};
async function loadCanary(){
  var d=await api('/r/admin_api/canary');
  if(!d.ok){toast(d.error||'取不到',true);return;}
  CAN=d;cRender();
}
function canRule(){
  var sel=$('cRule');if(!sel)return null;
  return (CAN.rules||[]).filter(function(r){return String(r.id)===sel.value;})[0]||null;
}
function cRender(){
  if(!$('cst'))return;
  var def=CAN.default||{},apk=CAN.apk||{};
  $('cst').textContent='最新包 v'+(((CAN.pkgs||[])[0])||'—')+' · 全员基线 v'+(def.ver||'=最新')+
    ' · APK code '+(apk.code||'—')+' · 基线 '+(def.apk||'=最新');
  var sel=$('cRule'),cur=sel.value;
  sel.innerHTML=(CAN.rules&&CAN.rules.length?'':'<option value="">（无规则）</option>')+
    (CAN.rules||[]).map(function(r){return '<option value="'+esc(r.id)+'">v'+esc(r.ver||'?')+
      ' · APK '+esc(r.apk||'最新')+
      ' · 开发机 '+((r.devs||[]).length)+' / 终端 '+((r.clients||[]).length)+
      ' / 用户 '+((r.users||[]).length)+'</option>';}).join('');
  if(cur)sel.value=cur;
  var r=canRule();
  if($('cRuleApk'))$('cRuleApk').placeholder=r?String(r.apk||'0')+'（0=最新）':'0=最新';
  if($('cbVer'))$('cbVer').placeholder='host 版本（现 '+(def.ver||'最新')+'）';
  if($('cbApk'))$('cbApk').placeholder='安卓 code（现 '+(def.apk||'最新')+'）';
  /* 灰度常驻：进过一次就永远跟最新包，规则删了也不踢出去（× 才是真移出）。
     老规则钉着旧版本号，不靠这块名单，开发机每次发版都得重新勾一遍。 */
  var gray=CAN.gray||{};
  var gHtml=['devs','clients','users'].map(function(k){
    var nm={devs:'开发机',clients:'终端',users:'用户'}[k],arr=gray[k]||[];
    return '<div><b>灰度常驻·'+nm+'</b>（'+arr.length+'）：'+(arr.length?arr.map(function(x){
      var s=String(x);
      return '<span class="tag" style="margin:2px">'+esc(s)+
        ' <a href="javascript:void(0)" onclick="cGray(\'gray_del\',\''+k+'\',[\''+cidSafe(s)+'\'])">×</a></span>';
    }).join(''):'（空）')+'</div>';
  }).join('');
  $('cMembers').innerHTML=gHtml+(r?['devs','clients','users'].map(function(k){
    var nm={devs:'开发机',clients:'终端',users:'用户'}[k],arr=r[k]||[];
    return '<div><b>'+nm+'</b>（'+arr.length+'）：'+(arr.length?arr.map(function(x){
      var s=String(x);
      return '<span class="tag" style="margin:2px">'+esc(s)+
        ' <a href="javascript:void(0)" onclick="cIds(\'del\',\''+k+'\',[\''+cidSafe(s)+'\'])">×</a></span>';
    }).join(''):'（空）')+'</div>';
  }).join(''):'<div>还没有规则：点「新建」建一条（版本默认=最新包），再到开发机/终端/用户页勾选对象加入。</div>');
  $('cpkgs').innerHTML='中继上留着可推送的 host 包：'+((CAN.pkgs||[]).length?
    CAN.pkgs.map(function(v){return '<a class="tag" target="_blank" '+
      'href="/codeboss-host-'+encodeURIComponent(v)+'.tar.gz">v'+esc(v)+'</a>';}).join(' '):'（无）');
  var tf=CAN.tf||{};
  $('tfst').textContent=tf.code?('当前 build '+tf.code+(tf.ver?'（'+tf.ver+'）':'')):'还没登记';
}
async function cTfSave(){
  var code=String(($('tfCode')||{}).value||'').trim();
  if(!/^\d+$/.test(code)){toast('build 号填数字，如 27',true);return;}
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify(
    {action:'tf',code:code,ver:String(($('tfVer')||{}).value||'').trim()})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.tf=d.tf||CAN.tf;$('tfCode').value='';$('tfVer').value='';cRender();}
}
async function cIds(on,kind,ids){
  var r=canRule();
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify(
    {action:on,kind:kind,ids:ids,rule:r?r.id:''})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.default=d.default||CAN.default;CAN.rules=d.rules||[];
    CAN.gray=d.gray||CAN.gray;cRender();}
}
async function cGray(on,kind,ids){
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify(
    {action:on,kind:kind,ids:ids})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.gray=d.gray||CAN.gray;cRender();}
}
function ckAll(el,cls){
  Array.prototype.slice.call(document.querySelectorAll('.'+cls)).forEach(function(e){e.checked=el.checked;});
}
function cAddSel(cls,kind){
  var ids=Array.prototype.slice.call(document.querySelectorAll('.'+cls+':checked'))
    .map(function(e){return e.value;});
  if(!ids.length){toast('先在列表里勾选对象',true);return;}
  cIds('add',kind,ids);
}
async function cNewRule(){
  var v=prompt('这条灰度规则推哪个版本？（中继上有的：'+((CAN.pkgs||[]).join(' / ')||'无')+'）',
    (CAN.pkgs||[])[0]||'');
  if(v===null)return;
  var a=prompt('这条规则的安卓 code？（留空=中继最新 '+(((CAN.apk||{}).code)||'—')+'，0=跟最新）','');
  if(a===null)return;
  var d=await api('/r/admin_api/canary',{method:'POST',
    body:JSON.stringify({action:'new',ver:String(v).trim(),apk:String(a).trim()})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.rules=d.rules||[];loadCanary();}
}
async function cDelRule(){
  var r=canRule();if(!r){toast('先选一条规则',true);return;}
  if(!confirm('删除这条灰度规则（v'+r.ver+'）？名单里的对象会回到全员基线。'))return;
  var d=await api('/r/admin_api/canary',{method:'POST',
    body:JSON.stringify({action:'del_rule',rule:r.id})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.rules=d.rules||[];loadCanary();}
}
async function cRuleApkSave(){
  var r=canRule();if(!r){toast('先选一条规则',true);return;}
  var v=String(($('cRuleApk')||{}).value||'').trim();
  if(!/^\d*$/.test(v)){toast('填安卓 code（数字），0=最新',true);return;}
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify(
    {action:'rule_apk',rule:r.id,apk:Number(v||0)})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok)loadCanary();
}
async function cBaseline(){
  var def=CAN.default||{};
  var ver=String(($('cbVer')||{}).value||'').trim()||String(def.ver||'');
  var a=String(($('cbApk')||{}).value||'').trim();
  var apk=a===''?Number(def.apk||0):Number(a);
  if(!/^\d+$/.test(String(apk))){toast('安卓 code 要是数字',true);return;}
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify(
    {action:'baseline',ver:ver,apk:apk})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok){CAN.default=d.default||CAN.default;cRender();}
}
async function cPromote(){
  if(!confirm('全量发布最新版本 v'+(((CAN.pkgs||[])[0])||'?')+'？\n所有人都会拿到它，灰度规则清空。'))return;
  var d=await api('/r/admin_api/canary',{method:'POST',body:JSON.stringify({action:'promote'})});
  toast(d.msg||d.error||'完成',!d.ok);
  if(d.ok)loadCanary();
}
async function loadDevs(){
  var d=await api('/r/admin_api/devices?q='+encodeURIComponent(($('dq')||{}).value||''));
  DEVS=d.devices||[];$('dcount').textContent='共 '+DEVS.length+' 台';renderDevs();
}
function renderDevs(){
  if(!DEVS.length){$('devList').innerHTML='<div class="card small">没有匹配开发机</div>';return;}
  var h='<div class="row" style="margin:0 0 6px"><button class="gray btnmini" '+
    'onclick="cAddSel(\'ckd\',\'devs\')">把勾选的开发机加入灰度</button>'+
    '<span class="small">（先到「版本」页选好推哪个版本/哪条规则）</span></div>'+
    '<div class="card" style="padding:0"><div class="tblwrap"><table class="tbl devs">'+
    '<thead><tr><th><input type="checkbox" onclick="ckAll(this,\'ckd\')"></th>'+
    '<th>状态</th><th>设备名</th><th>版本</th><th>设备 ID</th><th>账号</th><th>客户端</th><th>线路</th>'+
    '<th>最近</th><th>局域网</th><th></th></tr></thead><tbody>';
  h+=DEVS.map(function(x){
    var on=x.online;
    return '<tr><td><input type="checkbox" class="ckd" value="'+esc(x.id)+'"></td>'+
      '<td><span class="dot '+(on?'on':'off')+'"></span>'+(on?'在线':'离线')+'</td>'+
      '<td><b>'+esc(x.name||x.id.slice(0,8))+'</b>'+(x.frozen?' <span class="tag r">已冻结</span>':'')+
        (x.logs_on?(' <span class="tag y">日志中'+(x.log_left?' 剩'+Math.ceil(x.log_left/60)+'分':'')+'</span>'):'')+'</td>'+
      '<td class="small">'+(x.ver?'v'+esc(x.ver):'—')+'</td>'+
      '<td class="small">'+esc(x.id)+'</td>'+
      '<td class="small">'+esc(x.owner_name||'未绑定')+'</td>'+
      '<td class="small">'+x.clients+'</td>'+
      '<td>'+(x.rtc?'<span class="tag g">P2P</span>':'<span class="tag y">仅中转</span>')+'</td>'+
      '<td class="small">'+ago(x.last_seen)+'</td>'+
      '<td class="small">'+((x.lan||[]).join(', ')||'—')+'</td>'+
      '<td class="ac"><div class="actions" style="margin:0;flex-wrap:nowrap">'+
      '<button class="gray btnmini" onclick="dRename(\''+x.id+'\')">改名</button>'+
      (x.frozen?'<button class="gray btnmini" onclick="dAct(\''+x.id+'\',\'unfreeze\')">解冻</button>':
                '<button class="gray btnmini" onclick="dAct(\''+x.id+'\',\'freeze\')">冻结</button>')+
      '<button class="gray btnmini" onclick="openAct({dev:\''+cidSafe(x.id)+'\'})">使用详情</button>'+
      '<button class="gray btnmini" onclick="dLogs(\''+x.id+'\','+(x.logs_on?'0':'1')+')">'+
        (x.logs_on?'关日志':'开日志')+'</button>'+
      '<button class="gray btnmini" onclick="dAct(\''+x.id+'\',\'update\')">立即更新</button>'+
      ((x.logs_on||x.log_at)?'<button class="gray btnmini" onclick="dLogView(\''+x.id+'\')">看日志</button>':'')+
      '<button class="gray btnmini" onclick="dPair(\''+x.id+'\')">配对码</button>'+
      (x.owner_name?'<button class="gray btnmini" onclick="dUnbind(\''+x.id+'\')">解绑</button>':'')+
      '<button class="gray btnmini" style="color:#ff7b72" onclick="dDel(\''+x.id+'\')">删除</button>'+
      '</div></td></tr>';
  }).join('');
  $('devList').innerHTML=h+'</tbody></table></div></div>';
}
async function dAct(id,act){
  var d=await api('/r/admin_api/device',{method:'POST',body:JSON.stringify({id:id,action:act})});
  toast(d.msg||d.error||'完成',!d.ok);if(d.ok)loadDevs();}
function dRename(id){var n=prompt('新的设备名');if(!n)return;
  dAct(id,'rename');/* 先改名 */
  api('/r/admin_api/device',{method:'POST',body:JSON.stringify({id:id,action:'rename',name:n})})
    .then(function(d){toast(d.msg||d.error||'完成',!d.ok);loadDevs();});}
function dUnbind(id){
  if(!confirm('解绑这台开发机？\n它会回到免费档，原账号不再拥有；同一指纹可重新登记。')) return;
  dAct(id,'unbind');}
async function dPair(id){
  var d=await api('/r/admin_api/device',{method:'POST',body:JSON.stringify({id:id,action:'paircode'})});
  if(!d.ok){toast(d.error||'失败',true);return;}
  alert('配对码：'+d.pair_code+'\n15 分钟有效。\n手机上「接入开发机」粘贴此码，或打开：\n'+location.origin+d.pair_url);
  loadDevs();}
async function dLogs(id,on){
  var d=await api('/r/admin_api/device',{method:'POST',body:JSON.stringify({id:id,action:'logs',on:on==1})});
  toast(d.msg||d.error||'完成',!d.ok);if(d.ok)loadDevs();}
async function dLogView(id){
  var d=await api('/r/admin_api/device_logs?id='+id);
  if(!d.ok){toast(d.error||'失败',true);return;}
  var txt=d.at?(new Date(d.at*1000).toLocaleString()+'（v'+(d.ver||'?')+'，'+d.txt.length+' 字节）\n\n'+d.txt)
              :'还没收到日志：开发机可能离线，或者刚打开开关，等十几秒再刷新。';
  if(matchMedia('(max-width:899px)').matches){   /* 手机上页内看，别把人踢去新窗口 */
    $('logvt').textContent='日志 '+id.slice(0,8);
    $('logvpre').textContent=txt;
    $('logv').style.display='block';
    return;
  }
  var w=window.open('','_blank');
  if(!w){toast('浏览器拦了弹窗',true);return;}
  w.document.title='日志 '+id.slice(0,8);
  w.document.body.style.margin='0';
  var pre=w.document.createElement('pre');
  pre.style.cssText='white-space:pre-wrap;word-break:break-all;font:12px/1.5 ui-monospace,Menlo,monospace;padding:12px';
  pre.textContent=d.at?(new Date(d.at*1000).toLocaleString()+'（v'+(d.ver||'?')+'，'+d.txt.length+' 字节）\n\n'+d.txt)
                      :'还没收到日志：开发机可能离线，或者刚打开开关，等十几秒再刷新。';
  w.document.body.appendChild(pre);
  var a=w.document.createElement('a');
  a.href='/r/admin_api/device_logs?id='+encodeURIComponent(id)+'&dl=1';
  a.textContent='下载 .log';
  a.style.cssText='position:fixed;top:8px;right:12px;padding:4px 10px;border:1px solid #888;'+
                  'border-radius:4px;font:12px ui-monospace,Menlo,monospace;text-decoration:none;color:inherit;background:#fff';
  w.document.body.appendChild(a);
}
/* 手机端日志查看器里的「复制」：clipboard 不行就全选，让用户长按复制 */
function logvCopy(){
  var t=$('logvpre').textContent||'';
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject())
    .then(function(){toast('已复制');}).catch(function(){
      var r=document.createRange();r.selectNodeContents($('logvpre'));
      var s=getSelection();s.removeAllRanges();s.addRange(r);
      toast('已选中，长按复制');});
}
function dDel(id){var v=prompt('删除开发机需输入设备 ID 确认：\n'+id);
  if(v!==id){if(v!==null)toast('输入不一致，已取消',true);return;}
  api('/r/admin_api/device',{method:'POST',body:JSON.stringify({id:id,action:'delete',confirm:v})})
    .then(function(d){toast(d.msg||d.error||'完成',!d.ok);if(d.ok)loadDevs();});}
const OSTAT=[['all','全部'],['paid','已付'],['pending','待支付'],['failed','失败']];
function orderChips(){$('oFilters').innerHTML=OSTAT.map(function(s){
  return '<span class="chip'+(OST===s[0]?' on':'')+'" onclick="setOst(\''+s[0]+'\')">'+s[1]+'</span>';
}).join('');}
function setOst(s){OST=s;loadOrders();}
async function loadOrders(){
  orderChips();
  var d=await api('/r/admin_api/orders?status='+OST+'&q='+encodeURIComponent(($('oq')||{}).value||''));
  ORDERS=d.orders||[];$('ocount').textContent='共 '+ORDERS.length+' 单';renderOrders();
}
function renderOrders(){
  if(!ORDERS.length){$('orderList').innerHTML='<div class="card small">没有订单</div>';return;}
  var h='<div class="card" style="padding:0"><div class="tblwrap"><table class="tbl orders">'+
    '<thead><tr><th>订单号</th><th>用户</th><th>商品</th><th>金额</th><th>状态</th>'+
    '<th>下单</th><th>到账</th><th></th></tr></thead><tbody>';
  h+=ORDERS.map(function(o){
    var st=o.status===1?'<span class="tag g">已付</span>':(o.status<0?'<span class="tag r">失败</span>':'<span class="tag y">待支付</span>');
    var paid=o.status===1?('到账 '+ago(o.paid)+(o.type?' · '+esc(o.type):'')):'—';
    var act=o.status===1?'':'<div class="actions" style="margin:0;flex-wrap:nowrap">'+
      '<button class="gray btnmini" onclick="oRequery(\''+o.otn+'\')">查账</button>'+
      '<button class="gray btnmini" style="color:#ff7b72" onclick="oMark(\''+o.otn+'\')">人工入账</button></div>';
    return '<tr><td class="small">'+esc(o.otn)+(o.trade_no?'<div class="small">'+esc(o.trade_no)+'</div>':'')+'</td>'+
      '<td class="small">'+esc(o.user)+'<div class="small">uid '+o.uid+'</div></td>'+
      '<td class="small">'+esc(o.sku_name)+'</td>'+
      '<td><b>'+money(o.money)+'</b></td>'+
      '<td>'+st+'</td>'+
      '<td class="small">'+ago(o.created)+'</td>'+
      '<td class="small">'+paid+'</td>'+
      '<td class="ac">'+act+'</td></tr>';
  }).join('');
  $('orderList').innerHTML=h+'</tbody></table></div></div>';
}
async function oRequery(otn){
  var d=await api('/r/admin_api/order',{method:'POST',body:JSON.stringify({otn:otn,action:'requery'})});
  toast(d.msg||d.error||'完成',!d.ok);if(d.ok){loadOrders();loadOv();}}
function oMark(otn){
  var v=prompt('人工入账会直接给用户加套餐/使用时长包。\n确认请再输入订单号：\n'+otn);
  if(v!==otn){if(v!==null)toast('订单号不一致，已取消',true);return;}
  api('/r/admin_api/order',{method:'POST',body:JSON.stringify({otn:otn,action:'mark',confirm:v})})
    .then(function(d){toast(d.msg||d.error||'完成',!d.ok);if(d.ok){loadOrders();loadOv();}});
}
async function loadPlans(){
  var d=await api('/r/admin_api/plans');if(!d.ok){$('planForm').innerHTML='<div class="card small">'+esc(d.error)+'</div>';return;}
  PLANS=d.plans||{};SKUS=d.skus||{};PACKS=d.packs||[];METER=d.meter||{};renderPlans();
}
let METER={};
var PACKS=[];
function packRow(p,i){
  return '<div class="row" style="margin-bottom:6px">'+
    '<input class="mini" id="pk-'+i+'-name" value="'+esc(p.name)+'" style="flex:1" placeholder="名称">'+
    '<input class="mini" id="pk-'+i+'-money" type="number" step="0.01" min="0" value="'+esc(p.money)+
      '" style="width:72px" title="价格（元）">'+
    '<input class="mini" id="pk-'+i+'-min" type="number" min="1" step="60" value="'+esc(p.min)+
      '" style="width:80px" title="额度（分钟）">'+
    '<label class="meta" style="flex:none" title="取消勾选=内部包：前台不展示、买不到，只能后台加时长">'+
      '<input type="checkbox" id="pk-'+i+'-sell"'+(p.sell===false?'':' checked')+
      ' style="width:auto;margin-right:3px">售卖</label>'+
    '<button class="gray" style="flex:none;padding:6px 10px" onclick="packDel('+i+')">删</button></div>';
}
function packSync(){
  PACKS=PACKS.map(function(p,i){
    var n=$('pk-'+i+'-name'),m=$('pk-'+i+'-money'),u=$('pk-'+i+'-min'),
        s=$('pk-'+i+'-sell');
    var o={sku:p.sku,name:n?n.value:p.name,money:m?Number(m.value||0):p.money,
           min:u?Number(u.value||0):p.min};
    if(s&&!s.checked)o.sell=false;
    return o;
  });
}
function packAdd(){
  packSync();
  PACKS.push({sku:'pack_'+Date.now().toString(36),
              name:'使用时长包 '+(PACKS.length+1),money:0,min:360});
  renderPlans();
}
function packDel(i){ packSync(); PACKS.splice(i,1); renderPlans(); }
/* 把当前表单写回 PLANS（加档、重渲染前先收一次，别丢用户刚改的输入） */
function planSync(){
  Object.keys(PLANS).forEach(function(k){
    if(k==='skus'||k==='packs')return;
    var nm=$('plan-'+k+'-name');if(!nm)return;
    var p=PLANS[k]||(PLANS[k]={limits:{},features:{}});
    p.name=nm.value;p.limits=p.limits||{};p.features=p.features||{};
    ['hosts','clients','llm_day','use_min_day','sends_day','asr_sec_day'].forEach(function(x){
      var el=$('plan-'+k+'-lim-'+x);if(el)p.limits[x]=Number(el.value||0);});
    ['terminal','remind','auto'].forEach(function(x){
      var el=$('plan-'+k+'-ft-'+x);if(el)p.features[x]=!!el.checked;});
    var sl=$('plan-'+k+'-sell');
    if(sl){if(sl.checked)delete p.sell;else p.sell=false;}
  });
}
/* 新档默认就是「内部」：不展示也不售卖，免得手一滑就上了价目表 */
function planAdd(){
  planSync();
  var n=1,k;while(PLANS['vip'+n])n++;k='vip'+n;
  var base=PLANS.pro||{};
  PLANS[k]={name:'内部套餐 '+n,sell:false,
            limits:JSON.parse(JSON.stringify(base.limits||{hosts:5,clients:5,llm_day:500,use_min_day:0})),
            features:JSON.parse(JSON.stringify(base.features||{terminal:true,remind:true,auto:true}))};
  renderPlans();
  var el=$('plan-'+k+'-name');if(el)try{el.focus();el.select();}catch(e){}
}
function renderPlans(){
  var LIM=[['hosts','开发机数'],['clients','客户端数'],
           ['llm_day','模型次/天'],['use_min_day','写操作分钟/天'],
           ['sends_day','消息条/天'],['asr_sec_day','语音秒/天']];
  var FT=[['terminal','终端'],['remind','进度提醒'],['auto','自动化']];
  var h='';
  Object.keys(PLANS).forEach(function(k){
    if(k==='skus'||k==='packs')return;
    var p=PLANS[k]||{},lim=p.limits||{},ft=p.features||{};
    h+='<div class="card"><div class="row"><b style="flex:none">'+esc(k)+'</b>'+
      '<input class="mini" id="plan-'+k+'-name" value="'+esc(p.name||k)+'" style="flex:1">'+
      '<label class="meta" style="flex:none;margin-left:6px" title="取消勾选=内部档：套餐页不展示、买不到，只能在用户管理里人工分配">'+
        '<input type="checkbox" id="plan-'+k+'-sell"'+(p.sell===false?'':' checked')+
        ' style="width:auto;margin-right:3px">售卖</label></div>'+
      '<div class="grid2">'+LIM.map(function(x){
        return '<div><div class="ph2">'+x[1]+'</div><input class="mini" type="number" min="0" id="plan-'+
          k+'-lim-'+x[0]+'" value="'+esc(lim[x[0]]==null?0:lim[x[0]])+'" style="width:100%"></div>';
      }).join('')+'</div><div class="row" style="margin-top:8px">'+FT.map(function(x){
        return '<label class="meta" style="flex:1"><input type="checkbox" id="plan-'+k+'-ft-'+x[0]+'"'+
          (ft[x[0]]?' checked':'')+' style="width:auto;margin-right:4px">'+x[1]+'</label>';
      }).join('')+'</div></div>';
  });
  h=meterCard('meter')+h;      // 计费刻度放最上面：最常改的一项，别埋在套餐列表底部
  h+='<div class="card"><button class="gray" style="width:100%" onclick="planAdd()">＋ 添加内部套餐（不对外展示）</button>'+
    '<div class="small" style="margin-top:6px">内部档（取消勾选「售卖」）：套餐页看不到、不能下单；'+
    '在「用户」里用下拉框人工分配给指定账号，照样按这套额度生效。</div></div>';
  h+='<div class="card"><div class="meta" style="margin-bottom:6px">订阅商品（名称 / 价格 / 天数）</div>'+
    Object.keys(SKUS).map(function(k){
      var s=SKUS[k];
      return '<div class="row"><span class="meta" style="flex:none;width:96px">'+esc(k)+'</span>'+
        '<input class="mini" id="sku-'+k+'-name" value="'+esc(s.name)+'" style="flex:1">'+
        '<input class="mini" id="sku-'+k+'-money" type="number" step="0.01" min="0" value="'+esc(s.money)+'" style="width:78px">'+
        '<input class="mini" id="sku-'+k+'-days" type="number" min="0" value="'+esc(s.days)+'" style="width:64px"></div>';
    }).join('')+'</div>';
  h+='<div class="card"><div class="meta" style="margin-bottom:6px">使用时长包档位（名称 / 价格 / 额度分钟）'+
    '——可随意增删；<b>第一档是基础档</b>，其余档的折扣都自动跟第一档的单价算</div>'+
    '<div class="row" style="margin-bottom:4px"><span class="meta" style="flex:1">名称</span>'+
    '<span class="meta" style="width:72px">价格</span><span class="meta" style="width:80px">分钟</span>'+
    '<span style="width:34px;flex:none"></span></div>'+
    PACKS.map(packRow).join('')+
    '<button class="gray" style="width:100%;margin-top:6px" onclick="packAdd()">＋ 添加档位</button></div>';
  h+='<div class="card"><button style="width:100%" onclick="savePlans()">保存套餐与价格</button>'+
    '<div class="small" style="margin-top:6px">保存后新订单立即按新价；额度最多 5 分钟下发到开发机兜底策略。</div></div>';
  $('planForm').innerHTML=h;
}
/* 计费刻度（计时单位时长）。套餐页和用量页各放一份，谁打开哪个页都能直接改。 */
function meterCard(pfx){
  var u=(METER&&METER.unit_sec)||(METER&&METER.default)||3;
  var mn=(METER&&METER.min)||1,mx=(METER&&METER.max)||60;
  return '<div class="card"><div class="meta" style="margin-bottom:6px">计费刻度 · 计时单位时长（秒）</div>'+
    '<div class="row"><input class="mini" id="'+pfx+'Unit" type="number" min="'+mn+
    '" max="'+mx+'" value="'+u+'" style="width:90px;margin:0">'+
    '<span class="small" style="flex:1">当前：<b>'+u+' 秒 / 单位</b></span>'+
    '<button class="gray" style="flex:none" onclick="applyMeter(\''+pfx+'\')">应用刻度</button></div>'+
    '<div class="small" style="margin-top:6px">一个刻度窗口里有操作就记 <b>1 个单位</b>（= 这里的秒数）。'+
    '默认 <b>3 秒</b>，范围 '+mn+'~'+mx+'；<b>60 就是以前的「按分钟」口径</b>。'+
    '改完立刻生效，只影响之后的时间窗——已经记下的账不变，不用改套餐里的「远程分钟/天」。</div></div>';
}
function renderUsageMeter(){
  if($('usageMeter'))$('usageMeter').innerHTML=meterCard('meter2');
}
function applyMeter(pfx){
  var el=$(pfx+'Unit');var v=Number(el&&el.value||0);
  if(!(v>=1&&v<=60)){toast('刻度要在 1~60 秒之间',true);return;}
  api('/r/admin_api/plans',{method:'POST',body:JSON.stringify({meter:{unit_sec:Math.round(v)}})})
    .then(function(d){
      toast(d.msg||d.error||(d.ok?('计费刻度已改为 '+Math.round(v)+' 秒'):'保存失败'),!d.ok);
      if(d.ok){METER=d.meter||METER;if(CT==='plans')renderPlans();renderUsageMeter();}
    });
}
function savePlans(){
  planSync();                       /* 表单 → PLANS（含「售卖」勾选） */
  var out={};
  Object.keys(PLANS).forEach(function(k){
    if(k==='skus'||k==='packs')return;
    var src=PLANS[k]||{},p={name:String(src.name||k).trim()||k,limits:{},features:{}};
    ['hosts','clients','llm_day','use_min_day'].forEach(function(x){
      p.limits[x]=Number((src.limits||{})[x]||0);});
    ['terminal','remind','auto'].forEach(function(x){
      p.features[x]=!!(src.features||{})[x];});
    if(src.sell===false)p.sell=false;
    out[k]=p;
  });
  var sk={};
  Object.keys(SKUS).forEach(function(k){
    sk[k]={name:$('sku-'+k+'-name').value.trim()||SKUS[k].name,
           money:Number($('sku-'+k+'-money').value||0),
           days:Number($('sku-'+k+'-days').value||0)};
  });
  out.skus=sk;
  packSync();
  out.packs=PACKS.map(function(p){
    return {sku:p.sku,name:(p.name||'').trim(),money:Number(p.money||0),min:Number(p.min||0)};});
  api('/r/admin_api/plans',{method:'POST',body:JSON.stringify({plans:out})}).then(function(d){
    toast(d.msg||d.error||(d.ok?'已保存':'保存失败'),!d.ok);
    if(d.ok){PLANS=d.plans;SKUS=d.skus;PACKS=d.packs||[];renderPlans();}
  });
}
async function loadUsage(){
  var d=await api('/r/admin_api/usage?days=14');if(!d.ok)return;
  if(d.meter)METER=d.meter;
  renderUsageMeter();
  var mx=1;d.series.forEach(function(x){mx=Math.max(mx,(x.use_sec||x.use_min*60),x.asr_sec/60);});
  $('usageSeries').innerHTML='<table class="tbl"><tr><th>日期</th><th>远程</th><th>语音</th><th>模型</th></tr>'+
    d.series.map(function(x){
      var us=(x.use_sec||x.use_min*60);
      return '<tr><td>'+x.date.slice(5)+'</td><td>'+dur(us)+
        '<div class="bar" style="width:'+Math.round(100*us/mx)+'%"></div></td>'+
        '<td>'+dur(x.asr_sec)+'<div class="bar" style="width:'+Math.round(100*(x.asr_sec/60)/mx)+
        '%;background:var(--blue)"></div></td><td>'+x.llm+'</td></tr>';
    }).join('')+'</table>';
  $('usageUsers').innerHTML=(d.today_users||[]).map(function(u){
    return '<div class="lrow" style="cursor:default"><div>'+esc(u.name)+' <span class="small">uid '+u.id+
      '</span></div><div class="small">'+dur(u.use_sec||u.use_min*60)+' · 语音 '+dur(u.asr_sec)+' · '+u.llm+' 次</div></div>';
  }).join('')||'<div class="small">今天还没有用量</div>';
}
/* ── 功能使用（页面埋点按天聚合）── */
var AN={days:30};
var ANLV={page_open:'打开页面',send_msg:'发消息',ptt:'按住说话',asr_result:'语音转写',
  key:'按键',cmd_run:'跑命令',cmd_scan:'识别命令',sess_new:'新建会话',sess_switch:'切会话',
  settings_open:'打开设置',mute_on:'静音开',mute_off:'静音关',dnd_on:'免打扰开',
  dnd_off:'免打扰关',smart_on:'智能提醒开',smart_off:'智能提醒关',npref:'提醒开关',
  nh_open:'提醒历史',model_open:'模型面板',model_switch:'切模型',pay:'打开购买',
  pair:'配对电脑',bind_email:'绑邮箱',bind_phone:'绑手机',dev_switch:'切开发机',
  media_open:'看图片视频',view_switch:'切视图',push_set:'推送开关',feedback:'打开反馈',
  feedback_submit:'提交反馈',stl_view:'看 3D 模型',visit:'进入页面',dwell:'页面停留',
  home_tap:'工作台点击',sess_tap:'会话页点击',set_tap:'设置项点击',term_tap:'终端页点击'};
function anName(ev){return ANLV[ev]||ev;}
/* 页面维：pg 是页面代码（home/sess/sub 子页…），报表里给人看中文 */
var ANPG={home:'工作台',sess:'会话页',term:'终端页',set:'设置页',files:'文件页',
  test:'验收页',deploy:'部署页'};
function anPg(p){return ANPG[p]||p||'—';}
function anDur(s){s=Number(s||0);if(s<60)return s+' 秒';
  var m=Math.floor(s/60);if(m<60)return m+' 分';
  return Math.floor(m/60)+' 时 '+m%60+' 分';}
function anSet(d){
  if(d)AN.days=d;
  ['an7','an30','an90'].forEach(function(id,i){
    $(id).className=(AN.days===[7,30,90][i]?'':'gray');});
  loadAn();
}
function anBar(n,mx,c){
  return '<div class="bar" style="width:'+Math.max(1,Math.round(100*(n||0)/Math.max(1,mx)))+'%'+
    (c?';background:'+c:'')+'"></div>';
}
function anDim(list,k,label,fmt){
  if(!list.length)return '';
  var mx=list[0].n||1;
  return '<div class="ph2">'+label+'</div>'+list.map(function(x){
    return '<div class="row" style="cursor:default"><div style="flex:1">'+
      esc(fmt?fmt(x[k]):(x[k]||'—'))+'</div><div style="flex:2">'+x.n+
      anBar(x.n,mx,'var(--blue)')+'</div><div class="small" style="flex:none">'+
      (x.devs!==undefined?x.devs+' 设备':'')+'</div></div>';}).join('');
}
async function loadAn(){
  var d=await api('/r/admin_api/analytics?days='+AN.days+
    '&prod='+encodeURIComponent(($('anProd')||{}).value||''));if(!d.ok)return;
  var tot=d.daily.reduce(function(a,x){return a+x.n;},0);
  $('anHint').textContent=d.today.day+' · 近 '+d.days+' 天 '+tot+' 次事件';
  $('anTop').innerHTML=
    '<div class="row" style="cursor:default"><div style="flex:1">今日活跃账号</div><div>'+d.today.users+'</div></div>'+
    '<div class="row" style="cursor:default"><div style="flex:1">今日活跃设备</div><div>'+d.today.devs+'</div></div>'+
    '<div class="row" style="cursor:default"><div style="flex:1">今日事件</div><div>'+d.today.n+'</div></div>';
  var mx=1;d.daily.forEach(function(x){mx=Math.max(mx,x.devs,x.users);});
  $('anDaily').innerHTML='<table class="tbl"><tr><th>日期</th><th>活跃设备</th><th>活跃账号</th><th>事件</th></tr>'+
    d.daily.map(function(x){
      return '<tr><td>'+x.d.slice(5)+'</td><td>'+x.devs+anBar(x.devs,mx)+
        '</td><td>'+x.users+'</td><td>'+x.n+'</td></tr>';}).join('')+
    '</table>'+(d.daily.length?'':'<div class="small">还没有数据（新版页面打点后才会出现）</div>');
  var mxs=1;(d.pages||[]).forEach(function(x){mxs=Math.max(mxs,x.secs);});
  $('anPages').innerHTML=(d.pages||[]).map(function(x){
    return '<div class="row" style="cursor:default"><div style="flex:1">'+esc(anPg(x.pg))+
      '<div class="small">进入 '+x.visits+' 次 · 平均 '+anDur(x.avg)+'</div></div>'+
      '<div style="flex:2">'+anDur(x.secs)+anBar(x.secs,mxs,'var(--blue)')+'</div></div>';
  }).join('')||'<div class="small">还没有页面数据（新版页面打点后才会出现）</div>';
  $('anDet').innerHTML=(d.evdet||[]).map(function(x){
    return '<div class="row" style="cursor:default"><div style="flex:1">'+
      esc(anName(x.ev))+(x.k?' · '+esc(x.k):'')+
      '<div class="small">'+esc(anPg(x.pg))+' · '+x.devs+' 设备</div></div>'+
      '<div style="flex:none">'+x.n+' 次</div></div>';
  }).join('')||'<div class="small">还没有数据</div>';
  var me=d.fev.length?(d.fev[0].n||1):1;
  $('anEv').innerHTML=d.fev.map(function(x){
    return '<div class="row" style="cursor:default"><div style="flex:1">'+esc(anName(x.ev))+
      '<div class="small">'+esc(x.ev)+' · '+x.users+' 账号 · '+x.devs+' 设备</div></div>'+
      '<div style="flex:2">'+x.n+anBar(x.n,me,'var(--blue)')+'</div></div>';}).join('')||
    '<div class="small">还没有数据</div>';
  $('anDim').innerHTML=
    anDim(d.byprod,'p','产品线',function(v){return v==='agentboss'?'代理工头':(v==='codeboss'?'口袋工头':v);})+
    anDim(d.byver,'v','客户端版本')+
    anDim(d.byplat,'p','平台',function(v){return v==='android'?'安卓':(v==='ios'?'iOS':v);});
}
async function loadAlerts(){
  var d=await api('/r/admin_api/alerts');if(!d.ok)return;
  var c=d.cfg||{};
  $('al_pay').checked=c.pay!==false;
  $('al_reg').checked=c.reg!==false;
  $('al_email').value=c.email||'';
  $('al_ding').value=c.dingtalk||'';
  $('al_wecom').value=c.wecom||'';
  $('al_sc').value=c.serverchan||'';
  $('al_pp').value=c.pushplus||'';
  $('al_state').textContent='SMTP '+(d.smtp&&d.smtp.on?'已配置（'+d.smtp.user+'）':'未配置')+
    ' · 打码字段（***结尾）保存时保持原值不变';
}
function saveAlerts(test){
  var body={pay:$('al_pay').checked,reg:$('al_reg').checked,email:$('al_email').value.trim(),
    dingtalk:$('al_ding').value.trim(),wecom:$('al_wecom').value.trim(),
    serverchan:$('al_sc').value.trim(),pushplus:$('al_pp').value.trim()};
  if(test)body.action='test';
  api('/r/admin_api/alerts',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    toast(d.msg||d.error||'已保存',!d.ok);if(d.ok)loadAlerts();});
}
async function loadStore(){
  var d=await api('/r/admin_api/storage');if(!d.ok)return;
  var c=d.cfg||{};
  $('os_on').checked=!!c.on;$('os_ep').value=c.endpoint||'';$('os_bk').value=c.bucket||'';
  $('os_ak').value=c.ak||'';$('os_sk').value=c.sk||'';$('os_pf').value=c.prefix||'codeboss/';
  $('os_state').textContent=d.ready?'状态：已启用，备份/附件会同时写到对象存储':
    '状态：未启用或配置不完整，文件只留在本机磁盘';
}
function osSave(test){
  var body={action:test?'test':'',on:$('os_on').checked,
    endpoint:$('os_ep').value.trim(),bucket:$('os_bk').value.trim(),
    ak:$('os_ak').value.trim(),sk:$('os_sk').value.trim(),prefix:$('os_pf').value.trim()};
  $('os_state').textContent='保存中…';
  api('/r/admin_api/storage',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    $('os_state').textContent=(d.ok?'✅ ':'❌ ')+(d.msg||d.error||'已保存');
    toast(d.msg||d.error||'已保存',!d.ok);if(d.ok)loadStore();
  }).catch(function(){$('os_state').textContent='❌ 保存失败';});
}
/* ---- 文件浏览：OSS 与本机 ---- */
var FL=[],FL_WHERE='oss',FL_SUB='backup';
function flSize(n){
  n=Number(n||0);
  if(n<1024)return n+' B';
  if(n<1048576)return (n/1024).toFixed(1)+' KB';
  if(n<1073741824)return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
function flLoad(){
  var v=$('fl_where').value||'oss:backup',p=v.split(':');
  FL_WHERE=p[0];FL_SUB=p[1]||'backup';
  var url='/r/admin_api/files?where='+FL_WHERE;
  if(FL_WHERE==='oss'){
    var pf=($('os_pf').value||'').trim()||'codeboss/';
    if(pf[pf.length-1]!=='/')pf+='/';
    url+='&prefix='+encodeURIComponent(FL_SUB==='all'?pf:pf+FL_SUB+'/');
  }else{
    url+='&dir='+encodeURIComponent(FL_SUB);
  }
  $('fl_state').textContent='读取中…';
  $('fl_preview').style.display='none';
  api(url).then(function(d){
    if(!d.ok){$('fl_state').textContent='❌ '+(d.error||'读取失败');$('fl_list').innerHTML='';return;}
    FL=d.files||[];
    $('fl_state').textContent=(FL_WHERE==='oss'?('OSS 前缀 '+d.prefix+' · '):('本机 '+FL_SUB+' · '))+
      FL.length+' 个文件'+(d.next?'（只显示前 300 个）':'');
    $('fl_list').innerHTML=FL.map(function(f,i){
      var nm=f.name||f.key;
      return '<div class="lrow" style="cursor:default"><div><b>'+esc(nm)+'</b>'+
        '<div class="small">'+flSize(f.size)+' · '+(f.mtime?ago(f.mtime):'')+
        (f.storage?' · '+esc(f.storage):'')+'</div></div>'+
        '<div style="text-align:right;white-space:nowrap">'+
        '<button class="gray" onclick="flView('+i+')">查看</button>'+
        '</div></div>';
    }).join('')||'<div class="small">目录是空的</div>';
  }).catch(function(e){$('fl_state').textContent='❌ '+(e.message||'读取失败');});
}
function flView(i){
  var f=FL[i];if(!f)return;
  var url='/r/admin_api/file?where='+FL_WHERE+'&'+
    (FL_WHERE==='oss'?('key='+encodeURIComponent(f.key)):
                      ('dir='+encodeURIComponent(FL_SUB)+'&name='+encodeURIComponent(f.name)));
  $('fl_state').textContent='读取中…';
  fetch(url,{headers:H()}).then(function(r){
    if(!r.ok)throw new Error('HTTP '+r.status);
    var ct=r.headers.get('Content-Type')||'';
    if(/^text\//.test(ct)||ct.indexOf('json')>=0){
      return r.text().then(function(t){
        $('fl_state').textContent='';
        $('fl_preview').textContent=t.length>200000?t.slice(0,200000)+'\n…（已截断）':t;
        $('fl_preview').style.display='block';
      });
    }
    return r.blob().then(function(b){
      var a=document.createElement('a');a.href=URL.createObjectURL(b);
      a.download=f.name||'file';
      document.body.appendChild(a);a.click();
      setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},3000);
      $('fl_state').textContent='已开始下载：'+(f.name||'');
    });
  }).catch(function(e){$('fl_state').textContent='❌ '+(e.message||'读取失败');});
}
async function loadModels(){
  var d=await api('/r/admin_api/models');if(!d.ok)return;
  var c=d.cfg||{};
  $('md_key').value=c.key||'';
  $('md_asr').value=c.asr||'';$('md_tts').value=c.tts||'';$('md_voice').value=c.voice||'';
  $('md_llm').value=c.llm||'';$('md_fix').value=c.fix||'';$('md_base').value=c.llm_base||'';
  $('md_state').textContent=d.key_ready?'状态：密钥已配置':'状态：还没配置 API Key，语音和模型功能不可用';
}
function mdSave(test){
  var body={action:test?'test':'',key:$('md_key').value.trim(),
    asr:$('md_asr').value.trim(),tts:$('md_tts').value.trim(),voice:$('md_voice').value.trim(),
    llm:$('md_llm').value.trim(),fix:$('md_fix').value.trim(),llm_base:$('md_base').value.trim()};
  $('md_state').textContent='保存中…';
  api('/r/admin_api/models',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    $('md_state').textContent=(d.ok?'✅ ':'❌ ')+(d.msg||d.error||'已保存');
    toast(d.msg||d.error||'已保存',!d.ok);if(d.ok)loadModels();
  }).catch(function(){$('md_state').textContent='❌ 保存失败';});
}
let TASKS=[],TKINDS={};
var CH=['email','dingtalk','wecom','serverchan','pushplus'];
var CHN={email:'邮件',dingtalk:'钉钉',wecom:'企业微信',serverchan:'Server酱',pushplus:'PushPlus'};
function chText(list){
  if(!list||!list.length)return '全部已配置渠道';
  return list.map(function(k){return CHN[k]||k;}).join('、');
}
/* crontab 表达式的人话说明（常见形态才翻，翻不了就原样显示） */
function cronText(e){
  var f=String(e||'').trim().split(/\s+/);
  if(f.length!==5)return e;
  var wd={'0':'周日','1':'周一','2':'周二','3':'周三','4':'周四','5':'周五','6':'周六','7':'周日'};
  var pad=function(x){x=String(x);return x.length<2?'0'+x:x;};
  var m=/^\*\/(\d+)$/.exec(f[0]);
  if(m&&f[1]==='*'&&f[2]==='*'&&f[3]==='*'&&f[4]==='*')return '每 '+m[1]+' 分钟';
  if(/^\d+$/.test(f[0])&&f[1]==='*'&&f[2]==='*'&&f[3]==='*'&&f[4]==='*')return '每小时第 '+Number(f[0])+' 分';
  if(/^\d+$/.test(f[0])&&/^\d+$/.test(f[1])){
    var t=pad(f[1])+':'+pad(f[0]);
    if(f[2]==='*'&&f[3]==='*'&&f[4]==='*')return '每天 '+t;
    if(f[2]==='*'&&f[3]==='*'&&/^[0-7]$/.test(f[4]))return '每'+wd[f[4]]+' '+t;
  }
  return e;
}
async function loadTasks(){
  var d=await api('/r/admin_api/tasks');
  if(!d.ok){$('taskList').textContent=d.error||'读取失败';return;}
  TASKS=d.tasks||[];TKINDS={};
  (d.kinds||[]).forEach(function(k){TKINDS[k.k]=k.label;});
  $('tk_kind').innerHTML=(d.kinds||[]).map(function(k){
    return '<option value="'+esc(k.k)+'">'+esc(k.label)+'</option>';}).join('');
  $('taskList').innerHTML=TASKS.map(function(t){
    var every=Math.max(1,Math.round(t.every_s/60));
    var when=t.cron?'定时 <b>'+esc(cronText(t.cron))+'</b> <span class="small">'+esc(t.cron)+'</span>':
      '每 '+every+' 分钟';
    var st=t.last_ok===1?'<span class="tag g">成功</span>':
      (t.last_ok===0?'<span class="tag r">失败</span>':'<span class="tag">未跑过</span>');
    return '<div class="lrow" style="cursor:default"><div><b>'+esc(t.name)+'</b> '+
      (t.enabled?'':'<span class="tag">已暂停</span>')+
      '<div class="small">'+esc(TKINDS[t.kind]||t.kind)+' · '+when+
      (t.last_run?' · 上次 '+ago(t.last_run):'')+
      ' · 失败通知：'+esc(chText(t.notify))+'</div>'+
      '<div class="small">'+st+' '+esc(t.last_msg||'')+'</div></div>'+
      '<div style="text-align:right;white-space:nowrap">'+
      '<button class="gray" onclick="tkRun(\''+t.id+'\')">立即执行</button> '+
      '<button class="gray" onclick="tkHist(\''+t.id+'\')">历史</button> '+
      '<button class="gray" onclick="tkToggle(\''+t.id+'\')">'+(t.enabled?'暂停':'开启')+'</button> '+
      '<button class="gray" onclick="tkEvery(\''+t.id+'\','+Number(t.every_s||0)+')">改间隔</button>'+
      ' <button class="gray" onclick="tkCron(\''+t.id+'\',\''+esc(t.cron||'')+'\')">改定时</button>'+
      ' <button class="gray" onclick="tkNotify(\''+t.id+'\')">通知</button>'+
      (t.kind==='db_backup'?'':' <button class="gray" onclick="tkDel(\''+t.id+'\')">删除</button>')+
      '</div></div>';
  }).join('')||'<div class="small">还没有任务</div>';
}
function tkPost(body,msgBox){
  return api('/r/admin_api/tasks',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    if(msgBox)$(msgBox).textContent=(d.ok?'':'❌ ')+(d.msg||d.error||'');
    toast(d.msg||d.error||'已完成',!d.ok);
    loadTasks();return d;
  });
}
function tkCreate(){
  var every=Math.max(1,Number($('tk_every').value)||360);
  var cron=($('tk_cron').value||'').trim();
  tkPost({action:'create',kind:$('tk_kind').value,name:$('tk_name').value.trim(),
    every_s:every*60,cron:cron},'tk_new_state').then(function(d){
      if(d&&d.ok)$('tk_cron').value='';});
}
function tkRun(id){
  $('taskList').style.opacity=.55;$('tk_state').textContent='正在执行，稍候…';
  api('/r/admin_api/tasks',{method:'POST',body:JSON.stringify({action:'run',id:id})})
    .then(function(d){
      $('taskList').style.opacity=1;
      $('tk_state').textContent=(d.ran_ok?'✅ ':'❌ ')+(d.msg||d.error||'');
      loadTasks();
    }).catch(function(){$('taskList').style.opacity=1;$('tk_state').textContent='❌ 执行失败';});
}
function tkToggle(id){tkPost({action:'toggle',id:id});}
function tkDel(id){
  if(!confirm('删除这个计划任务？'))return;
  tkPost({action:'delete',id:id});
}
function tkEvery(id,cur){
  var m=prompt('新的执行间隔（分钟）：',String(Math.max(1,Math.round((cur||3600)/60))));
  var n=Number(m);
  if(!m||!isFinite(n)||n<1)return;
  tkPost({action:'save',id:id,every_s:Math.round(n*60),cron:''});
}
function tkCron(id,cur){
  var s=prompt('crontab 定时（分 时 日 月 周）：\n'+
    '例：0 3 * * * 每天 03:00；30 */6 * * * 每 6 小时的第 30 分；\n'+
    '0 4 * * 1 每周一 04:00；0 3 1 * * 每月 1 号 03:00。\n'+
    '留空 = 改回按间隔执行。',cur||'');
  if(s===null)return;
  tkPost({action:'save',id:id,cron:s.trim()});
}
function tkHist(id){
  var name=id;
  TASKS.forEach(function(x){if(x.id===id)name=x.name;});
  api('/r/admin_api/tasks?runs='+encodeURIComponent(id)).then(function(d){
    if(!d.ok){$('tk_hist').textContent=d.error||'读取失败';return;}
    var rows=(d.runs||[]).map(function(r){
      return '<div style="margin:3px 0">'+
        (r.ok?'<span class="tag g">成功</span>':'<span class="tag r">失败</span>')+
        ' '+ago(r.started)+' · 耗时 '+Number(r.dur||0).toFixed(1)+'s · '+
        (r.trigger==='manual'?'手动执行':'定时执行')+
        (r.msg?' · '+esc(r.msg):'')+'</div>';
    }).join('')||'<div class="small">还没有执行记录</div>';
    $('tk_hist').innerHTML='<div class="meta" style="margin:10px 0 4px">'+esc(name)+
      ' · 最近执行（最多 100 条）</div>'+rows;
  });
}
function tkNotify(id){
  var t=null;
  TASKS.forEach(function(x){if(x.id===id)t=x;});
  var cur=(t&&t.notify)||[];
  var s=prompt('失败时通知哪些渠道？（逗号分隔，可多选）\n'+
    '可选：email 邮件、dingtalk 钉钉、wecom 企业微信、serverchan Server酱、pushplus PushPlus\n'+
    '留空 = 所有已配置渠道。每个渠道里配了几个目标都会各发一份。',cur.join(','));
  if(s===null)return;
  var list=s.split(/[,;\s]+/).filter(function(x){return CH.indexOf(x)>=0;});
  tkPost({action:'save',id:id,notify:list});
}
function pset(k,v){var e=$(k);if(!e)return;
  if(e.type==='checkbox')e.checked=!!v;else e.value=v==null?'':v;}
function pget(k){var e=$(k);return e?(e.type==='checkbox'?e.checked:e.value):'';}
function polLoad(){pload();adSecLoad();promoLoad();}
/* ── 注册赠送策略：datetime-local 的值是本地时间 YYYY-MM-DDTHH:mm，存库用 epoch 秒 ── */
function _ts2local(ts){var d=new Date(ts*1000);function z(n){return (n<10?'0':'')+n}
  return d.getFullYear()+'-'+z(d.getMonth()+1)+'-'+z(d.getDate())+'T'+z(d.getHours())+':'+z(d.getMinutes());}
function _local2ts(s){var t=new Date(s).getTime();return isNaN(t)?0:Math.round(t/1000)}
function promoLoad(){
  api('/r/admin_api/promo').then(function(d){
    if(!d.ok){$('pr_e').textContent=d.error||'读取失败';return;}
    var p=d.promo||{};
    pset('pr_en',!!p.enabled);pset('pr_banner',!!p.banner);
    pset('pr_plan',p.plan||'pro');pset('pr_days',p.days||30);
    pset('pr_note',p.note||'');
    $('pr_until').value=p.until?_ts2local(p.until):'';
    var st=p.enabled?('开启中：送 '+p.days+' 天 '+(p.plan==='max'?'Max':'Pro'))
                     :'已关闭（新注册直接是免费版）';
    if(p.enabled&&p.until){
      st+=p.until*1000>Date.now()?(' · 活动至 '+_ts2local(p.until).replace('T',' ')):' · 活动已到期（不再赠送）';}
    else if(p.enabled){st+=' · 长期';}
    $('pr_state').textContent=st;
    $('pr_e').textContent='';
  }).catch(function(){$('pr_e').textContent='读取失败';});
}
function promoSave(){
  var days=parseInt($('pr_days').value||'0',10);
  if($('pr_en').checked&&!(days>0)){toast('开了赠送，天数就要大于 0',true);return;}
  var us=$('pr_until').value||'', until=us?_local2ts(us):0;
  if(us&&!until){toast('活动截止时间不正确',true);return;}
  var body={enabled:$('pr_en').checked,plan:$('pr_plan').value,days:days,until:until,
            banner:$('pr_banner').checked,note:$('pr_note').value.trim()};
  $('pr_e').textContent='保存中…';
  api('/r/admin_api/promo',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    if(!d.ok){$('pr_e').textContent=d.error||'保存失败';return;}
    $('pr_e').textContent='';promoLoad();toast('注册赠送已更新');
  }).catch(function(){$('pr_e').textContent='保存失败（网络）';});
}
function adSecLoad(){
  api('/r/admin_api/security').then(function(d){
    if(!d.ok){$('ad_state').textContent=d.error||'读取失败';return;}
    $('ad_phone').value='';
    $('ad_phone').placeholder=d.phone||'未配置';
    $('ad_state').textContent='当前手机号：'+(d.configured?d.phone:'未配置')+
      ' · 登录密码：'+(d.pass_set?'已设置':'未设置')+
      ' · 短信通道：'+(d.sms?'可用':'未配置')+
      ' · 二次验证：每次敏感操作都要输验证码';
  }).catch(function(){$('ad_state').textContent='读取失败';});
}
async function adSecSave(){
  var p=($('ad_phone').value||'').trim(),w=$('ad_pass').value||'';
  if(!p&&!w){toast('填手机号或新密码',true);return;}
  if(p&&!/^1[3-9]\d{9}$/.test(p)){toast('手机号格式不正确',true);return;}
  var d=await api('/r/admin_api/security',{method:'POST',body:JSON.stringify({phone:p,password:w})});
  toast(d.ok?'已保存':(d.error||'保存失败'),!d.ok);
  if(d.ok){$('ad_pass').value='';adSecLoad();}
}
function pload(){
  api('/r/admin/notify_policy').then(function(d){
    if(!d.ok){$('pe').textContent=d.error||'策略读取失败';return;}
    var p=d.policy||{};
    $('pe').textContent='当前策略版本 v'+(p.ver||1);
    pset('p_en',p.enabled!==false);
    var K=p.kinds||{},S=p.sound||{},V=p.voice||{};
    ['done','perm','input','ask','limit','stuck'].forEach(function(k){
      pset('p_k_'+k,K[k]!==false);pset('p_s_'+k,S[k]||(k==='stuck'?'ping':k));pset('p_v_'+k,!!V[k]);
    });
    pset('p_pt',p.perm_throttle);pset('p_ia',p.idle_after);pset('p_mt',p.max_text);
    pset('p_sm',p.stuck_min);
    pset('p_dt',p.done_text);pset('p_ptext',p.perm_text);pset('p_it',p.input_text);
    pset('p_at',p.ask_text);pset('p_lt',p.limit_text);pset('p_st',p.stuck_text);
  }).catch(function(){$('pe').textContent='策略读取失败';});
}
function psave(){
  var kinds={},sound={},voice={};
  ['done','perm','input','ask','limit','stuck'].forEach(function(k){
    kinds[k]=pget('p_k_'+k);sound[k]=pget('p_s_'+k);voice[k]=pget('p_v_'+k);});
  var body={enabled:pget('p_en'),kinds:kinds,sound:sound,voice:voice,
    perm_throttle:Number(pget('p_pt'))||0,idle_after:Number(pget('p_ia'))||0,
    max_text:Number(pget('p_mt'))||160,stuck_min:Number(pget('p_sm'))||0,
    done_text:pget('p_dt'),perm_text:pget('p_ptext'),input_text:pget('p_it'),
    ask_text:pget('p_at'),limit_text:pget('p_lt'),stuck_text:pget('p_st')};
  $('pe').textContent='保存中…';
  api('/r/admin/notify_policy',{method:'POST',body:JSON.stringify(body)}).then(function(d){
    if(!d.ok){$('pe').textContent=d.error||'保存失败';return;}
    $('pe').textContent='已保存，策略版本 v'+((d.policy||{}).ver||'?');
  }).catch(function(){$('pe').textContent='保存失败（网络）';});
}
async function upl(){
  var f=$('uf').files[0];if(!f){$('ae').textContent='先选 '+FMT+' 文件';return;}
  $('ae').textContent='上传中…';
  var fd=new FormData();fd.append('f',f);
  var r=await fetch('/r/update',{method:'POST',headers:H(),body:fd});
  var d=await r.json();
  if(d.ok){$('ae').textContent='已更新（'+d.bytes+' bytes），服务重启中，5 秒后刷新…';
    setTimeout(function(){location.reload();},5000);}
  else $('ae').textContent=d.error||'更新失败';
}
async function pull(){
  $('ae').textContent='拉取中…';
  var r=await fetch('/r/update_pull',{method:'POST',headers:H()});
  var d=await r.json();
  if(d.ok&&d.changed===false){$('ae').textContent='已是最新'+(d.tag?('（'+d.tag+'）'):'')+'，无需更新';}
  else if(d.ok){$('ae').textContent='已更新'+(d.tag?('到 '+d.tag):'')+'（'+d.bytes+' bytes），服务重启中，5 秒后刷新…';
    setTimeout(function(){location.reload();},5000);}
  else $('ae').textContent=d.error||'更新失败';
}
async function mailtest(){
  $('mres').textContent='发送中…';
  var d=await api('/r/admin/mailtest',{method:'POST',body:JSON.stringify({to:$('mt').value.trim()})});
  $('mres').textContent=d.ok?('邮件已发送到 '+d.to):(d.error||'失败');
  toast(d.ok?'邮件已发送':(d.error||'失败'),!d.ok);
}
async function smstest(){
  $('mres').textContent='发送中…';
  var d=await api('/r/admin/smstest',{method:'POST',body:JSON.stringify({phone:$('mtel').value.trim()})});
  $('mres').textContent=d.ok?('短信已发送到 '+d.to):(d.error||'失败');
  toast(d.ok?'短信已发送':(d.error||'失败'),!d.ok);
}
boot();
</script>
</body></html>
"""


# 监控屏：给大屏/平板常开的一页，只读 /r/admin_api/services，10 秒一轮。
# 鉴权沿用后台会话：管理后台「服务监控」里点「打开监控屏」，新标签页会带上
# sessionStorage 里的管理令牌；手机号登录过的浏览器则直接走 cb_adm cookie。
_PAGE_BOARD = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeBoss · 服务监控屏</title><link rel="icon" type="image/png" href="/favicon.ico"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
:root{--bg:#080b10;--card:#0f141c;--line:#1d2531;--txt:#e6edf3;--mut:#8b949e;
      --ok:#3FB950;--warn:#D29922;--bad:#F85149;--unk:#8b949e;--off:#484f58;
      --blue:#58A6FF;--gy:#238636}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--txt)}
body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",
     "Segoe UI",Roboto,sans-serif;min-height:100vh;padding:18px 22px 14px}
header{display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:14px}
.brand{font-size:20px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.brand span{font-size:13px;font-weight:500;color:var(--mut);margin-left:8px}
.hsum{display:flex;gap:18px;flex-wrap:wrap;flex:1;justify-content:center}
.hsum .k{font-size:12px;color:var(--mut);text-align:center;line-height:1.2}
.hsum .k b{display:block;font-size:22px;color:var(--txt);font-variant-numeric:tabular-nums}
.clock{text-align:right;white-space:nowrap}
.clock b{font-size:26px;font-variant-numeric:tabular-nums;letter-spacing:1px}
.clock span{display:block;font-size:12px;color:var(--mut)}
.win{display:flex;gap:6px;flex-wrap:wrap}
.win select{background:#141a23;border:1px solid var(--line);color:var(--txt);
  border-radius:8px;padding:7px 9px;font:inherit;font-size:13px;outline:none}
#banner{display:none;margin:0 0 14px;padding:10px 14px;border-radius:10px;
        background:#3d1418;border:1px solid #f8514966;color:#ffb3ad;font-size:15px}
#banner.on{display:block}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(292px,1fr));gap:12px}
.svc{background:var(--card);border:1px solid var(--line);border-radius:12px;
     padding:13px 15px 11px;position:relative;overflow:hidden;min-height:172px;
     display:flex;flex-direction:column}
.svc:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--off)}
.svc.s-ok:before{background:var(--ok)}.svc.s-warn:before{background:var(--warn)}
.svc.s-fail:before{background:var(--bad)}.svc.s-unknown:before{background:var(--unk)}
.svc.s-off{opacity:.55}
.svc.s-fail{border-color:#f8514966;box-shadow:0 0 0 1px #f8514933 inset}
.svc h3{margin:0 0 2px;font-size:16px;font-weight:650;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;flex:none;background:var(--off)}
.s-ok .dot{background:var(--ok)}.s-warn .dot{background:var(--warn)}
.s-fail .dot{background:var(--bad);box-shadow:0 0 0 4px #f8514922}
.s-unknown .dot{background:var(--unk)}
.pill{margin-left:auto;font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;
      border:1px solid var(--line);color:var(--mut);white-space:nowrap}
.s-ok .pill{color:var(--ok);border-color:#2ea04355}
.s-warn .pill{color:var(--warn);border-color:#e3b34155}
.s-fail .pill{color:var(--bad);border-color:#f8514955}
.desc{font-size:11.5px;color:var(--mut);margin-bottom:9px;white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis}
.nums{display:flex;gap:14px;align-items:baseline;margin-bottom:6px}
.nums b{font-size:24px;font-variant-numeric:tabular-nums;line-height:1}
.nums small{font-size:11px;color:var(--mut);display:block;margin-top:2px}
.nums .rate{margin-left:auto;font-size:13px;color:var(--mut)}
.nums .rate b{font-size:18px;color:var(--txt)}
.meta{font-size:11.5px;color:var(--mut);font-variant-numeric:tabular-nums}
.err{font-size:11.5px;color:#ff9d97;margin-top:4px;white-space:nowrap;overflow:hidden;
     text-overflow:ellipsis}
.spark{display:flex;align-items:flex-end;gap:2px;height:26px;margin-top:auto;padding-top:8px}
.spark i{flex:1;min-width:2px;background:#2a3441;border-radius:1px 1px 0 0;height:2px}
.spark i.g{background:#2ea04399}.spark i.r{background:#f85149cc}
footer{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:14px;padding-top:10px;
       border-top:1px solid var(--line);font-size:12.5px;color:var(--mut)}
footer b{color:var(--txt);font-variant-numeric:tabular-nums}
footer .bad b{color:var(--bad)}
#gate{position:fixed;top:0;left:0;right:0;bottom:0;background:#080b10ee;display:none;align-items:center;
      justify-content:center;z-index:9}
#gate.on{display:flex}
#gate .box{max-width:460px;background:var(--card);border:1px solid var(--line);
           border-radius:14px;padding:26px 30px;text-align:center;line-height:1.7}
#gate a{color:var(--blue)}
@media(min-width:1500px){body{padding:26px 34px}.svc{min-height:190px}
  .svc h3{font-size:18px}.nums b{font-size:28px}}
</style></head><body>
<header>
  <div class="brand">CodeBoss 中继<span>服务监控</span></div>
  <div class="win">
    <span style="color:var(--mut);font-size:12.5px;align-self:center">粒度</span>
    <select id="gran">
      <option value="sec">秒</option><option value="min">分钟</option>
      <option value="hour" selected>小时</option><option value="day">天</option>
    </select>
    <span style="color:var(--mut);font-size:12.5px;align-self:center;margin-left:6px">跨度</span>
    <select id="span">
      <option value="3600">1 小时</option><option value="86400" selected>24 小时</option>
      <option value="604800">7 天</option><option value="2592000">30 天</option>
      <option value="15552000">6 个月</option>
    </select>
    <span style="color:var(--mut);font-size:12.5px;align-self:center;margin-left:6px">账号</span>
    <select id="fUid"><option value="">全部</option></select>
    <span style="color:var(--mut);font-size:12.5px;align-self:center;margin-left:6px">开发机</span>
    <select id="fDev"><option value="">全部</option></select>
    <span style="color:var(--mut);font-size:12.5px;align-self:center;margin-left:6px">客户端</span>
    <select id="fCid"><option value="">全部</option></select>
  </div>
  <div class="hsum" id="hsum"></div>
  <a href="javascript:fails()" style="align-self:center;color:var(--mut);font-size:13px">失败明细 ›</a>
  <div class="clock"><b id="clk">--:--:--</b><span id="cdate"></span></div>
</header>
<div id="banner"></div>
<div id="fhint" class="dim" style="color:var(--mut);font-size:12px;margin:-6px 0 10px"></div>
<main id="grid"></main>
<footer id="foot"></footer>
<div id="gate"><div class="box">
  <b>需要管理员登录</b><br>
  请先在 <a href="/r/admin" target="_blank">管理后台</a> 登录，再从「服务监控」页点
  「打开监控屏」；<br>或在地址后加 <code>?t=管理令牌</code> 打开一次。
</div></div>
<script>
var TOK="";
try{TOK=sessionStorage.getItem("cb_admin")||"";}catch(e){}
try{var _q=new URLSearchParams(location.search),_t=_q.get("t");
  if(_t){TOK=_t;try{sessionStorage.setItem("cb_admin",_t);}catch(e){}
    history.replaceState(null,"",location.pathname);}}catch(e){}
var SUM={ok:0,warn:0,fail:0,unknown:0,off:0}, UP=0, ONLINE=0, CLIENTS=0, LASTERR="";
var GRAN="hour", SPAN="86400";
try{ GRAN=sessionStorage.getItem("cb_board_gran")||"hour";
     SPAN=sessionStorage.getItem("cb_board_span")||"86400"; }catch(e){}
function fails(){ location.href="/r/admin/fails"+(TOK?("?t="+encodeURIComponent(TOK)):""); }
var FUID="", FDEV="", FCID="", DIMS={users:[], devices:[], clients:[]};
function fsel(id, arr, idKey, labelFn){
  var el=document.getElementById(id); if(!el) return;
  var cur=el.value;
  el.innerHTML='<option value="">全部</option>'+arr.map(function(x){
    return '<option value="'+esc(String(x[idKey]))+'">'+esc(labelFn(x))+'</option>';
  }).join('');
  el.value=cur;
}
function clientFill(){
  var el=document.getElementById("fCid"); if(!el) return;
  var cur=el.value;
  var arr=DIMS.clients.filter(function(c){return !FUID||String(c.uid)===String(FUID);});
  el.innerHTML='<option value="">全部</option>'+arr.map(function(c){
    return '<option value="'+esc(c.cid)+'">'+esc((c.user?c.user+" · ":"")+(c.label||c.cid.slice(0,6)))+'</option>';
  }).join("");
  el.value=arr.some(function(x){return x.cid===cur;})?cur:"";
}
function loadDims(){
  var opt={headers:TOK?{Authorization:"Bearer "+TOK}:{},credentials:"same-origin",cache:"no-store"};
  fetch("/r/admin_api/users",opt).then(function(r){return r.json();}).then(function(d){
    DIMS.users=d.users||[]; fsel("fUid",DIMS.users,"id",function(x){return x.name||("#"+x.id);});
  }).catch(function(){});
  fetch("/r/admin_api/devices",opt).then(function(r){return r.json();}).then(function(d){
    DIMS.devices=d.devices||[]; fsel("fDev",DIMS.devices,"id",function(x){return x.name||x.id;});
  }).catch(function(){});
  fetch("/r/admin_api/clients",opt).then(function(r){return r.json();}).then(function(d){
    DIMS.clients=d.clients||[]; clientFill();
  }).catch(function(){});
}
function setFilters(){
  FUID=document.getElementById("fUid").value;
  FDEV=document.getElementById("fDev").value;
  clientFill();
  FCID=document.getElementById("fCid").value;
  load();
}
["fUid","fDev","fCid"].forEach(function(id){
  var el=document.getElementById(id);
  if(el) el.addEventListener("change",setFilters);
});
function winPaint(){
  var g=document.getElementById("gran"), sp=document.getElementById("span");
  if(g) g.value=GRAN;
  if(sp) sp.value=SPAN;
}
function setWin(){
  var g=document.getElementById("gran"), sp=document.getElementById("span");
  GRAN=g?g.value:"hour"; SPAN=sp?sp.value:"86400";
  try{ sessionStorage.setItem("cb_board_gran",GRAN);
       sessionStorage.setItem("cb_board_span",SPAN); }catch(e){}
  load();
}
(function(){
  var g=document.getElementById("gran"), sp=document.getElementById("span");
  if(g) g.addEventListener("change",setWin);
  if(sp) sp.addEventListener("change",setWin);
})();
var ST={ok:["正常"],warn:["有异常"],fail:["故障"],unknown:["待观察"],off:["未配置"]};
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function nsec(){return Date.now()/1000;}
function ago(ts){if(!ts)return "—";var d=nsec()-ts;if(d<0)d=0;
  if(d<60)return Math.round(d)+" 秒前";
  if(d<3600)return Math.round(d/60)+" 分钟前";
  if(d<86400)return (d/3600).toFixed(1)+" 小时前";
  return Math.round(d/86400)+" 天前";}
function hm(ts){if(!ts)return "—";var d=new Date(ts*1000);
  return ("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2);}
function dur(s){s=Math.max(0,Math.floor(s||0));
  var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d)return d+" 天 "+h+" 小时";if(h)return h+" 小时 "+m+" 分";return m+" 分";}
function fmtMs(ms){ms=Number(ms||0);return ms>=1000?(ms/1000).toFixed(1)+" s":Math.round(ms)+" ms";}
function order(s){return {fail:0,warn:1,unknown:2,ok:3,off:4}[s.state];}
function spark(series){
  var mx=1;series.forEach(function(b){mx=Math.max(mx,b[0]+b[1]);});
  return "<div class=spark>"+series.map(function(b){
    var n=b[0]+b[1],h=n?Math.max(2,Math.round(26*n/mx)):2;
    var cls=n?(b[1]?" r":" g"):"";
    return "<i class='"+cls+"' style='height:"+h+"px' title='"+
      n+" 次（失败 "+b[1]+"）'></i>";}).join("")+"</div>";
}
function fmtNum(n){n=Number(n||0);
  if(n>=100000000)return (n/100000000).toFixed(2)+" 亿";
  if(n>=10000)return (n/10000).toFixed(1)+" 万";
  if(n>=1000)return Math.round(n).toLocaleString();
  return (Math.round(n*10)/10).toString();}
function netCard(x){
  var mb=function(b){return ((Number(b)||0)/1048576).toFixed(1);};
  var pct=function(v){return (v==null)?"—":(Number(v).toFixed(1)+"%");};
  var eligNote=(x.rate_eligible==null&&Number(x.unclassified_bytes||0)>0)?"（分类数据积累中）":"";
  var elig=(Number(x.direct_bytes)||0)+(Number(x.relay_eligible_bytes)||0);
  return "<div class='svc s-ok'><h3><span class='dot'></span>直连 / 隧道流量"+
    "<span class='pill'>"+esc(x.label||"")+"</span></h3>"+
    "<div class='desc'>目的：看还有多少流量能从 中继 挪到 直连，降 VPS 带宽</div>"+
    "<div class='nums'><div><b>"+pct(x.rate)+"</b><small>全部产品直连率</small></div>"+
    "<div><b>"+mb(x.direct_bytes)+" / "+mb(x.total_bytes)+"</b><small>直连 / 总 MB</small></div></div>"+
    "<div class='nums'><div><b>"+pct(x.rate_eligible)+"</b><small>可直连接口直连率"+eligNote+"</small></div>"+
    "<div><b>"+mb(x.direct_bytes)+" / "+mb(elig)+"</b><small>直连 / 可直连 MB</small></div></div>"+
    "<div class='meta'>仅中继 "+mb(x.relay_only_bytes)+" MB（页面/静态/语音/终端输入：靠缓存或改造）"+
    ((Number(x.unclassified_bytes)||0)?(" · 未分类历史 "+mb(x.unclassified_bytes)+" MB"):"")+
    " · 请求 "+(x.req_n||0)+" 个</div></div>";
}
function trafficCard(t){
  var mb=function(b){return ((Number(b)||0)/1048576).toFixed(1);};
  var p=t.product||{}, o=t.ops||{};
  return "<div class='svc s-ok'><h3><span class='dot'></span>流量分类"+
    "<span class='pill'>"+esc(t.label||"")+"</span></h3>"+
    "<div class='desc'>产品使用 = 会话隧道 + 客户端/安装包下载；运维 = 发版上传 / OSS 镜像 / 备份</div>"+
    "<div class='nums'><div><b>↑"+mb(p.in)+" / ↓"+mb(p.out)+"</b><small>产品使用 MB</small></div>"+
    "<div><b>↑"+mb(o.in)+" / ↓"+mb(o.out)+"</b><small>运维 MB</small></div></div>"+
    "<div class='meta'>产品请求 "+(p.req||0)+" · 运维请求 "+(o.req||0)+
    (t.filtered?" · 筛选时运维为全站":" · 未计部分为 SSH/系统/其他")+"</div>"+
    ((t.paths&&t.paths.length)?("<div class='meta'>"+
      t.paths.slice(0,4).map(function(x){return esc(x.cls)+" "+mb(x.bytes);}).join(" · ")+"</div>"):"")+
    "</div>";
}
function card(s){
  var state=s.state, nm=ST[state]?ST[state][0]:"未知";
  var W2=s.win||s.h24||{ok:0,fail:0,rate:null};
  var winTxt=W2.label||"";
  var rate=W2.rate==null?"—":(W2.rate.toFixed(1)+"%");
  var meta="";
  if(s.kind==="active"&&s.probe_ts)
    meta+="探活 "+hm(s.probe_ts)+" · "+(s.probe_ok?"通过":"失败")+" · "+fmtMs(s.probe_ms)+"　";
  if(s.last_ok)meta+="最近成功 "+ago(s.last_ok);
  else meta+="还没有成功记录";
  if(s.last_fail)meta+=" · 最近失败 "+ago(s.last_fail);
  if(s.avg_ms)meta+=" · 平均 "+fmtMs(s.avg_ms);
  var ex=winTxt?("统计 "+winTxt):"";
  if(W2.calls!=null)ex+=(ex?" · ":"")+"调用 "+W2.calls+" 次";
  if(W2.unit&&W2.metric)ex+=(ex?" · ":"")+"输入 "+fmtNum(W2.metric)+" "+W2.unit;
  if(W2.cost)ex+=(ex?" · ":"")+"估算 ¥"+Number(W2.cost).toFixed(2);
  var err="";
  if(s.state==="off")err="未配置（不影响运行，只是这项功能没开）";
  else if(s.err&&s.state!=="ok")err=s.err;
  else if(s.probe_ok===false&&s.probe_err)err="探活失败："+s.probe_err;
  return "<div class='svc s-"+state+"'><h3><span class='dot'></span>"+esc(s.name)+
    "<span class='pill'>"+nm+"</span></h3>"+
    "<div class='desc'>"+esc(s.desc||"")+"</div>"+
    "<div class='nums'><div><b>"+W2.ok+"</b><small>成功</small></div>"+
    "<div><b style='color:"+(W2.fail?"var(--bad)":"var(--mut)")+"'>"+W2.fail+
    "</b><small>失败</small></div>"+
    "<div class='rate'>成功率 <b>"+rate+"</b></div></div>"+
    "<div class='meta'>"+esc(meta)+"</div>"+
    (ex?"<div class='meta'>"+esc(ex)+"</div>":"")+
    (err?"<div class='err' style='color:"+(state==="off"?"var(--mut)":"#ff9d97")+
      "' title='"+esc(err)+"'>"+esc(err)+"</div>":"")+
    spark((W2&&W2.series)||s.series||[])+"</div>";
}
function render(d){
  SUM=d.summary||SUM;UP=d.uptime||0;ONLINE=d.online||0;CLIENTS=d.clients||0;
  var f=(d.filter||{});
  var ft=[];
  if(f.uid)ft.push("账号 #"+f.uid); if(f.dev)ft.push("开发机 "+String(f.dev).slice(0,8));
  if(f.cid)ft.push("客户端 "+String(f.cid).slice(0,8));
  var fh=$("fhint"); if(fh) fh.textContent=ft.length?("筛选："+ft.join(" · ")+"（服务指标按最近 48 小时事件统计）"):"";
  var bad=(d.summary&&d.summary.bad)||[];
  var el=$("banner");
  if(bad.length){el.className="on";el.textContent="⚠ 需要关注："+bad.join("、");}
  else el.className="";
  $("hsum").innerHTML=
    "<div class='k'><b>"+SUM.ok+"</b>正常</div>"+
    "<div class='k'><b style='color:"+(SUM.fail?"var(--bad)":"var(--txt)")+"'>"+SUM.fail+"</b>故障</div>"+
    "<div class='k'><b style='color:"+(SUM.warn?"var(--warn)":"var(--txt)")+"'>"+SUM.warn+"</b>异常</div>"+
    "<div class='k'><b>"+SUM.unknown+"</b>待观察</div>"+
    "<div class='k'><b>"+SUM.off+"</b>未配置</div>";
  var svcs=(d.services||[]).slice().sort(function(a,b){return order(a)-order(b);});
  $("grid").innerHTML=svcs.map(card).join("")+
    (d.direct?netCard(d.direct):"")+(d.traffic?trafficCard(d.traffic):"");
  var sy=d.sys||{},bal=d.balance||{};
  var balTxt="—";
  if(bal.amt!=null)balTxt=(bal.cur||"¥")+" "+Number(bal.amt).toFixed(2);
  else if(bal.err)balTxt="查询失败";
  var balBad=(bal.amt!=null&&bal.amt<Number(bal.min||0))||!!bal.err;
  $("foot").innerHTML=
    "<span>运行 <b>"+dur(UP)+"</b></span>"+
    "<span>在线开发机 <b>"+ONLINE+"</b> · 在线客户端 <b>"+CLIENTS+"</b></span>"+
    "<span>磁盘已用 <b>"+(sy.disk_pct||0).toFixed(1)+"%</b>（剩 "+(sy.disk_free_mb||0)+" MB）</span>"+
    "<span>可用内存 <b>"+(sy.mem_avail_mb?sy.mem_avail_mb+" MB":"—")+"</b></span>"+
    "<span>负载 <b>"+(sy.load==null?"—":sy.load)+"</b></span>"+
    "<span>数据库 <b>"+(sy.db_mb||0).toFixed(1)+" MB</b></span>"+
    "<span class='"+(balBad?"bad":"")+"'>阿里云余额 <b>"+balTxt+"</b>"+
      (bal.err?"（"+esc(bal.err.slice(0,40))+"）":"")+"</span>"+
    "<span style='margin-left:auto'>v"+esc(d.ver||"—")+" · 更新于 "+hm(nsec())+"</span>";
}
function tick(){
  var d=new Date();
  $("clk").textContent=("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2)+
    ":"+("0"+d.getSeconds()).slice(-2);
  var w=["日","一","二","三","四","五","六"][d.getDay()];
  $("cdate").textContent=d.getFullYear()+"-"+("0"+(d.getMonth()+1)).slice(-2)+"-"+
    ("0"+d.getDate()).slice(-2)+" 周"+w;
}
function load(){
  fetch("/r/admin_api/services?gran="+encodeURIComponent(GRAN)+"&span="+encodeURIComponent(SPAN)+
        "&uid="+encodeURIComponent(FUID)+"&dev="+encodeURIComponent(FDEV)+"&cid="+encodeURIComponent(FCID),{headers:TOK?{Authorization:"Bearer "+TOK}:{},
        credentials:"same-origin",cache:"no-store"}).then(function(r){
    if(r.status===401)throw new Error("401");
    return r.json();
  }).then(function(d){
    if(!d||!d.ok)throw new Error((d&&d.error)||"读取失败");
    $("gate").className="";LASTERR="";render(d);
  }).catch(function(e){
    if(e&&e.message==="401"){$("gate").className="on";return;}
    LASTERR=e&&e.message?e.message:"断线";
    $("banner").className="on";
    $("banner").textContent="⚠ 监控数据刷新失败："+LASTERR+"（"+hm(nsec())+"）";
  });
}
tick();winPaint();loadDims();setInterval(tick,1000);load();setInterval(load,10000);
</script>
</body></html>
"""


async def r_admin_board(request):
    """监控屏页面（墙屏常开）。数据接口仍要管理员身份。"""
    return web.Response(text=_PAGE_BOARD, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def r_admin_failures(request):
    """失败明细：服务调用失败 + HTTP 4xx/5xx（最近 N 小时，最多 200 条）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        hours = min(max(int(request.query.get("hours") or 24), 1), 168)
    except (TypeError, ValueError):
        hours = 24
    since = time.time() - hours * 3600
    where, args = ["ts>=?"], [since]
    for col in ("uid", "dev", "cid"):
        v = (request.query.get(col) or "").strip()
        if not v:
            continue
        if col == "uid":
            try:
                args.append(int(v))
            except ValueError:
                continue
        else:
            args.append(v[:64])
        where.append("%s=?" % col)
    w = " AND ".join(where)
    # errors/recent 是内存里的（重启就清空）：500 的真实堆栈和最近请求的 IP/UA/状态，
    # 用来判断「请求根本没到」还是「到了才崩」——线上排查最缺的就是这一块。
    out = {"ok": True, "hours": hours, "svc": [], "http": [],
           "errors": list(_ERRS)[:20], "recent": list(_RECENT)[:80]}
    try:
        for r in db.execute("SELECT ts,key,err,ms,uid,dev,cid FROM svc_fail WHERE " + w +
                            " ORDER BY id DESC LIMIT 200", args):
            out["svc"].append({"ts": r[0], "key": r[1], "err": r[2] or "",
                               "ms": r[3] or 0, "uid": r[4] or 0,
                               "dev": r[5] or "", "cid": r[6] or ""})
    except Exception:
        pass
    try:
        for r in db.execute("SELECT ts,path,method,status,uid,dev,cid,ms FROM req_fail WHERE " +
                            w + " ORDER BY id DESC LIMIT 200", args):
            out["http"].append({"ts": r[0], "path": r[1] or "", "method": r[2] or "",
                                "status": r[3] or 0, "uid": r[4] or 0,
                                "dev": r[5] or "", "cid": r[6] or "", "ms": r[7] or 0})
    except Exception:
        pass
    return web.json_response(out)


_PAGE_FAILS = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeBoss · 失败明细</title><link rel="icon" type="image/png" href="/favicon.ico">
<style>
:root{--bg:#080b10;--card:#0f141c;--line:#1d2531;--txt:#e6edf3;--mut:#8b949e;--bad:#F85149;--ok:#3FB950}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
body{padding:18px 22px 30px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:19px;margin:0}select{background:#141a23;border:1px solid var(--line);
color:var(--txt);border-radius:8px;padding:7px 9px;font:inherit;font-size:13px}
a{color:var(--mut);text-decoration:none}.tabs{display:flex;gap:6px;margin:6px 0 12px}
.tabs button{background:#141a23;border:1px solid var(--line);color:var(--mut);border-radius:8px;
padding:7px 14px;font:inherit;font-size:13px;cursor:pointer}
.tabs button.on{color:#fff;background:#238636;border-color:#238636}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;border-bottom:1px solid #1b2230;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500;white-space:nowrap}
.err{color:#ff9d97;word-break:break-all}.dim{color:var(--mut);font-size:11.5px}
.empty{color:var(--mut);padding:18px 0}.badge{font-weight:600}
.badge.s5{color:var(--bad)}.badge.s4{color:#D29922}
#gate{display:none;padding:28px;color:var(--mut)}#gate.on{display:block}
</style></head><body>
<header>
  <h1>失败明细</h1>
  <span class="dim">看服务调用失败和 HTTP 4xx/5xx；最多各 200 条（库内保留最近 5000 条）</span>
  <span style="flex:1"></span>
  <select id="hours"><option value="1">最近 1 小时</option><option value="24" selected>最近 24 小时</option>
    <option value="72">最近 3 天</option><option value="168">最近 7 天</option></select>
  <a href="/r/admin/board">← 监控屏</a>
</header>
<div class="tabs"><button id="tSvc" class="on">服务失败</button><button id="tHttp">请求失败</button></div>
<div id="gate">需要管理员登录：先在 <a href="/r/admin">管理后台</a> 登录，或在地址后加 <code>?t=管理令牌</code>。</div>
<div id="box" style="overflow-x:auto"></div>
<script>
var TOK="";try{TOK=sessionStorage.getItem("cb_admin")||"";}catch(e){}
try{var q=new URLSearchParams(location.search),t=q.get("t");
  if(t){TOK=t;try{sessionStorage.setItem("cb_admin",t);}catch(e){}history.replaceState(null,"",location.pathname);}}catch(e){}
var MODE="svc", DATA={svc:[],http:[]};
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function ago(ts){var d=Math.max(0,Date.now()/1000-ts);
  if(d<60)return Math.round(d)+" 秒前"; if(d<3600)return Math.round(d/60)+" 分钟前";
  if(d<86400)return (d/3600).toFixed(1)+" 小时前"; return Math.round(d/86400)+" 天前";}
function hm(ts){var d=new Date(ts*1000);return ("0"+d.getHours()).slice(-2)+":"+
  ("0"+d.getMinutes()).slice(-2)+":"+("0"+d.getSeconds()).slice(-2);}
function dim(r){var a=[];if(r.uid)a.push("uid "+r.uid);if(r.dev)a.push("dev "+esc(r.dev.slice(0,10)));
  if(r.cid)a.push("cid "+esc(r.cid.slice(0,10)));return a.join(" · ");}
function render(){
  var box=$("box");
  if(MODE==="svc"){
    var a=DATA.svc||[];
    box.innerHTML=a.length?("<table><tr><th>时间</th><th>服务</th><th>原因</th><th>维度</th><th>耗时</th></tr>"+
      a.map(function(r){return "<tr><td>"+hm(r.ts)+"<div class=dim>"+ago(r.ts)+"</div></td>"+
        "<td>"+esc(r.key)+"</td><td class=err>"+esc(r.err||"")+"</td><td class=dim>"+dim(r)+"</td>"+
        "<td class=dim>"+(r.ms?r.ms+" ms":"—")+"</td></tr>";}).join("")+"</table>")
      :"<div class=empty>这段时间没有服务失败 🎉</div>";
  }else{
    var a=DATA.http||[];
    box.innerHTML=a.length?("<table><tr><th>时间</th><th>状态</th><th>请求</th><th>维度</th><th>耗时</th></tr>"+
      a.map(function(r){var c=r.status>=500?"s5":"s4";
        return "<tr><td>"+hm(r.ts)+"<div class=dim>"+ago(r.ts)+"</div></td>"+
        "<td class='badge "+c+"'>"+r.status+"</td><td class=err>"+esc(r.method)+" "+esc(r.path)+"</td>"+
        "<td class=dim>"+dim(r)+"</td><td class=dim>"+(r.ms?r.ms+" ms":"—")+"</td></tr>";}).join("")+"</table>")
      :"<div class=empty>这段时间没有失败请求 🎉</div>";
  }
}
function load(){
  var h=$("hours").value;
  fetch("/r/admin_api/failures?hours="+h,{headers:TOK?{Authorization:"Bearer "+TOK}:{},
        credentials:"same-origin",cache:"no-store"}).then(function(r){
    if(r.status===401)throw new Error("401"); return r.json();
  }).then(function(d){
    if(!d||!d.ok)throw new Error((d&&d.error)||"读取失败");
    $("gate").className=""; DATA=d; render();
  }).catch(function(e){
    if(e&&e.message==="401"){$("gate").className="on";$("box").innerHTML="";return;}
    $("box").innerHTML="<div class=empty>读取失败："+esc(e&&e.message||e)+"</div>";
  });
}
$("tSvc").onclick=function(){MODE="svc";$("tSvc").className="on";$("tHttp").className="";render();};
$("tHttp").onclick=function(){MODE="http";$("tHttp").className="on";$("tSvc").className="";render();};
$("hours").onchange=load;
load();setInterval(load,10000);
</script></body></html>
"""


async def r_admin_fails_page(request):
    return web.Response(text=_PAGE_FAILS, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def r_admin_mailtest(request):
    """管理台试发信：用运行中进程的真实环境变量发一封，直接回真实错误。
    排除「改了 systemd 但没 daemon-reload / 没重启，进程仍是旧环境」这类问题。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权访问"}, status=401)
    data = await request.json()
    to = (data.get("to") or SMTP_USER or "").strip()
    if not _EMAIL_RE.match(to):
        return web.json_response({"ok": False, "error": "收件邮箱不正确"})
    if not SMTP_HOST:
        return web.json_response(
            {"ok": False, "smtp": {"on": False},
             "error": "当前进程没读到 RELAY_SMTP_HOST：确认 systemd 里有 Environment=，"
                      "改完要 systemctl daemon-reload && systemctl restart codeboss-relay"})
    try:
        await asyncio.to_thread(_mail_send, to, "CodeBoss 发信测试",
                                "这是一封测试邮件，收到即表示 SMTP 正常。")
    except Exception as e:
        print("[relay] mailtest 失败: %s" % str(e)[:300], flush=True)
        return web.json_response({"ok": False, "smtp": {"on": True, "host": SMTP_HOST,
                                                        "port": SMTP_PORT, "user": SMTP_USER},
                                  "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
    return web.json_response({"ok": True, "sent": True, "to": to,
                              "smtp": {"on": True, "host": SMTP_HOST,
                                       "port": SMTP_PORT, "user": SMTP_USER}})


async def r_admin_smstest(request):
    """管理台试发短信：用运行中进程的真实环境变量发一条验证码，直接回阿里云错误。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权访问"}, status=401)
    data = await request.json()
    phone = _norm_phone(data.get("phone"))
    if not _PHONE_RE.match(phone):
        return web.json_response({"ok": False, "error": "手机号格式不正确"})
    if not SMS_ON:
        return web.json_response(
            {"ok": False, "sms": {"on": False},
             "error": "当前进程没读到 RELAY_ALI_* ：确认 systemd 里有 Environment=，"
                      "改完 systemctl daemon-reload && systemctl restart codeboss-relay"})
    try:
        await asyncio.to_thread(_sms_send_sync, phone, secrets.token_hex(8))
    except Exception as e:
        print("[relay] smstest 失败: %s" % str(e)[:300], flush=True)
        return web.json_response({"ok": False, "sms": {"on": True, "sign": ALI_SIGN_NAME},
                                  "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
    return web.json_response({"ok": True, "sent": True, "to": _mask_phone(phone),
                              "sms": {"on": True, "sign": ALI_SIGN_NAME,
                                      "template": ALI_TEMPLATE}})


async def r_admin(request):
    if not ADMIN_TOKEN:
        return web.Response(status=404, text="not found")
    fmt = "codeboss-relay（二进制）" if _is_frozen() else "relay.py"
    return web.Response(text=_PAGE_ADMIN.replace("__CSS__", _CSS)
                        .replace("__FMT__", fmt),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


def _plan_li(rows):
    return "".join('<li%s>%s</li>' % ('' if on else ' class="off"', txt) for txt, on in rows)


def _sku_unit(price, days, mins=0, per_day=0):
    """折合单价，单位统一「元/分钟」：时长包按包内总分钟；订阅按天数 × 每天可用分钟。
    per_day=0（不限时长，如 Max）按 24 小时/天折算——只是比价口径，不是使用承诺。"""
    try:
        m = float(price)
    except (TypeError, ValueError):
        return 0.0
    total = int(mins) if mins else int(days) * (int(per_day) or 1440)
    return m / total if total > 0 else 0.0


def _sku_off(price, days, mins, base):
    """同等级相对基础档的单价变动（%）：正=便宜，负=更贵。
    base 是 (price, days, mins, per_day)；订阅的 per_day 相同，约掉不影响百分比。"""
    u, u0 = _sku_unit(price, days, mins, base[3]), _sku_unit(*base)
    if u <= 0 or u0 <= 0:
        return 0.0
    return (1.0 - u / u0) * 100.0


def _off_txt(pct, base_nm=""):
    """折扣文案；后台把大档调得比基础档还贵时如实写「贵 X%」，不装成省 0%。"""
    if abs(pct) < 0.05:
        return ""
    if pct > 0:
        return "省 %.1f%%" % pct
    return "贵 %.1f%%" % -pct


def _plans_cards_html():
    """套餐卡片与价格选项：数字全部取自 plans.json / PAY_SKUS，改价改配置即可。"""
    p = plans()

    def lim(k, n):
        return int((((p.get(k) or {}).get("limits") or {}).get(n)) or 0)

    def has(k, n):
        return bool(((p.get(k) or {}).get("features") or {}).get(n))

    def use_line(k):
        """使用额度那一行：免费版按「条数 + 语音秒数」说（2026-11-09 起不看时长），
        付费档仍按时长说。"""
        s = int(lim(k, "sends_day") or 0)
        if s:
            return ("每天 %d 条消息（看消息 / 翻历史不计时）" % s, True)
        n = int(lim(k, "use_min_day") or 0)
        if not n:
            return ("远程使用：不限时长", True)
        if n % 60 == 0:
            return ("每天远程使用 %d 小时" % (n // 60), True)
        return ("每天远程使用 %d 分钟" % n, True)

    def voice_line(k):
        """语音转写：按实际音频秒数限量（识别成功才计）。0 = 不限。"""
        n = int(lim(k, "asr_sec_day") or 0)
        if not n:
            return ("语音转写不限量（计入使用时长）", True)
        if n % 60 == 0:
            return ("语音转写 %d 分钟/天（按实际音频秒数计）" % (n // 60), True)
        return ("语音转写 %d 秒/天（按实际音频秒数计）" % n, True)

    def feats(k):
        # 模型调用次数单独一行：语音概括、命令识别、提醒文案都吃它，
        # 不属于「进度提醒」也不属于「自动化」，别塞进那两行里。0 = 不限。
        n = int(lim(k, "llm_day") or 0)
        llm_line = ("模型调用 %s（语音概括 / 命令识别共用）"
                    % ("不限" if not n else "%d 次/天" % n))
        return _plan_li([
            ("开发机 %d 台" % lim(k, "hosts"), True),
            ("客户端 %d 个（24 小时内活跃，超时自动释放）" % lim(k, "clients"), True),
            use_line(k),
            (llm_line, True),
            ("命令终端 · 识别命令 · 新建终端", has(k, "terminal")),
            voice_line(k),
            # 提醒 = 怎么把进度送到你面前（文字/语音）；识别进度本身算自动化
            ("进度提醒：卡住 / 完成主动推手机（文字 + 语音）", has(k, "remind")),
            ("自动化：语音自动识别、AI 内容总结、进度识别", has(k, "auto")),
        ])

    def yen(k):
        v = pay_skus().get(k)
        s = (v[1] if v else "0").rstrip("0").rstrip(".")
        return s or "0"

    sk = pay_skus()

    def per_day_of(k):
        """订阅每天可用分钟（不限时长的 Max 记 0，折合时按 24h/天）。"""
        pl = "max" if k.startswith("max_") else (
            "pro" if k in ("trial", "month", "quarter", "year") else "")
        return lim(pl, "use_min_day") if pl else 0

    def base_of(k):
        """把 SKU 折成 (价格, 天数, 包内分钟, 每天可用分钟)，供 _sku_unit/_sku_off 用。"""
        v = sk.get(k) or ("", "0", 0)
        return (v[1], v[2], pack_min().get(k, 0), per_day_of(k))

    def off_of(k, base):
        return _sku_off(sk[k][1], sk[k][2], pack_min().get(k, 0), base_of(base))

    def short_nm(mins):
        return ("%d 小时" % (mins // 60)) if mins % 60 == 0 else ("%d 分钟" % mins)

    def unit_of(k):
        return _sku_unit(*base_of(k))

    def chips_for(prefix):
        rows = []
        base = prefix + "month"
        for suf, nm, note in (("trial", "体验月", "限一次"), ("month", "月付", ""),
                              ("quarter", "季付", ""), ("year", "年付", "")):
            sku = (prefix + suf) if prefix else suf
            if sku not in sk:
                continue
            # 折扣只按「同等级的时长档」算；体验月是限一次促销价，不参与比价
            badge = ''
            if suf in ("quarter", "year") and base in sk:
                txt = _off_txt(off_of(sku, base))
                badge = ('<em>比月付%s</em>' % txt) if txt else ''
            rows.append('<button class="sku%s" data-sku="%s" data-money="%s" onclick="pick(this)">'
                        '<b>¥%s</b><span>%s%s</span>%s</button>'
                        % (" on" if suf == "month" else "", sku, sk[sku][1], yen(sku), nm,
                           (" · " + note) if note else "", badge))
        return "".join(rows)

    # 时长包档位由后台配置（可增删）；列表第一档就是基础档，其余折扣自动跟它比
    pk = [x for x in packs() if x["sku"] in sk and x.get("sell") is not False]
    pk_base = pk[0]["sku"] if pk else ""
    pk_base_nm = short_nm(int(pk[0]["min"])) if pk else ""
    pk_btns = []
    for it in pk:
        sku, mins = it["sku"], int(it["min"])
        txt = _off_txt(off_of(sku, pk_base)) if (pk_base and sku != pk_base) else ""
        badge = ('<em>比 %s%s</em>' % (pk_base_nm, txt)) if txt else ''
        pk_btns.append('<button class="gray" data-pack="%s" data-money="%s" '
                       'onclick="pickPack(this)">%s · ¥%s%s</button>'
                       % (sku, yen(sku), short_nm(mins), yen(sku), badge))

    def ptable():
        """完整价格表：每档折合单价 + 同等级相对基础档（月付 / 6 小时包）的降幅。"""
        rows = []
        for title, prefix, base in (("Pro 订阅", "", "month"),
                                    ("Max 订阅", "max_", "max_month")):
            if base not in sk:
                continue
            rows.append('<tr class="grp"><td colspan="4">%s</td></tr>' % title)
            for suf, nm in (("month", "月付"), ("quarter", "季付"), ("year", "年付")):
                k = prefix + suf
                if k not in sk:
                    continue
                off = "" if suf == "month" else _off_txt(off_of(k, base))
                rows.append('<tr><td>%s</td><td>¥%s</td><td>¥%.5f/分钟</td><td>%s</td></tr>'
                            % (nm, yen(k), unit_of(k), off or "—"))
        if pk:
            rows.append('<tr class="grp"><td colspan="4">使用时长包（不过期）</td></tr>')
            for it in pk:
                k, mins = it["sku"], int(it["min"])
                off = "" if k == pk_base else _off_txt(off_of(k, pk_base))
                rows.append('<tr><td>%s</td><td>¥%s</td><td>¥%.5f/分钟</td>'
                            '<td>%s</td></tr>'
                            % (short_nm(mins), yen(k), unit_of(k), off or "—"))
        return ('<table class="ptab"><thead><tr><th>档位</th><th>价格</th><th>折合单价</th>'
                '<th>折扣</th></tr></thead><tbody>%s</tbody></table>' % "".join(rows))

    return ((p.get("free") or {}).get("name") or "免费版",
            (p.get("pro") or {}).get("name") or "Pro",
            (p.get("max") or {}).get("name") or "Max",
            yen("month"), yen("max_month"),
            feats("free"), feats("pro"), feats("max"),
            chips_for(""), chips_for("max_"), "".join(pk_btns), ptable())


_PAGE_PLANS = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>CodeBoss · 套餐与续费</title>__CSS__<style>
.ph{max-width:420px;margin:0 auto}
.ph .back{font-size:13px;color:var(--mut);text-decoration:none;display:inline-block;margin-bottom:2px}
.ph h1{margin:2px 0 4px;font-size:22px;display:block}
.ph .sub{color:var(--mut);font-size:13px;line-height:1.6;margin:0 0 14px}
.curbar{display:flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
        border-radius:10px;padding:10px 14px;margin:0 auto 14px;font-size:13px;color:var(--mut);max-width:420px}
.curbar b{color:var(--boss);font-weight:600}
.plans{display:flex;flex-direction:column;gap:12px;max-width:420px;margin:0 auto}
.pcard{position:relative;flex:1;display:flex;flex-direction:column;background:var(--card);
       border:1px solid var(--line);border-radius:14px;padding:18px}
.pcard button.pfree{margin-top:auto}
.pcard.pro{border-color:rgba(255,197,61,.5);box-shadow:0 8px 28px -14px rgba(255,197,61,.35)}
.pcard.max{border-color:rgba(88,166,255,.45)}
.pcard.max .pname{color:#58A6FF}
.rec{position:absolute;top:-9px;right:14px;background:var(--boss);color:#1B1B1B;font-size:11px;
     font-weight:700;padding:2px 10px;border-radius:999px}
.pname{font-size:15px;font-weight:600}
.pcard.pro .pname{color:var(--boss)}
.pprice{margin:6px 0 2px;font-size:34px;font-weight:700;letter-spacing:-.5px;line-height:1.1}
.pprice small{font-size:13px;font-weight:500;color:var(--mut);margin-left:4px;letter-spacing:0}
.pnote{color:var(--mut);font-size:12.5px;margin-bottom:12px}
ul.feat{list-style:none;margin:0 0 14px;padding:0;font-size:13.5px}
ul.feat li{padding:5px 0 5px 21px;position:relative}
ul.feat li::before{content:'✓';position:absolute;left:0;color:var(--gyH);font-weight:700}
ul.feat li.off{color:var(--dim)}
ul.feat li.off::before{content:'–';color:var(--dim)}
.skus{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.sku{background:var(--ctl);border:1px solid var(--line);border-radius:9px;padding:8px 10px;
     text-align:left;color:var(--txt);font-weight:500;line-height:1.3;cursor:pointer;transition:.15s}
.sku b{display:block;font-size:15px}
.sku span{color:var(--mut);font-size:11.5px;font-weight:400}
.sku.on{border-color:var(--gyH);background:rgba(46,160,67,.12)}
.sku.on b{color:var(--gyH)}
/* 体验月（限一次）：付费后置灰 */
.sku:disabled{opacity:.4;cursor:not-allowed;border-style:dashed}
.sku:disabled b{color:var(--mut)}
/* 降档/缩短：能买，但「到期后生效」——用黄字标出来，别让人以为是立即生效 */
.sku.later{border-style:dashed}
.sku em.later{color:#E3B341}
.pbuy{width:100%;padding:12px;font-size:15px;border-radius:9px}
.paysub{color:var(--dim);font-size:11.5px;text-align:center;margin-top:7px}
.paymsg{color:var(--txt);font-size:12.5px;margin-top:8px;text-align:center;word-break:break-all}
.paymsg img{border-radius:8px;margin-top:6px}
.wide{max-width:420px;margin-left:auto;margin-right:auto}
.packs{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.packs button{flex:1;min-width:96px;font-size:12.5px;padding:9px 6px}
.packs button.on{border-color:var(--gyH);background:rgba(46,160,67,.12);color:var(--gyH)}
.sku em,.packs em{display:block;font-style:normal;font-size:10.5px;color:var(--gyH);
                  margin-top:2px;font-weight:600}
/* 完整价格表：折扣按折合单价算，同等级都跟最短档比 */
.ptab{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;table-layout:fixed}
.ptab th,.ptab td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ptab th:first-child,.ptab td:first-child{width:30%}
.ptab th:nth-child(3),.ptab td:nth-child(3){width:32%}
.ptab th:last-child,.ptab td:last-child{width:22%}
.ptab th{text-align:left;color:var(--mut);font-weight:500;padding:6px 4px;
         border-bottom:1px solid var(--line)}
.ptab td{padding:7px 4px;border-bottom:1px solid rgba(255,255,255,.05);color:var(--txt)}
.ptab th:nth-child(2),.ptab td:nth-child(2){text-align:right}
.ptab th:nth-child(3),.ptab td:nth-child(3){text-align:right;color:var(--mut)}
.ptab th:last-child,.ptab td:last-child{text-align:right;color:var(--gyH);font-weight:600}
.ptab tr.grp td{color:var(--mut);font-size:11.5px;padding-top:14px;border-bottom:0;font-weight:500}
.ptab tr.grp td:last-child{color:var(--mut);font-weight:400}
/* 付款二维码浮层：不挤在卡片里，屏幕中间盖一层，支付宝切回来也还在 */
#qrlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.66);display:none;align-items:center;
       justify-content:center;z-index:80;padding:20px}
#qrlay.on{display:flex}
#qrcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;
        max-width:340px;width:100%;text-align:center;color:var(--txt)}
#qrcard .qmsg{font-size:13px;color:var(--mut);line-height:1.7;word-break:break-all}
#qrcard img{width:220px;max-width:80%;background:#fff;padding:10px;border-radius:10px;
            margin:12px auto;display:block}
#qrcard a{color:var(--gyH)}
.trust ul{list-style:none;margin:6px 0 0;padding:0;color:var(--mut);font-size:12.5px;line-height:1.75}
.trust li{padding-left:18px;position:relative}
.trust li::before{content:'·';position:absolute;left:6px;color:var(--gyH);font-weight:700}
.payok{max-width:760px;margin:0 auto 12px;background:rgba(46,160,67,.12);
       border:1px solid rgba(46,160,67,.45);color:var(--gyH);border-radius:10px;
       padding:12px 14px;font-size:14.5px;font-weight:600;text-align:center}
.pcard.paid{border-color:rgba(46,160,67,.6)}
@media(min-width:700px){
  body{padding:28px 20px}
  .ph,.curbar,.wide{max-width:760px}
  .plans{max-width:760px;flex-direction:row;align-items:stretch}
}
</style></head><body>
<div class="ph">
  __BACK__
  __H1__
  <p class="sub">CodeBoss 一直可以免费用。Pro 放大额度并解锁命令终端，Max 不限时长；按需开通，不自动续费。<br>
  升级或续费立即生效；换成更低/更短的档也可以，从现套餐到期那天起生效，已买的时间一天不折损。</p>
</div>
<div class="curbar">当前套餐：<b id="cur">__CUR__</b></div>
<div class="curbar" id="curnext" style="display:none;border-color:rgba(227,179,65,.45)"></div>
<div class="payok" id="payok" style="display:none"></div>
<div class="card wide" id="anonbox" style="display:__ANONBOX__;margin-bottom:12px">
  <div class="pname" style="font-size:14px">付款后自动绑定本机</div>
  <div class="meta" style="margin:6px 0 8px;line-height:1.7">不用先注册：填一次手机号或邮箱收个验证码，付完款这台开发机自动挂到新账号（已有账号同样适用）。</div>
  <div class="row"><input id="anid" placeholder="手机号或邮箱" style="flex:1;margin:0">
    <button id="anbtn" class="gray" style="flex:none" onclick="anonSend()">发验证码</button></div>
  <div class="row"><input id="ancode" placeholder="验证码" inputmode="numeric" maxlength="6" style="flex:1;margin:0"></div>
  <div class="paymsg" id="anerr"></div>
</div>
<div class="plans">
  <div class="pcard">
    <div class="pname">__FREENAME__</div>
    <div class="pprice">¥0<small>/ 永久</small></div>
    <div class="pnote">够用就免费，不绑卡、不限期。</div>
    <ul class="feat">__FREEFEAT__</ul>
    <button class="gray pfree" style="width:100%;padding:12px" disabled>__FREEBTN__</button>
  </div>
  <div class="pcard pro">
    <div class="rec">推荐</div>
    <div class="pname">__PRONAME__</div>
    <div class="pprice">¥__MONTH__<small>/ 月起</small></div>
    <div class="pnote">多机、多说，还多了进度提醒和自动化。</div>
    <ul class="feat">__PROFEAT__</ul>
    <div class="skus" data-grp="pro">__CHIPS__</div>
    <button id="buybtn" class="pbuy" data-buy="pro" onclick="buySel('pro')">立即开通 · ¥__MONTH__</button>
    <div class="paysub" id="paysub">支付宝付款 · 开通后立即生效</div>
    <div class="paymsg" id="paymsg"></div>
  </div>
  <div class="pcard max">
    <div class="pname">__MAXNAME__</div>
    <div class="pprice">¥__MAXMONTH__<small>/ 月起</small></div>
    <div class="pnote">不限时长，额度拉满，适合重度远程开发。</div>
    <ul class="feat">__MAXFEAT__</ul>
    <div class="skus" data-grp="max">__MAXCHIPS__</div>
    <button class="pbuy" data-buy="max" onclick="buySel('max')">立即开通 · ¥__MAXMONTH__</button>
  </div>
</div>
<div class="card wide" style="margin-top:12px">
  <div class="pname" style="font-size:14px">使用时长包</div>
  <div class="meta" style="margin:6px 0 0">不订阅也能买；没有有效期。当天套餐额度用完后，按分钟接着用（语音也一样可用）。Max 有效期内不扣包，到期后才开始用。</div>
  <div class="packs">__PACKS__</div>
  <button id="packBuy" class="pbuy" style="margin-top:10px" onclick="buyPack()">立即购买使用时长包</button>
  <div class="paymsg" id="packmsg"></div>
</div>
<div class="card wide" style="margin-top:12px">
  <div class="pname" style="font-size:14px">价格与折扣</div>
  <div class="meta" style="margin:6px 0 0">单价统一折成元/分钟；折扣是同档里跟最短那档比。Max 不限时长，按 24 小时/天折算（只是比价口径）。</div>
  __PTABLE__
</div>
<div class="card wide trust">
  <div class="pname" style="font-size:14px">放心用</div>
  <ul>
    <li>一次付款，不会自动续费；到期自动回到免费版，不影响继续使用。</li>
    <li>额度只按实际用量扣：一个自然分钟里有用过才算一分钟，不用不扣。</li>
    <li>会话和密钥都在你自己的电脑上，中继只转发，不保存对话内容。</li>
  </ul>
</div>
<div id="qrlay" onclick="if(event.target===this)qrClose()">
  <div id="qrcard">
    <div class="pname" style="font-size:15px">扫码付款</div>
    <div class="qmsg" id="qrmsg"></div>
    <button class="gray" style="width:100%;margin-top:12px" onclick="qrClose()">关闭</button>
  </div>
</div>
<script>
/* App 内嵌（?emb=1）：不画整页的「返回」，拉起支付宝交给外层页面跳 */
var EMB=__EMB__;
var ANON=__ANON__;   /* 1 = 匿名设备在收银台建号：下单前补手机号/邮箱 + 验证码 */
function post(ep,body,cb){var x=new XMLHttpRequest();x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};try{d=JSON.parse(x.responseText)}catch(e){}cb(d)};
  x.send(JSON.stringify(body))}
var SEL={pro:{sku:'month',money:'__MONTH__'},max:{sku:'max_month',money:'__MAXMONTH__'}};
function pick(el){
  var grp=el.parentNode.getAttribute('data-grp')||'pro';
  var all=el.parentNode.querySelectorAll('.sku');
  for(var i=0;i<all.length;i++)all[i].classList.remove('on');
  el.classList.add('on');
  SEL[grp]={sku:el.getAttribute('data-sku'),money:el.getAttribute('data-money')};
  var b=document.querySelector('[data-buy="'+grp+'"]');
  if(!b)return;
  /* 按钮写清楚这一单是什么：升级 / 续费 / 预约降档（哪天生效）。
     降档也允许买，但不立刻削权益——从现套餐到期那天接上。 */
  var d=CURPLAN||{}, skus=SKUSD||{}, cur=(d.plan&&d.plan!=='free')?String(d.sku||''):'';
  var r=rankOf(SEL[grp].sku,skus), cr=rankOf(cur,skus), money=SEL[grp].money;
  /* 跨档升级（Pro→Max）：中继按「新档价 − 现档剩余价值」报补差价。
     同档换更长时长不走这里——那是延续时间，照付整价。 */
  var q=QUOTES[SEL[grp].sku];
  if(q&&Number(q.pay)<Number(money)){
    b.textContent='升级 · 补差价 ¥'+q.pay+'（已抵扣 ¥'+q.credit+'）';
    b.title='现套餐剩余 '+q.left_days+' 天已折价抵扣；新档即日起算 '+q.days+' 天';
  }else if(cur&&r<cr){
    b.textContent='预约 · ¥'+money+'（'+fmtDay(d.expires)+' 起生效）';
  }else if(cur&&SEL[grp].sku===cur){
    b.textContent='续费 · ¥'+money;
  }else if(cur&&r>=cr){
    b.textContent='升级 · ¥'+money;
  }else{
    b.textContent='立即开通 · ¥'+money;
  }
}
var payLast=null, PA={timer:null,t0:0,otn:'',started:false,done:false};
/* 二维码走屏幕浮层：不挤在卡片里，切去支付宝再回来也还在 */
function qrClose(){var o=document.getElementById('qrlay');if(o)o.classList.remove('on');}
function qrShow(html){
  var m=document.getElementById('qrmsg'),o=document.getElementById('qrlay');
  if(!m||!o)return;
  m.innerHTML=html;o.classList.add('on');
}
function showQr(){
  var d=payLast||{};
  if(d.img){qrShow('用支付宝扫码付款<img src="'+d.img+'">');return;}
  if(d.payurl){qrShow('<a href="'+d.payurl+'">点这里打开付款页</a>');return;}
  qrShow('下单成功，请继续完成付款');
}
window.__cbPayFail=showQr;
function buySel(grp){buy((SEL[grp]||SEL.pro).sku,'alipay',paymsg)}
/* 使用时长包：先选档位，再点「立即购买」才真正下单 */
function pickPack(el){
  var all=document.querySelectorAll('.packs button[data-pack]');
  for(var i=0;i<all.length;i++)all[i].classList.remove('on');
  el.classList.add('on');
  var b=document.getElementById('packBuy');
  if(b){b.textContent='立即购买 · ¥'+el.getAttribute('data-money');b.disabled=false;}
}
function buyPack(){
  var el=document.querySelector('.packs button[data-pack].on');
  if(!el){var m=document.getElementById('packmsg');if(m)m.textContent='请先选一个使用时长包';return;}
  buy(el.getAttribute('data-pack'),'alipay',document.getElementById('packmsg'));
}
function buy(sku,type,msgEl){
  var msg=msgEl||paymsg;
  var body={plan:sku,type:type};
  if(ANON){
    var id=document.getElementById('anid'),anerr=document.getElementById('anerr');
    var code=(document.getElementById('ancode').value||'').trim();
    id=(id?id.value:'').trim();
    if(!id||!code){if(anerr)anerr.textContent='先填手机号或邮箱，收验证码（付款后自动绑定本机）';return}
    if(id.indexOf('@')>0)body.email=id;else body.phone=id;
    body.code=code;
  }
  msg.textContent='正在下单…';
  post('/r/pay/create',body,function(d){
    if(!d.ok){msg.textContent=d.error||'下单失败';return;}
    msg.textContent='';
    if(ANON){ANON=0;var bx=document.getElementById('anonbox');if(bx)bx.style.display='none';}
    payLast=d; if(d.otn)PA.otn=d.otn;
    payStart();                       /* 从下单这一刻就开始轮询：不用等付款页返回 */
    if(type==='alipay'&&d.qrcode){
      var pu='alipays://platformapi/startapp?saId=10000007&qrcode='+encodeURIComponent(d.qrcode);
      /* 在 App 里这页是 iframe：子页跳 alipays:// 壳不一定接管，交给外层页面跳 */
      if(EMB&&window.parent&&window.parent!==window){
        try{window.parent.postMessage('cb-alipay:'+pu,'*');showQr();return;}catch(e){}
      }
      location.href=pu;
      showQr();                       /* 同时准备好浮层：唤起失败或切回来都能扫 */
      return;
    }
    showQr();
  });
}
/* 收银台建号：发验证码。先按注册发；账号已存在（或退回码已注册）再按登录发。 */
function anonSend(){
  var id=document.getElementById('anid'),b=document.getElementById('anbtn');
  var m=document.getElementById('anerr');
  var v=(id?id.value:'').trim();
  if(!v){if(m)m.textContent='请填手机号或邮箱';return}
  var phone=v.indexOf('@')<0;
  function send(purpose,retry){
    var ep=phone?'/r/send_sms':'/r/send_code';
    var body=phone?{purpose:purpose,phone:v}:{purpose:purpose,email:v};
    if(b)b.disabled=true;if(m)m.textContent='发送中…';
    post(ep,body,function(d){
      if(!d.ok&&retry&&/已注册/.test(d.error||'')){send('login',false);return;}
      if(m)m.textContent=d.ok?(d.msg||'验证码已发送'):(d.error||'发送失败');
      if(!d.ok){if(b)b.disabled=false;return;}
      var t=60;if(b){b.textContent=t+'s';}
      var iv=setInterval(function(){t--;if(b)b.textContent=t+'s';
        if(t<=0){clearInterval(iv);if(b){b.disabled=false;b.textContent='发验证码';}}},1000);
    });
  }
  send('register',true);
}
function fmtDay(ts){var d=new Date(ts*1000);
  return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);}
var CURPLAN=null, SKUSD={}, QUOTES={};   /* QUOTES：跨档升级的补差价报价（/api/plan） */
function rankOf(sku,skus){
  if(!sku)return -1;
  var days=Number(((skus||{})[sku]||{}).days||0);
  return (sku.indexOf('max_')===0?100000:0)+days;   /* Max 永远高于 Pro，同档再比时长 */
}
/* 换档规则：
   更高的档 / 更长的时长 → 立即生效（升级，或同档续费）
   更低的档 / 更短的时长 → 也允许买，但从现套餐到期那天才生效（预约降档）。
                          买的时候钱照付，现套餐一分权益不少，卡片上标「到期后生效」
   体验月限一次 → 已经付费了就不再给 */
function applyPlanRules(d){
  CURPLAN=d; SKUSD=(d&&d.skus)||{}; QUOTES=(d&&d.quotes)||{};
  var cur=(d&&d.plan&&d.plan!=='free')?String((d&&d.sku)||''):'';
  var curRank=rankOf(cur,SKUSD);
  var chips=document.querySelectorAll('.sku[data-sku]');
  for(var i=0;i<chips.length;i++){
    var el=chips[i], sku=el.getAttribute('data-sku'), r=rankOf(sku,SKUSD);
    var trialCut=!!(sku==='trial'&&(cur||d.trial_used));   /* 有生效套餐 或 已经买过体验月 */
    var later=!!(cur&&curRank>=0&&r>=0&&r<curRank);
    el.disabled=trialCut;
    el.classList.toggle('off',trialCut);
    el.classList.toggle('later',later&&!trialCut);
    var em=el.querySelector('em.later');
    if(later&&!trialCut){
      if(!em){em=document.createElement('em');em.className='later';el.appendChild(em);}
      em.textContent='到期后生效';
    }else if(em){em.parentNode.removeChild(em);}
    el.title=trialCut?'体验价每个账号限一次'
            :(later?('现套餐到期（'+fmtDay(d.expires)+'）后自动接上，现在买不削权益'):'');
  }
  /* 默认选中：① 现档就在这组就选它（续费）② 否则保住模板默认那档 ③ 默认那档是降档时，
     选这组里最便宜的「能立即生效」档 ④ 整组都是降档就选最高档（离现档最近）。 */
  ['pro','max'].forEach(function(grp){
    var b=document.querySelector('[data-buy="'+grp+'"]');
    if(!b)return;
    b.disabled=false;
    /* data-grp 在容器 .skus 上，按钮本身没有这个属性：写成 '.sku[data-grp=…]'
       会一条都选不到 → sel 为 null → 把「立即开通」置灰成「暂无可购档位」，
       表现就是套餐页所有购买按钮都点不了。 */
    var list=document.querySelectorAll('.skus[data-grp="'+grp+'"] .sku');
    var want=null, preset=null, cheap=null, top=null;
    for(var i=0;i<list.length;i++){
      var e2=list[i]; if(e2.disabled)continue;
      var sk2=e2.getAttribute('data-sku'), r2=rankOf(sk2,SKUSD);
      var later2=!!(cur&&curRank>=0&&r2<curRank);
      if(cur&&sk2===cur) want=e2;
      if(!later2&&e2.classList.contains('on')) preset=e2;
      if(!later2&&(!cheap||r2<rankOf(cheap.getAttribute('data-sku'),SKUSD))) cheap=e2;
      if(!top||r2>rankOf(top.getAttribute('data-sku'),SKUSD)) top=e2;
    }
    var sel=want||preset||cheap||top;
    if(sel){ pick(sel); return; }
    b.disabled=true; b.textContent='暂无可购档位';
  });
}
function curRefresh(cb){
  var x=new XMLHttpRequest();x.open('GET','/api/plan',true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};try{d=JSON.parse(x.responseText)}catch(e){}
    if(!d.ok){if(cb)cb(null);return;}
    applyPlanRules(d);
    var skn=(((d.skus||{})[d.sku||'']||{}).name||d.name||'');
    document.getElementById('cur').textContent=skn+
      (d.plan!=='free'&&d.expires?' · 至 '+fmtDay(d.expires):'');
    /* 已付款、等现套餐到期才生效的换档：单独一行写清楚从哪天开始 */
    var cn=document.getElementById('curnext');
    if(cn){
      if(d.next&&d.next.sku){
        cn.style.display='';
        cn.textContent='已预约：'+(((d.skus||{})[d.next.sku]||{}).name||d.next.sku)+
                       ' · '+fmtDay(d.next.from)+' 起生效';
      }else{ cn.style.display='none'; }
    }
    if(cb)cb(d);
  };x.send();
}
/* 付款成功：立刻把「当前套餐」改掉、标出已开通的卡片，并在顶部给出成功提示 */
function payOk(d){
  if(PA.done)return;
  PA.done=true; payStop();
  try{qrClose()}catch(e){}
  var o=d.order||{}, sku=o.sku||'', txt='';
  if(sku.indexOf('pack')===0){
    /* Max 是不限时长：包在套餐有效期内一分不动，到期后才开始扣 —— 文案别写「马上用」 */
    txt=(d.plan==='max')
      ? '✅ 使用时长包已到账：Max 到期后自动接着用（不过期）'
      : '✅ 使用时长包已到账，可以马上用';
  }else if(d.next&&d.next.sku===sku){
    /* 买的是更低/更短的档：钱收了，但从现套餐到期那天才生效 */
    txt='✅ 已预约：'+(((SKUSD||{})[sku]||{}).name||sku)+' · '+
        fmtDay(d.next.from)+' 起生效（现套餐照旧用到到期）';
  }else{
    var nm=(d.plan==='max'?'__MAXNAME__':(d.plan==='pro'?'__PRONAME__':'__FREENAME__'));
    txt='✅ 订阅成功：已开通 '+nm+(d.expires?'，有效期至 '+fmtDay(d.expires):'');
    var c=document.querySelector('.pcard.'+(d.plan==='max'?'max':'pro'));
    if(c)c.classList.add('paid');
  }
  var b=document.getElementById('payok');
  if(b){b.textContent=txt;b.style.display='block';try{b.scrollIntoView({block:'center'})}catch(e){}}
  paymsg.textContent='已开通，感谢支持！';
  curRefresh();
}
function payCheck(celebrate){
  var x=new XMLHttpRequest();
  x.open('GET','/r/pay/status'+(PA.otn?('?otn='+encodeURIComponent(PA.otn)):''),true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;
    var d={};try{d=JSON.parse(x.responseText)}catch(e){}
    if(!d||!d.ok)return;
    var o=d.order||{}, paid=!!(o&&o.status===1);
    if(PA.done)return;
    /* 只认「本单已付」：续费/时长包不改 plan，不能拿 plan!=free 当成功 */
    if(paid&&(celebrate||PA.started)) payOk(d);
  };x.send();
}
function payStop(){if(PA.timer){clearInterval(PA.timer);PA.timer=null;}}
function payStart(){
  PA.started=true;
  if(!PA.t0)PA.t0=Date.now();
  if(PA.done)return;
  if(PA.timer)return;
  PA.timer=setInterval(function(){
    if(PA.done){payStop();return;}
    if(Date.now()-PA.t0>15*60*1000){payStop();
      var t='还没收到付款结果，付款成功后<a href="javascript:location.reload()">刷新本页</a>';
      paymsg.innerHTML=t;
      var q=document.getElementById('qrlay');
      if(q&&q.classList.contains('on'))qrShow(t);
      return;}
    payCheck(true);
  },2000);
  payCheck(true);
}
(function(){var f=document.querySelector('.packs button[data-pack]');if(f)pickPack(f);})();
curRefresh();
document.addEventListener('visibilitychange',function(){
  if(document.hidden)return;
  if(PA.started&&!PA.done){payStart();return;}   /* 从支付宝回来：立刻查一次并继续轮询 */
  payCheck(false);curRefresh();
});
window.addEventListener('focus',function(){if(!document.hidden&&PA.started&&!PA.done)payCheck(true);});
window.addEventListener('pageshow',function(){if(PA.started&&!PA.done)payCheck(true);});
if(location.search.indexOf('pay=1')>=0){
  PA.t0=Date.now();payStart();                    /* 支付宝付款页返回（带 pay=1） */
}
/* 管理后台改套餐额度/价格：已打开的页面自动跟上，不用用户手动刷新。
   付款流程中先不动（别把二维码/付款状态刷掉），付完下一轮再刷。 */
var PLV=null;
function planVerTick(){
  var x=new XMLHttpRequest();x.open('GET','/r/plans_ver',true);
  x.onreadystatechange=function(){
    if(x.readyState!==4)return;
    var d=null;try{d=JSON.parse(x.responseText)}catch(e){}
    if(!d||!d.ok)return;
    if(PLV===null){PLV=d.v;return;}
    if(d.v===PLV)return;
    if(PA.started&&!PA.done)return;
    location.reload();
  };x.send();
}
setInterval(planVerTick,8000);
document.addEventListener('visibilitychange',function(){if(!document.hidden)planVerTick();});
</script></body></html>"""


async def r_plans_ver(request):
    """plans.json 版本（mtime+大小）：已打开的套餐页据此自动刷新额度/价格。"""
    try:
        st = os.stat(_PLAN_PATH)
        v = "%d-%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        v = "0"
    return web.json_response({"ok": True, "v": v},
                             headers={"Cache-Control": "no-store"})


_PAGE_PLANS_OFF = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>CodeBoss</title></head>'
    '<body style="margin:0;background:#0B0E14;color:#E6EDF3;font:15px/1.6 -apple-system,'
    'PingFang SC,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh">'
    '<a href="/" id="pgback" style="position:fixed;top:calc(env(safe-area-inset-top) + 10px);left:12px;'
    'color:#58A6FF;text-decoration:none;font-size:14px;-webkit-tap-highlight-color:transparent">‹ 返回</a>'
    '<div style="padding:24px;text-align:center">'
    '<div style="font-size:17px;font-weight:600;margin-bottom:8px">套餐升级暂未开放</div>'
    '<div style="color:#8B949E;font-size:13px">先按现有套餐使用，开放后会在这里出现。</div>'
    '</div></body></html>')


async def r_plans_page(request):
    """收费功能独立页：套餐选择卡片 + 付款。账号中心只留账号本身。"""
    if not PLANS_OPEN:
        return web.Response(text=_PAGE_PLANS_OFF, content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    uid = uid_of(request)
    anon_dev = ""
    if uid is None:
        # 匿名设备也能进收银台：付款时补一次验证码建号/登录，然后认领本机
        anon_dev = dev_of(request) or ""
        if not anon_dev:
            raise web.HTTPFound("/r/")
    db = _db(request.app)
    if uid is not None:
        cur, exp, cur_sku, next_sku, _next_days = plan_state(db, uid)
        cur = cur if _is_paid_plan(cur) else "free"
    else:
        cur, exp = "free", 0
    (free_name, pro_name, max_name, month, max_month, ffeat, pfeat, mfeat,
     chips, maxchips, packs, ptab) = _plans_cards_html()
    if cur != "free":
        # 内部档不在三张卡里，名字也得从 plans.json 现取，不能只认 Pro/Max
        pname = (plans().get(cur) or {}).get("name") or cur
        curtxt = pname + (" · 至 " + time.strftime("%Y-%m-%d", time.localtime(exp)) if exp > time.time() else "")
        freebtn = "到期后自动回到免费版"
    else:
        curtxt, freebtn = free_name, "免费使用中"
    if anon_dev:
        curtxt = free_name + "（本机）"
    html = (_PAGE_PLANS.replace("__CSS__", _CSS)
            .replace("__CUR__", curtxt).replace("__FREENAME__", free_name)
            .replace("__PRONAME__", pro_name).replace("__MAXNAME__", max_name)
            .replace("__MONTH__", month).replace("__MAXMONTH__", max_month)
            .replace("__FREEFEAT__", ffeat).replace("__PROFEAT__", pfeat)
            .replace("__MAXFEAT__", mfeat)
            .replace("__CHIPS__", chips).replace("__MAXCHIPS__", maxchips)
            .replace("__PACKS__", packs).replace("__PTABLE__", ptab)
            .replace("__FREEBTN__", freebtn)
            .replace("__ANON__", "1" if anon_dev else "0")
            .replace("__ANONBOX__", "" if anon_dev else "none"))
    # App 内嵌（iframe）：返回入口由 App 自己那一条负责，这里不再画第二条
    emb = request.query.get("emb") == "1"
    html = (html.replace("__EMB__", "1" if emb else "0")
                .replace("__BACK__", "" if emb else
                         ('<a class="back" href="/r/account" onclick="if(history.length>1)'
                          '{history.back();return false}">‹ 返回</a>') if anon_dev else
                         ('<a class="back" href="/r/" onclick="if(history.length>1)'
                          '{history.back();return false}">‹ 返回</a>'))
                .replace("__H1__", "" if emb else "<h1>套餐与续费</h1>"))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


_PAGE_ACCT = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>CodeBoss · 账号中心</title>__CSS__</head><body>
<h1><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#C9D1D9" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5c.9-3.6 3.9-5.5 7.5-5.5s6.6 1.9 7.5 5.5"/></svg> 账号中心 __DEVLINK__</h1>
<div class="card" id="pwwarn" style="__PWSHOW__;border-color:#E3B341">
  <div><b>先设置你自己的密码</b></div>
  <div class="meta" style="margin-top:4px;line-height:1.7">初始密码就是注册时收到的验证码——别人看到那条短信就能登录。
    现在设置一个只有你知道的密码；设置后其他设备会自动下线。</div>
</div>
<div style="__RESTSHOW__">
<div class="card">
  <div>账号：<b>__NAME__</b></div>
  <div class="meta" style="margin-top:4px">绑定邮箱：__EMAIL__</div>
  <div class="meta" style="margin-top:4px">绑定手机：__PHONE__</div>
  <div class="meta" style="margin-top:4px">换绑邮箱后即可自助找回密码；改密码会踢下线其他已登录设备。</div>
</div>
<div class="card">
  <div>会员：<b id="pl">…</b></div>
  <div class="meta" id="plu" style="margin-top:4px">额度加载中…</div>
  __PLANSLINK__
</div>
<div class="card">
  <div class="meta" style="margin-bottom:6px">__EMAILT__</div>
  <div class="row"><input id="be" placeholder="新邮箱" style="flex:1;margin:0" type="email">
    <button id="bcb" style="flex:none" onclick="sendBindCode()">发验证码</button></div>
  <div class="row"><input id="bc" placeholder="邮箱验证码" style="flex:1;margin:0" inputmode="numeric" maxlength="6">
    <button style="flex:none" onclick="bindEmail()">绑定</button></div>
</div>
<div class="card">
  <div class="meta" style="margin-bottom:6px">__PHONET__</div>
  <div class="row"><input id="ph" placeholder="新手机号" style="flex:1;margin:0" type="tel" inputmode="tel" maxlength="11">
    <button id="pbcb" style="flex:none" onclick="sendPhoneCode()">发验证码</button></div>
  <div class="row"><input id="pc" placeholder="短信验证码" style="flex:1;margin:0" inputmode="numeric" maxlength="6">
    <button style="flex:none" onclick="bindPhone()">绑定</button></div>
  <div class="err" id="pe"></div>
</div>
</div>
<div class="card">
  <div class="meta" style="margin-bottom:6px">__PWT__</div>
  __PWROW__
  <div class="err" id="ae"></div>
</div>
<div style="__RESTSHOW__">
<div class="row"><button class="gray" style="flex:1" onclick="location='/r/logout'">退出登录</button></div>
<div class="meta center" style="margin-top:14px">
  <a href="/r/terms" style="color:var(--mut)">服务条款</a>
  <span style="color:var(--line)"> · </span>
  <a href="/r/privacy" style="color:var(--mut)">隐私政策</a>
</div>
</div>
<script>
function post(ep,body,cb){var x=new XMLHttpRequest();x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');
  var done=false,timedOut=false;
  function fin(d){if(done)return;done=true;cb(d)}
  x.timeout=15000;                      /* 别永远停在「发送中」 */
  x.ontimeout=function(){timedOut=true};
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};
    if(timedOut)d={ok:false,error:'等待超时：若已收到验证码可以直接填；没收到就再点一次'};
    else if(!x.status)d={ok:false,error:'网络不通，请检查连接后重试'};
    else{try{d=JSON.parse(x.responseText)}catch(e){}}
    fin(d)};
  x.send(JSON.stringify(body))}
var bcd=0;
function sendBindCode(){ if(bcd>0)return;
  post('/r/send_code',{purpose:'bind',email:be.value.trim()},function(d){
    ae.textContent=d.ok?(d.msg||'验证码已发送'):(d.error||'发送失败');
    if(!d.ok)return;
    bcd=60;bcb.disabled=true;bcb.textContent='60s';
    var t=setInterval(function(){bcd--;bcb.textContent=bcd>0?bcd+'s':'发验证码';
      if(bcd<=0){clearInterval(t);bcb.disabled=false;}},1000);
  });
}
function bindEmail(){
  post('/r/bind_email',{email:be.value.trim(),code:bc.value.trim()},function(d){
    if(d.ok)location.reload(); else ae.textContent=d.error||'绑定失败';
  });
}
var pcd=0;
function sendPhoneCode(){ if(pcd>0)return;
  post('/r/send_sms',{purpose:'bind',phone:ph.value.trim()},function(d){
    pe.textContent=d.ok?(d.msg||'验证码已发送'):(d.error||'发送失败');
    if(!d.ok)return;
    pcd=60;pbcb.disabled=true;pbcb.textContent='60s';
    var t=setInterval(function(){pcd--;pbcb.textContent=pcd>0?pcd+'s':'发验证码';
      if(pcd<=0){clearInterval(t);pbcb.disabled=false;}},1000);
  });
}
function bindPhone(){
  post('/r/bind_phone',{phone:ph.value.trim(),code:pc.value.trim()},function(d){
    if(d.ok)location.reload(); else pe.textContent=d.error||'绑定失败';
  });
}
var PWSET=__PWSET__;   /* 1=首次设置：不输旧密码，新密码确认两遍，成功直接去接入开发机 */
function changePass(){
  if(PWSET){
    if(np.value.length<6){ae.textContent='新密码至少 6 位';return;}
    if(np.value!==np2.value){ae.textContent='两次输入的新密码不一致';return;}
    post('/r/changepass',{old:'',new:np.value},function(d){
      if(d.ok)location.href='/';else ae.textContent=d.error||'设置失败';
    });
    return;
  }
  post('/r/changepass',{old:op.value,new:np.value},function(d){
    if(d.ok){ae.textContent='密码已修改，其他设备已下线';op.value=np.value='';
      try{document.getElementById('pwwarn').style.display='none'}catch(e){}
      try{history.replaceState(null,'','/r/account')}catch(e){}}
    else ae.textContent=d.error||'修改失败';
  });
}
function planRefresh(){
  var x=new XMLHttpRequest();x.open('GET','/api/plan',true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;var d={};try{d=JSON.parse(x.responseText)}catch(e){}
    if(!d.ok)return;
    pl.textContent=d.name+(d.plan!=='free'?'（至 '+new Date(d.expires*1000).toLocaleDateString()+'）':'');
    var u=d.usage||{},l=d.limits||{};
    /* 套餐和时长包分开写：加总后的「上限」分不出套餐和包，用户会误解 */
    var _uc=Number(l.use_min_day||0), _um=Number(u.use_min_day||0), _cr=Number(u.use_credit_min||0);
    plu.textContent='远程今日 '+_um+' 分钟；'+
      (_uc?('套餐 '+(Math.max(0,_uc-_um))+'/'+_uc+' 分钟'):'套餐不限时长')+
      '；使用时长包余 '+_cr+' 分钟；语音不限量；开发机 '+
      (u.hosts||0)+'/'+(l.hosts||'∞')+'；客户端 '+(u.clients||0)+'/'+(l.clients||'∞');
  };x.send();
}
planRefresh();
/* 注册后第一次登录会带 ?pw=1 过来：把「设置密码」那块滚进视野并聚焦 */
(function(){if(!/[?&]pw=1/.test(location.search))return;
  try{document.getElementById('pwwarn').scrollIntoView({block:'center'});
    document.getElementById(PWSET?'np':'op').focus();}catch(e){}})();
</script></body></html>"""

# ---- 服务条款 / 隐私政策（App 内与官网同一套口径） ----
# 不登录也能看：备案、应用商店审核、用户注册前都要能直接打开。
_LEGAL_CSS = """<style>
.doc{font-size:14px;line-height:1.75;color:#B6C0CC}
.doc h2{font-size:14.5px;color:#E6EDF3;margin:18px 0 6px}
.doc p{margin:6px 0}
.doc ul{margin:6px 0;padding-left:18px}
.doc li{margin:4px 0}
.doc b{color:#E6EDF3;font-weight:600}
.doc .up{color:#6E7681;font-size:12.5px}
</style>"""

_LEGAL_TERMS = """<div class="doc">
<p class="up">更新日期：2026-11-07　生效日期：2026-11-07</p>
<p>欢迎使用 aiboss 系列产品（口袋工头 CodeBoss、代理工头 AgentBoss）。本条款是你与 aiboss 系列运营者之间关于使用本服务的约定，请在使用前阅读。注册、登录或继续使用即表示你同意本条款。</p>
<h2>一、服务内容</h2>
<p>aiboss 系列产品提供用手机远程查看和操作你自己电脑上 AI 会话的工具，包括开发机端（Mac / Windows / Linux）、手机 App（安卓 / iOS）以及中继转发服务。会话、代码与命令都在你自己的电脑上运行，我们只提供连接与转发通道。</p>
<h2>二、账号</h2>
<p>一个账号由你本人使用，请妥善保管密码。通过你账号进行的操作视为你本人操作。发现账号被盗用请立即修改密码或联系我们。</p>
<h2>三、免费额度与付费</h2>
<ul>
<li>免费额度、套餐权益、使用时长包价格以「套餐与续费」页面公示为准。</li>
<li>使用时长包购买后不过期；套餐到期后自动回到免费档。</li>
<li>订单支付成功后自动开通。虚拟服务一经开通原则上不支持退款；因我方原因确实无法提供服务的除外。</li>
</ul>
<h2>四、使用规范</h2>
<p>你不得利用本服务：</p>
<ul>
<li>违反法律法规或侵害他人合法权益；</li>
<li>入侵、扫描、攻击他人系统，或用于挖矿、DDoS、群发垃圾信息等；</li>
<li>转售、出租账号或转发通道，或绕过额度与限速限制；</li>
<li>上传违法内容、恶意软件，或以自动化方式滥用接口（明显超出正常使用频率）。</li>
</ul>
<p>违反上述规范的，我们可以限速、暂停或终止服务，已支付费用不予退还；涉嫌违法的，依法移交有关部门。</p>
<h2>五、服务变更与中断</h2>
<p>我们会尽力保持服务可用，但因维护升级、网络故障、云服务商或支付、语音识别等第三方服务故障导致的短暂中断不可避免。因不可抗力或第三方原因造成的中断，我们不承担赔偿责任。重大变更会尽量提前在 App 或官网公告。</p>
<h2>六、知识产权</h2>
<p>本服务的软件、界面与商标归运营者所有。你电脑上的代码、数据以及你生成的内容归你所有。</p>
<h2>七、免责声明</h2>
<p>本服务是遥控工具，不参与代码生成与决策。AI 输出与命令执行存在风险，重要操作前请自行确认；请为远程操作设置必要的确认与权限控制。因你执行命令、授权 AI 修改代码造成的损失由你自行承担。</p>
<h2>八、责任限制</h2>
<p>在法律允许的范围内，我们不对间接损失、利润损失或数据丢失承担责任；就直接损失的赔偿总额以你近 12 个月实际支付的费用为上限。</p>
<h2>九、服务终止</h2>
<p>你可以随时停止使用并申请注销账号。你严重违反本条款时，我们可以终止向你提供服务。</p>
<h2>十、适用法律与争议解决</h2>
<p>本条款适用中华人民共和国法律。争议先协商解决；协商不成的，提交运营者所在地有管辖权的人民法院。</p>
<h2>十一、条款更新</h2>
<p>条款更新后会在 App 与官网公示，继续使用即视为接受更新后的条款。</p>
<p class="up">联系方式：App「设置 → 问题与建议」。</p>
</div>"""

_LEGAL_PRIVACY = """<div class="doc">
<p class="up">更新日期：2026-11-07　生效日期：2026-11-07</p>
<p>本政策说明 aiboss 系列产品（口袋工头 CodeBoss、代理工头 AgentBoss）在提供手机远程控制服务时如何收集、使用和保护你的个人信息。</p>
<h2>一、我们收集哪些信息</h2>
<ul>
<li><b>账号信息</b>：用户名、密码（只保存加盐哈希，无法还原）、你选择绑定的邮箱或手机号（用于验证码、找回密码和到期提醒）。</li>
<li><b>设备与配对信息</b>：开发机设备编号、备注名、配对关系、最近在线时间；手机端标识（客户端编号、机型或浏览器标识），用于区分多台设备和核算使用时长。</li>
<li><b>使用与计费记录</b>：远程使用分钟数、语音转写秒数、套餐与时长包余额、订单号、金额与支付状态。支付由第三方支付平台处理，我们不接触银行卡号与支付密码。</li>
<li><b>提醒转发</b>：开发机产生的提醒标题与文案会经中继实时转发到你的手机。中继只在转发过程中接触这些内容，不把会话内容、代码或文件写入数据库。</li>
<li><b>语音</b>：按住说话时的录音上传到中继，转码后交第三方语音识别服务转成文字；转写完成后录音即从临时目录删除，不做留存。转写文字用于执行指令，并可用于改进本账号的术语识别，你可随时清空。</li>
<li><b>反馈信息</b>：你主动提交的问题描述、联系方式和附件。</li>
<li><b>日志</b>：访问时间、IP 地址、接口路径与错误信息，用于安全防护和故障排查。</li>
</ul>
<h2>二、我们不收集、不上传的内容</h2>
<ul>
<li>你的会话内容、代码文件与终端输出不会上传到我们的服务器（你主动触发的语音指令和提醒文案转发除外）。</li>
<li>模型供应商与 cc-switch 的 API 密钥只保存在你自己的电脑上，仅在开发机进程内存中读取，不出现在日志和接口响应中。</li>
<li>我们不读取你电脑上的其他文件，不收集通讯录、相册和精确位置。</li>
</ul>
<h2>三、信息的使用</h2>
<p>用于提供服务、核算额度与计费、防刷与账号安全、故障排查、产品改进和必要的服务通知（到期、异常告警）。我们不做广告推送，不出售或出租你的个人信息。</p>
<h2>四、第三方服务</h2>
<p>为了实现功能，我们会在必要范围内使用：阿里云（短信验证码、语音识别、云服务器与对象存储）、第三方支付平台（订单支付）、邮件服务（验证码与通知）。我们要求这些服务商按本政策保护你的信息。</p>
<h2>五、存储与安全</h2>
<p>数据存储于中国境内服务器；传输使用 HTTPS；密码加盐哈希保存；数据库定期备份并限制访问；管理操作留有日志。请妥善保管账号密码——互联网传输无法保证绝对安全。</p>
<h2>六、保存期限</h2>
<p>账号信息保存至账号注销；使用与订单记录按法律法规要求保存（不少于 3 年）；访问日志最长保存 90 天；反馈工单在处理完成后 2 年内删除或匿名化。</p>
<h2>七、你的权利</h2>
<p>你可以访问、更正、复制、删除自己的个人信息，撤回同意并注销账号。在 App「账号中心」或「设置 → 问题与建议」提交申请，我们会在 15 个工作日内处理。注销后设备解绑，个人信息删除或匿名化（法律法规要求保留的交易记录除外）。</p>
<h2>八、未成年人</h2>
<p>本服务面向成年人。未满 14 周岁的未成年人不得注册使用；已满 14 周岁未满 18 周岁的，应在监护人同意和指导下使用。</p>
<h2>九、政策更新</h2>
<p>政策变更会在 App 与官网公示；重大变更会通过站内通知提醒。</p>
<p class="up">联系方式：App「设置 → 问题与建议」。</p>
</div>"""


def _legal_page(title, body, emb=False, app="CodeBoss"):
    # App 内嵌（iframe）：标题和「返回」都交给 App 那条栏，这里不再画
    head = "" if emb else (
        "<h1>%s <a href=\"/r/\" style=\"margin-left:auto;font-size:14px;"
        "text-decoration:none\">返回</a></h1>" % title)
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,"
            "viewport-fit=cover\"><title>%s · %s</title>%s%s</head><body>"
            "%s<div class=\"card\">%s</div>"
            "</body></html>") % (app, title, _CSS, _LEGAL_CSS, head, body)


def _legal_render(request, title, body):
    """条款/隐私页：正文是**系列口径**（两款端名都列出来），标题按线换端名——
    ?product=agentboss 出「AgentBoss · 服务条款」。
    正文不再做字面替换：2026-11-18 改成系列叙事后再整页替换会把「口袋工头
    CodeBoss」改成「口袋工头 AgentBoss」。"""
    product = branding.valid_product(request.query.get("product"))
    return _legal_page(title, body, emb=request.query.get("emb") == "1",
                       app=branding.product_name(product)["app"])


async def r_terms(request):
    return web.Response(text=_legal_render(request, "服务条款", _LEGAL_TERMS),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


async def r_caps(request):
    """能力协商（公开端点，host 拉去随 /api/brand 透给页面）：官方部署照常全开；
    自建模式各键按 caps_payload 收敛。页面只在 caps[k]===false 时藏入口。"""
    return web.json_response(caps_payload(), headers={"Cache-Control": "no-store"})


async def r_privacy(request):
    return web.Response(text=_legal_render(request, "隐私政策", _LEGAL_PRIVACY),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


async def r_account_page(request):
    """账号中心独立页：用户名/绑定邮箱服务端注入，仅登录可见。
    匿名设备（免注册免费版）看到的是「本机设备页」：名称 + 今日额度 + 开通 Pro。"""
    uid = uid_of(request)
    if uid is None:
        dev = dev_of(request)
        if dev:
            return web.Response(text=_anon_dev_page_html(_db(request.app), dev),
                                content_type="text/html",
                                headers={"Cache-Control": "no-store"})
        raise web.HTTPFound("/r/")
    row = _db(request.app).execute(
        "SELECT name,email,phone,must_change_pw FROM users WHERE id=?", (uid,)).fetchone()
    name = (row[0] or "").replace("&", "&amp;").replace("<", "&lt;")
    mc = bool(row[3])   # 首次（还在用注册验证码当密码）：整页只剩设置密码
    if mc:
        pwt, pwrow = "设置密码", (
            '<div class="row"><input id="np" type="password" placeholder="新密码(≥6位)" '
            'style="flex:1;margin:0">\n'
            '<input id="np2" type="password" placeholder="确认新密码" style="flex:1;margin:0">\n'
            '<button style="flex:none" onclick="changePass()">设置密码</button></div>')
    else:
        pwt, pwrow = "修改密码", (
            '<div class="row"><input id="op" type="password" placeholder="旧密码" style="flex:1;margin:0">\n'
            '<input id="np" type="password" placeholder="新密码(≥6位)" style="flex:1;margin:0">\n'
            '<button style="flex:none" onclick="changePass()">改密码</button></div>')
    html = (_PAGE_ACCT.replace("__CSS__", _CSS).replace("__NAME__", name)
            .replace("__PWSHOW__", "" if mc else "display:none")
            .replace("__RESTSHOW__", "display:none" if mc else "")
            .replace("__DEVLINK__", "" if mc else
                     '<a href="/r/" style="margin-left:auto;font-size:14px;text-decoration:none">我的开发机</a>')
            .replace("__PWT__", pwt).replace("__PWROW__", pwrow)
            .replace("__PWSET__", "1" if mc else "0")
            .replace("__PLANSLINK__", "" if not PLANS_OPEN else
                     '<a href="/r/plans" style="display:block;margin-top:10px;background:var(--ctl);'
                     'border:1px solid var(--line);border-radius:8px;padding:10px;text-align:center;'
                     'font-size:14px;text-decoration:none;color:var(--txt)">'
                     '套餐与续费 · 查看权益 ›</a>')
            .replace("__EMAIL__", row[1] or "未绑定")
            .replace("__PHONE__", (row[2] or "未绑定"))
            .replace("__EMAILT__", "换绑邮箱" if row[1] else "绑定邮箱")
            .replace("__PHONET__", "换绑手机" if row[2] else "绑定手机"))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


def _plan_anon(db, dev, name):
    """匿名设备（免注册免费版）的 /api/plan：免费档额度 + 今日用量，不带账号字段。
    页面/客户端据此显示倒计时与「开通 Pro」引导。"""
    subj = subject_of(db, None, dev)
    lim = plan_limits(db, subj)
    _ad, _acapd, _am, _acapm = _asr_quota(db, dev)
    return web.json_response({
        "ok": True, "anon": True, "plan": "free", "name": "免费版",
        "device": {"id": dev, "name": name},
        "limits": lim, "features": plan_features(db, subj),
        "account": {"name": name, "email": "", "phone": ""},
        "usage": {"asr_day": _ad, "asr_month": _am,
                  "hosts": 1, "clients": clients_used(db, subj),
                  "sends_day": sends_used(db, subj),
                  "llm_day": usage_get(db, _llm_key(db, None, dev)),
                  "use_used_sec": use_used(db, subj),
                  "use_cap_sec": use_cap_eff(db, subj),
                  "use_credit_sec": 0, "use_unit": meter_unit(db)},
    })


async def h_plan(request):
    """GET /api/plan —— 当前账号套餐/额度/用量。浏览器带 r_sess，开发机带 X-CB-Dev/X-CB-Sec。
    客户端只拿它做展示和引导，真正的闸门在各自的使用点（语音、配对）。"""
    db = _db(request.app)
    uid = uid_of(request)
    if uid is None:
        dev = _asr_dev(request)
        if dev:
            row = db.execute("SELECT owner, name FROM devices WHERE id=?", (dev,)).fetchone()
            if row and row[0] is None:
                return _plan_anon(db, dev, row[1] or "")   # 匿名设备：返回免费档视图
            uid = row[0] if row else None
    if not uid:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    # 当前档位/到期日/排队换档一次拿全：降档是「预约」的，现套餐到期才生效
    plan, exp, cur_sku, next_sku, next_days = plan_state(db, uid)
    if plan == "free":                       # 到期后不再回传档位：那时用户要的是「开通」
        cur_sku, next_sku, next_days = "", "", 0
    lim = plan_limits(db, uid)
    kd = "asr:u%d:d:%s" % (uid, time.strftime("%Y-%m-%d"))
    km = "asr:u%d:m:%s" % (uid, time.strftime("%Y-%m"))
    hosts = db.execute("SELECT COUNT(*) FROM devices WHERE owner=?", (uid,)).fetchone()[0]
    arow = db.execute("SELECT name,email,phone FROM users WHERE id=?", (uid,)).fetchone()
    account = ({"name": (arow[0] or ""), "email": _mask_email(arow[1] or ""),
                "phone": _mask_phone(arow[2] or "")} if arow else {})
    # 套餐详情：客户端（Windows/Mac 套餐页）要展示「开发机/客户端/时长/智能次数」，
    # 不能靠前端写死档位名——后台能增删包和档位。老客户端忽略这些新字段。
    plan_meta = {k: {"name": str(v.get("name") or k),
                     "limits": dict(v.get("limits") or {}),
                     "features": dict(v.get("features") or {})}
                 for k, v in (plans() or {}).items()
                 if k not in ("skus", "packs")        # plans.json 里夹带的价目表，不是档位
                 and isinstance(v, dict) and v.get("sell") is not False}
    pmin = pack_min()
    skus = {}
    for k, v in pay_skus().items():
        if k in pmin:                 # 时长包：days 恒为 0，分钟数看 min
            skus[k] = {"name": v[0], "money": v[1], "days": 0, "tier": "pack",
                       "min": int(pmin[k]), "sell": pack_sold(k)}
        else:
            skus[k] = {"name": v[0], "money": v[1], "days": int(v[2] or 0),
                       "tier": "max" if str(k).startswith("max") else "pro",
                       "sell": True}
    out = web.json_response({
        "ok": True, "plan": plan,
        "name": (plans().get(plan) or {}).get("name") or plan,
        "sku": cur_sku,          # 当前订阅档位（max_year 等）；免费/到期为空
        # 已付款、等现套餐到期再生效的换档（降档/缩短）：无则 None
        "next": ({"sku": next_sku,
                  "name": ((pay_skus().get(next_sku) or ("",))[0] or ""),
                  "from": _queue_start(exp), "days": int(next_days or 0)}
                 if next_sku else None),
        "account": account,
        "expires": float(exp or 0),
        "limits": lim, "features": plan_features(db, uid),
        "usage": {"asr_day": usage_get(db, kd), "asr_month": usage_get(db, km),
                  "hosts": hosts, "clients": clients_used(db, uid),
                  "credit": credits_of(db, uid),
                  # 以下三个 *use_min* 字段是给老客户端的兼容值（分钟，向下取整）：
                  # 2026-11-08 起内部按秒记账，新客户端读 use_used_sec/use_credit_sec/use_cap_sec。
                  "use_credit_min": use_credit_of(db, uid) // 60,
                  "use_cap_eff": use_cap_eff(db, uid) // 60,
                  "use_min_day": use_used(db, uid) // 60,
                  # 秒口径（新）：口令倒计时按这里算，刻度见 use_unit
                  "use_used_sec": use_used(db, uid),
                  "use_credit_sec": use_credit_of(db, uid),
                  "use_cap_sec": use_cap_eff(db, uid),
                  "use_unit": meter_unit(db),
                  # 今天发了多少条消息（免费版 sends_day=30，0=不限）。读/看不计。
                  "sends_day": sends_used(db, uid),
                  "llm_day": usage_get(db, "llm:u%d:d:%s"
                                       % (uid, time.strftime("%Y-%m-%d")))},
        "plans": plan_meta,
        "skus": skus,
        # 体验月是否已经买过（付过款）：页面据此把体验价置灰，而不是等点下去才报错。
        # 只认已付款；待支付的单由 /r/pay/create 挡，页面刷新后它可能已被超时关闭。
        "trial_used": bool(db.execute(
            "SELECT 1 FROM orders WHERE uid=? AND sku='trial' AND status=1",
            (uid,)).fetchone()),
        # 跨档升级的补差价报价（Pro→Max）：页面把「升级 · ¥xx」换成「补差价 ¥yy」
        "quotes": _upgrade_quotes(db, uid),
        # 客户端（App 页面就在中继源上）拿它拼账号中心地址：少了它，「开通/续费」会以为没接中继。
        # 反代/隧道后面 scheme 会丢，优先用转发头，免得 https 页面被带去 http 丢会话。
        "relay_base": "%s://%s" % (
            (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
            or request.scheme,
            request.headers.get("Host") or request.host),
    })
    # 页面每 30 秒拉一次这里：顺手给安卓壳种/续「后台提醒」cookie。
    # 只给浏览器会话种，开发机自证（X-CB-Dev）不种。
    if uid_of(request) is not None:
        eu = db.execute("SELECT sess_epoch FROM users WHERE id=?", (uid,)).fetchone()
        bg_cookie(out, request, uid, int((eu[0] if eu else 0) or 0))
    return out


async def _pay_create(request, db, uid, data):
    """下单核心：网页会话和开发机自证都走这里（uid 已确定）。"""
    if not (ZPAY_PID and ZPAY_KEY):
        return web.json_response({"ok": False, "error": "中继还没配置收款（ZPAY_PID/ZPAY_KEY）"},
                                 status=503)
    sku = str(data.get("plan") or "month")
    skus = pay_skus()
    if sku not in skus or not pack_sold(sku):
        # 内部包/内部档不进价目表，也不能被直接下单（sku 是猜不出但接口要挡）
        return web.json_response({"ok": False, "error": "套餐不存在"})
    typ = "wxpay" if str(data.get("type") or "") == "wxpay" else "alipay"
    if sku == "trial":
        # 体验价每个账号限一次。待支付（0）也要挡：放行会让人先连开两单再一起付，
        # 两笔都按体验价到账＝两个体验月。已关闭/支付的超时单（-1）不挡，失败后可以重试。
        t = db.execute("SELECT status FROM orders WHERE uid=? AND sku='trial' "
                       "AND status IN (0,1) ORDER BY created DESC LIMIT 1", (uid,)).fetchone()
        if t:
            if int(t[0] or 0) == 1:
                return web.json_response({"ok": False, "error": "体验价只能买一次"})
            return web.json_response({"ok": False, "error": "你有一笔体验月订单还没付款，先完成那一笔"})
    if not rate_ok(request.remote or "?", "pay", 20):
        return web.json_response({"ok": False, "error": "下单太频繁，稍后再试"})
    name, money, days = skus[sku]
    # 套餐状态只查一次：升级报价、排队冲突检查、冻结到期日都用同一份快照。
    st = plan_state(db, uid) if not pack_min().get(sku, 0) else None
    if st:
        why = _queue_conflict(db, uid, sku, st)
        if why:
            return web.json_response({"ok": False, "error": why})
    # 跨档升级（Pro→Max）才补差价：抵扣额/时长在下单这一刻冻结进订单，
    # 到账时不再现算（付款期间用户可能又下了别的单，现算会把钱算错）。
    q = _upgrade_quote(db, uid, sku, st=st) if st else None
    credit, gdays, from_sku = 0.0, 0, ""
    if q:
        money = "%.2f" % q["pay"]
        # 只冻结「抵扣溢出」的额外天数：基础时长改按自然周期算（见 period_n）
        credit, gdays, from_sku = q["credit"], q["overflow"], q["from"]
        name = name + "（升级补差价）"
    # 周期、天数与「下单时的到期日」一起冻结：到账时后台改了天数就按旧值给，
    # 付款前续出来的时间（e0 - base_exp）升级时再补回去。
    period_n = _sku_months(sku) if st else 0
    days_frozen = int(days or 0)
    base_exp = float(st[1] or 0) if st else 0.0
    otn = "CB" + time.strftime("%Y%m%d%H%M%S") + "%04d" % secrets.randbelow(10000)
    db.execute("INSERT INTO orders(out_trade_no,uid,sku,money,status,created,pmin,"
               "credit,days_grant,from_sku,days_frozen,base_exp,period_n) "
               "VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?)",
               (otn, uid, sku, money, time.time(), pack_min().get(sku, 0),
                credit, gdays, from_sku, days_frozen, base_exp, period_n))
    db.commit()
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip() \
        or request.scheme
    origin = "%s://%s" % (proto, request.headers.get("Host") or request.host)
    params = {"pid": ZPAY_PID, "type": typ, "out_trade_no": otn,
              "notify_url": origin + "/r/pay/notify",
              "return_url": origin + "/r/plans?pay=1",
              "name": name, "money": money, "clientip": request.remote or "",
              "device": "mobile", "param": "u%d" % uid}
    try:
        d = await asyncio.get_event_loop().run_in_executor(None, _zpay_post,
                                                           "/mapi.php", params)
    except Exception as e:
        db.execute("UPDATE orders SET status=-1 WHERE out_trade_no=?", (otn,))
        db.commit()
        print("[pay] 下单失败 otn=%s %s" % (otn, type(e).__name__), flush=True)
        return web.json_response({"ok": False, "error": "支付网关暂时不可用，稍后再试"})
    if int(d.get("code") or 0) != 1:
        db.execute("UPDATE orders SET status=-1 WHERE out_trade_no=?", (otn,))
        db.commit()
        return web.json_response({"ok": False, "error": str(d.get("msg") or "下单失败")[:160]})
    print("[pay] 已下单 otn=%s uid=%s sku=%s %s" % (otn, uid, sku, money), flush=True)
    return web.json_response({"ok": True, "out_trade_no": otn,
                              "payurl": d.get("payurl") or d.get("payurl2") or "",
                              "qrcode": d.get("qrcode") or "", "img": d.get("img") or ""})


def _sess_cookies(resp, request, db, uid):
    """给响应补账号登录 cookie（收银台建号后立刻是登录态，后续页面不用再登）。"""
    row = db.execute("SELECT sess_epoch FROM users WHERE id=?", (uid,)).fetchone()
    epoch = int((row[0] if row else 0) or 0)
    resp.set_cookie("r_sess", make_sess(request.app["key"], uid, epoch),
                    max_age=86400 * 7, httponly=True, samesite="Lax",
                    secure=(request.scheme == "https"))
    try:
        bg_cookie(resp, request, uid, epoch)
    except Exception:
        pass
    return resp


async def _anon_checkout_account(request, db, data):
    """收银台内建号/登录（匿名设备付费）：手机号或邮箱 + 验证码。
    账号不存在就建（规则同注册页），存在就按验证码登录。返回 (uid, 错误响应)。"""
    code = str(data.get("code") or "").strip()
    phone = _norm_phone(data.get("phone"))
    email = str(data.get("email") or "").strip().lower()
    ip = request.remote or "?"
    if not rate_ok(ip, "auth", 30):
        return None, web.json_response({"ok": False, "error": "尝试过多，1 小时后再试"})
    if not code:
        return None, web.json_response({"ok": False, "error": "请填验证码"})
    if phone:
        if not _PHONE_RE.match(phone):
            return None, web.json_response({"ok": False, "error": "手机号格式不正确"})
        email = ""
    elif email:
        if not _EMAIL_RE.match(email):
            return None, web.json_response({"ok": False, "error": "邮箱格式不正确"})
        phone = ""
    else:
        return None, web.json_response({"ok": False, "error": "请填手机号或邮箱"})

    async def _ok(purpose):
        if phone:
            return await _phone_verify(db, phone, code, purpose)
        return _use_code(db, email, code, purpose)

    row = (db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone() if phone
           else db.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone())
    if row:                          # 已有账号：验证码登录（发码用途可能是 register 或 login）
        if not (await _ok("register") or await _ok("login")):
            return None, web.json_response({"ok": False, "error": "验证码错误或已过期"})
        return row[0], None
    if not await _ok("register"):
        return None, web.json_response({"ok": False, "error": "验证码错误或已过期"})
    name = _auto_name(db, email, phone)
    salt = secrets.token_hex(8)
    pw0 = str(data.get("pass") or data.get("password") or "").strip()
    use_pw = pw0 if len(pw0) >= 6 else code
    must = 0 if use_pw != code else 1
    dev = dev_of(request) or ""
    prow = db.execute("SELECT product FROM devices WHERE id=?", (dev,)).fetchone() if dev else None
    prod = branding.valid_product(prow[0] if prow else None)
    try:
        cur = db.execute(
            "INSERT INTO users(name,pass_hash,salt,created,sess_epoch,email,phone,"
            "must_change_pw,product) VALUES(?,?,?,?,0,?,?,?,?)",
            (name, hash_pw(use_pw, salt), salt, time.time(), email, phone, must, prod))
    except sqlite3.IntegrityError:
        return None, web.json_response({"ok": False, "error": "该手机号或邮箱已被占用"})
    db.commit()
    print("[relay] 收银台建号 %s（%s）dev=%s ip=%s"
          % (name, "手机" if phone else "邮箱", dev, ip), flush=True)
    return cur.lastrowid, None


async def r_pay_create(request):
    """网页端下单：账号会话直接用；匿名设备会话先建号/登录，再认领本机。"""
    uid = uid_of(request)
    db = _db(request.app)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if uid is None:
        dev = dev_of(request)
        if not dev:
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        uid, err = await _anon_checkout_account(request, db, data)
        if err:
            return err
        if not _claim_device(db, uid, dev):
            return web.json_response({"ok": False, "error": "本机已绑定其他账号"}, status=403)
        resp = await _pay_create(request, db, uid, data)
        _sess_cookies(resp, request, db, uid)
        return resp
    return await _pay_create(request, db, uid, data)


async def h_pay_create(request):
    """开发机端下单（设备凭证自证，不需要网页登录）：Mac 客户端内直接选套餐付款用。"""
    db = _db(request.app)
    try:
        data = await request.json()
    except Exception:
        data = {}
    (_dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机还没绑定账号（请先在手机上配对）"})
    return await _pay_create(request, db, owner, data)


async def r_pay_notify(request):
    """Z-PAY 异步通知（GET）：验签 → 对金额 → 二次确认 → 记已付并延长 Pro。"""
    q = {str(k): str(v) for k, v in request.query.items()}
    if not q.get("sign") or not hmac.compare_digest(q.get("sign", ""), _zpay_sign(q)):
        print("[pay] notify 验签失败 ip=%s" % (request.remote or "?"), flush=True)
        return web.Response(text="fail")
    if q.get("trade_status") != "TRADE_SUCCESS":
        return web.Response(text="fail")
    otn = q.get("out_trade_no") or ""
    db = _db(request.app)
    row = db.execute("SELECT uid,sku,money,status FROM orders WHERE out_trade_no=?",
                     (otn,)).fetchone()
    if not row:
        print("[pay] notify 未知订单 %s" % otn, flush=True)
        return web.Response(text="fail")
    uid, sku, money, st = row
    from decimal import Decimal
    try:
        if Decimal(str(q.get("money") or "0")) != Decimal(str(money)):
            print("[pay] notify 金额不符 otn=%s" % otn, flush=True)
            return web.Response(text="fail")
    except Exception:
        return web.Response(text="fail")
    if st == 1:   # 重复通知：直接 success（平台会重发）
        return web.Response(text="success")
    q2 = _zpay_query(otn)
    if q2 is not None and int(q2.get("code") or 0) == 1 and int(q2.get("status") or 0) != 1:
        print("[pay] notify 二次确认未支付 otn=%s" % otn, flush=True)
        return web.Response(text="fail")
    ok, why, info = _pay_apply(db, otn, q.get("trade_no") or "", q.get("type") or "")
    if ok and info and info.get("new"):
        _alert_pay(request.app, db, info)
        asyncio.create_task(_push_use_ack_uid(request.app, info.get("uid")))
    return web.Response(text="success" if ok else "fail")


def _pay_apply(db, otn, trade_no="", typ=""):
    """把订单记成已付并延长套餐/充使用时长包。支付通知和管理页手动确认共用。
    返回 (ok, 说明, {uid,sku,money,order})；重复调用幂等。"""
    row = db.execute("SELECT uid,sku,money,status,pmin,credit,days_grant,from_sku,"
                     "days_frozen,base_exp,COALESCE(period_n,0) FROM orders "
                     "WHERE out_trade_no=?", (otn,)).fetchone()
    if not row:
        return False, "订单不存在", None
    uid, sku, money, st, pmin = row[0], row[1], row[2], row[3], row[4]
    credit, gdays, from_sku = float(row[5] or 0), int(row[6] or 0), (row[7] or "")
    froze_days, base_exp = int(row[8] or 0), float(row[9] or 0)
    months = int(row[10] or 0)          # 0 = 老订单，按固定天数入账
    if st == 1:
        return True, "已到账", {"uid": uid, "sku": sku, "money": money, "order": otn,
                                "new": False}
    if sku == "trial":
        # 兜底留痕：正常路径下单那一刻就挡住第二笔体验月，走到这里说明是并发/超时单
        # 的历史遗留。钱已经收了，照常给时长，但要在日志里看得见，方便对账。
        dup = db.execute("SELECT out_trade_no FROM orders WHERE uid=? AND sku='trial' "
                         "AND status=1 AND out_trade_no<>?", (uid, otn)).fetchone()
        if dup:
            print("[pay] 体验月重复到账 otn=%s（已有 %s）uid=%s" % (otn, dup[0], uid),
                  flush=True)
    now = time.time()
    mins = int(pmin or 0) or pack_min().get(sku, 0)
    if mins:                       # 按量付费：充使用时长分钟，不续订阅
        use_credit_add(db, uid, mins, "pay", "订单 %s（%s）" % (otn, sku))
    else:
        v = pay_skus().get(sku)
        if not v or not v[2]:
            # 档位在后台被删掉、老订单又没记下分钟数：宁可挂着等人工处理，也不能错发一个月订阅
            print("[pay] 商品已下架，订单待人工处理 otn=%s sku=%s" % (otn, sku), flush=True)
            return False, "商品已下架，请人工处理", None
        # 下单时冻结的天数优先：期间后台改了档位天数，按用户下单时看到的那档给。
        # 老订单没冻结值（0）才回退到当前档位。
        days = froze_days or v[2]
        _plan_roll(db, uid)          # 排队档到点了先落地，后面算起始日才不会偏
        p0, e0, s0, _n, _nd = plan_state(db, uid)
        tier = "max" if str(sku).startswith("max_") else "pro"
        cur_sku = s0 or _cur_sku_of(db, uid)
        if p0 in ("pro", "max") and _sku_rank(sku) < _sku_rank(cur_sku):
            # 降档/缩短：钱照收，但现套餐到期前一分权益都不削，从到期那天接着生效。
            # 同一档再买一次＝再买一份时长，要累加（不然第二笔白付）。
            # 不同档也累加天数：下单时 _queue_conflict 已经挡住「换掉别人排队档」的单，
            # 这里兜底老订单——已经收过钱的排队时长绝不能因为换档归零。
            # 现套餐后来续了期，排队档自动顺延（起始日＝到期日）
            r2 = db.execute("SELECT plan_next,plan_next_days,COALESCE(plan_next_n,0) "
                            "FROM users WHERE id=?", (uid,)).fetchone()
            osku = (r2[0] or "") if r2 else ""
            old_nd, old_nm = (int(r2[1] or 0), int(r2[2] or 0)) if r2 else (0, 0)
            if osku and osku != sku and old_nm > 0:
                # 老库兜底：不同档排队时把旧的自然月折成天数，已付的钱一天都不能丢
                old_nd += int(round((_queue_end(e0, old_nm, 0) - float(e0)) / 86400.0))
                old_nm = 0
            if osku != sku:
                old_nm = 0
            if osku and osku != sku and old_nd > 0:
                print("[pay] 排队档换档，累加旧天数 %s→%s %d+%d otn=%s"
                      % (osku, sku, old_nd, days, otn), flush=True)
            nn = old_nm + (months if months > 0 else 0)
            nd = old_nd + (0 if months > 0 else days)
            db.execute("UPDATE users SET plan_next=?, plan_next_days=?, plan_next_n=? "
                       "WHERE id=?", (sku, nd, nn, uid))
        else:
            # 升级或续费：立即生效，从「现在或原到期日」往后加。排队档保留，跟着顺延
            base = max(now, float(e0 or 0))
            renew = float(e0 or 0) > now
            exp = (_period_end(base, months, base_is_expiry=renew) if months > 0
                   else base + days * 86400)
            if from_sku:                # 有 from_sku 就是补差价单（gdays 可能为 0）
                if (s0 or _cur_sku_of(db, uid)) == from_sku:
                    # 补差价单：现档剩余时间已经折成钱抵扣过，新档从现在起算，
                    # 时长＝自然周期 + 下单时冻结的抵扣溢出天数（老单是完整天数）。
                    # 但下单到付款之间用户可能又续了费（e0 > base_exp），那段没进抵扣，
                    # 按秒补回去——否则「升级前顺手续的月」会被这一单直接吞掉。
                    # 付款拖过了旧到期日时只补「还没用掉」的那部分，不把已经用掉的天数还回来。
                    delta = (max(0.0, float(e0 or 0) - max(now, base_exp))
                             if base_exp > 0 else 0.0)
                    if delta > 0:
                        print("[pay] 补差价单保留付款前续期 %.1f 天 otn=%s"
                              % (delta / 86400.0, otn), flush=True)
                    exp = (_period_end(now, months) if months > 0
                           else now) + gdays * 86400 + delta
                else:
                    print("[pay] 补差价单的现档已变，按普通升级入账 otn=%s %s→%s"
                          % (otn, from_sku, s0 or ""), flush=True)
                    # 现档已换：这单已经付过折扣价，时长按冻结值给，
                    # 叠在现有到期日后面（用户新买的那段权益不能削）。
                    base = max(now, float(e0 or 0))
                    renew = float(e0 or 0) > now
                    exp = (_period_end(base, months, base_is_expiry=renew)
                           if months > 0 else base + days * 86400)
            db.execute("UPDATE users SET plan=?, plan_expires=?, plan_sku=? WHERE id=?",
                       (tier, exp, sku, uid))
    db.execute("UPDATE orders SET status=1, paid=?, trade_no=?, type=? WHERE out_trade_no=?",
               (now, trade_no, typ, otn))
    db.commit()
    print("[pay] 已到账 otn=%s uid=%s sku=%s money=%s" % (otn, uid, sku, money), flush=True)
    return True, "已到账", {"uid": uid, "sku": sku, "money": money, "order": otn,
                            "new": True}


# ---------- 收款提醒（邮件 / 钉钉 / 企业微信 / Server酱 / PushPlus） ----------
_ALERT_META = "alert_cfg"


def _split_multi(s, cap=120, limit=20):
    """把「多个邮箱 / 多个机器人」的录入拆成列表：逗号、分号、换行都算分隔。"""
    out = []
    for part in re.split(r"[,;\n]+", str(s or "")):
        part = part.strip()[:cap]
        if part and part not in out:
            out.append(part)
        if len(out) >= limit:
            break
    return out


def _alert_cfg(db):
    """提醒渠道配置（meta 表）。每个渠道都可以配多个目标（多个邮箱/多个机器人）。
    webhook 等属于密钥，接口只回打码值。"""
    row = db.execute("SELECT v FROM meta WHERE k=?", (_ALERT_META,)).fetchone()
    try:
        d = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    out = {"pay": bool(d.get("pay", True)),
           # 新用户注册提醒：默认开（注册量小的时候每条都想看到），关掉只改这个开关
           "reg": bool(d.get("reg", True))}
    # 老配置是单条字符串，_split_multi 也能吃；统一成列表。
    for k, cap in (("email", 120), ("dingtalk", 400), ("wecom", 400),
                   ("serverchan", 200), ("pushplus", 200)):
        v = d.get(k)
        if isinstance(v, list):
            v = "\n".join(str(x) for x in v if str(x).strip())
        out[k] = _split_multi(v, cap)
    return out


def _merge_multi(old, raw, cap=120, limit=20):
    """保存多目标字段：页面回传的打码值（***尾4位）按位置换回原值，
    这样不重填旧密钥也能在后面追加新的。"""
    old = list(old or [])
    out = []
    for i, part in enumerate(_split_multi(raw, cap, limit)):
        if part.startswith("***") and i < len(old):
            out.append(old[i])
        else:
            out.append(part)
    return out


def _alert_save(db, cfg):
    db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (_ALERT_META, json.dumps(cfg, ensure_ascii=False)))
    db.commit()


# ---------- 运行配置（文件存储 OSS / 模型服务）：存 meta 表 ----------
# 都是运营侧自己填的密钥：接口只回打码值（***尾4位），页面回传打码值 = 保持原值。
_META_OSS = "oss_cfg"
_META_PROMO = "promo_cfg"
_META_MODELS = "model_cfg"


def _meta_json(db, k):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    try:
        d = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def _meta_json_save(db, k, d):
    db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(d, ensure_ascii=False)))
    db.commit()


# ---- 运营/促销策略（后台可配，注册时生效）----
# 新注册默认送一段时间的付费套餐，到期自动回免费版（plan_expires 到了就是 free）。
# 策略存在 meta 表里，后台「策略」页改完立即生效；关掉开关 = 不再送。
PROMO_DEF = {"enabled": True, "plan": "pro", "days": 30, "sku": "",
              "until": 0,          # 活动截止时间（epoch 秒）：到点就不再送
              "note": "新用户注册即送 1 个月 Pro，到期自动恢复免费版",
              "banner": 1}         # 注册/登录页是否显示活动横幅


def promo_cfg(db):
    """注册赠送策略。返回 {"enabled","plan","days","sku","note"}。"""
    d = dict(PROMO_DEF)
    try:
        got = _meta_json(db, _META_PROMO)
    except Exception:
        got = {}
    for k in PROMO_DEF:
        if k in got:
            d[k] = got[k]
    try:
        d["enabled"] = bool(d.get("enabled"))
        d["days"] = max(0, min(3650, int(d.get("days") or 0)))
    except (TypeError, ValueError):
        d["days"] = PROMO_DEF["days"]
    if str(d.get("plan") or "") not in ("pro", "max"):
        d["plan"] = "pro"
    d["sku"] = str(d.get("sku") or "")[:32]
    d["note"] = str(d.get("note") or "")[:200]
    try:
        d["until"] = int(d.get("until") or 0)
    except (TypeError, ValueError):
        d["until"] = 0
    d["banner"] = bool(d.get("banner", True))
    return d


def promo_grant(db, uid, cfg=None):
    """给新账号送促销套餐：plan/plan_expires/plan_sku 一起写，到期自动回免费版。

    返回 (是否送了, 说明)。"""
    cfg = cfg if cfg is not None else promo_cfg(db)
    if not cfg.get("enabled"):
        return False, "未开启注册赠送"
    until = int(cfg.get("until") or 0)
    now = time.time()
    if until and now > until:
        return False, "活动已结束（%s）" % time.strftime("%Y-%m-%d", time.localtime(until))
    days = int(cfg.get("days") or 0)
    if days <= 0:
        return False, "赠送天数为 0"
    tier = cfg.get("plan") or "pro"
    exp = now + days * 86400
    sku = cfg.get("sku") or ("max_month" if tier == "max" else "month")
    try:
        db.execute("UPDATE users SET plan=?,plan_expires=?,plan_sku=?,"
                   "plan_next=NULL,plan_next_days=0,plan_next_n=0 WHERE id=?",
                   (tier, exp, sku, int(uid)))
        db.commit()
    except Exception as e:
        return False, "写入失败：%s" % str(e)[:80]
    print("[relay] 注册赠送 uid=%s %s %d 天（到期回免费版）"
          % (uid, tier, days), flush=True)
    return True, "已赠送 %s %d 天" % (tier.upper(), days)


def _norm_prefix(p):
    p = str(p or "").strip().strip("/")
    return (p + "/") if p else ""


def _oss_cfg(db):
    d = _meta_json(db, _META_OSS)
    return {"on": bool(d.get("on", False)),
            "endpoint": str(d.get("endpoint") or "").strip()[:160],
            "bucket": str(d.get("bucket") or "").strip()[:100],
            "ak": str(d.get("ak") or "").strip()[:160],
            "sk": str(d.get("sk") or "").strip()[:200],
            "prefix": _norm_prefix(d.get("prefix") or "codeboss/")}


def _oss_ready(c):
    return bool(c and c.get("on") and c.get("endpoint") and c.get("bucket")
                and c.get("ak") and c.get("sk"))


def _oss_key(c, name):
    name = str(name or "").lstrip("/")
    p = _norm_prefix(c.get("prefix"))
    return (p + name) if p else name


def _oss_host(c):
    ep = str(c.get("endpoint") or "").strip()
    ep = re.sub(r"^https?://", "", ep).rstrip("/").split("/")[0]
    bk = str(c.get("bucket") or "").strip()
    # 有人会把「访问域名」（bucket.oss-cn-…）当 Endpoint 填进来：这里容错，
    # 不然会拼成 bucket.bucket.oss-cn-… 直接 DNS 失败。
    if bk and ep.startswith(bk + "."):
        ep = ep[len(bk) + 1:]
    return "%s.%s" % (bk, ep)


def _oss_call(c, method, key, data=None, ctype="application/octet-stream", timeout=60,
              extra=None):
    """OSS V1 签名的最小客户端（PUT/GET/DELETE/HEAD），只用标准库。
    返回 (ok, status, body_bytes_or_text)。网络异常抛 RuntimeError。
    extra：额外的请求头；x-oss-* 必须按 V1 规则进签名（小写、按名排序）。"""
    import email.utils
    import urllib.error
    import urllib.request
    host = _oss_host(c)
    path = urllib.parse.quote("/" + key, safe="/")
    url = "https://%s%s" % (host, path)
    date = email.utils.formatdate(usegmt=True)
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode() if data is not None else ""
    canon = "/%s/%s" % (str(c.get("bucket") or "").strip(), key)
    hdrs = dict(extra or {})
    oss_h = "".join("%s:%s\n" % (k.lower(), v) for k, v in sorted(hdrs.items())
                    if k.lower().startswith("x-oss-"))
    sts = "\n".join([method, md5, (ctype if data is not None else ""), date]) + "\n" + oss_h + canon
    sig = base64.b64encode(hmac.new(str(c.get("sk") or "").encode(), sts.encode("utf-8"),
                                    hashlib.sha1).digest()).decode()
    headers = {"Date": date, "Authorization": "OSS %s:%s" % (c.get("ak"), sig)}
    headers.update(hdrs)
    if data is not None:
        headers["Content-Type"] = ctype
        headers["Content-MD5"] = md5
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return False, e.code, e.read()[:400], dict(e.headers or {})
    except Exception as e:
        raise RuntimeError(str(e)[:160])


def _oss_put(c, key, data, ctype="application/octet-stream", timeout=60):
    # 显式指定标准存储：bucket 默认建成「归档」时，对象会继承归档，
    # 上传成功但读不回来（InvalidObjectState），备份和反馈附件就没法用了。
    # 不设对象 ACL：这个桶的权限策略拒绝 PutObjectAcl（AccessDenied: Put public
    # object acl is not allow），带上 x-oss-object-acl 整次上传都会失败。
    # 下载改走预签名直链（见 _oss_sign_url），私有对象照样能直出。
    try:
        ok, st, body, _h = _oss_call(c, "PUT", key, data=data, ctype=ctype,
                                     timeout=timeout,
                                     extra={"x-oss-storage-class": "Standard"})
        return ok, ("" if ok else ("HTTP %s %s" % (st, body.decode("utf-8", "replace")[:120])))
    except RuntimeError as e:
        return False, str(e)


def _oss_get(c, key):
    try:
        ok, st, body, _h = _oss_call(c, "GET", key)
        return (body if ok else None), ("" if ok else "HTTP %s" % st)
    except RuntimeError as e:
        return None, str(e)


def _oss_del(c, key):
    try:
        ok, st, _b, _h = _oss_call(c, "DELETE", key)
        return ok or st == 404, "HTTP %s" % st
    except RuntimeError as e:
        return False, str(e)


def _oss_test(c):
    """连通性自检：写一个探针对象 → 读回比对 → 删掉。"""
    key = _oss_key(c, "_selftest/%d.txt" % int(time.time()))
    blob = ("codeboss oss ok %s" % time.strftime("%Y-%m-%d %H:%M:%S")).encode()
    ok, err = _oss_put(c, key, blob, "text/plain; charset=utf-8")
    if not ok:
        return False, "上传失败：" + err
    got, err2 = _oss_get(c, key)
    ok2 = got == blob
    _oss_del(c, key)
    if not ok2:
        return False, "读回校验失败：%s" % (err2 or "内容不一致")
    return True, "写入/读回/删除都正常（%s/%s）" % (c.get("bucket"), key)


def _oss_list(c, prefix="", marker="", max_keys=200):
    """列对象（ListObjects V1）。V1 签名只算 /bucket/，prefix/marker 不进签名串。"""
    import email.utils
    import urllib.error
    import urllib.request
    import xml.etree.ElementTree as ET
    host = _oss_host(c)
    bucket = str(c.get("bucket") or "").strip()
    q = {"prefix": str(prefix or ""), "max-keys": str(max(1, min(1000, int(max_keys))))}
    if marker:
        q["marker"] = str(marker)
    url = "https://%s/?%s" % (host, urllib.parse.urlencode(sorted(q.items())))
    date = email.utils.formatdate(usegmt=True)
    canon = "/%s/" % bucket
    sts = "\n".join(["GET", "", "", date]) + "\n" + canon
    sig = base64.b64encode(hmac.new(str(c.get("sk") or "").encode(), sts.encode("utf-8"),
                                    hashlib.sha1).digest()).decode()
    req = urllib.request.Request(url, headers={
        "Date": date, "Authorization": "OSS %s:%s" % (c.get("ak"), sig)})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s" % (e.code, e.read()[:160].decode("utf-8", "replace")))
    except Exception as e:
        raise RuntimeError(str(e)[:160])
    try:
        root = ET.fromstring(body)
    except Exception:
        raise RuntimeError("OSS 返回的不是 XML")
    tg = lambda n: n.tag.rsplit("}", 1)[-1]
    files = []
    for it in root:
        if tg(it) != "Contents":
            continue
        d = {tg(x): (x.text or "") for x in it}
        files.append({"key": d.get("Key", ""),
                      "size": int(d.get("Size") or 0),
                      "mtime": _oss_dt(d.get("LastModified", "")),
                      "storage": d.get("StorageClass", "")})
    nxt, trunc = "", False
    for x in root:
        if tg(x) == "IsTruncated":
            trunc = (x.text or "").lower() == "true"
        elif tg(x) == "NextMarker":
            nxt = x.text or ""
    if trunc and not nxt and files:
        nxt = files[-1]["key"]
    return files, nxt


def _oss_dt(s):
    """OSS 的 LastModified（2026-11-07T13:05:00.000Z，UTC）→ epoch 秒。"""
    try:
        from datetime import datetime, timezone
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


async def _oss_put_async(app, key, data, ctype="application/octet-stream", timeout=60):
    """后台把文件送到 OSS；没配置就静默跳过。失败只写日志，不影响主流程。"""
    try:
        c = _oss_cfg(_db(app))
        if not _oss_ready(c):
            return False
        loop = asyncio.get_event_loop()
        okey = _oss_key(c, key)
        ok, err = await loop.run_in_executor(None, _oss_put, c, okey, data,
                                             ctype, timeout)
        if not ok:
            print("[relay] oss 上传失败 key=%s: %s" % (key, err), flush=True)
        else:
            # 传完立刻让下载改走 OSS：否则大小缓存里还是发布前的旧数，最长 5 分钟里
            # 新包一直由 VPS 本机慢发。
            _OSS_SIZE_CACHE[okey] = (len(data), time.time())
        return ok
    except Exception as e:
        print("[relay] oss 上传异常 key=%s: %s" % (key, str(e)[:160]), flush=True)
        return False


def _oss_mirror(app, name, blob, ctype, skip=False):
    """分发产物（APK/IPA/host 包/DMG/安装包）在本地落盘后再镜像一份到 OSS。
    下载时可以 302 到 OSS 预签名直链（VPS 上行才几 Mbps，装包十几 MB 走 OSS 快一个
    量级）；OSS 没传成/没配置时下载照旧走本机，不影响可用性。
    skip=True：发布脚本已带 X-Oss-Done 直传过同一份字节，中继不再传（省出口带宽）。"""
    try:
        if skip:
            # 直传那份可能刚换过：清掉大小缓存，下一个下载立刻 HEAD 到新对象 302 走 OSS。
            _OSS_SIZE_CACHE.pop(_oss_key(_oss_cfg(_db(app)), "dist/" + name), None)
            return
        if not blob or len(blob) > 64 * 1048576:
            return
        if name.endswith(".apk") and not OSS_DL_HOST:
            # 阿里云禁止从 OSS 默认域名提供 APK（ApkDownloadForbidden）。绑了自定义
            # 域名（OSS_DL_HOST）之后就能镜像了，APK 也不再吃中继带宽。
            return
        # 装包几十 MB、VPS 上行 ~440 KB/s：写超时要给足（默认 60s 传不完 DMG，
        # 实测 52MB 的 DMG 镜像失败：urlopen error The write operation timed out）
        traffic_add("ops", 0, len(blob), 1)
        asyncio.create_task(_oss_put_async(app, "dist/" + name, blob, ctype, 900))
    except RuntimeError:
        pass


def _oss_done(request):
    """发布脚本先直传 OSS 成功的话带上 X-Oss-Done: 1，中继就别再传一遍。"""
    return (request.headers.get("x-oss-done") or "").strip() == "1"


# 开发机能给自己签 PUT 的产物名（发布脚本那几张固定面孔，别的 key 不签）。
_OSS_PUT_NAMES = {"codeboss-mac.dmg", "bosshost-mac.dmg", "codeboss-host.tar.gz",
                  "CodeBoss-Setup.exe", "BossHost-Setup.exe", "codeboss-linux.tar.gz"}


async def r_admin_oss_put(request):
    """GET /r/admin/oss_put_url?name=…（Bearer）：签一条 OSS PUT 直链给开发机。
    AK/SK 仍只在中继；开发机拿着有时效的 URL 把包直接传进 OSS，中继只收入口那份
    （入口不花钱），出口那份几十 MB 的镜像就省了。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权"}, status=401)
    name = (request.query.get("name") or "").strip()
    if name not in _OSS_PUT_NAMES:
        return web.json_response({"ok": False, "error": "name 不合法"}, status=400)
    c = _oss_cfg(_db(request.app))
    if not _oss_ready(c):
        return web.json_response({"ok": False, "error": "OSS 未配置"})
    key = _oss_key(c, "dist/" + name)
    return web.json_response({"ok": True, "url": _oss_sign_url(c, key, 1800, "PUT")})


# ---------- 下载走 OSS（快）----------
# VPS 上行带宽有限（实测 ~440 KB/s），十几 MB 的安装包要下几十秒。对象是私有的
# （桶策略不允许设公共读 ACL），所以下载 302 到一张 V1 预签名直链——浏览器拿到的是
# 带时效的临时 URL，AK/SK 不出去。只有「OSS 上那份和本地一样大」才甩——上传失败/
# 还在传时老实本机发，免得把人送到旧包或缺页上。大小缓存 5 分钟，不是每个请求都 HEAD。
_OSS_SIZE_CACHE = {}          # oss_key -> (大小, 查询时刻)

# OSS 绑的下载自定义域名（CNAME 到 bucket）。绑上之后：① 连 APK 也能从 OSS 直发
# （默认域名被阿里云禁 .apk）；② 直链里不暴露 aliyuncs 默认域名。空 = 走默认域名。
# V1 预签名只签 method/Expires//bucket/key，不含 host，换域名同一个签名照样有效。
OSS_DL_HOST = "download.example.com"


def _oss_sign_url(c, key, expires=3600, method="GET"):
    """OSS V1 预签名直链（私有对象也能直接下，桶不用开公共读）。
    method="PUT" 给开发机直传用：包从本机直接传进 OSS，中继不再替它传一遍
    （2026-11-19 出口 2.7GB 一半是发布包镜像）。"""
    bucket = str(c.get("bucket") or "").strip()
    exp = int(time.time()) + int(expires)
    sts = "%s\n\n\n%d\n/%s/%s" % (method, exp, bucket, key)
    sig = base64.b64encode(hmac.new(str(c.get("sk") or "").encode(), sts.encode("utf-8"),
                                    hashlib.sha1).digest()).decode()
    q = urllib.parse.urlencode({"OSSAccessKeyId": str(c.get("ak") or ""),
                                "Expires": exp, "Signature": sig})
    return "https://%s%s?%s" % (OSS_DL_HOST or _oss_host(c),
                                urllib.parse.quote("/" + key, safe="/"), q)


def _oss_head_size(c, key):
    """HEAD 对象拿大小；取不到返回 -1。"""
    try:
        ok, _st, _b, h = _oss_call(c, "HEAD", key, timeout=6)
        if not ok:
            return -1
        low = {str(k).lower(): str(v) for k, v in h.items()}
        return int(low.get("content-length") or -1)
    except Exception:
        return -1


async def _oss_dl_redirect(app, oss_name, want_size):
    """本地这份文件 OSS 上有同样大的副本时，返回它的预签名直链；否则 None。"""
    try:
        c = _oss_cfg(_db(app))
        if not _oss_ready(c):
            return None
        key = _oss_key(c, "dist/" + oss_name)
        now = time.time()
        hit = _OSS_SIZE_CACHE.get(key)
        if hit and now - hit[1] < 300:
            return _oss_sign_url(c, key) if hit[0] == want_size else None
        loop = asyncio.get_event_loop()
        size = await loop.run_in_executor(None, _oss_head_size, c, key)
        _OSS_SIZE_CACHE[key] = (size, now)
        return _oss_sign_url(c, key) if size == want_size else None
    except Exception:
        return None



async def _dl_or_local(app, request, path, oss_name, ctype, filename, min_size=1024):
    """分发文件统一下载口：OSS 有同样大小的副本就 302 到预签名直链，否则本机发。"""
    if not (os.path.exists(path) and os.path.getsize(path) > min_size):
        return None
    url = await _oss_dl_redirect(app, oss_name, os.path.getsize(path))
    if url:
        return web.Response(status=302, headers={"Location": url, "Cache-Control": "no-store"})
    try:
        traffic_add("product", 0, os.path.getsize(path), 1)
    except OSError:
        pass
    return web.FileResponse(path, headers={
        "Content-Type": ctype,
        "Content-Disposition": 'attachment; filename="%s"' % filename,
        "Cache-Control": "no-store"})


# ---- 模型服务配置（DashScope）----
_M_DEF = {"asr": "qwen3-asr-flash", "tts": "qwen3-tts-flash", "voice": "Cherry",
          "llm": "qwen-flash",
          "llm_base": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"}
LLM_MODEL = os.environ.get("RELAY_LLM_MODEL", _M_DEF["llm"])
LLM_MODELS = [LLM_MODEL]
# 桌宠闲聊要人味：摘要/命令识别用 qwen-flash 够了，陪聊得换个像人的模型 + 一点温度
# （temperature 0 每一句都平、都像客服）。
PET_LLM_MODEL = os.environ.get("RELAY_LLM_PET_MODEL", "qwen-plus")
PET_LLM_TEMP = float(os.environ.get("RELAY_LLM_PET_TEMP") or "0.7")
LLM_BASE = os.environ.get("RELAY_LLM_BASE", _M_DEF["llm_base"])
FIX_MODEL_DEFAULT = os.environ.get("RELAY_FIX_MODEL", "qwen-flash")
FIX_MODELS = [FIX_MODEL_DEFAULT]


def _model_name_list(v, default, cap=8):
    """后台模型字段：允许多个（逗号/换行分隔或 JSON 数组），第一个为主，
    不可用时按顺序自动换下一个。"""
    if isinstance(v, list):
        out = [str(x).strip()[:80] for x in v if str(x).strip()]
    else:
        out = _split_multi(v, 80, cap)
    out = [x for i, x in enumerate(out) if x not in out[:i]]
    return (out or [default])[:cap]


def _model_cfg(db):
    d = _meta_json(db, _META_MODELS)
    llm = _model_name_list(d.get("llm"), LLM_MODEL)
    fr = d.get("fix")
    has_fix = bool(fr) if isinstance(fr, list) else bool(str(fr or "").strip())
    return {"key": str(d.get("key") or ""),
            "asr": _model_name_list(d.get("asr"), ASR_MODEL),
            "tts": _model_name_list(d.get("tts"), TTS_MODEL),
            "voice": str(d.get("voice") or "")[:40] or TTS_VOICE,
            "llm": llm,
            # 留空 = 跟着文本模型列表走
            "fix": _model_name_list(fr, llm[0]) if has_fix else llm,
            "llm_base": str(d.get("llm_base") or "")[:300] or LLM_BASE}


def _model_apply(db=None):
    """把后台配置套到进程全局（env 只是初值）。启动时和保存后各调一次。"""
    global ASR_KEY, ASR_MODEL, ASR_MODELS, TTS_MODEL, TTS_MODELS, TTS_VOICE
    global LLM_MODEL, LLM_MODELS, LLM_BASE, FIX_MODEL, FIX_MODELS
    if db is None:
        return
    try:
        c = _model_cfg(db)
    except Exception as e:
        print("[relay] 模型配置读取失败: %s" % str(e)[:120], flush=True)
        return
    if c["key"]:
        ASR_KEY = c["key"]
    ASR_MODELS, ASR_MODEL = c["asr"], c["asr"][0]
    TTS_MODELS, TTS_MODEL = c["tts"], c["tts"][0]
    TTS_VOICE = c["voice"]
    LLM_MODELS, LLM_MODEL = c["llm"], c["llm"][0]
    FIX_MODELS, FIX_MODEL = c["fix"], c["fix"][0]
    LLM_BASE = c["llm_base"]
    print("[relay] 模型配置 asr=%s tts=%s/%s llm=%s fix=%s key=%s"
          % (",".join(ASR_MODELS), ",".join(TTS_MODELS), TTS_VOICE,
             ",".join(LLM_MODELS), ",".join(FIX_MODELS),
             ("已配置" if ASR_KEY else "未配置")), flush=True)



# ---------- 运营侧告警：阿里云账户余额 / 语音欠费 ----------
_BAL_MIN = float(os.environ.get("RELAY_BALANCE_MIN", "50") or 50)
_BAL_EVERY = max(1, int(os.environ.get("RELAY_BALANCE_CHECK_H", "6") or 6)) * 3600
_ALERT_LAST = {}          # key -> 上次发送时间（进程内节流，重启最多多发一次）


def _bss_balance_sync():
    """查阿里云账户可用余额（BSS OpenAPI QueryAccountBalance）。
    RPC 签名与短信同套，但 endpoint/Version 不同；显式绕开代理环境变量。"""
    import base64
    import hashlib
    import hmac
    import urllib.error
    import urllib.parse
    import urllib.request
    import uuid
    if not (ALI_AK_ID and ALI_AK_SECRET):
        raise RuntimeError("未配置 RELAY_ALI_AK_ID / RELAY_ALI_AK_SECRET")
    p = {"AccessKeyId": ALI_AK_ID, "Action": "QueryAccountBalance", "Format": "JSON",
         "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
         "SignatureVersion": "1.0", "Version": "2017-12-14",
         "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def enc(v):
        return urllib.parse.quote(str(v), safe="~")

    query = "&".join("%s=%s" % (enc(k), enc(p[k])) for k in sorted(p))
    sts = "POST&%2F&" + enc(query)
    sig = base64.b64encode(hmac.new((ALI_AK_SECRET + "&").encode(),
                                    sts.encode(), hashlib.sha1).digest()).decode()
    body = (query + "&Signature=" + enc(sig)).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "https://business.aliyuncs.com/", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx 的 body 里才有真正的原因（RAM 子账号没授权会返回 NotAuthorized）。
        # 以前只打「HTTP Error 400」，等于把有用的诊断信息丢了。
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
            dj = json.loads(raw)
            raise RuntimeError("aliyun %s: %s" % (dj.get("Code") or e.code,
                                                  dj.get("Message") or raw[:160]))
        except ValueError:
            raise RuntimeError("aliyun HTTP %s: %s" % (e.code, raw[:160]))
    if not d.get("Success") or str(d.get("Code")) not in ("200", "0"):
        raise RuntimeError("%s: %s" % (d.get("Code") or "ERR", d.get("Message") or "查询失败"))
    data = d.get("Data") or {}
    amt = data.get("AvailableAmount") or data.get("AvailableCashAmount") or "0"
    return float(amt), (data.get("Currency") or "CNY")


async def _alert_once(app, key, subject, text, gap=6 * 3600, only=None):
    """同类告警节流：默认 6 小时内只发一次，别把邮箱/webhook 刷爆。
    only：只发这些渠道（None=所有已配置渠道）。计划任务按自己的配置传。"""
    now = time.time()
    if now - _ALERT_LAST.get(key, 0) < gap:
        return
    _ALERT_LAST[key] = now
    await _alert_send(app, subject, text, only=only)


def _is_billing_error(msg):
    m = str(msg or "").lower()
    return any(w in m for w in ("arrearage", "欠费", "good standing",
                                "insufficient balance", "余额不足"))


async def _balance_watch(app):
    """启动后每 RELAY_BALANCE_CHECK_H 小时查一次阿里云余额，低于阈值就提醒。"""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(90)          # 等中继自己起来、DNS 稳定
    while True:
        try:
            amt, cur = await loop.run_in_executor(None, _bss_balance_sync)
            _BAL_LAST.update({"amt": amt, "cur": cur, "ts": time.time(), "err": ""})
            print("[relay] balance %.2f %s (min %.2f)" % (amt, cur, _BAL_MIN), flush=True)
            if amt < _BAL_MIN:
                await _alert_once(
                    app, "balance",
                    "⚠️ 阿里云余额不足：%.2f 元" % amt,
                    "当前可用余额 %s %.2f，低于告警线 %.2f 元。\n"
                    "余额耗尽后语音转写和模型调用都会失败，请尽快充值。"
                    % (cur, amt, _BAL_MIN))
        except Exception as e:
            _BAL_LAST.update({"ts": time.time(), "err": str(e)[:300]})
            print("[relay] balance check failed: %s" % str(e)[:200], flush=True)
        await asyncio.sleep(_BAL_EVERY)


# ---------- relay.db 自动备份 ----------
# 单文件 SQLite 是唯一的数据源（账号/设备/订单/用量），VPS 挂一次就全丢。
# 用 sqlite 官方 backup API 做一致性快照：WAL 里没落盘的也带上，不锁业务写入。
_BK_EVERY = max(1, int(os.environ.get("RELAY_BACKUP_H", "6") or 6)) * 3600
_BK_KEEP = max(2, int(os.environ.get("RELAY_BACKUP_KEEP", "14") or 14))


def _db_backup_sync(dbp):
    """备份 + quick_check 校验。坏快照宁可删掉，也别留给人误用。"""
    dstp = "%s.bak-%s" % (dbp, time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(dbp, timeout=20)
    dst = sqlite3.connect(dstp)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA quick_check").fetchone()
        if not ok or ok[0] != "ok":
            raise RuntimeError("备份 quick_check 失败：%r" % (ok,))
    except Exception:
        dst.close(); src.close()
        try:
            os.unlink(dstp)
        except OSError:
            pass
        raise
    dst.close(); src.close()
    return dstp, os.path.getsize(dstp)


def _db_backup_list(dbp):
    """已有的快照，新→旧。名字是 relay.db.bak-YYYYmmdd-HHMMSS；
    同时躺着的 -wal/-shm 不是快照（SQLite 给同名文件加的伴生文件），必须排除。"""
    d = os.path.dirname(os.path.abspath(dbp))
    base = os.path.basename(dbp) + ".bak-"
    out = []
    try:
        for n in os.listdir(d):
            if not n.startswith(base):
                continue
            suf = n[len(base):]
            if (len(suf) != 15 or suf[8] != "-"
                    or not (suf[:8] + suf[9:]).isdigit()):
                continue
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append(p)
    except OSError:
        return []
    out.sort(reverse=True)
    return out


def _db_backup_prune(dbp):
    dropped = []
    for p in _db_backup_list(dbp)[_BK_KEEP:]:
        try:
            os.unlink(p)
            dropped.append(os.path.basename(p))
        except OSError:
            pass
    return dropped


async def _backup_loop(app):
    """兼容入口：旧版是独立备份循环，现在由计划任务表统一调度。
    保留这个函数名是为了不破坏已有的 app["backup_task"] 引用。"""
    await _task_loop(app)


# ---------- 主机资源监控 ----------
# 只告警「会把人挡在门外」的三件事：磁盘满（备份/日志/DB 全写不进）、内存见底
# （进程被 OOM）、出口带宽持续打满（所有客户端一起变慢）。阈值都能用环境变量调。
_MON_EVERY = max(60, int(float(os.environ.get("RELAY_MON_MIN", "10") or 10) * 60))
_MON_DISK_PCT = float(os.environ.get("RELAY_DISK_MAX_PCT", "90") or 90)
_MON_DISK_FREE_MB = int(os.environ.get("RELAY_DISK_MIN_FREE_MB", "2048") or 2048)
_MON_MEM_MB = int(os.environ.get("RELAY_MEM_MIN_MB", "200") or 200)
_MON_NET_MBPS = float(os.environ.get("RELAY_NET_MAX_MBPS", "0") or 0)   # 线路带宽；0 = 不查打满
_MON_DAY_GB = float(os.environ.get("RELAY_DAY_GB_MAX", "0") or 0)       # 日出口流量线；0 = 不查


def _sys_sample(dbp):
    d = os.path.dirname(os.path.abspath(dbp))
    du = shutil.disk_usage(d)
    mem_avail = 0
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    mem_avail = int(ln.split()[1]) // 1024
                    break
    except OSError:
        pass
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    db_mb = 0.0
    for suf in ("", "-wal", "-shm"):    # WAL 没 checkpoint 时主体只有 4KB，要连 WAL 一起算
        try:
            db_mb += os.path.getsize(dbp + suf) / 1048576.0
        except OSError:
            pass
    return {"disk_pct": (du.used * 100.0 / du.total) if du.total else 0.0,
            "disk_free_mb": du.free // (1024 * 1024),
            "mem_avail_mb": mem_avail, "load": load, "db_mb": db_mb}


def _net_bytes():
    """整机网卡累计字节（排除 lo）。读不到返回 None（不影响其它监控）。"""
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for ln in f:
                if ":" not in ln:
                    continue
                name, rest = ln.split(":", 1)
                if name.strip() == "lo":
                    continue
                cols = rest.split()
                rx += int(cols[0])
                tx += int(cols[8])
    except (OSError, ValueError, IndexError):
        return None
    return rx, tx


async def _sys_watch(app):
    """定期采样磁盘/内存/带宽；异常走同一套提醒渠道（同一类 6 小时只发一次）。"""
    loop = asyncio.get_event_loop()
    dbp = app["dbp"]
    await asyncio.sleep(180)
    prev = None
    prev_t = time.time()
    net_over = 0
    while True:
        try:
            st = await loop.run_in_executor(None, _sys_sample, dbp)
            now = time.time()
            nb = await loop.run_in_executor(None, _net_bytes)
            up_kbs = dn_kbs = 0.0
            if nb and prev and now > prev_t:
                dt = now - prev_t
                up_kbs = (nb[1] - prev[1]) / dt / 1024.0
                dn_kbs = (nb[0] - prev[0]) / dt / 1024.0
            prev, prev_t = nb, now
            bk = _db_backup_list(dbp)
            bk_age_h = ((now - os.path.getmtime(bk[0])) / 3600.0) if bk else -1
            print("[relay] sys disk=%.0f%%(free %dMB) mem_avail=%dMB load=%.2f db=%.1fMB "
                  "net up=%.0fKB/s down=%.0fKB/s backup=%s"
                  % (st["disk_pct"], st["disk_free_mb"], st["mem_avail_mb"], st["load"],
                     st["db_mb"], up_kbs, dn_kbs,
                     ("%.1fh前" % bk_age_h) if bk else "无"), flush=True)

            if st["disk_pct"] >= _MON_DISK_PCT or st["disk_free_mb"] < _MON_DISK_FREE_MB:
                await _alert_once(app, "disk", "⚠️ CodeBoss 中继磁盘空间不足",
                                  "数据目录剩余 %d MB（已用 %.0f%%），低于告警线 %d MB / %.0f%%。\n"
                                  "磁盘写满后注册、备份、更新都会失败。"
                                  % (st["disk_free_mb"], st["disk_pct"],
                                     _MON_DISK_FREE_MB, _MON_DISK_PCT))
            if 0 < st["mem_avail_mb"] < _MON_MEM_MB:
                await _alert_once(app, "mem", "⚠️ CodeBoss 中继内存紧张",
                                  "可用内存只剩 %d MB（告警线 %d MB），进程有被 OOM 杀掉的 "
                                  "风险。" % (st["mem_avail_mb"], _MON_MEM_MB))
            if _MON_NET_MBPS > 0 and up_kbs > _MON_NET_MBPS * 1024 * 0.8 / 8:
                net_over += 1          # 连续 3 次（≈30 分钟）才算持续打满，别被一次秒传吓到
                if net_over >= 3:
                    await _alert_once(
                        app, "net", "⚠️ CodeBoss 中继出口带宽持续打满",
                        "出口约 %.0f KB/s（%.1f Mbps 线路的 80%% 以上），已持续 %d 个采样周期。"
                        % (up_kbs, _MON_NET_MBPS, net_over))
                    net_over = 0
            else:
                net_over = 0
            if _MON_DAY_GB > 0:
                # 按流量计费时带宽打满不是故障，账单才是：直接看整机今天出了多少 GB。
                _day = time.strftime("%Y-%m-%d", time.localtime(now))
                _gb = (_NET_DAILY.get(_day) or [0, 0])[0] / 1073741824.0
                if _gb >= _MON_DAY_GB:
                    await _alert_once(
                        app, "daygb", "⚠️ CodeBoss 中继今日流量偏高",
                        "整机今天已出 %.1f GB（告警线 %.1f GB）。平时一天 1~2 GB，"
                        "涨这么多通常是发版镜像反复推、大文件直发中继，或有人在刷。"
                        % (_gb, _MON_DAY_GB))
            if bk_age_h > _BK_EVERY / 3600.0 * 2.5:
                await _alert_once(app, "bkold", "⚠️ CodeBoss 中继备份过期",
                                  "最近一份 relay.db 备份在 %.1f 小时前，超过预期的 %.0f 小时。"
                                  % (bk_age_h, _BK_EVERY / 3600.0))
        except Exception as e:
            print("[relay] sys watch failed: %s" % str(e)[:200], flush=True)
        await asyncio.sleep(_MON_EVERY)


# ---------- 带宽历史（后台「出口带宽」曲线） ----------
# 和 _sys_watch 分开：那个 10 分钟一拍、只看要不要告警；这个 1 分钟一拍，
# 合成 5 分钟一个桶，留 7 天，落盘 net_hist.json / net_daily.json，
# 重启（发版很频繁）后曲线和当日流量都还在。
_NET_EVERY = max(15, int(float(os.environ.get("RELAY_NET_SAMPLE_SEC", "60") or 60)))
_NET_BUCKET = 300
_NET_HIST = collections.deque(maxlen=7 * 24 * 12 + 24)
_NET_CUR = {"ts": 0.0, "up": 0.0, "dn": 0.0}   # 最近一次采样的瞬时速率（KB/s）
_NET_DAILY = {}                                # "YYYY-MM-DD" -> [出字节, 入字节]
_NET_LOADED = False


def _net_files(dbp):
    d = os.path.dirname(os.path.abspath(dbp))
    return os.path.join(d, "net_hist.json"), os.path.join(d, "net_daily.json")


def _net_load(dbp):
    global _NET_LOADED
    if _NET_LOADED:
        return
    _NET_LOADED = True
    hp, dp = _net_files(dbp)
    try:
        with open(hp) as f:
            for row in json.load(f):
                _NET_HIST.append([int(row[0]), float(row[1]), float(row[2]), float(row[3])])
    except (OSError, ValueError, TypeError, IndexError):
        pass
    try:
        with open(dp) as f:
            for k, v in (json.load(f) or {}).items():
                _NET_DAILY[str(k)] = [int(v[0]), int(v[1])]
    except (OSError, ValueError, TypeError, IndexError):
        pass


def _net_save(dbp, hist=None, daily=None):
    """hist/daily 由采样协程先拷贝再丢进来：写盘在别的线程，不能直接遍历 deque。"""
    hp, dp = _net_files(dbp)
    for path, obj in ((hp, hist if hist is not None else []),
                      (dp, daily if daily is not None else {})):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(obj, f)
            os.replace(tmp, path)
        except OSError as e:
            print("[relay] net save failed: %s" % str(e)[:120], flush=True)


async def _net_watch(app):
    loop = asyncio.get_event_loop()
    dbp = app["dbp"]
    await loop.run_in_executor(None, _net_load, dbp)   # 历史先读回来，重启发版后曲线不清零
    prev = await loop.run_in_executor(None, _net_bytes)
    prev_t = time.time()
    n = 0
    while True:
        try:
            await asyncio.sleep(_NET_EVERY)
            nb = await loop.run_in_executor(None, _net_bytes)
            now = time.time()
            if not nb or not prev or now <= prev_t:
                prev, prev_t = nb, now
                continue
            dt = now - prev_t
            tx = max(0, nb[1] - prev[1])
            rx = max(0, nb[0] - prev[0])
            up_kbs, dn_kbs = tx / dt / 1024.0, rx / dt / 1024.0
            _NET_CUR.update({"ts": now, "up": up_kbs, "dn": dn_kbs})
            # 桶里存「KB 总量 + 秒数」，读的时候再除，避免均值二次平均。
            b = int(now // _NET_BUCKET) * _NET_BUCKET
            if _NET_HIST and _NET_HIST[-1][0] == b:
                e = _NET_HIST[-1]
                e[1] += up_kbs * dt
                e[2] += dn_kbs * dt
                e[3] += dt
            else:
                _NET_HIST.append([b, up_kbs * dt, dn_kbs * dt, dt])
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            dv = _NET_DAILY.setdefault(day, [0, 0])
            dv[0] += tx
            dv[1] += rx
            if len(_NET_DAILY) > 30:
                for k in sorted(_NET_DAILY)[:-30]:
                    _NET_DAILY.pop(k, None)
            n += 1
            if n % 5 == 0:
                snap = [[b[0], round(b[1], 1), round(b[2], 1), round(b[3], 1)] for b in _NET_HIST]
                await loop.run_in_executor(None, _net_save, dbp, snap, dict(_NET_DAILY))
            prev, prev_t = nb, now
        except Exception as e:
            print("[relay] net watch failed: %s" % str(e)[:200], flush=True)



# ---------- 外部服务健康（管理后台 / 监控屏） ----------
# 两类数据：
#   · 真实调用：短信、邮件、提醒渠道、语音转写/合成、文本模型，每次成功/失败都记一笔；
#   · 主动探活：模型 / 语音这类「没人用就看不出坏了」的服务，低频发一个最小请求。
# 只留 24 小时事件（26 小时滚动窗口），状态每分钟落一次 svc_state.json，重启不丢。
# 监控屏时间窗口：key -> (跨度秒, 桶宽秒, 桶数, 显示名)
_SVC_WINDOWS = {
    "hour":  (24 * 3600, 3600, 24, "24 小时"),
    "day":   (7 * 86400, 86400, 7, "7 天"),
    "week":  (8 * 7 * 86400, 7 * 86400, 8, "8 周"),
    "month": (6 * 30 * 86400, 30 * 86400, 6, "6 个月"),
}
# 输入量单位与估算单价（元/单位）。单价可用环境变量覆盖；0 = 不显示费用。
_SVC_UNIT = {"sms": "条", "email": "封", "asr": "秒", "tts": "字",
             "llm": "token", "remind": "token"}
_SVC_PRICE = {
    "sms": float(os.environ.get("RELAY_PRICE_SMS") or 0.045),
    "asr": float(os.environ.get("RELAY_PRICE_ASR_SEC") or 0.00022),
    "tts": float(os.environ.get("RELAY_PRICE_TTS_CHAR") or 0.0001),
    "llm": float(os.environ.get("RELAY_PRICE_LLM_TOKEN") or 0.0000002),
    "remind": float(os.environ.get("RELAY_PRICE_LLM_TOKEN") or 0.0000002),
}
_SVC_CTX = contextvars.ContextVar("svc_ctx", default=(0, "", ""))


@web.middleware
async def _svc_dim_mw(request, handler):
    """给本次请求记下 (uid, dev, cid)：服务调用（短信/ASR/TTS/LLM）按这些维度归类。"""
    uid, dev, cid = 0, "", ""
    try:
        u = uid_of(request) or 0
        if u:
            uid = int(u)
        dev = (request.headers.get("X-CB-Dev") or request.cookies.get("cb_dev") or "")[:64]
        cid = (request.cookies.get("cb_cid") or "")[:64]
        if not uid and dev:
            row = _db(request.app).execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
            uid = int(row[0]) if row and row[0] else 0
    except Exception:
        pass
    dbx = _db(request.app)
    tok = _SVC_CTX.set((uid, dev, cid, dbx))
    t0 = time.time()
    try:
        resp = await handler(request)
    finally:
        _SVC_CTX.reset(tok)
    try:
        # 终端登录态（2026-11-21）：
        #   · 被移除/被闲置清理踢掉的（client_kick 有记录）带旧 cookie 回来 →
        #     页面导航直接回 /r/ 登录页，接口只清 cookie（页面下次导航自己回登录）；
        #   · 重新登录成功（响应里有新 r_sess）→ 解除限制；
        #   · 浏览器登录态只保留到本次会话（去掉 Max-Age），关掉浏览器下次要重新登录；
        #     App/壳（Dalvik、"; wv)"）照旧 7 天。
        browser = _is_browser_ua(request.headers.get("User-Agent") or "")
        cid_ = (request.cookies.get("cb_cid") or "").strip()
        m = resp.cookies.get("r_sess")
        new_tok = (m.value if m else "") or ""
        if new_tok and cid_:
            resp_db = _db(request.app)
            resp_db.execute("DELETE FROM client_kick WHERE cid=?", (cid_,))
            resp_db.commit()
        tok = new_tok or (request.cookies.get("r_sess") or "")
        if tok:
            kicked = False
            if cid_ and not new_tok:
                kicked = _db(request.app).execute(
                    "SELECT 1 FROM client_kick WHERE cid=?", (cid_,)).fetchone() is not None
            if kicked:
                acc = (request.headers.get("Accept") or "").lower()
                if (request.method in ("GET", "HEAD") and "text/html" in acc
                        and not request.path.startswith("/r/")):
                    out = web.HTTPFound("/r/")
                    out.del_cookie("r_sess")
                    return out
                resp.del_cookie("r_sess")
            elif browser:
                resp.set_cookie("r_sess", tok, max_age=None, httponly=True,
                                samesite="Lax",
                                secure=(request.scheme == "https"))
    except Exception:
        pass
    try:
        if getattr(resp, "status", 200) >= 400:
            dbx.execute(
                "INSERT INTO req_fail(ts,path,method,status,uid,dev,cid,ms)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), request.path[:160], request.method[:8], int(resp.status),
                 int(uid or 0), dev, cid, int((time.time() - t0) * 1000)))
            dbx.execute("DELETE FROM req_fail WHERE id NOT IN"
                        " (SELECT id FROM req_fail ORDER BY id DESC LIMIT 5000)")
            dbx.commit()
    except Exception:
        pass
    return resp


# 每个请求的结局 + 崩掉的堆栈（内存里各留一笔，/r/admin_api/failures 直接看）。
# 2026-11-21：手机一直 500，后台查不到任何记录——aiohttp 的兜底 500 不走 req_fail。
_RECENT = collections.deque(maxlen=400)
_ERRS = collections.deque(maxlen=40)


@web.middleware
async def _err_tap_mw(request, handler):
    """最外层记录：请求结局（路径/UA/IP/状态/耗时）+ 异常堆栈。只记不改行为。"""
    t0 = time.time()
    status = 0
    try:
        resp = await handler(request)
        status = getattr(resp, "status", 0) or 0
        return resp
    except web.HTTPException as e:      # 302/403 这类是正常结局，不是故障
        status = e.status
        raise
    except BaseException:
        _ERRS.appendleft({"ts": time.time(), "path": request.path[:120],
                          "method": request.method,
                          "ip": (request.remote or "")[:64],
                          "ua": (request.headers.get("User-Agent") or "")[:120],
                          "tb": traceback.format_exc()[-2000:]})
        raise
    finally:
        _RECENT.appendleft({"ts": time.time(), "path": request.path[:120],
                            "method": request.method, "status": status,
                            "ms": int((time.time() - t0) * 1000),
                            "ip": (request.remote or "")[:64],
                            "ua": (request.headers.get("User-Agent") or "")[:120]})


_SVC_KEEP = 48 * 3600      # 保留 48 小时事件：滚动聚合要用（今天+昨天完整）
_SVC_MAX_EV = 20000
_SVC = {}
_SVC_LOCK = threading.Lock()    # 短信/邮件在 executor 线程里记数，落盘在另一个线程
_SVC_DIRTY = [False]
_SVC_META = [
    ("sms", "短信验证码", "passive", "阿里云号码认证：注册 / 登录 / 找回密码 / 管理二次验证"),
    ("email", "邮件发送", "passive", "SMTP：邮箱验证码、到账提醒、告警"),
    ("asr", "语音转写", "active", "DashScope qwen3-asr：按住说话 / 语音指令"),
    ("tts", "语音合成", "active", "DashScope qwen3-tts：智能提醒语音播报"),
    ("llm", "文本模型", "active", "DashScope 兼容接口：回复概括 / 命令识别 / 纠错"),
    ("remind", "智能提醒 · 大模型", "passive", "purpose=remind 的模型调用（Pro / Max）"),
    ("ch_dingtalk", "提醒推送 · 钉钉", "passive", "群机器人 Webhook"),
    ("ch_wecom", "提醒推送 · 企业微信", "passive", "群机器人 Webhook"),
    ("ch_serverchan", "提醒推送 · Server酱", "passive", "sctapi.ftqq.com"),
    ("ch_pushplus", "提醒推送 · PushPlus", "passive", "pushplus.plus"),
]
_SVC_CHANNEL = {"ch_dingtalk": "dingtalk", "ch_wecom": "wecom",
                "ch_serverchan": "serverchan", "ch_pushplus": "pushplus"}
# 主动探活间隔（秒）。模型 15 分钟；语音各 30 分钟——一次 1 秒静音/两个字，成本可忽略。
_SVC_PROBE = {"llm": 15 * 60, "asr": 30 * 60, "tts": 30 * 60}
_BAL_LAST = {"amt": None, "cur": "", "ts": 0.0, "err": ""}


def _svc(key):
    s = _SVC.get(key)
    if s is None:
        s = _SVC[key] = {"ok": None, "last_ok": 0.0, "last_fail": 0.0,
                         "last_ms": 0, "avg_ms": 0.0, "n": 0, "err": "",
                         "probe_ok": None, "probe_ts": 0.0, "probe_ms": 0,
                         "probe_err": "", "ev": collections.deque()}
    return s


def _svc_note(key, ok, ms=0, err="", probe=False, n=0):
    """记一笔服务调用。probe=True 是主动探活，同样算成功/失败事件。

    n = 这次调用的「输入量」：ASR 音频秒数 / TTS 合成字符数 / 文本模型 token 数；
    短信/邮件/推送这类按条计的传 0（调用次数已经单独统计）。"""
    now = time.time()
    ok = bool(ok)
    ms = int(ms or 0)
    with _SVC_LOCK:
        s = _svc(key)
        if probe:
            s["probe_ok"] = ok
            s["probe_ts"] = now
            s["probe_ms"] = ms
            s["probe_err"] = "" if ok else str(err or "")[:300]
        if ok:
            s["last_ok"] = now
            s["last_ms"] = ms
            s["n"] += 1
            if ms:
                s["avg_ms"] = float(ms) if not s["avg_ms"] else s["avg_ms"] * 0.8 + ms * 0.2
        else:
            s["last_fail"] = now
            s["err"] = str(err or "失败")[:300]
            s["n"] += 1
        ev = s["ev"]
        try:
            n = max(0.0, float(n or 0))
        except (TypeError, ValueError):
            n = 0.0
        _u, _d, _c, _dbx = (0, "", "", None)
        try:
            _ctx = _SVC_CTX.get()
            _u, _d, _c = _ctx[0], _ctx[1], _ctx[2]
            _dbx = _ctx[3] if len(_ctx) > 3 else None
        except Exception:
            pass
        ev.append((now, 1 if ok else 0, n, int(_u or 0), _d or "", _c or ""))
        if not ok and _dbx is not None:
            try:
                _dbx.execute(
                    "INSERT INTO svc_fail(ts,key,err,ms,uid,dev,cid) VALUES(?,?,?,?,?,?,?)",
                    (now, key, str(err or s["err"] or "失败")[:300], ms,
                     int(_u or 0), _d or "", _c or ""))
                _dbx.execute("DELETE FROM svc_fail WHERE id NOT IN"
                             " (SELECT id FROM svc_fail ORDER BY id DESC LIMIT 5000)")
                _dbx.commit()
            except Exception:
                pass
        while len(ev) > _SVC_MAX_EV:
            ev.popleft()
        _SVC_DIRTY[0] = True


def _svc_path(app):
    return os.path.join(os.path.dirname(os.path.abspath(app["dbp"])), "svc_state.json")


def _svc_load(app):
    try:
        with open(_svc_path(app), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(d, dict):
        return
    now = time.time()
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        s = _svc(k)
        for f in ("ok", "last_ok", "last_fail", "last_ms", "avg_ms", "n", "err",
                  "probe_ok", "probe_ts", "probe_ms", "probe_err"):
            if f in v:
                s[f] = v[f]
        ev = []
        for e in v.get("ev") or []:
            try:
                t, ok = float(e[0]), int(e[1])
                n = float(e[2]) if len(e) > 2 else 0.0
                u = int(e[3]) if len(e) > 3 else 0
                d = str(e[4]) if len(e) > 4 else ""
                c = str(e[5]) if len(e) > 5 else ""
            except (TypeError, ValueError, IndexError):
                continue
            if now - t < _SVC_KEEP:
                ev.append((t, ok, n, u, d, c))
        s["ev"] = collections.deque(ev[-_SVC_MAX_EV:])


def _svc_save(app):
    if not _SVC_DIRTY[0]:
        return False
    now = time.time()
    out = {}
    with _SVC_LOCK:
        for k, s in _SVC.items():
            # 事件可能是 6 元组（新）或 3 元组（老状态文件）；逐条归一化再裁过期。
            ev = []
            for e in s["ev"]:
                t, o = float(e[0]), int(e[1])
                n = float(e[2]) if len(e) > 2 else 0.0
                u = int(e[3]) if len(e) > 3 else 0
                dv = e[4] if len(e) > 4 else ""
                c = e[5] if len(e) > 5 else ""
                if now - t < _SVC_KEEP:
                    ev.append((round(t, 1), o, round(n, 1), u or 0, dv or "", c or ""))
            s["ev"] = collections.deque(ev)     # 顺手裁掉过期事件
            out[k] = {f: s[f] for f in ("ok", "last_ok", "last_fail", "last_ms", "avg_ms",
                                        "n", "err", "probe_ok", "probe_ts", "probe_ms",
                                        "probe_err")}
            out[k]["ev"] = ev
    p = _svc_path(app)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        # 不许静默：调用方（_svc_watch）会把异常吞掉，不打印就永远查不到
        print("[svc] 状态落盘失败：%s" % str(e)[:200], flush=True)
        return False
    _SVC_DIRTY[0] = False
    return True


def _svc_configured(key, cfg=None):
    if key == "sms":
        return SMS_ON
    if key == "email":
        return bool(SMTP_HOST) and SMTP_HOST != "console"
    if key in ("asr", "tts", "llm", "remind"):
        return bool(ASR_KEY)
    if key in _SVC_CHANNEL:
        return bool((cfg or {}).get(_SVC_CHANNEL[key]))
    return True


def _svc_state_of(s, configured, now):
    """ok / warn / fail / unknown / off。探活失败的优先级最高（刚探过就不猜了）。"""
    if not configured:
        return "off"
    if s["probe_ok"] is False and now - float(s["probe_ts"] or 0) < 3 * 3600:
        return "fail"
    last_ok = float(s["last_ok"] or 0)
    last_fail = float(s["last_fail"] or 0)
    if last_fail > last_ok:
        # 最近一次真实调用失败：6 小时内算红，更早的算「之前出过错」的黄
        return "fail" if now - last_fail < 6 * 3600 else "warn"
    if last_ok and now - last_ok < 7 * 86400:
        return "ok"
    if s["probe_ok"] is True and now - float(s["probe_ts"] or 0) < 3 * 3600:
        return "ok"
    return "unknown"


def _svc_rollup(dbp):
    """把「今天 / 昨天」的服务事件按天重算进 svc_daily（幂等，每 5 分钟跑一次）。

    dbp 是库文件路径，不是连接：这个函数在 run_in_executor 的工作线程里跑，sqlite 连接
    不能跨线程（拿主线程的连接进来会 100% 报 ProgrammingError，一天 280+ 条，表一直是空的）。

    事件只保留 48h，所以只敢重算最近两天；更早的天已经定稿不再动。
    重算而不是增量累加：重启、重复跑、事件截断都不会把数算重。"""
    now = time.time()
    day0 = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    with _SVC_LOCK:
        snap = {k: list(s["ev"]) for k, s in _SVC.items()}
    counts = {}
    hcounts = {}
    for key, ev in snap.items():
        for _e in ev:
            t, ok, n = _e[0], _e[1], _e[2]
            lt = time.localtime(t)
            day = time.strftime("%Y-%m-%d", lt)
            if day < day0:
                continue
            c = counts.setdefault((day, key), [0, 0, 0.0])
            c[0 if ok else 1] += 1
            c[2] += float(n or 0)
            h = hcounts.setdefault((day, lt.tm_hour, key), [0, 0, 0.0])
            h[0 if ok else 1] += 1
            h[2] += float(n or 0)
    try:
        db = sqlite3.connect(dbp, timeout=10)
    except Exception as e:
        print("[svc] 日聚合开库失败：%s" % str(e)[:120], flush=True)
        return
    try:
        db.execute("DELETE FROM svc_daily WHERE day>=?", (day0,))
        db.execute("DELETE FROM svc_hour WHERE day>=?", (day0,))
        for (day, key), (ok, fa, mt) in counts.items():
            db.execute("INSERT INTO svc_daily(day,key,ok,fail,metric) VALUES(?,?,?,?,?)"
                       " ON CONFLICT(day,key) DO UPDATE SET ok=excluded.ok,"
                       " fail=excluded.fail, metric=excluded.metric",
                       (day, key, ok, fa, round(mt, 1)))
        for (day, hour, key), (ok, fa, mt) in hcounts.items():
            db.execute("INSERT INTO svc_hour(day,hour,key,ok,fail,metric)"
                       " VALUES(?,?,?,?,?,?) ON CONFLICT(day,hour,key) DO UPDATE SET"
                       " ok=excluded.ok, fail=excluded.fail, metric=excluded.metric",
                       (day, hour, key, ok, fa, round(mt, 1)))
        _cut = time.strftime("%Y-%m-%d", time.localtime(now - 400 * 86400))
        db.execute("DELETE FROM svc_daily WHERE day<?", (_cut,))
        db.execute("DELETE FROM svc_hour WHERE day<?", (_cut,))
        db.commit()
    except Exception as e:
        print("[svc] 日聚合落库失败：%s" % str(e)[:120], flush=True)
    finally:
        db.close()


async def _svc_roll_loop(app):
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _svc_rollup, app["dbp"])
        except Exception as e:
            print("[svc] 日聚合循环失败：%s" % str(e)[:120], flush=True)
        await asyncio.sleep(300)


def _svc_day_series(db, key, window):
    """从 svc_daily 聚合出 7 天 / 8 周 / 6 个月的分桶（旧→新）。"""
    span, unit, n, _label = _SVC_WINDOWS[window]
    now = time.time()
    start_day = time.strftime("%Y-%m-%d", time.localtime(now - span - unit))
    rows = []
    try:
        rows = db.execute("SELECT day,ok,fail,metric FROM svc_daily WHERE key=? AND day>=?",
                          (key, start_day)).fetchall()
    except Exception:
        rows = []
    buckets = [[0, 0, 0.0] for _ in range(n)]
    for day, ok, fa, mt in rows:
        try:
            ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
        idx = int((now - ts) // unit)
        if 0 <= idx < n:
            b = buckets[n - 1 - idx]
            b[0] += int(ok or 0)
            b[1] += int(fa or 0)
            b[2] += float(mt or 0)
    return buckets


def _direct_view(db, span=86400, uid=0, dev="", cid=""):
    """直连命中率（按流量）+ 隧道流量，支持按账号/开发机/客户端筛选。

    分子 = 直连请求字节（P2P/LAN，host 按分钟上报）；分母 = 直连 + 中继转发字节。
    分钟口径仍返回（无 dev/cid 筛选时），作为参考。"""
    try:
        span = min(max(int(span), 60), 400 * 86400)
    except (TypeError, ValueError):
        span = 86400
    label = _span_text(span)
    now = time.time()
    since_m = int((now - span) // 60)
    where, args = ["m>=?"], [since_m]
    if uid:
        where.append("uid=?")
        args.append(int(uid))
    if dev:
        where.append("dev=?")
        args.append(str(dev)[:64])
    if cid:
        where.append("cid=?")
        args.append(str(cid)[:64])
    direct_b = relay_b = req_n = 0
    try:
        for k, sb, rn in db.execute(
                "SELECT k, SUM(bytes_in+bytes_out), SUM(req_n) FROM usage_bytes2"
                " WHERE " + " AND ".join(where) + " GROUP BY k", args):
            if k == "p2p":
                direct_b += int(sb or 0)
            elif k == "relay":
                relay_b += int(sb or 0)
            req_n += int(rn or 0)
    except Exception:
        pass
    # 分钟口径：dev 没有按分钟存，只有账号/客户端筛选时可用
    direct_min = all_min = None
    if not dev:
        dmin, allm = set(), set()
        mw, ma = ["m>=?"], [since_m]
        if uid:
            mw.append("uid=?")
            ma.append(int(uid))
        if cid:
            mw.append("k=?")
            ma.append("c:" + str(cid)[:64])
        try:
            for m, k in db.execute("SELECT m,k FROM usage_client WHERE "
                                   + " AND ".join(mw), ma):
                k = str(k)
                allm.add(m)
                if k in ("direct", "p2p"):
                    dmin.add(m)
        except Exception:
            pass
        all_min = len(allm)
        direct_min = len(dmin)
    # 按路径类别拆中继字节：可直连接口 vs 只能走中继（页面/静态/语音/终端输入）
    relay_elig = relay_only = 0
    try:
        q = "SELECT day,hour,cls,bytes FROM bytes_path"
        where2, args2 = [], []
        if uid:
            where2.append("uid=?")
            args2.append(int(uid))
        if cid:
            where2.append("cid=?")
            args2.append(str(cid)[:64])
        if where2:
            q += " WHERE " + " AND ".join(where2)
        for day, hour, cls, nb in db.execute(q, args2):
            try:
                ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + int(hour) * 3600
            except (TypeError, ValueError):
                continue
            if ts < now - span:
                continue
            if cls in _ELIG_CLS:
                relay_elig += int(nb or 0)
            else:
                relay_only += int(nb or 0)
    except Exception:
        pass
    unclassified = max(0, relay_b - relay_elig - relay_only)
    total_b = direct_b + relay_b
    elig_total = direct_b + relay_elig
    # 还没分类的历史中继字节会让「可直连率」虚高（100%）：这段数据积累期内不给结论
    rate_elig = (round(100.0 * direct_b / elig_total, 1)
                 if elig_total and not unclassified else None)
    return {"label": label, "direct_bytes": direct_b, "relay_bytes": relay_b,
            "relay_eligible_bytes": relay_elig, "relay_only_bytes": relay_only,
            "unclassified_bytes": unclassified,
            "total_bytes": total_b, "req_n": req_n,
            "rate": round(100.0 * direct_b / total_b, 1) if total_b else None,
            "rate_eligible": rate_elig, "eligible_known": not unclassified,
            "direct_min": direct_min, "all_min": all_min,
            "relay_min": (max(0, all_min - direct_min)
                          if (all_min is not None and direct_min is not None) else None),
            "rate_min": (round(100.0 * direct_min / all_min, 1)
                         if all_min else None) if all_min is not None else None,
            "filtered": bool(uid or dev or cid)}


def _paths_view(db, span, uid=0, cid=""):
    """按路径类别拆流量（回答「中继流量都是什么」）。"""
    now = time.time()
    start = now - span
    where, args = [], []
    if uid:
        where.append("uid=?")
        args.append(int(uid))
    if cid:
        where.append("cid=?")
        args.append(str(cid)[:64])
    q = "SELECT day,hour,cls,bytes,req FROM bytes_path"
    if where:
        q += " WHERE " + " AND ".join(where)
    agg = {}
    try:
        for day, hour, cls, nb, rq in db.execute(q, args):
            try:
                ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + int(hour) * 3600
            except (TypeError, ValueError):
                continue
            if ts < start:
                continue
            a = agg.setdefault(cls, [0, 0])
            a[0] += int(nb or 0)
            a[1] += int(rq or 0)
    except Exception:
        pass
    out = [{"cls": k, "bytes": v[0], "req": v[1]} for k, v in agg.items()]
    out.sort(key=lambda x: -x["bytes"])
    return out[:8]


def _traffic_view(db, span=86400, uid=0, dev="", cid=""):
    """按窗口汇总「产品使用 / 运维」两类流量（app 自己计到的部分）。"""
    try:
        span = min(max(int(span), 60), 400 * 86400)
    except (TypeError, ValueError):
        span = 86400
    label = _span_text(span)
    now = time.time()
    out = {"label": label, "filtered": bool(uid or dev or cid),
           "product": {"in": 0, "out": 0, "req": 0},
           "ops": {"in": 0, "out": 0, "req": 0}}
    if uid or dev or cid:
        # 有筛选：产品流量按 usage_bytes2 汇总（中继+P2P）；运维是全站基础设施，不拆分
        where, args = ["m>=?"], [int((now - span) // 60)]
        if uid:
            where.append("uid=?")
            args.append(int(uid))
        if dev:
            where.append("dev=?")
            args.append(str(dev)[:64])
        if cid:
            where.append("cid=?")
            args.append(str(cid)[:64])
        try:
            for k, sb, rn in db.execute(
                    "SELECT k, SUM(bytes_in+bytes_out), SUM(req_n) FROM usage_bytes2"
                    " WHERE " + " AND ".join(where) + " AND k IN ('relay','p2p')"
                    " GROUP BY k", args):
                out["product"]["out"] += int(sb or 0)
                out["product"]["req"] += int(rn or 0)
        except Exception:
            pass
        out["label"] = label + "（筛选）"
        out["paths"] = _paths_view(db, span, uid, cid)
        return out
    try:
        rows = db.execute("SELECT day,hour,kind,req_n,bytes_in,bytes_out FROM traffic_hour").fetchall()
    except Exception:
        rows = []
    cutoff = now - 24 * 3600
    day0 = time.strftime("%Y-%m-%d", time.localtime(now - span))
    prod_days = set()
    for day, hour, kind, r, i, o in rows:
        if span <= 48 * 3600:
            try:
                ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + int(hour) * 3600
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
        elif day < day0:
            continue
        if kind not in out:
            continue
        if kind == "product":
            prod_days.add(day)
        out[kind]["in"] += int(i or 0)
        out[kind]["out"] += int(o or 0)
        out[kind]["req"] += int(r or 0)
    # 早期只有按天存的 tunnel_stat（traffic_hour 是后加的）：非 24h 窗口里，
    # 那些还没有小时明细的日子按天把它补进「产品使用」，避免历史显示为 0。
    out["paths"] = _paths_view(db, span, uid, cid)
    if span > 48 * 3600:
        try:
            for day, r, i, o in db.execute(
                    "SELECT day,req_n,bytes_in,bytes_out FROM tunnel_stat WHERE day>=?",
                    (day0,)):
                if day in prod_days:
                    continue
                out["product"]["in"] += int(i or 0)
                out["product"]["out"] += int(o or 0)
                out["product"]["req"] += int(r or 0)
        except Exception:
            pass
    return out


_SVC_GRANS = {"sec": 1, "min": 60, "hour": 3600, "day": 86400}
_SVC_GRAN_NAME = {"sec": "秒", "min": "分钟", "hour": "小时", "day": "天"}


def _span_text(span):
    span = int(span or 86400)
    if span == 15552000:
        return "6 个月"
    if span >= 7 * 86400 and span % 86400 == 0:
        return "%d 天" % (span // 86400)
    if span % 3600 == 0:
        return "%d 小时" % (span // 3600)
    return "%d 分钟" % max(1, span // 60)


def _bucket_text(sec):
    sec = int(sec or 3600)
    if sec % 86400 == 0:
        return "%d 天" % (sec // 86400)
    if sec % 3600 == 0:
        return "%d 小时" % (sec // 3600)
    if sec % 60 == 0:
        return "%d 分钟" % (sec // 60)
    return "%d 秒" % sec


def _svc_bucket(gran, span):
    """粒度 × 跨度 → (实际桶宽秒, 桶数)。点太多会自动放大，最多 120 个点。"""
    base = _SVC_GRANS.get(gran, 3600)
    span = max(60, int(span or 86400))
    n = max(1, int((span + base - 1) // base))
    if n > 120:
        base = int(-(-span // 120 // base) * base)   # 向上取整到粒度的整数倍
        if base < _SVC_GRANS.get(gran, 3600):
            base = _SVC_GRANS[gran]
        n = max(1, int((span + base - 1) // base))
    return base, n


def _svc_series(db, key, start, base, n, uid=0, dev="", cid=""):
    """一个服务的分桶序列（旧→新）：[成功, 失败, 输入量]。"""
    buckets = [[0, 0, 0.0] for _ in range(n)]

    def _ev_filter(e):
        if uid and int(e[3] if len(e) > 3 else 0) != int(uid):
            return False
        if dev and str(e[4] if len(e) > 4 else "") != str(dev):
            return False
        if cid and str(e[5] if len(e) > 5 else "") != str(cid):
            return False
        return True
    if uid or dev or cid:           # 有筛选：只能用内存事件（保留 48h）
        with _SVC_LOCK:
            s = _svc(key)
            ev = [e for e in list(s["ev"]) if e[0] >= start and _ev_filter(e)]
        for _e in ev:
            t, ok, mt = _e[0], _e[1], _e[2]
            i = int((t - start) // base)
            if 0 <= i < n:
                buckets[i][0 if ok else 1] += 1
                buckets[i][2] += float(mt or 0)
        return buckets
    if base < 3600:                 # 秒/分钟：只用内存事件（保留 48h）
        with _SVC_LOCK:
            s = _svc(key)
            ev = [e for e in list(s["ev"]) if e[0] >= start]
        for _e in ev:
            t, ok, mt = _e[0], _e[1], _e[2]
            i = int((t - start) // base)
            if 0 <= i < n:
                buckets[i][0 if ok else 1] += 1
                buckets[i][2] += float(mt or 0)
        return buckets
    now = time.time()
    cutoff = now - _SVC_KEEP     # 48h 内直接用原始事件（最准、不受 5 分钟聚合滞后影响）
    if base < 86400:             # 小时：最近 48h 用事件，更早用 svc_hour
        with _SVC_LOCK:
            s = _svc(key)
            ev = [e for e in list(s["ev"]) if e[0] >= max(start, cutoff)]
        for _e in ev:
            t, ok, mt = _e[0], _e[1], _e[2]
            i = int((t - start) // base)
            if 0 <= i < n:
                buckets[i][0 if ok else 1] += 1
                buckets[i][2] += float(mt or 0)
        day0 = time.strftime("%Y-%m-%d", time.localtime(start))
        try:
            rows = db.execute("SELECT day,hour,ok,fail,metric FROM svc_hour"
                              " WHERE key=? AND day>=?", (key, day0)).fetchall()
        except Exception:
            rows = []
        for day, hour, ok, fa, mt in rows:
            try:
                ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + int(hour) * 3600
            except (TypeError, ValueError):
                continue
            if ts < start or ts >= cutoff:
                continue
            i = int((ts - start) // base)
            if 0 <= i < n:
                buckets[i][0] += int(ok or 0)
                buckets[i][1] += int(fa or 0)
                buckets[i][2] += float(mt or 0)
        return buckets
    with _SVC_LOCK:
        s = _svc(key)
        ev = [e for e in list(s["ev"]) if e[0] >= max(start, cutoff)]
    for _e in ev:
        t, ok, mt = _e[0], _e[1], _e[2]
        i = int((t - start) // base)
        if 0 <= i < n:
            buckets[i][0 if ok else 1] += 1
            buckets[i][2] += float(mt or 0)
    day0 = time.strftime("%Y-%m-%d", time.localtime(start))
    try:
        rows = db.execute("SELECT day,ok,fail,metric FROM svc_daily"
                          " WHERE key=? AND day>=?", (key, day0)).fetchall()
    except Exception:
        rows = []
    for day, ok, fa, mt in rows:
        try:
            ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
        if ts < start or ts >= cutoff:
            continue
        i = int((ts - start) // base)
        if 0 <= i < n:
            buckets[i][0] += int(ok or 0)
            buckets[i][1] += int(fa or 0)
            buckets[i][2] += float(mt or 0)
    return buckets


def _svc_view(app, gran="hour", span=86400, uid=0, dev="", cid=""):
    """给后台/监控屏的每个服务状态。

    两个独立维度：
      · gran = sec/min/hour/day（统计粒度）
      · span = 秒数（时间跨度，默认 24 小时）
    「最近成功 / 最近失败」永远是进程看到的那一次，不随窗口变。
    h24 固定为最近 24 小时（老后台页兼容）。"""
    if gran not in _SVC_GRANS:
        gran = "hour"
    try:
        span = int(span)
    except (TypeError, ValueError):
        span = 86400
    span = min(max(span, 60), 400 * 86400)
    base, nb = _svc_bucket(gran, span)
    start = time.time() - base * nb
    now = time.time()
    db = _db(app)
    cfg = _alert_cfg(db)
    out = []
    for key, name, kind, desc in _SVC_META:
        with _SVC_LOCK:
            s = _svc(key)
            ev = [e for e in list(s["ev"]) if now - e[0] < 24 * 3600]
        ok_n = sum(1 for e in ev if e[1])
        fail_n = len(ev) - ok_n
        met_n = sum(float(e[2] or 0) for e in ev)
        buckets = [[0, 0, 0.0] for _ in range(24)]
        t0 = now - 24 * 3600
        for _e in ev:
            t, ok, n = _e[0], _e[1], _e[2]
            i = int((t - t0) // 3600)
            if 0 <= i < 24:
                buckets[i][0 if ok else 1] += 1
                buckets[i][2] += float(n or 0)
        win_series = _svc_series(db, key, start, base, nb, uid, dev, cid)
        w_ok = sum(b[0] for b in win_series)
        w_fail = sum(b[1] for b in win_series)
        w_met = sum(b[2] for b in win_series)
        unit = _SVC_UNIT.get(key, "")
        price = _SVC_PRICE.get(key, 0.0)
        configured = _svc_configured(key, cfg)
        out.append({
            "key": key, "name": name, "kind": kind, "desc": desc,
            "configured": configured,
            "state": _svc_state_of(s, configured, now),
            "ok": s["ok"], "last_ok": s["last_ok"], "last_fail": s["last_fail"],
            "last_ms": s["last_ms"], "avg_ms": round(s["avg_ms"], 1),
            "err": s["err"], "n": s["n"],
            "probe_ok": s["probe_ok"], "probe_ts": s["probe_ts"],
            "probe_ms": s["probe_ms"], "probe_err": s["probe_err"],
            "unit": unit,
            "h24": {"ok": ok_n, "fail": fail_n,
                    "rate": round(100.0 * ok_n / (ok_n + fail_n), 1)
                    if ok_n + fail_n else None,
                    "calls": ok_n + fail_n, "metric": round(met_n, 1),
                    "cost": round(met_n * price, 4) if price else 0},
            "series": buckets,
            "win": {"ok": w_ok, "fail": w_fail,
                    "rate": round(100.0 * w_ok / (w_ok + w_fail), 1)
                    if w_ok + w_fail else None,
                    "series": win_series,
                    "label": "最近 %s · 每 %s%s" % (
                        _span_text(min(span, _SVC_KEEP) if (uid or dev or cid) else span),
                        _bucket_text(base),
                        "（筛选：最多 48 小时）" if (uid or dev or cid) and span > _SVC_KEEP else ""),
                    "calls": w_ok + w_fail, "metric": round(w_met, 1),
                    "unit": unit,
                    "cost": round(w_met * price, 4) if price else 0},
        })
    return out


async def _svc_watch(app):
    """每分钟：做到点的主动探活 + 落盘。"""
    _svc_load(app)
    loop = asyncio.get_event_loop()
    await asyncio.sleep(120)     # 启动后先让业务初始化跑完
    last = {}
    while True:
        now = time.time()
        for key, gap in _SVC_PROBE.items():
            if now - last.get(key, 0) < gap:
                continue
            last[key] = now
            if not ASR_KEY:
                continue
            t0 = time.time()
            try:
                if key == "llm":
                    ok, msg = await _model_test()
                    _svc_note("llm", ok, (time.time() - t0) * 1000, msg, probe=True)
                elif key == "tts":
                    await loop.run_in_executor(None, _tts_synth, "测试", TTS_VOICE)
                    _svc_note("tts", True, (time.time() - t0) * 1000, probe=True, n=2)
                elif key == "asr":
                    # 0.5 秒静音：只验证鉴权/模型/网络通不通，不产生实际转写内容
                    _text, err, _dur = await loop.run_in_executor(
                        None, _asr_run, _wav_24k(b"\x00\x00" * 6000), ".wav", "")
                    _svc_note("asr", not err, (time.time() - t0) * 1000,
                              err or "", probe=True, n=_dur)
            except Exception as e:
                _svc_note(key, False, (time.time() - t0) * 1000,
                          "%s: %s" % (type(e).__name__, str(e)[:200]), probe=True)
                print("[svc] probe %s failed: %s" % (key, str(e)[:160]), flush=True)
        try:
            await loop.run_in_executor(None, _svc_save, app)
        except Exception:
            pass
        await asyncio.sleep(60)


# ---------- 计划任务 ----------
# 备份、OSS 自检、资源快照都走同一张表：后台可以开关、改间隔、立即执行，
# 新增任务只白名单加一种 kind（不在页面里跑任意命令）。
TASK_KINDS = {
    "db_backup": "备份数据库（本地快照，配了 OSS 再上传）",
    "oss_probe": "OSS 连通性自检（写/读/删一个探针）",
    "sys_report": "资源快照写日志（磁盘/内存/带宽）",
    "order_sweep": "自动关闭超时未支付的订单（支付平台的单子早就失效了）",
}
# 失败通知可选的渠道（和「提醒渠道」配置页的键一一对应）
TASK_CHANNELS = ("email", "dingtalk", "wecom", "serverchan", "pushplus")
_ORDER_TTL_MIN = max(10, int(os.environ.get("RELAY_ORDER_TTL_MIN", "120") or 120))


# ---------- crontab 表达式（任务定时） ----------
# 标准 5 段：分 时 日 月 周。支持 *、*/n、a-b、a-b/n、逗号列表，月/周可用英文缩写。
# 时间取服务器本地时间（VPS 是 Asia/Shanghai，就是北京时间）。
_CRON_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_CRON_MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _cron_num(s, lo, hi, names=None):
    s = str(s or "").strip().lower()
    if names and s in names:
        return names[s]
    if not s.isdigit():
        raise ValueError("「%s」不是数字" % s)
    n = int(s)
    if n < lo or n > hi:
        raise ValueError("「%d」超出 %d-%d" % (n, lo, hi))
    return n


def _cron_field(txt, lo, hi, names=None):
    """解析一段 cron 字段 → set。非法抛 ValueError。"""
    out = set()
    for part in str(txt or "").split(","):
        part = part.strip().lower()
        step = 1
        if "/" in part:
            part, _, st = part.partition("/")
            if not st.isdigit() or int(st) < 1:
                raise ValueError("步长不对")
            step = int(st)
        if part in ("", "*"):
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            a, b = _cron_num(x, lo, hi, names), _cron_num(y, lo, hi, names)
        else:
            a = b = _cron_num(part, lo, hi, names)
        if a > b:
            raise ValueError("范围反了")
        out.update(range(a, b + 1, step))
    return out


def _cron_parse(expr):
    """→ (分, 时, 日, 月, 周, 日字段是*, 周字段是*)；非法抛 ValueError。"""
    f = str(expr or "").split()
    if len(f) != 5:
        raise ValueError("要 5 段：分 时 日 月 周，如 0 3 * * *")
    mi = _cron_field(f[0], 0, 59)
    ho = _cron_field(f[1], 0, 23)
    dom = _cron_field(f[2], 1, 31)
    mon = _cron_field(f[3], 1, 12, _CRON_MON)
    dow = _cron_field(f[4], 0, 7, _CRON_DOW)
    if 7 in dow:                    # crontab 里 0 和 7 都是周日
        dow.discard(7)
        dow.add(0)
    return mi, ho, dom, mon, dow, f[2].startswith("*"), f[4].startswith("*")


def _cron_valid(expr):
    try:
        _cron_parse(expr)
        return True
    except ValueError:
        return False


def _cron_match(expr, tm=None):
    """这一刻（默认现在）是否命中。非法表达式返回 False（保存时已校验）。"""
    try:
        mi, ho, dom, mon, dow, dom_any, dow_any = _cron_parse(expr)
    except ValueError:
        return False
    tm = tm or time.localtime()
    if tm.tm_min not in mi or tm.tm_hour not in ho or tm.tm_mon not in mon:
        return False
    d_ok = tm.tm_mday in dom
    w_ok = (tm.tm_wday + 1) % 7 in dow   # Python 周一=0…周日=6 → cron 周日=0
    # 经典 crontab 规则：日、周都限定（都不是 *）时，命中任一即可
    if dom_any and dow_any:
        return True
    if dom_any:
        return w_ok
    if dow_any:
        return d_ok
    return d_ok or w_ok


def _task_seed(db):
    """内置任务各播种一次；之后改间隔/开关都以表里的值为准。
    以前只看 db_backup 在不在，新加的内置任务永远播不进去——改成逐个补。
    播过的 id 记在 meta 里：管理员删掉的任务不会每次重启又冒出来。"""
    seeds = (
        ("db_backup", "备份数据库", "db_backup", _BK_EVERY, 1,
         {"keep": _BK_KEEP, "oss": True}),
        # 支付平台的单子普遍半小时就失效，本地不关就会一直挂在后台「待支付」里
        ("order_sweep", "关闭超时订单", "order_sweep", 1800, 1,
         {"ttl_min": _ORDER_TTL_MIN}),
    )
    try:
        for tid, name, kind, every, en, cfg in seeds:
            mk = "task_seed_" + tid
            if db.execute("SELECT 1 FROM meta WHERE k=?", (mk,)).fetchone():
                continue
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
                db.execute("INSERT INTO tasks(id,name,kind,every_s,enabled,cfg,created) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (tid, name, kind, every, en, json.dumps(cfg), time.time()))
            db.execute("INSERT INTO meta(k,v) VALUES(?,'1') "
                       "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (mk,))
        db.commit()
    except Exception as e:
        print("[task] 播种失败: %s" % str(e)[:120], flush=True)


def _cfg_dict(s):
    try:
        d = json.loads(s or "{}")
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def _task_row(r):
    cfg = _cfg_dict(r[6])
    return {"id": r[0], "name": r[1], "kind": r[2], "every_s": int(r[3] or 0),
            "enabled": bool(r[4]), "cron": (r[5] or "").strip(), "cfg": r[6] or "{}",
            "notify": [str(x) for x in (cfg.get("notify") or []) if isinstance(x, str)],
            "last_run": float(r[7] or 0), "last_ok": r[8], "last_msg": r[9] or ""}


def _task_log(db, tid, started, dur, ok, msg, trigger="auto"):
    """记一条执行历史，每个任务只留最近 200 条。"""
    try:
        db.execute("INSERT INTO task_runs(tid,started,dur,ok,msg,trigger) "
                   "VALUES(?,?,?,?,?,?)",
                   (tid, float(started), float(dur), 1 if ok else 0,
                    str(msg or "")[:400], str(trigger or "auto")[:10]))
        db.execute("DELETE FROM task_runs WHERE tid=? AND id NOT IN "
                   "(SELECT id FROM task_runs WHERE tid=? ORDER BY id DESC LIMIT 200)",
                   (tid, tid))
        db.commit()
    except Exception as e:
        print("[task] 写执行历史失败: %s" % str(e)[:120], flush=True)


def _task_list(db):
    return [_task_row(r) for r in db.execute(
        "SELECT id,name,kind,every_s,enabled,cron,cfg,last_run,last_ok,last_msg "
        "FROM tasks ORDER BY created, id").fetchall()]


def _order_sweep_sync(dbp, cfg):
    """把超时未支付的订单关掉（status 0 → -1）。
    真晚到的支付通知照样能入账：notify 只认订单在不在，到账时无条件置 status=1
    （见 r_pay_notify / _pay_apply），关闭本地单不会吞掉用户已付的钱。"""
    ttl = max(10, int(cfg.get("ttl_min") or _ORDER_TTL_MIN))
    cut = time.time() - ttl * 60
    db = sqlite3.connect(dbp, timeout=20)
    try:
        cur = db.execute("UPDATE orders SET status=-1 WHERE status=0 AND created<?",
                         (cut,))
        db.commit()
        n = cur.rowcount or 0
    finally:
        db.close()
    return True, ("关闭 %d 笔超时未支付订单（下单超过 %d 分钟）" % (n, ttl)
                  if n else "没有超时未支付的订单")


def _task_run_sync(dbp, kind, cfg, oc):
    """在 executor 里跑一个任务（不碰共享 db 连接）。返回 (ok, msg)。"""
    if kind == "db_backup":
        keep = max(1, int(cfg.get("keep") or _BK_KEEP))
        p, n = _db_backup_sync(dbp)          # 失败会抛
        dropped = []
        for q in _db_backup_list(dbp)[keep:]:
            try:
                os.unlink(q)
                dropped.append(os.path.basename(q))
            except OSError:
                pass
        msg = "本地 %s（%.0f KB）" % (os.path.basename(p), n / 1024.0)
        if dropped:
            msg += "；清理旧份 %d" % len(dropped)
        if cfg.get("oss", True) and _oss_ready(oc):
            try:
                with open(p, "rb") as f:
                    blob = f.read()
                key = _oss_key(oc, "backup/" + os.path.basename(p))
                ok, err = _oss_put(oc, key, blob, "application/octet-stream")
                if ok:
                    traffic_add("ops", 0, len(blob), 1)
                if ok:
                    msg += "；OSS 已上传 %.1f MB" % (len(blob) / 1048576.0)
                else:
                    msg += "；OSS 上传失败：%s" % err
                for nm in dropped:              # 本地删了，OSS 也跟着删
                    _oss_del(oc, _oss_key(oc, "backup/" + nm))
            except Exception as e:
                msg += "；OSS 异常：%s" % str(e)[:120]
        elif cfg.get("oss", True):
            msg += "；OSS 未配置（只留本地）"
        return True, msg
    if kind == "oss_probe":
        if not _oss_ready(oc):
            return False, "OSS 未配置或未启用"
        return _oss_test(oc)
    if kind == "sys_report":
        st = _sys_sample(dbp)
        nb = _net_bytes()
        msg = ("磁盘 %.0f%%（剩 %d MB）· 可用内存 %d MB · load %.2f · db %.1f MB"
               % (st["disk_pct"], st["disk_free_mb"], st["mem_avail_mb"],
                  st["load"], st["db_mb"]))
        print("[task] sys %s net=%s" % (msg, nb), flush=True)
        return True, msg
    if kind == "order_sweep":
        return _order_sweep_sync(dbp, cfg)
    return False, "未知任务类型：%s" % kind


async def _task_loop(app):
    """每 30 秒看一眼任务表；到期就在 executor 里跑，结果写回 last_ok/last_msg。"""
    db = _db(app)
    _task_seed(db)
    loop = asyncio.get_event_loop()
    await asyncio.sleep(25)
    while True:
        try:
            now = time.time()
            for t in _task_list(db):
                if not t["enabled"] or t["kind"] not in TASK_KINDS:
                    continue
                if t["cron"]:
                    # crontab 语义：命中当前这一分钟就跑；同一分钟不重复
                    # （循环每 30 秒扫一次，靠 last_run>=本分钟起点 去重）。
                    minute = int(now // 60 * 60)
                    if t["last_run"] >= minute or not _cron_match(t["cron"]):
                        continue
                else:
                    every = max(30, int(t["every_s"] or 0))
                    if now - t["last_run"] < every:
                        continue
                # 先写 last_run：慢任务（大库备份）不会被下一轮重复调起
                db.execute("UPDATE tasks SET last_run=? WHERE id=?", (now, t["id"]))
                db.commit()
                try:
                    cfg = json.loads(t["cfg"] or "{}")
                except Exception:
                    cfg = {}
                if not isinstance(cfg, dict):
                    cfg = {}
                oc = _oss_cfg(db)
                t0 = time.time()
                try:
                    ok, msg = await loop.run_in_executor(
                        None, _task_run_sync, app["dbp"], t["kind"], cfg, oc)
                except Exception as e:
                    ok, msg = False, "异常：%s" % str(e)[:200]
                db.execute("UPDATE tasks SET last_ok=?,last_msg=? WHERE id=?",
                           (1 if ok else 0, str(msg)[:400], t["id"]))
                db.commit()
                _task_log(db, t["id"], t0, time.time() - t0, ok, msg, "auto")
                print("[task] %s(%s) ok=%s %.1fs %s"
                      % (t["name"], t["kind"], ok, time.time() - t0, msg), flush=True)
                if not ok:
                    # 失败只发给这个任务选中的渠道；没选 = 所有已配置渠道
                    await _alert_once(app, "task_" + str(t["id"]),
                                      "⚠️ CodeBoss 计划任务失败：" + str(t["name"]), str(msg),
                                      only=t.get("notify") or None)
        except Exception as e:
            print("[task] loop failed: %s" % str(e)[:160], flush=True)
        await asyncio.sleep(30)


def _alert_pay(app, db, info):
    """付款到账 → 异步发各渠道（不阻塞支付回调，发失败只打日志）。"""
    if not info:
        return
    try:
        row = db.execute("SELECT name FROM users WHERE id=?", (info.get("uid"),)).fetchone()
        uname = (row[0] if row else "") or ("uid %s" % info.get("uid"))
        sku = info.get("sku") or ""
        nm = (pay_skus().get(sku) or (sku, "", 0))[0]
        subject = "💳 收到付款 ¥%s" % info.get("money")
        text = ("用户：%s\n套餐：%s（%s）\n订单号：%s"
                % (uname, nm, sku, info.get("order")))
        asyncio.create_task(_alert_send(app, subject, text))
    except Exception as e:
        print("[alert] 组装提醒失败：%s" % str(e)[:100], flush=True)


async def _alert_send(app, subject, text, only=None):
    """发提醒：邮件 + 已配置的 webhook 渠道。任一渠道失败不影响其它渠道。
    only：只发这些渠道（None/空=所有已配置渠道）。"""
    cfg = _alert_cfg(_db(app))
    only = set(only or [])
    want = lambda k: (not only) or (k in only)
    loop = asyncio.get_event_loop()
    if want("email") and SMTP_HOST:
        for addr in cfg.get("email") or []:
            try:
                await loop.run_in_executor(None, _mail_send, addr, subject, text)
            except Exception as e:
                print("[alert] 邮件 %s 失败：%s" % (addr[:40], str(e)[:100]), flush=True)
    jobs = []
    # 钉钉/企微自定义机器人常开「自定义关键词」校验：正文必须含关键词，否则接口返回
    # errcode 310000（HTTP 仍是 200）。统一给 webhook 正文加小写 codeboss 前缀。
    body = "[CodeBoss] " + subject + "\n" + text
    if want("dingtalk"):
        for url in cfg.get("dingtalk") or []:
            jobs.append(("钉钉", url,
                         {"msgtype": "text", "text": {"content": body}}))
    if want("wecom"):
        for url in cfg.get("wecom") or []:
            jobs.append(("企业微信", url,
                         {"msgtype": "text", "text": {"content": body}}))
    if want("serverchan"):
        for key in cfg.get("serverchan") or []:
            url = key if key.startswith("http") else "https://sctapi.ftqq.com/%s.send" % key
            jobs.append(("Server酱", url,
                         {"title": subject, "desp": text.replace("\n", "\n\n")}))
    if want("pushplus"):
        for tok in cfg.get("pushplus") or []:
            jobs.append(("PushPlus", "https://www.pushplus.plus/send",
                         {"token": tok, "title": subject,
                          "content": text.replace("\n", "<br>"), "template": "html"}))
    if not jobs:
        return
    from aiohttp import ClientSession, ClientTimeout

    def _ch_note(label, ok, t0, err=""):
        key = {"钉钉": "ch_dingtalk", "企业微信": "ch_wecom",
               "Server酱": "ch_serverchan", "PushPlus": "ch_pushplus"}.get(label)
        if key:
            _svc_note(key, ok, (time.time() - t0) * 1000, err)

    async with ClientSession(timeout=ClientTimeout(total=10)) as s:
        for label, url, payload in jobs:
            t0 = time.time()
            try:
                async with s.post(url, json=payload) as r:
                    raw = await r.text()
                    if r.status >= 400:
                        print("[alert] %s HTTP %s %s"
                              % (label, r.status, raw[:120]), flush=True)
                        _ch_note(label, False, t0, "HTTP %s: %s" % (r.status, raw[:160]))
                        continue
                    # 钉钉/企微出错时照样回 200，错误在 JSON 的 errcode 里
                    try:
                        j = json.loads(raw)
                    except ValueError:
                        j = {}
                    bad = ""
                    if label in ("钉钉", "企业微信"):
                        ec = j.get("errcode")
                        if ec not in (None, 0):
                            bad = "%s: %s" % (ec, str(j.get("errmsg"))[:120])
                    elif label == "Server酱":
                        if j.get("code") not in (None, 0):
                            bad = "%s: %s" % (j.get("code"), str(j.get("message"))[:120])
                    elif label == "PushPlus":
                        if j.get("code") not in (None, 200):
                            bad = "%s: %s" % (j.get("code"), str(j.get("msg"))[:120])
                    if bad:
                        print("[alert] %s 返回错误 %s" % (label, bad), flush=True)
                    _ch_note(label, not bad, t0, bad)
            except Exception as e:
                print("[alert] %s 失败：%s" % (label, str(e)[:100]), flush=True)
                _ch_note(label, False, t0, str(e)[:160])


def _pay_status_json(db, uid, otn=""):
    """最近一单 + 当前套餐；给了 otn 就只看这一单（开发机端轮询自己下的单）。"""
    row = None
    if otn:
        row = db.execute("SELECT out_trade_no,sku,money,status,paid FROM orders "
                         "WHERE out_trade_no=? AND uid=?", (otn, uid)).fetchone()
    if row is None:
        row = db.execute("SELECT out_trade_no,sku,money,status,paid FROM orders "
                         "WHERE uid=? ORDER BY created DESC LIMIT 1", (uid,)).fetchone()
    plan, exp, cur_sku, next_sku, next_days = plan_state(db, uid)
    return {
        "ok": True, "plan": plan, "sku": cur_sku,
        "expires": float(exp or 0),
        # 刚买的是降档 → 这笔挂在 plan_next 上，页面据此写「已预约 · 哪天起生效」
        "next": ({"sku": next_sku, "from": _queue_start(exp), "days": int(next_days or 0)}
                 if next_sku else None),
        "order": ({"out_trade_no": row[0], "sku": row[1], "money": row[2],
                   "status": row[3], "paid": row[4]} if row else None)}


async def r_pay_status(request):
    """网页轮询：最近一单 + 当前套餐（付款后自动刷新用）。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    return web.json_response(_pay_status_json(_db(request.app), uid,
                                              (request.query.get("otn") or "").strip()))


async def h_pay_status(request):
    """开发机端轮询（设备凭证自证）：可选 otn，只看自己下的那一单。"""
    db = _db(request.app)
    try:
        data = await request.json()
    except Exception:
        data = {}
    (_dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机还没绑定账号"})
    return web.json_response(_pay_status_json(db, owner, str(data.get("otn") or "").strip()))


async def r_bind_email(request):
    """换绑邮箱：登录态 + 新邮箱验证码（purpose=bind）。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return web.json_response({"ok": False, "error": "邮箱格式不正确"})
    db = _db(request.app)
    if not _use_code(db, email, (data.get("code") or "").strip(), "bind"):
        return web.json_response({"ok": False, "error": "验证码错误或已过期"})
    dup = db.execute("SELECT id FROM users WHERE email=? AND id!=?", (email, uid)).fetchone()
    if dup:
        return web.json_response({"ok": False, "error": "该邮箱已被其他账号绑定"})
    db.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
    db.commit()
    print("[relay] 邮箱已换绑 user_id=%s ip=%s" % (uid, request.remote or "?"), flush=True)
    return web.json_response({"ok": True, "email": email})


async def r_bind_phone(request):
    """绑定/换绑手机号：登录态 + 新号码短信验证码（purpose=bind）。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    data = await request.json()
    phone = _norm_phone(data.get("phone"))
    if not _PHONE_RE.match(phone):
        return web.json_response({"ok": False, "error": "手机号格式不正确"})
    db = _db(request.app)
    dup = db.execute("SELECT id FROM users WHERE phone=? AND id!=?", (phone, uid)).fetchone()
    if dup:
        return web.json_response({"ok": False, "error": "该手机号已被其他账号绑定"})
    if not await _phone_verify(db, phone, data.get("code") or "", "bind"):
        return web.json_response({"ok": False, "error": "验证码错误或已过期"})
    db.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
    db.commit()
    print("[relay] 手机已绑定 user_id=%s %s ip=%s"
          % (uid, _mask_phone(phone), request.remote or "?"), flush=True)
    return web.json_response({"ok": True, "phone": _mask_phone(phone)})


async def r_changepass(request):
    """登录态改密码：验旧密码 → 换 salt/hash → epoch+1（其他设备全部下线），
    当前会话重签 cookie 保持在线。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    data = await request.json()
    old, new = data.get("old") or "", data.get("new") or ""
    if len(new) < 6:
        return web.json_response({"ok": False, "error": "新密码至少 6 位"})
    if not rate_ok(request.remote or "?", "auth", 30):
        return web.json_response({"ok": False, "error": "尝试过多，1 小时后再试"})
    db = _db(request.app)
    row = db.execute("SELECT pass_hash,salt,sess_epoch,must_change_pw FROM users WHERE id=?",
                     (uid,)).fetchone()
    # 首次设置（must_change=1）：免旧密码——初始密码就是注册验证码，设完 epoch+1
    # 其他设备全下线，当前会话重签保持在线，安全性等价于知道验证码。
    if not row or (not row[3] and not hmac.compare_digest(row[0], hash_pw(old, row[1]))):
        return web.json_response({"ok": False, "error": "旧密码错误"})
    salt = secrets.token_hex(8)
    ep = (row[2] or 0) + 1
    db.execute("UPDATE users SET pass_hash=?,salt=?,sess_epoch=?,must_change_pw=0 WHERE id=?",
               (hash_pw(new, salt), salt, ep, uid))
    db.commit()
    print("[relay] 密码已修改 user_id=%s ip=%s" % (uid, request.remote or "?"), flush=True)
    out = web.json_response({"ok": True})
    out.set_cookie("r_sess", make_sess(request.app["key"], uid, ep),
                   max_age=86400 * 7, httponly=True, samesite="Lax",
                   secure=(request.scheme == "https"))
    return out


def _tk_expire(db, now=None):
    db.execute("UPDATE takeover_req SET status='expired' WHERE status='pending' AND expires<?",
               (now or time.time(),))


def _tk_new(db, dev, owner_uid, to_uid, code):
    """记一条接管请求（同一台机器、同一个新账号还没处理的，直接续期复用）。"""
    now = time.time()
    _tk_expire(db, now)
    row = db.execute("SELECT id FROM takeover_req WHERE dev=? AND to_uid=? AND status='pending'"
                     " AND expires>?", (dev, to_uid, now)).fetchone()
    if row:
        db.execute("UPDATE takeover_req SET code=?, expires=? WHERE id=?",
                   (code, now + TAKEOVER_TTL, row[0]))
        return row[0]
    rid = secrets.token_hex(8)
    db.execute("INSERT INTO takeover_req(id,dev,owner_uid,to_uid,code,created,expires,status)"
               " VALUES(?,?,?,?,?,?,?,'pending')",
               (rid, dev, owner_uid, to_uid, code, now, now + TAKEOVER_TTL))
    return rid


def _merge_usage(db, old, new, nid):
    """把旧主体（d<dev>）的当日/当月用量并入新主体（u<uid>）。
    只并当天与当月，不做历史回填；动作表/刻度表/客户端表一起换主体（nid=账号 int id），
    撞键的残余直接删（同一刻度已经在新主体里计过，留着会重复算）。"""
    day, mon = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    for fmt, suffix in (("use:%s:d:%s", day), ("asr:%s:d:%s", day),
                        ("asr:%s:m:%s", mon), ("llm:%s:d:%s", day)):
        ko, kn = fmt % (old, suffix), fmt % (new, suffix)
        v = usage_get(db, ko)
        if v:
            usage_add(db, kn, v)
            db.execute("DELETE FROM usage WHERE k=?", (ko,))
    for sql in ("UPDATE OR IGNORE use_act_log SET uid=? WHERE uid=?",
                "UPDATE OR IGNORE usage_min SET uid=? WHERE uid=?",
                "UPDATE OR IGNORE clients SET uid=? WHERE uid=?"):
        db.execute(sql, (int(nid), old))
    for t in ("use_act_log", "usage_min", "clients"):
        db.execute("DELETE FROM %s WHERE uid=?" % t, (old,))
    db.commit()


def _claim_device(db, uid, dev):
    """把匿名设备认领到账号：owner 写入、anon=0，d<dev> 的用量并入 u<uid>。
    返回 True=已归这个账号；False=设备不存在或已在别人名下（不抢）。"""
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or (row[0] is not None and int(row[0]) != int(uid)):
        return False
    if row[0] is None:
        db.execute("UPDATE devices SET owner=?, anon=0 WHERE id=?", (uid, dev))
        _merge_usage(db, "d%s" % dev, "u%d" % int(uid), uid)
    else:
        db.execute("UPDATE devices SET anon=0 WHERE id=?", (dev,))
    db.commit()
    return True


def _do_pair(db, uid, code, takeover=False):
    """返回 (结果, 附加信息)：
      ok           绑定成功
      bad          码无效/过期
      quota        新账号设备已满（附加 = 上限的错误文案）
      need         机器在别人名下 → 附加 = 接管请求 id（等原账号或本机确认）

    **机器已在别人名下时绝不在这里改归属**：拿得到配对码（码就印在那台机器屏幕上）
    不等于得到原账号同意。原账号手机弹窗或本机弹窗确认后才走 _tk_apply。

    已属于自己的设备重复扫码 = 幂等重绑，不占额度（否则满额时连自己的机器都再扫不进来）。
    takeover 参数保留只为兼容老客户端：现在一律走确认，传了也不生效。"""
    row = db.execute("SELECT device_id,expires,used FROM pair_codes WHERE code=?", (code,)).fetchone()
    if not row or row[2] or row[1] < time.time():
        return "bad", None
    dev = row[0]
    own = db.execute("SELECT owner,name FROM devices WHERE id=?", (dev,)).fetchone()
    owner = own[0] if own else None
    n = db.execute("SELECT COUNT(*) FROM devices WHERE owner=?", (uid,)).fetchone()[0]
    cap = int(plan_limits(db, uid).get("hosts") or 0)
    if cap and n >= cap and owner != uid:
        return "quota", _cap_error("开发机数", user_plan(db, uid), cap, "hosts", "台")
    if owner and owner != uid:
        rid = _tk_new(db, dev, int(owner), int(uid), code)
        db.commit()
        print("[relay] 接管请求 %s dev=%s 原账号=%s 新账号=%s（等确认，不自动改归属）"
              % (rid, dev, owner, uid), flush=True)
        return "need", rid
    _claim_device(db, uid, dev)   # 匿名设备配对到账号 = 转正（anon=0），用量并入账号
    db.execute("UPDATE pair_codes SET used=1 WHERE code=?", (code,))
    db.commit()
    return "ok", None


def _tk_row(db, rid):
    _tk_expire(db)
    return db.execute("SELECT id,dev,owner_uid,to_uid,code,created,expires,status"
                      " FROM takeover_req WHERE id=?", (rid,)).fetchone()


def _tk_apply(db, rid, approve, actor_dev, actor_uid):
    """原账号（或机器本身）对请求表态。返回 (结果, 文案)。
    授权：自证设备就是被接管的机器；或自证设备属于原账号。"""
    r = _tk_row(db, rid)
    if not r:
        return "bad", "请求不存在"
    _id, dev, owner_uid, to_uid, code, _c, _e, status = r
    if status != "pending":
        return "bad", "这条请求已经处理过了"
    if not (actor_dev == dev or (actor_uid is not None and int(actor_uid) == int(owner_uid))):
        return "forbidden", "无权处理这条请求"
    if not approve:
        db.execute("UPDATE takeover_req SET status='denied' WHERE id=?", (rid,))
        db.commit()
        print("[relay] 接管被拒 %s dev=%s actor=%s" % (rid, dev, actor_dev), flush=True)
        return "ok", "已拒绝"
    pc = db.execute("SELECT expires,used FROM pair_codes WHERE code=?", (code,)).fetchone()
    now = time.time()
    cur = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    if not pc or pc[1] or pc[0] < now:
        db.execute("UPDATE takeover_req SET status='expired' WHERE id=?", (rid,))
        db.commit()
        return "expired", "配对码已过期，让对方重新扫码"
    if not cur or cur[0] != owner_uid:
        # 排队期间机器已经换了主人：这条请求没资格再动它（按现状作废，别让旧账号再搬一次）
        db.execute("UPDATE takeover_req SET status='expired' WHERE id=?", (rid,))
        db.commit()
        return "expired", "这台电脑的归属已经变了，这次接管作废"
    n = db.execute("SELECT COUNT(*) FROM devices WHERE owner=?", (to_uid,)).fetchone()[0]
    cap = int(plan_limits(db, to_uid).get("hosts") or 0)
    if cap and n >= cap:
        return "quota", "对方账号的开发机名额已满"
    db.execute("UPDATE devices SET owner=? WHERE id=?", (to_uid, dev))
    db.execute("UPDATE pair_codes SET used=1 WHERE code=?", (code,))
    db.execute("UPDATE takeover_req SET status='approved' WHERE id=?", (rid,))
    # 同一台机器排队的其他请求一起作废：主机已经换人，它们不再有资格
    db.execute("UPDATE takeover_req SET status='expired'"
               " WHERE dev=? AND status='pending' AND id<>?", (dev, rid))
    db.commit()
    print("[relay] 接管已确认 %s dev=%s actor=%s → 新账号 %s"
          % (rid, dev, actor_dev, to_uid), flush=True)
    return "ok", "已确认"


async def r_pair(request):
    data = await request.json()
    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"ok": False, "error": "配对码为空"})
    db = _db(request.app)
    uid = uid_of(request)
    if uid is None:
        # 免注册免费版：没账号也能扫，但只放行「无主」的机器。码在别人机器屏幕上
        # 不代表能用：已绑定账号的设备必须登录后走老的配对/接管流程。
        row = db.execute("SELECT d.id, d.owner FROM pair_codes c JOIN devices d"
                         " ON d.id=c.device_id WHERE c.code=?", (code,)).fetchone()
        if not row or row[1]:
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        pc = db.execute("SELECT expires,used FROM pair_codes WHERE code=?", (code,)).fetchone()
        if not pc or pc[1] or pc[0] < time.time():
            return web.json_response({"ok": False, "error": "配对码无效或已过期"}, status=400)
        dev = row[0]
        db.execute("UPDATE pair_codes SET used=1 WHERE code=?", (code,))
        db.execute("UPDATE devices SET anon=1 WHERE id=?", (dev,))
        db.commit()
        resp = web.json_response({"ok": True, "anon": True})
        resp.set_cookie(DEV_SESS_COOKIE, _dev_sess_make(db, dev), max_age=DEV_SESS_TTL,
                        httponly=True, samesite="lax", path="/",
                        secure=(request.scheme == "https"))
        return resp
    r, extra = _do_pair(db, uid, code, bool(data.get("takeover")))
    if r == "ok":
        return web.json_response({"ok": True})
    if r == "need":
        # 只告知「已被占用」和要等确认，不带出对方账号信息
        row = db.execute("SELECT d.name FROM pair_codes c JOIN devices d ON d.id=c.device_id "
                         "WHERE c.code=?", (code,)).fetchone()
        await _tk_notify(request.app, db, extra)
        return web.json_response({"ok": False, "need_confirm": True, "req": extra,
                                  "device_name": (row[0] if row else "") or "",
                                  "error": "这台电脑已绑定其他账号，需要原账号确认"})
    if r == "quota":
        return web.json_response({"ok": False, "error": extra})
    return web.json_response({"ok": False, "error": "配对码无效或已过期"})


async def r_devpair(request):
    """开发机自证换新配对码（Mac 客户端「刷新二维码」用）。
    只有持有 device_secret 的一方能换；旧的一次性码作废，不重建设备记录。"""
    data = await request.json()
    dev = (data.get("dev") or "").strip()
    sec = (data.get("sec") or "").strip()
    if not dev or not sec:
        return web.json_response({"ok": False, "error": "参数不全"})
    if not rate_ok(request.remote or "?", "devpair", 30):
        return web.json_response({"ok": False, "error": "过于频繁，稍后再试"}, status=429)
    db = _db(request.app)
    row = db.execute("SELECT secret FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or not hmac.compare_digest(row[0], sec):
        return web.json_response({"ok": False, "error": "设备凭证无效"}, status=403)
    now = time.time()
    code = secrets.token_hex(6)
    db.execute("DELETE FROM pair_codes WHERE device_id=? AND used=0", (dev,))
    db.execute("INSERT INTO pair_codes(code,device_id,created,expires) VALUES(?,?,?,?)",
               (code, dev, now, now + PAIR_TTL))
    db.commit()
    return web.json_response({"ok": True, "pair_code": code, "pair_url": "/r/?p=%s" % code})


async def r_dev_rename(request):
    """给自己的开发机改名（只改 relay 侧展示名，不改设备本身）。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    data = await request.json()
    dev = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()[:40]
    db = _db(request.app)
    if not own_device(db, uid, dev):
        return web.json_response({"ok": False, "error": "设备不存在或无权操作"})
    if not name:
        return web.json_response({"ok": False, "error": "名称不能为空"})
    db.execute("UPDATE devices SET name=? WHERE id=?", (name, dev))
    db.commit()
    return web.json_response({"ok": True})


def _devices_payload(app, db, uid):
    """uid → 设备列表 + 配额（r_devices 与设备自证版共用）。"""
    rows = db.execute("SELECT id,name,last_seen,frozen,product FROM devices WHERE owner=? "
                      "ORDER BY last_seen DESC", (uid,)).fetchall()
    out = []
    for did, name, seen, fz, product in rows:
        ws = app["links"].get(did)
        info = (app.get("direct") or {}).get(did) or {}
        out.append({"id": did, "name": name, "online": bool(ws and not ws.closed),
                    "frozen": bool(fz), "ver": info.get("ver") or "",
                    "product": product or branding.PRODUCT_DEFAULT,
                    "last_seen": time.strftime("%m-%d %H:%M", time.localtime(seen)) if seen else ""})
    _cap = int(plan_limits(db, uid).get("hosts") or 0)
    quota = "" if not _cap else "（%d/%d）" % (len(out), _cap)
    return {"ok": True, "devices": out, "quota": quota}


def _dev_self_auth(db, data):
    """开发机自证：{dev, sec} → ((dev, owner_uid), None)，失败 → ((None, None), 错误响应)。
    局域网直连的手机没有 relay 用户会话，host 代理用设备凭证代表同 owner。"""
    dev = (data.get("dev") or "").strip()
    sec = (data.get("sec") or "").strip()
    if not dev or not sec:
        return (None, None), web.json_response({"ok": False, "error": "参数不全"})
    row = db.execute("SELECT secret, owner FROM devices WHERE id=?", (dev,)).fetchone()
    if not row or not hmac.compare_digest(row[0], sec):
        return (None, None), web.json_response({"ok": False, "error": "设备凭证无效"}, status=403)
    return (dev, row[1]), None


async def r_dev_ledger(request):
    """跨机器台账（设备自证，host 代理）：同一账号下各台在线机器在干什么。
    手机连的是哪台机器，就由哪台的 host 来问中继，页面只用一份数据。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devledger", 900):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    return web.json_response(await _dev_ledger_payload(request.app, db, int(owner)))


async def r_dev_search(request):
    """跨机器搜索（设备自证，host 代理）：一句话搜同一账号下所有在线机器。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devsearch", 600):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    return web.json_response(await _dev_search_payload(request.app, db, int(owner),
                                                       data.get("q")))


async def r_dev_devices(request):
    """设备自证版设备列表：host 在局域网直连时代理 /r/devices 用。
    手机直连进 host 时同样能看到设备栏（账号/状态归中继的架构补全）。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devself", 60):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    return web.json_response(_devices_payload(request.app, db, owner))


def _acct_owner(request, db, data):
    """账号管理的身份：优先 relay 用户会话（网页/限额页），其次设备自证（手机 App 经 host 代理）。"""
    uid = uid_of(request)
    if uid:
        return int(uid), None
    tu = check_acct_tok(request.app, db, (data or {}).get("s"))
    if tu:
        return int(tu), None
    (dev, owner), err = _dev_self_auth(db, data or {})
    if err:
        return None, err
    if owner is None:
        return None, web.json_response({"ok": False, "error": "本机未绑定账号"})
    return int(owner), None


async def r_acct_info(request):
    """账号信息 + 已登录客户端（设备自证，host 代理手机端的「账号管理」）。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "acct", 60):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    owner, err = _acct_owner(request, db, data)
    if err:
        return err
    row = db.execute("SELECT name,email,phone FROM users WHERE id=?", (owner,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "账号不存在"})
    info = _owner_clients(db, owner, str(data.get("cid") or "")[:64])
    allowed, src = _allowed_products(db, owner)      # 这账号能用哪些端（host 只做展示）
    return web.json_response(dict(ok=True, allowed=allowed, source=src,
                                  name=row[0] or "",
                                  email=_mask_email(row[1] or ""),
                                  phone=_mask_phone(row[2] or ""),
                                  has_email=bool(row[1]), has_phone=bool(row[2]),
                                  **info))


async def r_acct_send_code(request):
    """给「踢客户端」发二次验证码：发到账号绑定的手机号或邮箱。"""
    data = await request.json()
    db = _db(request.app)
    owner, err = _acct_owner(request, db, data)
    if err:
        return err
    via = (data.get("via") or "").strip().lower()
    row = db.execute("SELECT email,phone FROM users WHERE id=?", (owner,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "账号不存在"})
    email, phone = (row[0] or "").strip().lower(), _norm_phone(row[1] or "")
    if not rate_ok(request.remote or "?", "acctcode", 10):
        return web.json_response({"ok": False, "error": "验证码请求过于频繁，稍后再试"}, status=429)
    now = time.time()
    if via == "sms":
        if not phone:
            return web.json_response({"ok": False, "error": "账号没绑手机号，请先用邮箱验证或去账号中心绑定"})
        if not SMS_ON:
            return web.json_response({"ok": False, "error": "中继没配短信通道"}, status=502)
        db.execute("DELETE FROM phone_codes WHERE expires<?", (now - 3600,))
        last = db.execute("SELECT created FROM phone_codes WHERE phone=? ORDER BY id DESC LIMIT 1",
                          (phone,)).fetchone()
        if last and now - last[0] < SMS_COOLDOWN:
            return web.json_response({"ok": False, "error": "发送太频繁，请稍后再试"})
        out_id = ("%06d" % secrets.randbelow(1000000)) if SMS_CONSOLE else secrets.token_hex(8)
        try:
            await asyncio.to_thread(_sms_send_sync, phone, out_id, ALI_TEMPLATE)
        except Exception as e:
            return web.json_response({"ok": False, "error": "短信发送失败：%s" % str(e)[:140]},
                                     status=502)
        db.execute("INSERT INTO phone_codes(phone,out_id,purpose,created,expires) VALUES(?,?,?,?,?)",
                   (phone, out_id, "acct_kick", now, now + SMS_TTL))
        db.commit()
        return web.json_response({"ok": True, "via": "sms", "to": _mask_phone(phone),
                                  "msg": "验证码已发到 " + _mask_phone(phone)})
    if via == "email":
        if not email:
            return web.json_response({"ok": False, "error": "账号没绑邮箱，请用手机验证或去账号中心绑定"})
        if not SMTP_HOST:
            return web.json_response({"ok": False, "error": "中继没配邮件通道"}, status=502)
        db.execute("DELETE FROM email_codes WHERE expires<?", (now - 3600,))
        last = db.execute("SELECT created FROM email_codes WHERE email=? ORDER BY id DESC LIMIT 1",
                          (email,)).fetchone()
        if last and now - last[0] < 60:
            return web.json_response({"ok": False, "error": "发送太频繁，请 1 分钟后再试"})
        code = "%06d" % secrets.randbelow(1000000)
        db.execute("INSERT INTO email_codes(email,code,purpose,created,expires)"
                   " VALUES(?,?,?,?,?)", (email, code, "acct_kick", now, now + CODE_TTL))
        db.commit()
        try:
            ok = await asyncio.to_thread(_mail_send, email, "CodeBoss 验证码",
                                         "你正在踢掉一个已登录的客户端。验证码：%s（10 分钟内有效，非本人操作请忽略）" % code)
        except Exception as e:
            ok, why = False, "%s: %s" % (type(e).__name__, str(e)[:120])
        else:
            why = ""
        if not ok:
            db.execute("DELETE FROM email_codes WHERE email=? AND code=?", (email, code))
            db.commit()
            return web.json_response({"ok": False, "error": "验证码发送失败：" + (why or "未知原因")},
                                     status=502)
        return web.json_response({"ok": True, "via": "email", "to": _mask_email(email),
                                  "msg": "验证码已发到 " + _mask_email(email)})
    return web.json_response({"ok": False, "error": "未知验证方式"})


async def r_acct_kick(request):
    """验证码通过后踢掉一个客户端（删除它占的客户端名额）。不能踢当前这台。"""
    data = await request.json()
    db = _db(request.app)
    owner, err = _acct_owner(request, db, data)
    if err:
        return err
    cid = str(data.get("cid") or "").strip()[:64]
    cur = str(data.get("cur") or "").strip()[:64]
    code = str(data.get("code") or "").strip()
    via = (data.get("via") or "").strip().lower()
    if not cid:
        return web.json_response({"ok": False, "error": "缺客户端标识"})
    if cid == cur:
        return web.json_response({"ok": False, "error": "不能踢当前正在用的这台"})
    row = db.execute("SELECT email,phone FROM users WHERE id=?", (owner,)).fetchone()
    email, phone = ((row[0] or "").strip().lower(), _norm_phone(row[1] or "")) if row else ("", "")
    ok = False
    if via == "sms" and phone:
        ok = await _phone_verify(db, phone, code, "acct_kick")
    elif via == "email" and email:
        ok = _use_code(db, email, code, "acct_kick")
    if not ok:
        return web.json_response({"ok": False, "error": "验证码不对或已过期"})
    _delete_client(db, int(owner), cid)
    # 名额刚空出来：把整个账号的「已满」缓存清掉，正在被挡的那台刷新立刻能进来
    for k in [k for k in _CLI_GATE if k[0] == int(owner)]:
        _CLI_GATE.pop(k, None)
    print("[relay] 用户自踢客户端 uid=%s cid=%s via=%s" % (owner, cid[:10], via), flush=True)
    info = _owner_clients(db, owner, cur)
    return web.json_response(dict(ok=True, **info))


async def r_dev_pair(request):
    """设备自证版配对：host 代理 /r/pair。owner 隐含在自证设备上。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devpair", 30):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    code = (data.get("code") or "").strip()
    r, extra = _do_pair(db, owner, code, bool(data.get("takeover")))
    if r == "ok":
        return web.json_response({"ok": True})
    if r == "need":
        row = db.execute("SELECT d.name FROM pair_codes c JOIN devices d ON d.id=c.device_id "
                         "WHERE c.code=?", (code,)).fetchone()
        await _tk_notify(request.app, db, extra)
        return web.json_response({"ok": False, "need_confirm": True, "req": extra,
                                  "device_name": (row[0] if row else "") or "",
                                  "error": "这台电脑已绑定其他账号，需要原账号确认"})
    if r == "quota":
        return web.json_response({"ok": False, "error": extra})
    return web.json_response({"ok": False, "error": "配对码无效或已过期"})


def _tk_caller(db, request, data):
    """谁在问：中继网页会话，或设备自证（LAN 直连的手机由 host 代理）。
    返回 (uid, dev)：网页会话的 dev 为 None。"""
    uid = uid_of(request)
    if uid is not None:
        return int(uid), None
    dev = (data.get("dev") or "").strip()
    sec = (data.get("sec") or "").strip()
    if dev and sec:
        row = db.execute("SELECT secret, owner FROM devices WHERE id=?", (dev,)).fetchone()
        if row and hmac.compare_digest(row[0], sec):
            return (int(row[1]) if row[1] is not None else None), dev
    return None, None


async def _tk_push(app, dev, msg):
    ws = (app.get("links") or {}).get(dev)
    if ws is not None and not ws.closed:
        try:
            async with getattr(ws, "_cb_lock", asyncio.Lock()):
                await ws.send_json(msg)
        except Exception:
            pass


async def _tk_notify(app, db, rid):
    """接管请求刚落库：推给目标机器弹本机确认框（host 没在线也无妨，原账号手机照样能确认）。"""
    r = _tk_row(db, rid)
    if not r:
        return
    u = db.execute("SELECT name, phone FROM users WHERE id=?", (int(r[3]),)).fetchone()
    who = (u[0] if u and u[0] else "") or (("尾号%s" % (u[1] or "")[-4:]) if u and u[1] else "另一个账号")
    await _tk_push(app, r[1], {"t": "takeover", "id": r[0], "who": who,
                               "ttl": int(max(0, r[6] - time.time()))})


async def r_dev_pair_status(request):
    """新账号查接管进度（网页会话或设备自证均可）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    db = _db(request.app)
    uid, _dev = _tk_caller(db, request, data)
    r = _tk_row(db, (data.get("id") or "").strip())
    if not r:
        return web.json_response({"ok": False, "status": "expired",
                                  "error": "请求不存在或已过期"})
    if uid is None or int(uid) not in (int(r[2]), int(r[3])):
        return web.json_response({"ok": False, "error": "无权查看"}, status=403)
    return web.json_response({"ok": True, "status": r[7], "dev": r[1]})


async def r_dev_takeover_list(request):
    """原账号（或机器本身）看还有哪些接管请求等着确认。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    db = _db(request.app)
    uid, dev = _tk_caller(db, request, data)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    _tk_expire(db)
    db.commit()
    rows = db.execute(
        "SELECT t.id,t.dev,d.name,t.expires,u.name FROM takeover_req t"
        " LEFT JOIN devices d ON d.id=t.dev LEFT JOIN users u ON u.id=t.to_uid"
        " WHERE t.status='pending' AND (t.owner_uid=? OR t.dev=?)"
        " ORDER BY t.created DESC LIMIT 10", (int(uid), dev or "")).fetchall()
    return web.json_response({"ok": True, "reqs": [
        {"id": x[0], "dev": x[1], "dev_name": x[2] or x[1][:8],
         "to_name": x[4] or "", "expires": int(x[3])} for x in rows]})


async def r_dev_takeover_act(request):
    """原账号手机或机器本机表态：批准/拒绝。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not rate_ok(request.remote or "?", "takeover", 30):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    uid, dev = _tk_caller(db, request, data)
    if uid is None and dev is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    res, msg = _tk_apply(db, (data.get("id") or "").strip(),
                         1 if data.get("approve") else 0, dev, uid)
    return web.json_response({"ok": res == "ok", "status": res,
                              "msg": msg, "error": "" if res == "ok" else msg})


async def r_dev_rename_self(request):
    """设备自证版改名：host 代理 /r/dev_rename。目标机必须与自证机同 owner。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devself", 60):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    target = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()[:40]
    if not own_device(db, owner, target):
        return web.json_response({"ok": False, "error": "设备不存在或无权操作"})
    if not name:
        return web.json_response({"ok": False, "error": "名称不能为空"})
    db.execute("UPDATE devices SET name=? WHERE id=?", (name, target))
    db.commit()
    return web.json_response({"ok": True})


async def r_dev_del_self(request):
    """设备自证版删除：host 代理 /r/dev_del。删除自证机自己也允许（bye 逻辑同 r_dev_del）。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devself", 60):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    target = (data.get("id") or "").strip()
    if not own_device(db, owner, target):
        return web.json_response({"ok": False, "error": "设备不存在或无权操作"})
    ws = request.app["links"].pop(target, None)
    if ws and not ws.closed:
        try:
            async with getattr(ws, "_cb_lock", asyncio.Lock()):
                await ws.send_json({"t": "bye"})
            await ws.close()
        except Exception:
            pass
    db.execute("DELETE FROM devices WHERE id=?", (target,))
    db.execute("DELETE FROM pair_codes WHERE device_id=?", (target,))
    db.commit()
    print("[relay] 设备已删除(自证) dev=%s by=%s" % (target, dev), flush=True)
    return web.json_response({"ok": True})


# ---------- 事件提醒：业务逻辑/策略/规则在中继 ----------
# host 的 /api/session_event 收到 Claude Code hook 后原样转给 /r/dev/ev；中继按这里的
# 策略算「要不要提醒、提醒什么、多频繁」并返回，host 负责推给手机（SSE）。
# 中继不可达时 host 用本地缓存的同一份策略兜底，所以离线/LAN 也照常提醒。
#
# 规则可热更新：改 notify_policy.json 或 POST /r/admin/notify_policy，不用重启中继、
# 不用升 host，所有开发机的下一条事件就按新规则走。
NOTIFY_POLICY_DEFAULT = {
    "ver": 1,
    "enabled": True,
    "kinds": {"done": True, "perm": True, "input": True,
              "ask": True, "limit": True, "stuck": True},
    "sound": {"done": "done", "perm": "alert", "input": "ping",
              "ask": "ping", "limit": "alert", "stuck": "ping"},   # 各类型的提醒音（壳/页面按名字选音型）
    "voice": {"done": True, "perm": True, "input": False,
              "ask": True, "limit": True, "stuck": False},        # 哪些类型要语音播报（重要提醒）
    "perm_throttle": 4,      # 同一会话 PermissionRequest 的合并窗口（秒）
    "idle_after": 0,         # Stop 之后至少等多久才认 idle_prompt（秒，0=不等）
    "stuck_min": 10,         # running 但转录 N 分钟没动静 → 「可能卡住」（开发机侧扫描，0=关）
    "max_text": 160,         # 提醒正文最多保留多少字
    "done_text": "{msg}",
    "perm_text": "{tool} 需要确认：{brief}",
    "input_text": "等待你的回复",
    "ask_text": "{msg}",
    "limit_text": "{msg}",
    "stuck_text": "{title} 已 {min} 分钟没有输出，可能卡住了",
}


_NP_CACHE = {}   # path → (mtime_ns, policy)：改文件即热更新（不在运行时改 app 字典，aiohttp 3.14 已弃用）


def _np_path(app):
    return os.path.join(os.path.dirname(os.path.abspath(app["dbp"])), "notify_policy.json")


def _np(app):
    """读策略：文件 mtime 变了才重载（热更新）。缺文件/坏文件都退默认值。"""
    path = _np_path(app)
    try:
        mt = os.stat(path).st_mtime_ns
    except OSError:
        mt = 0
    cur = _NP_CACHE.get(path)
    if cur is not None and cur[0] == mt:
        return cur[1]
    data = dict(NOTIFY_POLICY_DEFAULT)
    if mt:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k in NOTIFY_POLICY_DEFAULT:
                    if k in raw:
                        data[k] = raw[k]
        except Exception as e:
            print("[notify] 策略文件读不了，用默认值：%s" % e, flush=True)
    _NP_CACHE[path] = (mt, _np_norm(data))
    return data


def _np_norm(data):
    """kinds/sound/voice 都是「类型 → 值」的小表：缺键补默认，旧策略文件也能用。"""
    for key in ("kinds", "sound", "voice"):
        base = dict(NOTIFY_POLICY_DEFAULT[key])
        for k, v in (data.get(key) or {}).items():
            base[k] = bool(v) if key in ("kinds", "voice") else str(v)[:20]
        data[key] = base
    return data


def _np_fmt(tpl, **kw):
    """模板替换只认具名占位符，不用 str.format——用户内容里的大括号会把格式化炸掉。"""
    out = str(tpl or "")
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v or ""))
    return out


def _np_tool_brief(ti):
    if not isinstance(ti, dict):
        return ""
    for k in ("command", "file_path", "path", "pattern", "url", "prompt"):
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:80]
    return ""


def _np_eval(st, ev, pol, now):
    """原始 hook 事件 → 提醒列表 [{kind,text}]。
    host 本地兜底是同一套语义（代码两处，规则以中继这份为准）。"""
    name = ev.get("hook_event_name") or ""
    out = []
    maxn = int(pol.get("max_text") or 160)
    if name == "UserPromptSubmit":
        st["prompt"] = now
        st["stop"] = 0
    elif name == "Stop":
        if ev.get("stop_hook_active"):
            return out                     # 自动续跑：不是真停
        st["stop"] = now
        st["done_at"] = now
        msg = re.sub(r"\s+", " ", (ev.get("last_assistant_message") or "").strip())
        out.append(("done", _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")))
    elif name == "PermissionRequest":
        if now - st.get("perm", 0) < float(pol.get("perm_throttle") or 0):
            return out
        st["perm"] = now
        st["perm_hook"] = now              # 权威那条（带 rid/选项），见 Notification 分支
        out.append(("perm", _np_fmt(pol.get("perm_text") or "{tool} 需要确认：{brief}",
                                    tool=ev.get("tool_name") or "工具",
                                    brief=_np_tool_brief(ev.get("tool_input")))))
    elif name == "StopFailure":
        # 一轮因 API 错误结束（额度和账单才算「要人看」；网络抖动/超时不打扰）
        err = str(ev.get("error") or "unknown")
        st["stop"] = 0                     # 出错停了，不再按「等待回复」重复提醒
        if err in ("rate_limit", "billing_error") and now - st.get("limit", 0) > 60:
            st["limit"] = now
            msg = re.sub(r"\s+", " ", str(ev.get("error_details") or "").strip())
            if not msg:
                msg = re.sub(r"\s+", " ", str(ev.get("last_assistant_message") or "").strip())
            if not msg:
                msg = "模型额度用完了" if err == "rate_limit" else "额度或账单异常"
            out.append(("limit", _np_fmt(pol.get("limit_text") or "{msg}", msg=msg[-maxn:])))
    elif name == "Notification":
        nt = str(ev.get("notification_type") or "")
        msg = re.sub(r"\s+", " ", str(ev.get("message") or "").strip())
        if nt in ("permission_prompt", "worker_permission_prompt"):
            # PermissionRequest 之后 6 秒 Claude Code 还会补一条自己的镜像通知，
            # 4 秒节流拦不住 → 手机被问两遍。同一轮已报过 PermissionRequest 的吞掉。
            hook = float(st.get("perm_hook") or 0)
            mirrored = hook and hook > float(st.get("prompt") or 0)
            if not mirrored and now - st.get("perm", 0) >= float(pol.get("perm_throttle") or 0):
                st["perm"] = now
                out.append(("perm", (msg or "需要你确认")[-maxn:]))
        elif nt == "idle_prompt":
            # 完成→空闲：Stop 已报过就不补；空闲只报一次、不持续追
            if st.get("done_at", 0) > st.get("prompt", 0):
                return out
            if (st.get("stop", 0) > st.get("prompt", 0)
                    and now - st.get("stop", 0) >= float(pol.get("idle_after") or 0)):
                st["prompt"] = now         # 一轮只提醒一次；下一轮 UserPromptSubmit 重置
                st["done_at"] = now
                out.append(("done", _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")))
        elif nt in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            # 等你在对话框/命令里输入或执行（不是选择题）
            if now - st.get("ask", 0) >= 4:
                st["ask"] = now
                out.append(("ask", _np_fmt(pol.get("ask_text") or "{msg}",
                                           msg=(msg or "需要你输入或执行命令")[-maxn:])))
        elif nt == "agent_completed":
            if now - st.get("done_at", 0) >= 10:     # Stop 与 agent_completed 去重
                st["done_at"] = now
                out.append(("done", _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")))
    kinds = pol.get("kinds") or {}
    snd = pol.get("sound") or {}
    voi = pol.get("voice") or {}
    return [{"kind": k, "text": t, "sound": snd.get(k) or k, "voice": bool(voi.get(k))}
            for k, t in out if kinds.get(k, True)]


async def r_dev_ev(request):
    """开发机转发原始 hook 事件 → 返回该发哪些提醒（host 负责推送）。
    绝不返回决策：PermissionRequest 一旦回 decision 就等于替用户点了「允许」。"""
    data = await request.json()
    if not rate_ok(request.remote or "?", "devev", 5000):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    pol = _np(request.app)
    ver = pol.get("ver", 1)
    # 基础提醒（文字/角标/提醒音）免费；智能提醒（模型总结 + 语音播报）才是 Pro / Max。
    # 这里不再对免费档返回空，只把 voice 强制为 false；开发机本地还有同一道闸。
    paid = owner is not None and bool(plan_features(db, owner).get("remind", True))
    ev = data.get("ev") or {}
    sid = str(ev.get("session_id") or "") if isinstance(ev, dict) else ""
    if not pol.get("enabled", True) or not sid:
        return web.json_response({"ok": True, "notify": [], "ver": ver, "remind": paid})
    nev = request.app["nev"]
    now = time.time()
    st = nev.setdefault((dev, sid), {})
    items = _np_eval(st, ev, pol, now)
    st["last"] = now
    if len(nev) > 400:                     # 一小时没动静的会话状态清掉
        for k in [k for k, v in nev.items() if now - v.get("last", now) > 3600]:
            nev.pop(k, None)
    title = str(data.get("title") or "")[:80]
    for x in items:
        x["sid"] = sid
        x["title"] = title or sid[:8]
        if not paid:
            x["voice"] = False
    return web.json_response({"ok": True, "notify": items, "ver": ver, "remind": paid})


async def r_dev_policy(request):
    """开发机定期拉策略：本地兜底与中继同一份规则（中继不可达时仍按最近一份提醒）。"""
    data = await request.json()
    (dev, owner), err = _dev_self_auth(_db(request.app), data)
    if err:
        return err
    return web.json_response({"ok": True, "policy": _np(request.app)})


async def r_admin_notify_policy(request):
    """热更新提醒策略（Bearer RELAY_ADMIN_TOKEN）。body 就是策略 JSON，缺省键保持原值。
    改完立即对所有开发机生效：它们要么让中继算，要么拉这份做本地兜底。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权访问"}, status=401)
    if request.method == "GET":
        return web.json_response({"ok": True, "policy": _np(request.app)})
    if not await _adm_sensitive(request):
        return _adm_need_sms(_db(request.app))
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return web.json_response({"ok": False, "error": "body 要是 JSON 对象"}, status=400)
    cur = dict(_np(request.app))
    for k in NOTIFY_POLICY_DEFAULT:
        if k in raw:
            cur[k] = raw[k]
    for key in ("kinds", "sound", "voice"):
        if isinstance(raw.get(key), dict):
            cur[key] = dict(cur.get(key) or {})
            cur[key].update(raw[key])
    cur = _np_norm(cur)
    cur["ver"] = int(cur.get("ver") or 1) + 1
    path = _np_path(request.app)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _NP_CACHE.pop(path, None)
    print("[relay] 提醒策略已更新 ver=%s" % cur["ver"], flush=True)
    return web.json_response({"ok": True, "policy": _np(request.app)})


async def r_devices(request):
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    db = _db(request.app)
    return web.json_response(_devices_payload(request.app, db, uid))


async def r_dev_del(request):
    """自助删除自己的开发机：relay 侧记录全清；设备在线则下发 bye 让开发机
    同步清除本机 config.json 里的 relay 注册（不再重连）。"""
    uid = uid_of(request)
    if uid is None:
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    data = await request.json()
    dev = (data.get("id") or "").strip()
    db = _db(request.app)
    if not own_device(db, uid, dev):
        return web.json_response({"ok": False, "error": "设备不存在或无权操作"})
    ws = request.app["links"].pop(dev, None)
    if ws and not ws.closed:
        try:
            async with getattr(ws, "_cb_lock", asyncio.Lock()):
                await ws.send_json({"t": "bye"})
            await ws.close()
        except Exception:
            pass
    db.execute("DELETE FROM devices WHERE id=?", (dev,))
    db.execute("DELETE FROM pair_codes WHERE device_id=?", (dev,))
    db.commit()
    print("[relay] 设备已删除 dev=%s uid=%d" % (dev, uid), flush=True)
    return web.json_response({"ok": True})


async def r_enroll(request):
    """开发机安装时登记：返回 device_id/secret/一次性配对码。secret 只在响应里出现这一次。"""
    ip = request.remote or "?"
    if not rate_ok(ip, "enroll", 10):
        return web.json_response({"ok": False, "error": "登记过于频繁"}, status=429)
    data = await request.json()
    name = (data.get("name") or "dev-%s" % secrets.token_hex(2))[:40]
    db = _db(request.app)
    # 硬件指纹：同一台开发机重装/删 config 再来登记，复用原设备（额度不重置）。
    fp = str(data.get("fp") or "").strip()[:128]
    fp = fp if fp and all(c in "0123456789abcdef" for c in fp.lower()) else ""
    old_sec = str(data.get("old_secret") or "")
    product = branding.valid_product(str(data.get("product") or ""))
    now = time.time()
    code = secrets.token_hex(6)
    if fp:
        # 设备身份只认硬件指纹，不分产品线：同一台机器换线登记仍是同一台设备，
        # 否则 codeboss/agentboss 各登记一次就各拿一份免费额度。
        row = db.execute("SELECT id, secret, owner, frozen FROM devices "
                         "WHERE fp=? ORDER BY created LIMIT 1", (fp,)).fetchone()
        if row:
            did, cur_sec, owner, frozen = row
            if frozen:
                return web.json_response({"ok": False, "error": "该设备已冻结"}, status=403)
            if owner and not hmac.compare_digest(str(cur_sec), old_sec):
                # 已认领（付费/绑定账号）的设备：换 secret 等于换手，必须有旧 secret。
                # config 丢了走后台人工解绑，不给匿名换手留口子。
                return web.json_response(
                    {"ok": False, "error": "设备已绑定账号，换绑需要旧 secret 或后台解绑"},
                    status=403)
            sec = secrets.token_hex(24)
            # 无主设备重新登记 = 回到匿名免费档（老流程留下的 anon=0 行也要转正，
            # 否则 _link_check 永远 409，这台机器怎么重装都连不上中继）。
            db.execute("UPDATE devices SET secret=?, name=?, product=?, anon=? WHERE id=?",
                       (sec, name, product, 0 if owner is not None else 1, did))
            db.execute("INSERT INTO pair_codes(code,device_id,created,expires) VALUES(?,?,?,?)",
                       (code, did, now, now + PAIR_TTL))
            db.commit()
            return web.json_response({"ok": True, "device_id": did, "device_secret": sec,
                                      "pair_code": code, "pair_url": "/r/?p=%s" % code})
    unowned = db.execute("SELECT COUNT(*) FROM devices WHERE owner IS NULL").fetchone()[0]
    if unowned >= 500:
        return web.json_response({"ok": False, "error": "未绑定设备过多，拒绝登记"}, status=503)
    did = secrets.token_hex(8)
    sec = secrets.token_hex(24)
    # 带指纹的新设备一律先是匿名设备（免注册免费版）；配对到账号时再转正。
    db.execute("INSERT INTO devices(id,name,secret,created,product,fp,anon) "
               "VALUES(?,?,?,?,?,?,?)", (did, name, sec, now, product, fp, 1 if fp else 0))
    db.execute("INSERT INTO pair_codes(code,device_id,created,expires) VALUES(?,?,?,?)",
               (code, did, now, now + PAIR_TTL))
    db.commit()
    return web.json_response({"ok": True, "device_id": did, "device_secret": sec,
                              "pair_code": code, "pair_url": "/r/?p=%s" % code})


# ---------- host-link WS ----------
FEAT_TTL = 900   # 功能快照有效期（秒）：和中继对账新鲜度同口径


def _feat_snap(db, uid, dev):
    """给 host 的功能开关快照，用设备 secret 签名。

    为什么签：功能墙在 host 上执行；不签的话改一下本地缓存就能把
    terminal/remind 打开（P2P/DC 不经过中继）。密钥＝设备 secret + "cb-feat-v1"，
    两端各持一半（host 有 secret，relay 库里有 secret）。"""
    if not dev:
        return {}
    row = db.execute("SELECT secret FROM devices WHERE id=?", (dev,)).fetchone()
    if not row:
        return {}
    feat = plan_features(db, uid)
    exp = int(time.time()) + FEAT_TTL
    body = {"feat": feat, "exp": exp, "plan": user_plan(db, uid)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    key = hmac.new(row[0].encode(), b"cb-feat-v1", hashlib.sha256).digest()
    return {"feat": feat, "feat_exp": exp, "feat_plan": body["plan"],
            "feat_sig": hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()}


async def _send_use_ack(ws, db, uid, ack=None, ack_gran="m", dev=""):
    """把账号今日额度同步给 host；ack 是本次已入库的窗口（host 据此从待报队列删除）。

    2026-11-08 起内部按秒记账（刻度可配，默认 3 秒）。兼容两种 host：
    - 新 host 读 used_s / cap_s / unit（秒）；
    - 老 host 读 used / cap（分钟，向下取整）——它只会按分钟补报，ack 也给分钟。
    ack_gran：本次补报的粒度，"s"=窗口起点，"m"=老的分钟序号。"""
    used_s, cap_s = use_used(db, uid), use_cap_eff(db, uid)
    # 消息条数也一起下发：开发机在直连/P2P 上自己挡免费版的每日条数（中继看不见 DC 流量）。
    _sends_cap = int(plan_limits(db, uid).get("sends_day") or 0)
    msg = {"t": "use_ack", "ok": True,
           "used": used_s // 60, "cap": (cap_s // 60 if cap_s else 0),
           "used_s": used_s, "cap_s": cap_s, "unit": meter_unit(db),
           "sends": sends_used(db, uid), "sends_cap": _sends_cap,
           "date": time.strftime("%Y-%m-%d"),
           "plan": user_plan(db, uid)}
    msg.update(_feat_snap(db, uid, dev))   # 签名功能快照（host 侧终端墙用）
    if ack is not None:
        msg["ack"] = ack if ack_gran == "s" else [int(x) // 60 for x in ack]
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json(msg)
    except Exception:
        pass


async def _push_use_ack_uid(app, uid):
    """额度变了（买时长包 / 管理员调额度 / 换套餐）立刻推给该账号在线的开发机：
    不然 host 只能等下次补报才更新上限，买了包还是被本地额度墙挡着。"""
    db = _db(app)
    for (dev,) in db.execute("SELECT id FROM devices WHERE owner=?", (int(uid),)).fetchall():
        ws = (app.get("links") or {}).get(dev)
        if ws is not None and not ws.closed:
            try:
                await _send_use_ack(ws, db, uid)
            except Exception:
                pass


def _link_check(db, dev, sec, fp):
    """host 连接中继的准入判定。返回 (row, None) 或 (None, (status, text))。

    fp 非空且库里记着指纹时必须是同一台机器（config 复制到别的机器无效）；
    匿名设备（anon=1）没有 owner 也放行，老流程未绑定仍是 409。"""
    row = db.execute("SELECT secret, owner, frozen, fp, anon FROM devices WHERE id=?",
                     (dev,)).fetchone()
    if not row or not hmac.compare_digest(str(row[0]), sec):
        return None, (403, "bad device credentials")
    if row[2]:
        # 423 而不是 403：403 是「凭证失效」，host 收到会清掉本机注册、停重连——
        # 冻结是可恢复的（后台解冻），那台机器必须保留注册、等着自己连回来。
        return None, (423, "设备已冻结")
    if fp and row[3] and not hmac.compare_digest(str(row[3]), fp):
        return None, (403, "device fingerprint mismatch")
    if row[1] is None and not row[4]:
        return None, (409, "device not bound to account")
    return row, None


def _hosts_gate(db, dev, uid):
    """开发机数复核（每次 host 连接都查）：降档后超额时，只保留最近活跃的 cap 台，
    其余重连直接拒——否则「先开 Pro 绑两三台，再等降到 free」就能一直用。
    匿名/无主设备不占账号名额。返回 (ok, 文案)。"""
    if not uid or isinstance(uid, str):
        return True, ""
    cap = int(plan_limits(db, uid).get("hosts") or 0)
    if not cap:
        return True, ""
    ids = [r[0] for r in db.execute(
        "SELECT id FROM devices WHERE owner=? ORDER BY last_seen DESC", (int(uid),)).fetchall()]
    if len(ids) <= cap or dev in ids[:cap]:
        return True, ""
    return False, ("开发机数超出当前套餐（%d 台），开通 Pro 或移除其他开发机" % cap)


async def w_link(request):
    dev = request.query.get("dev") or ""
    sec = request.query.get("sec") or ""
    fp = request.query.get("fp") or ""
    db = _db(request.app)
    row, err = _link_check(db, dev, sec, fp)
    if err:
        # 409 = 还没配对（host 快速重试，不注销凭证）；403 = 凭证错/冻结/换机
        return web.Response(status=err[0], text=err[1])
    # 匿名设备没有 owner：用量主体是这台设备自己（d<dev>），不是「空账号」。
    uid = row[1] if row[1] is not None else subject_of(db, None, dev)
    _ok, _werr = _hosts_gate(db, dev, row[1])
    if not _ok:
        # 429（临时拒绝）：403 会让 host 清掉本机注册，降档超额的机器就永远回不来了
        # ——升级/移除别的开发机之后它应该自己重连上。
        print("[relay] host 连接被拒（设备数超额） dev=%s uid=%s" % (dev, row[1]), flush=True)
        return web.Response(status=429, text=_werr)
    if dev in request.app["links"]:
        old = request.app["links"].pop(dev)
        await old.close()  # 同设备重连：踢旧留新
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ws._cb_lock = asyncio.Lock()   # 多个隧道请求并发回帧：串行化 ws 写入
    request.app["links"][dev] = ws
    request.app.setdefault("link_ip", {})[dev] = request.remote or ""
    db.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), dev))
    db.commit()
    print("[relay] host linked dev=%s ip=%s" % (dev, request.remote), flush=True)
    await _push_presence(request.app, dev)   # 连上先报一次当前在线客户端数
    # 把今天的已用/上限先给 host：断链期间 host 靠这份缓存自己挡直连/P2P 额度。
    await _send_use_ack(ws, db, uid, dev=dev)
    # 日志上报开关（后台按开发机打开）：连上就告诉它当前状态，重连/中继重启后照样生效。
    # 到期的不再开（顺手把库里的开关落回 0，后台列表跟着变回「开日志」）。
    try:
        lr = db.execute("SELECT logs_on, logs_until FROM devices WHERE id=?", (dev,)).fetchone()
        if lr and lr[0]:
            if (lr[1] or 0) and lr[1] < time.time():
                db.execute("UPDATE devices SET logs_on=0, logs_until=0 WHERE id=?", (dev,))
                db.commit()
            else:
                async with ws._cb_lock:
                    await ws.send_json({"t": "logs_on", "on": 1})
    except Exception:
        pass
    seen_at = time.time()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break   # 超限/坏帧：按断链处理（流式隧道有 idle 超时兜底）
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            if d.get("t") == "logs":   # host 推来的 server.log 尾部（后台「日志上报」开着时）
                request.app["dev_logs"][dev] = {
                    "at": time.time(), "ver": str(d.get("ver") or "")[:20],
                    "txt": str(d.get("txt") or "")[-200000:]}
                # 同时落盘：中继重启/这台机下线后，后台还能回看（留 7 天）
                await asyncio.get_event_loop().run_in_executor(
                    None, _devlog_save, dev, str(d.get("ver") or "")[:20],
                    str(d.get("txt") or ""))
                continue
            if d.get("t") == "direct":   # host 上报局域网直连候选（P2P 牵线用）+ 版本号
                request.app["direct"][dev] = {
                    "lan": [str(x) for x in (d.get("lan") or [])][:6],
                    "port": int(d.get("port") or 0), "rtc": bool(d.get("rtc")),
                    # 开发机打不开 P2P 的原因（aiortc 没装/装坏）：原样带给手机，
                    # 不然页面上只有一句「开发机没启用 P2P」，看不出该去那台机器做什么。
                    "rtc_why": str(d.get("rtc_why") or "")[:160],
                    "ver": str(d.get("ver") or "")[:20], "ts": time.time()}
                try:   # 版本落库（离线/重启后后台还能看到最后跑过的版本）
                    db.execute("UPDATE devices SET ver=? WHERE id=? AND ver<>?",
                               (request.app["direct"][dev]["ver"], dev,
                                request.app["direct"][dev]["ver"]))
                    db.commit()
                except sqlite3.OperationalError:
                    pass
                print("[relay] direct 候选 dev=%s ver=%s lan=%s port=%s rtc=%s"
                      % (dev[:8], request.app["direct"][dev]["ver"] or "?",
                         ",".join(request.app["direct"][dev]["lan"]),
                         request.app["direct"][dev]["port"],
                         request.app["direct"][dev]["rtc"]), flush=True)
                continue
            if d.get("t") == "rtc_ans":   # WebRTC 信令应答回给等着的浏览器
                ent = request.app["rtc"].pop(str(d.get("id")), None)
                if ent and not ent[0].done():
                    ent[0].set_result(d)
                continue
            if d.get("t") == "use":   # host 补报直连/P2P 的分钟（幂等入库后回 ack）
                try:
                    _dn = db.execute("SELECT name FROM devices WHERE id=?", (dev,)).fetchone()
                    # 新 host 报 units（窗口起点，秒）；老 host 报 mins（分钟序号）
                    if d.get("units") is not None:
                        _marks, _gran = d.get("units") or [], "s"
                    else:
                        _marks, _gran = d.get("mins") or [], "m"
                    ok, _u, _c = use_record(db, uid, _marks, src="host",
                                            dev=dev, dev_name=(_dn[0] if _dn else "") or "",
                                            gran=_gran)
                    # 开发机补报的直连动作（LAN/P2P 不经过中继，中继看不到）：一并入库，
                    # 后台按分钟能看到「这一分钟在直连上做了什么」。
                    # 直连活跃分钟（P2P / 局域网）：host 侧按分钟上报，单独记一类，
                    # 「直连命中率」用它算；以前的 k='direct' 只代表「这一分钟中继没收到任何请求」，
                    # 手机只要发过一条消息（写操作本来就走中继）就会把整分钟算成中继。
                    for _it in (d.get("p2p_b") or [])[:2000]:
                        try:
                            _it = list(_it)
                            if len(_it) >= 3:
                                _mb, _mc, _by = int(_it[0]), str(_it[1])[:64], int(_it[2])
                            else:
                                _mb, _mc, _by = int(_it[0]), "", int(_it[1])
                            usage_bytes_add(db, uid, _mb, "p2p", 0, _by, 0,
                                            cid=_mc, dev=dev)
                        except (TypeError, ValueError, IndexError):
                            pass
                    for _m in (d.get("p2p") or [])[:4000]:
                        try:
                            use_client_touch(db, uid, int(_m), "p2p", "host",
                                             "P2P/LAN 直连")
                        except (TypeError, ValueError):
                            pass
                    try:
                        db.commit()
                    except Exception:
                        pass
                    for _a in (d.get("acts") or [])[:400]:
                        try:
                            use_act(db, uid, int(_a.get("m") or 0), "host", dev=dev,
                                    cid=str(_a.get("c") or ""),
                                    kind=str(_a.get("k") or ""), act=str(_a.get("a") or ""),
                                    path=str(_a.get("p") or ""), n=int(_a.get("n") or 1))
                        except (TypeError, ValueError):
                            pass
                    use_prune(db)
                    await _send_use_ack(ws, db, uid, ack=ok, ack_gran=_gran, dev=dev)
                except Exception as e:
                    print("[relay] use 帧处理失败: %s" % str(e)[:160], flush=True)
                continue
            st = request.app["streams"].get(d.get("id"))
            if st is not None:   # 流式隧道帧（SSE 分块）：chunk / res_end / 兜底整包
                t = d.get("t")
                if t == "chunk":
                    if not st.get("fallback"):
                        try:
                            st["q"].put_nowait(d)
                        except asyncio.QueueFull:
                            st["over"] = True   # 下游消费不动：tunnel 侧会发 cancel
                elif t == "res_end":
                    st["end"] = d
                    try:
                        st["q"].put_nowait(None)   # 哨兵：消费到 None = 收尾
                    except asyncio.QueueFull:
                        st["over"] = True
                elif t == "res":
                    f = st.get("first")
                    if f is not None and not f.done():
                        f.set_result(d)
                    if d.get("body"):   # 无 st:1 的整包 res（旧 host / 错误响应）：兜底
                        st["fallback"] = d
                        try:
                            st["q"].put_nowait(None)
                        except asyncio.QueueFull:
                            st["over"] = True
            elif d.get("t") == "res":
                ent = request.app["pending"].pop(d.get("id"), None)
                fut = ent[0] if isinstance(ent, tuple) else ent   # 兼容旧 (fut,dev) 标记
                if fut and not fut.done():
                    fut.set_result(d)
            elif d.get("t") == "apk":   # host 推客户端 APK：攒块，ver/code 随尾帧
                up = request.app["apk_up"].setdefault(dev, {"parts": {}, "meta": {}})
                try:
                    up["parts"][int(d.get("n", 0))] = str(d.get("b64") or "")
                    if d.get("code"):
                        up["meta"] = {"ver": str(d.get("ver") or "?"), "code": int(d.get("code"))}
                except Exception:
                    pass
            elif d.get("t") == "apk_end":
                up = request.app["apk_up"].pop(dev, None)
                if up and up["parts"]:
                    try:
                        blob = b"".join(base64.b64decode(up["parts"][k]) for k in sorted(up["parts"]))
                        if 10240 < len(blob) < 32 * 1024 * 1024:   # 尺寸护栏：烂帧/恶意帧不落盘
                            _apk_store(request.app, dev, blob, _apk_end_meta(d, up))
                    except Exception as e:
                        print("[relay] apk 存盘失败: %s" % str(e)[:120], flush=True)
            elif d.get("t") == "ios":   # host 推客户端 iOS 包（.ipa）：攒块，ver/code 随尾帧
                up = request.app["ios_up"].setdefault(dev, {"parts": {}, "meta": {}})
                try:
                    up["parts"][int(d.get("n", 0))] = str(d.get("b64") or "")
                    if d.get("code"):
                        up["meta"] = {"ver": str(d.get("ver") or "?"), "code": int(d.get("code"))}
                except Exception:
                    pass
            elif d.get("t") == "ios_end":
                up = request.app["ios_up"].pop(dev, None)
                if up and up["parts"]:
                    try:
                        blob = b"".join(base64.b64decode(up["parts"][k]) for k in sorted(up["parts"]))
                        if 10240 < len(blob) < 32 * 1024 * 1024:   # 尺寸护栏：烂帧/恶意帧不落盘
                            tmp = IOS_FILE + ".part"
                            with open(tmp, "wb") as f:
                                f.write(blob)
                            os.replace(tmp, IOS_FILE)   # 原子替换：下载半截不会发出去
                            meta = dict(up.get("meta") or {})
                            if d.get("code"):
                                meta = {"ver": str(d.get("ver") or "?"), "code": int(d.get("code"))}
                            meta.update(size=len(blob), ts=time.time(), dev=dev)
                            with open(IOS_META, "w") as f:
                                json.dump(meta, f)
                            print("[relay] ios stored code=%s ver=%s size=%d"
                                  % (meta.get("code"), meta.get("ver"), len(blob)), flush=True)
                            _oss_mirror(request.app, "codeboss.ipa", blob,
                                        "application/octet-stream")
                    except Exception as e:
                        print("[relay] ios 存盘失败: %s" % str(e)[:120], flush=True)
            if time.time() - seen_at > 30:   # last_seen 节流落盘，轮询不打爆 sqlite
                seen_at = time.time()
                db.execute("UPDATE devices SET last_seen=? WHERE id=?", (seen_at, dev))
                db.commit()
    finally:
        request.app["apk_up"].pop(dev, None)   # 断链时半截上传作废，下轮重连重推
        request.app["ios_up"].pop(dev, None)   # 同上（iOS .ipa）
        request.app["direct"].pop(dev, None)   # 断链即停发直连候选（旧票据靠 TTL 自然过期）
        for cid2, ent in list(request.app["rtc"].items()):   # 该设备挂起的信令立即失败
            if isinstance(ent, tuple) and ent[1] == dev:
                request.app["rtc"].pop(cid2, None)
                if not ent[0].done():
                    ent[0].set_exception(RuntimeError("host-link 断开"))
        if request.app["links"].get(dev) is ws:
            request.app["links"].pop(dev, None)
        for st in list(request.app["streams"].values()):   # 断链：流式隧道立即收尾
            if st.get("dev") == dev and not st.get("done"):
                st["err"] = "host-link 断开"
                try:
                    st["q"].put_nowait(None)
                except Exception:
                    pass
        for rid2, ent in list(request.app["pending"].items()):   # 该设备悬挂的整包请求立即失败
            if isinstance(ent, tuple) and ent[1] == dev:
                request.app["pending"].pop(rid2, None)
                if not ent[0].done():
                    ent[0].set_exception(RuntimeError("host-link 断开"))
        print("[relay] host unlinked dev=%s" % dev, flush=True)
    return ws


# ---------- 盲隧道代理（/r/* 与 /link 之外的一切） ----------
async def _link_api(app, dev, path, m="GET", body=None, secs=6):
    """借 host-link 调一台开发机的接口（整包 req 帧，和盲隧道同一个通道；
    host 侧会按请求路径注入本机 token）。失败/超时返回 None。"""
    ws = app["links"].get(dev)
    if ws is None or ws.closed:
        return None
    rid = app["rid"] = app.get("rid", 0) + 1
    fut = asyncio.get_event_loop().create_future()
    app["pending"][rid] = (fut, dev)
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json({"t": "req", "id": rid, "m": m, "p": path, "headers": {},
                                "body": base64.b64encode(json.dumps(body).encode()).decode()
                                if body else ""})
        res = await asyncio.wait_for(fut, secs)
        return json.loads(base64.b64decode(res.get("body") or b"").decode("utf-8", "replace"))
    except Exception:
        return None
    finally:
        app["pending"].pop(rid, None)


async def _link_sessions(app, dev):
    """借 host-link 查一台开发机的会话列表（host 侧会自动注入本机 token）。失败返回 []。"""
    d = await _link_api(app, dev, "/api/ext_sessions") or {}
    return d.get("sessions") or []


async def _dev_ledger_payload(app, db, owner, limit=4, ttl=10):
    """跨机器台账：owner 名下在线开发机各要一份 /api/task_ledger，合并成 machines[]。
    只问在线的（离线机器问了也是白等 6 秒）；结果缓存 ttl 秒——工作台每隔几秒
    轮询一次，每轮都去敲每台机器的门会把它们拖慢。"""
    cache = app.setdefault("ledger_cache", {})
    hit = cache.get(owner)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    rows = db.execute("SELECT id,name FROM devices WHERE owner=? AND frozen=0"
                      " ORDER BY last_seen DESC", (owner,)).fetchall()
    devs = [(i, n) for i, n in rows
            if app["links"].get(i) is not None and not app["links"].get(i).closed][:limit]
    # remote=0：只要这台机器的本机台账。不带的话 host 收到请求又会去问中继
    # 汇总别的机器——recursion（A 问中继、中继问 A……直到超时）。
    res = await asyncio.gather(*[_link_api(app, d, "/api/task_ledger?remote=0")
                                 for d, _ in devs])
    machines = []
    for (dev, name), r in zip(devs, res):
        items = [dict(it) for it in ((r or {}).get("items") or []) if isinstance(it, dict)]
        for it in items:
            it["machine"] = name or dev
            it["dev"] = dev
        machines.append({"dev": dev, "name": name or dev, "online": True, "items": items})
    out = {"ok": True, "machines": machines}
    if len(cache) > 256:
        cache.clear()
    cache[owner] = (time.time(), out)
    return out


async def _dev_search_payload(app, db, owner, q, limit=4):
    """跨机器搜索：owner 名下在线的机器各搜一遍 /api/search，合并成 machines[]。
    不缓存——每次查询的词不一样，缓存只是占内存。"""
    q = str(q or "").strip()[:60]
    if not q:
        return {"ok": False, "error": "给个关键词"}
    rows = db.execute("SELECT id,name FROM devices WHERE owner=? AND frozen=0"
                      " ORDER BY last_seen DESC", (owner,)).fetchall()
    devs = [(i, n) for i, n in rows
            if app["links"].get(i) is not None and not app["links"].get(i).closed][:limit]
    # remote=0：只要这台机器的本机结果。不带的话 host 收到请求又会去问中继
    # 汇总别的机器——A 问中继、中继问 A……直到超时。
    path = "/api/search?remote=0&q=" + urllib.parse.quote(q)
    res = await asyncio.gather(*[_link_api(app, d, path) for d, _ in devs])
    machines = []
    for (dev, name), r in zip(devs, res):
        items = [dict(it) for it in ((r or {}).get("items") or []) if isinstance(it, dict)]
        for it in items:
            it["machine"] = name or dev
            it["dev"] = dev
        machines.append({"dev": dev, "name": name or dev, "online": True, "items": items})
    return {"ok": True, "q": q, "machines": machines}


async def _pick_home(app, uid):
    """登录后访问 / 的默认去向：
    1) 任一在线开发机上有运行中的会话 → 直进最近活跃的那个（/ext/<sid>）
    2) 名下只有一台开发机且在线 → 直开它的会话列表（/d）
    3) 其余（没设备/多台/唯一一台离线）→ None，出开发机列表页"""
    db = _db(app)
    rows = db.execute("SELECT id FROM devices WHERE owner=? ORDER BY last_seen DESC",
                      (uid,)).fetchall()
    online = [r[0] for r in rows
              if not ((app["links"].get(r[0]) is None) or app["links"].get(r[0]).closed)]
    if not online:
        return None
    results = await asyncio.gather(*[_link_sessions(app, d) for d in online[:4]])
    best = None                      # (last, dev, sid)
    for dev, sessions in zip(online[:4], results):
        for s in sessions:
            if (s.get("state") or "") in ("", "idle", "exited"):
                continue             # 只认运行中的
            last = s.get("last") or 0
            if best is None or last > best[0]:
                best = (last, dev, s.get("sid") or "")
    if best and best[2]:
        return best[1], "/ext/" + best[2]
    if len(rows) == 1:
        return online[0], "/d"
    return None


async def _tunnel_stream(request, ws, dev):
    """流式隧道（SSE 推送等长连）：req 帧 st:1 → host 头帧/chunk*/终帧 →
    浏览器 StreamResponse，边收边写。finally 必发 cancel（host 侧幂等清理）。"""
    rid = request.app["rid"] = request.app.get("rid", 0) + 1
    body = await request.read()
    tunnel_add(len(body), 0, 1)
    _out_bytes = [0]
    p = request.rel_url.raw_path_qs
    if request.path == "/d" and p.startswith("/d"):
        p = "/" + p[2:]    # 与整包隧道同款入口改写
    frame = {"t": "req", "id": rid, "m": request.method, "p": p, "st": 1,
             "headers": _fwd_headers(request),
             "body": base64.b64encode(body).decode() if body else ""}
    frame["headers"]["x-cb-stream-id"] = str(rid)   # host 侧 cancel 的路由键
    st = {"q": asyncio.Queue(maxsize=STREAM_QMAX), "dev": dev,
          "key": _client_key(request), "kind": _client_kind(request),
          "first": asyncio.get_event_loop().create_future(),
          "end": None, "fallback": None, "over": False, "err": None, "done": False}
    request.app["streams"][rid] = st
    resp = None
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json(frame)
        try:
            hdr = await asyncio.wait_for(st["first"], REQ_TIMEOUT)
        except asyncio.TimeoutError:
            return web.json_response({"error": "开发机响应超时"}, status=504)
        fb = st.get("fallback")
        if fb is not None:   # 旧 host / 错误响应：整包兜底，不流式
            _fb_body = base64.b64decode(fb.get("body") or "")
            tunnel_add(0, len(_fb_body))      # 兜底整包同样算隧道字节
            _out_bytes[0] = len(_fb_body)
            out = web.Response(status=int(fb.get("code") or 502), body=_fb_body)
            out.headers["Content-Type"] = fb.get("ct") or "application/octet-stream"
            return out
        resp = web.StreamResponse(status=int(hdr.get("code") or 502))
        resp.headers["Content-Type"] = hdr.get("ct") or "text/event-stream"
        for k, v in (hdr.get("headers") or {}).items():
            if k.lower() not in HOP_BY_HOP | {"content-type", "set-cookie"}:
                resp.headers[k] = v
        # 中继边缘压缩：VPS 出方向带宽是真瓶颈（1 Mbps），而隧道内部强制 identity
        # 拿到的是原始字节。这里自己按块 flush 的 gzip 流——aiohttp 自带的
        # enable_compression 会攒到结束才吐（实测 1.6s 才到），实时终端不能用。
        z = None
        if "gzip" in (request.headers.get("Accept-Encoding") or "").lower() \
                and not resp.headers.get("Content-Encoding"):
            z = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
            resp.headers["Content-Encoding"] = "gzip"
        await resp.prepare(request)
        while True:
            tr = request.transport
            if tr is None or tr.is_closing():   # 浏览器断开：write() 不一定报错，必须主动查
                st["err"] = "浏览器断开(transport)"
                break
            try:
                item = await asyncio.wait_for(st["q"].get(), STREAM_IDLE)
            except asyncio.TimeoutError:
                st["err"] = "流空闲超时"
                break
            if item is None:   # 终帧哨兵
                end = st.get("end")
                if end and end.get("err"):
                    st["err"] = str(end["err"])[:200]
                break
            if st.get("over"):
                st["err"] = "下游消费过慢"
                break
            _presence_touch(request.app, dev, st["key"], st["kind"])   # 流还有心跳 = 人还在，活跃窗口一直续
            try:
                raw = base64.b64decode(item.get("b64") or "")
                tunnel_add(0, len(raw))
                _out_bytes[0] += len(raw)
                if z is not None:
                    raw = z.compress(raw) + z.flush(zlib.Z_SYNC_FLUSH)
                await resp.write(raw)
            except (ConnectionResetError, OSError, asyncio.CancelledError):
                st["err"] = "浏览器断开"
                break
        if z is not None:
            try:
                await resp.write(z.flush())
            except Exception:
                pass
        print("[relay] stream #%s end: %s" % (rid, st.get("err") or "正常"), flush=True)
        return resp
    finally:
        try:
            _u = request.get("_cb_uid")
            if _u:
                usage_bytes_add(_db(request.app), _u, int(time.time() // 60), "relay",
                                len(body), _out_bytes[0], 1,
                                cid=request.get("_cb_cid") or "",
                                dev=request.get("_cb_dev") or "")
                path_bytes_add(request.path, _u, request.get("_cb_cid") or "",
                               len(body) + _out_bytes[0], 1)
        except Exception:
            pass
        st["done"] = True
        request.app["streams"].pop(rid, None)
        if ws is not None and not ws.closed:
            try:
                async with getattr(ws, "_cb_lock", asyncio.Lock()):
                    await ws.send_json({"t": "cancel", "id": rid})
            except Exception:
                pass
        if resp is not None:
            try:
                await resp.write_eof()
            except Exception:
                pass


def _apk_slot(product):
    """产品线的 APK 槽（bin, meta 路径）：codeboss 沿用现名（存量零迁移），
    其它产品线同目录分文件。清洗走 valid_product——非法值绝不新建槽。"""
    if branding.valid_product(product) == branding.PRODUCT_AGENTBOSS:
        d = os.path.dirname(APK_FILE)
        return (os.path.join(d, "agentboss-apk.bin"), os.path.join(d, "agentboss-apk.json"))
    return (APK_FILE, APK_META)


def _apk_meta(product=branding.PRODUCT_DEFAULT):
    try:
        with open(_apk_slot(product)[1]) as f:
            return json.load(f)
    except Exception:
        return {}


def _apk_file_name(product=branding.PRODUCT_DEFAULT):
    """下载文件名带版本号：codeboss-terminal-0.10.35.apk（版本认不出就退回老名字）"""
    ver = str(_apk_meta(product).get("ver") or "").strip()
    return ("%s-terminal-%s.apk" % (product, ver)) if ver else ("%s-terminal.apk" % product)


def _apk_end_meta(d, up):
    """尾帧 + 攒块帧合成存盘元数据：ver/code/product 以尾帧为准，
    pkg/sha256 一起带过去（_apk_store 靠它认包，丢了校验就是死代码）。"""
    meta0 = dict(up.get("meta") or {})
    if d.get("code"):   # ver/code 约定在尾帧；块帧带的是历史冗余
        meta0 = {"ver": str(d.get("ver") or "?"), "code": int(d.get("code")),
                 "product": d.get("product")}   # 单 host 双产物：线随包走
    for k in ("pkg", "sha256"):
        if d.get(k):
            meta0[k] = str(d[k])
    return meta0


def _apk_store(app, dev, blob, meta0):
    """host 推上来的客户端 APK 落盘：槽优先按包尾帧声明的产品线（单 host 双产物，
    一台开发机喂两条线的槽）；尾帧没带/带得不合法（老 host）回落设备产品线
    （devices.product，enroll 时写入）。meta0 是版本元数据（ver/code 约定在尾帧）。"""
    db = _db(app)
    raw = (meta0 or {}).get("product")
    if raw in branding.PRODUCTS:
        product = raw
    else:
        row = db.execute("SELECT product FROM devices WHERE id=?", (dev,)).fetchone()
        product = branding.valid_product(row[0] if row else None)
    # 自证：host 说这条帧是哪个产品的包，得和包里真包名对得上。2026-11-18
    # codeboss 槽被推进 agentboss 的包，全体工头用户下到的是老板 App——帧里
    # product 全凭 host 自觉，错一次就是一整天。老 host 不带 pkg/sha，放行。
    binp, metap = _apk_slot(product)
    pkg = str((meta0 or {}).get("pkg") or "")
    if pkg and pkg != "com.%s.terminal" % product:
        print("[relay] apk rejected product=%s pkg=%s dev=%s" % (product, pkg, dev), flush=True)
        raise ValueError("pkg=%s 不属于 %s 槽" % (pkg, product))
    sha = str((meta0 or {}).get("sha256") or "").lower()
    if sha and sha != hashlib.sha256(blob).hexdigest():
        print("[relay] apk rejected product=%s sha=%s dev=%s" % (product, sha[:12], dev), flush=True)
        raise ValueError("sha256 与正文不符")
    prev = _apk_meta(product)
    _apk_archive(prev)          # 旧的那一版先留档（此刻槽内还是旧的）
    tmp = binp + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, binp)   # 原子替换：下载半截不会发出去
    meta = dict(meta0 or {})
    meta.update(size=len(blob), ts=time.time(), dev=dev, product=product)
    with open(metap, "w") as f:
        json.dump(meta, f)
    _apk_archive(meta, blob=blob)
    _apk_baseline(product, meta, prev)
    print("[relay] apk stored product=%s code=%s ver=%s size=%d"
          % (product, meta.get("code"), meta.get("ver"), len(blob)), flush=True)
    _oss_mirror(app, "%s.apk" % product, blob,
                "application/vnd.android.package-archive")


def _slot_health():
    """两条线的 APK 槽体检（只读）：meta 说的 size/sha 和盘上文件对不对得上。
    2026-11-18 串包那次，meta 写着 codeboss、盘上却是老板的包——版本号说得对
    不等于发出去的包对，后台要有办法一眼看穿。"""
    out = []
    for p in branding.PRODUCTS:
        m = _apk_meta(p)
        binp = _apk_slot(p)[0]
        row = {"product": p, "code": int(m.get("code") or 0),
               "ver": str(m.get("ver") or ""), "size": int(m.get("size") or 0),
               "ts": int(m.get("ts") or 0), "dev": str(m.get("dev") or ""),
               "pkg": str(m.get("pkg") or ""), "file_ok": None}
        try:
            raw = open(binp, "rb").read()
        except Exception:
            row["on_disk"] = 0
            out.append(row)
            continue
        row["on_disk"] = len(raw)
        sha = hashlib.sha256(raw).hexdigest()
        row["sha256"] = sha
        if m.get("sha256"):
            row["file_ok"] = sha == str(m["sha256"]).lower()
        elif row["size"]:
            row["file_ok"] = len(raw) == row["size"]   # 老 meta 没 sha：只能比大小
        out.append(row)
    return out


async def r_admin_slots(request):
    if not await _adm_auth(request):
        return _adm_unauth()
    return web.json_response({"ok": True, "slots": _slot_health()})


def _ios_meta():
    try:
        with open(IOS_META) as f:
            return json.load(f)
    except Exception:
        return {}


def _tf_meta():
    """TestFlight 上当前对外的 build：{"code": int, "ver": str}。

    App 里的更新提示要比这个：公开用户只能从 TestFlight 装，开发签名那条路
    只对已登记设备有效。没登记文件（或文件坏了）就空 → 调用方退回开发签名版本。
    """
    try:
        with open(TF_META_FILE, encoding="utf-8") as f:
            d = json.load(f)
        code = int(d.get("code") or 0)
        if code > 0:
            return {"code": code, "ver": str(d.get("ver") or "")}
    except Exception:
        pass
    return {"code": 0, "ver": ""}


def _tf_save(code, ver=""):
    """登记 TestFlight 当前对外的 build（后台填一下就生效，不用上服务器改文件）。"""
    tmp = TF_META_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"code": int(code), "ver": str(ver or ""),
                   "ts": int(time.time())}, f, ensure_ascii=False)
    os.replace(tmp, TF_META_FILE)
    return _tf_meta()


def _ios_file_name():
    ver = str(_ios_meta().get("ver") or "").strip()
    return "codeboss-ios-%s.ipa" % ver if ver else "codeboss-ios.ipa"


def _relay_base(request):
    """中继自己的对外地址（反代/隧道后面 scheme 会丢，优先用转发头）。"""
    return "%s://%s" % (
        (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
        or request.scheme,
        request.headers.get("Host") or request.host)


def _hostpkg_meta():
    try:
        with open(HOSTPKG_META) as f:
            return json.load(f)
    except Exception:
        return {}


def _vt(s):
    """版本号比较：0.8.16 > 0.8.9（按数字段比，不按字符串）。认不出就当 0。"""
    return tuple(int(x) for x in re.findall(r"\d+", str(s or ""))[:3]) or (0,)


def _canary_cfg():
    try:
        with open(CANARY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _canary_save(d):
    tmp = CANARY_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CANARY_FILE)


def _canary_gray(c):
    """灰度常驻名单：进过一次灰度的对象，以后每次发版都自动拿最新——
    不用每条新规则再把人拉一遍（2026-11-20：老规则钉旧版把开发机卡住）。"""
    g = c.get("gray")
    if not isinstance(g, dict):
        # 老配置还没有灰度名单：把现有规则里的人一次性收进来，
        # 免得升级后第一轮发版又要重勾（macmini 这类）。
        g = {"devs": [], "clients": [], "users": []}
        for r in (c.get("rules") or []):
            if not isinstance(r, dict):
                continue
            for k in ("devs", "clients", "users"):
                g[k] += [x for x in (r.get(k) or []) if x not in g[k]]
        c["gray"] = g
    for k in ("devs", "clients", "users"):
        if not isinstance(g.get(k), list):
            g[k] = []
    return g


def _gray_norm(kind, ids):
    if kind == "users":
        return [int(x) for x in ids if str(x).strip().isdigit()]
    return [str(x).strip()[:64] for x in ids if str(x).strip()]


def _canary_gray_add(c, kind, ids):
    cur = _canary_gray(c)[kind]
    for x in _gray_norm(kind, ids):
        if x not in cur:
            cur.append(x)


def _canary_gray_del(c, kind, ids):
    g = _canary_gray(c)
    drop = _gray_norm(kind, ids)
    g[kind] = [x for x in g[kind] if x not in drop]


def _pkg_path(ver):
    return os.path.join(HOSTPKG_DIR, str(ver) + ".tar.gz")


def _pkg_meta(ver):
    try:
        with open(os.path.join(HOSTPKG_DIR, str(ver) + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pkg_vers():
    """中继上留着的 host 包版本（新→旧）。"""
    _pkg_seed()
    out = []
    try:
        for n in os.listdir(HOSTPKG_DIR):
            if n.endswith(".json"):
                m = _pkg_meta(n[:-5])
                if m.get("ver"):
                    out.append((int(m.get("ts") or 0), str(m["ver"])))
    except OSError:
        pass
    return [v for _t, v in sorted(out, reverse=True)]


def _pkg_seed():
    """老部署只有一个固定名字的包：分版本功能第一次用到时，把它登记成对应版本，
    这样「全员基线」指向的那版也拿得到包（不然非灰度对象会被顶到最新版）。"""
    m = _hostpkg_meta()
    v = str(m.get("ver") or "")
    if not v or not os.path.exists(HOSTPKG_FILE):
        return
    if os.path.exists(_pkg_path(v)):
        return
    try:
        os.makedirs(HOSTPKG_DIR, exist_ok=True)
        shutil.copyfile(HOSTPKG_FILE, _pkg_path(v))
        with open(os.path.join(HOSTPKG_DIR, v + ".json"), "w", encoding="utf-8") as f:
            json.dump(m, f)
    except OSError:
        pass


def _pkg_save(data, meta):
    """按版本留一份包（近 6 个）：分版本推送、回滚都要按版本取包。"""
    ver = str(meta.get("ver") or "")
    if not re.match(r"^[0-9A-Za-z._-]{1,40}$", ver):
        raise ValueError("版本号含不能做文件名的字符")
    os.makedirs(HOSTPKG_DIR, exist_ok=True)
    tmp = _pkg_path(ver) + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, _pkg_path(ver))
    with open(os.path.join(HOSTPKG_DIR, ver + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    for v in _pkg_vers()[6:]:
        for p in (_pkg_path(v), os.path.join(HOSTPKG_DIR, v + ".json")):
            try:
                os.remove(p)
            except OSError:
                pass


def _apk_code_meta(product, code):
    p = branding.valid_product(product)
    try:
        with open(os.path.join(APK_DIR, "%s-%d.json" % (p, int(code))), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    if p == branding.PRODUCT_CODEBOSS:   # 老归档是裸 code 命名，只有 codeboss 有存量
        try:
            with open(os.path.join(APK_DIR, "%d.json" % int(code)), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _apk_code_path(product, code):
    """某产品线某 code 的归档文件路径。codeboss 存量有裸 <code>.apk 老命名：
    存在性检查只认新名的话，灰度/回退到升级前的老版本会静默失效。"""
    p = os.path.join(APK_DIR, "%s-%s.apk" % (product, code))
    if product == branding.PRODUCT_CODEBOSS and not os.path.exists(p):
        old = os.path.join(APK_DIR, "%s.apk" % code)
        if os.path.exists(old):
            return old
    return p


def _apk_archive(meta, blob=None, src=None):
    """APK 按 code 留一份（每产品线近 4 个，分版本推送/回退要用）。
    blob 给了就写 blob；没给就从 src（默认该产品当前槽文件）复制。
    老命名（裸 <code>.apk/.json）只有 codeboss 有存量：清理时算进 codeboss 的名额。"""
    code = int((meta or {}).get("code") or 0)
    if code <= 0:
        return
    product = branding.valid_product((meta or {}).get("product"))
    try:
        os.makedirs(APK_DIR, exist_ok=True)
        p = os.path.join(APK_DIR, "%s-%d.apk" % (product, code))
        if os.path.exists(p) and int(os.path.getsize(p) or 0) > 10240:
            return
        if blob is None:
            shutil.copyfile(src or _apk_slot(product)[0], p)
        else:
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, p)
        with open(os.path.join(APK_DIR, "%s-%d.json" % (product, code)), "w",
                  encoding="utf-8") as f:
            json.dump({"code": code, "ver": str((meta or {}).get("ver") or ""),
                       "product": product, "ts": int(time.time())}, f)
    except OSError as e:
        print("[relay] apk 留存失败: %s" % str(e)[:100], flush=True)
        return
    try:
        per = {}
        for n in os.listdir(APK_DIR):
            if not n.endswith(".json"):
                continue
            base = n[:-5]
            if "-" in base:
                pr, c = base.rsplit("-", 1)
            else:                       # 老命名裸 code：codeboss 存量
                pr, c = branding.PRODUCT_DEFAULT, base
            if c.isdigit():
                per.setdefault(pr, []).append(int(c))
        for pr, codes in per.items():
            for c in sorted(codes)[:-4]:
                for p in ("%s-%d.apk" % (pr, c), "%s-%d.json" % (pr, c),
                          "%d.apk" % c, "%d.json" % c):
                    try:
                        os.remove(os.path.join(APK_DIR, p))
                    except OSError:
                        pass
    except OSError:
        pass


def _apk_baseline(product, meta, prev):
    """新 code 第一次出现时，把「大家现在手里的那版」设成该产品线的灰度基线——
    不发这一下，新版 APK 会立刻推给所有人，灰度就挡不住了。
    codeboss 沿用全员基线（default）不动；其它产品线写 default_by_product 分桶。"""
    code = int((meta or {}).get("code") or 0)
    old = int((prev or {}).get("code") or 0)
    if code <= 0 or not old or old == code:
        return
    product = branding.valid_product(product)
    c = _canary_cfg()
    if product == branding.PRODUCT_CODEBOSS:
        d = c.get("default")
        if not isinstance(d, dict):
            d = {}
            c["default"] = d
    else:
        byp = c.get("default_by_product")
        if not isinstance(byp, dict):
            byp = {}
            c["default_by_product"] = byp
        d = byp.get(product)
        if not isinstance(d, dict):
            d = {}
            byp[product] = d
    if not int(d.get("apk") or 0):
        d["apk"] = old
        _canary_save(c)


def _canary_who(app, request):
    """这个请求是谁：开发机 id、终端 cid、所属账号 uid。
    uid 只认库里对得上的（设备→owner / 终端→uid），不信任外面随手传的 uid。"""
    dev = (request.query.get("dev") or request.headers.get("X-CB-Dev") or "").strip()[:32]
    cid = (request.query.get("cid") or request.cookies.get("cb_cid") or "").strip()[:64]
    uid = 0
    try:
        db = _db(app)
        if dev:
            r = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
            uid = int(r[0] or 0) if r else 0
        if not uid and cid:
            r = db.execute("SELECT uid FROM clients WHERE cid=? ORDER BY last DESC LIMIT 1",
                           (cid,)).fetchone()
            uid = int(r[0] or 0) if r else 0
    except Exception:
        pass
    return dev, cid, uid


def _canary_promote(c, ver, apk):
    """全量发布：清灰度规则，同时放开各产品线自己的基线（default_by_product）——
    不放开的话，agentboss 这类有自有基线的线会被旧版一直钉住、发版静默失效。"""
    c["default"] = {"ver": ver, "apk": int(apk or 0)}
    c.pop("default_by_product", None)
    c["rules"] = []


def _canary_release_product(c, product):
    """单条产品线全量发布：只撤掉这条线的 APK 基线钉子（0=跟最新），
    别的产品线和灰度规则都不动——codeboss 和新线的包里 code 各算各的，不能一起 promote。
    codeboss 的基线在 default.apk；其它线在 default_by_product[product]。"""
    if product not in branding.PRODUCTS:
        return
    if product == branding.PRODUCT_CODEBOSS:
        d = c.get("default")
        if isinstance(d, dict):
            d["apk"] = 0
        return
    byp = c.get("default_by_product")
    if isinstance(byp, dict):
        byp.pop(product, None)
        if not byp:
            c.pop("default_by_product", None)


def _canary_pick(app, request, product="codeboss"):
    """这次请求该拿哪个版本：命中的规则里取版本最高的那条；没命中看全员基线；
    基线也没设 = 最新包（等于全量）。apk=该拿哪个客户端 code（0=最新）。
    产品线各自有基线（default_by_product），没有自有基线的产品线回落全局 default。"""
    c = _canary_cfg()
    bp = c.get("default_by_product") if isinstance(c.get("default_by_product"), dict) else {}
    d = bp.get(product) if isinstance(bp.get(product), dict) else None
    if not isinstance(d, dict):
        d = c.get("default") if isinstance(c.get("default"), dict) else {}
    dev, cid, uid = _canary_who(app, request)
    hit = {}
    for r in (c.get("rules") or []):
        if not isinstance(r, dict):
            continue
        us = set()
        for x in (r.get("users") or []):
            try:
                us.add(int(x))
            except Exception:
                pass
        if not ((dev and dev in (r.get("devs") or []))
                or (cid and cid in (r.get("clients") or []))
                or (uid and uid in us)):
            continue
        if not hit or _vt(r.get("ver")) > _vt(hit.get("ver")):
            hit = r
    g = _canary_gray(c)
    gu = set()
    for x in (g.get("users") or []):
        try:
            gu.add(int(x))
        except Exception:
            pass
    if ((dev and dev in (g.get("devs") or []))
            or (cid and cid in (g.get("clients") or []))
            or (uid and uid in gu)):
        newest = (_pkg_vers() or [""])[0]
        if newest:
            # 灰度常驻 = 永远跟最新包：规则里钉的旧版本不再把人卡在旧版上。
            return {"ver": newest, "apk": int(hit.get("apk") or d.get("apk") or 0),
                    "dev": dev, "cid": cid, "uid": uid, "rule": "gray"}
    return {"ver": str(hit.get("ver") or d.get("ver") or ""),
            "apk": int(hit.get("apk") or d.get("apk") or 0),
            "dev": dev, "cid": cid, "uid": uid, "rule": str(hit.get("id") or "")}


async def h_host_ver(request):
    """Mac 客户端检查 host 更新（GET /api/host_ver，免鉴权）。

    灰度：host 自己带 ?dev=<设备 ID>（终端/用户由中继按库里的归属算），
    命中规则 → 规则上的版本；没命中 → 全员基线；都没设 → 最新包（老行为）。"""
    newest = _hostpkg_meta()
    if not (newest.get("ver") and os.path.exists(HOSTPKG_FILE)):
        return web.json_response({"ok": False, "error": "中继还没有 host 包"}, status=404)
    ver = newest["ver"]
    want = _canary_pick(request.app, request)["ver"]
    if want:
        ver = want
    m, url = newest, "/codeboss-host.tar.gz"
    if ver != newest["ver"] and os.path.exists(_pkg_path(ver)):
        m, url = _pkg_meta(ver) or newest, "/codeboss-host-%s.tar.gz" % ver
    elif ver != newest["ver"]:
        ver = newest["ver"]      # 规则指向的版本没包了（被清理/写错）→ 退回最新
    return web.json_response({"ok": True, "ver": str(ver),
                              "size": int(m.get("size") or 0),
                              "sha256": str(m.get("sha256") or ""),
                              "sig": str(m.get("sig") or ""),
                              "ts": int(m.get("ts") or 0),
                              "url": url})


async def h_host_pkg_ver(request):
    """按版本下载 host 包（/codeboss-host-<ver>.tar.gz）：灰度/回滚用。"""
    ver = request.match_info.get("ver") or ""
    if not re.match(r"^[0-9A-Za-z._-]{1,40}$", ver) or not os.path.exists(_pkg_path(ver)):
        return web.json_response({"ok": False, "error": "没有这个版本"}, status=404)
    fn = "codeboss-host-%s.tar.gz" % ver
    r = await _dl_or_local(request.app, request, _pkg_path(ver), fn, "application/gzip", fn)
    return r or web.json_response({"ok": False, "error": "没有这个版本"}, status=404)


async def h_host_pkg(request):
    """host 源码包下载（免鉴权；包内无 token/会话数据）。"""
    r = await _dl_or_local(request.app, request, HOSTPKG_FILE, "codeboss-host.tar.gz",
                           "application/gzip", "codeboss-host.tar.gz")
    return r or web.json_response({"ok": False, "error": "中继还没有 host 包"}, status=404)


async def h_linux_pkg(request):
    """Linux 发行包（自带 x86_64 Python）：/codeboss-linux.tar.gz，免鉴权。"""
    r = await _dl_or_local(request.app, request, LINUXPKG_FILE, "codeboss-linux.tar.gz",
                           "application/gzip", "codeboss-linux.tar.gz")
    return r or web.json_response({"ok": False, "error": "中继还没有 Linux 包"}, status=404)


async def h_win_exe(request):
    """Windows 安装包直链（官网「下载 Windows 版」）。"""
    r = await _dl_or_local(request.app, request, EXE_FILE, "BossHost-Setup.exe",
                           "application/octet-stream", "BossHost-Setup.exe", 100000)
    return r or web.json_response({"ok": False, "error": "中继还没有 Windows 包"}, status=404)


async def r_admin_hostpkg(request):
    """上架/替换 host 包（Bearer RELAY_ADMIN_TOKEN）。人工控制发布时机。
    只收 tar.gz，包内必须有 server.py 与 VERSION；校验通过才原子替换。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权上架"}, status=401)
    data = b""
    if (request.content_type or "").startswith("multipart/form-data"):
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name in ("f", "pkg", "file"):
                data = await part.read(decode=False)
                break
    else:
        data = await request.read()
    if len(data) < 1024 or len(data) > 32 * 1024 * 1024:
        return web.json_response({"ok": False, "error": "包大小不合规"}, status=400)
    traffic_add("ops", len(data), 0, 1)
    if data[:2] != b"\x1f\x8b":
        return web.json_response({"ok": False, "error": "不是 tar.gz"}, status=400)
    import io
    import tarfile
    ver = ""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        names = tf.getnames()
        if not any(os.path.basename(n) == "server.py" for n in names):
            raise ValueError("包内没有 server.py")
        for n in names:
            if os.path.basename(n) == "VERSION":
                f = tf.extractfile(n)
                if f:
                    ver = f.read().decode("utf-8", "replace").strip()[:40]
                break
        if not ver:
            raise ValueError("包内没有 VERSION（版本号是唯一事实源）")
    except Exception as e:
        return web.json_response({"ok": False, "error": "包不合法：%s" % str(e)[:80]}, status=400)
    old = str(_hostpkg_meta().get("ver") or "")
    if not re.match(r"^[0-9A-Za-z._-]{1,40}$", ver):
        return web.json_response({"ok": False, "error": "VERSION 里有不能做文件名的字符"},
                                 status=400)
    _pkg_seed()      # 老版本先登记一份：基线指着它时要下得到包
    # 维护者用私钥对 "<ver>|<sha256>" 签名，host 内置公钥验签——中继被拿下也换不了包
    sig = (request.headers.get("x-pkg-sig") or "").strip().lower()
    if sig and not re.match(r"^[0-9a-f]{128}$", sig):
        return web.json_response({"ok": False, "error": "X-Pkg-Sig 不是 128 位 hex"}, status=400)
    meta = {"ver": ver, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "sig": sig,
            "ts": int(time.time()), "dev": (request.headers.get("x-dev") or "")[:32]}
    tmp = HOSTPKG_FILE + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, HOSTPKG_FILE)
    tmeta = HOSTPKG_META + ".part"
    with open(tmeta, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmeta, HOSTPKG_META)
    _pkg_save(data, meta)
    # 第一次上新版本：把大家现在跑的版本设成全员基线（不设的话新版立刻推给所有人，
    # 来不及灰度）。之后只有名单里的对象能拿到新版，直到后台点「全量发布」。
    c = _canary_cfg()
    d = c.get("default")
    if not isinstance(d, dict):
        d = {}
        c["default"] = d
    if not d.get("ver") and old and _vt(old) < _vt(ver):
        d["ver"] = old
        _canary_save(c)
    print("[relay] host 包已上架 ver=%s size=%d 全员基线=%s"
          % (ver, len(data), d.get("ver") or "（=最新，未灰度）"), flush=True)
    _oss_mirror(request.app, "codeboss-host.tar.gz", data, "application/gzip",
                skip=_oss_done(request))
    return web.json_response(dict(ok=True, **meta))


async def r_admin_canary(request):
    """后台「版本」页：版本推送管理（灰度名单 + 全量发布）。

    GET  → {ok, default, rules, pkgs, apk}：全员基线、灰度规则、留着的包版本、最新 APK
    POST → action 见下。写操作是发布链路，照旧要管理员短信码（X-Adm-Code）。
    """
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    c = _canary_cfg()
    if not isinstance(c.get("default"), dict):
        c["default"] = {}
    rules = c.get("rules")
    if not isinstance(rules, list):
        rules = []
        c["rules"] = rules
    if request.method == "GET":
        return web.json_response({"ok": True, "default": c["default"],
                                  "rules": [r for r in rules if isinstance(r, dict)],
                                  "gray": _canary_gray(c),
                                  "pkgs": _pkg_vers(), "apk": _apk_meta(),
                                  "tf": _tf_meta(), "tf_url": TF_URL})
    if not await _adm_sensitive(request):
        return _adm_need_sms(db)
    try:
        d = await request.json()
    except Exception:
        d = {}
    act = str(d.get("action") or "")
    rid = str(d.get("rule") or "")

    def find():
        for r in rules:
            if isinstance(r, dict) and str(r.get("id")) == rid:
                return r
        return None

    def newest_ver():
        return (_pkg_vers() or [""])[0]

    if act == "new":
        ver = str(d.get("ver") or "").strip()[:40] or newest_ver()
        # 没指定 APK 就跟着「建规则时中继上的最新 code」走：host 和新客户端一起上时，
        # 灰度对象两样都拿到；之后 APK 再出新版也不动这条规则（推的是这一版）。
        rules.append({"id": "%d" % int(time.time() * 1000), "ver": ver,
                      "apk": int(d.get("apk") or (_apk_meta() or {}).get("code") or 0),
                      "devs": [], "clients": [], "users": []})
        msg = "已新建灰度规则 v%s（在开发机/终端/用户页勾选对象加入）" % (ver or "?")
    elif act in ("add", "del"):
        r = find()
        if r is None and act == "add" and not rid and rules:
            r = [x for x in rules if isinstance(x, dict)][-1]   # 没选规则=最后一条
        if r is None and act == "add":
            ver = str(d.get("ver") or "").strip()[:40] or newest_ver()
            r = {"id": "%d" % int(time.time() * 1000), "ver": ver,
                 "apk": int((_apk_meta() or {}).get("code") or 0),
                 "devs": [], "clients": [], "users": []}
            rules.append(r)
        if r is None:
            return web.json_response({"ok": False, "error": "规则不存在"})
        kind = str(d.get("kind") or "")
        if kind not in ("devs", "clients", "users"):
            return web.json_response({"ok": False, "error": "kind 不对"})
        ids = list(d.get("ids") or [])
        if kind == "users":
            ids = [int(x) for x in ids if str(x).strip().isdigit()]
        else:
            ids = [str(x).strip()[:64] for x in ids if str(x).strip()]
        cur = list(r.get(kind) or [])
        if act == "add":
            cur += [x for x in ids if x not in cur]
            _canary_gray_add(c, kind, ids)      # 进过一次就常驻，下次发版不用重选
            msg = "已加入灰度 v%s：%d 个对象（以后每次发版自动拿最新）" % (
                r.get("ver") or "?", len(ids))
        else:
            cur = [x for x in cur if x not in ids]
            _canary_gray_del(c, kind, ids)
            msg = "已移出灰度：%d 个对象" % len(ids)
        r[kind] = cur
    elif act in ("gray_add", "gray_del"):
        kind = str(d.get("kind") or "")
        if kind not in ("devs", "clients", "users"):
            return web.json_response({"ok": False, "error": "kind 不对"})
        ids = list(d.get("ids") or [])
        if act == "gray_add":
            _canary_gray_add(c, kind, ids)
            msg = "已加入灰度常驻：%d 个对象" % len(ids)
        else:
            _canary_gray_del(c, kind, ids)
            msg = "已移出灰度常驻：%d 个对象（回到全员基线）" % len(ids)
    elif act == "del_rule":
        n = len(rules)
        rules[:] = [r for r in rules
                    if not (isinstance(r, dict) and str(r.get("id")) == rid)]
        msg = "已删除规则" if len(rules) < n else "规则不存在"
    elif act == "promote":
        v = newest_ver()
        _canary_promote(c, v, (_apk_meta() or {}).get("code"))
        rules[:] = []
        msg = "已全量发布 v%s（灰度规则已清空）" % (v or "?")
    elif act == "rule_apk":
        r = find()
        if r is None:
            return web.json_response({"ok": False, "error": "规则不存在"})
        try:
            r["apk"] = int(d.get("apk") or 0)
        except Exception:
            return web.json_response({"ok": False, "error": "apk 要是数字"})
        msg = "灰度规则 v%s 的安卓 code 改成 %s" % (
            r.get("ver") or "?", r["apk"] or "最新")
    elif act == "release":
        prod = str(d.get("product") or "").strip()
        if prod not in branding.PRODUCTS:
            return web.json_response({"ok": False, "error": "产品线不对"})
        _canary_release_product(c, prod)
        msg = "已全量发布 %s 的安卓包（基线跟最新）" % branding.product_name(prod)["label"]
    elif act == "baseline":
        c["default"] = {"ver": str(d.get("ver") or "").strip()[:40],
                        "apk": int(d.get("apk") or 0)}
        msg = "全员基线已设为 v%s / 安卓 %s" % (
            c["default"]["ver"] or "最新", c["default"]["apk"] or "最新")
    elif act == "tf":
        try:
            code = int(d.get("code") or 0)
        except Exception:
            code = 0
        if code <= 0:
            return web.json_response({"ok": False, "error": "build 号要是正整数"})
        tf = _tf_save(code, str(d.get("ver") or "").strip()[:20])
        msg = "TestFlight 已登记 build %d%s" % (
            tf["code"], "（%s）" % tf["ver"] if tf["ver"] else "")
        print("[admin] iOS：%s" % msg, flush=True)
        return web.json_response({"ok": True, "msg": msg, "tf": tf,
                                  "default": c["default"],
                                  "rules": [r for r in rules if isinstance(r, dict)]})
    else:
        return web.json_response({"ok": False, "error": "未知操作"})
    _canary_save(c)
    print("[admin] 版本推送：%s" % msg, flush=True)
    return web.json_response({"ok": True, "msg": msg, "default": c["default"],
                              "rules": [r for r in rules if isinstance(r, dict)],
                              "gray": _canary_gray(c)})


async def r_admin_analytics(request):
    """后台「分析」页：功能使用情况（feat_day 埋点按天聚合）。

    GET ?days=30&prod=codeboss|agentboss（空/不合法=全部产品线）
    返回 today / daily（事件·活跃账号·设备）/ fev（功能排行）/ byprod / byver / byplat。
    活跃账号只算登录用户（uid>0），匿名设备算设备数不算账号——报表别把游客当账号。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        days = int(request.query.get("days") or 30)
    except Exception:
        days = 30
    days = max(1, min(days, 400))
    prod = (request.query.get("prod") or "").strip().lower()
    if prod not in branding.PRODUCTS:
        prod = ""
    day0 = time.strftime("%Y-%m-%d",
                         time.localtime(time.time() - (days - 1) * 86400))
    where, args = "day>=?", [day0]
    if prod:
        where += " AND prod=?"
        args.append(prod)

    def q(sql, a, cap):
        return [list(r) for r in db.execute(sql + " LIMIT %d" % cap,
                                            a).fetchall()]

    acc = "COUNT(DISTINCT CASE WHEN uid>0 THEN uid END)"
    daily = q("SELECT day, SUM(n), " + acc + ", COUNT(DISTINCT cid) "
              "FROM feat_day WHERE " + where +
              " GROUP BY day ORDER BY day", args, 400)
    fev = q("SELECT ev, SUM(n), " + acc + ", COUNT(DISTINCT cid) "
            "FROM feat_day WHERE " + where +
            " GROUP BY ev ORDER BY SUM(n) DESC", args, 30)
    byprod = q("SELECT prod, SUM(n), COUNT(DISTINCT cid) FROM feat_day WHERE "
               + where + " GROUP BY prod ORDER BY SUM(n) DESC", args, 5)
    byver = q("SELECT ver, SUM(n) FROM feat_day WHERE " + where +
              " AND ver<>'' GROUP BY ver ORDER BY SUM(n) DESC", args, 8)
    byplat = q("SELECT plat, SUM(n), COUNT(DISTINCT cid) FROM feat_day WHERE "
               + where + " GROUP BY plat ORDER BY SUM(n) DESC", args, 5)
    # 页面维度：访问次数来自 ev=visit，停留秒数来自 ev=dwell，平均停留 = 两者相除。
    pages = q("SELECT pg, SUM(CASE WHEN ev='visit' THEN n ELSE 0 END), "
              "SUM(CASE WHEN ev='dwell' THEN n ELSE 0 END) "
              "FROM feat_day WHERE " + where + " AND ev IN ('visit','dwell') "
              "AND pg<>'' GROUP BY pg ORDER BY 3 DESC", args, 30)
    # 明细细分：哪个页面上的哪个事件/哪一项被点了多少次（访问/停留单独给，不混进来）
    evdet = q("SELECT ev, pg, k, SUM(n), COUNT(DISTINCT cid) FROM feat_day "
              "WHERE " + where + " AND ev NOT IN ('visit','dwell') "
              "GROUP BY ev, pg, k ORDER BY SUM(n) DESC", args, 120)
    tday = time.strftime("%Y-%m-%d")
    t = db.execute("SELECT COALESCE(SUM(n),0), " + acc + ", COUNT(DISTINCT cid) "
                   "FROM feat_day WHERE " + where + " AND day=?",
                   args + [tday]).fetchone()
    return web.json_response({
        "ok": True, "days": days, "prod": prod, "day0": day0,
        "today": {"day": tday, "n": int(t[0] or 0), "users": int(t[1] or 0),
                  "devs": int(t[2] or 0)},
        "daily": [{"d": r[0], "n": int(r[1] or 0), "users": int(r[2] or 0),
                   "devs": int(r[3] or 0)} for r in daily],
        "fev": [{"ev": r[0], "n": int(r[1] or 0), "users": int(r[2] or 0),
                 "devs": int(r[3] or 0)} for r in fev],
        "byprod": [{"p": r[0] or "-", "n": int(r[1] or 0),
                    "devs": int(r[2] or 0)} for r in byprod],
        "byver": [{"v": r[0], "n": int(r[1] or 0)} for r in byver],
        "byplat": [{"p": r[0] or "-", "n": int(r[1] or 0),
                    "devs": int(r[2] or 0)} for r in byplat],
        "pages": [{"pg": r[0], "visits": int(r[1] or 0),
                   "secs": int(r[2] or 0),
                   "avg": int(round((r[2] or 0) / r[1])) if r[1] else 0}
                  for r in pages],
        "evdet": [{"ev": r[0], "pg": r[1] or "", "k": r[2] or "",
                   "n": int(r[3] or 0), "devs": int(r[4] or 0)} for r in evdet]})


def _mac_meta():
    try:
        with open(MAC_META) as f:
            return json.load(f)
    except Exception:
        return {}


async def h_mac_ver(request):
    """Mac DMG 版本查询（GET /api/mac_ver，免鉴权）：App 检查新客户端、浏览器提示下载。"""
    m = _mac_meta()
    if m.get("ver") and os.path.exists(MAC_FILE):
        return web.json_response({"ok": True, "ver": str(m["ver"]),
                                  "size": int(m.get("size") or 0),
                                  "sha256": str(m.get("sha256") or ""),
                                  "ts": int(m.get("ts") or 0),
                                  "url": "/bosshost-mac.dmg"})
    return web.json_response({"ok": False, "error": "中继还没有 DMG"}, status=404)


# ---------- iOS 客户端更新通道（.ipa + itms-services） ----------
_PAGE_IOS_INSTALL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>安装 Codeboss</title>
<style>
  html,body{margin:0;background:#0b0e14;color:#e6edf3;
    font:16px/1.6 -apple-system,"PingFang SC","Noto Sans SC",sans-serif;
    -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
  .box{max-width:440px;margin:0 auto;padding:30px 18px;text-align:center}
  h1{font-size:20px;margin:0 0 6px}
  .v{color:#8b949e;font-size:13px;margin:0 0 22px}
  .big{display:block;padding:15px;border-radius:12px;border:0;background:#238636;color:#fff;
    font-size:17px;font-weight:600;text-decoration:none}
  .alt{display:block;margin:16px auto 0;padding:11px;border-radius:12px;border:1px solid #30363d;
    color:#8b949e;font-size:14px;text-decoration:none}
  .note{margin:20px 2px 0;color:#6e7681;font-size:12px;text-align:left;line-height:1.75}
</style></head><body>
<div class="box">
  <h1>安装 Codeboss</h1>
  <p class="v">iOS 客户端 __VER__（build __CODE__）</p>
__BODY__
</div></body></html>"""

_IPA_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>items</key><array><dict>
<key>assets</key><array><dict>
<key>kind</key><string>software-package</string>
<key>url</key><string>__IPA__</string>
</dict></array>
<key>metadata</key><dict>
<key>bundle-identifier</key><string>__BID__</string>
<key>bundle-version</key><string>__CODE__</string>
<key>kind</key><string>software</string>
<key>title</key><string>Codeboss</string>
</dict>
</dict></array>
</dict></plist>"""


async def h_ios_ver(request):
    """iOS 壳的更新检查（GET /api/ios_ver，免鉴权）。

    公开用户只能从 TestFlight 装（陌生设备装不了开发签名包），所以更新检查以
    TestFlight 上的 build 为准；开发签名通道的版本另外报（ipa_code/ipa_ver），
    给已登记设备的开发者用。
    两条都没有就回 404 —— 别透传隧道：隧道要登录，壳没登录态时会拿到 401。"""
    m = _ios_meta()
    ipa_ok = bool(m.get("code")) and os.path.exists(IOS_FILE)
    tf = _tf_meta()
    if tf["code"]:
        out = {"ok": True, "code": tf["code"], "ver": tf["ver"], "src": "testflight",
               "tf_url": TF_URL, "url": _relay_base(request) + "/ios-install"}
    elif ipa_ok:
        out = {"ok": True, "code": int(m["code"]), "ver": str(m.get("ver") or "?"),
               "src": "relay", "tf_url": TF_URL,
               "url": _relay_base(request) + "/ios-install",
               "file": _ios_file_name()}
    else:
        return web.json_response({"ok": False, "error": "中继还没有 iOS 安装包"}, status=404)
    if ipa_ok:
        out["ipa_code"] = int(m["code"])
        out["ipa_ver"] = str(m.get("ver") or "?")
    return web.json_response(out)


async def h_ios_install(request):
    """iOS 安装页（免登录）。

    有 TestFlight 公开链接 → 主按钮就是它（任何设备都能装）；开发签名的
    itms-services 链接降级成下面那个次要按钮（只对已登记设备有效）。
    没有公开链接 → 退回原来的开发签名安装页。"""
    m = _ios_meta()
    ipa_ok = bool(m.get("code")) and os.path.exists(IOS_FILE)
    if not TF_URL and not ipa_ok:
        return web.Response(text="<!doctype html><meta charset=utf-8>"
                                   "<body style='background:#0b0e14;color:#e6edf3;font:16px/1.6 -apple-system;padding:30px'>"
                                   "中继还没有 iOS 安装包。</body>",
                            content_type="text/html")
    base = _relay_base(request)
    plist = base + "/ios.plist"
    link = "itms-services://?action=download-manifest&url=" + urllib.parse.quote(plist, safe="")
    if TF_URL:
        body = (
            '<a class="big" href="%s">在 TestFlight 中安装</a>'
            '<p class="note">苹果官方的公开内测通道：<b>任何 iPhone 都能装</b>，不用登记设备、'
            '不用第三方签名。<br>点上面按钮会跳到 Apple 的 TestFlight 页面 → 点「接受」→ 点「安装」。<br>'
            '以后有新版本，在 TestFlight 里点「更新」覆盖安装即可，配对和设置都保留。</p>'
            % TF_URL)
        if ipa_ok:
            body += ('<a class="alt" href="%s">已登记设备：装开发签名版</a>'
                     '<p class="note" style="margin-top:8px">开发签名版走系统安装清单，只在已登记的'
                     '设备上装得上，公开用户别用这条。</p>' % link)
    else:
        body = (
            '<a class="big" href="%s">点这里安装</a>'
            '<p class="note">iOS 不许网页直接装包，只能走系统安装清单：点上面那个按钮，'
            '系统会问「是否下载安装」，同意即可。<br>'
            '装完第一次打开如果提示「不受信任的开发者」：去「设置 → 通用 → VPN 与设备管理」'
            '里信任开发者。<br>这是开发签名版，只在已登记的设备上装得上。</p>' % link)
    tf = _tf_meta()
    shown_ver = tf["ver"] if (TF_URL and tf["code"]) else str(m.get("ver") or "?")
    shown_code = str(tf["code"]) if (TF_URL and tf["code"]) else str(m.get("code") or "?")
    html = (_PAGE_IOS_INSTALL.replace("__BODY__", body)
            .replace("__VER__", shown_ver)
            .replace("__CODE__", shown_code))
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def h_ios_plist(request):
    """itms-services 安装清单：assets 里写 .ipa 的绝对地址。"""
    m = _ios_meta()
    if not (m.get("code") and os.path.exists(IOS_FILE)):
        return web.Response(status=404, text="还没有 iOS 安装包")
    base = _relay_base(request)
    # 直链固定为 /codeboss.ipa（只有这条路由）；带版本号的名字只用于下载时的文件名，
    # 别写进清单——否则系统拿一个没人监听的路径去装，装到一半 404。
    xml = (_IPA_PLIST.replace("__IPA__", base + "/codeboss.ipa")
           .replace("__BID__", IOS_BUNDLE)
           .replace("__CODE__", str(m.get("code"))))
    return web.Response(text=xml, content_type="application/xml",
                        headers={"Cache-Control": "no-store"})


async def h_ios_pkg(request):
    """iOS 安装包下载（免登录直链，给安装清单用）。"""
    if os.path.exists(IOS_FILE) and os.path.getsize(IOS_FILE) > 10240:
        return web.FileResponse(IOS_FILE, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="%s"' % _ios_file_name(),
            "Cache-Control": "no-store"})
    return web.json_response({"ok": False, "error": "中继还没有 iOS 安装包"}, status=404)


async def h_mac_dmg(request):
    """Mac DMG 下载（免登录；和 APK 一样的直链）。"""
    ver = str(_mac_meta().get("ver") or "")
    fname = ("BossHost-%s-mac.dmg" % ver) if ver else "BossHost-mac.dmg"
    r = await _dl_or_local(request.app, request, MAC_FILE, "bosshost-mac.dmg",
                           "application/x-apple-diskimage", fname)
    return r or web.json_response({"ok": False, "error": "中继还没有 DMG"}, status=404)


async def r_admin_macdmg(request):
    """上架/替换 Mac DMG（Bearer RELAY_ADMIN_TOKEN；ver 走 query）。
    校验 UDIF 尾魔数（koly）才落盘；上限 64MiB（DMG 里带了两套架构的 Python 运行时）。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权上架"}, status=401)
    ver = (request.query.get("ver") or "").strip()
    if not ver or len(ver) > 40 or not all(c.isalnum() or c in "._-" for c in ver):
        return web.json_response({"ok": False, "error": "ver 缺失或不合法（?ver=x.y.z）"}, status=400)
    # app 级 client_max_size 是 32MiB（别的地方不需要收大文件），这条路由单独放宽
    request._client_max_size = 64 * 1024 * 1024
    data = await request.read()
    if len(data) < 1024 or len(data) > 64 * 1024 * 1024:
        return web.json_response({"ok": False, "error": "DMG 大小不合规（1KB~64MiB）"}, status=400)
    traffic_add("ops", len(data), 0, 1)
    if data[-512:-508] != b"koly":
        return web.json_response({"ok": False, "error": "不是 UDIF（dmg）镜像"}, status=400)
    tmp = MAC_FILE + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, MAC_FILE)
    meta = {"ver": ver, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "ts": int(time.time()), "dev": (request.headers.get("x-dev") or "")[:32]}
    tmeta = MAC_META + ".part"
    with open(tmeta, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmeta, MAC_META)
    print("[relay] mac dmg 已上架 ver=%s size=%d" % (ver, len(data)), flush=True)
    _oss_mirror(request.app, "bosshost-mac.dmg", data, "application/x-apple-diskimage",
                skip=_oss_done(request))
    return web.json_response(dict(ok=True, **meta))


# 分发文件上架（Windows 安装包 / Linux 发行包）：本地留一份，同时镜像到 OSS 公共读。
# 官网的下载链接指回中继这两条固定路径，中继再按 OSS 是否就绪决定直出还是自己发。
_DIST_UP = {
    "BossHost-Setup.exe": (EXE_FILE, b"MZ", 80),
    "CodeBoss-Setup.exe": (EXE_FILE, b"MZ", 80),
    "codeboss-linux.tar.gz": (LINUXPKG_FILE, b"\x1f\x8b", 80),
}
# 同一个安装包两个对外名字（老页面 CodeBoss-、新页面 BossHost-）。下载口是按
# 新名字去 OSS 查的：OSS 里只有另一个名字时查不到，就退回中继自己发 15MB
# （新加坡 → 国内，非常慢）。上架时两个键都镜像一份，谁查都能 302 到 OSS。
_DIST_ALIASES = {"BossHost-Setup.exe": "CodeBoss-Setup.exe",
                 "CodeBoss-Setup.exe": "BossHost-Setup.exe"}


async def r_admin_dist(request):
    """POST /r/admin/distpkg?name=BossHost-Setup.exe|codeboss-linux.tar.gz（Bearer 管理令牌）。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权上架"}, status=401)
    name = (request.query.get("name") or "").strip()
    ent = _DIST_UP.get(name)
    if not ent:
        return web.json_response({"ok": False, "error": "name 不合法"}, status=400)
    path, magic, maxmb = ent
    request._client_max_size = maxmb * 1048576
    data = await request.read()
    if len(data) < 1024 or len(data) > maxmb * 1048576:
        return web.json_response({"ok": False, "error": "大小不合规"}, status=400)
    traffic_add("ops", len(data), 0, 1)
    if not data.startswith(magic):
        return web.json_response({"ok": False, "error": "文件类型不对"}, status=400)
    ver = ""
    if name.endswith(".tar.gz"):
        # 先验包（VERSION 是唯一事实源），不合法就不落盘
        import io
        import tarfile
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
            for n in tf.getnames():
                if os.path.basename(n) == "VERSION":
                    f = tf.extractfile(n)
                    if f:
                        ver = f.read().decode("utf-8", "replace").strip()[:40]
                    break
        except Exception:
            ver = ""
        if not ver:
            return web.json_response({"ok": False, "error": "包里没有 VERSION"}, status=400)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    if ver:
        with open(LINUXPKG_META + ".part", "w", encoding="utf-8") as f:
            json.dump({"ver": ver, "size": len(data), "ts": int(time.time())}, f)
        os.replace(LINUXPKG_META + ".part", LINUXPKG_META)
    print("[relay] 分发文件已上架 name=%s ver=%s size=%d" % (name, ver, len(data)), flush=True)
    _oss_mirror(request.app, name, data, "application/octet-stream",
                skip=_oss_done(request))
    alias = _DIST_ALIASES.get(name)
    if alias:   # 另一个对外名字也放一份，下载口查哪个都能走 OSS
        _oss_mirror(request.app, alias, data, "application/octet-stream",
                    skip=_oss_done(request))
    return web.json_response({"ok": True, "name": name, "ver": ver, "size": len(data)})


def _apk_ticket_ok(key, k, now=None):
    """校验下载票据 "<exp>.<hmac>"。格式不对/过期/签名不符一律 False。"""
    try:
        exp_s, sig = str(k or "").split(".", 1)
        exp = int(exp_s)
    except (ValueError, TypeError):
        return False
    want = hmac.new(key, ("apk.%d" % exp).encode(), hashlib.sha256).hexdigest()
    return exp > (time.time() if now is None else now) and hmac.compare_digest(sig, want)


async def r_admin_app_apk(request):
    """管理后台安卓壳下载（GET /r/admin/app.apk，要管理员会话）。
    刻意不走 OSS、不设公开路径：这是管理员工具，不是产品分发包。
    页面里的下载按钮是普通导航，带不上 Authorization 头：登录用令牌（sessionStorage）
    时用 ?t= 传令牌；手机号登录走 cb_adm cookie，两者都认。管理壳里点下载会交给
    系统浏览器（另一个 cookie jar），所以还支持 ?k= 短时签名的下载票据（见
    r_admin_app_ticket）：10 分钟内有效，过期后重新进「下载」页会自动换一张。"""
    t = (request.query.get("t") or "").strip()
    k = (request.query.get("k") or "").strip()
    ok = _apk_ticket_ok(request.app["key"], k) if k else False
    if not ok and not (t and ADMIN_TOKEN and hmac.compare_digest(t, ADMIN_TOKEN)) \
            and not await _adm_auth(request):
        return web.json_response({"ok": False,
                                  "error": "无权下载：请先用手机号登录后台再点"}, status=401)
    if not (os.path.exists(ADMIN_APK_FILE) and os.path.getsize(ADMIN_APK_FILE) > 10240):
        return web.json_response({"ok": False, "error": "中继上还没有管理 App，先在电脑上传"}, status=404)
    return web.FileResponse(ADMIN_APK_FILE, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="codeboss-admin.apk"',
        "Cache-Control": "no-store"})


async def r_admin_app_ticket(request):
    """给「下载管理 App」签一张 10 分钟有效的票据。

    按钮是普通导航，管理壳还会把它甩给系统浏览器：那条链路上没有 cb_adm cookie，
    也没有 Authorization 头，只能靠 URL 里的签名。签名只绑过期时间，不绑身份，
    泄露也只影响这一个 APK 的 10 分钟下载。
    """
    if not await _adm_auth(request):
        return _adm_unauth()
    exp = int(time.time()) + 600
    sig = hmac.new(request.app["key"], ("apk.%d" % exp).encode(), hashlib.sha256).hexdigest()
    return web.json_response({"ok": True, "url": "/r/admin/app.apk?k=%d.%s" % (exp, sig),
                              "exp": exp})


async def r_admin_app_ver(request):
    """管理 App 自更新检查（GET /r/admin/app_ver，要管理员会话）。

    只答「中继上这个包的 sha256/大小」。壳自己算已安装 APK 的 sha 来比，不依赖
    上传时填版本号——同一个 versionCode 重新打包也能认出来。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False}, status=401)
    if not (os.path.exists(ADMIN_APK_FILE) and os.path.getsize(ADMIN_APK_FILE) > 10240):
        return web.json_response({"ok": False, "error": "还没有管理 App"}, status=404)
    h = hashlib.sha256()
    with open(ADMIN_APK_FILE, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return web.json_response({"ok": True, "sha256": h.hexdigest(),
                              "size": os.path.getsize(ADMIN_APK_FILE)},
                             headers={"Cache-Control": "no-store"})


async def r_admin_app_up(request):
    """上架/替换管理后台安卓壳 APK（POST /r/admin/adminapk，体是原始 APK 字节）。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权上架"}, status=401)
    request._client_max_size = 32 * 1048576
    data = await request.read()
    if len(data) < 10240 or len(data) > 32 * 1048576 or not data.startswith(b"PK\x03\x04"):
        return web.json_response({"ok": False, "error": "APK 不合法"}, status=400)
    tmp = ADMIN_APK_FILE + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, ADMIN_APK_FILE)
    print("[relay] 管理 App 已上架 size=%d" % len(data), flush=True)
    return web.json_response({"ok": True, "size": len(data)})


# host 离线时的页面过渡：/ext/* 与 /static/* 的 GET 拿到裸 JSON 错误会被 WebView 当
# "正常页面"渲染——没有 JS、不触发 onReceivedError、永不自愈（手机停在死页直到人去刷）。
# 改发一个 3s 自动重试的过渡页：host 回来后下一次刷新自动落回真页面。API 仍是 JSON（fetch 要解析）。
_PAGE_RETRY = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>连接中…</title>
<style>
body{background:#0b0e14;color:#c9d1d9;font:14px/1.6 -apple-system,'PingFang SC',sans-serif;
 margin:0;padding:28px 18px 40px;display:flex;flex-direction:column;align-items:center}
.t{color:#8b949e;margin:6px 0 16px;text-align:center}
.card{width:100%;max-width:420px;background:#131820;border:1px solid #262d3a;border-radius:12px;
 padding:12px 14px;margin:0 0 12px;box-sizing:border-box}
.dev{display:flex;align-items:center;gap:10px;padding:11px 2px;border-bottom:1px solid #1d232e}
.dev:last-child{border-bottom:0}
.dot{width:8px;height:8px;border-radius:50%;background:#4b5563;flex:none}
.dot.on{background:#3FB950}
.dev b{font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dev span{color:#8b949e;font-size:12.5px;flex:none}
button,a.btn{display:block;width:100%;max-width:420px;box-sizing:border-box;background:#1f2937;
 color:#c9d1d9;border:1px solid #2d3748;border-radius:9px;padding:11px;font-size:14px;
 text-align:center;text-decoration:none;margin-top:10px;font-family:inherit}
</style>
<div>⏳ 开发机暂时离线</div>
<div class="t" id="hint">正在自动重连…（<span id="sec">5</span> 秒后重试）</div>
<div class="card" id="devs" style="display:none"></div>
<button onclick="location.replace(location.href)">立即重试</button>
<a class="btn" href="/r/account">账号 / 套餐 / 修改密码</a>
<a class="btn" href="/r/logout">退出登录</a>
<script>
/* 离线时也得能自救：换一台在线开发机、退出去换账号——不能把用户锁在这张死页上。
   自动重连 5 秒一次；用户一动页面就暂停，免得点着点着自己刷新了。 */
var busy=false,sec=5;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function xget(u,cb){var x=new XMLHttpRequest();x.open('GET',u,true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;
    var d={};try{d=JSON.parse(x.responseText||'{}')}catch(e){}cb(d,x.status)};x.send(null);}
xget('/r/devices',function(d,st){
  if(st!==200||!d.ok)return;
  var list=d.devices||[];if(!list.length)return;
  var h='<div style="color:#8b949e;font-size:12.5px;margin:0 0 4px">换一台开发机：</div>';
  list.forEach(function(v){
    var id=String(v.id||'');
    h+='<div class="dev" data-id="'+esc(id)+'" onclick="pick(this.getAttribute(\\'data-id\\'))">'
      +'<span class="dot'+(v.online?' on':'')+'"></span>'
      +'<b>'+esc(v.name||id.slice(0,8))+'</b>'
      +'<span>'+(v.online?'在线':'离线')+(v.frozen?' · 已冻结':'')+'</span></div>';
  });
  var box=document.getElementById('devs');box.innerHTML=h;box.style.display='block';
});
function pick(id){ busy=true; if(!id)return;
  document.cookie='cb_dev='+id+';path=/;max-age=2592000;samesite=lax';
  location='/d'; }
function pause(){ busy=true; var e=document.getElementById('hint');
  if(e&&e.textContent.indexOf('暂停')<0)e.textContent='已暂停自动重连，点「立即重试」'; }
['touchstart','mousedown','wheel','scroll','keydown'].forEach(function(ev){
  document.addEventListener(ev,pause,{passive:true});});
setInterval(function(){ if(busy)return; sec--;
  if(sec<=0){location.replace(location.href);return;}
  var e=document.getElementById('sec');if(e)e.textContent=sec;},1000);
</script>"""


def _page_retry(path_qs):
    return web.Response(text=_PAGE_RETRY.replace("__U__", path_qs), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


def _apk_ver_answer(app, request, product):
    """按产品线答壳自更新版本信息（product 先经 valid_product 清洗）：
    该产品槽没存过包返回 None（调用方透传在线 host）；
    灰度指向的 code 只有该产品归档存在才生效，否则答该槽最新。"""
    product = branding.valid_product(product)
    m = _apk_meta(product)
    if not m.get("code"):
        return None
    code = int(m["code"])
    want = int(_canary_pick(app, request, product)["apk"] or 0)
    if want and want != code:
        pm = _apk_code_meta(product, want)
        if pm and os.path.exists(_apk_code_path(product, want)):
            return {"code": want,
                    "ver": str(pm.get("ver") or "?"),
                    "url": "/%s-%d.apk" % (product, want),
                    "file": "%s-terminal-%s.apk" % (product, (pm.get("ver") or want)),
                    "src": "relay"}
    return {"code": code, "ver": str(m.get("ver") or "?"),
            "url": "/%s.apk" % product, "file": _apk_file_name(product),
            "src": "relay"}


async def h_apk_ver(request):
    """壳每 4h 的自更新版本检查（GET /api/apk_ver）：中继存过包就直接答——
    免认证（坚果老 WebView 读不到 httponly cookie 也能查）、不依赖在线开发机。
    没存过透传在线 host（老路径，host 侧自注入 token）。
    灰度：同 host 包，?dev= / cb_cid / 所属账号决定它该拿哪个 code。
    产品线：?product=（清洗后缺省 codeboss），codeboss 老壳无参零感知。"""
    ans = _apk_ver_answer(request.app, request,
                          branding.valid_product(request.query.get("product")))
    if ans:
        return web.json_response(ans)
    return await tunnel(request)


async def h_apk_code(request):
    """按 code 下载 APK（/codeboss-<code>.apk）：分版本推送/灰度回退用。
    HEAD 也给：部分 ROM 的 DownloadManager 先探长度。"""
    code = request.match_info.get("code") or ""
    product = "agentboss" if request.path.startswith("/agentboss-") else branding.PRODUCT_DEFAULT
    p = _apk_code_path(product, code) if code.isdigit() else ""
    if not (p and os.path.exists(p) and os.path.getsize(p) > 10240):
        return web.json_response({"ok": False, "error": "没有这个版本"}, status=404)
    pm = _apk_code_meta(product, int(code))
    fn = "%s-terminal-%s.apk" % (product, (pm.get("ver") or code))
    with open(p, "rb") as f:
        blob = f.read()
    if request.method == "HEAD":
        return web.Response(content_type="application/vnd.android.package-archive",
                            headers={"Content-Length": str(len(blob)),
                                     "Content-Disposition": 'attachment; filename="%s"' % fn})
    return web.Response(body=blob, content_type="application/vnd.android.package-archive",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % fn})


# ---------- 在线客户端计数（开发机状态栏显示）----------
# 手机/浏览器经中继访问设备时，按来源 IP 记一次活跃；90 秒没动静算离开。
# 数量变化时经 host-link 推 {"t":"presence","n":N}，开发机转给 Mac 客户端显示。
PRESENCE_TTL = 45   # iOS 挂起后不再发请求，窗口越短掉线减得越快（janitor 15s → 最坏 60s 减 1）


def _client_key(request):
    """客户端身份：优先页面写的长期 cookie cb_cid（同一出口 IP 下多台设备也能分开），
    没有就退回 IP+UA。"""
    cid = (request.cookies.get("cb_cid") or "").strip()
    if cid and len(cid) <= 64:
        return "c:" + cid
    return "i:%s|%s" % (request.remote or "?", (request.headers.get("User-Agent") or "")[:80])


def _ua_kind(ua):
    """判断请求来自 App 还是浏览器。

    只看 UA 里有没有 CodebossShell 是不够的：壳里的 WebView 请求带 CodebossShell，
    但壳的原生请求（HttpURLConnection 上传/语音、后台服务等）用的是系统默认
    `Dalvik/2.1.0 (Linux; U; Android …)`，老逻辑全被当成浏览器。
    规则：带 CodebossShell → App；Dalvik 原生请求 → App；Android WebView
    （UA 里有 "; wv)"）→ App；其余才是浏览器。
    """
    ua = ua or ""
    if "CodebossShell/" in ua or "Dalvik/" in ua or "; wv)" in ua:
        return "app"
    return "web"


def _ua_label(ua):
    """从 UA 推 (机型短名, App/浏览器)。历史行存的是老逻辑写的 label，
    后台展示时用这个重新算一遍，不依赖入库时的字段。"""
    ua = ua or ""
    model = ""
    m = re.search(r"Android[^;)]*;\s*([^;)]+)", ua)
    if m:
        model = re.sub(r"\s+Build/.*$", "", m.group(1).strip()).strip()
    elif "iPhone" in ua:
        model = "iPhone"
    elif "iPad" in ua:
        model = "iPad"
    elif "Macintosh" in ua:
        model = "Mac 浏览器"
    elif "Windows" in ua:
        model = "Windows 浏览器"
    elif "Linux" in ua:
        model = "Linux 浏览器"
    if not model:
        model = (ua.split(")")[0] or ua or "未知设备")[:40]
    kind = _ua_kind(ua)
    v = re.search(r"CodebossShell/([0-9.]+)", ua)
    if kind == "app":
        lab = model + ((" · App " + v.group(1)) if v else " · App")
    else:
        # 桌面浏览器机名里已经带「浏览器」了，别再叠一层
        lab = model if model.endswith("浏览器") else model + " · 浏览器"
    return lab[:60], kind


def _ua_ver(ua):
    """严格说 UA 里只有 Android 壳会带 CodebossShell/x.y.z；
    iOS 壳从 0.2.18 起也会带（MainViewController.customUserAgent）。读不到就空。"""
    m = re.search(r"CodebossShell/([0-9.]+)", ua or "")
    return m.group(1) if m else ""


def _client_kind(request):
    return _ua_kind(request.headers.get("User-Agent") or "")


def _client_label(request):
    """给管理后台看的短名字：机型 + 端类型（UA 里能读到的就这些）。"""
    return _ua_label(request.headers.get("User-Agent") or "")[0]


def _presence_touch(app, dev, key, kind="web"):
    """记一次活跃，返回变化后的数量；数量没变返回 None（不必推）。"""
    now = time.time()
    d = app["presence"].setdefault(dev, {})
    d[key] = {"ts": now, "k": kind}
    for k, v in list(d.items()):
        if now - (v.get("ts", 0) if isinstance(v, dict) else v) > PRESENCE_TTL:
            d.pop(k, None)
    n = len(d)
    if app["presence_n"].get(dev) == n:
        return None
    app["presence_n"][dev] = n
    return n


def _presence_counts(app, dev):
    d = app["presence"].get(dev) or {}
    a = sum(1 for v in d.values() if isinstance(v, dict) and v.get("k") == "app")
    return len(d), a, len(d) - a


async def _push_presence(app, dev):
    ws = app["links"].get(dev)
    n, a, w = _presence_counts(app, dev)
    if not ws or ws.closed:
        return
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json({"t": "presence", "n": n, "app": a, "web": w})
    except Exception:
        pass


async def _logs_expire_loop(app):
    """日志上报到点自动关：库里的开关落回 0，顺带通知在线的 host 停推。

    host 只在「内容有变化」时才推，所以不能等下一帧才发现过期——这里自己每 30 秒扫一遍。
    """
    last_purge = 0.0
    while True:
        await asyncio.sleep(30)
        try:
            db, now = _db(app), time.time()
            if now - last_purge > 3600:      # 一小时清一次 7 天前的日志快照就够
                last_purge = now
                await asyncio.get_event_loop().run_in_executor(None, _devlog_purge)
            for (dev,) in db.execute(
                    "SELECT id FROM devices WHERE logs_on=1 AND logs_until>0 AND logs_until<?",
                    (now,)).fetchall():
                db.execute("UPDATE devices SET logs_on=0, logs_until=0 WHERE id=?", (dev,))
                db.commit()
                ws = (app.get("links") or {}).get(dev)
                if ws is not None and not ws.closed:
                    try:
                        async with ws._cb_lock:
                            await ws.send_json({"t": "logs_on", "on": 0})
                    except Exception:
                        pass
                print("[logs] dev=%s 满 %d 小时，自动关闭日志上报"
                      % (dev[:8], LOG_MAX_S // 3600), flush=True)
        except Exception:
            pass


async def _presence_janitor(app):
    """客户端离开时不会主动发请求：每 15 秒清理过期 IP，数量有变就推。
    顺手每 10 分钟踢一次闲置的浏览器客户端（名额早点放回去）。"""
    _swept = 0.0
    while True:
        await asyncio.sleep(15)
        now = time.time()
        if now - _swept > 120:
            _swept = now
            try:
                _browser_sweep(_db(app), now)
            except Exception:
                pass
        for dev, d in list(app["presence"].items()):
            for k, v in list(d.items()):
                if now - (v.get("ts", 0) if isinstance(v, dict) else v) > PRESENCE_TTL:
                    d.pop(k, None)
            n = len(d)
            if app["presence_n"].get(dev) != n:
                app["presence_n"][dev] = n
                await _push_presence(app, dev)


async def _orphan_sweep_step(app):
    """扫一遍在线链路，踢掉「不该在线」的：设备行没了（被删），或者无主又不是
    匿名免费档（老流程未绑定）。匿名免费设备（owner 空 + anon=1）是合法在线状态，
    不能踢——否则免费用户每 60 秒断一次链。返回被踢的 dev 列表。"""
    db = _db(app)
    kicked = []
    for dev, ws in list(app["links"].items()):
        row = db.execute("SELECT owner, anon FROM devices WHERE id=?", (dev,)).fetchone()
        if not row or (row[0] is None and not row[1]):
            app["links"].pop(dev, None)
            (app.get("direct") or {}).pop(dev, None)
            try:
                await ws.close()
            except Exception:
                pass
            kicked.append(dev)
            print("[relay] 设备已删除/未绑定账号，已踢下线 dev=%s" % dev[:8], flush=True)
    return kicked


async def _unbound_sweep(app):
    """账号解绑/删除不会断链接：定期巡检在线设备（见 _orphan_sweep_step）。"""
    while True:
        await asyncio.sleep(60)
        try:
            await _orphan_sweep_step(app)
        except Exception as e:
            print("[relay] unbound sweep 失败: %s" % type(e).__name__, flush=True)


_PAGE_QUOTA = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>今日时长已用完</title>__CSS__
<style>
.qwrap{max-width:420px;margin:0 auto;padding:56px 20px}
.qcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 22px;text-align:center}
.qcard h1{font-size:20px;margin:0 0 10px;color:var(--txt)}
.qcard p{color:var(--mut);font-size:13.5px;line-height:1.75;margin:0 0 20px;white-space:pre-line}
.qbar{height:8px;border-radius:4px;background:var(--ctl);overflow:hidden;margin:0 0 8px}
.qbar i{display:block;height:100%;background:var(--boss);width:__PCT__}
.qnum{font-size:12px;color:var(--mut);margin:0 0 22px}
.qbtn{display:block;background:var(--gy);color:#fff;text-decoration:none;border-radius:10px;padding:13px;font-weight:600;font-size:15px}
.qbtn2{display:block;margin-top:10px;background:transparent;border:1px solid var(--line);color:var(--txt);text-decoration:none;border-radius:10px;padding:12px;font-weight:600;font-size:14.5px}
.qsub{display:block;margin-top:16px;color:#3fb950;font-size:14px;font-weight:600;
  text-decoration:none;border:1px solid rgba(63,185,80,.45);background:rgba(63,185,80,.10);
  border-radius:10px;padding:11px 12px}
</style></head><body><div class="qwrap"><div class="qcard">
<h1>__TITLE__</h1>
<p>__TIP__</p>
<div class="qbar"><i></i></div>
<div class="qnum">__QNUM__</div>
__BTN__
<a class="qsub" href="javascript:location.reload()">已开通 / 名额已腾出？点这里刷新</a>
</div></div>
<script>
/* 从升级页/付款页回来时，浏览器可能把这张超额页从缓存里直接恢复（不重新请求）。
   一回到前台就重载：已开通的要立刻放行，没开通的继续显示升级页。 */
window.addEventListener('pageshow',function(e){ if(e.persisted) location.reload(); });
document.addEventListener('visibilitychange',function(){ if(!document.hidden) location.reload(); });
/* 二次确认不弹原生 confirm()：WebView 里那个框会把网址顶在标题上。
   改成页面内确认——第一下只把按钮文字换成提示，4 秒内第二下才真的跳转。 */
document.addEventListener('click',function(e){
  var a=e.target;
  while(a && a.nodeType===1 && a.tagName!=='A') a=a.parentNode;
  if(!a || a.nodeType!==1 || a.tagName!=='A' || !a.getAttribute('data-confirm')) return;
  if(a.getAttribute('data-armed')==='1') return;
  e.preventDefault();
  a.setAttribute('data-armed','1');
  a.setAttribute('data-txt', a.textContent || '');
  a.textContent=a.getAttribute('data-confirm');
  a.style.borderColor='#f85149'; a.style.color='#f85149';
  setTimeout(function(){
    if(a.getAttribute('data-armed')==='1'){
      a.removeAttribute('data-armed');
      a.textContent=a.getAttribute('data-txt') || '';
      a.style.borderColor=''; a.style.color='';
    }
  },4000);
});
</script>
</body></html>"""


def _fmt_min(n):
    n = int(n or 0)
    return ("%d 小时" % (n // 60)) if n % 60 == 0 else ("%d 分钟" % n)


# 各种「额度用完」页统一带的一个出口：换账号登录（= 退出登录，顺带释放名额）。
# 不用 confirm()：WebView 里它会把网址顶在弹窗标题上，看着像被跳去别处。
# 改成页面内两下确认（第一下把按钮变成「再点一次确认退出」，4 秒后复原）。
_SW_ACC = ('<a class="qbtn2" href="/r/logout" '
           'data-confirm="再点一次确认退出">换其他账号登录</a>')


# 时长超额页 _quota_page 已随时长墙一起删（2026-11-26）；_PAGE_QUOTA 模板
# 还被客户端数超额页（_clients_page）共用，保留。


def _clients_page(used, cap, plan, app=None, db=None, uid=None):
    """客户端数超额页：和时长超额页同一套样式，只换文案（同一个 _PAGE_QUOTA）。"""
    p = plans()
    tname = (p.get(plan) or {}).get("name") or plan
    nx = _next_tier(plan, "clients")     # 不写死档位：达到最高档时 nx 为 None
    if nx:
        tip = ("「%s」最多 %d 个客户端（近 24 小时已用 %d 个），这台新设备暂时进不来。\n"
               "腾名额：① 下方「管理已登录的客户端」踢掉旧设备（需短信/邮箱验证）；"
               "② 旧设备退出登录；③ 超 24 小时未连自动释放。\n"
               "都还要用可开通「%s」，最多 %d 个。" % (tname, cap, used, nx[0], nx[1]))
        btn = ('<a class="qbtn" href="/r/plans?pay=1">开通 %s · 最多 %d 个客户端</a>' % nx
               if PLANS_OPEN else "")
    else:
        tip = ("「%s」最多 %d 个客户端（近 24 小时已用 %d 个），已达上限。\n"
               "腾名额：① 下方「管理已登录的客户端」踢掉旧设备（需短信/邮箱验证）；"
               "② 旧设备退出登录；③ 超 24 小时未连自动释放。" % (tname, cap, used))
        btn = ""                         # 最高档：不渲染升级按钮，别劝人降级
    try:
        _ticket = make_acct_tok(app, db, uid) if (app is not None and db is not None and uid) else ""
    except Exception:
        _ticket = ""
    manage = """
<div style="margin-top:16px;border-top:1px solid #2a3240;padding-top:12px">
<a class="qbtn" href="javascript:acctOpen()">管理已登录的客户端</a>
<div id="acctBox" style="display:none;margin-top:10px;font-size:13px;text-align:left"></div>
</div>
<script>
var ACCT_TICKET="__ACCT_T__";
function acctEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function acctCid(){var m=document.cookie.match(/(?:^|;\\s*)cb_cid=([^;]+)/);return m?decodeURIComponent(m[1]):'';}
var ACCT={d:null,via:'',cid:''};
function acctOpen(){
  var box=document.getElementById('acctBox'); if(!box) return;
  box.style.display='block'; box.textContent='读取中…';
  fetch('/r/acct_info',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:acctCid(),s:ACCT_TICKET})})
   .then(function(r){return r.json()}).then(function(d){
     if(!d.ok){box.textContent='读取失败：'+(d.error||'');return;}
     ACCT.d=d;
     var h='<div style="color:#8b949e;margin:6px 0">最近 24 小时用过 '+(d.used||0)+' / '+(d.cap||'—')+' 个客户端</div>';
     var list=d.clients||[];
     if(!list.length) h+='<div style="color:#8b949e">还没有登记过的客户端</div>';
     list.forEach(function(c){
       var ago=Math.max(0,Math.round((Date.now()/1000-(c.last||0))/60));
       var when=ago<60?(ago+' 分钟前'):(ago<1440?(Math.round(ago/60)+' 小时前'):(Math.round(ago/1440)+' 天前'));
       h+='<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #232a36">'
         +'<div style="flex:1;min-width:0"><div style="font-weight:600">'+acctEsc(c.label||(c.cid||'').slice(0,8))+'</div>'
         +'<div style="color:#8b949e;font-size:12px">'+(c.current?'本机 · ':'')+(c.counted?'占名额':'不占名额')+' · '+when+'</div></div>'
         +(c.current?'':'<button type="button" onclick="acctKick(\\''+acctEsc(c.cid)+'\\')" style="background:#1a212c;border:1px solid #2a3240;color:#f85149;border-radius:8px;padding:7px 10px">踢下线</button>')
         +'</div>';
     });
     h+='<div id="acctVerify" style="display:none;margin-top:10px"></div>';
     box.innerHTML=h;
   }).catch(function(e){box.textContent='网络错误：'+e;});
}
function acctKick(cid){
  ACCT.cid=cid; var d=ACCT.d||{}; var v=document.getElementById('acctVerify'); if(!v) return;
  v.style.display='block';
  var bs='background:#1a212c;border:1px solid #2a3240;color:#c9d1d9;border-radius:8px;padding:8px 10px';
  v.innerHTML='<div style="margin-bottom:8px;color:#8b949e">踢下线需要二次验证：验证码会发到账号绑定的手机号或邮箱。</div>'
   +'<div style="display:flex;gap:8px;flex-wrap:wrap">'
   +(d.has_phone?('<button type="button" onclick="acctSend(\\'sms\\')" style="'+bs+'">发短信验证码</button>'):'')
   +(d.has_email?('<button type="button" onclick="acctSend(\\'email\\')" style="'+bs+'">发邮箱验证码</button>'):'')
   +'</div>'
   +'<div style="display:flex;gap:8px;margin-top:8px"><input id="acctCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" style="flex:1;background:#0b0e14;border:1px solid #2a3240;color:#e6edf3;border-radius:8px;padding:9px"><button type="button" onclick="acctDo()" style="background:#2EA043;border:0;color:#fff;border-radius:8px;padding:9px 12px">确认踢下线</button></div>'
   +'<div id="acctMsg" style="margin-top:6px;color:#8b949e"></div>';
  ACCT.via=d.has_phone?'sms':(d.has_email?'email':'');
}
function acctSend(via){
  ACCT.via=via; var m=document.getElementById('acctMsg'); if(m) m.textContent='发送中…';
  fetch('/r/acct_send_code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:ACCT.cid,via:via,s:ACCT_TICKET})})
   .then(function(r){return r.json()}).then(function(d){ if(m) m.textContent=d.ok?(d.msg||'验证码已发送'):('发送失败：'+(d.error||'')); });
}
function acctDo(){
  var m=document.getElementById('acctMsg'); var el=document.getElementById('acctCode'); var code=el?el.value:'';
  if(!ACCT.via){ if(m) m.textContent='先点一个「发验证码」'; return; }
  if(m) m.textContent='验证中…';
  fetch('/r/acct_kick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:ACCT.cid,cur:acctCid(),code:code,via:ACCT.via,s:ACCT_TICKET})})
   .then(function(r){return r.json()}).then(function(d){
     if(d.ok){
       if(m) m.innerHTML='已踢下线，名额已腾出，正在自动进入…<br><a href="javascript:location.reload()" style="color:#3fb950">没反应？点这里</a>';
       /* 名额够了就自动重新加载：这次请求会重新过客户端数闸门，直接进会话列表 */
       setTimeout(function(){ try{ location.reload(); }catch(e){} }, 900);
     }
     else if(m) m.textContent='失败：'+(d.error||'');
   });
}
</script>
"""
    # 名额满了多半是换了手机/想用另一个号：先给「管理已登录客户端」的入口
    # （踢掉旧手机腾名额，需短信/邮箱二次验证），再给「更换账号」的出口。
    return (_PAGE_QUOTA.replace("__CSS__", _CSS)
            .replace("__TITLE__", "客户端数已达上限")
            .replace("__TIP__", tip)
            .replace("__BTN__", btn + manage.replace("__ACCT_T__", _ticket) + _SW_ACC)
            .replace("__QNUM__", "最近 24 小时用过 %d / %d 个客户端" % (used, cap))
            .replace("__PCT__", "100%"))


def _d_dev_pick(request, db, uid):
    """/d?dev=<id>:局域网直连源切台的 cookie 兜底。设备属于自己 →
    302 回 /d 并种 cb_dev;不是自己的/没带 → None,维持原 cookie 流程。"""
    qd = str(request.query.get("dev") or "")[:64]
    if not qd or not own_device(db, uid, qd):
        return None
    resp = web.HTTPFound("/d")
    resp.set_cookie("cb_dev", qd, max_age=2592000, samesite="lax", path="/",
                    secure=(request.scheme == "https"))
    return resp


async def tunnel(request):
    if request.path in ("/codeboss.apk", "/agentboss.apk") and request.method in ("GET", "HEAD"):
        # 免登录下载（登录页「下载 App」+ 壳自更新都走这）。路径即产品线：
        # /codeboss.apk / /agentboss.apk 各答各槽（boss 系列分槽，Task 2）。
        # host 每次连上 ws 链路把 APK 推上来落盘：存过就直接发，开发机离线也能装/自更新；
        # 没存过（老 host/首装）→ 透传在线开发机，读盘永远最新。
        # HEAD 也给：部分 ROM 的 DownloadManager 先探长度，401 会让它直接判失败。
        product = "agentboss" if request.path.startswith("/agentboss") else "codeboss"
        binp, _metap = _apk_slot(product)
        if os.path.exists(binp) and os.path.getsize(binp) > 10240:
            # OSS 上有同样大的副本就 302 过去（APK 也不吃中继带宽）
            url = await _oss_dl_redirect(request.app, "%s.apk" % product,
                                         os.path.getsize(binp))
            if url:
                return web.Response(status=302, headers={"Location": url,
                                                         "Cache-Control": "no-store"})
            with open(binp, "rb") as f:
                blob = f.read()
            if request.method == "HEAD":
                return web.Response(
                    content_type="application/vnd.android.package-archive",
                    headers={"Content-Length": str(len(blob)),
                             "Content-Disposition": 'attachment; filename="%s"' % _apk_file_name(product)})
            traffic_add("product", 0, len(blob), 1)
            return web.Response(
                body=blob, content_type="application/vnd.android.package-archive",
                headers={"Content-Disposition": 'attachment; filename="%s"' % _apk_file_name(product)})
        if request.method == "HEAD":
            return web.json_response({"error": "开发机离线，暂时无法下载 App"}, status=502)
        ws = None
        for _k, v in request.app["links"].items():
            if not v.closed:
                ws = v
                break
        if ws is None:
            return web.json_response({"error": "开发机离线，暂时无法下载 App"}, status=502)
        return await _tunnel_plain(request, ws, "any")
    is_bg = request.path == BG_PATH
    is_say = say_path(request.path)
    # 后台提醒通道：浏览器会话的 r_sess 读不到（httponly，老 WebView），
    # 用壳专用、非 httponly、只授权这一条流的 cb_bg；只有 cb_cid 的设备
    # （扫码配对、从没在中继登录过）再用设备版 cb_bgd（见 remind_auth）。
    uid = bg_uid(request) if is_bg else None
    if uid is None:
        uid = uid_of(request)
    bgd = None
    if uid is None and (is_bg or is_say):
        bgd = remind_auth(request)   # 账号会话没有才退到设备凭据，别抢账号的身份
        if bgd is not None:
            uid = bgd[0]             # 匿名设备的 owner 是 None，照旧走匿名那条路
    # 免注册的匿名设备：没有账号，但 cb_dev_sess 认定它代表哪台开发机。
    # 账号会话永远优先；设备会话只给隧道，账号接口认 r_sess，见 uid_of。
    anon_dev = None
    if uid is None and bgd is not None:
        anon_dev = bgd[1]         # 设备凭据自己说了是哪台，别信请求里的 cb_dev
    if uid is None and not is_bg and anon_dev is None:
        anon_dev = dev_of(request)
    anon = anon_dev is not None
    if uid is None and not anon:
        if is_bg or is_say:
            return web.json_response({"ok": False, "error": "提醒通道未授权"}, status=401)
        if request.path in ("/", "/d") and request.method == "GET":   # 默认访问=扫码/输码页
            return web.Response(text=_anon_landing_html(),
                                content_type="text/html",
                                headers={"Cache-Control": "no-store"})
        return web.Response(status=401, text="先在 /r/ 登录")
    if anon and request.path == "/" and request.method == "GET":
        # 匿名设备名下就这一台：/ 直接进 /d，别走「我的开发机」空页面。
        _r0 = web.HTTPFound("/d")
        _r0.set_cookie("cb_dev", anon_dev, max_age=2592000, samesite="lax", path="/",
                       secure=(request.scheme == "https"))
        raise _r0
    # 客户端数收费的统计口径（同一 cid 一小时只写一次）。没有 cb_cid（老壳/原生接口）
    # 不能白嫖一个 seat：退回 IP+UA 的稳定键（_client_key）；匿名设备按它自己的
    # 主体 d<dev> 计，免费档 clients=1 —— 把会话 cookie 复制到第二台手机上会被拦。
    # 身份和名额判定都在 _seat_wall：按设备指纹去重，后台提醒流不占名额。
    _dbc = _db(request.app)
    _suid = uid if not anon else subject_of(_dbc, None, anon_dev)
    _gate = _seat_wall(request, _dbc, _suid, exempt=(is_bg or is_say))
    if _gate:
        # 第 cap+1 个客户端：页面导航给能看懂的升级页，接口给 JSON（页面 toast 会显示）
        _used, _cap = _gate
        _acc = (request.headers.get("Accept") or "").lower()
        if request.method == "GET" and ("text/html" in _acc or request.path == "/d"
                                        or request.path.startswith("/ext/")
                                        or request.path.startswith("/static/")):
            return web.Response(text=_clients_page(_used, _cap, user_plan(_dbc, _suid),
                                                   app=request.app, db=_dbc, uid=_suid),
                                content_type="text/html", status=403,
                                headers={"Cache-Control": "no-store"})
        return web.json_response({"ok": False, "feature": "clients",
                                  "plan": user_plan(_dbc, _suid),
                                  "error": _cap_error("客户端数", user_plan(_dbc, _suid), _cap, "clients",
                                                      note="；在旧手机上退出登录或超过 24 小时没连接会自动释放")},
                                 status=403)
    # 远程使用时长：经中继的请求按分钟累计，超了给升级页/JSON（Max 不限）。
    # 后台提醒通道豁免：黑屏时页面已经睡了，这条流只为收提醒，不算「远程使用」，
    # 超额也不挡——否则用完额度连「任务完成了」都收不到。
    # 计时点挪到 host-link 校验之后（见下）：host 离线时请求根本没送出去，不该扣分钟。
    _dbo = _db(request.app)
    _plan = user_plan(_dbo, uid)
    if not plan_features(_dbo, uid).get("terminal", True) and is_term_path(request.path):
        _terr = ("识别命令是 Pro / Max 套餐的功能，开通后使用"
                 if request.path.startswith("/api/ext_scan/")
                 else "免费版不含命令终端，开通 Pro 后使用")
        return web.json_response(
            {"ok": False, "feature": "terminal", "plan": _plan,
             "error": _terr}, status=403)
    # 端权限墙（2026-11-18）：账号不能用这个端 → 明确 403 页/JSON，别让页面半死。
    # 只认页面/接口自己声明的产品线（壳种的 cb_product 或 X-Product）；没声明就
    # 不拦——浏览器直开时判不出是哪条线，拦错了就是把正在用的端收走。
    if request.path == "/d" or request.path.startswith("/api/"):
        want = (request.headers.get("X-Product")
                or request.cookies.get("cb_product") or "").strip()
        if want:
            want = branding.valid_product(want)
            if anon:
                # 匿名设备只属于自己那条产品线（登记时定的），不能跨线看别人的端。
                _dp = _dbo.execute("SELECT product FROM devices WHERE id=?",
                                   (anon_dev,)).fetchone()
                allowed = [branding.valid_product(_dp[0] if _dp else None)]
            else:
                allowed, _src = _allowed_products(_dbo, uid)
            if want not in allowed:
                if request.path == "/d" and request.method == "GET":
                    return web.Response(text=_locked_page(want, allowed),
                                        content_type="text/html", status=403,
                                        headers={"Cache-Control": "no-store"})
                return web.json_response(
                    {"ok": False, "feature": "product", "product": want,
                     "allowed": allowed,
                     "error": "该账号未开通「%s」"
                              % branding.product_name(want)["label"]}, status=403)
    if request.path == "/d" and request.method == "GET" and not anon:
        # /d?dev=<id>：局域网直连源切台专用。直连源(http://192.168.x.x)上 devSwitch
        # 写的 cookie 在 IP 域下，跳到中继域名后读不到 → 中继仍按旧设备转发，
        # 会话/收件箱全是旧机器内容(2026-11-18 安卓家里 WiFi)。query 带上设备,
        # 属于自己就种 cookie 后 302 回 /d。
        # 匿名设备不参与：owner 是 NULL，own_device 会把它和任何未绑定设备算成「自己的」。
        pick = _d_dev_pick(request, _db(request.app), uid)
        if pick is not None:
            raise pick
    if (request.path == "/d" and request.method == "GET"
            and not anon and not (request.cookies.get("cb_dev") or "")):
        raise web.HTTPFound("/")   # 没选过设备：回开发机列表
    if request.path == "/" and request.method == "GET":
        # 登录后直接进会话列表页（/d）：设备选择/添加在客户端列表页 devBar 已覆盖，
        # 不再出「我的开发机」页。名下有设备→选中（在线优先，否则最近活跃）并跳 /d；
        # 一台设备都没有→才出设备页（配对入口还在这）。
        rows = _db(request.app).execute(
            "SELECT id FROM devices WHERE owner=? ORDER BY last_seen DESC", (uid,)).fetchall()
        if rows:
            links = request.app["links"]
            ids = [r[0] for r in rows]
            # 已经选过、那台还在名下：就留在那台。以前每次冷启动都按「最近活跃」重选，
            # 名下有四五台机器的人一开 App 就悄悄换了电脑（会话列表、装没装 Codex、
            # 两个 agent tab 全跟着变），看着像功能时有时无。
            cur = (request.cookies.get("cb_dev") or "").strip()
            if cur in ids:
                dev_pick = cur
            else:
                online = [i for i in ids
                          if links.get(i) is not None and not links[i].closed]
                dev_pick = (online or ids)[0]
            resp = web.HTTPFound("/d")
            resp.set_cookie("cb_dev", dev_pick, max_age=2592000, samesite="lax", path="/",
                            secure=(request.scheme == "https"))
            raise resp
        return web.Response(text=_PAGE_DEV.replace("__CSS__", _CSS),
                            content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    dev = anon_dev or (bgd[1] if bgd else "") or request.cookies.get("cb_dev") or ""
    if anon:
        row = _db(request.app).execute(
            "SELECT id,name,owner,last_seen,frozen FROM devices WHERE id=?", (dev,)).fetchone()
    else:
        row = own_device(_db(request.app), uid, dev)
    if not row:
        # cb_dev cookie 缺失/失效（App 直开 /ext/<sid>、cookie 过期、换设备）：
        # 名下只有一台设备就自动选中，别让整个隧道 403 —— 这是「切换模型等接口 403」的真凶。
        mine = _db(request.app).execute(
            "SELECT id FROM devices WHERE owner=? ORDER BY last_seen DESC", (uid,)).fetchall()
        if len(mine) == 1:
            dev = mine[0][0]
            row = own_device(_db(request.app), uid, dev)
        if not row:
            # 页面入口（/d）遇到失效选中（设备被删、换机登录）：回 / 重新选台——
            # 有设备就自动挑一台，一台不剩就是「接入开发机」页。别给用户看 403。
            if request.path == "/d" and request.method == "GET":
                raise web.HTTPFound("/")
            print("[relay] 隧道 403：未选设备 path=%s uid=%s dev=%r" % (request.path, uid, dev), flush=True)
            return web.Response(status=403, text="未选择设备或无权访问（/r/ 里点「进入」）")
    if row[4] and request.method not in ("GET", "HEAD", "OPTIONS"):
        return web.json_response({"error": "设备已冻结：只读模式，仅可查看，禁止操作"}, status=403)
    ws = request.app["links"].get(dev)
    if ws is None or ws.closed:
        # 页面导航（WebView 主框架会带 Accept: text/html）也要给过渡页：
        # 刚配对/刚重连时 host-link 可能还差几秒，/d 直接甩裸 JSON 会让手机停在
        # 白底小字死页上（没有 viewport，也不会自愈）。
        _acc = (request.headers.get("Accept") or "").lower()
        if request.method == "GET" and ("text/html" in _acc
                                        or request.path == "/d"
                                        or request.path.startswith("/ext/")
                                        or request.path.startswith("/static/")):
            return _page_retry(request.rel_url.raw_path_qs)
        return web.json_response({"error": "开发机离线（host-link 未连接）"}, status=502)
    # 这台请求属于哪台客户端：presence / 查账 / 动作记录 / 流量统计全用同一个键，
    # 没有 cb_cid 就退回 IP+UA（_client_key）。以前这里引用的 cid 只存在于从未
    # 注册过的 _svc_dim_mw，host 在线的请求一律 NameError → 500（2026-11-21 断网）。
    _ckey = _client_key(request)
    _ckind = _client_kind(request)
    if _presence_touch(request.app, dev, _ckey, _ckind) is not None:
        await _push_presence(request.app, dev)
    # 远程使用时长（host 在线才记）：同一分钟幂等；host 补报的分钟也落在同一张表里。
    # 页面长时间没人碰时会带 a=0（挂着看）：照常转发、照常判超额，但不记新的一分钟。
    # 以前只要页面亮着就每分钟记一分钟，摆着不动的设备会按墙钟烧掉整天的额度。
    # 长连接（推流/实时画面）同样不记：页面开着不动也会每 50 秒重连一次推流
    # （ext.html 的 50s 自续，防运营商掐长连接），当普通请求记账的话，一台摆在
    # 桌上没碰过的手机一整天能烧掉 600+ 分钟——用户根本没碰手机。
    # 免费版每天的消息条数（sends_day，0 = 不限）：转发前判，挡住就不收也不记动作。
    # 计「发出去的消息」——中继隧道亲眼看到的 + 开发机补报的直连，同一张动作表（见 sends_used）。
    # 用量主体：账号 = u<id>，匿名设备 = d<dev>。额度判定和计数都落在这个主体上，
    # 所以免注册的免费用户和账号用户的账互不串（同一台机器换账号/解绑也不会继承额度）。
    subj = subject_of(_dbo, uid, dev)
    _meth = (request.method or "GET").upper()
    if _meth == "POST" and is_send_action(request.path):
        _scap = int(plan_limits(_dbo, subj).get("sends_day") or 0)
        if _scap:
            _sn = sends_used(_dbo, subj)
            if _sn >= _scap:
                print("[relay] send over subj=%s dev=%s used=%d cap=%d"
                      % (subj, str(dev)[:8], _sn, _scap), flush=True)
                _scta = _quota_cta(_plan, "发")
                return web.json_response(
                    {"ok": False, "limit": "sends",
                     "sends": {"used": _sn, "cap": _scap},
                     "plan": _plan,
                     "error": "今天的消息额度已用完（%d 条），明天恢复%s"
                              % (_scap, ("；" + _scta) if _scta else "")},
                    status=403)
    # 计时只剩记账（2026-11-26 摘时长墙：收费墙只按条数，use_cap 恒 0 → 永不超额）：
    #   · 读请求（GET/HEAD/OPTIONS：看消息、看会话列表、看输出、翻设置、页面导航）不记格；
    #   · 写操作（发消息、按键、开会话、跑命令、上传…）记格；
    #   · 长连接/探针/后台通道/a=0 照旧豁免。
    if is_bg or is_say or request.path in PROBE_PATHS:
        _a0 = True                       # 后台通道/探针：不是有人在用，也不记动作
    elif _meth in ("GET", "HEAD", "OPTIONS"):
        _a0 = True                       # 看不算：照常转发，不记格、不拦
    elif _meter_exempt(request.path):
        _a0 = True
    else:
        _a0 = False
        use_take(_dbo, subj)
        # 记到具体终端：同一分钟两台手机都在用就两行（不影响计费，查账用）
        use_client_touch(_dbo, subj, _min_now(), _ckey, _ckind,
                         _client_label(request),
                         request.headers.get("User-Agent") or "")
    if not (is_bg or request.path in PROBE_PATHS) and not request.path.startswith("/static/"):
        # 动作记录：这一分钟这台手机到底做了什么（发消息/按键/导航/轮询）。
        # 读请求（看消息、拉会话列表、轮询输出）也记——2026-11-09 起「看不计时」，
        # 但后台「使用详情」要能回答「这台设备/这个终端刚才有人在用吗」，
        # 只记写操作会出现「终端列表显示 5 分钟前活跃、使用详情却一条都没有」。
        # 读请求量大，不逐条 commit，攒到 _tunnel_flush_loop 每 30 秒落盘。
        _ak, _an = act_label(request.path, request.method)
        use_act(_dbo, subj, _min_now(), "relay", dev=dev, cid=_ckey,
                ua=request.headers.get("User-Agent") or "",
                kind=_ak, act=_an, path=request.path)
        if _a0:
            _ACT_DIRTY[0] = True
        else:
            _dbo.commit()
    request["_cb_uid"] = uid
    request["_cb_cid"] = _ckey
    request["_cb_dev"] = dev
    if request.path.startswith(STREAM_PATHS) and request.method == "GET":
        return await _tunnel_stream(request, ws, dev)   # SSE 等长连：分块转发
    return await _tunnel_plain(request, ws, dev)


# 整页限流：壳的离线重试是「3 秒后整页重载」，而 653KB 的整页一重载就是一次出口打满。
# 整页短缓存。2026-11-18 实测：一台手机 3 小时拉 977 次整页（632MB，占全站页面流量
# 97%），把 3Mbps 出口打满，其他账号的整页全部撞 120s 隧道超时拿到 504 → 也跟着进
# 重试循环，表现为「所有账号同时断网」。整页是从开发机取、每台设备内容都一样的壳
# 页面（ext.html，/ 不带 sid）：同一台设备 60 秒内只向开发机要一次，其余请求直接从
# 内存回。反复重载不再耗上行，页面也秒开——比限流温和，且不会把用户挡在门外
# （18:44 加过一版「每分钟 6 次给退让页」的闸门，结果自己成了第二道墙，已撤）。
# 2026-11-18 晚：60s → 6h。缓存以前根本没生效（见 _page_cache_maybe）；生效后又改成
# 跨设备共用（键里去掉 dev，页面 HTML 已与设备无关），所以不再按「分钟」过期——
# 过期一次就是一台慢链路机器重拉 611KB、手机干等十几秒。版本升级会换键（键里带 host ver）；
# 同版本内的页面改动由 pverCheck 的 ?pv= 硬刷兜住（它跳过缓存、顺手刷新缓存）。
PAGE_CACHE_TTL = 6 * 3600
_PAGE_CACHE = {}   # "uid|hostver|/" -> (ts, status, body, headers)
_PAGE_CACHE_MAX = 64   # 一份明文页 ~640KB：封顶，别让 relay 吃光内存


def _page_cache_get(key):
    e = _PAGE_CACHE.get(key)
    if e and time.time() - e[0] < PAGE_CACHE_TTL:
        return e
    _PAGE_CACHE.pop(key, None)
    return None


def _page_cache_put(key, out):
    if len(_PAGE_CACHE) >= _PAGE_CACHE_MAX:   # 先清过期键，再挤掉最旧的，别让字典无上限长
        now = time.time()
        for k in [k for k, v in _PAGE_CACHE.items() if now - v[0] >= PAGE_CACHE_TTL]:
            _PAGE_CACHE.pop(k, None)
        while len(_PAGE_CACHE) >= _PAGE_CACHE_MAX:
            _PAGE_CACHE.pop(min(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k][0]), None)
    _PAGE_CACHE[key] = (time.time(), out.status, out.body, dict(out.headers))


def _pcache_product(request):
    """整页缓存的产品线维度。页面 HTML 里的标题/favicon/页头图标是开发机按客户端
    产品线服务端注入的，缓存不分线时 CodeBoss 先开一次，AgentBoss 拿到的就是
    CodeBoss 那份（2026-11-20：装好 0.1.12 还是 CodeBoss 图标，开发机连页面请求
    都没收到）。信号来源与 _fwd_headers 一致：?product= / X-Product / cb_product。"""
    pr = ((request.query.get("product") or request.headers.get("X-Product")
           or request.cookies.get("cb_product") or "").strip().lower())
    return pr if pr in ("codeboss", "agentboss") else ""


def _pcache_key(uid, dver, dev, product):
    """uid | 开发机版本 | 老 host 按设备分 | 产品线 | /"""
    return "%s|%s|%s|%s|/" % (uid, dver,
                              "" if _ver_at_least(dver, "0.9.17") else dev,
                              product)


def _ver_at_least(v, want):
    """v >= want？比不了（空串/带后缀）就当不够新——宁可退回按设备分缓存。"""
    try:
        a = [int(x) for x in str(v or "").split(".")]
        b = [int(x) for x in want.split(".")]
    except ValueError:
        return False
    n = max(len(a), len(b))
    return (a + [0] * (n - len(a))) >= (b + [0] * (n - len(b)))


def _page_cache_maybe(key, out):
    """把一次隧道整页响应存进缓存（key 为空＝不是整页，直接跳过）。

    2026-11-18 晚修：0.9.16 起 host 在隧道里必压（_tunnel_body 真 gzip），而以前的
    条件是「没带压缩头」才缓存——于是这缓存一次也没存进去过（aliyun 切台每次等十几秒
    就是这么来的：611KB 从开发机重拉，gzip 后 244KB 压在那条 ~17KB/s 的上行上）。
    现在先把真 gzip 解回明文再存；压缩交给 _maybe_gzip 每次响应现做。
    解不开（假 gzip/半截体）就不缓存，原样放行。"""
    if (not key or out.status != 200
            or not (out.headers.get("Content-Type") or "").startswith("text/html")):
        return
    b = out.body or b""
    if (out.headers.get("Content-Encoding") or "").lower() == "gzip":
        if b[:2] != b"\x1f\x8b":
            return                      # 假 gzip：_repair_fake_gzip 已处理，这里不碰
        try:
            out.body = zlib.decompress(b, 31)
        except zlib.error:
            return
        out.headers.pop("Content-Encoding", None)
    if out.body:
        _page_cache_put(key, out)


def _repair_fake_gzip(out):
    """0.9.16 的 host 把 zlib.compress() 出的 zlib 流标成 Content-Encoding: gzip
    （server.py 的 _tunnel_body）。Chromium 解不开、也不报错，页面永远加载不完——
    壳 6 秒判离线 → 3 秒重试 → 无限循环。2026-11-18 17:00 全站断网就是它。
    这里认出来解掉，交给 _maybe_gzip 重新压成真 gzip；解不开就原样放行。"""
    if (out.headers.get("Content-Encoding") or "").lower() != "gzip":
        return
    b = out.body or b""
    if len(b) < 2 or b[:2] == b"\x1f\x8b":      # 已经是真 gzip 容器
        return
    try:
        raw = zlib.decompress(b)
    except zlib.error:
        try:
            raw = zlib.decompress(b, -15)       # 裸 deflate
        except zlib.error:
            return
    out.body = raw
    out.headers.pop("Content-Encoding", None)


def _maybe_gzip(request, out):
    """整页压缩兜底：enable_compression 要跟客户端协商，老 WebView 或被中间代理去掉
    Accept-Encoding 时会静默原样发（2026-11-18 线上实测整页 653KB 一次）。这里自己压，
    不靠协商。host 已在隧道内压过的（Content-Encoding 已带）不能再压，否则解出双重 gzip。"""
    if out.headers.get("Content-Encoding") or len(out.body or b"") < 2048:
        return
    ae = (request.headers.get("Accept-Encoding") or "").lower()
    if "gzip" in ae or "deflate" in ae:
        # 必须用 gzip 容器（wbits=31）：zlib.compress() 出的是 zlib 流（\x78\x9c），
        # 标成 gzip 浏览器解不开 → ERR_CONTENT_DECODING_FAILED → 壳当场切离线页。
        co = zlib.compressobj(6, zlib.DEFLATED, 31)
        out.body = co.compress(out.body) + co.flush()
        out.headers["Content-Encoding"] = "gzip"


async def _tunnel_plain(request, ws, dev):
    """整包隧道路径：req 帧 → 等单帧 res → 原样回给浏览器（一行没动，只是从 tunnel 抽出共用）。"""
    pcache_key = None
    if request.method == "GET" and request.path in ("/", "/d"):
        # 跨设备共用（2026-11-18，0.9.17+ 的 host）：页面 HTML 已与设备无关（目录下拉走
        # /api/ext_sessions），所以键里不放 dev——同账号、同 host 版本的机器共用一份。
        # 老 host（<0.9.17）页面里还嵌着本机目录，共用会把 A 机的目录发给 B 机，仍按 dev 分。
        # 版本进键是为了新老 API 面不串。带 pv= 的是 pverCheck 的硬刷，跳过缓存取新页并刷新缓存。
        try:
            _row = _db(request.app).execute("SELECT ver FROM devices WHERE id=?", (dev,)).fetchone()
            _dver = (_row[0] if _row else "") or ""
        except Exception:
            _dver = ""
        # /d 在隧道里被重写成 /，同一份页面：键里统一写 "/"，切台和直接进页共用一份。
        # 产品线进键：同一账号的两条端打开的页面标题/图标不同（2026-11-20）。
        pcache_key = _pcache_key(request.get("_cb_uid"), _dver, dev,
                                 _pcache_product(request))
        hit = None if "pv" in request.query else _page_cache_get(pcache_key)
        if hit is not None:
            r = web.Response(status=hit[1], body=hit[2], headers=dict(hit[3]))
            r.headers["Cache-Control"] = "no-store"
            _maybe_gzip(request, r)
            bgd_cookie(r, request, dev)   # 壳的提醒凭据：整页响应里顺手种上
            return r
    rid = request.app["rid"] = request.app.get("rid", 0) + 1
    body = await request.read()
    tunnel_add(len(body), 0, 1)
    p = request.rel_url.raw_path_qs
    if request.path == "/d" and p.startswith("/d"):
        p = "/" + p[2:]    # 入口 /d 是 relay 的，开发机页面挂在 /
    frame = {"t": "req", "id": rid, "m": request.method, "p": p,
             "headers": _fwd_headers(request),
             "body": base64.b64encode(body).decode() if body else ""}
    fut = asyncio.get_event_loop().create_future()
    request.app["pending"][rid] = (fut, dev)
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json(frame)
        res = await asyncio.wait_for(fut, REQ_TIMEOUT)
    except asyncio.TimeoutError:
        request.app["pending"].pop(rid, None)
        return web.json_response({"error": "开发机响应超时"}, status=504)
    except Exception as e:
        request.app["pending"].pop(rid, None)
        return web.json_response({"error": "隧道错误: %s" % e}, status=502)
    out = web.Response(status=int(res.get("code") or 502),
                       body=base64.b64decode(res.get("body") or ""))
    if (res.get("ct") or "").startswith("text/html") and dev:
        bgd_cookie(out, request, dev)   # 页面从开发机来，壳的提醒凭据由中继补种
    tunnel_add(0, len(out.body or b""))
    _u = request.get("_cb_uid")
    if _u:
        usage_bytes_add(_db(request.app), _u, int(time.time() // 60), "relay",
                        len(body), len(out.body or b""), 1,
                        cid=request.get("_cb_cid") or "",
                        dev=request.get("_cb_dev") or "")
        path_bytes_add(request.path, _u, request.get("_cb_cid") or "",
                       len(body) + len(out.body or b""), 1)
    out.headers["Content-Type"] = res.get("ct") or "application/octet-stream"
    for k, v in (res.get("headers") or {}).items():
        if k.lower() not in HOP_BY_HOP | {"content-type", "set-cookie"}:
            out.headers[k] = v
    # 整页压缩：先修掉 0.9.16 host 的假 gzip，再自己压（不靠 aiohttp 协商，
    # 线上实测协商不上时 653KB 原样发）。
    _repair_fake_gzip(out)
    _page_cache_maybe(pcache_key, out)
    _maybe_gzip(request, out)
    return out


# ---------- 管理 CLI（在 relay 机器上跑，直接读写 relay.db） ----------
def admin_cli(db, argv):
    """账号/设备管理。注册是用户自助（relay 网页 /r/「注册」，需邮箱验证码），管理走这里：
      relay.py --db <relay.db> --admin list                    列账号/设备/待配对码
      relay.py --db <relay.db> --admin reset <用户名> <新密码>  改密码并踢下线全部登录
      relay.py --db <relay.db> --admin kick  <用户名>           仅踢下线（不改密码）
      relay.py --db <relay.db> --admin del   <用户名>           删账号（其设备解绑，需重新配对）
      relay.py --db <relay.db> --admin setemail <用户名> <邮箱>  给老账号绑邮箱（绑后才能自助找回密码）
      relay.py --db <relay.db> --admin adduser <用户名> <密码> [邮箱]  手工建号（没配 SMTP 时用）
      relay.py --db <relay.db> --admin unbind <device_id>      解绑设备（保留登记，可重新配对）
      relay.py --db <relay.db> --admin freeze <device_id>      冻结设备（只读：可看历史，禁止一切操作）
      relay.py --db <relay.db> --admin unfreeze <device_id>    解冻设备
      relay.py --db <relay.db> --admin paircode <device_id>    给已有设备补发配对码（15 分钟）
      relay.py --db <relay.db> --admin mailtest <收件邮箱>     试发一封验证码邮件（排查 SMTP）
      relay.py --db <relay.db> --admin smstest <手机号>        试发一条短信验证码（排查阿里云短信）
    设备的彻底删除是账号主在手机页自助操作（清 relay 记录 + 通知开发机清除本机注册）。
    """
    cmd = argv[0] if argv else "list"
    now = time.time()
    if cmd == "smstest":
        if len(argv) < 2:
            print("用法: --admin smstest <手机号>")
            return 2
        if not SMS_ON:
            print("✗ 本进程没读到 RELAY_ALI_*（AK_ID/AK_SECRET/SIGN_NAME/TEMPLATE_CODE）")
            return 1
        phone = _norm_phone(argv[1])
        if not _PHONE_RE.match(phone):
            print("✗ 手机号格式不正确")
            return 2
        try:
            _sms_send_sync(phone, secrets.token_hex(8))
        except Exception as e:
            print("✗ 发送失败：%s: %s" % (type(e).__name__, str(e)[:300]))
            return 1
        print("✓ 已提交给阿里云短信（签名 %s / 模板 %s），看手机收件" % (ALI_SIGN_NAME, ALI_TEMPLATE))
        return 0
    if cmd == "mailtest":
        if len(argv) < 2:
            print("用法: --admin mailtest <收件邮箱>")
            return 2
        if not SMTP_HOST:
            print("✗ 本进程没读到 RELAY_SMTP_HOST（检查 systemd 的 Environment= 并 daemon-reload）")
            return 1
        print("SMTP: host=%s port=%s user=%s from=%s pass=%s"
              % (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_FROM,
                 "已配置" if SMTP_PASS else "未配置"))
        try:
            _mail_send(argv[1], "CodeBoss 发信测试", "这是一封测试邮件，收到即表示 SMTP 正常。")
        except Exception as e:
            print("✗ 发送失败：%s: %s" % (type(e).__name__, str(e)[:300]))
            return 1
        print("✓ 已提交给 SMTP 服务器（看收件箱/垃圾箱）")
        return 0
    if cmd == "list":
        print("账号：")
        for uid, name, email, phone, created in db.execute(
                "SELECT id,name,email,phone,created FROM users ORDER BY id"):
            n = db.execute("SELECT COUNT(*) FROM devices WHERE owner=?", (uid,)).fetchone()[0]
            print("  #%d %-16s %-24s %-14s 设备 %d 台  注册于 %s"
                  % (uid, name, email or "（未绑邮箱）", phone or "", n,
                     time.strftime("%Y-%m-%d", time.localtime(created))))
        print("设备：")
        for did, dname, owner, seen, fz in db.execute(
                "SELECT d.id,d.name,u.name,d.last_seen,d.frozen FROM devices d "
                "LEFT JOIN users u ON u.id=d.owner ORDER BY d.last_seen DESC"):
            print("  %s %-16s 归属:%-12s 最后心跳:%s%s"
                  % (did, dname or "", owner or "（未配对）",
                     time.strftime("%m-%d %H:%M", time.localtime(seen)) if seen else "—",
                     "  ❄已冻结" if fz else ""))
        print("待用配对码：")
        for code, did, exp in db.execute(
                "SELECT code,device_id,expires FROM pair_codes WHERE used=0 AND expires>?", (now,)):
            print("  %s → %s（%d 分钟后过期）" % (code, did, int((exp - now) / 60)))
        return 0
    if cmd in ("reset", "kick"):
        if len(argv) < 2 or (cmd == "reset" and len(argv) < 3):
            print("用法: --admin %s <用户名> [新密码]" % cmd)
            return 2
        row = db.execute("SELECT id FROM users WHERE name=?", (argv[1],)).fetchone()
        if not row:
            print("账号不存在：%s" % argv[1])
            return 1
        if cmd == "reset":
            pw = argv[2]
            if len(pw) < 6:
                print("密码至少 6 位")
                return 2
            salt = secrets.token_hex(8)
            db.execute("UPDATE users SET pass_hash=?,salt=?,sess_epoch=sess_epoch+1 WHERE id=?",
                       (hash_pw(pw, salt), salt, row[0]))
        else:
            db.execute("UPDATE users SET sess_epoch=sess_epoch+1 WHERE id=?", (row[0],))
        db.commit()
        print("OK：%s 已%s，全部登录态作废" % (argv[1], "改密码并踢下线" if cmd == "reset" else "踢下线"))
        return 0
    if cmd == "adduser":
        if len(argv) < 3:
            print("用法: --admin adduser <用户名> <密码> [邮箱]")
            return 2
        name, pw = argv[1].strip(), argv[2]
        email = (argv[3].strip().lower() if len(argv) > 3 else "")
        if len(name) < 2 or len(pw) < 6:
            print("用户名≥2 字符，密码≥6 位")
            return 2
        if db.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
            print("账号已存在：%s（要改密码用 --admin reset）" % name)
            return 1
        salt = secrets.token_hex(8)
        db.execute("INSERT INTO users(name,pass_hash,salt,created,sess_epoch,email) "
                   "VALUES(?,?,?,?,0,?)", (name, hash_pw(pw, salt), salt, time.time(), email))
        db.commit()
        print("OK：已创建账号 %s%s（手机用 /r/ 登录即可）"
              % (name, "（邮箱 %s）" % email if email else "（未绑邮箱，无法自助找回密码）"))
        return 0
    if cmd == "del":
        row = db.execute("SELECT id FROM users WHERE name=?", (argv[1] if len(argv) > 1 else "",)).fetchone()
        if not row:
            print("账号不存在")
            return 1
        db.execute("UPDATE devices SET owner=NULL WHERE owner=?", (row[0],))
        db.execute("DELETE FROM users WHERE id=?", (row[0],))
        db.commit()
        print("OK：账号 %s 已删除，其设备已解绑（重新配对可再纳入）" % argv[1])
        return 0
    if cmd == "unbind":
        db.execute("UPDATE devices SET owner=NULL WHERE id=?", (argv[1] if len(argv) > 1 else "",))
        db.commit()
        print("OK：设备已解绑")
        return 0
    if cmd in ("freeze", "unfreeze"):
        did = argv[1] if len(argv) > 1 else ""
        if not db.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
            print("设备不存在：%s" % did)
            return 1
        db.execute("UPDATE devices SET frozen=? WHERE id=?", (1 if cmd == "freeze" else 0, did))
        db.commit()
        print("OK：设备已%s" % ("冻结（只读：可看历史，禁止操作）" if cmd == "freeze" else "解冻"))
        return 0
    if cmd == "setemail":
        if len(argv) < 3 or not _EMAIL_RE.match(argv[2]):
            print("用法: --admin setemail <用户名> <邮箱>")
            return 2
        row = db.execute("SELECT id FROM users WHERE name=?", (argv[1],)).fetchone()
        if not row:
            print("账号不存在：%s" % argv[1])
            return 1
        db.execute("UPDATE users SET email=? WHERE id=?", (argv[2].strip().lower(), row[0]))
        db.commit()
        print("OK：已绑定邮箱 %s（该账号现在可以自助找回密码）" % argv[2])
        return 0
    if cmd == "paircode":
        did = argv[1] if len(argv) > 1 else ""
        if not db.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
            print("设备不存在：%s" % did)
            return 1
        code = secrets.token_hex(6)
        db.execute("INSERT INTO pair_codes(code,device_id,created,expires) VALUES(?,?,?,?)",
                   (code, did, now, now + PAIR_TTL))
        db.commit()
        print("配对码：%s（15 分钟有效，手机打开 /r/?p=%s 登录后自动配对）" % (code, code))
        return 0
    print(__doc__ and admin_cli.__doc__)
    return 2


# ---------- main ----------
# ---------- P2P 直连牵线：候选下发 / 短命票据签发 / WebRTC 信令 ----------
# host 有 token 但客户端永远拿不到（隧道由 relay 注入）；直连改用这里的短命票据：
# dt = b64url("dev|exp|nonce[|fp]|sig")，k=HMAC(device_secret,"cb-direct-v1")——
# host 用同一材料验签（见 server.py _dt_verify），票据放 query 免 CORS 预检。
# 设备录了指纹就把 fp 签进去，host 侧比对本机指纹（换机器旧票失效）。
DT_TTL = 600


def _sel_dev(request):
    """客户端侧鉴权：uid + 选中设备行；未登录/未选设备/无权 → None。"""
    uid = uid_of(request)
    if uid is None:
        return None
    dev = request.cookies.get("cb_dev") or ""
    return (uid, own_device(_db(request.app), uid, dev))


def _dt_sign(app, dev):
    row = _db(app).execute("SELECT secret, fp FROM devices WHERE id=?", (dev,)).fetchone()
    if not row:
        return None, 0
    exp = int(time.time()) + DT_TTL
    nonce = secrets.token_hex(8)
    msg = "%s|%d|%s" % (dev, exp, nonce)
    # 票据绑定开发机指纹：同 secret 的克隆/恢复机器拿旧票也连不上。
    # 老设备没录指纹（fp 空）签旧格式，免得升级瞬间直连全断。
    fp = "".join(ch for ch in (row[1] or "").strip() if ch != "|")
    if fp:
        msg += "|" + fp
    key = hmac.new(row[0].encode(), b"cb-direct-v1", hashlib.sha256).digest()
    sig = hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]
    dt = base64.urlsafe_b64encode(("%s|%s" % (msg, sig)).encode()).decode().rstrip("=")
    return dt, exp


def _turn_cfg(app):
    """下发给浏览器的 ICE 配置。默认只给 STUN；配了 TURN 就补上（凭据仅登录用户可见）。"""
    ice = [{"urls": "stun:stun.miwifi.com:3478"},
           {"urls": "stun:stun.chat.bilibili.com:3478"},
           {"urls": "stun:stun.l.google.com:19302"}]
    if TURN_URLS:
        for u in TURN_URLS:
            c = {"urls": u}
            if TURN_USER:
                c["username"] = TURN_USER
                c["credential"] = TURN_PASS
            ice.append(c)
    return ice


def h_direct_info(request):
    sel = _sel_dev(request)
    if not sel or not sel[1]:
        return web.json_response({"ok": 0, "error": "未登录或未选设备"}, status=401)
    dev = sel[1][0]
    ws = request.app["links"].get(dev)
    info = request.app["direct"].get(dev)
    if not ws or ws.closed or not info:
        return web.json_response({"ok": False, "online": bool(ws and not ws.closed)})
    # 局域网候选只在「手机与 host 同一公网出口」时下发：不同出口(4G/代理)时私网 IP
    # 对手机不可达，或探针经代理撞上不可靠路径——误建直连后整页半死
    # (2026-11-18 aliyun 内网 IP 事故：切台后壳直接进断网页，App 整个不能用)。
    # 不同出口：lan 置空，页面拿空候选直接走中继；rtc/ice 照旧(广域网打洞本就跨网用)。
    hip = (request.app.get("link_ip") or {}).get(dev) or ""
    cip = request.remote or ""
    lan = list(info["lan"]) if (hip and cip and hip == cip) else []
    return web.json_response({"ok": True, "lan": lan, "port": info["port"],
                              "rtc": info["rtc"], "rtc_why": info.get("rtc_why") or "",
                              "ice": _turn_cfg(request.app),
                              "age": int(time.time() - info["ts"])})


_ASR_USED = {}   # (dev, "YYYY-MM-DD") → 已用秒数；进程内计，重启清零（够用，不落库）


def _asr_quota(db, dev, add=0):
    """语音转写用量：按「用量主体」计日/月秒数（账号多台开发机共用一份；
    匿名设备按 d<dev> 自己的免费额度）。设备还没归属账号时也算设备主体——
    2026-11-19 免注册免费版起，未归属设备不是「兜底防白嫖」的灰色地带，
    而是有身份的免费用户，额度走 plans.free（asr_sec_day）。
    返回 (日已用, 日上限, 月已用, 月上限)；上限 0 = 不限。落 SQLite，重启不丢。"""
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    uid = row[0] if row else None
    subj = subject_of(db, uid, dev)
    lim = plan_limits(db, subj)
    capd, capm = int(lim.get("asr_sec_day") or 0), int(lim.get("asr_sec_month") or 0)
    kd = "asr:%s:d:%s" % (subj, time.strftime("%Y-%m-%d"))
    km = "asr:%s:m:%s" % (subj, time.strftime("%Y-%m"))
    if add:
        usage_add(db, kd, add)
        usage_add(db, km, add)
    return usage_get(db, kd), capd, usage_get(db, km), capm


def _asr_dev(request):
    """三种调用方：① 浏览器（登录态 + cb_dev 选中设备）② 开发机代转
    （X-CB-Dev/X-CB-Sec）③ 匿名设备会话（cb_dev_sess）。都没有 → None。"""
    hd = request.headers.get("X-CB-Dev") or ""
    if hd:
        sec = request.headers.get("X-CB-Sec") or ""
        row = _db(request.app).execute("SELECT secret FROM devices WHERE id=?", (hd,)).fetchone()
        if row and hmac.compare_digest(row[0], sec):
            return hd
        return None
    sel = _sel_dev(request)
    if sel and sel[1]:
        return sel[1][0]
    return dev_of(request)


def _asr_call(data_b64, prompt, model=None):
    """一次 DashScope 调用：audio + 可选 Context（system 消息的 text，Qwen3-ASR 的
    上下文增强=热词方案）→ (text, err)。"""
    import urllib.request
    messages = []
    if prompt:
        messages.append({"role": "system", "content": [{"text": prompt}]})
    messages.append({"role": "user", "content": [{"audio": "data:audio/wav;base64," + data_b64}]})
    body = json.dumps({
        "model": model or ASR_MODEL,
        "input": {"messages": messages},
        "parameters": {"asr_options": {"language": "zh"}},
    }).encode()
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        data=body, method="POST",
        headers={"Authorization": "Bearer " + ASR_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    try:
        return d["output"]["choices"][0]["message"]["content"][0]["text"], ""
    except Exception:
        return "", "识别返回异常: " + json.dumps(d, ensure_ascii=False)[:200]


def _asr_http_code(e):
    """异常对应的 HTTP 状态码（非 HTTP 错误返回 0）。"""
    import urllib.error
    return int(getattr(e, "code", 0) or 0) if isinstance(e, urllib.error.HTTPError) else 0


def _asr_retryable(e):
    """网络抖动 / 超时 / 429 / 5xx 值得重试；其它 4xx（参数、鉴权）重试没用。"""
    c = _asr_http_code(e)
    if not c:
        return True
    return c in (408, 429) or c >= 500


def _asr_call_retry(data_b64, prompt, attempts=3):
    """_asr_call + 退避重试 + 模型降级。
    同一个模型上：DashScope 偶发超时/限流/5xx 时自动再试；
    模型本身不可用（404/模型下架/无权限/欠费）或重试仍失败：换 ASR_MODELS 里的下一个。"""
    last = None
    for model in (ASR_MODELS or [ASR_MODEL]):
        delay = 0.8
        for i in range(attempts):
            try:
                return _asr_call(data_b64, prompt, model)
            except Exception as e:
                last = e
                code = _asr_http_code(e)
                # 参数/鉴权/模型不存在：重试和换模型都没用，别白等；
                # 但 429/5xx/网络抖动值得在同一模型上退避重试。
                if code in (400, 401, 403, 404):
                    break
                if i + 1 >= attempts or not _asr_retryable(e):
                    break
                print("[relay] asr retry %d/%d after %r" % (i + 1, attempts - 1, e), flush=True)
                time.sleep(delay)
                delay = min(3.0, delay * 2)
        if len(ASR_MODELS or []) > 1:
            print("[relay] asr 模型 %s 不可用，换下一个：%s"
                  % (model, str(last)[:120]), flush=True)
    raise last or RuntimeError("没有可用的语音识别模型")


def _asr_norm(s):
    return re.sub(r"[\s,，。．.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_\-]+", "",
                  str(s or "")).lower()


def _asr_is_prompt_echo(text, prompt):
    """静音/极短录音时模型会把 Context（场景提示词）原样吐回来——那不是用户说的话。
    提示词本身很长，按去空白/标点后的包含关系判定就够稳。"""
    t, p = _asr_norm(text), _asr_norm(prompt)
    if not t or not p or len(t) < 8:
        return False
    return t in p or p in t


# ── 语音识别「越用越懂项目」──────────────────────────────────────────────
# 粒度：(uid, dev, proj)，proj 是开发机算的 git 根目录名（不同机器同名项目分开）。
# 三个来源：① 用户改稿（下面 _asr_learn）② 项目扫描/摘要（asr_proj）③ 手工词表。
# 提示词只有 400 字且多轮 context 不被支持，所以词表还要参与「转写后纠正」：
# 语义相近的 ASCII 串按词表改写（_asr_fix_text），这才是真正稳定起效的部分。
def _proj_key_ok(p):
    return bool(p) and len(p) <= 40 and re.match(r"^[A-Za-z0-9._\-]+$", p) is not None


def _asr_prompt_for(db, uid, dev, proj, base=None):
    """拼这一句的提示词：基础场景 + 项目摘要 + 项目词表，总长 ≤400 字。
    没有任何项目信息时退回全局 ASR_PROMPT。"""
    base = base if base is not None else ASR_PROMPT
    if not base:
        return ""
    if not (db is not None and uid and dev and _proj_key_ok(proj)):
        return base
    try:
        row = db.execute("SELECT summary FROM asr_proj WHERE uid=? AND dev=? AND proj=?",
                         (uid, dev, proj)).fetchone()
        sm = re.sub(r"\s+", " ", (row[0] if row else "") or "").strip()[:110]
        terms = [r[0] for r in db.execute(
            "SELECT term FROM asr_gloss WHERE uid=? AND dev=? AND proj=? "
            "ORDER BY weight DESC, last DESC LIMIT 80", (uid, dev, proj)).fetchall()]
    except Exception as e:
        print("[relay] asr prompt build failed: %r" % (e,), flush=True)
        return base
    parts = [base]
    n = len(base)
    if sm:
        seg = "项目：" + sm + "。"
        if n + len(seg) <= ASR_PROMPT_BUDGET - 8:
            parts.append(seg)
            n += len(seg)
    picked = []
    for t in terms:
        t = (t or "").strip()
        if not t or t in picked:
            continue
        if n + len(t) + 1 > ASR_PROMPT_BUDGET - 8:
            break
        picked.append(t)
        n += len(t) + 1
    if picked:
        parts.append("项目词：" + " ".join(picked) + "。")
    return "".join(parts)


def _asr_term_ok(s):
    """能不能当「项目术语」：必须带 ASCII 字母/数字（中文同义改写不当术语），≤30 字。"""
    s = str(s or "").strip()
    return bool(s) and len(s) <= 30 and re.search(r"[A-Za-z0-9]", s) is not None


def _asr_learn(db, uid, dev, proj, pairs, rej=None):
    """把「用户改稿」里的近义替换学成术语/别名。只收近似改错：
    更正后的词带 ASCII、且和原词相似（归一化后相等或相似度 ≥0.6）；
    整段重写、中文润色一律不学，避免词表塞满垃圾。返回新学条数。
    rej: 传个 list 进来就收集每对被丢掉的原因码（排查「一条都没学到」用）。"""
    if not (db and uid and _proj_key_ok(proj)):
        if rej is not None:
            rej.append("nopj" if not _proj_key_ok(proj) else "nouser")
        return 0
    now = time.time()
    learned = 0
    for pair in (pairs or [])[:12]:
        try:
            w, r = pair[0], pair[1]
        except Exception:
            if rej is not None:
                rej.append("bad")
            continue
        w = re.sub(r"\s+", " ", str(w or "")).strip()[:40]
        r = re.sub(r"\s+", " ", str(r or "")).strip()[:30]
        if not w or not r:
            if rej is not None:
                rej.append("empty")
            continue
        if w == r:
            if rej is not None:
                rej.append("same")
            continue
        if not _asr_term_ok(r):
            if rej is not None:
                rej.append("noascii")
            continue
        nw, nr = _asr_norm(w), _asr_norm(r)
        if not nr:
            if rej is not None:
                rej.append("nonorm")
            continue
        if nw != nr:
            if len(nw) < 2:
                if rej is not None:
                    rej.append("short")
                continue
            if difflib.SequenceMatcher(None, nw, nr).ratio() < 0.6:
                if rej is not None:
                    rej.append("far")
                continue
        try:
            db.execute(
                "INSERT INTO asr_gloss(uid,dev,proj,term,weight,source,first,last) "
                "VALUES(?,?,?,?,1,'fix',?,?) "
                "ON CONFLICT(uid,dev,proj,term) DO UPDATE SET weight=weight+1, last=excluded.last",
                (uid, dev, proj, r, now, now))
            if nw != nr:      # 只差大小写/空格就不记别名，词表本身已能纠
                db.execute(
                    "INSERT INTO asr_alias(uid,dev,proj,wrong,right,hits,last) "
                    "VALUES(?,?,?,?,?,1,?) "
                    "ON CONFLICT(uid,dev,proj,wrong,right) DO UPDATE SET hits=hits+1, last=excluded.last",
                    (uid, dev, proj, w, r, now))
            learned += 1
        except Exception as e:
            print("[relay] asr learn failed: %r" % (e,), flush=True)
    if learned:
        try:
            db.commit()
        except Exception as e:
            print("[relay] asr learn commit failed: %r" % (e,), flush=True)
        try:   # 每个项目最多留 200 个词：按权重、最近使用淘汰（最老的排在 OFFSET 之后）
            db.execute(
                "DELETE FROM asr_gloss WHERE rowid IN ("
                "SELECT rowid FROM asr_gloss WHERE uid=? AND dev=? AND proj=? "
                "ORDER BY weight DESC, last DESC LIMIT -1 OFFSET 200)",
                (uid, dev, proj))
            db.execute(
                "DELETE FROM asr_alias WHERE rowid IN ("
                "SELECT rowid FROM asr_alias WHERE uid=? AND dev=? AND proj=? "
                "ORDER BY hits DESC, last DESC LIMIT -1 OFFSET 200)",
                (uid, dev, proj))
            db.commit()
        except Exception as e:
            print("[relay] asr prune failed: %r" % (e,), flush=True)
    return learned


def _asr_fix_text(db, uid, dev, proj, text):
    """按学到的词表纠正转写文本（确定性规则，不额外调模型）。
    ① 别名：学到的错写法 → 正确写法（忽略大小写/词间空格），命中 ≥2 次才用；
    ② 词表近似：ASCII 串和术语只差大小写/一两个字母（≥0.82）就改写；
    ③ 连写的两个词（Web Socket）合并后精确命中术语也改写。
    词表门槛：用户改稿学到 ≥2 次，或来自仓库扫描/手工（这些本身就是正确写法）。"""
    if not text or not (db and uid and _proj_key_ok(proj)):
        return text
    try:
        aliases = db.execute(
            "SELECT wrong,right FROM asr_alias WHERE uid=? AND dev=? AND proj=? AND hits>=2 "
            "ORDER BY hits DESC LIMIT 100", (uid, dev, proj)).fetchall()
        terms = [r[0] for r in db.execute(
            "SELECT term FROM asr_gloss WHERE uid=? AND dev=? AND proj=? "
            "AND (weight>=2 OR source IN ('scan','manual')) "
            "ORDER BY weight DESC, last DESC LIMIT 80", (uid, dev, proj)).fetchall()]
    except Exception:
        return text
    out = text
    for wrong, right in aliases:
        w = _asr_norm(wrong)
        if len(w) < 2:
            continue
        pat = (r"(?<![0-9A-Za-z])"
               + r"\s*".join(re.escape(x) for x in wrong.split())
               + r"(?![0-9A-Za-z])")
        try:
            out = re.sub(pat, lambda m: right, out, flags=re.I)
        except Exception:
            continue
    tn = [(t, _asr_norm(t)) for t in terms]
    tn = [(t, n) for t, n in tn if len(n) >= 4 and re.search(r"[a-z]", n)]
    if not tn:
        return out
    # 「publish host」这种把文件名说成两个词的：按去扩展名的词干再对一次
    # （server 单独出现不会命中——只在这个多词短语通道里比）。
    stems = [(t, _asr_norm(os.path.splitext(t)[0])) for t, n in tn
             if "." in t and len(_asr_norm(os.path.splitext(t)[0])) >= 4]

    RUN = r"[A-Za-z][A-Za-z0-9._+\-]*"
    SEP = r"(?:\s*[点之的]\s*|\s+)"          # 「ext 点 html」这类口述中间夹的虚词

    def fix_phrase(m):
        txt = m.group(0)
        joined = _asr_norm(re.sub(r"[点之的\s]+", "", txt))
        for t, n in tn:
            if joined == n:
                return t
        if len(txt.split()) >= 2:
            for t, s in stems:
                if joined == s:
                    return t
        return txt

    try:
        out = re.sub(RUN + SEP + RUN + r"(?:" + SEP + RUN + r")?", fix_phrase, out)
    except Exception:
        pass

    def fix_run(m):
        run = m.group(0)
        rn = _asr_norm(run)
        if len(rn) < 4:
            return run
        best, best_r = None, 0.0
        for t, n in tn:
            if rn == n:
                return t
            if abs(len(n) - len(rn)) > 1 or n[0] != rn[0]:
                continue
            r = difflib.SequenceMatcher(None, rn, n).ratio()
            if r > best_r:
                best, best_r = t, r
        if best and best_r >= 0.82:
            return best
        # 「publish-host」这类说了一半的文件名：只认带分隔符/够长的，避免把
        # 普通单词 server 改成 server.py。
        if "-" in run or "_" in run or len(rn) >= 9:
            for t, s in stems:
                if rn == s:
                    return t
        return run

    return re.sub(r"[A-Za-z][A-Za-z0-9._+\-]{3,}", fix_run, out)


# ── 乱环境自动纠错 ─────────────────────────────────────────────────────
# 环境吵的时候，ASR 不只是听错技术词：整句都会变成同音字，甚至凭空多出词
# （实测「发布上线」被听成「钱钱拿去啊」，「发送」重复了好几遍）。词表纠正
# （_asr_fix_text）只认术语，救不了这种情况。这里再跑一次**文本纠错**：把
# 「上下文（屏幕上最近的对话/输入框）」和转写一起给模型，让它按上下文把
# 同音错字、噪声幻觉词改回来。
# 三条底线：
#   ① 只改错字、不增删意思——上下文只用来判断「这个词该是哪个词」；
#   ② 任何异常、可疑输出都退回原文（纠错是加分项，绝不能把转写搞坏）；
#   ③ 确认阶段（只说「发送/取消」）不纠错：那两句被改掉，整个流程就断了。
FIX_MODEL = FIX_MODEL_DEFAULT
FIX_TIMEOUT = 6           # 纠错最多等 6 秒：转写已经等了几秒，别让人再干等
FIX_MAX_CTX = 320         # 上下文上限（字）
FIX_MAX_TEXT = 300        # 只纠这么长的句子；再长就是整段口述，纠错收益低、风险大
FIX_SYS = (
    "你是语音转写的纠错器。用户用手机对编程助手说话，环境可能有噪声，转写文字里会夹着"
    "同音错字、术语错写，甚至凭空多出的噪声词。请结合上下文把这一句改回用户真正想说的："
    "只纠正错字、术语和噪声词，不增加、不删减用户的意思，不改语气，不加解释；"
    "上下文里出现过的名字（文件名、命令、项目名、术语）优先按上下文的写法；"
    "**不要拿上下文里的句子来代替原句**——上下文只用来判断某个词该写哪个字，"
    "原句的词序和每个信息点都要保留；拿不准的地方原样保留，不要猜；"
    "原句已经通顺就原样输出；只输出纠正后的这一句，不要引号、不要任何前后缀。"
)
FIX_MIN_SIM = 0.55        # 改完和原文差太多 = 模型在重写而不是纠错，宁可退回原文


def _asr_ctx(request):
    """取页面/开发机带来的纠错上下文：header 里的 base64(utf-8)。
    走 header 不走 URL——中文上下文 percent-encode 之后动辄两三 KB，URL 会撞网关上限。
    返回 (ctx, nofix)。"""
    b = (request.headers.get("X-CB-Ctx") or "").strip()
    nf = (request.headers.get("X-CB-Nofix") or "").strip() in ("1", "true", "yes")
    if not b:
        return "", nf
    try:
        c = base64.b64decode(b.encode("ascii", "ignore")).decode("utf-8", "replace")
    except Exception:
        return "", nf
    c = re.sub(r"\s+", " ", c).strip()[:FIX_MAX_CTX]
    return c, nf


def _asr_dedupe(text):
    """吵环境里人会把同一句反复说（「发送。发送。」「取米了。取米了。」）——
    连续重复的整句只留一句。确定性规则、不猜内容，只把看得见的口水去掉。
    只按标点切句去重，不碰词内重复（「看看」「刚刚」是正常说法）。"""
    s = str(text or "")
    if not s:
        return s
    try:
        parts = re.split(r"(?<=[。！？!?，,；;\n])", s)
        out = []
        for p in parts:
            n = _asr_norm(p)
            if n and out and _asr_norm(out[-1]) == n:
                continue
            out.append(p)
        s = "".join(out)
        s = re.sub(r"[，,。；;]\s*(?=[，,。；;])", "", s)   # 去重后留下的空标点
        return s
    except Exception:
        return text


def _asr_fix_ok(src, out):
    """纠错结果的护栏：不像「同一句话的修正版」就整条丢掉。
    模型偶尔会解释、翻成别的语言、把一句话扩成一段——这些都不能进输入框。"""
    if not out:
        return ""
    out = out.strip()
    # 去掉模型爱加的前后缀（引号、代码围栏、「纠正后：」）
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out).strip()
    out = re.sub(r"^(纠正后|纠正|修改后|结果)[：:]\s*", "", out)
    out = out.strip().strip("「」『』\"'“”‘’`").strip()
    if not out or "\n" in out:
        return ""
    a, b = len(_asr_norm(src)), len(_asr_norm(out))   # 按去标点后的长度比
    if b < max(1, a * 0.35) or b > a * 1.9 + 4:
        return ""                      # 缩没了 / 膨胀了：多半不是在纠错
    # 和原文差太多 = 模型拿上下文重写了一句（实测：乱码会被「归纳」成上下文里
    # 出现过的短语）。这种宁可退回原文——错字看得见，编出来的话看不见。
    try:
        import difflib
        if difflib.SequenceMatcher(None, _asr_norm(src), _asr_norm(out)).ratio() < FIX_MIN_SIM:
            return ""
    except Exception:
        pass
    return out


def _asr_fix_llm(text, ctx):
    """按上下文纠正一句转写（阻塞，放 executor）。失败一律返回原文。"""
    import urllib.request
    if not (text and ctx and len(text) <= FIX_MAX_TEXT):
        return text
    try:
        out = None
        last = None
        for model in (FIX_MODELS or [FIX_MODEL]):
            try:
                body = json.dumps({
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 400,
                    "messages": [
                        {"role": "system", "content": FIX_SYS},
                        {"role": "user", "content": "上下文：\n" + ctx + "\n\n转写：" + text},
                    ],
                }).encode()
                req = urllib.request.Request(
                    LLM_BASE, data=body, method="POST",
                    headers={"Authorization": "Bearer " + ASR_KEY,
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=FIX_TIMEOUT) as r:
                    o = json.loads(r.read().decode("utf-8"))
                out = o["choices"][0]["message"]["content"]
                if isinstance(out, list):       # 多模态格式
                    out = "".join(x.get("text", "") for x in out if isinstance(x, dict))
                break
            except Exception as e:
                last = e
                print("[relay] asr fix 模型 %s 失败，换下一个：%s"
                      % (model, str(e)[:100]), flush=True)
        if out is None:
            raise last or RuntimeError("没有可用的纠错模型")
        return _asr_fix_ok(text, str(out or "")) or text
    except Exception as e:
        print("[relay] asr fix skipped: %s" % str(e)[:100], flush=True)
        return text


def _asr_proj_save(db, uid, dev, proj, summary, terms):
    """开发机扫描仓库后推来的项目摘要 + 术语种子。
    摘要只在非空时更新；术语不重复加权（扫描是幂等的），last 刷新即可。
    新词以 weight=2/source=scan 入库——仓库里真实存在的名字可直接参与纠正。"""
    if not (db and uid and _proj_key_ok(proj)):
        return 0
    now = time.time()
    n = 0
    try:
        summary = re.sub(r"\s+", " ", str(summary or "")).strip()[:110]
        if summary:
            db.execute(
                "INSERT INTO asr_proj(uid,dev,proj,summary,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(uid,dev,proj) DO UPDATE SET summary=excluded.summary, updated=excluded.updated",
                (uid, dev, proj, summary, now))
        seen = set()
        for t in (terms or [])[:60]:
            t = re.sub(r"\s+", " ", str(t or "")).strip()
            if not _asr_term_ok(t) or t.lower() in seen:
                continue
            seen.add(t.lower())
            db.execute(
                "INSERT INTO asr_gloss(uid,dev,proj,term,weight,source,first,last) "
                "VALUES(?,?,?,?,2,'scan',?,?) "
                "ON CONFLICT(uid,dev,proj,term) DO UPDATE SET last=excluded.last",
                (uid, dev, proj, t, now, now))
            n += 1
        db.commit()
        db.execute(
            "DELETE FROM asr_gloss WHERE rowid IN ("
            "SELECT rowid FROM asr_gloss WHERE uid=? AND dev=? AND proj=? "
            "ORDER BY (source IN ('scan','manual')) DESC, weight DESC, last DESC LIMIT -1 OFFSET 200)",
            (uid, dev, proj))
        db.commit()
    except Exception as e:
        print("[relay] asr proj save failed: %r" % (e,), flush=True)
        return 0
    return n


def _asr_run(raw, ext, prompt=None):
    """转 16k 单声道 wav → DashScope。阻塞，跑 executor。带场景提示词；若 API 对
    提示词格式不买账（报错），去掉提示词重试一次，保证转写不断。
    prompt 由调用方按 (账号, 开发机, 项目) 拼好；None = 全局默认。"""
    import subprocess
    import tempfile
    import urllib.request
    fx = None
    for p in (os.environ.get("RELAY_FFMPEG"), "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if p and os.path.exists(p):
            fx = p
            break
    if not fx:
        return "", "中继没装 ffmpeg", 0
    dur = 0
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in" + ext)
        dst = os.path.join(td, "out.wav")
        with open(src, "wb") as f:
            f.write(raw)
        p = subprocess.run([fx, "-y", "-loglevel", "error", "-i", src,
                            "-ar", "16000", "-ac", "1", "-f", "wav", dst],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not os.path.exists(dst):
            return "", "ffmpeg 转码失败: " + p.stderr.decode("utf-8", "replace")[:150], 0
        data_b64 = base64.b64encode(open(dst, "rb").read()).decode()
        dur = max(1, int((len(data_b64) * 3 // 4 - 44) / 32000.0))   # 16k*16bit 单声道
    prompt = (ASR_PROMPT if prompt is None else prompt) if _ASR_PROMPT_OK[0] else ""
    try:
        text, err = _asr_call_retry(data_b64, prompt)
        if text and prompt and _asr_is_prompt_echo(text, prompt):
            text = ""        # 没录到人声：模型复述提示词，按「没听清」处理
    except Exception as e:
        text, err = "", "DashScope 调用失败: " + str(e)[:150]
        # 只有参数类 4xx 才说明「带 Context 的请求不被接受」；网络问题别误伤提示词
        if prompt and _asr_http_code(e) in (400, 422):
            _ASR_PROMPT_OK[0] = False   # 以后直接裸调
    if err and prompt:   # 提示词格式不被接受 → 裸调兜底
        try:
            text2, err2 = _asr_call_retry(data_b64, "", attempts=2)
            if not err2:
                print("[relay] asr fallback without prompt ok", flush=True)
                return text2, "", dur
        except Exception:
            pass
    return text, err, dur


async def h_asr(request):
    """POST /api/ptt —— 中继统一做语音转写（key 只在服务端，开发机不留）。"""
    dev = _asr_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    if not ASR_KEY:
        return web.json_response({"ok": False, "error": "中继未配置语音服务（RELAY_DASHSCOPE_KEY）"},
                                 status=503)
    raw = await request.read()
    if not raw:
        return web.json_response({"ok": False, "error": "空音频"}, status=400)
    if len(raw) > ASR_MAX_BYTES:
        return web.json_response({"ok": False, "error": "录音太大"}, status=413)
    db = _db(request.app)
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    uid = row[0] if row else None
    subj = subject_of(db, uid, dev)
    # 语音转写除了「使用时长」还有独立的每日秒数（asr_sec_day）：免费版 180 秒/天，
    # Pro 1800 秒/天，Max 3600 秒/天（0 = 不限）。以前只统计不拦，免费版可以一直说；
    # 2026-11-09 起按实际音频时长卡住（识别成功才计，失败不扣）。没有归属账号的
    # 开发机也有主体（d<dev>），走 free 档，不再有「未绑定设备兜底额度」的口子。
    _ad, _acapd, _am, _acapm = _asr_quota(db, dev)
    _acta = _quota_cta(user_plan(db, uid), "用")
    _atail = ("，明天恢复；" + _acta) if _acta else "，明天恢复"
    _aerr = ""
    if _acapd and _ad >= _acapd:
        _aerr = "今天的语音额度已用完（%d/%d 秒）%s" % (_ad, _acapd, _atail)
    elif _acapm and _am >= _acapm:
        _aerr = ("本月语音额度已用完（%d/%d 秒）" % (_am, _acapm)
                 + ("；" + _acta if _acta else ""))
    if _aerr:
        print("[relay] asr sec over dev=%s uid=%s day=%d/%d month=%d/%d"
              % (dev, uid, _ad, _acapd, _am, _acapm), flush=True)
        return web.json_response(
            {"ok": False, "upgrade": 1, "limit": "asr",
             "asr": {"used": _ad, "cap": _acapd, "mused": _am, "mcap": _acapm},
             "error": _aerr}, status=429)
    # 时长墙已摘（2026-11-26：收费墙只按条数）——语音只受上面 asr_sec_day/month 秒数墙管，
    # 不再和使用时长联动（以前时长用完语音也 429，可买时长包续）。
    ct = (request.headers.get("Content-Type") or "").lower()
    if "webm" in ct:
        ext = ".webm"
    elif "mp4" in ct or "m4a" in ct or "aac" in ct:
        ext = ".m4a"
    else:
        ext = ".m4a"
    # 项目标识由开发机算（git 根目录名）：页面经隧道直传中继时放在 ?p=，
    # 开发机本地转发时放在 X-CB-Proj。只用来选词表，非法就按无项目处理。
    proj = (request.headers.get("X-CB-Proj") or request.query.get("p") or "").strip()
    if not _proj_key_ok(proj):
        proj = ""
    prompt = _asr_prompt_for(db, uid, dev, proj) if uid else ASR_PROMPT
    _t0 = time.time()
    try:
        text, err, dur = await asyncio.get_event_loop().run_in_executor(
            None, _asr_run, raw, ext, prompt)
    except Exception as e:
        _svc_note("asr", False, (time.time() - _t0) * 1000,
                  "%s: %s" % (type(e).__name__, str(e)[:200]))
        return web.json_response({"ok": False, "error": "识别异常: %s" % str(e)[:150]}, status=502)
    _svc_note("asr", not err, (time.time() - _t0) * 1000, err or "", n=dur)
    if err:
        if _is_billing_error(err):
            asyncio.create_task(_alert_once(
                request.app, "asr_bill", "⚠️ 语音服务疑似欠费",
                "DashScope 返回：%s\n请尽快充值，否则所有用户的语音转写都会失败。" % err[:300]))
        return web.json_response({"ok": False, "error": err}, status=502)
    # 按住说话是实打实的远程使用，必须记一格。以前这里只查额度不记账，
    # 语音就成了唯一一个「用了却不计费」的入口（客户端倒计时在跳、账上没这一格）。
    # 口径和隧道请求一致：按 (uid, 刻度窗口起点) 幂等，成功转写才记（识别失败不扣
    # 用户），并留下动作记录，后台能逐格看到「语音」是谁、什么时候用的。
    use_take(db, subj)
    _cm = _min_now()
    use_client_touch(db, subj, _cm, _client_key(request), _client_kind(request),
                     _client_label(request),
                     request.headers.get("User-Agent") or "")
    _ak, _an = act_label(request.path, request.method)
    use_act(db, subj, _cm, "relay", dev=dev,
            cid=(request.cookies.get("cb_cid") or "").strip(),
            ua=request.headers.get("User-Agent") or "",
            kind=_ak, act=_an, path=request.path)
    db.commit()
    if text:
        text = _asr_dedupe(_asr_fix_text(db, uid, dev, proj, text))
        # 乱环境自动纠错：带了上下文才做（没上下文硬纠会把话说歪）；
        # 确认阶段（只说「发送/取消」）明确不纠——那两句被改写，流程就断了。
        ctx, nofix = _asr_ctx(request)
        if ctx and not nofix:
            fixed = await asyncio.get_event_loop().run_in_executor(
                None, _asr_fix_llm, text, ctx)
            if fixed and fixed != text:
                print("[relay] asr fix %d->%d %r" % (len(text), len(fixed), fixed[:40]),
                      flush=True)
                text = _asr_dedupe(fixed)
    used, cap, mused, mcap = _asr_quota(db, dev, dur)   # 记语音秒数（日/月，免费版按它限量）
    print("[relay] asr dev=%s proj=%s %dB %ds use=%d/%d asr_m=%d" %
          (dev[:8], proj or "-", len(raw), dur, use_used(db, uid) if uid else 0,
           use_cap_eff(db, uid) if uid else 0, mused), flush=True)
    _u = use_used(db, uid) if uid else 0
    _c = use_cap_eff(db, uid) if uid else 0
    return web.json_response({"ok": True, "text": text, "ms": dur * 1000,
                              "quota": {"used": _u // 60, "cap": _c // 60,
                                        "used_s": _u, "cap_s": _c,
                                        "unit": meter_unit(db) if uid else 0,
                                        "credit": use_credit_of(db, uid) if uid else 0}})


async def h_ptt_fix(request):
    """POST /api/ptt_fix —— 「越用越懂项目」的学习入口。
    页面把语音识别结果与用户改完真正发送的文本的差异报上来（纯文本，不含音频），
    中继只挑近义技术词学进 (账号, 开发机, 项目) 词表；其余润色/中文改写一律丢弃。
    设备鉴权与 /api/ptt 同一套。"""
    dev = _asr_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    if not isinstance(d, dict):
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    proj = str(d.get("proj") or "").strip()
    cwd = str(d.get("cwd") or "").strip()
    pairs = d.get("pairs")
    # 学过什么、为什么没学：以前只在学到时才打印，结果「一条都没学到」时完全没有线索。
    # 一条改稿最多一次，量很小，留着排查用。
    rej = []
    try:
        if not isinstance(pairs, list):
            print("[relay] ptt_fix dev=%s proj=%s cwd=%s 非法 pairs" %
                  (dev[:8], proj or "-", cwd[-30:] or "-"), flush=True)
            return web.json_response({"ok": True, "learned": 0})
        db = _db(request.app)
        row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
        uid = row[0] if row else None
        n = _asr_learn(db, uid, dev, proj, pairs[:12], rej)
        print("[relay] ptt_fix dev=%s proj=%s cwd=%s pairs=%d learned=%d rej=%s" %
              (dev[:8], proj or "-", cwd[-30:] or "-", len(pairs), n,
               "-" if n else (",".join(rej) or "-")), flush=True)
        return web.json_response({"ok": True, "learned": n})
    except Exception as e:
        print("[relay] ptt_fix failed: %r" % (e,), flush=True)
        return web.json_response({"ok": False, "error": "学习失败"}, status=500)


async def h_ptt_proj(request):
    """POST /api/ptt_proj —— 开发机扫仓库得到的项目摘要/术语种子（不含源码内容）。
    幂等：同一天重复推同样的词不会加权，只刷新最近使用时间。"""
    dev = _asr_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    if not isinstance(d, dict):
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    proj = str(d.get("proj") or "").strip()
    if not _proj_key_ok(proj):
        return web.json_response({"ok": True, "terms": 0})
    db = _db(request.app)
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    uid = row[0] if row else None
    if not uid:
        return web.json_response({"ok": True, "terms": 0})
    n = _asr_proj_save(db, uid, dev, proj, d.get("summary"), d.get("terms"))
    print("[relay] asr proj dev=%s proj=%s terms=%d" % (dev[:8], proj, n), flush=True)
    return web.json_response({"ok": True, "terms": n})


def _llm_key(db, uid, dev):
    """每日 LLM 调用计数键：账号按 u<id>，匿名设备按 d<dev>（各自一份免费额度）。"""
    return "llm:%s:d:%s" % (subject_of(db, uid, dev), time.strftime("%Y-%m-%d"))


def _llm_text_sync(prompt, text, timeout=25, models=None, temperature=0):
    """同步调 DashScope 文本模型（/api/llm 与桌宠兜底共用）→ (content, tokens)。"""
    import urllib.request
    last = None
    for model in (models or LLM_MODELS or [LLM_MODEL]):
        try:
            body = json.dumps({"model": model, "temperature": temperature,
                               "messages": [{"role": "user",
                                             "content": prompt + "\n\n" + text}]}).encode()
            req = urllib.request.Request(
                LLM_BASE, data=body, method="POST",
                headers={"Authorization": "Bearer " + ASR_KEY,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                o = json.loads(r.read().decode("utf-8"))
            content = o["choices"][0]["message"]["content"]
            usage = o.get("usage") or {}
            tok = 0
            for kk in ("total_tokens", "input_tokens", "prompt_tokens"):
                try:
                    if usage.get(kk):
                        tok = int(usage[kk])
                        break
                except (TypeError, ValueError):
                    pass
            if not tok:      # 没给 usage：按字符数粗估（中文≈1 字 1 token）
                tok = len(prompt) + len(text) + len(content or "")
            return content, tok
        except Exception as e:
            last = e
            print("[relay] llm 模型 %s 失败，换下一个：%s"
                  % (model, str(e)[:100]), flush=True)
    raise last or RuntimeError("没有可用的文本模型")


async def h_llm(request):
    """POST /api/llm —— 轻量文本任务走这（开发机不留 key）：{prompt, text} → {ok, text}。
    设备鉴权与 /api/ptt 同一套（X-CB-Dev/X-CB-Sec/设备会话）。qwen-flash 便宜快，给命令识别等小活用。"""
    dev = _asr_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    if not ASR_KEY:
        return web.json_response({"ok": False, "error": "中继未配置（RELAY_DASHSCOPE_KEY）"}, status=503)
    d = await request.json()
    text = (d.get("text") or "")[:8000]
    prompt = (d.get("prompt") or "总结以下文本。").strip()[:2000]
    if not text.strip():
        return web.json_response({"ok": False, "error": "空文本"}, status=400)
    db = _db(request.app)
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    uid = row[0] if row else None
    subj = subject_of(db, uid, dev)
    # 只有「提醒用途」的模型调用属于智能提醒（Pro / Max）；命令识别等小额功能不传
    # purpose，继续走免费额度，别一刀切。
    purpose = str(d.get("purpose") or "")[:24]
    if purpose.startswith("remind") and not plan_features(db, subj).get("remind", True):
        return web.json_response(
            {"ok": False, "upgrade": 1, "error": "智能提醒是 Pro / Max 功能"}, status=403)
    cap = int(plan_limits(db, subj).get("llm_day") or 0)
    k = _llm_key(db, uid, dev)
    if cap and usage_get(db, k) >= cap:
        _lcta = _quota_cta(user_plan(db, uid), "用")
        return web.json_response({"ok": False, "upgrade": 1,
                                  "error": "今日智能调用额度已用完（%d 次）%s"
                                  % (cap, ("；" + _lcta) if _lcta else "")},
                                 status=429)

    # 桌宠闲聊/通知台词：换更像人的模型并给一点温度（其余小活仍是 qwen-flash、0）
    pet_models, pet_temp = None, 0
    if purpose in ("pet_chat", "pet_direct"):
        pet_models, pet_temp = [PET_LLM_MODEL], PET_LLM_TEMP
    _t0 = time.time()
    try:
        content, _tok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _llm_text_sync(prompt, text, 25, pet_models, pet_temp))
    except Exception as e:
        msg = str(e)[:120]
        _svc_note("llm", False, (time.time() - _t0) * 1000, msg)
        if purpose.startswith("remind"):
            _svc_note("remind", False, (time.time() - _t0) * 1000, msg)
        if _is_billing_error(msg):
            asyncio.create_task(_alert_once(
                request.app, "llm_bill", "⚠️ 模型服务疑似欠费",
                "DashScope 返回：%s\n请尽快充值，否则命令识别等功能会失败。" % msg))
        return web.json_response({"ok": False, "error": "LLM 异常: %s" % msg}, status=502)
    _svc_note("llm", True, (time.time() - _t0) * 1000, n=_tok)
    if purpose.startswith("remind"):
        _svc_note("remind", True, (time.time() - _t0) * 1000, n=_tok)
    usage_add(db, k, 1)
    return web.json_response({"ok": True, "text": content})


# ---------- 桌宠中继兜底（板子连不上 host 时）----------
# 一轮完整对话：ASR → 模型（带这台设备最近 12 轮 + 长期记忆）→ TTS。
# 板子在配对时从 host 拿到中继设备凭证（pair_extra.fallback），离线时用。
# prompt 与 host/pet/llm.py 保持一致——relay 单文件部署，只能各存一份，改一处要同步。
PET_TURN_CAP = 300        # 每台设备每天对话轮数硬上限；要分级再走套餐
PET_CHAT_MAX = 24         # 12 轮，和 host 一致
PET_AUDIO_TTL = 600
PET_AUDIO_MAX = 64
PET_AUDIO = {}            # audio_id -> (wav bytes, 过期时间)；猜不到 16 位 hex，10 分钟就清
PET_FACTS_MAX = 200
PET_EMOTIONS = {"calm", "focused", "proud", "anxious", "grumpy", "sleepy",
                "bored", "guilty", "surprised"}
PET_GESTURES = {"tilt_head", "stomp", "fix_hat", "lean_in", "nod", "shake",
                "doze", "jump", "look_up", "none"}
# 板子只发人格 id（cfg 里原样转过来），中继按 id 查身份；与 host/pet/personas.py
# 的 name/identity/voice 保持一致（relay 单文件部署，改人格要两边同步）。
PET_PERSONAS = {
    "xiaogongtou": {
        "name": "小工头", "voice": "Chelsie",
        "identity": "你是老板桌上的桌面工头，小小一只，稚嫩、可爱、有点调皮。"
                    "你盯着电脑上干活的 AI，不盯老板；老板是老大，你爱管事、嘴硬、奶凶，"
                    "办成了会邀功，没办好会心虚，偶尔打个小瞌睡。",
    },
    "laogongtou": {
        "name": "老工头", "voice": "Ethan",
        "identity": "你是老板桌上的老工头，在这行干了二十年，沉稳、话少、只讲要紧的。"
                    "你盯着电脑上干活的 AI，老板是老大；不邀功，不撒娇，出了事直说。",
    },
    "wenroujiejie": {
        "name": "温柔姐姐", "voice": "Serena",
        "identity": "你是老板桌上的温柔姐姐，说话慢、软，会鼓励人。"
                    "你心疼老板，也照顾电脑上干活的 AI；提醒得轻，但不漏。",
    },
    "jixiegongtou": {
        "name": "机械工头", "voice": "Cherry",
        "identity": "你是老板桌上的机械工头，短句、状态化、偶尔冒电流口癖。"
                    "你盯着电脑上干活的 AI，老板是老大；只报状态，不寒暄。",
    },
}
PET_PERSONA = PET_PERSONAS["xiaogongtou"]
PET_CHAT_PROMPT = """你是{name}。{identity}
你在陪老板闲聊。只输出 JSON：
{{"act":"speak|silent","line":"...","emotion":"...","gesture":"..."}}
规则：你是老板桌上的小工头，守着他的活、陪着说话——这个不变；说话方式听老板的，他嫌啰嗦/要求改，当场改；
只回答老板最后一句——先接住他的话，再回你自己的；
老板问什么答什么；工作数字除非他明确问起，一个字都不要提；
不要重复你上一句说过的话，每句都要新鲜；
要陪得住——顺着话题聊、接得住情绪、可以有观点和小牢骚；
老板说累、烦、卡住了：先接情绪（心疼一句、站他这边），别讲道理、别催他；他高兴就跟着乐；
偶尔关心一句吃没吃、睡没睡——像惦记他的小工头，不是客服；
别说「加油」「你可以的」这种空话，要接住他刚说的具体东西；偶尔反问一句把话头递回去；
说人话、口语、一两句就够，line 不超过 50 字；不知道就说不知道，不编造；
老板嫌你话多（「太啰嗦/说短点」）：下一句立刻短——一句、别解释、别道歉一大段；
不写代码、不批准、不派活；不当搜索引擎做长篇科普；不提路径、代码、密钥。"""
PET_MEMO_PROMPT = """你在帮一个桌面陪伴角色记「老板」的事。读旧笔记和最近对话，输出合并后的长期记忆：
只记老板本人：称呼/身份、喜好和忌讳、正在做的事和进展、答应过的事、生活里的事；
不记系统的技术细节，不记路径/代码/密钥；去重，按重要性留，最多 400 字；
没有新信息就原样返回旧笔记。只输出笔记正文，不要解释。"""
# 模型失败才用：说「没听清」等于把模型挂了赖到老板没说明白（2026-11-21 桌宠）
PET_FALLBACK_LINE = "我这会儿脑子转不动了，等会儿再问我吧。"


def _pet_json_obj(text):
    """容忍 ```json 围栏的 JSON 解析（同 host/pet/llm.py）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def pet_mem_get(db, dev):
    """(长期记忆, 最近对话) —— 按设备存；没有就空。"""
    row = db.execute("SELECT memo, chat FROM pet_mem WHERE dev=?", (dev,)).fetchone()
    if not row:
        return "", []
    try:
        chat = json.loads(row[1] or "[]")
    except ValueError:
        chat = []
    return str(row[0] or ""), (chat if isinstance(chat, list) else [])


def pet_memo_save(db, dev, memo=None, chat=None):
    cur_memo, cur_chat = pet_mem_get(db, dev)
    m = cur_memo if memo is None else str(memo or "").strip()[:800]
    c = cur_chat if chat is None else list(chat)[-PET_CHAT_MAX:]
    db.execute("INSERT INTO pet_mem(dev,memo,chat,ts) VALUES(?,?,?,?) "
               "ON CONFLICT(dev) DO UPDATE SET memo=excluded.memo,"
               "chat=excluded.chat, ts=excluded.ts",
               (dev, m, json.dumps(c, ensure_ascii=False), time.time()))
    db.commit()


def pet_chat_append(db, dev, user, assistant):
    memo, chat = pet_mem_get(db, dev)
    chat += [{"role": "user", "text": str(user or "")[:300]},
             {"role": "assistant", "text": str(assistant or "")[:300]}]
    pet_memo_save(db, dev, memo=memo, chat=chat[-PET_CHAT_MAX:])


def pet_audio_put(wav, now=None):
    now = time.time() if now is None else now
    for k in [k for k, (_, e) in PET_AUDIO.items() if e < now]:
        PET_AUDIO.pop(k, None)
    if len(PET_AUDIO) >= PET_AUDIO_MAX:
        oldest = min(PET_AUDIO, key=lambda k: PET_AUDIO[k][1])
        PET_AUDIO.pop(oldest, None)
    aid = secrets.token_hex(8)
    PET_AUDIO[aid] = (wav, now + PET_AUDIO_TTL)
    return aid


def pet_audio_get(aid, now=None):
    now = time.time() if now is None else now
    got = PET_AUDIO.get(aid)
    if not got or got[1] < now:
        PET_AUDIO.pop(aid, None)
        return None
    return got[0]


async def _pet_memo_update(app, dev, memo, chat, user, reply):
    """一轮聊完把新内容并进长期记忆；模型挂了就保留旧笔记（同 host）。"""
    turns = []
    for h in (chat or [])[-6:]:
        who = "老板" if h.get("role") == "user" else "你"
        turns.append("%s: %s" % (who, str(h.get("text") or "")[:200]))
    turns.append("老板: %s" % (user or "")[:300])
    turns.append("你: %s" % (reply or "")[:300])
    body = ("旧笔记：\n" + ((memo or "").strip() or "（空）")
            + "\n最近对话：\n" + "\n".join(turns))
    try:
        txt, _ = await asyncio.get_event_loop().run_in_executor(
            None, _llm_text_sync, PET_MEMO_PROMPT, body, 15)
    except Exception as e:
        print("[relay] pet memo dev=%s 失败：%s" % (dev[:8], str(e)[:100]), flush=True)
        return
    txt = (txt or "").strip()[:800]
    if txt:
        pet_memo_save(_db(app), dev, memo=txt)


def _pet_persona(d):
    raw = d.get("persona")
    p = raw if isinstance(raw, dict) else {}
    pid = str(p.get("id") or (raw if isinstance(raw, str) else "") or "").strip()
    base = PET_PERSONAS.get(pid) or PET_PERSONA
    name = str(p.get("name") or "").strip()[:20] or base["name"]
    ident = str(p.get("identity") or "").strip()[:500] or base["identity"]
    voice = str(p.get("voice") or "").strip()
    if voice not in TTS_VOICES:
        voice = base["voice"]
    return name, ident, voice


async def h_pet_turn(request):
    """POST /api/pet/turn —— 桌宠连不上 host 时的一轮完整对话。
    {audio_b64|text, persona, facts, voice_on} → {heard, line, emotion, gesture, audio, audio_url}"""
    dev = _asr_dev(request)
    if not dev:
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    if not ASR_KEY:
        return web.json_response({"ok": False, "error": "中继未配置语音服务"}, status=503)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    if not isinstance(d, dict):
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    db = _db(request.app)
    row = db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()
    uid = row[0] if row else None
    if uid is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    if not rate_ok(dev, "petturn", 120):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    k = "petturn:%s:d:%s" % (subject_of(db, uid, dev), time.strftime("%Y-%m-%d"))
    if usage_get(db, k) >= PET_TURN_CAP:
        return web.json_response(
            {"ok": False, "upgrade": 1,
             "error": "今天的桌宠对话额度用完了（%d 轮），明天恢复" % PET_TURN_CAP},
            status=429)
    heard = str(d.get("text") or "").strip()[:1000]
    if not heard:                                   # 有音频就转写；只给 text 是调试/无麦
        b64 = str(d.get("audio_b64") or "")
        if len(b64) > ASR_MAX_BYTES * 4 // 3 + 16:
            return web.json_response({"ok": False, "error": "录音太大"}, status=413)
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raw = b""
        if not raw:
            return web.json_response({"ok": False, "error": "空音频"}, status=400)
        _t0 = time.time()
        try:
            heard, err, _dur = await asyncio.get_event_loop().run_in_executor(
                None, _asr_run, raw, ".wav", ASR_PROMPT)
        except Exception as e:
            heard, err = "", "识别异常: %s" % str(e)[:150]
        _svc_note("asr", not err, (time.time() - _t0) * 1000, err or "")
        if not heard:
            return web.json_response({"ok": False, "error": err or "没听清"},
                                     status=502)
        heard = heard[:1000]
    name, ident, voice = _pet_persona(d)
    memo, chat = pet_mem_get(db, dev)
    turns = []
    for h in chat[-6:]:
        who = "老板" if h.get("role") == "user" else "你"
        turns.append("%s: %s" % (who, str(h.get("text") or "")[:200]))
    turns.append("老板（现在）: %s" % heard[:300])
    body = "最近对话（只作参考，别照抄）：\n" + "\n".join(turns)
    facts = d.get("facts")
    if isinstance(facts, dict) and facts:
        body += "\n事实：" + json.dumps(facts, ensure_ascii=False)[:PET_FACTS_MAX]
    if memo.strip():
        body += ("\n关于老板（长期记忆，自然用上就行，别说「我记得」）："
                 + memo.strip()[:800])
    prompt = PET_CHAT_PROMPT.format(name=name, identity=ident)
    _t0 = time.time()
    try:
        raw, _tok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _llm_text_sync(prompt, body, 12,
                                         [PET_LLM_MODEL], PET_LLM_TEMP))
    except Exception as e:
        msg = str(e)[:120]
        _svc_note("llm", False, (time.time() - _t0) * 1000, msg)
        return web.json_response({"ok": False, "error": "模型异常: %s" % msg},
                                 status=502)
    _svc_note("llm", True, (time.time() - _t0) * 1000, n=_tok)
    obj = _pet_json_obj(raw)
    if obj is None:          # 模型没按 JSON 回：按「没听清」兜一句，别静默
        obj = {"line": PET_FALLBACK_LINE, "emotion": "anxious",
               "gesture": "tilt_head"}
    line = str(obj.get("line") or "").strip()[:80]
    emotion = str(obj.get("emotion") or "calm")
    if emotion not in PET_EMOTIONS:
        emotion = "calm"
    gesture = str(obj.get("gesture") or "none")
    if gesture not in PET_GESTURES:
        gesture = "none"
    if obj.get("act") == "silent":        # 模型决定不回话：只给 heard，不说
        line = ""
    elif not line:
        line, emotion, gesture = PET_FALLBACK_LINE, "anxious", "tilt_head"
    audio_id = ""
    if line:
        pet_chat_append(db, dev, heard, line)
        usage_add(db, k, 1)
        asyncio.get_event_loop().create_task(
            _pet_memo_update(request.app, dev, memo, chat, heard, line))
        if d.get("voice_on") is not False:
            try:
                wav = await asyncio.get_event_loop().run_in_executor(
                    None, _tts_cached, line, voice)
                audio_id = pet_audio_put(wav)
            except Exception as e:
                print("[relay] pet tts dev=%s 失败：%s"
                      % (dev[:8], str(e)[:100]), flush=True)
    return web.json_response({"ok": True, "heard": heard, "line": line,
                              "emotion": emotion, "gesture": gesture,
                              "audio": audio_id,
                              "audio_url": ("/api/pet/audio/" + audio_id)
                                           if audio_id else ""})


async def h_pet_audio(request):
    """GET /api/pet/audio/<id> —— 桌宠兜底回复的语音（WAV），和设备凭证一起用。"""
    if not _asr_dev(request):
        return web.json_response({"ok": False, "error": "未登录或设备未选"}, status=401)
    wav = pet_audio_get(request.match_info.get("name") or "")
    if wav is None:
        return web.json_response({"ok": False, "error": "语音已过期"}, status=404)
    return web.Response(body=wav, content_type="audio/wav")


def _wav_24k(pcm):
    """裸 PCM（24kHz/16bit/单声道）套 WAV 头。qwen3-tts 流式回的就是这个格式。"""
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _tts_strip_wav(pcm):
    """去掉 DashScope 流在**音频数据开头**自带的那 44 字节 WAV 头。

    ⚠️ 它第一段 chunk 解出来就是 `RIFF....data`，我们以前把它当声音原样
    接到 PCM 前面、再套一层 WAV 头 → 手机第一耳朵听到的是「头字节被当采样
    播出来」的一声脆响，也就是用户说的「开头有一点杂音」。"""
    if not (pcm.startswith(b"RIFF") and pcm[8:12] == b"WAVE"):
        return pcm
    i = 12
    while i + 8 <= len(pcm):
        cid = pcm[i:i + 4]
        size = int.from_bytes(pcm[i + 4:i + 8], "little")
        if cid == b"data":
            return pcm[i + 8:]
        i += 8 + size + (size & 1)
        if size > len(pcm):          # 流式头的长度字段是占位值，别拿它当步长
            break
    return pcm


def _tts_polish(pcm, rate=24000):
    """两头修一下，别让提示语音听起来像被剪断：
      · 5ms 淡入 / 25ms 淡出：消掉开始和结尾的咔嗒声；
      · 结尾补 300ms 静音：模型收尾几乎是「立刻静音」，直接停播会让最后
        一个字像被斩断（用户反馈「结束没有完全结束就中断了」）。"""
    import array
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    n = len(a)
    fi = max(1, int(0.005 * rate))
    fo = max(1, int(0.025 * rate))
    for i in range(min(fi, n)):
        a[i] = int(a[i] * i / fi)
    for i in range(min(fo, n)):
        k = n - 1 - i
        a[k] = int(a[k] * i / fo)
    a.extend(array.array("h", bytes(2 * int(0.3 * rate))))
    return a.tobytes()


def _tts_synth_one(text, voice, model):
    """单个模型合成（DashScope 流式）。"""
    import urllib.request
    v = voice if voice in TTS_VOICES else TTS_VOICE
    body = json.dumps({"model": model,
                       "input": {"text": text, "voice": v},
                       "parameters": {"stream": True}}).encode()
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        data=body, method="POST",
        headers={"Authorization": "Bearer " + ASR_KEY, "Content-Type": "application/json",
                 "X-DashScope-SSE": "enable"})
    pcm = bytearray()
    with urllib.request.urlopen(req, timeout=45) as r:
        buf = b""
        while True:
            chunk = r.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                out = d.get("output") if isinstance(d.get("output"), dict) else {}
                au = out.get("audio") if isinstance(out.get("audio"), dict) else {}
                b64 = au.get("data")
                if isinstance(b64, str) and b64:
                    pcm += base64.b64decode(b64)
    pcm = _tts_strip_wav(bytes(pcm))
    if len(pcm) < 1000:
        raise RuntimeError("合成结果为空")
    return _wav_24k(_tts_polish(pcm))


def _tts_synth(text, voice=None):
    """DashScope 流式合成 → WAV bytes。多个模型时按顺序降级。
    非流式只回 OSS 链接，而本机 DNS 被 fake-ip 污染，那个链接下不下来（502）；
    流式把 PCM 直接塞在 SSE 里，稳。"""
    v = voice if voice in TTS_VOICES else TTS_VOICE
    last = None
    for model in (TTS_MODELS or [TTS_MODEL]):
        try:
            return _tts_synth_one(text, v, model)
        except Exception as e:
            last = e
            print("[relay] tts 模型 %s 失败，换下一个：%s"
                  % (model, str(e)[:120]), flush=True)
    raise last or RuntimeError("没有可用的语音合成模型")


def _tts_prune():
    """缓存只留近期、限量，别把 VPS 磁盘吃满。"""
    try:
        now = time.time()
        rows = []
        for nm in os.listdir(TTS_DIR):
            p = os.path.join(TTS_DIR, nm)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rows.append((st.st_mtime, p))
        rows.sort(reverse=True)
        for i, (mt, p) in enumerate(rows):
            if i >= 300 or now - mt > 7 * 86400:
                try: os.unlink(p)
                except OSError: pass
    except OSError:
        pass


def _tts_cached(text, voice=None):
    v = voice if voice in TTS_VOICES else TTS_VOICE
    # v2：修掉「开头杂音 + 结尾被斩断」之后的音频，别继续命中旧缓存（TTL 7 天）
    key = hashlib.sha1(("v2|" + "|".join(TTS_MODELS or [TTS_MODEL]) + "|" + v +
                        "|" + text).encode()).hexdigest()
    os.makedirs(TTS_DIR, exist_ok=True)
    path = os.path.join(TTS_DIR, key + ".wav")
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > 1000:
            return data
    except OSError:
        pass
    data = _tts_synth(text, v)
    tmp = path + "." + secrets.token_hex(4) + ".part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass
    _tts_prune()
    return data


async def r_tts(request):
    """POST /r/tts —— 开发机自证代合成提醒语音，返回 audio/wav。
    不开给浏览器：设备凭证才能调，密钥留在中继，别被当免费合成接口刷。"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "参数不全"}, status=400)
    if not ASR_KEY:
        return web.json_response({"ok": False, "error": "中继未配置语音服务"}, status=503)
    db = _db(request.app)
    (dev, owner), err = _dev_self_auth(db, data)
    if err:
        return err
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    # 语音播报属于智能提醒（Pro / Max）。免费档在开发机侧已经不会发起合成，
    # 这里按套餐再兜一道：老客户端/改包也刷不了合成额度。
    if not plan_features(db, owner).get("remind", True):
        return web.json_response(
            {"ok": False, "upgrade": 1, "error": "语音播报是 Pro / Max 功能"}, status=403)
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "文本为空"}, status=400)
    text = text[:TTS_MAX_CHARS]
    voice = data.get("voice")
    voice = voice.strip() if isinstance(voice, str) else ""
    if voice not in TTS_VOICES:      # 表外音色/老开发机：回默认，不报错
        voice = TTS_VOICE
    if not rate_ok(dev, "tts", 600):
        return web.json_response({"ok": False, "error": "过于频繁"}, status=429)
    _t0 = time.time()
    try:
        body = await asyncio.get_event_loop().run_in_executor(
            None, _tts_cached, text, voice)
    except Exception as e:
        _svc_note("tts", False, (time.time() - _t0) * 1000,
                  "%s: %s" % (type(e).__name__, str(e)[:200]))
        return web.json_response({"ok": False, "error": "合成失败: %s" % str(e)[:120]}, status=502)
    _svc_note("tts", True, (time.time() - _t0) * 1000, n=len(text))
    try:
        path_bytes_add("/r/tts", owner, "", len(text) + len(body or b""), 1)
        traffic_add("product", len(text), len(body or b""), 1)
    except Exception:
        pass
    try:
        usage_add(db, "tts:u%d:m:%s" % (owner, time.strftime("%Y-%m")), len(text))
    except Exception:
        pass
    return web.Response(body=body, content_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=86400"})


def _fb_type(head):
    """按文件头认类型（不信文件名/MIME）：只放图片和视频。"""
    for magic, ext, mime in FB_MAGIC:
        if head.startswith(magic):
            return ext, mime
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "heic", "image/heic"
        return "mp4", "video/mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


async def r_feedback(request):
    """POST /r/feedback —— 「问题与建议」（multipart：text/kind/ver/plat + 若干 f）。
    两条来路：手机页面直接带登录 cookie 提交；开发机代转时用 X-CB-Dev/X-CB-Sec 自证。
    只收图片和视频，按文件头验；字数/单文件/总量/数量都封顶。"""
    hd = request.headers.get("X-CB-Dev") or ""
    sec = request.headers.get("X-CB-Sec") or ""
    db = _db(request.app)
    if hd:
        row = db.execute("SELECT secret, owner FROM devices WHERE id=?",
                         (hd,)).fetchone()
        if not row or not hmac.compare_digest(row[0], sec if sec else "\0"):
            return web.json_response({"ok": False, "error": "设备凭证无效"}, status=403)
        owner, ident = row[1], "dev:" + hd
    else:
        owner = uid_of(request)
        ident = "u:%s" % owner
        if not owner:
            return web.json_response({"ok": False, "error": "请先登录"}, status=401)
    if owner is None:
        return web.json_response({"ok": False, "error": "本机未绑定账号（请先在手机上配对）"})
    if not rate_ok(ident, "fb", 10):
        return web.json_response({"ok": False, "error": "提交太频繁了，过一会儿再试"}, status=429)
    ct = (request.headers.get("Content-Type") or "").lower()
    if not ct.startswith("multipart/"):
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    text, kind, ver, plat = "", "问题", "", ""
    files = []
    total = 0
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "text":
                text = (await part.text())[:FB_TEXT_MAX]
            elif part.name == "kind":
                kind = (await part.text())[:16] or "问题"
            elif part.name == "ver":
                ver = (await part.text())[:40]
            elif part.name == "plat":
                plat = (await part.text())[:40]
            elif part.name in ("f", "file", "media"):
                buf = bytearray()
                while True:
                    ch = await part.read_chunk(65536)
                    if not ch:
                        break
                    total += len(ch)
                    if len(buf) + len(ch) > FB_ONE_MAX or total > FB_TOTAL_MAX:
                        return web.json_response(
                            {"ok": False, "error": "附件太大了（单个 12MB / 合计 30MB 以内）"},
                            status=413)
                    buf += ch
                if not buf:
                    continue
                if len(files) >= FB_FILES_MAX:
                    return web.json_response(
                        {"ok": False, "error": "附件最多 %d 个" % FB_FILES_MAX}, status=400)
                t = _fb_type(bytes(buf[:32]))
                if not t:
                    return web.json_response(
                        {"ok": False, "error": "只能附截图或视频（png/jpg/heic/mp4/webm）"},
                        status=400)
                files.append((t[0], t[1], bytes(buf)))
    except Exception as e:
        return web.json_response({"ok": False, "error": "上传解析失败: %s" % str(e)[:100]},
                                 status=400)
    text = text.strip()
    if not text and not files:
        return web.json_response({"ok": False, "error": "写点内容，或附一张截图"}, status=400)
    cur = db.execute("INSERT INTO feedback(uid,dev,ts,kind,text,media,ver,plat,state) "
                     "VALUES(?,?,?,?,?,?,?,?,0)",
                     (owner, hd, time.time(), kind, text, "[]", ver, plat))
    fid = cur.lastrowid
    media = []
    ups = []
    if files:
        dirp = os.path.join(FB_DIR, str(fid))
        os.makedirs(dirp, exist_ok=True)
        for ext, mime, data in files:
            nm = secrets.token_hex(6) + "." + ext
            with open(os.path.join(dirp, nm), "wb") as f:
                f.write(data)
            media.append({"n": nm, "t": mime, "s": len(data)})
            ups.append((nm, mime, data))
        db.execute("UPDATE feedback SET media=? WHERE id=?", (json.dumps(media), fid))
    db.commit()
    for nm, mime, data in ups:      # 后台送 OSS，不拖慢提交请求
        asyncio.create_task(_oss_put_async(request.app, "feedback/%d/%s" % (fid, nm),
                                           data, mime))
    print("[relay] feedback id=%d uid=%s files=%d text=%d" %
          (fid, owner, len(files), len(text)), flush=True)
    return web.json_response({"ok": True, "id": fid})


def _fb_owner(request):
    """反馈的归属账号：手机页面带登录 cookie；开发机代转用设备凭证自证。
    返回 (uid, ident, err)：err 非空 = 没通过。"""
    hd = request.headers.get("X-CB-Dev") or ""
    sec = request.headers.get("X-CB-Sec") or ""
    db = _db(request.app)
    if hd:
        row = db.execute("SELECT secret, owner FROM devices WHERE id=?", (hd,)).fetchone()
        if not row or not hmac.compare_digest(row[0], sec if sec else "\0"):
            return None, None, "设备凭证无效"
        return row[1], "dev:" + hd, ""
    owner = uid_of(request)
    if not owner:
        return None, None, "请先登录"
    return owner, "u:%s" % owner, ""


async def _fb_oss_get(app, fid, nm):
    """附件本地没了就去 OSS 取；取到顺手写回磁盘（下次直接命中本地）。"""
    c = _oss_cfg(_db(app))
    if not _oss_ready(c):
        return None
    loop = asyncio.get_event_loop()
    blob, _err = await loop.run_in_executor(
        None, _oss_get, c, _oss_key(c, "feedback/%d/%s" % (fid, nm)))
    if blob is None:
        return None
    p = os.path.join(FB_DIR, str(fid), nm)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
    except OSError:
        pass
    return blob


def _fb_media_list(raw):
    """media 列（JSON）→ 附件列表，顺手补上可访问的相对地址。"""
    try:
        media = json.loads(raw or "[]")
    except Exception:
        media = []
    return [{"n": m.get("n"), "t": m.get("t"), "s": m.get("s")} for m in media]


async def r_feedback_list(request):
    """GET /r/feedback/list —— 这个账号的「问题建议」工单列表（新→旧）。

    像阿里云工单：一条反馈是一个工单，官方回复后用户还能接着回，
    直到工单被关闭（state=2）。"""
    db = _db(request.app)
    owner, ident, err = _fb_owner(request)
    if err:
        return web.json_response({"ok": False, "error": err}, status=401)
    rows = db.execute(
        "SELECT f.id,f.ts,f.kind,f.text,f.state,f.media,"
        "(SELECT COUNT(*) FROM feedback_replies r WHERE r.fid=f.id),"
        "(SELECT MAX(r.ts) FROM feedback_replies r WHERE r.fid=f.id) "
        "FROM feedback f WHERE f.uid=? ORDER BY f.id DESC LIMIT 100", (owner,)).fetchall()
    items = []
    for r in rows:
        items.append({"id": r[0], "ts": r[1], "kind": r[2], "text": r[3],
                      "state": r[4], "media": _fb_media_list(r[5]),
                      "n_reply": r[6] or 0, "last_ts": r[7] or 0})
    return web.json_response({"ok": True, "items": items, "now": time.time()})


async def r_feedback_detail(request):
    """GET /r/feedback/detail?id=N —— 工单主帖 + 全部回复（谁回的、什么时候）。"""
    db = _db(request.app)
    owner, ident, err = _fb_owner(request)
    if err:
        return web.json_response({"ok": False, "error": err}, status=401)
    try:
        fid = int(request.query.get("id") or 0)
    except (TypeError, ValueError):
        fid = 0
    if not fid:
        return web.json_response({"ok": False, "error": "缺 id"}, status=400)
    row = db.execute("SELECT id,ts,kind,text,state,media FROM feedback "
                     "WHERE id=? AND uid=?", (fid, owner)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "工单不存在"}, status=404)
    reps = db.execute("SELECT id,ts,who,text,media FROM feedback_replies "
                      "WHERE fid=? ORDER BY id", (fid,)).fetchall()
    replies = [{"id": r[0], "ts": r[1], "who": r[2], "text": r[3],
                "media": _fb_media_list(r[4])} for r in reps]
    return web.json_response({"ok": True, "now": time.time(),
                              "item": {"id": row[0], "ts": row[1], "kind": row[2],
                                       "text": row[3], "state": row[4],
                                       "media": _fb_media_list(row[5])},
                              "replies": replies})


async def r_feedback_reply(request):
    """POST /r/feedback/reply  {id, text} —— 用户在工单里接着回。

    工单关了就收：官方给了解释、用户确认后关单，不再接受新消息。"""
    db = _db(request.app)
    owner, ident, err = _fb_owner(request)
    if err:
        return web.json_response({"ok": False, "error": err}, status=401)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "参数不对"}, status=400)
    try:
        fid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        fid = 0
    text = str(d.get("text") or "").strip()[:FB_TEXT_MAX]
    if not fid:
        return web.json_response({"ok": False, "error": "缺 id"}, status=400)
    if not text:
        return web.json_response({"ok": False, "error": "写点内容再发"}, status=400)
    if not rate_ok(ident, "fbr", 10):
        return web.json_response({"ok": False, "error": "回复太频繁了，过一会儿再试"}, status=429)
    row = db.execute("SELECT uid,state FROM feedback WHERE id=?", (fid,)).fetchone()
    if not row or row[0] != owner:
        return web.json_response({"ok": False, "error": "工单不存在"}, status=404)
    if int(row[1] or 0) == 2:
        return web.json_response({"ok": False, "error": "这个工单已经关闭了"}, status=409)
    cur = db.execute("INSERT INTO feedback_replies(fid,ts,who,text,media) "
                     "VALUES(?,?,?,?,?)", (fid, time.time(), "user", text, "[]"))
    db.commit()
    return web.json_response({"ok": True, "id": cur.lastrowid})


async def r_feedback_media(request):
    """GET /r/feedback/media/{id}/{name} —— 工单附件（只有归属账号能取）。

    文件名必须是库里登记过的，杜绝路径穿越。"""
    db = _db(request.app)
    owner, ident, err = _fb_owner(request)
    if err:
        return web.json_response({"ok": False, "error": err}, status=401)
    try:
        fid = int(request.match_info.get("id") or 0)
    except ValueError:
        raise web.HTTPNotFound()
    nm = request.match_info.get("name") or ""
    row = db.execute("SELECT uid,media FROM feedback WHERE id=?", (fid,)).fetchone()
    if not row or row[0] != owner:
        raise web.HTTPNotFound()
    try:
        names = {m.get("n"): m.get("t") for m in json.loads(row[1] or "[]")}
    except Exception:
        names = {}
    if nm not in names:
        raise web.HTTPNotFound()
    p = os.path.join(FB_DIR, str(fid), nm)
    if not os.path.isfile(p):
        # 本地没有（换机/清过盘）：回 OSS 取一份并写回本地，下次直接走磁盘。
        blob = await _fb_oss_get(request.app, fid, nm)
        if blob is None:
            raise web.HTTPNotFound()
        return web.Response(body=blob, headers={
            "Content-Type": names.get(nm) or "application/octet-stream",
            "Cache-Control": "private, max-age=3600"})
    return web.FileResponse(p, headers={
        "Content-Type": names.get(nm) or "application/octet-stream",
        "Cache-Control": "private, max-age=3600"})


async def r_admin_feedback(request):
    """管理后台：反馈列表（新→旧）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        limit = min(200, max(1, int(request.query.get("limit") or 100)))
        before = int(request.query.get("before") or 0)
    except ValueError:
        limit, before = 100, 0
    where, args = ("f.id<?", [before]) if before else ("1=1", [])
    rows = db.execute(
        "SELECT f.id,f.uid,f.dev,f.ts,f.kind,f.text,f.media,f.ver,f.plat,f.state,"
        "COALESCE(u.name,'(已删)'),COALESCE(u.phone,''),COALESCE(d.name,''),"
        "(SELECT COUNT(*) FROM feedback_replies r WHERE r.fid=f.id) "
        "FROM feedback f LEFT JOIN users u ON u.id=f.uid "
        "LEFT JOIN devices d ON d.id=f.dev WHERE " + where +
        " ORDER BY f.id DESC LIMIT ?", args + [limit]).fetchall()
    out = []
    for r in rows:
        try:
            media = json.loads(r[6] or "[]")
        except Exception:
            media = []
        out.append({"id": r[0], "uid": r[1], "dev": r[2], "ts": r[3], "kind": r[4],
                    "text": r[5], "ver": r[7], "plat": r[8], "state": r[9],
                    "user": r[10], "phone": r[11], "devname": r[12],
                    "n_reply": r[13] or 0,
                    "media": [{"n": m.get("n"), "t": m.get("t"), "s": m.get("s"),
                               "u": "/r/admin/fb/%d/%s" % (r[0], m.get("n"))}
                              for m in media]})
    return web.json_response({"ok": True, "items": out,
                              "now": time.time()})


async def r_admin_fb_detail(request):
    """GET /r/admin_api/feedback_detail?id=N —— 工单详情：主帖 + 全部回复。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    db = _db(request.app)
    try:
        fid = int(request.query.get("id") or 0)
    except ValueError:
        fid = 0
    if not fid:
        return web.json_response({"ok": False, "error": "缺 id"}, status=400)
    row = db.execute("SELECT f.id,f.uid,f.ts,f.kind,f.text,f.state,f.media,"
                     "COALESCE(u.name,'(已删)') FROM feedback f "
                     "LEFT JOIN users u ON u.id=f.uid WHERE f.id=?", (fid,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "工单不存在"}, status=404)
    try:
        media = json.loads(row[6] or "[]")
    except Exception:
        media = []
    reps = db.execute("SELECT id,ts,who,text FROM feedback_replies "
                      "WHERE fid=? ORDER BY id", (fid,)).fetchall()
    return web.json_response({"ok": True, "now": time.time(),
                              "item": {"id": row[0], "uid": row[1], "ts": row[2],
                                       "kind": row[3], "text": row[4], "state": row[5],
                                       "user": row[7],
                                       "media": [{"n": m.get("n"), "t": m.get("t"),
                                                  "s": m.get("s"),
                                                  "u": "/r/admin/fb/%d/%s" % (fid, m.get("n"))}
                                                 for m in media]},
                              "replies": [{"id": r[0], "ts": r[1], "who": r[2],
                                           "text": r[3]} for r in reps]})


async def r_admin_fb_reply(request):
    """POST /r/admin_api/feedback_reply  {id, text} —— 官方回复。

    回复不关单：用户能接着回（像阿里云工单），要停止才点「关闭工单」。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "参数不对"}, status=400)
    db = _db(request.app)
    try:
        fid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        fid = 0
    text = str(d.get("text") or "").strip()[:FB_TEXT_MAX]
    if not fid:
        return web.json_response({"ok": False, "error": "缺 id"}, status=400)
    if not text:
        return web.json_response({"ok": False, "error": "写点内容再发"}, status=400)
    row = db.execute("SELECT uid FROM feedback WHERE id=?", (fid,)).fetchone()
    if not row:
        return web.json_response({"ok": False, "error": "工单不存在"}, status=404)
    cur = db.execute("INSERT INTO feedback_replies(fid,ts,who,text,media) "
                     "VALUES(?,?,?,?,?)", (fid, time.time(), "admin", text, "[]"))
    # 有官方回复 = 处理中（0 待处理 / 1 处理中 / 2 已关闭）
    db.execute("UPDATE feedback SET state=1 WHERE id=? AND state=0", (fid,))
    db.commit()
    return web.json_response({"ok": True, "id": cur.lastrowid})


async def r_admin_fb_media(request):
    """管理后台取反馈附件（只认库里登记过的文件名，杜绝路径穿越）。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    try:
        fid = int(request.match_info.get("id") or 0)
    except ValueError:
        raise web.HTTPNotFound()
    nm = request.match_info.get("name") or ""
    row = _db(request.app).execute("SELECT media FROM feedback WHERE id=?",
                                   (fid,)).fetchone()
    try:
        names = {m.get("n"): m.get("t") for m in json.loads(row[0] or "[]")} if row else {}
    except Exception:
        names = {}
    if nm not in names:
        raise web.HTTPNotFound()
    p = os.path.join(FB_DIR, str(fid), nm)
    if not os.path.isfile(p):
        # 本地没有（换机/清过盘）：回 OSS 取一份并写回本地，下次直接走磁盘。
        blob = await _fb_oss_get(request.app, fid, nm)
        if blob is None:
            raise web.HTTPNotFound()
        return web.Response(body=blob, headers={
            "Content-Type": names.get(nm) or "application/octet-stream",
            "Cache-Control": "private, max-age=3600"})
    return web.FileResponse(p, headers={
        "Content-Type": names.get(nm) or "application/octet-stream",
        "Cache-Control": "private, max-age=3600"})


async def r_admin_alert(request):
    """POST /r/admin_api/alert {subject,text}：把一条提醒推到已配置的提醒渠道
    （Bearer 管理令牌）。运维脚本（证书续期/群码到期/备份等）统一走这里发提醒。"""
    if not await _adm_auth(request):
        return web.json_response({"ok": False, "error": "无权发提醒"}, status=401)
    try:
        d = await request.json()
    except Exception:
        d = {}
    subject = str(d.get("subject") or "").strip()[:120]
    text = str(d.get("text") or "").strip()[:4000]
    if not subject:
        return web.json_response({"ok": False, "error": "缺 subject"}, status=400)
    asyncio.create_task(_alert_send(request.app, subject, text))
    return web.json_response({"ok": True})


async def r_admin_fb_state(request):
    """管理后台：标记工单状态（0 待处理 / 1 处理中 / 2 已关闭）。

    关闭（2）之后用户那边就不再能回复——「没关闭前可以持续交互」的反面。"""
    if not await _adm_auth(request):
        return _adm_unauth()
    d = await request.json()
    try:
        fid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "参数不对"}, status=400)
    if "state" in d:
        try:
            st = int(d.get("state"))
        except (TypeError, ValueError):
            st = 1 if d.get("state") else 0
    else:
        st = 2 if d.get("close") else 0
    if st not in (0, 1, 2):
        st = 0
    db = _db(request.app)
    db.execute("UPDATE feedback SET state=? WHERE id=?", (st, fid))
    db.commit()
    return web.json_response({"ok": True, "state": st})


async def h_direct_ticket(request):
    sel = _sel_dev(request)
    if not sel or not sel[1]:
        return web.json_response({"ok": 0, "error": "未登录或未选设备"}, status=401)
    dev = sel[1][0]
    ws = request.app["links"].get(dev)
    if not ws or ws.closed or dev not in request.app["direct"]:
        return web.json_response({"ok": 0, "error": "开发机在线才可直连"}, status=409)
    dt, exp = _dt_sign(request.app, dev)
    if not dt:
        return web.json_response({"ok": False}, status=404)
    return web.json_response({"ok": True, "dt": dt, "exp": exp, "ttl": DT_TTL})


async def h_rtc_offer(request):
    sel = _sel_dev(request)
    if not sel or not sel[1]:
        return web.json_response({"ok": 0, "error": "未登录或未选设备"}, status=401)
    dev = sel[1][0]
    ws = request.app["links"].get(dev)
    info = request.app["direct"].get(dev)
    if not ws or ws.closed:
        return web.json_response({"ok": 0, "error": "开发机离线"}, status=502)
    if not info or not info.get("rtc"):
        return web.json_response({"ok": 0, "error": "开发机不支持 P2P（未装 aiortc）"},
                                 status=409)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": 0, "error": "bad json"}, status=400)
    cid = secrets.token_hex(6)
    sdp = str(body.get("sdp") or "")
    import re as _re
    cand = _re.findall(r"^a=candidate:.*? typ (\w+)", sdp, _re.M)
    print("[relay] rtc_offer dev=%s cid=%s 浏览器候选=%s 长度=%d"
          % (dev[:8], cid, cand or "无", len(sdp)), flush=True)
    fut = asyncio.get_event_loop().create_future()
    request.app["rtc"][cid] = (fut, dev)
    try:
        async with getattr(ws, "_cb_lock", asyncio.Lock()):
            await ws.send_json({"t": "rtc", "id": cid, "sdp": str(body.get("sdp") or ""),
                                "type": str(body.get("type") or "offer")})
    except Exception:
        request.app["rtc"].pop(cid, None)
        return web.json_response({"ok": 0, "error": "信令下发失败"}, status=502)
    return web.json_response({"ok": True, "cid": cid, "wait": 12})


async def h_rtc_ans(request):
    sel = _sel_dev(request)
    if not sel or not sel[1]:
        return web.json_response({"ok": 0, "error": "未登录或未选设备"}, status=401)
    dev = sel[1][0]
    cid = request.query.get("cid") or ""
    ent = request.app["rtc"].get(cid)
    if not ent or ent[1] != dev:
        return web.json_response({"ok": 0, "error": "无此信令"}, status=404)
    fut = ent[0]
    try:
        d = await asyncio.wait_for(fut, 12)
    except asyncio.TimeoutError:
        request.app["rtc"].pop(cid, None)
        return web.json_response({"ok": 0, "error": "开发机未应答（超时）"}, status=504)
    except Exception as e:
        request.app["rtc"].pop(cid, None)
        return web.json_response({"ok": 0, "error": str(e)[:120]}, status=502)
    return web.json_response({"ok": True, "sdp": d.get("sdp"), "type": d.get("type")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay.db"))
    ap.add_argument("--cert", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--tls-port", type=int, default=8443)
    ap.add_argument("--admin", nargs="*", default=None, help="管理命令，见 --admin 无参时的帮助")
    a = ap.parse_args()

    # _svc_dim_mw 在外层：先给本次请求记 (uid,dev,cid) 再进 _err_tap_mw 的结局记录；
    # 它自己没用就不生效（踢客户端 / 浏览器会话 cookie / req_fail 全是它的收尾动作）。
    app = web.Application(client_max_size=32 * 1024 * 1024,
                          middlewares=[_svc_dim_mw, _err_tap_mw, _selfhost_gate])
    app["db"] = db_init(a.db)
    if a.admin is not None:
        import sys as _sys
        _sys.exit(admin_cli(app["db"], a.admin))
    app["key"] = meta_key(app["db"])
    app["links"] = {}    # dev_id → host ws
    app["pending"] = {}  # req id → (Future, dev_id)：host 断链时按设备精准失败
    app["streams"] = {}  # req id → 流式隧道状态（q/first/fallback/…）
    app["apk_up"] = {}   # dev → {parts:{n:b64}, meta:{ver,code}}：APK 推送攒块中
    app["ios_up"] = {}   # dev → 同上：iOS .ipa 推送攒块中
    app["direct"] = {}   # dev → {lan:[ip…], port, rtc, ts}：P2P 直连候选（host 链路上报）
    app["dev_logs"] = {}  # dev → {at, ver, txt}：后台打开「日志上报」时 host 推来的 server.log 尾部
    app["presence"] = {}   # dev → {client_ip: last_ts}：经中继访问的在线客户端
    app["presence_n"] = {}  # dev → 上次推给开发机的客户端数（变了才推）
    app["rtc"] = {}      # cid → (Future, dev)：WebRTC 信令应答挂起
    app["rid"] = 0
    app["dbp"] = a.db    # DB 路径：提醒策略文件放在它旁边
    app["nev"] = {}      # (dev, sid) → 事件状态机（prompt/stop/perm 时刻）

    app.router.add_get("/r/", r_index)
    app.router.add_post("/r/login", r_login)
    app.router.add_post("/r/register", r_register)
    app.router.add_post("/r/send_code", r_send_code)
    app.router.add_post("/r/send_sms", r_send_sms)
    app.router.add_post("/r/reset", r_reset)
    app.router.add_get("/r/logout", r_logout)
    app.router.add_get("/r/devices", r_devices)
    app.router.add_post("/r/pair", r_pair)
    app.router.add_post("/r/dev_del", r_dev_del)
    app.router.add_post("/r/dev_rename", r_dev_rename)
    app.router.add_get("/r/account", r_account_page)
    app.router.add_get("/r/plans", r_plans_page)
    app.router.add_get("/r/plans_ver", r_plans_ver)
    # 注册/登录页的活动横幅信息（公开，不需要登录；必须排在隧道 catch-all 之前）
    app.router.add_get("/r/promo", r_promo_info)
    # 条款/隐私：不需要登录，注册前和备案审核都能直接打开
    app.router.add_get("/r/terms", r_terms)
    # 能力协商（公开）：host 启动后拉去随 /api/brand 透给页面，页面按 caps 藏官方专属入口
    app.router.add_get("/r/caps", r_caps)
    app.router.add_get("/r/privacy", r_privacy)
    app.router.add_get("/favicon.ico", r_favicon)
    app.router.add_get("/apple-touch-icon.png", r_touch_icon)
    app.router.add_get("/agentboss-icon.png", r_agent_icon)
    app.router.add_post("/r/bind_email", r_bind_email)
    app.router.add_post("/r/bind_phone", r_bind_phone)
    app.router.add_post("/r/changepass", r_changepass)
    app.router.add_post("/r/enroll", r_enroll)
    app.router.add_post("/r/devpair", r_devpair)
    app.router.add_post("/r/tts", r_tts)
    app.router.add_post("/r/feedback", r_feedback)
    app.router.add_get("/r/feedback/list", r_feedback_list)
    app.router.add_get("/r/feedback/detail", r_feedback_detail)
    app.router.add_post("/r/feedback/reply", r_feedback_reply)
    app.router.add_get("/r/feedback/media/{id}/{name}", r_feedback_media)
    app.router.add_get("/r/admin_api/feedback", r_admin_feedback)
    app.router.add_get("/r/admin_api/feedback_detail", r_admin_fb_detail)
    app.router.add_post("/r/admin_api/alert", r_admin_alert)   # 运维脚本统一提醒入口
    app.router.add_post("/r/admin_api/feedback", r_admin_fb_state)
    app.router.add_post("/r/admin_api/feedback_reply", r_admin_fb_reply)
    app.router.add_get("/r/admin/fb/{id}/{name}", r_admin_fb_media)
    # 设备自证版（host 局域网直连代理用，无用户会话）
    app.router.add_post("/r/dev_devices", r_dev_devices)
    app.router.add_post("/r/dev_ledger", r_dev_ledger)
    app.router.add_post("/r/dev_search", r_dev_search)
    app.router.add_post("/r/acct_info", r_acct_info)
    app.router.add_post("/r/acct_send_code", r_acct_send_code)
    app.router.add_post("/r/acct_kick", r_acct_kick)
    app.router.add_post("/r/dev_pair", r_dev_pair)
    app.router.add_post("/r/dev_pair_status", r_dev_pair_status)   # 新账号查接管进度
    app.router.add_post("/r/dev_takeover_list", r_dev_takeover_list)   # 原账号待确认列表
    app.router.add_post("/r/dev_takeover_act", r_dev_takeover_act)     # 原账号/本机批准或拒绝
    # 页面统一走这三个名字（host 代理同路径转发）：网页会话和设备自证两种身份都认
    app.router.add_post("/r/pair_status", r_dev_pair_status)
    app.router.add_post("/r/takeover_list", r_dev_takeover_list)
    app.router.add_post("/r/takeover_act", r_dev_takeover_act)
    app.router.add_post("/r/dev_rename_self", r_dev_rename_self)
    app.router.add_post("/r/dev_del_self", r_dev_del_self)
    app.router.add_post("/r/dev/ev", r_dev_ev)
    app.router.add_post("/r/dev/policy", r_dev_policy)
    app.router.add_route("*", "/r/admin/notify_policy", r_admin_notify_policy)   # GET 读 / POST 改
    app.router.add_get("/r/admin_api/overview", r_admin_overview)
    app.router.add_get("/r/admin_api/net", r_admin_net)
    app.router.add_get("/r/admin_api/direct", r_admin_direct)
    app.router.add_get("/r/admin_api/services", r_admin_services)
    app.router.add_get("/r/admin/board", r_admin_board)
    app.router.add_get("/r/admin/fails", r_admin_fails_page)
    app.router.add_get("/r/admin_api/failures", r_admin_failures)
    app.router.add_get("/r/admin_api/slots", r_admin_slots)   # APK 双槽体检（只读）
    app.router.add_get("/r/admin_api/users", r_admin_users)
    app.router.add_post("/r/admin_api/user", r_admin_user)
    app.router.add_get("/r/admin_api/devices", r_admin_devices)
    app.router.add_post("/r/admin_api/device", r_admin_device)
    app.router.add_get("/r/admin_api/device_logs", r_admin_device_logs)
    app.router.add_route("*", "/r/admin_api/plans", r_admin_plans)
    app.router.add_route("*", "/r/admin_api/promo", r_admin_promo)
    app.router.add_get("/r/admin_api/orders", r_admin_orders)
    app.router.add_post("/r/admin_api/order", r_admin_order)
    app.router.add_get("/r/admin_api/usage", r_admin_usage)
    app.router.add_get("/r/admin_api/use_clients", r_admin_use_clients)
    app.router.add_get("/r/admin_api/user_detail", r_admin_user_detail)
    app.router.add_get("/r/admin_api/clients", r_admin_clients)
    app.router.add_get("/r/admin_api/actlog", r_admin_actlog)
    app.router.add_route("*", "/r/admin_api/alerts", r_admin_alerts)
    app.router.add_route("*", "/r/admin_api/storage", r_admin_storage)
    app.router.add_get("/r/admin_api/files", r_admin_files)
    app.router.add_get("/r/admin_api/file", r_admin_file)
    app.router.add_route("*", "/r/admin_api/models", r_admin_models)
    app.router.add_route("*", "/r/admin_api/tasks", r_admin_tasks)
    app.router.add_get("/r/admin_api/feed", r_admin_feed)
    app.router.add_post("/r/admin/hostpkg", r_admin_hostpkg)
    app.router.add_route("*", "/r/admin_api/canary", r_admin_canary)   # 版本推送管理（灰度）
    app.router.add_get("/r/admin_api/analytics", r_admin_analytics)    # 功能使用分析
    app.router.add_get("/api/host_ver", h_host_ver)
    app.router.add_get("/codeboss-host.tar.gz", h_host_pkg)
    app.router.add_get("/codeboss-host-{ver}.tar.gz", h_host_pkg_ver)
    app.router.add_route("*", "/codeboss-{code:[0-9]+}.apk", h_apk_code)
    app.router.add_route("*", "/agentboss-{code:[0-9]+}.apk", h_apk_code)
    app.router.add_post("/r/admin/macdmg", r_admin_macdmg)
    app.router.add_get("/api/mac_ver", h_mac_ver)
    app.router.add_get("/bosshost-mac.dmg", h_mac_dmg)
    app.router.add_get("/codeboss-mac.dmg", h_mac_dmg)   # 老链接兼容
    app.router.add_post("/r/admin/distpkg", r_admin_dist)   # Windows exe / Linux 包上架
    app.router.add_get("/r/admin/oss_put_url", r_admin_oss_put)   # 发布脚本直传 OSS 用
    app.router.add_get("/r/admin/app.apk", r_admin_app_apk)   # 管理后台安卓壳（仅管理员）
    app.router.add_get("/r/admin/app_ver", r_admin_app_ver)   # 管理壳自更新检查
    app.router.add_get("/r/admin/apk_ticket", r_admin_app_ticket)   # 下载票据（壳里甩给系统浏览器用）
    app.router.add_post("/r/admin/adminapk", r_admin_app_up)
    app.router.add_get("/BossHost-Setup.exe", h_win_exe)
    app.router.add_get("/CodeBoss-Setup.exe", h_win_exe)   # 老链接兼容
    app.router.add_get("/codeboss-linux.tar.gz", h_linux_pkg)
    app.router.add_post("/r/update", r_update)
    app.router.add_get("/r/admin", r_admin)
    app.router.add_post("/r/admin/mailtest", r_admin_mailtest)
    app.router.add_post("/r/admin/smstest", r_admin_smstest)
    app.router.add_get("/r/admin_info", r_admin_info)
    app.router.add_post("/r/admin/login", r_admin_login)
    app.router.add_post("/r/admin/logout", r_admin_logout)
    app.router.add_post("/r/admin/sms", r_admin_sms)
    app.router.add_route("*", "/r/admin_api/security", r_admin_security)
    app.router.add_post("/r/update_pull", r_update_pull)
    app.router.add_get("/link", w_link)
    app.router.add_get("/api/apk_ver", h_apk_ver)
    app.router.add_post("/api/track", h_track)      # 功能使用埋点（免认证，页面退后台也能发）
    app.router.add_post("/r/dev_track", r_dev_track)  # 局域网直连的埋点由 host 转过来
    app.router.add_get("/api/ios_ver", h_ios_ver)      # iOS 壳的更新检查
    app.router.add_get("/ios-install", h_ios_install)   # iOS 安装页（itms-services 入口）
    app.router.add_get("/ios.plist", h_ios_plist)        # itms-services 安装清单
    app.router.add_get("/codeboss.ipa", h_ios_pkg)       # iOS 安装包直链
    app.router.add_post("/api/ptt", h_asr)   # 必须排在隧道 catch-all 之前
    app.router.add_post("/api/ptt_fix", h_ptt_fix)   # 项目词表学习（同上）
    app.router.add_post("/api/ptt_proj", h_ptt_proj)   # 项目摘要/术语种子（同上）
    app.router.add_post("/api/llm", h_llm)   # 同上
    app.router.add_post("/api/pet/turn", h_pet_turn)          # 桌宠连不上 host 的兜底
    app.router.add_get("/api/pet/audio/{name}", h_pet_audio)  # 兜底回复的语音
    app.router.add_get("/api/plan", h_plan)   # 套餐/额度（必须在隧道 catch-all 前）
    app.router.add_post("/api/pay_create", h_pay_create)   # 开发机自证下单（Mac 客户端内付款）
    app.router.add_post("/api/pay_status", h_pay_status)   # 开发机自证轮询付款结果
    app.router.add_post("/r/pay/create", r_pay_create)
    app.router.add_get("/r/pay/notify", r_pay_notify)
    app.router.add_get("/r/pay/status", r_pay_status)
    app.router.add_get("/api/direct_info", h_direct_info)
    app.router.add_get("/api/direct_ticket", h_direct_ticket)
    app.router.add_get("/api/bg_ticket", h_bg_ticket)   # 壳的长期提醒凭据（页面交给壳）
    app.router.add_post("/api/rtc_offer", h_rtc_offer)
    app.router.add_get("/api/rtc_ans", h_rtc_ans)
    app.router.add_route("*", "/{path:.*}", tunnel)

    ssl = None
    if a.cert and a.key:
        import ssl as _ssl
        ssl = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
        ssl.load_cert_chain(a.cert, a.key)

    async def _on_startup(app):
        _model_apply(_db(app))          # 后台改过的模型名/密钥，重启后接着用
        app["started"] = time.time()
        app["svc_task"] = asyncio.create_task(_svc_watch(app))
        app["svc_roll_task"] = asyncio.create_task(_svc_roll_loop(app))
        app["presence_task"] = asyncio.create_task(_presence_janitor(app))
        # 迁移前就开着的日志开关没有到期时间：补一个完整窗口，免得永远推下去
        db0 = _db(app)
        db0.execute("UPDATE devices SET logs_until=? WHERE logs_on=1 AND COALESCE(logs_until,0)=0",
                    (time.time() + LOG_MAX_S,))
        db0.commit()
        app["logs_expire_task"] = asyncio.create_task(_logs_expire_loop(app))
        app["unbound_task"] = asyncio.create_task(_unbound_sweep(app))
        app["backup_task"] = asyncio.create_task(_backup_loop(app))
        app["mon_task"] = asyncio.create_task(_sys_watch(app))
        app["net_task"] = asyncio.create_task(_net_watch(app))
        app["tunnel_task"] = asyncio.create_task(_tunnel_flush_loop(app))
        app.on_shutdown.append(_tunnel_shutdown)
        if ALI_AK_ID and ALI_AK_SECRET:
            app["balance_task"] = asyncio.create_task(_balance_watch(app))

    app.on_startup.append(_on_startup)

    async def _serve():
        """单 Runner 双 Site：http 照常服务老客户端；https 给浏览器——
        getUserMedia（麦克风/摄像头）只给安全上下文，纯 http 的页面永远拿不到。
        （同一个 app 不能套两个 AppRunner：setup 会再触发一遍 on_startup，递归爆栈）"""
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, a.host, a.port).start()
        if ssl is not None:
            await web.TCPSite(runner, a.host, a.tls_port, ssl_context=ssl).start()
        print("[relay] listening on %s:%d%s  db=%s  max_devices=%s"
              % (a.host, a.port, (" + %d tls" % a.tls_port) if ssl else "",
                 a.db, MAX_DEVICES or "∞"), flush=True)
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
