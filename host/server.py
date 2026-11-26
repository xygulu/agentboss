#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PTY Host（Codeboss）—— 一个 PTY，多个客户端同时接入（手机 + 电脑共享同一个会话）

为什么需要服务端终端仿真器（pyte）
- Claude Code 的 TUI（Ink）用「相对光标移动」重绘：\x1b[19B 往下跳 19 行、\x1b[K 擦到行尾
- 这类指令假定「当前光标位置」和「终端行列数」与它上次绘制时完全一致
- 所以：回放被截断的原始字节流会永久错位；客户端行列数与服务端不一致也会永久错位
- 解法：服务端用 pyte 维护一份权威屏幕，客户端接入时发「清屏 + 整屏重绘」的快照，
  之后双方屏幕与光标完全同步，实时增量才接得上
"""
import asyncio
import base64
import pathlib as _pathlib
import sys as _sys
# 仓库按组件分目录（host/ 与 shared/ 是兄弟）；发布包是扁平的，品牌模块就在旁边。
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent / "shared"))
import brand_icons
import branding
import hashlib
import html as _html_mod
import json
import mimetypes
import os
import random
import re
import signal
import atexit
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

import pyte
from aiohttp import WSMsgType, web
from pyte.screens import Char

# 插件模块（notify/hooks/transcript…）里写的是 `import server`，必须认领这个名字，
# 否则第一次调用会**再加载一份**。入口有两种模块名，两种都得认领：
#   · 源码模式 `python server.py`            → __main__
#   · 无源码包的薄壳 `from server_impl import main` → server_impl
# 只认 __main__ 时无源码包会加载入口薄壳，而薄壳顶层是 `sys.exit(main())`——
# 于是在**本进程里**再起一个 main()：占着自己的 8790 等 20 秒，同步代码堵住事件循环、
# /api/ping 答不上来 → 判成「旧实例」→ `_kill_proc(自己)`。2026-11-14 全量 0.9.x
# 实测就是每 30 秒自杀重启一次：会话已结束/空、连接开发机不正常。必须在 import
# 别的模块之前认领（源码模式下不认领的后果见 tests/test_server_module_alias.py）。
_self_mod = sys.modules.get(__name__)
if _self_mod is not None:
    sys.modules.setdefault("server", _self_mod)

import plat   # 平台层：macOS=Terminal.app+AppleScript，Linux=tmux，Windows=本机托管
import pty_backend   # PTY 后端：Unix=openpty，Windows=ConPTY（pywinpty）
import agents   # agent 适配层：Claude / Codex 的差异都收在这里，骨架不管具体是谁
import scrollback   # 滚动历史持久仓：重启不清零（独立于 logs/ansi，别碰那边）
import window_source   # 窗口来源判别：pty / protocol / screen 占位（Phase 1 地基）
import agent_acp   # gemini 等 ACP 协议源窗口（T6）：注册即生效，key="gemini"
import agent_codex   # Codex 适配器（rollout 解析、lsof 找活会话、notify 提醒）
import agent_opencode   # opencode 适配器（sqlite 发现、export 转录、session.idle 提醒）
import agent_hermes     # hermes 适配器（state.db 发现、sessions export 转录、on_session_end 提醒）
import codex_daemon   # 桌面版会话的流式通道：直接连 Codex 守护进程跑这一轮
import outcome    # 本轮结果核验：从转录证据判断完成/失败/未核实，不听模型自述
import art_edit   # 产出物文本编辑：文本/大小双闸 + 乐观锁落 diff（.boss/edits/）
import risk_review   # 审批风险 agent 二审：不确定区才问、8s 上限、降级留痕
import remind_rules   # 提醒判定规则（纯函数）：待确认问句校验、na 规则、概括规整
from remind_rules import (
    _act_conflict, _act_grounded, _reply_clean, _reply_decide, _reply_span,
    _reply_split, _say_clip, _na_rule, _na_tail, _NA_WEAK)
import pet   # 桌面工头：桌宠导演 + 板子接口（pet/ 包，只在这里挂三个钩子）
import boss   # 工头（新界面）事实层：会话/审批/提醒 → 三块板（不调模型）
import notify   # 提醒引擎：notify_add / 历史 / 概述 / 语音合成缓存（原名都在，调用点没动）
from notify import (
    _notify_add,
    _nh_add,
    _nh_fill,
    _nh_list,
    _reply_pref,
    _reply_key,
    _reply_llm,
    _reply_get,
    _na_pref,
    _na_llm,
    _na_need,
    _say_style,
    _say_pref,
    _say_decision,
    _say_text,
    _say_relay,
    _say_gen,
    _say_gen_locked,
    _say_files,
    _say_prune,
    _say_prepare,
    _say_cache_loop,
    SAY_STYLE_DEFAULT,
    _LLM_REPLY_PROMPT,
    _NA_PROMPT,
    _NH_CLEAN,
    _NH_DAYS,
    _NH_MAX,
    _NOTIFY_MAX,
    _NOTIFY_Q,
    _NOTIFY_SEQ,
    _PREF,
    _REPLY,
    _REPLY_BUSY,
    _REPLY_TTL,
    _SAY_BUSY,
    _SAY_DIR,
    _SAY_LOCK,
    _SAY_MAX,
    _SAY_RELAY_COOL,
    _SAY_RELAY_FAIL,
    _SAY_STYLES,
    _SAY_TEXTS,
    _SAY_TTL)

import transcript   # 转录定位/解析（看回话、已退出召回、命令识别共用）
from transcript import (
    parse_transcript, project_jsonl, _transcript_meta, _transcript_cmds,
    _claude_closed_sessions, _TRANSCRIPT_META)
import hooks        # Claude 的 hook 安装/自愈（提醒链路入口）
from hooks import (
    _hook_cmd, _write_hook_sh, _load_claude_settings, _is_cb_group, _hooks_mutate,
    _hooks_installed, _hooks_mutate_all, _hooks_installed_all, _hooks_guard,
    _hooks_guard_loop, _hooks_cli,
    HOOK_SH, HOOK_CMD, _CLAUDE_SETTINGS, _HOOK_SPEC, HOOKS_OFF)
import screen       # 屏幕 → HTML：全彩镜像行渲染、CJK 折行、TUI 弹层识别
from screen import (
    _css_color, _dialog_screen, _disp_w, _dlg_clean, _light_bg, _reflow_html,
    _reflow_rows, _row_plain, _row_runs, _runs_html, _cells_fg_bg, _xterm256,
    _DLG_HINT, _DLG_HINTS, _DLG_OPT_RE)
import static_res   # 网页资源（static_pack / static/ 读取、/static 路由）
from static_res import (
    _static_bytes, _static_text, _static_ver, _static_rev, _static_rewrite,
    h_static, STATIC, _STATIC_CACHE, _STATIC_REV)


def _pyte_kw_tolerant(name):
    """pyte 0.8.x 会把 private=True 传给 DSR 类处理器，而 claude 启动时会发 ESC[?6n / ESC[?u
    这类私有查询序列，不兼容会炸掉整个 feed。这里让处理器容忍并丢弃 private 参数。"""
    _f = getattr(pyte.Screen, name, None)
    if not _f:
        return

    def _g(self, *a, **kw):
        kw.pop("private", None)
        return _f(self, *a, **kw)

    setattr(pyte.Screen, name, _g)


for _n in ("report_device_status", "report_device_attributes"):
    _pyte_kw_tolerant(_n)
del _pyte_kw_tolerant, _n


class _CleanStream(pyte.ByteStream):
    """喂进 pyte 前剥掉 ESC[>…（DEC 私有 CSI，如 DECSCUSR 光标形状 ESC[>4m）。

    pyte 的 CSI 解析器把 '>' 直接丢掉（"Secondary DA is not supported atm."），
    于是 ESC[>4m 的 4 被当成 SGR 参数执行 → 屏幕最后几行凭空出现下划线。
    跨块残缺的转义序列要先扣住（_safe_cut），否则 '>' 照样会被丢掉。
    """

    _GT = re.compile(rb"\x1b\[>[0-9;:<=>?]*[\x20-\x2f]*[\x40-\x7e]")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pend = b""

    def feed(self, data):
        self._pend += data
        safe = _safe_cut(self._pend)
        if safe <= 0:
            return
        out = self._GT.sub(b"", self._pend[:safe])
        self._pend = self._pend[safe:]
        super().feed(out)

HERE = Path(__file__).resolve().parent


def _shared(*p):
    """仓库共享资源（products/、clients/android 构建产物）。
    发布包是扁平的，资源就在 HERE 旁；开发检出在仓库根/shared、仓库根/clients。
    都找不到就返回原路径，由调用方按老逻辑回落。"""
    for base in (HERE, HERE.parent, HERE.parent / "shared"):
        q = base.joinpath(*p)
        if q.exists():
            return q
    return HERE.joinpath(*p)


TOKEN_FILE = HERE / "token.txt"
REG_FILE = HERE / "sessions.json"   # 会话注册表：重启后据此恢复列表
HOST = "0.0.0.0"
PORT = 8790
DEFAULT_COLS, DEFAULT_ROWS = 100, 32
# pyte 保留的回滚行数。每个会话的回滚是满宽字符网格（每格一个 Char 对象），
# 3000 行 ≈ 40MB/会话——终端开多几个 host 就几百 MB（2026-11-19 用户实报）。
# 而快照本来就只带最后 SNAP_HIST_MAX=600 行：留 700 行够快照用，多出来的是白占。
HIST_LINES = 700
EOF_HIST_LINES = 200       # 进程退出后只留这么多行：退出会话没人再往下滚了
SNAP_HIST_MAX = 600        # 快照最多带多少行历史，防止消息过大
EXITED_KEEP_SEC = 600      # 进程已退出的终端再留 10 分钟就自动摘掉：结束的终端不关，屏幕全在内存里
OUT_BUF_MAX = 256 * 1024   # 终端输出轮询缓冲上限（字节）

_NPM_BIN = str(Path.home() / ".npm-global/bin")   # npm 全局 bin（claude 常落这里）
IS_WIN = os.name == "nt"
if IS_WIN:
    # Windows 上不能把 PATH 换成一串固定目录：node/npm/git/claude 可能装在任意盘
    # （实测这台在 D:\nodejs、winget 的 links 在 %LOCALAPPDATA%）。保留系统 PATH，
    # 只把几个「装了但没进 PATH」的常见位置补在最前面。
    _WIN_EXTRA = [
        str(Path.home() / ".local" / "bin"),
        str(Path(os.environ.get("APPDATA", "")) / "npm"),
        str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Launcher"),
    ]
    CHILD_PATH = os.pathsep.join(_WIN_EXTRA + [os.environ.get("PATH", "")])
else:
    CHILD_PATH = (
        _NPM_BIN + ":/usr/local/bin:/opt/homebrew/bin:"
        "/usr/bin:/bin:/usr/sbin:/sbin"
    )


def load_tokens() -> list:
    """token.txt：每行一个 token，# 开头为注释。文件不存在/为空则生成一个。"""
    if TOKEN_FILE.exists():
        ts = [ln.strip() for ln in TOKEN_FILE.read_text().splitlines()]
        ts = [t for t in ts if t and not t.startswith("#")]
        if ts:
            return ts
    t = uuid.uuid4().hex[:16]
    TOKEN_FILE.write_text(t + "\n")
    TOKEN_FILE.chmod(0o600)
    return [t]


_TOKENS = load_tokens()
TOKEN = _TOKENS[0]   # 主 token：cookie 下发/页面跳转/term.html 内嵌都用它
_TOK_MTIME = TOKEN_FILE.stat().st_mtime


def _tokens_current() -> list:
    """改 token.txt 即生效（按 mtime 热加载），不用重启服务"""
    global _TOKENS, _TOK_MTIME
    try:
        m = TOKEN_FILE.stat().st_mtime
    except OSError:
        return _TOKENS
    if m != _TOK_MTIME:
        _TOK_MTIME = m
        ts = load_tokens()
        if ts:
            _TOKENS = ts
    return _TOKENS

# ---------------- 屏幕渲染（带颜色） ----------------

FG16 = {"black": 30, "red": 31, "green": 32, "brown": 33,
        "blue": 34, "magenta": 35, "cyan": 36, "white": 37}
FG16B = {"brightblack": 90, "brightred": 91, "brightgreen": 92, "brightbrown": 93,
         "brightblue": 94, "brightmagenta": 95, "brightcyan": 96, "brightwhite": 97}
BG16 = {"black": 40, "red": 41, "green": 42, "brown": 43,
        "blue": 44, "magenta": 45, "cyan": 46, "white": 47}
BG16B = {"brightblack": 100, "brightred": 101, "brightgreen": 102, "brightbrown": 103,
         "brightblue": 104, "brightmagenta": 105, "brightcyan": 106, "brightwhite": 107}


def _hexcolor(v):
    if isinstance(v, str) and len(v) == 6:
        try:
            return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        except ValueError:
            return None
    return None


def _fg(v):
    if v == "default":
        return "39"
    h = _hexcolor(v)
    if h:
        return "38;2;%d;%d;%d" % h
    if v in FG16:
        return str(FG16[v])
    if v in FG16B:
        return str(FG16B[v])
    if isinstance(v, int):
        return "38;5;%d" % v
    return "39"


def _bg(v):
    if v == "default":
        return "49"
    h = _hexcolor(v)
    if h:
        return "48;2;%d;%d;%d" % h
    if v in BG16:
        return str(BG16[v])
    if v in BG16B:
        return str(BG16B[v])
    if isinstance(v, int):
        return "48;5;%d" % v
    return "49"


def _term_color(v, default):
    """终端色 → CSS 色：default→页面默认色；名字/真彩→CSS；256 色整数→xterm 调色板"""
    if v is None or v == "default":
        return default
    c = _css_color(v)
    if c:
        return c
    if isinstance(v, int) and 0 <= v < 256:
        return _xterm256(v)
    return default


def _fg_sgr(fg, bg, reverse=False):
    """前景色 SGR。浅底在 _row_sgr 里整块拿掉了（灰字+不发底），进到这里的
    背景都是深色/默认，直接按原色发。反显的底/字由 xterm 自己交换，不动。"""
    return _fg(fg)


def _row_text(row, cols):
    """把 pyte 的一行渲染成带 SGR 的字符串，去掉行尾空白"""
    last = -1
    for x in range(cols):
        ch = row[x]
        if ch.data.strip() or ch.bg != "default" or ch.reverse:
            last = x
    if last < 0:
        return ""
    out = []
    prev = None
    for x in range(last + 1):
        ch = row[x]
        attr = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse)
        if attr != prev:
            code = ["0"]
            if ch.bold:
                code.append("1")
            if ch.italics:
                code.append("3")
            if ch.underscore:
                code.append("4")
            if ch.reverse:
                code.append("7")
            code.append(_fg(ch.fg))
            code.append(_bg(ch.bg))
            out.append("\x1b[" + ";".join(code) + "m")
            prev = attr
        out.append(ch.data)
    out.append("\x1b[0m")
    return "".join(out)


def _agent_of_cmd(cmd):
    """argv[0] → agent key，不是 agent 就 None（集合跟着注册表走，不写死）。
    Windows 上的入口是 claude.cmd/claude.exe，后缀一并剥掉。"""
    if not cmd:
        return None
    # 两个平台的路径分隔符都认（测试/日志里也会混着看）
    base = re.split(r"[\\/]", str(cmd[0]))[-1].lower()
    for suf in (".exe", ".cmd", ".bat"):
        if base.endswith(suf):
            base = base[:-len(suf)]
            break
    keys = {ad.key.lower() for ad in agents.adapters()}
    return base if base in keys else None


def _pick_agent(data):
    """data["agent"] -> (适配器, 报错)。空/缺 → 默认 agent；未知 key → 显式报错。

    以前是 `agents.get(key) or default()`：手滑传个不存在的 key 就静默开成 Claude，
    用户以为在跟 opencode 说话，实际另一个进程在跑——宁可把话说破。"""
    key = str((data or {}).get("agent") or "").strip()
    if not key:
        return agents.default(), ""
    ad = agents.get(key)
    if ad is None:
        return None, "没装或不支持这个工具: %s" % key
    return ad, ""


class Session:
    def __init__(self, sid, name, cwd, cmd, cols=DEFAULT_COLS, rows=DEFAULT_ROWS):
        self.sid = sid
        self.name = name
        self.cwd = cwd
        self.cmd = cmd
        self.agent = _agent_of_cmd(cmd)   # host 自己托管的 agent 会话（Windows）；命令终端为 None
        self.ghost = False
        self.clients: set = set()
        self._tasks: set = set()
        self.cols, self.rows = cols, rows
        self.screen = pyte.HistoryScreen(cols, rows, history=HIST_LINES)
        self.stream = _CleanStream(self.screen)
        self.alive = True
        self.started = time.time()
        self.last_out = time.time()
        self.exited_at = 0.0   # 退出时刻：_reap_exited 按它算宽限期
        self.exit_code = None
        self._snap_handle = None
        self.out_buf = bytearray()   # 最近输出（绝对偏移 out_start 起，给 HTTP 轮询用）
        self.out_start = 0
        self.out_ev = asyncio.Event()  # 有新输出 → 唤醒轮询者
        self.in_pq = ""       # 输入去重：客户端页面标识 + 最后应用过的序号
        self.in_q = 0
        self._loop = asyncio.get_running_loop()
        self._gen = 0          # 重启代次：旧 reader 的迟到回调靠它丢弃
        self._mlog = None      # Windows：原始输出落盘（全彩镜像读的就是它）
        self._mmeta = None
        self._mmeta_path = None
        self._spawn()
        self._start_reader()

    # ---- 子进程 ----
    def _spawn(self):
        """起 PTY：Unix 是 openpty+Popen，Windows 是 ConPTY（都在 pty_backend 里）。
        上层只认 self.pid / self.master（Windows 上 master 为 None）。"""
        env = os.environ.copy()
        env.update({
            "PATH": CHILD_PATH,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": "en_US.UTF-8",
        })
        env.pop("VIRTUAL_ENV", None)
        # host 有时是从一个 Claude Code 会话里手起的（开发机）：带这个标记的子进程
        # 会关掉转录保存，会话历史全丢——聊天页/召回都靠转录，必须摘掉。
        env.pop("CLAUDE_CODE_CHILD_SESSION", None)
        self._pty = pty_backend.spawn(self.cmd, self.cwd, env, self.cols, self.rows)
        self.master = self._pty.fd
        self.pid = self._pty.pid
        if IS_WIN:
            self._mirror_open()

    # ---- 全彩镜像（Windows）----
    def _mirror_open(self):
        """Windows 会话没有 wrapper：自己把 ConPTY 原始输出落盘、写一份同款 meta。
        手机端认 meta（_meta_for_pid → live/hist 全彩），所以这样 Windows 会话在 App
        里和 mac 上 wrapper 起的会话是同一个界面；没有 wrapper 那条 App 只能拿到纯文本屏幕。"""
        try:
            if self._mlog:
                try:
                    self._mlog.close()
                except Exception:
                    pass
                self._mlog = None
            if not self.agent:
                return
            ANSI_DIR.mkdir(parents=True, exist_ok=True)
            log = ANSI_DIR / (self.sid + ".log")
            self._mmeta_path = ANSI_DIR / (self.sid + ".meta.json")
            self._mlog = open(log, "ab", buffering=0)   # 无缓冲：size 一变镜像立刻能读到
            self._mmeta = {"sid": self.sid, "pid": self.pid, "cmd": list(self.cmd),
                           "cwd": self.cwd, "log": str(log), "host": True,
                           "agent": self.agent, "rows": self.rows, "cols": self.cols,
                           "started": self.started}
            self._mirror_meta_write()
        except Exception as e:
            self._mlog = None
            print("[mirror] Windows 镜像日志没起来 sid=%s：%s"
                  % (self.sid[:8], str(e)[:120]), flush=True)

    def _mirror_meta_write(self):
        try:
            # 原子替换：后台每秒钟全量读这些 meta，写一半会读到半个 JSON 被跳过
            tmp = self._mmeta_path.with_suffix(".part")
            tmp.write_text(json.dumps(self._mmeta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._mmeta_path)
        except Exception:
            pass

    def _mirror_close(self, code=None):
        """会话结束：日志收手，meta 标 exited（_ansi_metas 会跳过，App 回落纯文本屏）。"""
        if self._mlog is not None:
            try:
                self._mlog.close()
            except Exception:
                pass
            self._mlog = None
        if self._mmeta:
            self._mmeta["exited"] = True
            self._mmeta["exit_code"] = code
            self._mirror_meta_write()

    def _start_reader(self):
        """接上输出。可能在事件循环线程之外调用（restart 在 executor 里跑），
        那时不能直接 loop.add_reader，要扔回循环线程再挂。"""
        gen = self._gen

        def _chunk(c):
            if gen == self._gen:
                self._on_chunk(c)

        def _eof():
            if gen == self._gen:
                self._on_eof()

        try:
            asyncio.get_running_loop()
            self._pty.start_reader(self._loop, _chunk, _eof)
        except RuntimeError:
            self._loop.call_soon_threadsafe(self._pty.start_reader, self._loop, _chunk, _eof)

    def restart(self, cmd):
        """同一个终端里换进程重开（Windows 切模型用）：杀旧的、按新 argv 起一个。
        sid / 尺寸 / WS 客户端都保留，屏幕清一下，输出的绝对偏移继续往前推。"""
        self._gen += 1                 # 旧 reader 之后的输出和 EOF 一律作废
        old = self._pty
        try:
            old.kill()
        except Exception:
            pass
        self.cmd = list(cmd)
        self.agent = _agent_of_cmd(self.cmd)
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=HIST_LINES)
        self.stream = _CleanStream(self.screen)
        self.alive = True
        self.exit_code = None
        self.started = time.time()
        self.last_out = time.time()
        self._spawn()
        self.broadcast_bytes("\x1b[2J\x1b[H\x1b[2m[已切换模型，重新拉起会话]\x1b[0m\r\n"
                             .encode("utf-8"))
        self._start_reader()

    # ---- 输出 ----
    def _on_chunk(self, chunk):
        try:
            self.stream.feed(chunk)
        except Exception as e:
            # pyte 抛异常时这段字节的「重排视图」就丢了，解析器还可能停在半个转义序列上：
            # 以前静默 pass，现场一点线索都没有。留日志 + 换一个新解析器，别让错误累积。
            self._feed_err = getattr(self, "_feed_err", 0) + 1
            now = time.time()
            if now - getattr(self, "_feed_err_at", 0) > 60:
                self._feed_err_at = now
                print("[screen] feed 异常（累计 %d 次，sid=%s）：%s"
                      % (self._feed_err, self.sid[:8], str(e)[:120]), flush=True)
            try:
                self.stream = _CleanStream(self.screen)
            except Exception:
                pass
        self.last_out = time.time()
        self.broadcast_bytes(chunk)

    def _on_eof(self):
        if not self.alive:
            return
        self.alive = False
        code = None
        try:
            code = self._pty.proc.poll()
        except Exception:
            code = getattr(self._pty.proc, "exitstatus", None)   # ConPTY 没有 poll()
        self.exit_code = code
        self.exited_at = time.time()
        self.broadcast_bytes(
            ("\r\n\x1b[2m[进程已退出 code=%s]\x1b[0m\r\n" % self.exit_code).encode())
        self._mirror_close(self.exit_code)
        _trim_history(self.screen, EOF_HIST_LINES)   # 退出的会话别把整段回滚带在内存里
        save_registry()

    def _send_bytes(self, ws, data):
        if ws.closed:
            return
        t = asyncio.create_task(ws.send_bytes(data))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _send_text(self, ws, text):
        if ws.closed:
            return
        t = asyncio.create_task(ws.send_str(text))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def broadcast_bytes(self, chunk: bytes):
        if self._mlog is not None:
            try:
                self._mlog.write(chunk)      # 全彩镜像的日志（Windows，和 wrapper 的 log 同一种东西）
            except Exception:
                pass
        # 追加到轮询缓冲（中继不带 WS 升级，手机端只能 HTTP 拉字节）
        self.out_buf.extend(chunk)
        if len(self.out_buf) > OUT_BUF_MAX:
            cut = len(self.out_buf) - OUT_BUF_MAX
            del self.out_buf[:cut]
            self.out_start += cut
        ev = self.out_ev
        self.out_ev = asyncio.Event()
        ev.set()
        for ws in list(self.clients):
            self._send_bytes(ws, chunk)

    def out_off(self):
        """已产出的输出字节总数（绝对偏移）"""
        return self.out_start + len(self.out_buf)

    def write(self, data: bytes):
        if not self.alive:
            return
        self._pty.write(data)

    # ---- 尺寸 ----
    def resize(self, cols, rows):
        cols = max(20, min(300, int(cols)))
        rows = max(6, min(120, int(rows)))
        if (cols, rows) == (self.cols, self.rows):
            return False
        self.cols, self.rows = cols, rows
        self._pty.resize(cols, rows)
        try:
            self.screen.resize(rows, cols)
        except Exception:
            pass
        if self._mmeta:
            # 镜像网格按 meta 里的列数建，尺寸变了必须跟着改（否则全彩页错位）
            self._mmeta.update(cols=cols, rows=rows)
            self._mirror_meta_write()
        self.schedule_snapshot(0.35)
        return True

    # ---- 快照 ----
    def snapshot(self) -> str:
        s = self.screen
        cols = s.columns
        hist = list(getattr(s.history, "top", []))[-SNAP_HIST_MAX:]
        lines = [_row_text(row, cols) for row in hist]

        # 显示区实际用到的行数（去掉底部空行）
        used = 0
        for y in range(s.lines):
            if s.display[y].strip():
                used = y + 1
        disp = [_row_text(s.buffer[y], cols) for y in range(used)]

        total = len(lines) + len(disp)
        scroll = max(0, total + 1 - s.lines)
        out = ["\x1b[2J\x1b[H"]
        for ln in lines:
            out.append(ln + "\r\n")
        for i, ln in enumerate(disp):
            out.append(ln)
            if i != len(disp) - 1:
                out.append("\r\n")
        crow = len(lines) + s.cursor.y + 1 - scroll
        crow = max(1, min(s.lines, crow))
        ccol = max(1, min(cols, s.cursor.x + 1))
        out.append("\x1b[%d;%dH" % (crow, ccol))
        return "".join(out)

    def broadcast_snapshot(self):
        snap = self.snapshot()
        msg = json.dumps({"t": "snap", "d": snap, "c": self.cols, "r": self.rows})
        for ws in list(self.clients):
            self._send_text(ws, msg)

    def schedule_snapshot(self, delay=0.4):
        if self._snap_handle is not None:
            try:
                self._snap_handle.cancel()
            except Exception:
                pass
        self._snap_handle = self._loop.call_later(delay, self.broadcast_snapshot)

    def kill(self):
        try:
            self._pty.kill()
        except Exception:
            pass
        self.alive = False
        self._mirror_close()


SESSIONS: dict = {}


def _trim_history(screen, n):
    """按住内存：pyte 回滚是满宽字符网格，行数直接决定每个会话几十 MB 的占用。
    只留最近 n 行（快照只取最后 SNAP_HIST_MAX 行，上面的是白存）。"""
    try:
        h = getattr(screen, "history", None)
        for dq in (getattr(h, "top", None), getattr(h, "bottom", None)):
            while dq is not None and len(dq) > n:
                dq.popleft()
    except Exception:
        pass


def _reap_exited(now=None):
    """退出超过宽限期、也没人连着的终端：从会话表和 sessions.json 里摘掉。
    摘掉后 Session 上的 pyte 屏幕和输出缓冲跟着回收——「终端开多了就一直占内存」
    的主因就是这些已结束的会话没人关（2026-11-19 用户要求自动关掉）。
    返回摘掉的 sid 列表。"""
    now = time.time() if now is None else now
    dead = [sid for sid, s in list(SESSIONS.items())
            if not getattr(s, "ghost", False) and not s.alive and not s.clients
            and now - (getattr(s, "exited_at", 0.0) or getattr(s, "last_out", 0.0))
            >= EXITED_KEEP_SEC]
    if not dead:
        return dead
    for sid in dead:
        SESSIONS.pop(sid, None)
        with _MIRROR_LOCK:                 # 手机端看过的回放屏也是满宽网格，一起放掉
            _MIRROR.pop(sid, None)
        _MIRROR_REDRAW_AT.pop(sid, None)
        _TTY_SIZE_CACHE.pop(sid, None)
    save_registry()
    return dead


async def _reap_loop(app):
    while True:
        await asyncio.sleep(60)
        try:
            dead = _reap_exited()
            if dead:
                print("[reap] 自动关掉 %d 个已退出的终端：%s"
                      % (len(dead), ", ".join(s[:8] for s in dead)), flush=True)
        except Exception:
            pass


async def _reap_startup(app):
    app["reap_task"] = asyncio.create_task(_reap_loop(app))


class Ghost:
    """服务重启后从 sessions.json 恢复的占位会话：进程已不在，可一键重新拉起"""
    def __init__(self, sid, name, cwd, cmd, started, cols, rows, exit_code=None):
        self.sid = sid
        self.name = name
        self.cwd = cwd
        self.cmd = cmd
        self.started = started
        self.cols, self.rows = cols, rows
        self.alive = False
        self.exit_code = exit_code
        self.clients: set = set()
        self.ghost = True
        self.agent = _agent_of_cmd(cmd)

    def kill(self):
        pass


def _has_resume_flag(args):
    return any(a in ("-c", "--continue", "-r", "--resume") for a in args)


def _respawn_cmd(cmd):
    """重新拉起时，claude 会话自动接续上一次对话（--continue 按 cwd 找最近会话）。

    第一次创建用的 `--session-id <id>` 不能原样再用：那个 id 已经被 claude 占住，
    重开会报「session id 已被使用」。改成 `--resume <id>`，接续的正是同一个会话。"""
    out = list(cmd)
    _base = os.path.basename(out[0]).lower() if out else ""
    if _base not in ("claude", "claude.exe", "claude.cmd") or _has_resume_flag(out[1:]):
        return out
    for i, a in enumerate(out):
        if a == "--session-id" and i + 1 < len(out):
            out[i] = "--resume"
            return out
    return out + ["--continue"]


def _entry_from_session(s):
    return {
        "sid": s.sid, "name": s.name, "cwd": s.cwd, "cmd": list(s.cmd),
        "started": s.started, "alive": bool(s.alive), "exit_code": s.exit_code,
        "cols": s.cols, "rows": s.rows,
    }


def save_registry():
    try:
        data = [_entry_from_session(s) for s in SESSIONS.values()]
        REG_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def load_registry():
    """启动时恢复上次的会话列表：全部标记为「随重启失效」，等用户重新拉起"""
    try:
        data = json.loads(REG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    now = time.time()
    n = 0
    for e in data:
        sid = e.get("sid")
        if not sid or sid in SESSIONS:
            continue
        if now - e.get("started", 0) > 24 * 3600:
            # 一天前的旧终端不再恢复：它们只会变成「已失效」占位条堆在列表里
            # （服务每次自动更新重启都会把当时开着的终端全变成占位行）
            continue
        SESSIONS[sid] = Ghost(sid, e.get("name") or "session",
                              e.get("cwd") or str(Path.home()),
                              e.get("cmd") or ["claude"],
                              e.get("started", now),
                              e.get("cols", DEFAULT_COLS), e.get("rows", DEFAULT_ROWS),
                              e.get("exit_code"))
        n += 1
    save_registry()
    return n


# ============ 电脑上的会话：发现 / 读取 / 遥控 Terminal.app 里的 Claude Code ============
# 读：tail ~/.claude/projects/<proj>/<sid>.jsonl（权威对话内容）
# 写：AppleScript `do script`——实测可透传控制字节（ESC/0x03 等），不抢焦点、不需要
#     辅助功能权限；仅方向键走 System Events key code（会抢焦点，需辅助功能授权）
# 发现：claude agents --json 拿 pid → ps 拿 tty → osascript 把 tty 映射到窗口

def _find_claude() -> str:
    """定位本机 claude 可执行文件：环境变量 > 常见安装位 > PATH。
    别人机器上的安装位置不同，写死路径会直接废掉"读取本机 claude"这个核心功能。"""
    env = os.environ.get("PTYHOST_CLAUDE_BIN")
    if env and Path(env).exists():
        return env
    home = Path.home()
    if IS_WIN:
        # Windows：winget 装的在 WinGet\Links，npm 装的在 %APPDATA%\npm（claude.cmd），
        # 原生安装器在 ~/.local/bin。.cmd 由 _run/_win_cmd 自动交给 cmd.exe。
        import shutil
        for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "claude.exe",
                     Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
                     home / ".local" / "bin" / "claude.exe",
                     home / ".local" / "bin" / "claude.cmd"):
            if cand.exists():
                return str(cand)
        return shutil.which("claude") or "claude"
    # 全是 Path：混进裸字符串时 cand.exists() 会 AttributeError（本机第一个候选存在
    # 所以没暴露；别人机器上没 ~/.npm-global 就开机崩）
    for cand in map(Path, (home / ".npm-global/bin/claude", "/opt/homebrew/bin/claude",
                           "/usr/local/bin/claude", home / ".local/bin/claude",
                           "/usr/bin/claude", "/snap/bin/claude")):
        if cand.exists():
            return str(cand)
    # systemd/launchd 下 PATH 受限，最后在我们的 PATH 里找一遍
    import shutil
    found = shutil.which("claude", path=(str(home / ".npm-global/bin") +
                                         ":/usr/local/bin:/opt/homebrew/bin:"
                                         "/usr/bin:/bin:/usr/sbin:/sbin"))
    return found or "claude"


CLAUDE_BIN = _find_claude()
WRAPPER = HERE / "wrapper.py"
ANSI_DIR = HERE / "logs" / "ansi"


def _load_cfg():
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


_CFG = _load_cfg()


# ---- 产品线（设计文档 D5/C2：一套底座，客户端按产品线见能力面；缺省 codeboss，存量零感知）----

def _host_product():
    """这台开发机自己的产品线（config.json 顶层 product，随 enroll 上报 relay）。

    分线架构（2026-11-20）：这是「本机是谁」，只给发布侧/本机身份用（版本上报、
    端口探针、中继登记）。**客户端可见的一切**——页面品牌位、供包、能力面——
    必须走 _client_product_for / _apk_product_for，因为一个 host 同时伺候两条线的
    客户端。handler 里出现 _host_product() 会被 tests/test_line_architecture.py 拦下。"""
    return branding.valid_product((_load_cfg() or {}).get("product"))


def _pair_full(base, pair_url):
    """配对页完整 URL：agentboss 线追加 &product=agentboss——系统相机扫码进浏览器
    注册的用户也能落对产品线（pair_url 恒含 ?p=，直接拼 &）。"""
    url = base + pair_url
    if _host_product() == "agentboss":
        url += "&product=agentboss"
    return url


def _client_product(headers, cfg=None, cookies=None):
    """客户端所属产品线：头 X-Product 优先（合法才认），其次壳种的 cookie
    cb_product（批次一的主发送方，WebView 页面发起的所有请求都自带），
    回落配置 dev_client_product（联调开关），再跟随本机 host 自己登记的产品线。
    头/cookie 只是体验差异（能力面过滤），权限仍由 token 管——不是安全边界。"""
    cfg = _CFG if cfg is None else cfg
    h = (headers or {}).get("X-Product") or ""
    if h:
        return branding.valid_product(h)
    ck = (cookies or {}).get("cb_product") or ""
    if ck:
        return branding.valid_product(ck)
    return branding.valid_product((cfg or {}).get("dev_client_product") or _host_product())


def _client_product_for(request):
    """handler 里用：query + headers + cookies 一起看。调用点统一走它，别再只传 headers。
    ?product= 排第一：壳的首屏 URL 自带它——cookie 没种下的那个窗口（装机第一次打开、
    刚配对换地址）全靠它，否则两条线会渲染成同一个页面（2026-11-20 用户）。"""
    q = ((getattr(request, "query", None) or {}).get("product") or "").strip().lower()
    if q in branding.PRODUCTS:
        return q
    return _client_product(getattr(request, "headers", None) or {}, None,
                           getattr(request, "cookies", None) or {})


_CODING_FACE = frozenset({"claude", "codex", "opencode"})
_FACE_CACHE = {}


def _agent_face(product):
    """产品线可见的 agent 集合；None = 不过滤（agentboss 全量）。
    单一来源 = products/<线>/product.json 的 face_agents（2026-11-19 计划阶段 0 接线）；
    读不到/脏值回落内置 coding 面。配置在 host 重启后生效。"""
    product = branding.valid_product(product)
    if product in _FACE_CACHE:
        return _FACE_CACHE[product]
    face = _CODING_FACE if product != branding.PRODUCT_AGENTBOSS else None
    try:
        with open(_shared("products", product) / "product.json", encoding="utf-8") as f:
            v = json.load(f).get("face_agents")
        if v is None:
            face = None
        else:
            face = frozenset(str(x) for x in v) or face
    except Exception:
        pass
    _FACE_CACHE[product] = face
    return face


def _face_only(out, face):
    """按 face 过滤会话/台账/提醒行（face None = 原样）。行里没带 agent 的（离线台账行）
    按 sid 查登记表/转录推出来，不默认成 claude——默认会把 hermes 漏过去。"""
    if face is None:
        return out
    keep = []
    for x in out:
        a = x.get("agent") or agents.agent_of(str(x.get("sid") or ""), x.get("cwd") or "")
        if a in face:
            keep.append(x)
    return keep


def _hdr(request, name):
    """头大小写不敏感地取一个值（aiohttp 是 CIMultiDict，测试里是普通 dict）。"""
    h = getattr(request, "headers", None) or {}
    v = h.get(name)
    if v:
        return v
    ln = name.lower()
    for k, v2 in h.items():
        if str(k).lower() == ln:
            return v2
    return ""


def _desktop_window(request):
    """本机桌面窗口（BossHost 菜单栏）——看全量 agent，hermes 也要能管。

    判据：127.0.0.1 进来、自己没声明产品线（?product= / X-Product / cb_product）、
    也不是中继隧道（X-CB-Via）或 P2P 直连（dc=1）转进来的。后两种同样从 127.0.0.1
    进来且 cookie 被剥掉，不认出来就会把 codeboss 手机误当桌面窗口、冒出 hermes。
    """
    try:
        if not _peer_is_local(request):
            return False
        q = getattr(request, "query", None) or {}
        if q.get("dc") == "1" or (q.get("product") or "").strip().lower() in branding.PRODUCTS:
            return False
        if _hdr(request, "X-Product") or _hdr(request, "X-CB-Via"):
            return False
        if (getattr(request, "cookies", None) or {}).get("cb_product"):
            return False
        return True
    except Exception:
        return False


def _face_for(request):
    """这条请求该按哪个能力面过滤：桌面窗口 None（全量），其余按产品线。"""
    if _desktop_window(request):
        return None
    return _agent_face(_client_product_for(request))


def _face_product(request):
    """给 _agents_installed 用：桌面窗口 None（装了什么报什么），手机走产品线。"""
    return None if _desktop_window(request) else _client_product_for(request)


def _migrate_relay_cfg():
    """旧默认中继域名 relay.example.com（明文 8791 或后来补的 https 443）→ 新主域名。
    只改我们自己的域名，用户自填的地址不动。"""
    rc = _CFG.get("relay") or {}
    url = (rc.get("url") or "").rstrip("/")
    if not re.match(r"^https?://codeboss\.example\.top(:\d+)?$", url, re.I):
        return
    rc["url"] = branding.RELAY_URL_DEFAULT
    _CFG["relay"] = rc
    try:
        (HERE / "config.json").write_text(
            json.dumps(_CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[host] 中继地址已升级为 %s" % branding.RELAY_URL_DEFAULT)
    except Exception as e:
        print("[host] 写回 config.json 失败: %s" % e)


_migrate_relay_cfg()
AUTO_MIGRATE = bool(_CFG.get("auto_migrate", True))   # 部署即自动：空闲的旧会话自动转全彩
PROTECT_CWD_PREFIX = tuple(_CFG.get("protect_cwd_prefixes", [str(HERE.parent)]))  # 基建会话不自动迁
EXT_PATH_ENV = CHILD_PATH   # 与子进程 PATH 同口径（Windows 上必须保留系统 PATH）

# 终端通道（AppleScript / tmux）的实现都在 plat.py 里：macOS 走 Terminal.app，
# Linux 走 tmux。这里只保留公共部分。


def _user_shell() -> str:
    """开交互 shell 用哪个可执行文件。mac 习惯 zsh；Linux 机器上多半是 bash，
    /bin/zsh 在很多 Linux 上压根不存在，硬写会直接起不来。Windows 有 Git Bash
    就用 bash（Claude Code 也依赖它），否则 cmd.exe。"""
    if IS_WIN:
        return pty_backend.find_git_bash() or os.environ.get("COMSPEC", "cmd.exe")
    cands = [os.environ.get("SHELL")]
    cands.append("/bin/zsh" if plat.IS_MAC else "/bin/bash")
    cands += ["/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/zsh"]
    for c in cands:
        if c and Path(c).exists():
            return c
    return "/bin/sh"


def _win_cmd(cmd):
    """Windows：.cmd/.bat（npm、claude 的 shim）不能被 CreateProcess 直接执行，
    要交给 cmd.exe /c 代跑；其它平台原样返回。"""
    if not IS_WIN or not isinstance(cmd, (list, tuple)) or not cmd:
        return cmd
    exe = str(cmd[0])
    if exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                subprocess.list2cmdline([str(c) for c in cmd])]
    return cmd


def _run(cmd, inp=None, timeout=12, env_extra=None):
    env = os.environ.copy()
    env["PATH"] = EXT_PATH_ENV
    env.pop("VIRTUAL_ENV", None)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    try:
        p = subprocess.run(_win_cmd(cmd), input=inp, capture_output=True, text=True,
                           timeout=timeout, env=env,
                           creationflags=pty_backend.win_no_window())
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def _window_map():
    """tty -> (window_id, 标题)。平台相关：macOS 是 Terminal.app 的窗口，
    Linux 是 tmux 的面板。"""
    return plat.window_map()


def _pid_tty(pid):
    if IS_WIN:
        return ""      # Windows 没有 tty 概念；注入只对 host 自己托管的会话生效
    _, out, _ = _run(["ps", "-o", "tty=", "-p", str(pid)])
    t = out.strip()
    return ("/dev/" + t) if t and t != "??" else ""


def _ps_tree():
    """pid -> (ppid, tty)。一次拿全机进程表。

    发现会话时每个 pid 都要 tty、还要父进程的 tty：挨个 `ps` 一轮 30 多个短命进程、
    每 2 秒一轮（实测 10 个会话 = 31 个 ps/轮）。整张表一次就够，别按 pid 挨个问。"""
    _, out, _ = _run(["ps", "-A", "-o", "pid=,ppid=,tty="])
    m = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        try:
            m[int(p[0])] = (int(p[1]), p[2])
        except ValueError:
            pass
    return m


def _ps_cmd():
    """pid -> (ppid, tty, 命令行)。后台会话的 attach 客户端只能按命令行认出来。"""
    _, out, _ = _run(["ps", "-A", "-o", "pid=,ppid=,tty=,command="])
    m = {}
    for line in out.splitlines():
        p = line.split(None, 3)
        if len(p) < 3:
            continue
        try:
            m[int(p[0])] = (int(p[1]), p[2], p[3] if len(p) > 3 else "")
        except ValueError:
            pass
    return m


_ext_cache = {"at": 0.0, "data": []}
_disc_lock = threading.Lock()     # discover 单飞：一轮 1.2s，多份并发只会互相拖慢

# 推送 tick 的共享会话表：discover() 每次要跑 `claude agents --json` 子进程，
# N 条 SSE 连接 = 每 tick N 次子进程 + N 次转录文件 stat，事件循环被拖死（慢/推送断流）。
# 进程级缓存：所有连接在同一 tick 内共用一份。
_tick_cache = {"at": 0.0, "data": []}


_DISC_ERR = {"m": ""}      # discover 上一次的报错；同样的错只打一次


def _dedupe_sessions(items):
    """同一 sid 只留一条。claude agents 偶尔把同一会话列成两个进程（2026-11-22
    「项目价值与前景」两个 interactive）：列表两张一样的卡、会话页两个圆点一起亮，
    滑动还卡死。留看得见的那个（有窗口/tty），都不/都有就留先出现的。"""
    seen = {}
    out = []
    for x in items:
        k = x.get("sid")
        if k not in seen:
            seen[k] = len(out)
            out.append(x)
            continue
        old = out[seen[k]]
        if not (old.get("window_id") or old.get("tty")) and (x.get("window_id") or x.get("tty")):
            out[seen[k]] = x
    return out


def _discover(force=False):
    """discover 的兜底。以前出错直接吞成空列表：手机那边「一个会话都没有」，
    日志里却干干净净，完全看不出是发现炸了还是机器上真没会话。"""
    try:
        d = discover(force=force)
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, str(e)[:160])
        if msg != _DISC_ERR["m"]:
            _DISC_ERR["m"] = msg
            print("[disc] 发现会话出错（按空列表处理）：%s" % msg, flush=True)
        return []
    _DISC_ERR["m"] = ""
    return d


def _tick_sessions(force=False):
    """共享会话表。只在 executor 里调（调用方负责挪出事件循环）。

    先吃后台 `_push_sessions_loop` 每 2s 刷出来的 `_ext_cache`（4s 内都算新鲜）：
    以前这里只有 0.3s 热缓存，缓存一过期调用方就在 executor 里同步跑一遍
    discover——`osascript` 列窗口 + 每个会话一条 `ps`，实测 1.2s。手机端进会话
    时 ext_msg / ext_live / ext_sessions 同时打过来，各跑一遍，全堵在它后面
    （表现就是「内容加载很慢，感觉卡住了」）。
    force=True 只在「按 sid 找不到会话」时用一次：刚开的会话可能还没进缓存。"""
    now = time.time()
    if not force and _ext_cache["data"] and now - _ext_cache["at"] < 4.0:
        return _ext_cache["data"]
    if not force and now - _tick_cache["at"] < 0.3:
        return _tick_cache["data"]
    d = _discover(force)
    _tick_cache["at"] = now
    _tick_cache["data"] = d
    return d


def _cached_sessions():
    """SSE 循环专用：只读缓存，永不触发子进程（0 阻塞）。
    数据由后台 _push_sessions_loop 每 2s 在 executor 里刷新。"""
    return _ext_cache["data"]


async def _find_session(sid):
    """按 sid 找会话。绝大多数请求直接吃后台 2s 刷新的热缓存（0 开销）；缓存里
    没有才在 executor 里真跑一次发现（刚打开/刚召回的会话），仍然没有再强制刷一次。
    以前一上来就走发现，缓存刚过期就得同步等 1.2s——进会话的请求全卡在这。
    pending 占位 sid（win-…）在真会话出现后按别名转到真 sid。"""
    sid = _alias_sid(sid)
    # ACP 协议源窗口：无发现协议，窗口表就是真相（gemini 等）
    aw = agent_acp.window(sid)
    if aw is not None:
        return {"sid": aw.sid, "kind": "computer", "agent": "gemini", "acp": 1,
                "cwd": aw.cwd, "title": aw.title, "state": aw.state,
                "pid": None, "window_id": None, "tty": ""}
    if agent_acp.is_ghost(sid):
        # 重启后的鬼影：进程没了，hist 还在仓里（列表/发消息/读历史都要认得）
        return {"sid": sid, "kind": "computer", "agent": "gemini", "acp": 1,
                "cwd": "", "title": "Gemini", "state": "exited",
                "pid": None, "window_id": None, "tty": ""}
    for x in _cached_sessions():
        if x["sid"] == sid:
            return x
    loop = asyncio.get_event_loop()
    listed = []
    for f in (False, True):
        listed = await loop.run_in_executor(None, _tick_sessions, f)
        got = next((x for x in listed if x["sid"] == sid), None)
        if got:
            return got
    # 已退出会话不在 discover 里（列表端点单独补 closed），但 app 点开时
    # ext_msg / SSE 启动都按 sid 走这里——找不到就答 gone，页面直接打不开。
    live = {x["sid"] for x in listed}
    hit = await loop.run_in_executor(
        None, lambda: next((x for x in _closed_sessions(live) if x["sid"] == sid), None))
    if hit:
        return hit
    # 手机点进来却 404/gone 时只能靠这行：是不在这个 agent 的发现结果里，还是整台机器没会话
    _dbg("[ext] 找不到会话 %s（发现 %d 条）" % (sid[:8], len(listed)))
    return None


# ---- 持久化（SQLite，替代零散 JSON 小文件）：会话登记 + 模型切换状态 ----
DB_PATH = HERE / "codeboss.db"
_NH_CAUSE_OK = [False]      # notify_hist.cause 只在第一次连库时补列（_db() 每次请求都调）
_NH_STATE_OK = [False]      # notify_hist.state：'' 未处理 / sent 已确认等结果 / done 已完成
_NH_ACT_OK = [False]        # notify_hist.act：这一轮要用户拍板/执行的那件事（模型第二行输出）
_NH_RID_OK = [False]        # notify_hist.rid：权限请求号（host 生成），回传/审计对账用
_NH_TRACE_OK = [False]      # notify_hist.trace：这一轮「怎么判的」（规则/模型/丢掉的问句）
_REG_AGENT_OK = [False]     # ext_registry.agent：这个会话是哪个 agent 的（claude/codex）


_DB_READY = [False]
_DB_LOCK = threading.Lock()


def _db():
    """每个请求一条连接（sqlite 连接不能跨线程/跨 await 用）。

    建表和补列只在进程内跑一次（_DB_READY）：以前每次请求都执行 6 个 CREATE TABLE
    + 2 个 CREATE INDEX + 一堆 ALTER 试探，属于纯浪费。"""
    import sqlite3
    con = sqlite3.connect(str(DB_PATH), timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    if not _DB_READY[0]:
        with _DB_LOCK:
            if not _DB_READY[0]:
                _db_migrate(con)
                _DB_READY[0] = True
    return con


def _db_migrate(con):
        con.execute("CREATE TABLE IF NOT EXISTS ext_registry("
                    "sid TEXT PRIMARY KEY, cwd TEXT, title TEXT, last_seen REAL)")
        # 用户改过的会话名：手机上改名 → 手机列表和电脑终端 tab 都显示这个名字。
        # ext_registry.title 每轮发现都会被自动起名覆盖，不能放这里。
        con.execute("CREATE TABLE IF NOT EXISTS sess_title("
                    "sid TEXT PRIMARY KEY, title TEXT, at REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS model_state("
                    "sid TEXT PRIMARY KEY, provider_id TEXT, provider_name TEXT, base_url TEXT,"
                    " model TEXT, tier TEXT, via TEXT, at REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS cmd_hist("
                    "sid TEXT, cmd TEXT, ts REAL, cnt INTEGER DEFAULT 1,"
                    " PRIMARY KEY(sid,cmd))")
        # 测试红绿卡（2026-11-19 计划 A1）：卡片重跑只跑验证类命令，按退出码落红绿。
        con.execute("CREATE TABLE IF NOT EXISTS test_runs("
                    "n INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, cmd TEXT,"
                    " state TEXT, rc INTEGER, ts REAL, dur REAL, tail TEXT, src TEXT)")
        # 老库补列（src='agent' = agent 自己跑过的，自动落卡；空 = 人点出来的）
        try:
            con.execute("ALTER TABLE test_runs ADD COLUMN src TEXT")
        except Exception:
            pass
        # 交付链（2026-11-19 计划 A4）：发布/回滚一条一条记账，健康卡据此说「上一个成功发布点」。
        con.execute("CREATE TABLE IF NOT EXISTS deploys("
                    "n INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, kind TEXT,"
                    " cmd TEXT, state TEXT, rc INTEGER, ts REAL, dur REAL, tail TEXT)")
        # 提醒历史：智能识别出来的进度/概括留痕，手机「提醒历史」页按天+会话看。
        # n 自增（提醒 id 每次重启从 1 重来，不能当主键）。
        con.execute("CREATE TABLE IF NOT EXISTS notify_hist("
                    "n INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts REAL, sid TEXT, title TEXT, kind TEXT, text TEXT,"
                    " summary TEXT, about TEXT, cause TEXT, act TEXT, cwd TEXT, src TEXT,"
                    " rep INTEGER DEFAULT 0, state TEXT DEFAULT '', rid TEXT DEFAULT '')")
        # 派单（B2）：手机指定 agent+目录+内容 → 电脑上开新会话干活。台账以任务为主体，
        # sid 只是它的执行面（dispatch 成功前还没有 sid）。
        con.execute("CREATE TABLE IF NOT EXISTS tasks("
                    "tid TEXT PRIMARY KEY, title TEXT, agent TEXT, cwd TEXT, sid TEXT,"
                    " body TEXT, state TEXT, err TEXT, created REAL, updated REAL)")
        if not _NH_CAUSE_OK[0]:
            try:   # 老库补列：这一轮是「为什么做的」——用户当轮的原话摘要（前因）
                con.execute("ALTER TABLE notify_hist ADD COLUMN cause TEXT")
            except Exception as e:
                # 「已存在」= 迁移完成；锁库之类下次连库再试
                _NH_CAUSE_OK[0] = "duplicate column" in str(e).lower()
            else:
                _NH_CAUSE_OK[0] = True
        if not _NH_STATE_OK[0]:
            try:   # 老库补列：提醒历史里这条处理的进度（未处理 / 已确认等结果 / 已完成）
                con.execute("ALTER TABLE notify_hist ADD COLUMN state TEXT DEFAULT ''")
            except Exception as e:
                _NH_STATE_OK[0] = "duplicate column" in str(e).lower()
            else:
                _NH_STATE_OK[0] = True
        if not _NH_ACT_OK[0]:
            try:   # 老库补列：这一轮等用户拍板的具体那件事（「要不要发 0.7.38？」）
                con.execute("ALTER TABLE notify_hist ADD COLUMN act TEXT")
            except Exception as e:
                _NH_ACT_OK[0] = "duplicate column" in str(e).lower()
            else:
                _NH_ACT_OK[0] = True
        if not _NH_RID_OK[0]:
            try:   # 老库补列：权限请求号。Claude 的钩子不带 id，由 host 生成后一路带到手机
                con.execute("ALTER TABLE notify_hist ADD COLUMN rid TEXT DEFAULT ''")
            except Exception as e:
                _NH_RID_OK[0] = "duplicate column" in str(e).lower()
            else:
                _NH_RID_OK[0] = True
        if not _NH_TRACE_OK[0]:
            try:   # 老库补列：这一轮判定的来路（提醒健康页据此算「模型判了几次、丢了几条编的问句」）
                con.execute("ALTER TABLE notify_hist ADD COLUMN trace TEXT DEFAULT ''")
            except Exception as e:
                _NH_TRACE_OK[0] = "duplicate column" in str(e).lower()
            else:
                _NH_TRACE_OK[0] = True
        if not _REG_AGENT_OK[0]:
            try:   # 老库补列：这个会话是哪个 agent 的（claude/codex）。空 = 老的 Claude 会话
                con.execute("ALTER TABLE ext_registry ADD COLUMN agent TEXT")
            except Exception as e:
                _REG_AGENT_OK[0] = "duplicate column" in str(e).lower()
            else:
                _REG_AGENT_OK[0] = True
        con.execute("CREATE INDEX IF NOT EXISTS notify_hist_ts ON notify_hist(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS notify_hist_sid ON notify_hist(sid, ts)")
        # 使用时长：host 侧计数（直连 + 中继回放都算）。待上报的计费窗口 + 中继下发的额度缓存。
        # use_pending.m：新版本是窗口起点（epoch 秒）；老库里是分钟序号，_use_load 按量级迁移。
        con.execute("CREATE TABLE IF NOT EXISTS use_pending(m INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE IF NOT EXISTS use_state(k TEXT PRIMARY KEY, v TEXT)")
        con.commit()


# ---- 使用时长：host 侧计数 / 补报 / 离线额度 ----
# 口径：某个计费窗口（默认 3 秒，刻度由中继下发）里有 ≥1 个非后台请求
# （局域网直连 dt、P2P DataChannel、中继回放 t 都算）。账目单位是秒。
# 中继用 (uid, 窗口起点) 幂等合并；host 断链时先攒本地，恢复后补报。额度用中继下发的
# use_ack 缓存，断链期间直连/P2P 由 host 自己挡。
USE_BG_PATH = "/api/bg_push"
# 纯探针接口：页面/壳在后台定时问「版本变了吗」「有没有新包」。它们不产生任何用户操作，
# 却会被老页面（还没自更新）每分钟打一次——手机息屏后时长就是这么被一直计着的。
# 不论带不带 a=0，一律不算「在用」。
USE_PROBE_PATHS = ("/api/page_ver", "/api/version", "/api/apk_ver", "/api/ping")
# 长连接（推流/实时画面/消息流）：是「通道」不是「使用」。页面开着不动也会每 50 秒
# 主动重连一次（ext.html pushStart 的 50s 自续，防运营商掐长连接），当普通请求
# 记账的话，一台摆在桌上没碰过的手机一上午就能烧掉六小时额度——实测有账号一整天
# 记了 600+ 分钟，用户根本没碰手机。这里一律不记分钟：真正在用的动作（发消息/
# 按键/建会话/读文件）各自会记账。
# 「原样」视图 1 秒一次的终端帧（ext_snap）和实时画面看的是同一个终端、同一件事：
# 视觉上都是「盯着输出看」，不该因为一个是长连接、一个是短请求就一个免费一个收费。
# 动手的动作（发消息/按键/终端输入/开会话）走别的路径，照常记账。relay.py 的
# METER_EXEMPT_PATHS 与 ext.html 的 BILL_SKIP 必须同步加，否则倒计时会和账脱节。
USE_STREAM_PATHS = ("/api/ext_push", "/api/ext_msg/", "/api/ext_live/", "/api/ext_screen/", "/api/ext_cells/",
                    "/api/ext_snap/",
                    # 终端页（/s/<sid>）的输出拉取：中继不透传 WS 时退化成 HTTP 长轮询，
                    # 拉的是和 ext_snap 完全同一份「看输出」，不能一个免费一个收费。
                    "/api/ext_term_out/",
                    # 本机偏好（提醒开关、语音样式等）：改配置不是远程操作电脑，不记分钟。
                    "/api/ext_pref",
                    # 标已读（开页/回前台/提醒看完自动发）：属于「看」的一部分，
                    # 以前靠 a=0 豁免，现在 a=0 不再算数，必须进表。
                    "/api/seen")
# 开发机本机的 Claude Code 事件上报（hook.sh → 127.0.0.1）：那是开发机自己在跑，
# 不是「远程使用」。记到账号头上等于替用户的老实开发机收费。
USE_LOCAL_PATHS = ("/api/session_event",)
USE_KEEP_DAYS = 2
# 「这一分钟里手机端做了什么」：路径前缀 → (kind, 名字)。和中继 relay.py 的 ACT_RULES
# 同口径（两边各留一份，host 单独跑也要能记）。kind：act=主动操作 / nav=打开页面 / read=轮询。
USE_ACT_RULES = (
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
    ("/api/ext_model_session", "act", "切会话模型"),
    ("/api/ext_model", "read", "看模型"),
    ("/api/ext_pref", "act", "改设置"),
    ("/api/ext_sessions", "read", "看会话列表"),
    ("/api/ext_agents", "read", "看已装 agent"),
    ("/r/account_info", "read", "看账号客户端"),
    ("/r/account_send_code", "act", "账号验证码"),
    ("/r/account_kick", "act", "踢客户端"),
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
)


def _act_label(path, method="GET"):
    p = str(path or "")
    for pre, kind, name in USE_ACT_RULES:
        if p.startswith(pre):
            return kind, name
    if method in ("GET", "HEAD"):
        if p.startswith("/ext/"):
            return "nav", "打开会话页"
        if p.startswith("/s/") or p.startswith("/ws/"):
            return "nav", "打开终端页"
        if p in ("/", "/d", ""):
            return "nav", "打开设备页" if p == "/d" else "打开控制台"
        return "read", p[:60]
    return "act", (method + " " + p)[:60]


# cap_known：上限是不是「中继确认过的」。没确认过（新装/刚绑定/换账号）时不能用它拦人——
# 2026-11 实测：开发机绑定前自己记的分钟留在 use_state 里，默认上限 60 一顶就满，
# P2P/直连全部 403「今天的远程使用时长已用完」，而中继账上才 5/60。
# cap=0 表示不限（套餐配 0 = 不限），与中继口径一致。
# 单位：2026-11 起账目一律按**秒**存，刻度窗口 use_unit 秒（中继后台可配，默认 3）。
# 「一个窗口里有 >=1 个计费请求，记 unit 秒」——和中继 relay.use_take 完全同口径。
# ver=2 是「按秒」这一代的本地状态版本；老状态（ver 缺失）读进来时按分钟 ×60 迁移。
_USE_UNIT_DEFAULT = 3
# 中继失联（没权威值/太久没对账）时的兜底：免费档常量。
# 以前是 fail-open（不拦），断网/改路由就能白嫖 Pro 的发送条数和功能开关（复查确认的绕过）。
_FREE_SENDS_CAP = 15
_FREE_FEATURES = {"terminal": False, "remind": False, "auto": False}
_USE_ST = {"used": 0, "cap": 0, "cap_known": False, "date": "", "synced": 0.0,
           # 套餐功能开关（中继 /api/plan 带回的 features 快照）：提醒的收费墙用它。
           # feat_ts 是这份快照的时间；太久没对上账就不用它拦人。
           "feat": {}, "feat_ts": 0.0,
           # use_unit：中继下发的计费刻度（秒）；没拿到过就按默认 3 秒。
           "unit": _USE_UNIT_DEFAULT,
           # ever：这台设备是否曾经成功跟中继对上过账（落库）。用来区分「绑定前自己
           # 记的来路不明的窗口」和「绑好之后断链攒下的窗口」——后者跨天、重启都要留着补报。
           "ever": False,
           # 免费版每天的消息条数：sends=中继确认的今日已发；sends_cap=每日上限（0=不限）；
           # sends_local=本机直连/P2P 自己发出去的（中继那边要等补报，先本地算着）。
           "sends": 0, "sends_cap": 0, "sends_local": 0,
           # sends_cap_known：中继确认过每日条数上限没有（0 = 不限，只在中继确认后成立）。
           "sends_cap_known": False,
           "loaded": False}
_USE_PEND = set()
_USE_P2P = set()      # 有 P2P/LAN 直连请求的分钟（报给中继算直连命中率）
_USE_P2P_BYTES = {}   # 分钟 -> 直连请求的字节数（in+out），报给中继按流量算命中率


def _p2p_note_lan(request, n):
    """局域网直连（非 P2P）的流式响应字节。P2P 的字节在 _dc_serve 按帧算，别重复。"""
    try:
        if request.query.get("dc") == "1":
            return
        if _peer_is_local(request):
            return
        cid = (request.query.get("c") or request.headers.get("X-CB-Cid") or "")[:64]
        _p2p_bytes_add(n, cid)
    except Exception:
        pass


def _p2p_bytes_add(n, cid=""):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    m = int(time.time()) // 60
    k = (m, str(cid or "")[:64])
    _USE_P2P_BYTES[k] = _USE_P2P_BYTES.get(k, 0) + n
    cut = m - USE_KEEP_DAYS * 24 * 60
    if len(_USE_P2P_BYTES) > 400:
        for old in [x for x in _USE_P2P_BYTES if x[0] < cut]:
            _USE_P2P_BYTES.pop(old, None)
# 动作计数：(分钟, kind, 动作, 路径, UA) → 次数。只用于查账，随 use 帧一起补报给中继。
# 中继按 MAX 合并（开发机每 5 秒会重发未确认的部分，MAX 保证重发不加倍）。
_USE_ACT = {}
_USE_DIRTY = False
_USE_LAST_PUSH = 0.0
_LINK = {"ws": None, "lock": None}
_LOGS_ON = {"on": False}   # 中继后台打开了「日志上报」才推 server.log 尾部


def _dbg(msg, **_kw):
    """排查用的细日志（发现/召回/开窗口/慢请求…）：后台给这台机打开了「日志上报」才写。
    平时客户机的 server.log 只留真正的故障和操作记录，不替我们攒诊断数据；
    打开后从那一刻起记，关掉就停。（和 print 同签名：`_f = print if err else _dbg`）"""
    if _LOGS_ON.get("on"):
        print(msg, flush=True)


def _use_day(m=None):
    """日界与中继一致（CST）：不依赖 Mac 本机时区。m 是「分钟序号」（动作记录用）。"""
    m = int(time.time() // 60) if m is None else int(m)
    return time.strftime("%Y-%m-%d", time.gmtime(m * 60 + 8 * 3600))


def _use_day_ts(ts):
    """同一个日界，按 epoch 秒算（计费窗口现在按秒存）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + 8 * 3600))


def _use_unit():
    """当前计费刻度（秒）。中继下发的为准；拿到之前按默认 3 秒。"""
    try:
        u = int(_USE_ST.get("unit") or _USE_UNIT_DEFAULT)
    except (TypeError, ValueError):
        u = _USE_UNIT_DEFAULT
    return max(1, min(60, u))


def _use_bucket(ts=None):
    """ts 落在哪个计费窗口：窗口起点（epoch 秒），也是补报的幂等键。"""
    u = _use_unit()
    return int(int(time.time() if ts is None else ts) // u) * u


def _use_load():
    if _USE_ST["loaded"]:
        return
    _USE_ST["loaded"] = True
    try:
        con = _db()
        try:
            ver = 0
            for k, v in con.execute("SELECT k,v FROM use_state"):
                try:
                    if k == "used":
                        _USE_ST["used"] = int(float(v or 0))
                    elif k == "cap":
                        _USE_ST["cap"] = int(float(v or 0))
                    elif k == "date":
                        _USE_ST["date"] = v or ""
                    elif k == "synced":
                        _USE_ST["synced"] = float(v or 0)
                    elif k == "unit":
                        _USE_ST["unit"] = max(1, min(60, int(float(v or _USE_UNIT_DEFAULT))))
                    elif k == "ver":
                        ver = int(float(v or 0))
                    elif k == "feat":
                        try:
                            ft = json.loads(v or "{}")
                            if isinstance(ft, dict):
                                _USE_ST["feat"] = {str(a): bool(b) for a, b in ft.items()}
                        except ValueError:
                            pass
                    elif k == "feat_ts":
                        _USE_ST["feat_ts"] = float(v or 0)
                    elif k == "ever":
                        _USE_ST["ever"] = str(v or "").lower() in ("1", "true")
                    elif k == "sends":
                        _USE_ST["sends"] = int(float(v or 0))
                    elif k == "sends_cap":
                        _USE_ST["sends_cap"] = int(float(v or 0))
                    elif k == "sends_local":
                        _USE_ST["sends_local"] = int(float(v or 0))
                    elif k == "sends_cap_known":
                        _USE_ST["sends_cap_known"] = str(v or "").lower() in ("1", "true")
                except (TypeError, ValueError):
                    pass
            for (m,) in con.execute("SELECT m FROM use_pending"):
                try:
                    _USE_PEND.add(int(m))
                except (TypeError, ValueError):
                    pass
        finally:
            con.close()
        # 老状态的单位是分钟（ver 缺失）：已用/上限、待报窗口统一换成秒。
        # 不迁移的话，重启后的那几分钟里 60 分钟的上限会被当成 60 秒用——
        # 本地额度墙会把直连/P2P 直接 403 掉（2026-11 踩过一次同类坑）。
        if ver < 2:
            if _USE_ST.get("used") or _USE_ST.get("cap"):
                print("[use] 本地状态升级到秒：used=%s cap=%s → ×60"
                      % (_USE_ST.get("used"), _USE_ST.get("cap")), flush=True)
            _USE_ST["used"] = int(_USE_ST.get("used") or 0) * 60
            _USE_ST["cap"] = int(_USE_ST.get("cap") or 0) * 60
            # 老 pending 是「分钟序号」（现在 epoch 秒 ~1.8e9，分钟序号 ~3e7），按量级区分
            _conv = set(x * 60 if x < 1000000000 else x for x in _USE_PEND)
            _USE_PEND.clear()
            _USE_PEND.update(_conv)
            _use_dirty()
        # 来路不明的账不认：从来没跟中继对上过（synced=0）＝不知道这台机器算在谁头上
        # （新装/还没绑定/绑定前自己记的）——一律清零重来。
        # 跨天则只清「已用」，**待报窗口保留**：断链/局域网期间攒下的窗口最多留 48 小时，
        # 重启后还要能补报（2026-11-09；以前跨天重启会把整段直连的账一起丢掉）。
        # 只清数不删表：下一轮 _use_save 会把清零后的状态写回去。
        if not _USE_ST.get("synced"):
            if _USE_ST.get("used") or _USE_PEND:
                print("[use] 丢弃未对账的本地时长：used=%s date=%r pending=%d"
                      % (_USE_ST.get("used"), _USE_ST.get("date"), len(_USE_PEND)),
                      flush=True)
            _USE_ST["used"] = 0
            _USE_ST["date"] = ""
            _USE_ST["cap_known"] = False
            _USE_ST["sends"] = 0
            _USE_ST["sends_local"] = 0
            _USE_PEND.clear()
            _USE_ACT.clear()
            _use_dirty()
        elif (_USE_ST.get("date") or "") != _use_day():
            if _USE_ST.get("used"):
                print("[use] 跨天：已用清零，待报窗口保留 %d 个"
                      % len(_USE_PEND), flush=True)
            _USE_ST["used"] = 0
            _USE_ST["date"] = ""
            _USE_ST["sends"] = 0
            _USE_ST["sends_local"] = 0
            _use_dirty()
    except Exception as e:
        print("[use] 读取本地时长状态失败: %s" % e, flush=True)


def _use_dirty():
    global _USE_DIRTY
    _USE_DIRTY = True


def _use_today():
    """当前计费日。刚跟中继同步过就取「中继给的那天」和「本机算的那天」里较晚的一天：
    本机时钟偏慢时不会把新一天的额度算到昨天，偏快时也不会拿旧缓存挡住今天。"""
    local = _use_day()
    d = _USE_ST.get("date") or ""
    if not d or time.time() - float(_USE_ST.get("synced") or 0) > 900:
        return local
    return d if d > local else local


def _use_roll_day():
    _use_load()
    today = _use_today()
    if _USE_ST["date"] != today:
        _USE_ST["date"] = today
        _USE_ST["used"] = 0
        _USE_ST["sends"] = 0
        _USE_ST["sends_local"] = 0
        _USE_ST["synced"] = 0.0
        _use_dirty()


def _use_pending_today():
    """今天还没补报上去的秒数 = 今天攒下的窗口数 × 每格秒数。"""
    d = _USE_ST["date"] or _use_day()
    n = sum(1 for m in _USE_PEND if _use_day_ts(m) == d)
    return n * _use_unit()


def _use_used_eff():
    return int(_USE_ST.get("used") or 0) + _use_pending_today()


_FEAT_TTL = 7 * 86400


def _feat_on(name, default=True):
    """套餐功能开关（中继 /api/plan 带回的 features 快照）。
    自建/裸机全开（收费墙只挂官方中继，口径 2026-11-26）；官方快照缺失或一周
    没更新过就按免费档：别让一次断网把付费用户的提醒锁死——宁可免费用户多收几条，
    也别让付了钱的人突然没提醒。"""
    if _relay_selfhost_fast():
        return True
    _use_load()
    ft = _USE_ST.get("feat") or {}
    ts = float(_USE_ST.get("feat_ts") or 0)
    if not ft or not ts or time.time() - ts > _FEAT_TTL:
        # 没有可信快照：按免费档（全关）算。以前返回 default=True 是 fail-open ——
        # 断网就能继续用收费功能（复查确认的绕过）。快照落库，付费用户断网一周内照常。
        return bool(_FREE_FEATURES.get(name, False))
    return ft.get(name, default) is not False


def _feat_key():
    import hashlib as _hl
    import hmac as _hmac
    sec = (_relay_cfg().get("device_secret") or "").encode()
    return _hmac.new(sec, b"cb-feat-v1", _hl.sha256).digest()


def _feat_ack(d):
    """验签并缓存中继下发的功能快照；验不过/过期就不存（按 free 算）。"""
    ft, exp, sig = d.get("feat"), d.get("feat_exp"), d.get("feat_sig")
    if not isinstance(ft, dict) or not sig or not exp:
        return
    try:
        import hashlib as _hl
        import hmac as _hmac
        body = {"feat": {str(k): bool(v) for k, v in ft.items()},
                "exp": int(exp), "plan": str(d.get("feat_plan") or "")}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        want = _hmac.new(_feat_key(), raw.encode(), _hl.sha256).hexdigest()
        if not _hmac.compare_digest(str(sig), want):
            print("[use] 功能快照验签失败，按 free 处理", flush=True)
            return
        if int(exp) < time.time():
            return
        _USE_ST["feat"] = body["feat"]
        _USE_ST["feat_ts"] = time.time()
    except Exception:
        return


# 收费终端（识别命令/命令终端）在 host 侧也要挡：P2P/DC 不经过中继。
_TERM_WALL = ("/api/ext_cmdrun/", "/api/ext_term_in/", "/api/ext_scan/",
              "/api/term_new", "/api/term_close", "/api/term_mac",
              "/s/", "/new", "/api/ext_sess_fork")


def _feat_blocked(path):
    p = str(path or "")
    return any(p.startswith(x) for x in _TERM_WALL) and not _feat_on("terminal")


# 时长墙已整体摘除（2026-11-26 拍板：收费墙只按条数）。原 _use_over/_use_403
# （token/dt/P2P 三处 403「今天的远程使用时长已用完」）退役；_use_touch/
# _use_send_count 记账保留，/api/plan 的 use_cap_sec 只是展示。


def _feat_403():
    return web.HTTPForbidden(
        text=json.dumps({"ok": False, "feature": "terminal",
                         "error": "识别命令是 Pro / Max 套餐的功能，开通后使用"}),
        content_type="application/json")


def _relay_selfhost_fast():
    """这块 host 挂的是不是自建中继（0 网络成本，只读现成缓存）。

    没配 relay = 开源裸机，自立门户（免费无墙）；caps 缓存里 selfhost=1 = 自建
    中继（免费无墙，2026-11-26 口径：收费墙只挂官方中继）。其余（官方/老版本
    中继/缓存还没热）一律按官方——收费墙照旧，不因断网放宽。caps 的常规刷新
    由 brand/页面路径驱动（_relay_caps），这里不发网络请求。"""
    base = str((_relay_cfg() or {}).get("url") or "").strip()
    if not base:
        return True
    val = _CAPS_CACHE.get("val")
    return bool(isinstance(val, dict) and val.get("selfhost"))


def _send_cap_eff():
    """当前生效的每日条数上限（收费墙只按次数，只挂官方中继）。

    自建/裸机 = 0（不限）；官方没拿到/对账过期回落免费档（fail-closed 复查
    加固保留：防断链逃逸官方条数墙）。"""
    if _relay_selfhost_fast():
        return 0
    if (not _USE_ST.get("sends_cap_known")
            or time.time() - float(_USE_ST.get("synced") or 0) > 900):
        return _FREE_SENDS_CAP
    return int(_USE_ST.get("sends_cap") or 0)


def _send_over():
    """官方中继下发的每日条数用完了没有（sends_day，0 = 不限）。

    中继看不见 P2P/局域网流量，直连发送只能由开发机自己挡；条数以中继 ack/plan
    下发的为准（它把隧道和补报的直连都算在一起）。自建/裸机一律无墙（口径
    2026-11-26）；官方没拿到/太久没对账按免费档拦（fail-closed 复查加固保留）。"""
    if _relay_selfhost_fast():
        return False
    _use_load()
    _use_roll_day()
    cap = int(_USE_ST.get("sends_cap") or 0)
    if not _USE_ST.get("sends_cap_known") or time.time() - float(_USE_ST.get("synced") or 0) > 900:
        # 官方：没有权威值/太久没对账：按免费档拦（fail-closed），顺手催一次对账。
        _use_reconcile()
        cap = _FREE_SENDS_CAP
    elif cap <= 0:
        return False
    used = max(int(_USE_ST.get("sends") or 0), int(_USE_ST.get("sends_local") or 0))
    if used < cap:
        return False
    print("[use] 消息条数用完 used=%d cap=%d" % (used, cap), flush=True)
    return True


# 和 relay 的 SEND_PATHS/SEND_ACTS 一致：免费版「每天 N 条」计的是发送类动作。
# 读（看输出/拉列表/轮询/看画面）不计——「看不计时、写计时」。
_SEND_PATHS = ("/api/ext_send", "/api/ext_key", "/api/ext_term_in/",
               "/api/ext_cmdrun/", "/api/ext_write", "/api/ext_sess_new")


def _is_send_req(path):
    p = str(path or "")
    return any(p.startswith(x) for x in _SEND_PATHS)


def _use_send_count(request):
    """直连（局域网 / P2P）发出去的消息，本机先记一条。

    中继转发的请求来自 127.0.0.1、没有 dc=1 标记——那些中继自己已经记过，
    这里跳过，否则同一条消息会被算两次。"""
    try:
        if (request.method or "").upper() != "POST":
            return
        if not _is_send_req(request.path):
            return
        if not (request.query.get("dc") == "1" or not _peer_is_local(request)):
            return
        _use_load()
        _use_roll_day()
        _USE_ST["sends_local"] = int(_USE_ST.get("sends_local") or 0) + 1
        _use_dirty()
    except Exception:
        pass


# 最近一次额度判定的结论（放行/拦下 + 当时看到的数）。出问题时不用翻日志：
# /api/host_stat 里能直接看到「这台开发机当时拿到的账是什么」。
_USE_DEC = {"when": 0.0, "verdict": "-", "used": 0, "cap": 0, "relay": 0,
            "pending": 0, "why": ""}


def _use_note(verdict, used=0, cap=0, path=None, relay=None, why=""):
    try:
        _USE_DEC.update({"when": time.time(), "verdict": verdict,
                         "used": int(used or 0), "cap": int(cap or 0),
                         "relay": int(relay if relay is not None
                                      else (_USE_ST.get("used") or 0)),
                         "pending": _use_pending_today(), "why": str(why or "")[:80]})
    except Exception:
        pass


def _use_snapshot():
    """额度状态的完整快照：给 /api/host_stat 用（排查「明明没到额度却说用完」）。"""
    try:
        _use_load()
        synced = float(_USE_ST.get("synced") or 0)
        return {"used": int(_USE_ST.get("used") or 0),
                "cap": int(_USE_ST.get("cap") or 0),
                "unit": _use_unit(),          # 计费刻度（秒）：used/cap/pending 都是秒
                "cap_known": bool(_USE_ST.get("cap_known")),
                "day": _USE_ST.get("date") or "",
                "synced_age": int(max(0, time.time() - synced)) if synced else -1,
                "plan_cap": _USE_ST.get("plan_cap"),
                "credit": _USE_ST.get("credit"),
                "pending": _use_pending_today(),
                "local_used": _use_used_eff(),
                "dec": dict(_USE_DEC)}
    except Exception as e:
        return {"error": str(e)[:80]}


_RECON_AT = 0.0


def _use_reconcile():
    """账过期了就催一次：让页面/本机去问中继要权威的已用/上限（h_plan → _use_cache_cap）。"""
    global _RECON_AT
    now = time.time()
    if now - _RECON_AT < 30:
        return
    _RECON_AT = now
    print("[use] 本地额度账过期，放行并等待对账", flush=True)
    try:
        asyncio.get_running_loop().create_task(_use_plan_pull())
    except Exception:
        pass


async def _use_plan_pull():
    """主动向中继要一次权威额度（不依赖页面轮询）。失败就算了，下次再试。"""
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/plan",
                             headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict):
            _use_cache_cap(d)
    except Exception:
        pass


def _peer_is_local(request):
    """这个请求是不是这台开发机自己发出来的（127.0.0.1 / ::1）。

    Mac 菜单栏客户端开着主窗口时每 5 秒轮询一次本机 /api/ext_model，Claude Code 的
    hook 也往 127.0.0.1 上报——那是这台电脑自己在跑，不是「远程使用」。真按分钟记，
    桌面上一台忘了关窗口的 Mac 就能替用户把一整天额度烧光（实测正好 60 分钟/小时）。
    手机走局域网 IP 或经中继转发的公网 IP，不受影响；中继自己计费那条路也不受影响。"""
    try:
        peer = (request.remote or "")
    except Exception:
        return False
    return peer == "::1" or peer.startswith("127.")


def _use_skip(request):
    """这条请求不记分钟：后台提醒 / 静态资源 / 纯探针 / 长连接 / 本机 hook / 本机发来的请求。
    `_use_touch` 与 token 分支的时长墙共用这一个口径——两边分开写就会「账上记了却拦、
    或者拦了却不记」。"""
    if request is None:
        return False
    dc = request.query.get("dc") == "1"   # P2P 直连（host 自己转发进 127.0.0.1，不算本机）
    return (request.path == USE_BG_PATH
            or request.path.startswith("/static/")
            or request.path in USE_PROBE_PATHS
            or request.path.startswith(USE_STREAM_PATHS)
            or request.path in USE_LOCAL_PATHS
            or (not dc and _peer_is_local(request)))


def _use_touch(request=None, path=None, method=None, cid=None):
    """记一格（一个计费窗口，默认 3 秒）。后台提醒通道不算（黑屏时它一直在，但不代表有人在用）；
    静态资源也不算（一次页面加载会顺带拉一堆，页面导航本身已经记过了）。

    path/method：P2P（DataChannel）来的请求没有 aiohttp request 对象，由调用方传路径，
    这样直连的动作也能进「动作记录」——不然 LAN/P2P 用掉的分钟在后台只有数字没有解释。"""
    dc = bool(request is not None and request.query.get("dc") == "1")   # P2P 直连
    # 直连活跃分钟要在「排除长连接/静态资源」之前记：手机上推送/实时画面（ext_live、
    # ext_push）正是直连的主要流量，它们本来就不记使用时长，但必须算进「直连命中率」。
    # 只有真正从这台电脑发出去的请求（127.0.0.1 且没有 dc=1）不算。
    try:
        if request is not None and (dc or not _peer_is_local(request)):
            _m2 = int(time.time()) // 60
            _USE_P2P.add(_m2)
            _cut2 = _m2 - USE_KEEP_DAYS * 24 * 60
            for _old in [x for x in _USE_P2P if x < _cut2]:
                _USE_P2P.discard(_old)
    except Exception:
        pass
    if _use_skip(request):
        return   # 后台提醒 / 静态资源 / 纯探针 / 长连接 / 本机 hook / 中继转发的本机请求：都不代表有人在用
    _use_roll_day()
    now = int(time.time())
    m = now // 60          # 动作记录仍按分钟归（中继 use_act_log 的键是分钟）
    bs = _use_bucket(now)  # 计费窗口：起点当幂等键，重复请求同一格只记一次
    if path is None and request is not None:
        path = request.path
    if method is None and request is not None:
        method = request.method
    # 「看不计时、写计时」（2026-11-09）：GET/HEAD/OPTIONS 是看（消息、会话列表、
    # 终端输出、设置页、页面导航），一律不记格；只有 POST/PUT/DELETE 这类写操作才计。
    # 和中继 tunnel()、页面 BILL_SKIP 同一口径。
    if str(method or "GET").upper() in ("GET", "HEAD", "OPTIONS"):
        return
    ua = ""
    if request is not None:
        try:
            ua = (request.headers.get("User-Agent") or "")[:80]
        except Exception:
            ua = ""
    if path:
        # 动作记录：这一分钟里手机端做了什么。同一分钟同一动作累加次数。
        # 端子 ID（cid）一起记：直连时中继看不到请求，只有这份补报能把动作算到具体终端上，
        # 否则「使用详情」按终端筛永远为空（2026-11-10）。
        if cid is None and request is not None:
            try:
                cid = (request.query.get("c") or request.headers.get("X-CB-Cid") or "")[:64]
            except Exception:
                cid = ""
        cid = str(cid or "")[:64]
        k, a = _act_label(path, method or "GET")
        key = (m, k, a, str(path)[:80], ua, cid)
        _USE_ACT[key] = _USE_ACT.get(key, 0) + 1
        cutoff = m - USE_KEEP_DAYS * 24 * 60
        for old in [x for x in _USE_ACT if x[0] < cutoff]:
            _USE_ACT.pop(old, None)
    if bs in _USE_PEND:
        return
    _USE_PEND.add(bs)
    # 这一格是被什么请求触发的：只打第一笔，一格一行。半夜没人碰手机却一直在记
    # 时长时，日志里直接能看到是哪条路径/哪台设备（2026-11 查账用）。
    print("[use] 记一格 %ds %s %s ua=%s" % (_use_unit(), str(path or "")[:60],
                                             method or "", ua[:40]), flush=True)
    cutoff = int(time.time()) - USE_KEEP_DAYS * 86400
    for old in [x for x in _USE_PEND if x < cutoff]:
        _USE_PEND.discard(old)
    _use_dirty()


def _use_save(force=False):
    global _USE_DIRTY
    if not (_USE_DIRTY or force):
        return
    _USE_DIRTY = False
    try:
        con = _db()
        try:
            con.execute("DELETE FROM use_pending")
            con.executemany("INSERT OR IGNORE INTO use_pending(m) VALUES(?)",
                            [(m,) for m in _USE_PEND])
            # ver=2：账目按秒（老状态 ver 缺失=按分钟，_use_load 里迁移）
            con.execute("INSERT INTO use_state(k,v) VALUES('ver','2') "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v")
            for k in ("used", "cap", "date", "synced", "feat_ts", "unit", "ever",
                      "sends", "sends_cap", "sends_local", "sends_cap_known"):
                con.execute("INSERT INTO use_state(k,v) VALUES(?,?) "
                            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (k, str(_USE_ST.get(k))))
            con.execute("INSERT INTO use_state(k,v) VALUES('feat',?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (json.dumps(_USE_ST.get("feat") or {}, ensure_ascii=False),))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print("[use] 保存失败: %s" % e, flush=True)


def _use_ack(d):
    """中继回传：今天已用/上限（断链期间 host 靠它挡直连额度），ack 是已入库的窗口。

    单位：新中继带 "unit"/"used_s"（秒）；老中继只有 used/cap（分钟）和 ack（分钟序号）。
    用「有没有 unit」区分，两种都能对账，host 可以先升级、中继后升级。"""
    _use_load()
    try:
        used = int(d.get("used") or 0)
        cap = int(d.get("cap") or 0)
        date = str(d.get("date") or "")
    except (TypeError, ValueError):
        return
    sec_mode = d.get("unit") is not None or d.get("used_s") is not None
    if sec_mode:
        try:
            _USE_ST["unit"] = max(1, min(60, int(d.get("unit") or _USE_UNIT_DEFAULT)))
        except (TypeError, ValueError):
            pass
        if d.get("used_s") is not None:
            try:
                used = int(d.get("used_s") or 0)
            except (TypeError, ValueError):
                pass
        if d.get("cap_s") is not None:
            try:
                cap = int(d.get("cap_s") or 0)
            except (TypeError, ValueError):
                pass
    else:
        used, cap = used * 60, cap * 60      # 老中继：分钟 → 秒
    if date:   # 有日期就以中继的账为准（它是计费方），但不往回倒一天
        day = date if date > _use_day() else _use_day()
        # 只有「这台设备从来没跟中继对上过账」（ever=False）才清本地窗口：那些是
        # 还没绑定账号时自己记的，来路不明，绑定后不能挂到新账号头上。
        # 已经对上过账的（ever=True）跨天、重启都不清——断链/局域网攒下的窗口要能
        # 补报，最多留 48 小时（以前跨天或重启会把整段直连的账丢掉，2026-11-09 修）。
        first = not _USE_ST.get("ever")
        if first and (_USE_PEND or _USE_ACT):
            print("[use] 对账：丢弃未归属的本地窗口 %d 个 / 动作 %d 条"
                  % (len(_USE_PEND), len(_USE_ACT)), flush=True)
            _USE_PEND.clear()
            _USE_ACT.clear()
        _USE_ST["date"] = day
        if day == date:
            _USE_ST["used"] = used
            _USE_ST["cap"] = cap
            _USE_ST["cap_known"] = True      # 中继确认过的上限（0 = 不限）
            _USE_ST["ever"] = True           # 对上过账：以后不再按「来路不明」清窗口
            # 免费版消息条数：中继是计费方（隧道 + 直连补报都在它账上）。
            # 新中继才带 sends；老中继不带就不动，别把本地数清零。
            if d.get("sends") is not None:
                try:
                    _USE_ST["sends"] = int(d.get("sends") or 0)
                    _USE_ST["sends_cap"] = int(d.get("sends_cap") or 0)
                    _USE_ST["sends_cap_known"] = True
                except (TypeError, ValueError):
                    pass
        else:
            # 本机时钟和中继不在同一天：账对不上，先别拿本地数拦人（条数也同理）。
            _USE_ST["cap_known"] = False
            _USE_ST["sends_cap"] = 0
            _USE_ST["sends_cap_known"] = False
    _feat_ack(d)
    _USE_ST["synced"] = time.time()
    for m in (d.get("ack") or []):
        try:
            v = int(m)
            # 新中继：ack 是窗口起点秒，直接按窗口对齐后清。
            # 老中继：ack 是「分钟序号」，它确认的是整整一分钟——把落在这一分钟里的
            # 所有窗口都清掉（只清 v*60 那一格的话，3 秒刻度下每分钟 20 格全留到
            # 300 秒兜底才掉，待报数会虚高）。
            if sec_mode:
                u = _use_unit()
                _USE_PEND.discard(int(v // u) * u)
                _USE_PEND.discard(v)
                mk = v // 60
            else:
                lo, hi = v * 60, v * 60 + 60
                for x in [y for y in _USE_PEND if lo <= y < hi]:
                    _USE_PEND.discard(x)
                mk = v
            # 中继已确认收到这一格（连同随帧补报的动作计数）：本地清掉，不再重发
            for k in [x for x in _USE_ACT if x[0] == mk]:
                _USE_ACT.pop(k, None)
        except (TypeError, ValueError):
            pass
    if "ack" in d:
        # 待报队列要自己收敛：中继刚回过一批 ack 之后，超过补报窗口还留着没被确认
        # 的窗口，要么被中继按时间窗 clamp 掉了、要么当年这台设备还没归属账号被拒——
        # 再攒着也不会变成账。窗口给到和中继一样的 48 小时：断链/局域网用了半天再
        # 重连，账要能补上去（以前这里只留 5 分钟，断链一长整段就没了）。
        cut = int(time.time()) - USE_KEEP_DAYS * 86400
        stale = [x for x in _USE_PEND if x < cut]
        if stale:
            print("[use] 丢弃中继不收的旧窗口 %d 个（最旧 %s）"
                  % (len(stale), _use_day_ts(min(stale))), flush=True)
            for x in stale:
                _USE_PEND.discard(x)
    _use_dirty()
    _use_save()


async def _use_loop():
    """每 5 秒落盘一次；链路在就补报待上传的计费窗口（幂等，重复发也没关系）。

    "units" = 窗口起点（epoch 秒，单位见 _USE_ST["unit"]）；老中继只认 "mins"，
    所以 uplink 版本带 units；中继侧两种都收（relay use_record gran=s/m）。"""
    global _USE_LAST_PUSH
    _p2p_sent = 0
    _p2p_b_sent = 0
    _use_load()
    _use_roll_day()
    while True:
        try:
            _use_save()
            ws, lock = _LINK.get("ws"), _LINK.get("lock")
            _p2p_new = sorted(m for m in _USE_P2P if m > _p2p_sent)
            _p2p_b_new = [[m, c, b] for (m, c), b in sorted(_USE_P2P_BYTES.items())
                          if m > _p2p_b_sent]
            if ws is not None and not ws.closed and (_USE_PEND or _p2p_new or _p2p_b_new) and lock is not None:
                now = time.time()
                if now - _USE_LAST_PUSH >= 5:
                    _USE_LAST_PUSH = now
                    # 断链/局域网期间可能攒下很多窗口（最多 48 小时，10 秒刻度约 1.7 万格）。
                    # 中继单帧最多收 2000 个，按「老的先报」分帧发；每轮最多 4 帧，
                    # 把通道让给别的消息，剩下的下一轮接着报。
                    units = sorted(_USE_PEND)
                    chunks = [units[i:i + 2000] for i in range(0, len(units), 2000)][:4]
                    # 顺带补报动作计数（这一分钟做了什么）：中继看不到直连/LAN 的请求，
                    # 没有这份数据，后台只能看到「已用 N 秒」一个数字。动作按分钟归并、
                    # 中继按 MAX 合并，重复发不会加倍——只在最后一帧带一次。
                    acts = [{"m": m, "k": k, "a": a, "p": p, "n": n, "ua": ua, "c": c}
                            for (m, k, a, p, ua, c), n in
                            sorted(_USE_ACT.items(), key=lambda kv: kv[0][0])[-400:]]
                    if not chunks and (_p2p_new or _p2p_b_new):
                        chunks = [[]]      # 只有直连分钟/字节：也要发一帧
                    for i, ch in enumerate(chunks):
                        # 同时带一份老的分钟口径：新中继只读 units（存在就以它为准），
                        # 老中继只读 mins——先升级 host、后部署中继的那段时间也不会漏账。
                        mins = sorted(set(u // 60 for u in ch))
                        last = (i == len(chunks) - 1)
                        async with lock:
                            await ws.send_json({"t": "use", "units": ch,
                                                "mins": mins,
                                                "p2p": _p2p_new if last else [],
                                                "p2p_b": _p2p_b_new if last else [],
                                                "acts": acts if last else []})
                    if _p2p_new:
                        _p2p_sent = _p2p_new[-1]
                    if _p2p_b_new:
                        _p2p_b_sent = max(x[0] for x in _p2p_b_new)
        except Exception as e:
            print("[use] 补报失败: %s" % str(e)[:120], flush=True)
        await asyncio.sleep(5)


def _reg_update(sessions):
    """把发现到的运行中会话落库：终端杀掉后 agents 列表就没了，靠这张表召回。"""
    now = time.time()
    try:
        con = _db()
        con.executemany(
            "INSERT INTO ext_registry(sid,cwd,title,last_seen,agent) VALUES(?,?,?,?,?)"
            " ON CONFLICT(sid) DO UPDATE SET cwd=excluded.cwd,title=excluded.title,"
            "last_seen=excluded.last_seen,agent=excluded.agent",
            [(s["sid"], s.get("cwd", ""), s.get("title", ""), now,
              s.get("agent") or "claude") for s in sessions])
        con.execute("DELETE FROM ext_registry WHERE last_seen < ?", (now - 30 * 86400,))
        con.commit()
        con.close()
    except Exception:
        pass


def _reg_agent(sid):
    """登记表里记的 agent（老行是空 = Claude）。"""
    try:
        con = _db()
        r = con.execute("SELECT agent FROM ext_registry WHERE sid=?", (sid,)).fetchone()
        con.close()
    except Exception:
        return None
    return (r[0] or "claude") if r else None








def _closed_sessions(live_sids, limit=50):
    """所有 agent 的「已退出但还能召回」的会话，按最近活跃排。

    每个 agent 各自认自己的（Claude 看 ~/.claude/projects，Codex 看 ~/.codex/sessions）。
    合并后按最后活跃时间重排——只有 Claude 的时候跟老行为逐条一样。"""
    out = []
    for ad in agents.adapters():
        try:
            out.extend(ad.closed_sessions(live_sids, limit) or [])
        except Exception as e:
            print("[closed] %s 找已退出会话出错: %s" % (ad.key, str(e)[:120]), flush=True)
    out.sort(key=lambda x: x.get("last") or 0, reverse=True)
    return _apply_titles(out)


def _user_titles():
    """用户在手机上改过的会话名：sid -> 名字。空 = 没改过，走自动起名。"""
    try:
        con = _db()
        rows = con.execute("SELECT sid,title FROM sess_title").fetchall()
        con.close()
    except Exception:
        return {}
    return {r[0]: (r[1] or "").strip() for r in rows if (r[1] or "").strip()}


def _apply_titles(sessions):
    """把用户改过的名字盖到会话列表上。这是名字的最终裁判：
    运行中（discover）、已退出（_closed_sessions）、Windows 托管三路都过这里，
    改名后手机列表、推送标题、提醒文案用的是同一个名字。"""
    if not sessions:
        return sessions
    t = _user_titles()
    if not t:
        return sessions
    for s in sessions:
        sid = s.get("sid") or ""
        u = t.get(sid)
        if not u and (s.get("agent") or "claude") == "claude":
            # 续篇（--resume 出的新 sid）继承原件上用户改的名字：列表合成本来
            # 只留续篇一条，名字不跟着走就等于「改名后会话不见了」（2026-11-21）。
            u = t.get(transcript.chain_head(sid, s.get("cwd") or ""))
        if u:
            s["title"] = u
    return sessions


_TAB_SYNC = {}     # sid -> (meta pid, 上次推的名字)；pid 变了 = wrapper 重生，必须重钉

def _sync_titles(sessions):
    """把统一后的名字钉进全彩镜像会话的终端 tab：wrapper 在输出路上把 agent 写的
    OSC 标题换成这个名字，host 名字一变（ai-title 冒出来、手机改名）就从 ctl
    socket 推过去，跑着的会话 2 秒内跟上。没经 wrapper 的会话（用户自己开的终端、
    Codex 桌面版）没有 ctl 通道，管不到，保持 agent 自己的标题。"""
    for x in sessions:
        if x.get("pending"):
            continue
        sid = x.get("sid") or ""
        ti = (x.get("title") or "").strip()
        if not sid:
            continue
        meta = _meta_for_pid(x.get("pid"))   # 没有 meta/ctl = 不是 wrapper 会话
        if not meta or not meta.get("ctl") or not meta.get("wv"):
            continue   # wv 缺失 = 旧 wrapper：不认控制协议，推过去会当成按键打进会话
        rec = (meta.get("pid"), ti)
        if _TAB_SYNC.get(sid) == rec:
            continue   # 同世代同名才算推过——wrapper 重生后 pin 是空的，名字没变也得重钉
        try:
            ok = _pty_ctl_write(meta, b"\x00T:" + ti.encode("utf-8", "replace") + b"\n")
        except Exception:
            ok = False
        if ok:
            _TAB_SYNC[sid] = rec   # 推送失败不记，下一轮发现再试
        elif sid not in _TAB_SYNC:
            _dbg("[tab] 钉标题失败 sid=%s name=%s" % (sid[:8], ti[:20]))
        # ponytail: 钉进 tab 的名字会被发现的「窗口标题拆段」捡回去当自动名——闭环
        # 收敛恒定（手机/电脑显示一致）；「清空恢复自动起名」会停在清空前的名字，
        # ai-title 出现后仍按仲裁链覆盖。要真还原就得记钉前标题，不值得。


# ---- agent 适配层 ----
# Claude 的适配器：实现全是转调本文件里原有的函数，一行逻辑都没重写。
# 这样「加一个 Codex」只会在别处多一个适配器，不会顺手把 Claude 弄坏。
class _ClaudeAdapter(agents.AgentAdapter):
    key = "claude"
    label = "Claude Code"
    supports_fork = True
    can_resume = True
    can_notify = True
    can_model_switch = True
    # Windows 托管新会话时把 host 生成的 uuid 直接指定给 claude（host 的会话 id
    # == 转录 id）。别的 agent 不认这个旗标 → None 走通用 uuid 分支。
    new_session_id_flag = ["--session-id"]

    def discover(self):
        return _claude_discover()

    def transcript_path(self, cwd, sid):
        return project_jsonl(cwd, sid)

    def parse_transcript(self, path, limit=120):
        return parse_transcript(path, limit=limit)

    def transcript_meta(self, path):
        return _transcript_meta(path)

    def closed_sessions(self, live_sids, limit=50):
        return _claude_closed_sessions(live_sids, limit)

    def delete_transcript(self, sid):
        """删掉 Claude 的转录文件（主转录 + 子代理目录）。返回删掉的文件数。"""
        removed = 0
        base = Path.home() / ".claude" / "projects"
        try:
            for p in base.glob("*/%s.jsonl" % sid):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
            # 子代理转录在 <项目>/<sid>/ 目录里，一起删干净
            for d in base.glob("*/%s" % sid):
                try:
                    if d.is_dir():
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass
        return removed

    def installed(self):
        # CLAUDE_BIN 是 import 时找到的绝对路径（找不到就是裸 "claude"）。
        # 没有 CLI 但 ~/.claude 在（以前装过/桌面端装过）也算：会话列表的切换条据此出现。
        return bool(shutil.which(CLAUDE_BIN) or os.path.isfile(CLAUDE_BIN)
                    or (Path.home() / ".claude").is_dir())

    def prepare_cwd(self, cwd):
        return _claude_pretrust(cwd)

    def launch_argv(self, cwd, sid=None, model=None):
        argv = [plat.claude_cmd()]
        if sid:
            argv += ["--resume", sid]
        model = (model or "").strip()
        # 「默认」是原生 provider 的占位（不是真实模型名），别传给 claude
        if model and model not in ("默认", "default"):
            argv += ["--model", model]
        return argv

    def fork_argv(self, cwd, sid):
        return [plat.claude_cmd(), "--resume", sid, "--fork-session"]

    def hook_spec(self):
        return _HOOK_SPEC

    def hook_install(self, on):
        return _hooks_mutate(on)

    def hooks_installed(self):
        return _hooks_installed()

    def hook_status(self):
        cfg = _load_claude_settings() or {}
        hooks = cfg.get("hooks") or {}
        have = [ev for ev, _ in _HOOK_SPEC
                if any(_is_cb_group(g) for g in (hooks.get(ev) or []) if isinstance(g, dict))]
        return have, str(_CLAUDE_SETTINGS)

    def dialog_hints(self):
        return _DLG_HINTS

    def dialog_prefixes(self):
        return _DLG_HINT

    def agent_keys(self):
        return ("slash_model",)      # /model 是 Claude 独有的命令

    def model_info(self):
        try:
            return (_machine_model() or {}).get("model") or ""
        except Exception:
            return ""


agents.register(_ClaudeAdapter())


def _codex_notify_argv():
    """Codex 的 notify 命令：hook.sh 带上 "codex" 标记，payload 由 Codex 追加在最后
    （notify 是 argv 列表、走 execve，不经 shell，所以中文/空格都不用转义）。"""
    return [str(HOOK_SH), "codex"]


def _codex_ensure_script():
    _write_hook_sh()


# Codex 的适配器在 agent_codex.py：找会话/读 rollout/开进程都在那边，
# 这里只把「跑完提醒」的两个本地件（上报脚本、装回钩子）交给它。
# Windows 不接 notify：Codex 官方在 Windows 上推荐 WSL，原生 notify 是 cmd 解析
# argv，JSON 里的引号会在 shell 层被吃掉——宁可不装，也不能装个时灵时不灵的。
agents.register(agent_codex.Adapter(
    notify_argv=None if IS_WIN else _codex_notify_argv,
    ensure_script=None if IS_WIN else _codex_ensure_script))
# 进程重启后内存缓存没了，登记表里还记着每个会话是哪个 agent 的
agents.set_lookup(_reg_agent)


def _model_get(sid):
    try:
        con = _db()
        r = con.execute("SELECT provider_id,provider_name,base_url,model,tier,via,at"
                        " FROM model_state WHERE sid=?", (sid,)).fetchone()
        con.close()
    except Exception:
        return None
    return dict(zip(("provider_id", "provider_name", "base_url", "model", "tier", "via", "at"), r)) if r else None


def _model_set(sid, rec):
    try:
        con = _db()
        con.execute("INSERT INTO model_state(sid,provider_id,provider_name,base_url,model,tier,via,at)"
                    " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(sid) DO UPDATE SET"
                    " provider_id=excluded.provider_id,provider_name=excluded.provider_name,"
                    " base_url=excluded.base_url,model=excluded.model,tier=excluded.tier,"
                    " via=excluded.via,at=excluded.at",
                    (sid, rec.get("provider_id", ""), rec.get("provider_name", ""), rec.get("base_url", ""),
                     rec.get("model", ""), rec.get("tier", ""), rec.get("via", ""), time.time()))
        con.commit()
        con.close()
    except Exception:
        pass


def _model_clear(sid):
    try:
        con = _db()
        con.execute("DELETE FROM model_state WHERE sid=?", (sid,))
        con.commit()
        con.close()
    except Exception:
        pass


def _model_all():
    try:
        con = _db()
        rows = con.execute("SELECT sid,model FROM model_state").fetchall()
        con.close()
        return dict(rows)
    except Exception:
        return {}


def _claude_discover():
    """找出 Terminal.app 里前台交互运行中的 Claude Code 会话。

    Claude 专属逻辑（`claude agents --json`）；接口见 agents.py。
    多个 agent 的合并在 discover() 里做——这里只管 Claude 自己的。"""
    _, raw, _ = _run([CLAUDE_BIN, "agents", "--json"], timeout=15)
    try:
        alist = json.loads(raw) if raw else []
    except Exception:
        alist = []
    wmap = _window_map()
    tree = _ps_tree()
    st = _ps_stat()          # 僵尸/正在退出的进程别再列成「开着」（关不掉、点了也没反应）
    cmds = None

    def _attach_pid(sid):
        """Claude 新版把 `--resume` 的会话放进后台宿主，前台只挂一个
        `claude attach <sid前8位>` 客户端。agents --json 里这种会话 kind=background、
        pid 是后台宿主，没有 tty 也没有窗口；真正能打字的是 attach 客户端。
        按命令行找到它，拿它当会话 pid（2026-11-11 macmini：唤起后窗口其实起来了，
        host 却报「18 秒没起来」，用户连点三次开了三个窗口）。"""
        nonlocal cmds
        if cmds is None:
            cmds = _ps_cmd()
        short = sid.split("-")[0]
        pat = re.compile(r"\battach\s+(?:%s|%s)\b" % (re.escape(short), re.escape(sid)))
        hit = 0
        for apid, (_pp, atty, acmd) in cmds.items():
            if "attach" not in acmd or not pat.search(acmd):
                continue
            if atty and atty != "??" and wmap.get("/dev/" + atty):
                return apid          # 有窗口的优先（多开时挑用户看得见的那份）
            hit = hit or apid
        return hit

    def _tty_of(pid):
        t = (tree.get(int(pid or 0)) or ("", ""))[1]
        return ("/dev/" + t) if t and t != "??" else ""

    out = []
    for a in alist:
        kind = a.get("kind")
        if kind not in ("interactive", "background"):
            continue
        sid = a.get("sessionId", "")
        if not sid:
            continue
        if kind == "interactive":
            pid = a.get("pid")
        else:
            pid = _attach_pid(sid)
            if not pid:
                continue   # 纯后台、没有 attach 客户端：没有可写入口，列出来也点不动
        _s = st.get(int(pid)) if pid else None
        if _s and _stat_dead(_s):
            continue       # 进程卡在退出（E）/僵尸（Z）：别再当「开着」，按已退出走历史回放

        tty = _tty_of(pid)
        wid, title = wmap.get(tty, (None, ""))
        if wid is None and pid:
            # 经 wrapper 启动的会话：claude 在代理 pty 里，用父进程（wrapper）的 tty 找窗口
            ptty = _tty_of((tree.get(int(pid)) or (0, ""))[0])
            wid2, t2 = wmap.get(ptty, (None, ""))
            if wid2:
                wid, title = wid2, t2
                tty = ptty        # 发送定位必须用 tab（wrapper）的 tty，内层 pty 不属于任何 tab
        parts = [p.strip() for p in title.split("—")]
        if parts and parts[-1].replace("×", "x").replace("X", "x").split("x")[-1].isdigit():
            parts.pop()                      # 丢掉窗口标题里的 "80×24"
        nice = re.sub(r"^[^\w]+", "", parts[1]).strip() if len(parts) >= 2 else ""
        # 标题优先用 Claude 生成的 ai-title（像「排查 IPv6 地址失败」），比窗口名/首句话干净
        tp = project_jsonl(a.get("cwd", ""), sid) if a.get("cwd") else None
        ai = ""
        if tp:
            _transcript_meta(tp)
            hit = _TRANSCRIPT_META.get(str(tp))
            ai = ((hit[2] if hit else "") or "")
        cand = ai or nice or title or ""
        if not cand or cand.strip().lower() in ("claude", "claude code"):
            cand = Path(a.get("cwd", "")).name or sid[:8]   # 兜底用项目名，别露 "Claude Code"
        out.append({
            "sid": sid, "short": sid[:8], "pid": pid, "cwd": a.get("cwd", ""),
            "title": cand, "window_id": wid, "tty": tty,
            "state": a.get("status", ""), "agent": "claude",
        })
    return out


# ---- 新开 agent 会话的「预登记」----
# Claude 一启动就写好转录，列表立刻能看到；Codex 要等第一句话才建 thread。
# 手机开 Codex 会话后如果列表里什么都没有，就是「只看到终端、看不到会话」。
# 这里先按窗口登记一条 pending，真会话出现前用它占位；出现后把 pending 的 sid
# 记成别名，页面已经点进去的连接（按键/画面/发送）继续按真 sid 走。
_PENDING_AG = {}      # "win-<wid>" -> {agent,wid,tty,cwd,at,title,pid}
_PENDING_ALIAS = {}   # "win-<wid>" -> 真 sid


def _pending_key(wid, tty):
    if wid:
        return "win-%s" % wid
    return "tty-%s" % (tty or "").rstrip("/").split("/")[-1]


def _alias_sid(sid):
    """pending 占位 sid -> 真会的话 sid（没有就原样返回）。"""
    return _PENDING_ALIAS.get(sid, sid)


def _merge_pending(out):
    """把还没长出真会话的 pending 窗口并进会话表；长出真会话的转为别名。"""
    if not _PENDING_AG:
        return out
    by_tty = {x.get("tty"): x for x in out if x.get("tty")}
    by_win = {x.get("window_id"): x for x in out if x.get("window_id")}
    now = time.time()
    for key, p in list(_PENDING_AG.items()):
        hit = (p.get("wid") and by_win.get(p["wid"])) or by_tty.get(p.get("tty"))
        if hit:
            _PENDING_ALIAS[key] = hit["sid"]
            _PENDING_AG.pop(key, None)
            continue
        if now - p["at"] > 6 * 3600:
            _PENDING_AG.pop(key, None)
            continue
        meta = _meta_for_tty(p.get("tty")) if p.get("tty") else None
        if not meta and now - p["at"] > 90:
            _PENDING_AG.pop(key, None)
            continue
        pid = (meta or {}).get("pid")
        agents.note_agent(key, p["agent"])
        out.append({"sid": key, "short": "新", "pid": pid, "cwd": p["cwd"],
                    "title": p["title"], "window_id": p.get("wid"),
                    "tty": p.get("tty") or "", "state": "idle",
                    "agent": p["agent"], "pending": True, "last": p["at"]})
    return out


def _win_host_discover():
    """Windows：会话全由 host 自己用 ConPTY 托管（抓不到别的终端窗口）。
    这里把跑着 agent 的 Session 报成「电脑会话」——手机端才能按会话打开、发消息、按键，
    而不是只剩一个网页终端。新会话的 host sid 就是 claude 的会话 id（--session-id），
    转录因此能直接对上；老会话的 sid 不是 UUID，只能退化成终端视图。"""
    out = []
    for s in list(SESSIONS.values()):
        if getattr(s, "ghost", False) or getattr(s, "hidden", False) or not s.alive:
            continue
        key = getattr(s, "agent", None)
        if not key:
            continue
        title = s.name or key
        try:
            p = project_jsonl(s.cwd, s.sid)
            if p:
                _c, ai = _transcript_meta(p)
                title = ai or title
        except Exception:
            pass
        out.append({"sid": s.sid, "short": s.sid[:8], "pid": s.pid,
                    "cwd": s.cwd, "title": title, "window_id": None, "tty": "",
                    "state": "busy" if time.time() - s.last_out < 1.5 else "idle",
                    "agent": key, "host": True})
    return _apply_titles(out)


def discover(force=False):
    """找出所有 agent 在终端里前台交互运行中的会话（Claude Code + Codex）。

    每个 agent 自己认自己的会话（agents.py 里的适配器）；谁家认错了、
    或者某个 agent 这台机器上没装，都不影响别家。

    单飞：同一时刻只允许一轮发现（一轮 ≈ osascript 列窗口 + N 条 ps，实测 1.2s）。
    后台循环每 2s 用 force=True 刷，其它调用者撞上就先用旧表，绝不排队再跑一份。"""
    if IS_WIN:
        out = _win_host_discover()
        _ext_cache.update(at=time.time(), data=out)   # SSE/热路径吃的就是这份
        return out
    now = time.time()
    if not force and now - _ext_cache["at"] < 2.0:
        return _ext_cache["data"]
    if force:
        _disc_lock.acquire()
    elif not _disc_lock.acquire(False):
        return _ext_cache["data"]      # 有人正在刷新：先给旧表，别双跑
    try:
        now = time.time()
        if not force and now - _ext_cache["at"] < 2.0:
            return _ext_cache["data"]  # 等锁期间别人刚刷完
        out = []
        for ad in agents.adapters():
            try:
                got = ad.discover() or []
            except Exception as e:
                print("[discover] %s 发现会话出错: %s" % (ad.key, str(e)[:120]), flush=True)
                continue
            for x in got:
                x.setdefault("agent", ad.key)
                agents.note_agent(x.get("sid"), ad.key)
                out.append(x)
        out = _dedupe_sessions(out)
        out = _merge_pending(out)
        _apply_titles(out)
        _sync_titles(out)
        _ext_cache.update(at=now, data=out)
    finally:
        _disc_lock.release()
    _reg_update(out)
    return out






# (do_script字节, keycode)：do script 自带一个回车，故只放"回车无害"的按键；
# 方向键回车有副作用（↑+回车=重发上一条），必须走 key code
EXT_SPECIAL_KEYS = {
    "esc":       (b"\x1b", None),
    "shift_tab": (b"\x1b[Z", None),
    "ctrl_c":    (b"\x03", None),
    "ctrl_o":    (b"\x0f", None),   # 展开/收起详细输出（手机键栏从 ⌃C 换成 ⌃O）
    "ctrl_l":    (b"\x0c", None),
    "ctrl_d":    (b"\x04", None),
    "enter":     (b"", None),
    "tab":       (None, 48),   # Tab 不能走 do script（自带回车=补全完直接提交），字节路+keycode 路
    "ctrl_a":    (b"\x01", None),   # 行首
    "ctrl_e":    (b"\x05", None),   # 行尾
    "ctrl_u":    (b"\x15", None),   # 删到行首
    "ctrl_k":    (b"\x0b", None),   # 删到行尾
    "ctrl_w":    (b"\x17", None),   # 删前一个词
    "ctrl_r":    (b"\x12", None),   # 搜历史
    "up":        (None, 126),
    "down":      (None, 125),
    "left":      (None, 123),
    "right":     (None, 124),
    "space":     (b" ", None),    # 空格：滚动/选中文本
    "backspace": (b"\x7f", None), # 退格：改错字（DEL，pty 原始字节）
}

# 全部特殊键的原始字节（控制通道用）：终端那条路 do script 自带回车，
# ↑+回车=重发上一条，esc+回车会误提交，所以按键一律走 pty 原始字节。
_SPECIAL_BYTES = {
    "esc": b"\x1b", "shift_tab": b"\x1b[Z", "ctrl_c": b"\x03", "tab": b"\t",
    "ctrl_o": b"\x0f", "ctrl_l": b"\x0c", "ctrl_d": b"\x04", "enter": b"\r",
    "ctrl_a": b"\x01", "ctrl_e": b"\x05", "ctrl_u": b"\x15", "ctrl_k": b"\x0b",
    "ctrl_w": b"\x17", "ctrl_r": b"\x12",
    "up": b"\x1b[A", "down": b"\x1b[B", "left": b"\x1b[D", "right": b"\x1b[C",
    "space": b" ", "backspace": b"\x7f",
}


def _pty_ctl_write(meta, data):
    """经 wrapper 的控制 socket 把原始字节写进会话 pty。返回是否送达。"""
    meta = meta or {}
    path = meta.get("ctl")
    if not path and meta.get("host") and data:
        # Windows 的 host 会话没有 wrapper 控制 socket：session 自己就是 pty，直接写
        hs = SESSIONS.get(str(meta.get("sid") or ""))
        if hs is None or not getattr(hs, "alive", False):
            return False
        try:
            hs.write(data)
            return True
        except Exception:
            return False
    if not path or not data or not os.path.exists(path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(path)
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        s.close()
        return True
    except Exception:
        return False


def _ctl_type(meta, text):
    """经 wrapper 的控制 socket 直接把字敲进会话 pty（毫秒级）。
    osascript 那条路要先遍历所有窗口和 tab 找 tty（实测 ~250ms，开得多更久），
    再叠上 do script 的打字时间——语音「自动发送」的迟滞主要就是这一段。
    控制字符一律不透传（回车由我们自己加在末尾）。"""
    if not (meta or {}).get("ctl"):
        return False
    body = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 0x20)
    if not body:
        return False
    try:
        # 回车单独一次写、隔几十毫秒：Codex 的输入框会把「同一批里的 \r」当成换行
        # 塞进正文，不认作提交（实测发出去的话停在输入行、会话都建不出来）。
        if not _pty_ctl_write(meta, body.encode("utf-8")):
            return False
        time.sleep(0.09)
        return _pty_ctl_write(meta, b"\r")
    except Exception:
        return False


def _win_host_session(s):
    """这个会话是不是 Windows 上由 host 自己 ConPTY 托管的 agent 会话；是就返回 Session。
    这种会话没有 wrapper meta，输入/屏幕都直接走 Session，不再问终端通道。"""
    if not IS_WIN or not s or not s.get("host"):
        return None
    hs = SESSIONS.get(s.get("sid"))
    return hs if isinstance(hs, Session) and hs.alive else None


def ext_send_text(s, text):
    """按 tty 定位终端再注入。不按 window id 找（Terminal 关窗/合并后 id 失效，
    osascript 报「无效索引」）——模板自己遍历所有窗口按 tty 命中，tab 是会话的稳定身份。"""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    hs = _win_host_session(s)
    if hs is not None:
        hs.write((text + "\r").encode("utf-8"))
        return True, "ok"
    # 快路：有控制 socket（wrapper 起的会话都有）就直接敲，不碰终端通道
    if _ctl_type(_meta_for_pid(s.get("pid")), text):
        return True, "ok"
    return plat.send_text(s.get("tty") or "", text)


def _input_pending(st, typed):
    """发送的文本是否还停在 claude 输入框里没被提交。
    do script 自带的那个回车偶发被 TUI 吞掉（撞上重绘帧），文本就留在输入框，
    手机端只能再手点一次 ⏎。判定：从屏底往上找输入框提示行（❯/> 开头——
    转录回显里的 ❯ 行都在它上面，屏底最下方的那个才是输入框），
    未提交时输入块里有文本尾巴；提交后输入框只剩提示符。"""
    cols, rin = st["win"]
    tail = typed.strip()[-10:]
    if not tail:
        return False
    y0 = -1
    for y in range(rin - 1, max(-1, rin - 12), -1):
        t = _row_plain(st["scr"].buffer[y], cols)
        if t[:1] in ("❯", ">", "›"):      # Claude ❯ / Codex ›
            y0 = y
            break
    if y0 < 0:
        return False
    block = []
    for y in range(y0, min(rin, y0 + 6)):   # 长输入在框内折行，尾巴可能在提示行下方
        t = _row_plain(st["scr"].buffer[y], cols)
        if y > y0 and not t.strip():
            break
        block.append(t.rstrip())
    return tail in "".join(block)


def ext_send_special(s, key, meta=None):
    """meta：调用方已经查好的 wrapper meta（正常传 None，这里自己按 pid 查）。
    h_ext_key 会在缓存没命中时自己读盘查一次再传进来——否则一个 stale 的
    meta 缓存会让这个会话的按键整段退化成 osascript（很多时候就是没反应）。"""
    hs = _win_host_session(s)
    if hs is not None:
        if key == "slash_model":
            hs.write(b"\x15")
            time.sleep(0.05)
            hs.write(b"/model\r")
            return True, "ok"
        b = _SPECIAL_BYTES.get(key)
        if b is None:
            return False, "未知按键: %s" % key
        hs.write(b)
        return True, "ok"
    if key == "slash_model":
        # 一键 /model：先清掉输入行里已有的字（⌃U），再敲 "/model" 并回车，
        # 发出去的就只有这一条命令，不会变成「半句话/model」。
        meta = meta if meta is not None else _meta_for_pid(s.get("pid"))
        if not (meta or {}).get("ctl"):
            return False, "这个会话没有控制通道，发不了 /model"
        _pty_ctl_write(meta, b"\x15")
        time.sleep(0.05)
        ok = _ctl_type(meta, "/model")
        return ok, ("ok" if ok else "写不进会话")
    data, kc = EXT_SPECIAL_KEYS.get(key, (None, None))
    if data is None and kc is None:
        return False, "未知按键: %s" % key
    # 首选：wrapper 控制 socket → 原始字节进会话 pty。它跟终端无关（Warp/iTerm/
    # VS Code 这些没有 AppleScript 通道的终端也能用），所以**先问 socket 再问 tty**。
    if meta is None:
        meta = _meta_for_pid(s.get("pid"))
    if _pty_ctl_write(meta, _SPECIAL_BYTES.get(key)):
        return True, "ok"
    if not s.get("tty"):
        return False, "未找到对应的终端窗口（可能已关闭）"
    return plat.send_special(s.get("tty") or "", key, kc, data)


async def _send_verified(s, text):
    """发送文本，然后**在后台**核实回车有没有被 TUI 重绘吞掉（文本还停在输入框就补一次，
    最多 3 次；无全彩镜像无法核实，只发一遍）。
    osascript 是同步阻塞（最长 20s），必须进 executor——留在循环里会把整个 host 冻住。
    核实不再挡在响应前面：以前每次发送至少白等 0.35s，语音自动发送的迟滞感主要来自这里。"""
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(None, ext_send_text, s, text)
    if ok:
        meta = _meta_for_pid(s.get("pid"))
        if meta is not None or _win_host_session(s) is not None:
            asyncio.ensure_future(_verify_enter(s, meta, text))
    return ok, msg


async def _verify_enter(s, meta, text):
    """后台补回车：文本已经敲进去了，就算晚一两百毫秒用户也看不出来。"""
    loop = asyncio.get_event_loop()
    typed = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    try:
        for _ in range(3):
            await asyncio.sleep(0.35)
            hs = _win_host_session(s)
            if hs is not None:
                # host 托管的会话直接看 pyte 屏（在循环线程里读，跟 feed 不并发）
                st = {"win": (hs.screen.columns, hs.screen.lines), "scr": hs.screen}
            else:
                st = await loop.run_in_executor(None, _mirror_feed, s["sid"], meta)
            if st is None or not _input_pending(st, typed):
                return
            await loop.run_in_executor(None, ext_send_special, s, "enter")
    except Exception:
        pass


_VOL_PROBE = {}    # /Volumes 卷根 -> 上次探测时刻（-1.0 = 探测通过永久放行）


def _vol_root(cand):
    """路径所属卷根：/Volumes/<卷>/… 返回 /Volumes/<卷>；内盘返回 ""（无需探测）。"""
    if cand.startswith("/Volumes/"):
        parts = cand.split("/", 3)
        return "/".join(parts[:3]) if len(parts) >= 3 else "/Volumes"
    return ""


def _vol_probe_once(root):
    # 2026-11-26 三次冻结的教训：TCC 弹窗挂起时 stat/isdir 能过、opendir（readdir）
    # 才挂——探测必须试会挂的那一类操作，用 isdir 探出「假通行」闸门形同虚设。
    try:
        with os.scandir(root):
            return True
    except OSError:
        return False


def _vol_ok(root, wait_s=0.5):
    """这个目录真能列吗？root 传将要访问的实际目录，状态按卷记账。

    TCC 弹窗没人点 / USB 盘休眠时 opendir 会卡进内核永不返回——必须独立线程限时
    探测。TCC 粒度名义上是「卷」，但 2026-11-26 威刚盘实测：卷根能列、项目子目录
    照样挂死（5 个线程 1643/1643 钉在 opendir）——探测必须打在真实目标上，
    记账却必须按卷（不然收件箱逐会话各付一次超时）。探不过/超时的卷 10 分钟内
    全卷快败；内盘路径直接放行。用户不点授权：弹窗挂着或直接拒绝，host 都健康，
    只是该盘上的项目暂不可列。"""
    root = root or ""
    vr = _vol_root(root)
    if not root or vr == "":
        return True
    now = time.time()
    st = _VOL_PROBE.get(vr)
    if st == -1.0:
        return True
    if st and now - st < 600:
        return False
    _VOL_PROBE[vr] = now
    box = []
    th = threading.Thread(target=lambda: box.append(_vol_probe_once(root)), daemon=True)
    th.start()
    th.join(wait_s)
    if box and box[0]:
        _VOL_PROBE[vr] = -1.0
        return True
    return False


def recent_dirs():
    out = []
    p = Path.home() / ".claude" / "projects"
    if p.is_dir():
        for d in sorted(p.iterdir(), key=lambda x: -x.stat().st_mtime)[:12]:
            parts = d.name.split("-")
            if len(parts) > 1:
                cand = "/" + "/".join(parts[1:])
                # 外置盘候选先限时探测（2026-11-26 事故：TCC 弹窗挂起 + stat 在事件
                # 循环上 → 整站冻结 20 分钟，app 全离线）。没通过的卷直接跳。
                if _vol_ok(_vol_root(cand)) and Path(cand).is_dir():
                    out.append(cand)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:8]


def _listen_touch(request):
    """把「有客户端来过」记进智能提醒的省钱闸门（见 _listen_now）。
    本机 hook / 自己人（127.x、::1）不算：Claude Code 干活时本地请求不断，
    算进去就等于闸门永远开着，那点省钱就白做了。局域网直连的手机 IP 不是本机。"""
    try:
        ip = request.remote or ""
        if ip and not (ip.startswith("127.") or ip in ("::1", "localhost")):
            _LISTEN_AT[0] = time.time()
    except Exception:
        pass


async def require_token(request):
    t = (request.query.get("t") or request.cookies.get("codeboss_t")
         or request.cookies.get("ptyhost_t")
         # 富客户端可以直接带走头（不会进历史/Referer/访问日志）；?t= 留着兼容旧壳
         or (request.headers.get("authorization") or "").replace("Bearer", "", 1).strip()
         or (request.headers.get("x-cb-token") or "").strip() or "")
    if t in _tokens_current():
        # 直连（局域网）发消息：先按中继下发的条数挡一道，再把这一条记到本机。
        # 读请求不挡——免费版可以一直看。
        _m = (request.method or "GET").upper()
        _wr = _m not in ("GET", "HEAD", "OPTIONS")
        if _wr and _is_send_req(request.path) and _send_over():
            _sc = _send_cap_eff()
            raise web.HTTPForbidden(
                text=json.dumps({"ok": False, "limit": "sends",
                                 "sends": {"used": max(int(_USE_ST.get("sends") or 0),
                                                       int(_USE_ST.get("sends_local") or 0)),
                                           "cap": _sc},
                                 "error": "今天的免费消息额度已用完（%d 条），明天恢复" % _sc},
                                ensure_ascii=False),
                content_type="application/json")
        # 时长墙已摘（2026-11-26 拍板：收费墙只按条数，条数墙在上面挡）。
        if _wr and _feat_blocked(request.path):
            raise _feat_403()
        _use_send_count(request)
        _use_touch(request)
        _listen_touch(request)
        return
    dt = request.query.get("dt") or ""   # P2P 直连票据（relay 签发，见 _dt_verify）
    if dt and _dt_verify(dt):
        if not _dt_path_ok(request.path):
            raise web.HTTPForbidden(text="dt 只授权只读路径")
        # 直连请求不经过中继，条数墙只能在这里挡（中继断链时也有效）。
        # 「看不拦」：读请求不拦，只有写操作才可能被条数墙/功能墙拒绝。
        _m = (request.method or "GET").upper()
        _wr = _m not in ("GET", "HEAD", "OPTIONS")
        if _wr and _is_send_req(request.path) and _send_over():
            _sc = _send_cap_eff()
            raise web.HTTPForbidden(
                text=json.dumps({"ok": False, "limit": "sends",
                                 "sends": {"used": max(int(_USE_ST.get("sends") or 0),
                                                       int(_USE_ST.get("sends_local") or 0)),
                                           "cap": _sc},
                                 "error": "今天的免费消息额度已用完（%d 条），明天恢复" % _sc},
                                ensure_ascii=False),
                content_type="application/json")
        if _wr and _feat_blocked(request.path):
            raise _feat_403()
        _use_send_count(request)
        _use_touch(request)
        _listen_touch(request)
        return
    raise web.HTTPForbidden(text="bad token")


def _h(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------- 语音识别（PTT）：按住说话 → 这里转写 → 文字回给页面 ----------
ASR_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
ASR_MODEL = "qwen3-asr-flash"
AUDIO_DIR = Path(__file__).parent / "audio"      # 留档（gitignore）


def _dashscope_key():
    """key 现在统一在中继；这里只为老部署/自建保留。值必须像一把 key：
    去掉行内注释、空值、含非 ASCII 的一律当没配（曾经把注释文本当 key 用过，
    Authorization 头 latin-1 编码直接炸）。"""
    cand = [os.environ.get("DASHSCOPE_API_KEY", "")]
    try:
        with open(os.path.expanduser("~/.codeboss/.env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DASHSCOPE_API_KEY="):
                    cand.append(line.split("=", 1)[1])
    except OSError:
        pass
    for v in cand:
        v = v.split("#", 1)[0].strip().strip('"').strip("'")
        if v and v.isascii():
            return v
    return ""


def _ffmpeg():
    """找 ffmpeg：launchd 起的服务 PATH 很短，which 常常找不到，只能挨个试。"""
    import shutil
    for p in (os.environ.get("CB_FFMPEG"), shutil.which("ffmpeg"),
              "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/local/bin/ffmpeg"):
        if p and os.path.exists(p):
            return p
    return None


def _audio_gc():
    """留档目录别无限涨：只留 7 天内的录音（2026-11-03 用户确认这个清理范围）。"""
    try:
        cut = time.time() - 7 * 86400
        for f in AUDIO_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cut:
                f.unlink()
    except OSError:
        pass


LOGS_KEEP_S = 7 * 86400        # logs/ansi 留几天
LOGS_MAX_BYTES = 300 << 20     # 或总量超过 300MB 就先删最旧的
LOG_ROT_BYTES = 20 << 20       # 启动时 server.log 超过 20MB 就轮转


def _log_rotate_at_start():
    """launchd 把 stdout 直接追加进 logs/server.log，进程里轮不动它（fd 在 launchd 手上）。
    启动时（新进程会重新打开文件）把它挪走，留两份历史 —— 不在运行中截断。"""
    for nm in ("server.log", "server.err.log"):
        p = HERE / "logs" / nm
        try:
            if not p.exists() or p.stat().st_size < LOG_ROT_BYTES:
                continue
            for i in (2, 1):
                src = HERE / "logs" / ("%s.%d" % (nm, i))
                if src.exists():
                    os.replace(src, HERE / "logs" / ("%s.%d" % (nm, i + 1)))
            os.replace(p, HERE / "logs" / (nm + ".1"))
        except OSError:
            pass


LOG_ROT_KEEP_DAYS = 7      # 轮转出来的 server.log.<日期> 留 7 天
LOG_HIST_MAX = 20 << 20    # 但历史加一起也不超过 20MB（多了就是白占盘，日志只是排查用）


def _log_rotate_live():
    """运行中给 server.log 减肥。不能像启动时那样改名：launchd 的 fd 还指着旧 inode，
    改名后新进程的日志会继续写进那份「历史文件」，logs/server.log 永远不再出现。
    所以是 copytruncate——先抄一份尾部，再把原文件截成 0；launchd 是 O_APPEND 打开的，
    截断后下一次写自然落在新文件开头。轮转出来的历史按日期存，7 天后删。"""
    logs = HERE / "logs"
    now = time.time()
    for nm in ("server.log", "server.err.log"):
        p = logs / nm
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < LOG_ROT_BYTES:
            continue
        dst = logs / ("%s.%s" % (nm, time.strftime("%Y%m%d-%H%M%S", time.localtime(now))))
        try:
            with open(p, "rb") as src, open(dst, "wb") as out:
                if st.st_size > LOG_ROT_BYTES:
                    src.seek(st.st_size - LOG_ROT_BYTES)   # 只留尾部，别把几百 MB 全抄一遍
                shutil.copyfileobj(src, out, 1 << 20)
            os.truncate(p, 0)
            print("[logs] 轮转 %s（%.0fMB → %s）" % (nm, st.st_size / 1048576.0, dst.name), flush=True)
        except OSError:
            pass
    cut = now - LOG_ROT_KEEP_DAYS * 86400
    hist = []
    for f in logs.glob("server*.log.*"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cut:                 # 7 天前的直接删
            try:
                f.unlink()
            except OSError:
                pass
        else:
            hist.append((st.st_mtime, st.st_size, f))
    total = sum(x[1] for x in hist) if hist else 0
    for _mt, sz, f in sorted(hist, key=lambda x: x[0]):   # 有 20MB 历史就够了
        if total <= LOG_HIST_MAX:
            break
        try:
            f.unlink()
            total -= sz
        except OSError:
            pass


def _logs_gc():
    """logs/ansi 是全彩镜像：会话多、开得久会涨到几百 MB（实测 610 个文件 452MB）。
    按「7 天」+「总量 300MB」两道闸清，活着的会话不动。"""
    try:
        live = {s.sid for s in SESSIONS.values()}
    except Exception:
        live = set()
    try:
        files = []
        for f in ANSI_DIR.iterdir():
            if not f.is_file():
                continue
            sid = f.name.split(".")[0]
            if sid in live:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
        cut = time.time() - LOGS_KEEP_S
        total = 0
        for mt, sz, f in files:
            if mt < cut:
                try:
                    f.unlink()
                except OSError:
                    pass
            else:
                total += sz
        if total > LOGS_MAX_BYTES:
            for mt, sz, f in sorted(files, key=lambda x: x[0]):
                if total <= LOGS_MAX_BYTES:
                    break
                try:
                    f.unlink()
                    total -= sz
                except OSError:
                    pass
    except OSError:
        pass
    try:
        scrollback.gc(live)   # 滚动历史仓跟着一起 GC（14 天口径，活会话不动）
    except Exception:
        pass


async def _logs_gc_loop(app):
    async def _run():
        await asyncio.sleep(120)
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, _logs_gc)
                await asyncio.get_event_loop().run_in_executor(None, _log_rotate_live)
            except Exception:
                pass
            await asyncio.sleep(6 * 3600)
    app["logs_gc_task"] = asyncio.create_task(_run())


def _ptt_asr_sync(raw, ext):
    """落盘 → ffmpeg 转 16k 单声道 wav → DashScope qwen3-asr-flash。阻塞，跑 executor。"""
    import base64 as _b64
    import urllib.request as _ur
    AUDIO_DIR.mkdir(exist_ok=True)
    _audio_gc()
    stamp = time.strftime("%m%d-%H%M%S")
    src = AUDIO_DIR / ("ptt-" + stamp + ext)
    dst = AUDIO_DIR / ("ptt-" + stamp + ".wav")
    src.write_bytes(raw)
    fx = _ffmpeg()
    if not fx:
        return "", "没找到 ffmpeg（brew install ffmpeg，或设 CB_FFMPEG 指向它）"
    p = subprocess.run(
        [fx, "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-f", "wav", str(dst)],
        capture_output=True, timeout=60)
    if p.returncode != 0 or not dst.exists():
        return "", "ffmpeg 转码失败: " + p.stderr.decode("utf-8", "replace")[:150]
    key = _dashscope_key()
    if not key:
        return "", "没有 DASHSCOPE_API_KEY（环境变量或 ~/.codeboss/.env）"
    body = {
        "model": ASR_MODEL,
        "input": {"messages": [{"role": "user", "content": [
            {"audio": "data:audio/wav;base64," + _b64.b64encode(dst.read_bytes()).decode()}]}]},
        "parameters": {"asr_options": {"language": "zh"}},
    }
    req = _ur.Request(ASR_URL, data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    try:
        return d["output"]["choices"][0]["message"]["content"][0]["text"], ""
    except Exception:
        return "", "识别返回异常: " + json.dumps(d, ensure_ascii=False)[:200]


def _jsonz(data, **kw):
    """json_response + gzip 协商。完整响应，浏览器自动解压（老 WebView 也安全）；
    经中继时隧道内部强制 identity（不压），由 relay 在边缘压给手机——省的是 VPS 出方向。"""
    resp = web.json_response(data, **kw)
    resp.enable_compression()
    return resp


_PROJ_CACHE = {}
# 这些目录名不是「项目」：家在 ~ 根目录开的会话、/tmp、/Users 之类也会有个 cwd，
# 逐字当项目键会产生 tmp/xuxingyu/.app 这种没意义的词表。
_PROJ_STOP_NAMES = {
    "tmp", "temp", "home", "users", "root", "var", "private", "desktop",
    "documents", "downloads", "volumes", "applications", "library", "public",
}


def _git_root(cwd):
    """向上找 .git，返回仓库根；不是仓库就返回 cwd 本身；目录不存在返回空。

    入口先过卷闸门：cwd 在 /Volumes 下且卷被 TCC 弹窗挂起 / USB 休眠时，
    任何 stat 都会卡进内核永不返回（2026-11-26 两次整站冻结都是这条线）。
    所有调用点（_proj_key/_proj_facts/_proj_scan_due）都从这走，一处修四个。"""
    if not _vol_ok(_vol_root(cwd or "")):
        return ""
    try:
        start = os.path.realpath(cwd)
        if not os.path.isdir(start):
            return ""
        p = start
        while True:
            if os.path.exists(os.path.join(p, ".git")):
                return p
            up = os.path.dirname(p)
            if up == p:
                return start
            p = up
    except Exception:
        return ""


def _proj_key(cwd):
    """会话工作目录 → 项目键（git 根目录名），给语音词表用。缓存 10 分钟。
    子目录开会话也归到同一个项目。"""
    cwd = (cwd or "").strip()
    if not cwd:
        return ""
    now = time.time()
    hit = _PROJ_CACHE.get(cwd)
    if hit and now - hit[1] < 600:
        return hit[0]
    key = ""
    try:
        root = _git_root(cwd)
        if not root:
            return ""
        real = os.path.realpath(root)
        if real == os.path.realpath(os.path.expanduser("~")):
            return ""                      # 在家目录开会话：不是项目
        key = os.path.basename(real.rstrip("/")) or ""
        if key.startswith(".") or key.lower() in _PROJ_STOP_NAMES:
            return ""
        key = re.sub(r"[^A-Za-z0-9._\-]+", "-", key).strip("-")[:40]
        if not key or key.startswith("."):
            return ""                      # 「数据重组.app」这类目录名洗不出有效项目名
    except Exception:
        return ""
    if len(_PROJ_CACHE) > 200:
        _PROJ_CACHE.clear()
    _PROJ_CACHE[cwd] = (key, now)
    return key


# 仓库扫描：只推「项目摘要 + 术语种子」给中继，不上传源码内容。
_PROJ_SCAN = {}      # 仓库根 → 上次扫描时间
_SCAN_STOP = {
    "src", "lib", "bin", "app", "main", "docs", "doc", "test", "tests", "spec",
    "build", "dist", "out", "target", "node_modules", "vendor", "public", "static",
    "assets", "images", "img", "data", "config", "conf", "scripts", "tools", "utils",
    "examples", "example", "demo", "temp", "tmp", "log", "logs", "cache", "coverage",
    "package", "readme", "license", "changelog", "todo", "makefile", "dockerfile",
    "version", "sessions", "prefs", "audio", "saycache", "logs", "release",
    "token", "tokens", "secret", "secrets", "credential", "credentials",
    "password", "passwd", "key", "keys",
}
_SCAN_EXT_STOP = {
    ".dmg", ".apk", ".zip", ".tar", ".gz", ".tgz", ".log", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".icns", ".pdf", ".mp4", ".mov", ".wav", ".mp3", ".bak", ".swp",
    ".db", ".sqlite", ".sqlite3", ".pid", ".lock", ".service",
}
_SCAN_VER_RE = re.compile(r"[-_v]\d+\.\d+(?:\.\d+)?", re.I)


def _scan_term_ok(name):
    """仓库里哪些名字值得当术语：够长或带 . - _，排除太泛的词和构建产物。
    太泛的词（test/build/docs）会误伤正常说话，纠正时反而改错；
    带版本号的安装包（codeboss-0.5.40-mac.dmg）、数据库/日志临时文件也一律不要。"""
    if not re.match(r"^[A-Za-z][A-Za-z0-9._\-]*$", name or ""):
        return False
    ext = os.path.splitext(name)[1].lower()
    if ext in _SCAN_EXT_STOP or _SCAN_VER_RE.search(name):
        return False
    if re.search(r"\.(db|sqlite3?)([-.]|$)", name, re.I):   # codeboss.db-shm/-wal
        return False
    base = name.rsplit(".", 1)[0]
    if len(base) < 3 or len(base) > 30 or base.lower() in _SCAN_STOP:
        return False
    return len(base) >= 5 or ("." in name or "-" in name or "_" in name)


def _proj_facts(cwd, proj):
    """扫一眼仓库根：顶层名字 + README 首行 + 语言线索。24 小时一次，返回 None 表示这次跳过。"""
    root = _git_root(cwd)   # 非仓库时本就返回 cwd；"" 只可能是卷被闸门挡住，别旁路回去
    if not root or not _vol_ok(root) or not os.path.isdir(root):
        return None
    now = time.time()
    hit = _PROJ_SCAN.get(root)
    if hit and now - hit < 86400:
        return None
    _PROJ_SCAN[root] = now
    if len(_PROJ_SCAN) > 100:
        _PROJ_SCAN.clear()
        _PROJ_SCAN[root] = now
    terms, langs, desc = [], [], ""
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return None
    lower = {n.lower() for n in names}
    if "package.json" in lower:
        langs.append("Node/npm")
    if "pyproject.toml" in lower or "requirements.txt" in lower or "setup.py" in lower:
        langs.append("Python")
    if "cargo.toml" in lower:
        langs.append("Rust")
    if "go.mod" in lower:
        langs.append("Go")
    if "androidmanifest.xml" in lower or "settings.gradle" in lower or "build.gradle" in lower:
        langs.append("Android/Java")
    if any(n.endswith(".swift") for n in lower):
        langs.append("Swift")
    if any(n.endswith(".ts") or n.endswith(".tsx") for n in lower):
        langs.append("TypeScript")
    for name in names[:200]:
        if name.startswith("."):
            continue
        if _scan_term_ok(name):
            t = name if "." in name else name.rsplit(".", 1)[0]
            if t.lower() not in {x.lower() for x in terms}:
                terms.append(t)
        if len(terms) >= 24:
            break
    if len(terms) < 24:   # 顶层太干净时（很多项目只有 src/、app/）再看一层
        dirs = [n for n in names if not n.startswith(".")
                and os.path.isdir(os.path.join(root, n)) and n.lower() not in _SCAN_STOP]
        for d in dirs[:3]:
            try:
                subs = sorted(os.listdir(os.path.join(root, d)))
            except Exception:
                continue
            for name in subs[:80]:
                if name.startswith(".") or not _scan_term_ok(name):
                    continue
                t = name if "." in name else name.rsplit(".", 1)[0]
                if t.lower() not in {x.lower() for x in terms}:
                    terms.append(t)
                if len(terms) >= 24:
                    break
            if len(terms) >= 24:
                break
    for cand in ("README.md", "README", "readme.md"):
        p = os.path.join(root, cand)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read(4096)
        except Exception:
            break
        for line in txt.splitlines():
            s = line.strip().lstrip("#").strip()
            if not s or s.startswith("![") or s.startswith("[!") or s.startswith("<"):
                continue
            if set(s) <= set("-=*_ "):
                continue
            desc = re.sub(r"\s+", " ", s)[:60]
            break
        break
    summary = ""
    if desc:
        summary = desc
    elif langs:
        summary = "%s 项目（%s）" % (proj, "、".join(langs))
    elif proj:
        summary = "%s 项目" % proj
    return {"proj": proj, "summary": summary, "terms": terms}


def _proj_scan_due(cwd):
    """这个仓库今天还没扫过吗（给列表轮询用，避免每次都排任务）。"""
    root = _git_root(cwd)
    if not root:
        return False
    hit = _PROJ_SCAN.get(root)
    return not hit or time.time() - hit >= 86400


async def _proj_scan_push(cwd, proj):
    """把仓库扫描结果推给中继（异步、失败静默）。语音识别因此从第一天就认得项目词。"""
    import aiohttp
    try:
        loop = asyncio.get_event_loop()
        facts = await loop.run_in_executor(None, _proj_facts, cwd, proj)
        if not facts or not facts.get("terms"):
            return
        rc = _relay_cfg()
        base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
        if not (base and dev and sec):
            return
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/ptt_proj", json=facts,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                await r.read()
    except Exception as e:
        print("[codeboss] proj scan push failed: %s" % str(e)[:120], flush=True)


async def _asr_via_relay(raw, ctype, proj="", ctx="", nofix=False):
    """本地没配 key 时把音频转给中继做转写（key 只在 VPS）。
    走 host 自己的上行，不占中继出方向；中继只把音频发给 DashScope 一次。
    proj 让中继按项目词表拼提示词、并在转写后纠正术语；
    ctx 是页面的纠错上下文（base64），中继据此在乱环境下自动纠错。"""
    import aiohttp
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return "", "本机没配语音服务，且还没接入中继（server.py --enroll）", None
    headers = {"Content-Type": ctype or "audio/m4a",
               "X-CB-Dev": dev, "X-CB-Sec": sec}
    if proj:
        headers["X-CB-Proj"] = proj
    if ctx:
        headers["X-CB-Ctx"] = ctx
    if nofix:
        headers["X-CB-Nofix"] = "1"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/ptt", data=raw,
                              headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                d = await r.json(content_type=None)
    except Exception as e:
        return "", "中继语音不可用: %s" % str(e)[:120], None
    if not d.get("ok"):
        # 使用时长用完了：把 quota 带回去，页面据此弹额度浮层（语音和别的操作共用额度）
        q = d.get("quota") if d.get("upgrade") or r.status == 429 else None
        return "", d.get("error") or "中继语音识别失败", q
    return d.get("text") or "", "", None


async def h_ext_ptt(request):
    await require_token(request)
    raw = await request.read()
    if not raw:
        return web.json_response({"ok": False, "error": "空音频"}, status=400)
    if len(raw) > 20 * 1024 * 1024:
        return web.json_response({"ok": False, "error": "录音太大"}, status=400)
    ct = request.headers.get("Content-Type") or ""
    ext = ".webm" if "webm" in ct else ".m4a"
    # 页面带上当前会话的工作目录/项目键：中继按项目词表转写、并纠正术语。
    # 页面从会话列表拿到 host 算好的 proj 时直接用；老页面没带就自己从 cwd 算。
    cwd = request.query.get("cwd") or ""
    p = (request.query.get("p") or "").strip()
    proj = p if re.fullmatch(r"[A-Za-z0-9._\-]{1,40}", p) else _proj_key(cwd)
    if proj and _proj_scan_due(cwd):
        try: asyncio.create_task(_proj_scan_push(cwd, proj))
        except Exception: pass
    err = ""
    text = ""
    quota = None
    try:   # 临时诊断：自动听写的确认阶段到底有没有把音频送上来
        print("[ptt] %dB ms=%s proj=%s" % (len(raw), request.query.get("ms") or "-",
                                           proj or "-"), flush=True)
    except Exception:
        pass
    if _dashscope_key():
        try:
            text, err = await asyncio.get_event_loop().run_in_executor(
                None, _ptt_asr_sync, raw, ext)
        except Exception as e:
            err = "识别异常: %s" % str(e)[:150]
    else:
        # 页面的纠错上下文：header 原样转给中继（乱环境自动纠错用）
        text, err, quota = await _asr_via_relay(
            raw, ct, proj,
            ctx=request.headers.get("X-CB-Ctx") or "",
            nofix=(request.headers.get("X-CB-Nofix") or "") in ("1", "true"))
    try:
        print("[ptt] -> %s %r" % ("err" if err else "ok", (err or text or "")[:60]), flush=True)
    except Exception:
        pass
    if err:
        if quota:
            return web.json_response({"ok": False, "error": err, "quota": quota}, status=403)
        return web.json_response({"ok": False, "error": err}, status=502)
    return web.json_response({"ok": True, "text": text, "bytes": len(raw)})


async def h_ptt_fix(request):
    """页面把「ASR 原文 → 用户改完发送的文本」差异报上来，转给中继学项目词表。
    纯文本、尽力而为：失败不影响发送，页面也不提示。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    pairs = d.get("pairs") if isinstance(d, dict) else None
    p = (str(d.get("proj") or "") if isinstance(d, dict) else "").strip()
    cwd = (d.get("cwd") or "") if isinstance(d, dict) else ""
    proj = p if re.fullmatch(r"[A-Za-z0-9._\-]{1,40}", p) else _proj_key(cwd)
    if not proj or not isinstance(pairs, list) or not pairs:
        return web.json_response({"ok": True, "learned": 0})
    import aiohttp
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return web.json_response({"ok": True, "learned": 0})
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/ptt_fix",
                              json={"proj": proj, "pairs": pairs[:12]},
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                out = await r.json(content_type=None)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:120]}, status=502)
    return web.json_response(out if isinstance(out, dict) else {"ok": True})


def _android_dir():
    """Android 工程目录：仓库在 clients/android；发布目录/老布局在 HERE/android。"""
    for q in (_shared("clients", "android"), HERE / "android"):
        if (q / "build").is_dir() or (q / "AndroidManifest.xml").is_file():
            return q
    return _shared("clients", "android")


def _apk_file(product=None):
    """本线 APK 产物：android/build/<线>/<线>-terminal.apk（build.sh OUT 结构，两线同构）。"""
    p = branding.valid_product(product or _host_product())
    return _android_dir() / "build" / p / (p + "-terminal.apk")


def _apk_product_for(request):
    """这次供包该给哪条线：路径点名的（/agentboss.apk）> 壳带的 ?product= > 客户端
    cookie/头 > 本机线。一个 host 服务两条线，按本机线供包会让 agentboss 手机
    下载到工头的包——装上是第二个口袋工头，图标一模一样（2026-11-20 用户）。"""
    head = (getattr(request, "path", "") or "").lstrip("/").split(".")[0]
    if head in branding.PRODUCTS:
        return head
    q = (request.query.get("product") or "").strip().lower()
    if q in branding.PRODUCTS:
        return q
    return _client_product_for(request)


async def h_apk(request):
    """安卓客户端 APK 下载：经中继访问时 host-link 自动补 token，局域网带 ?t= 或 cookie。"""
    await require_token(request)
    p = _apk_product_for(request)
    f = _apk_file(p)
    if not f.exists():
        return web.json_response(
            {"error": "APK 还没构建（%s 线 android/build/%s 下没有产物）" % (p, p)}, status=404)
    ver = str((_apk_meta(p) or {}).get("ver") or "").strip()
    fname = "%s-terminal-%s.apk" % (p, ver) if ver else f.name
    return web.Response(
        body=f.read_bytes(), content_type="application/vnd.android.package-archive",
        headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


def _apk_meta(product=None):
    """APK + 版本信息（build.sh 落的 apk.json）。没有就返回 None。"""
    p = branding.valid_product(product or _host_product())
    apk = _apk_file(p)
    meta = apk.parent / "apk.json"
    if not apk.exists():
        return None
    m = {}
    try:
        m = json.loads(meta.read_text())
    except (OSError, ValueError):
        pass
    m.setdefault("ver", ""), m.setdefault("code", 0)
    m["url"] = "/%s.apk" % p
    return m


def _use_cache_cap(d):
    """h_plan 的成功响应就是一份权威额度快照：顺手把「有效上限」缓存下来。
    断链后的本地估算（_plan_local）和直连额度墙都靠它——不缓存的话，Max 用户
    断链时会被按默认的 60 分钟报。
    套餐上限和时长包余额分开缓存：断链后也要分得清哪部分是套餐、哪部分是买的包，
    不然页面把两者加总当成「套餐上限」，用户会误解。"""
    try:
        us = (d or {}).get("usage") or {}
        lim = (d or {}).get("limits") or {}
        sec_mode = "use_cap_sec" in us or "use_used_sec" in us
        if not sec_mode and "use_cap_eff" not in us:
            return
        _use_load()
        if sec_mode:   # 新中继：原生秒
            cap = int(us.get("use_cap_sec") or 0)
            used = int(us.get("use_used_sec") or 0)
            cred = int(us.get("use_credit_sec") or 0)
            try:
                _USE_ST["unit"] = max(1, min(60, int(us.get("use_unit") or _USE_UNIT_DEFAULT)))
            except (TypeError, ValueError):
                pass
        else:          # 老中继：分钟 → 秒
            cap = int(us.get("use_cap_eff") or 0) * 60
            used = int(us.get("use_min_day") or 0) * 60
            cred = int(us.get("use_credit_min") or 0) * 60
        plan_cap = int(lim.get("use_min_day") or 0) * 60   # 套餐那一份（不含时长包）
        today = _use_day()
        first = not _USE_ST.get("synced")
        new_day = bool(_USE_ST.get("date")) and _USE_ST["date"] != today
        # 已用也要校准：以前只缓存上限，本地 used 一直是自己数的那个数，
        # 绑定前攒的分钟一顶就满，P2P/直连被本地墙误拦（中继账上才 5/60）。
        if first or new_day:
            # 当日第一次对上账：本机之前攒的分钟来路不明（可能是绑定前/换账号前记的），
            # 以中继的账为准，本地待报队列一并清掉，别事后算到账号头上。
            if _USE_PEND or _USE_ACT:
                print("[use] 对账：丢弃本地待报 %d 个窗口 / %d 条动作"
                      % (len(_USE_PEND), len(_USE_ACT)), flush=True)
            _USE_PEND.clear()
            _USE_ACT.clear()
            _USE_ST["used"] = used
            _use_dirty()
        elif used != int(_USE_ST.get("used") or 0):
            _USE_ST["used"] = used       # 中继是计费方：它说多少就是多少
            _use_dirty()
        if cap != int(_USE_ST.get("cap") or 0) or first:
            _USE_ST["cap"] = cap
            _use_dirty()
        _USE_ST["date"] = today
        _USE_ST["cap_known"] = True      # 中继给的上限，可以拿来拦直连了
        _USE_ST["plan_cap"] = plan_cap
        _USE_ST["credit"] = cred
        _USE_ST["used_at"] = used
        _USE_ST["synced"] = time.time()
        # 免费版消息条数（新中继才有）：已发和中继下发的上限一起缓存，
        # 直连/P2P 上由开发机自己挡；老中继没有 sends_day 就不动。
        try:
            if us.get("sends_day") is not None:
                _USE_ST["sends"] = int(us.get("sends_day") or 0)
            if "sends_day" in lim:
                _USE_ST["sends_cap"] = int(lim.get("sends_day") or 0)
                _USE_ST["sends_cap_known"] = True
            elif (d or {}).get("plan") not in ("", None, "free"):
                # 付费档不限条数：老中继不带 sends_day 时也要把旧的免费上限清掉，
                # 别让升级后的设备还按 30 条拦。
                _USE_ST["sends_cap"] = 0
                _USE_ST["sends_cap_known"] = True
        except (TypeError, ValueError):
            pass
        if first or new_day:
            _USE_ST["sends_local"] = 0   # 新的一天从 0 数
        # 套餐功能开关一起缓存：提醒是 Pro/Max 功能（plans.json features.remind），
        # 断网/中继不可达时开发机本地兜底也要按同一份快照收住。
        ft = (d or {}).get("features")
        if isinstance(ft, dict):
            _USE_ST["feat"] = {str(k): bool(v) for k, v in ft.items()}
            _USE_ST["feat_ts"] = time.time()
        _use_save()
    except Exception:
        pass


def _plan_local():
    """中继断链时的本地估算：host 自己也在按窗口记账（见 _use_touch），恢复后补报。
    套餐和时长包分开报：套餐上限用缓存的原值；超出套餐的部分从包的缓存余额里扣
    （与中继 use_take 的扣法一致）。老状态文件里没有分开的数，才退回按一整池报。
    单位：内部一律秒；老字段（use_min_day / use_credit_min / use_cap_eff）按分钟给老页面。"""
    try:
        _use_load()
        if not _USE_ST.get("synced"):
            return None      # 从没跟中继对上过账：额度不明，别乱报一个数
        _use_roll_day()
        cap = int(_USE_ST.get("cap") or 0)
        # 只报中继确认过的「已用」：本机待报的窗口还没归账，报大了会让手机显示
        # 「已用 200 / 60」这种假数（真账在中继，页面上标着「本地估算」）。
        used = int(_USE_ST.get("used") or 0)
        if _USE_ST.get("plan_cap") is None:
            lim, cred, split = {"use_min_day": cap // 60}, 0, False   # 老状态：分不出来，按一整池
        else:
            plan_cap = int(_USE_ST.get("plan_cap") or 0)
            used_at = int(_USE_ST.get("used_at") or 0)
            over = max(0, used - max(plan_cap, used_at))
            cred = max(0, int(_USE_ST.get("credit") or 0) - over)
            lim, split = {"use_min_day": plan_cap // 60}, True
        # 免费版消息条数：中继确认过的已发 + 本机还没报上去的直连发送。
        sends = max(int(_USE_ST.get("sends") or 0), int(_USE_ST.get("sends_local") or 0))
        lim["sends_day"] = int(_USE_ST.get("sends_cap") or 0)
        return {"ok": False, "fallback": True, "split": split, "plan": "free",
                "srv_ts": time.time(), "limits": lim,
                "usage": {"use_min_day": used // 60, "use_credit_min": cred // 60,
                          "use_cap_eff": cap // 60,
                          "use_used_sec": used, "use_credit_sec": cred,
                          "use_cap_sec": cap, "use_unit": _use_unit(),
                          "sends_day": sends}}
    except Exception:
        return None


async def h_plan(request):
    """设置页看套餐/额度：用本机设备凭证问中继要，并带回中继地址（页面拿它跳账号中心）。

    srv_ts：读秒的锚点（服务器时钟，手机时钟不可信）。中继本身没有这个字段，
    页面在「中继直连」那条路上退回响应的 Date 头。
    中继连不上时回一份本地估算（fallback），让手机在 LAN/P2P 还通的时候继续读秒；
    两边都不通，页面就冻结（见 ext.html useActive）。"""
    await require_token(request)
    q = request.query.get("cbdbg")
    if q:
        try:
            # 带时间与机型：页面探针是排查「说完了没转写/被谁掐掉」的唯一现场，
            # 没有时间就分不清「1 秒后取消」和「20 秒后超时」，也分不清是哪台手机。
            ua = request.headers.get("User-Agent") or ""
            mm = re.search(r"Android[^;]*;\s*([^;()]+)", ua)
            if mm:
                who = mm.group(1).strip()
            elif "iPhone" in ua:
                who = "iPhone"
            elif "iPad" in ua:
                who = "iPad"
            elif "Macintosh" in ua:
                who = "Mac"
            elif "Windows" in ua:
                who = "Windows"
            else:
                who = request.remote or ""
            who = who[:18]
            _dbg("[dbg %s %s] %s" % (time.strftime("%m-%d %H:%M:%S"), who, q[:80]))
        except Exception:
            pass
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id"), rc.get("device_secret")
    out = {"ok": False, "plan": "free", "relay_base": base, "dev": dev or "",
           "error": "本机还没接入中继" if not base else "", "srv_ts": time.time()}
    if base and dev and sec:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/plan",
                                 headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                                 timeout=aiohttp.ClientTimeout(total=6)) as r:
                    d = await r.json(content_type=None)
                    if isinstance(d, dict):
                        out = d
                        out["relay_base"] = base
                        out["dev"] = dev or ""   # 本机设备号：页面据此判断"删的是不是正在看的这台"
                        out["srv_ts"] = time.time()
                        _use_cache_cap(out)
        except Exception as e:
            out["error"] = "中继连不上，稍后再试"
            print("[plan] 中继查询失败: %s" % type(e).__name__, flush=True)
            fb = _plan_local()
            if fb:
                return web.json_response(fb)
    return web.json_response(out)


async def _relay_dev_post(path, extra, timeout=20):
    """用本机设备凭证 POST 中继（设备自证接口），返回 JSON dict。"""
    import aiohttp
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return {"ok": False, "error": "本机还没接入中继"}
    body = {"dev": dev, "sec": sec}
    body.update(extra or {})
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base + path, json=body,
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                d = await r.json(content_type=None)
                return d if isinstance(d, dict) else {"ok": False, "error": "中继返回异常"}
    except Exception as e:
        print("[pay] 中继请求失败 %s: %s" % (path, type(e).__name__), flush=True)
        return {"ok": False, "error": "中继连不上，稍后再试"}


async def h_pay_create(request):
    """Mac 客户端内下单：不需要网页登录，用开发机设备凭证代表账号。"""
    await require_token(request)
    try:
        b = await request.json()
    except Exception:
        b = {}
    d = await _relay_dev_post("/api/pay_create", {
        "plan": str(b.get("plan") or "month"),
        "type": str(b.get("type") or "alipay")})
    return web.json_response(d)


async def h_pay_status(request):
    """Mac 客户端轮询付款结果（带 otn 时只看这一单）。"""
    await require_token(request)
    try:
        b = await request.json()
    except Exception:
        b = {}
    d = await _relay_dev_post("/api/pay_status", {"otn": str(b.get("otn") or "")})
    return web.json_response(d)


async def h_apk_ver(request):
    """壳自动更新用：返回 {ver,code,built,bytes,url}，没构建过就问中继要（新 host 本机没有安装包）。
    答的是「要包的那条线」的版本（?product= / cookie / 本机线），不是本机线——
    否则 agentboss 壳会拿到工头的包和指向工头的 url（2026-11-20 用户）。"""
    await require_token(request)
    p = _apk_product_for(request)
    m = _apk_meta(p)
    if m:
        return web.json_response(m)
    base = (_relay_cfg().get("url") or "").rstrip("/")
    if base and not request.headers.get("X-CB-No-Proxy"):   # 中继透传回本机时不来回打圈
        dev = (_relay_cfg().get("device_id") or "").strip()[:32]
        q = ("?dev=" + dev + "&") if dev else "?"     # product 必须带上：不带会被按 codeboss 槽答包
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                # dev：中继按灰度名单决定这台开发机下面的客户端能拿到哪个 code
                async with s.get(base + "/api/apk_ver" + q + "product=" + p,
                                 headers={"X-CB-No-Proxy": "1"},
                                 timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        d["url"] = d.get("url") or ("/%s.apk" % p)
                        if not str(d["url"]).startswith("http"):
                            d["url"] = base + d["url"]
                        return web.json_response(d)
        except Exception as e:
            print("[apk] 中继版本查询失败: %s" % str(e)[:120], flush=True)
    return web.json_response({"error": "APK 还没构建（%s 线）" % p}, status=404)


_TPL_CACHE = {}      # 资源名 -> (内容哈希, 改写后的模板)


def _tpl(name):
    """改写（/static/x.js -> ?v=哈希）只做一次：ext.html 536KB，每次请求全文正则太浪费。
    内容哈希变了（换了版本）才重算。"""
    rev = _static_rev(name)
    hit = _TPL_CACHE.get(name)
    if hit and hit[0] == rev:
        return hit[1]
    t = _static_rewrite(_static_text(name))
    _TPL_CACHE[name] = (rev, t)
    return t


def _app_page(sid, title, product=None):
    """单页应用（三面板：列表 / 控制 / 详情，横滑切换）。/ 与 /ext/<sid> 共用。

    页面里不再嵌本机目录（新建终端的目录下拉由 /api/ext_sessions 现场带回来）：
    嵌了的话每台开发机下发的 HTML 都不一样，中继就只能按设备各存一份缓存，
    切到一台久没访问的机器要重拉 611KB（aliyun 上行 ~17KB/s，等十几秒）。"""
    tpl = _tpl("ext.html")
    print("[page] %s 页面下发 sid=%s" % (time.strftime("%m-%d %H:%M:%S"), sid or "-"),
          flush=True)   # 诊断：手机到底有没有真的重新拉页面（pverCheck → location.reload）
    # 品牌位跟「客户端」产品线走：一台 host 同时服务两条线，跟着 host 自己走的话
    # codeboss 手机连到 agentboss 线的机器，页头全是老板的图（2026-11-19 用户）。
    prod = branding.valid_product(product or _host_product())
    resp = web.Response(
        text=(tpl.replace("__TITLE__", _h(title))
             .replace("__SID__", _h(sid))
             .replace("__FAVICON__", "data:image/png;base64,"
                      + brand_icons.FAVICON_B64[prod])
             .replace("__BRANDICON__", "data:image/png;base64,"
                      + brand_icons.BRAND_B64[prod])),
        content_type="text/html",
    )
    resp.enable_compression()      # 536KB 页面，手机上 gzip 后 ~1/5
    resp.set_cookie("codeboss_t", TOKEN, max_age=86400 * 30, samesite="Lax")
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


async def handle_list(request):
    await require_token(request)
    prod = _client_product_for(request)
    title = ("Agentboss（代理工头）" if prod == branding.PRODUCT_AGENTBOSS
             else "CodeBoss（口袋工头）")
    return _app_page(sid="", title=title, product=prod)


_TERM_GONE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<style>html,body{margin:0;height:100%;background:#0b0e14;color:#e6edf3;
font:14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;display:flex;
align-items:center;justify-content:center}div{text-align:center;padding:24px}
small{display:block;color:#8b949e;margin:8px 0 16px}
button{background:#1a212c;border:1px solid #2a3240;color:#c9d1d9;border-radius:8px;
padding:9px 16px;font:inherit}</style></head><body><div>终端已结束
<small>开发机重启或这个终端已经被关闭。</small>
<button onclick="parent.postMessage({cbTermGone:1},'*')">返回列表</button></div>
<script>try{parent.postMessage({cbTermGone:1},'*')}catch(e){}</script></body></html>"""


async def handle_term(request):
    sid = request.match_info["sid"]
    if sid not in SESSIONS:
        # 不再回纯 404（iframe 里就是一块白/黑屏，用户以为界面坏了）：
        # 给一页看得懂的话，并通知外层把终端浮层关掉、回到列表。
        return web.Response(text=_TERM_GONE_HTML, content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    await require_token(request)
    s = SESSIONS[sid]
    if s.ghost:
        return web.Response(text=_TERM_GONE_HTML.replace(
                                "终端已结束", "该终端随服务重启失效"),
                            content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    tpl = _tpl("term.html")
    resp = web.Response(
        text=(tpl.replace("__TITLE__", s.name)
             .replace("__SID__", sid)
             .replace("__POLL__", "1" if (request.query.get("t") or request.query.get("poll")) else "0")),
        content_type="text/html",
    )
    resp.set_cookie("codeboss_t", TOKEN, max_age=86400 * 30, samesite="Lax")
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def _split_cmd(cmd):
    """命令行 → argv。Windows 的默认 shell 路径带空格（C:\\Program Files\\Git\\bin\\bash.exe），
    不能用 str.split；posix=False 保留 Windows 路径里的反斜杠。"""
    import shlex
    try:
        return shlex.split(cmd, posix=not IS_WIN)
    except ValueError:
        return cmd.split()


async def handle_new(request):
    await require_token(request)
    data = await request.post()
    cwd = (data.get("cwd") or "").strip() or str(Path.home())
    cmd = (data.get("cmd") or "claude").strip()
    name = (data.get("name") or "").strip() or Path(cwd).name or "session"
    if not Path(cwd).is_dir():
        return web.Response(text="目录不存在: %s" % cwd, status=400)
    argv = _split_cmd(cmd)
    if argv and os.path.basename(str(argv[0])).lower() in ("claude", "claude.exe", "claude.cmd"):
        # 网页端「新建会话」也要先按掉首次启动（选界面风格/信任目录），
        # 否则新会话就卡在选择界面上（2026-11-11 DESKTOP-CMURT6R）。
        _prep_cwd(agents.get("claude"), cwd)
    sid = uuid.uuid4().hex[:8]
    try:
        SESSIONS[sid] = Session(sid, name, cwd, argv)
    except Exception as e:
        return web.Response(text="启动失败: %s" % e, status=500)
    save_registry()
    raise web.HTTPFound("/s/%s?t=%s" % (sid, TOKEN))


async def handle_kill(request):
    await require_token(request)
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if s and not s.ghost:
        s.kill()
        save_registry()
    raise web.HTTPFound("/?t=%s" % TOKEN)


async def handle_remove(request):
    """移除一个已失效/已退出的会话条目"""
    await require_token(request)
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if s and not s.alive:
        del SESSIONS[sid]
        save_registry()
    raise web.HTTPFound("/?t=%s" % TOKEN)


async def handle_respawn(request):
    """重新拉起：同一个 sid 起一个新进程（claude 会话自动接续上次对话）"""
    await require_token(request)
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if not s or s.alive:
        raise web.HTTPFound("/?t=%s" % TOKEN)
    if not Path(s.cwd).is_dir():
        return web.Response(text="目录不存在: %s" % s.cwd, status=400)
    try:
        SESSIONS[sid] = Session(sid, s.name, s.cwd, _respawn_cmd(s.cmd), s.cols, s.rows)
    except Exception as e:
        return web.Response(text="启动失败: %s" % e, status=500)
    save_registry()
    raise web.HTTPFound("/s/%s?t=%s" % (sid, TOKEN))


async def handle_ws(request):
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if not s:
        raise web.HTTPNotFound()
    if s.ghost:
        raise web.HTTPNotFound()
    t = (request.query.get("t") or request.cookies.get("codeboss_t")
         or request.cookies.get("ptyhost_t")
         # 富客户端可以直接带走头（不会进历史/Referer/访问日志）；?t= 留着兼容旧壳
         or (request.headers.get("authorization") or "").replace("Bearer", "", 1).strip()
         or (request.headers.get("x-cb-token") or "").strip() or "")
    if t not in _tokens_current():
        raise web.HTTPForbidden()
    _use_touch(request)   # 直连/本机打开终端也算一次请求（一分钟）

    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)
    s.clients.add(ws)

    # 接入即发整屏快照：客户端清屏后写入，光标状态与服务端完全对齐
    await ws.send_str(json.dumps(
        {"t": "snap", "d": s.snapshot(), "c": s.cols, "r": s.rows}))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                k = m.get("t")
                if k == "i":
                    s.write((m.get("d") or "").encode())
                elif k == "r":
                    s.resize(m.get("c", s.cols), m.get("r", s.rows))
                elif k == "q":
                    await ws.send_str(json.dumps(
                        {"t": "snap", "d": s.snapshot(), "c": s.cols, "r": s.rows}))
            elif msg.type == WSMsgType.BINARY:
                s.write(msg.data)
    finally:
        s.clients.discard(ws)
    return ws


async def h_api_sessions(request):
    await require_token(request)
    out = []
    for s in sorted(SESSIONS.values(), key=lambda x: -x.started):
        if getattr(s, "hidden", False):
            continue   # 命令运行的 PTY：只在命令终端页显示，不进 🌐 列表
        if getattr(s, "agent", None) and len(s.sid) > 8:
            continue   # 已作为「电脑会话」出现（sid 就是 claude 转录 id），别在 🌐 里列第二份

        out.append({"sid": s.sid, "kind": "web", "name": s.name, "cwd": s.cwd,
                    "alive": bool(s.alive), "ghost": bool(getattr(s, "ghost", False)),
                    "exit_code": s.exit_code, "started": s.started,
                    "last": getattr(s, "last_out", s.started), "clients": len(s.clients)})
    return _jsonz({"sessions": out})


def _sess_proj(x, seen):
    """会话 → 项目键；同一项目每次列表只处理一次，并顺手排一次仓库扫描（24h 一次）。"""
    cwd = x.get("cwd") or ""
    proj = _proj_key(cwd)
    if proj and proj not in seen:
        seen.add(proj)
        if _proj_scan_due(cwd):
            try: asyncio.create_task(_proj_scan_push(cwd, proj))
            except Exception: pass
    return proj


async def h_ext_diag(request):
    """手机端把视口数字打到 host 日志里：iOS 键盘弹起后页面位移是老壳的坑，
    光看代码猜不准（visualViewport/innerHeight 各系统版本不一样），让手机自己报。"""
    await require_token(request)
    msg = (request.query.get("m") or "")[:500]
    print("[clidiag] %s %s" % (request.headers.get("User-Agent", "")[-60:], msg), flush=True)
    return web.json_response({"ok": True})


def _link_caps(x, ad, host, meta):
    """能力声明：手机端按这个渲染，别让用户点了按钮才知道这个终端不支持。
    返回 (link, caps, why)：
      wrapped  = 走 wrapper 的 ctl/ansi，跟终端无关，全能力
      attached = 终端自己有 API（Terminal.app / iTerm2 / tmux），能力由它给
      readonly = 两者都没有，只能看转录 + 一键接管
    适配器能力位（ad.can_*）在这里并入 caps，前端数据驱动渲染。"""
    if host:
        link = "wrapped"
        caps = {"input", "keys"}
        why = ""
    elif meta is not None:
        link = "wrapped"
        caps = {"screen", "input", "keys"}
        why = ""
        if x.get("window_id"):
            caps.add("close")
    else:
        caps = set(plat.caps(x.get("tty"), x.get("window_id")))
        link = "attached" if caps else "readonly"
        # 只读就带上原因（iTerm2 没开 / 窗口关了 / Warp 这类无外部通道）：
        # 手机端只读横幅直接写出来，别让人对着「只能看」猜。
        why = "" if caps else plat.no_caps_why(x.get("tty"), x.get("window_id"))
        if x.get("desktop"):
            dcap, dwhy = _desk_caps(x)
            caps |= dcap
            why = dwhy
            if dcap:
                link = "wrapped"      # 有写入通道（exec resume），别再标只读
        if (x.get("agent") or "claude") == "claude":
            caps.add("takeover")        # 有转录就能 kill + --resume 转成 wrapper 会话
    if ad is not None and getattr(ad, "can_resume", False):
        caps.add("resume")              # 该 agent 能从盘上记录召回
    return link, caps, why


async def _ext_sessions_items(request):
    """在线会话列表（含退出会话补回）。h_ext_sessions 与 h_task_ledger（台账，
    批次二 #5）共用——台账要的就是这份列表按 sid 聚合后的生命周期。"""
    out = []
    mstate = _model_all()
    mm = _machine_model()
    # discover(force=True) 会阻塞跑 `claude agents --json` 子进程（~0.5s），
    # 且在事件循环里跑 → 每 5s 一次的列表轮询就把整个 host 冻住（页面加载慢、推送断流）。
    # 走共享 tick 缓存（0.3s 内复用），子进程挪到 executor 里，不占循环。
    seen_proj = set()
    mdflt = {}          # agent key -> 该 agent 的默认模型名（每个 agent 每次请求只算一次）
    for x in await asyncio.get_event_loop().run_in_executor(None, _tick_sessions):
        ad = agents.get(x.get("agent"))
        p = ad.transcript_path(x.get("cwd", ""), x["sid"]) if ad else None
        mtime = 0
        try:
            if p:
                mtime = p.stat().st_mtime
        except OSError:
            pass
        proj = _sess_proj(x, seen_proj)
        # 模型名：先看这个会话切过没有（登记表），再看这个 agent 自己的默认（Codex 不走 cc-switch）
        akey = x.get("agent") or agents.DEFAULT_AGENT
        if akey not in mdflt:
            mdflt[akey] = (ad.model_info() if ad else "") or mm["model"]
        mdl = mstate.get(x["sid"]) or mdflt[akey]
        host = bool(x.get("host"))          # Windows 上 host 自管 ConPTY 的会话
        meta = None if host else _meta_for_pid(x.get("pid"))
        if meta and meta.get("term"):
            _term_note(x["sid"], meta["term"])   # 关掉后召回/换模型还要回到原终端
        link, caps, why = _link_caps(x, ad, host, meta)
        out.append(dict(x, kind="computer", last=mtime or x.get("last") or 0,
                        full_color=meta is not None,
                        host=host,
                        link=link, caps=sorted(caps), why=why,
                        term="Codeboss" if host else
                             ("Codex 桌面版" if x.get("desktop") else
                              (meta or {}).get("term") or plat.term_label(x.get("tty"), x.get("window_id"))),
                        model=mdl,
                        proj=proj, agent=x.get("agent") or "claude"))
    # 已退出的电脑会话：历史（转录 jsonl）一直在盘上，只是 agents 列表里没了——
    # 从登记表补回列表显示「已退出」，可一键 --resume 召回
    for c in _closed_sessions({x["sid"] for x in out}):
        c["proj"] = _sess_proj(c, seen_proj)
        out.append(c)
    # ACP 协议源窗口（gemini 等，含重启鬼影）：行构造与上两路一致，
    # acp=1 让 source_of 判 protocol；无发现协议，窗口表+仓就是真相。
    out.extend(_acp_rows())
    # 窗口来源标注（WindowSource 抽象）：在线/已退出两路统一收口在这里，
    # 手机端据此决定徽标与 live 降级；判别逻辑全在 window_source.source_of。
    for r in out:
        r["source"] = window_source.source_of(r["sid"], r)
    return out


async def h_ext_sessions(request):
    await require_token(request)
    out = await _ext_sessions_items(request)
    # 产品线能力面：codeboss 客户端只见 coding 面（D5/C2），agentboss 全量
    out = _face_only(out, _face_for(request))
    # 顺手带回 agent 列表：页面靠它画 Claude/Codex 切换条。以前要单独发一次
    # /api/ext_agents，那一次失败（中继抖/超时）切换条就静默消失，像「tab 又没了」；
    # 列表这次一定在发，跟着它走就没有这个单点了。
    # dirs 同理（2026-11-18）：页面 HTML 不再嵌本机目录，否则整页没法跨设备共用缓存。
    return _jsonz({"sessions": out, "agents": _agents_installed(_face_product(request)),
                   # 目录下拉要 stat 候选路径（含 /Volumes 外置盘）：必须在 executor 里跑。
                   # 2026-11-26 事故：USB 盘休眠窗口里一次 is_dir() 卡进内核，
                   # 事件循环冻 20 分钟 → relay 判死（app 全离线）+ 全彩回放全停。
                   "dirs": await asyncio.get_event_loop().run_in_executor(None, recent_dirs)})


async def h_ext_page(request):
    await require_token(request)
    sid = request.match_info["sid"]
    # 会话查不到也照常渲染页面：前端会把状态显示为「已结束」，陈旧链接不再撞 404
    s = await _find_session(sid)
    title = s["title"] if s else "会话 %s…" % sid[:8]
    return _app_page(sid=sid, title=title, product=_client_product_for(request))


async def h_ext_msg(request):
    await require_token(request)
    sid = request.match_info["sid"]
    s = await _find_session(sid)
    if not s:
        return web.json_response({"total": 0, "messages": [], "state": "gone"})
    # parse_transcript 读尾部 6MB（转录大时几十 ms，磁盘忙时更久）→ 挪进 executor
    def _load():
        ad = agents.get(s.get("agent")) or agents.default()
        p = ad.transcript_path(s.get("cwd", ""), s.get("sid") or sid) if ad else None
        msgs = ad.parse_transcript(p, limit=400) if (ad and p) else []
        host = bool(s.get("host"))
        meta = None if host else _meta_for_pid(s.get("pid"))
        full = meta is not None
        if host:
            caps = {"input", "keys"}
            why = ""
        elif meta is not None:
            caps = {"screen", "input", "keys"}
            why = ""
        else:
            caps = set(plat.caps(s.get("tty"), s.get("window_id")))
            why = "" if caps else plat.no_caps_why(s.get("tty"), s.get("window_id"))
            if s.get("desktop"):
                caps, why = _desk_caps(s)
            if (s.get("agent") or "claude") == "claude":
                caps.add("takeover")
        return msgs, full, caps, why
    t0 = time.time()
    msgs, full_color, caps, why = await asyncio.get_event_loop().run_in_executor(None, _load)
    # 「点进去转半天/白屏」靠这行：慢就说明卡在读转录或 meta，不慢就是手机端没画出来
    dt = time.time() - t0
    if dt > 1.2:
        _dbg("[ext] msg 慢 %.1fs sid=%s agent=%s 条数=%d"
             % (dt, sid[:8], s.get("agent") or "claude", len(msgs)))
    return _jsonz({"total": len(msgs), "messages": msgs, "state": s["state"],
                              "full_color": full_color, "caps": sorted(caps), "why": why,
                              "desktop": bool(s.get("desktop"))})


# ---- 产物通道（批次一）：会话 cwd 内的文件，手机可列表/预览/上传 ----
# 安全边界：resolve 后必须在会话 cwd 子树内 + 符号链接一律拒收；凭据类文件
# 拒发；下载 20MB / 上传 10MB 上限；上传不执行。cookie/头只是体验差异，
# 这里的权限同样由 token（require_token）管。

class _ExtPathError(Exception):
    """路径越界 / 凭据拒发 / 超限 / 会话不存在——handler 统一答 403/404。"""


_EXT_DENY_PARTS = (".env", "token", "secret", "id_rsa", ".pem", ".key")
_EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
             ".md": "text/markdown", ".txt": "text/plain; charset=utf-8",
             ".csv": "text/csv; charset=utf-8", ".json": "application/json",
             ".stl": "model/stl",
             # 产出物（批次三）：老板收件箱里的视频/文档/压缩包，走 mediaPull 分块拉
             ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
             ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             ".zip": "application/zip", ".log": "text/plain; charset=utf-8"}
# 媒体上限抬到 100MB：单帧仍是 2MiB，靠 mediaPull 分块拉，不放大中继帧
_EXT_FILE_MAX = 100 * 1024 * 1024     # 下载上限
_EXT_UPLOAD_MAX = 10 * 1024 * 1024    # 上传上限


import concurrent.futures as _cf

# ext 簇专用 FS 池：就算卷挂死也只废这 2 个线程（TCC 解除/卷恢复后自然放行），
# PTY/会话扫描用的默认池不受牵连。
_EXT_FS = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="extfs")


async def _ext_fs_run(fn, *a, vol=None, timeout=2.0):
    """ext 簇的同步 FS 一律进专用池，限时快败。

    卷闸门挡得住已知坏卷，但探测通过≠担保（2026-11-26 第 4 次冻结：闸门放行后
    walk 的 scandir 照样挂死循环）。挂死时手机页面在等的请求必须变成干净的
    「项目盘不可用」，而不是永久转圈把整页拖住（历史消息白屏就是这么来的）。
    超时同时把该卷按坏记账 10 分钟。闸门必须在提交前查（vol=实际目录）：工人被
    挂死时排队任务永远轮不到执行，藏在任务里的闸门等于不存在。
    超时还要重建池：挂死线程永远不会返回，2 个工人被占满后整个池报废，
    连内盘任务都排队 5 秒（2026-11-26 实测 /home/dev 也 TIMEOUT——
    家目录 walk 撞上 TCC 保护的 ~/Desktop 类目录，TCC 挂点不止 /Volumes）。"""
    global _EXT_FS
    _t0 = time.time()
    if vol is not None and not _vol_ok(vol):
        raise _ExtPathError("项目盘不可用")
    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(_EXT_FS, fn, *a), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            _EXT_FS.shutdown(wait=False)   # 挂死线程随它去，不等人
        except Exception:
            pass
        _EXT_FS = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="extfs")
        if vol:
            # 按卷记坏 10 分钟：后续请求（含收件箱里的其余会话）在闸门处秒级跳过
            _VOL_PROBE[_vol_root(vol)] = time.time()
        print("[ext] fs_run 超时重建池 vol=%r %.0fms" % (vol, (time.time() - _t0) * 1000), flush=True)
        raise _ExtPathError("项目盘不可用")


def _ext_guard_path(cwd, rel):
    """cwd 子树内的真实路径，越界/符号链接/凭据名一律 _ExtPathError。

    入口过卷闸门：产物页/文件列表/源码读取全走这里，会话 cwd 在 /Volumes 下而
    卷被 TCC 弹窗挂起时，resolve/scandir/open 都会卡进内核（2026-11-26 三次整站
    冻结，第三次就是 _ext_files_payload 在循环上 scandir 项目盘）；闸门关着直接
    报「项目盘不可用」，端点变错误应答，循环不冻。"""
    if not _vol_ok(str(cwd or "")):
        raise _ExtPathError("项目盘不可用")
    try:
        root = Path(cwd).resolve()
        p = (root / rel).resolve() if rel else root
    except (OSError, ValueError):
        raise _ExtPathError("bad path")
    if not str(p).startswith(str(root) + os.sep) and p != root:
        raise _ExtPathError("越界：%s" % rel)
    if p.is_symlink():
        raise _ExtPathError("符号链接：%s" % rel)
    low = p.name.lower()
    if any(s in low for s in _EXT_DENY_PARTS) or (root / rel).name.startswith(".env"):
        raise _ExtPathError("凭据类文件不提供下载：%s" % rel)
    return p


async def _ext_files_payload(sid, sub=""):
    """会话 cwd（或其 sub 子目录）一层的文件列表，给手机产物页。"""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    if not cwd:
        raise _ExtPathError("会话不存在或没有 cwd")

    def _work():
        d = _ext_guard_path(cwd, sub)
        if not d.is_dir():
            raise _ExtPathError("不是目录")
        out = []
        for e in os.scandir(d):
            if e.name.startswith(".") or e.is_symlink():
                continue                  # 隐藏文件与链接不进列表（.boss-uploads 由 UI 单独点名）
            try:
                st = e.stat()
                out.append({"name": e.name, "size": st.st_size,
                            "mtime": int(st.st_mtime), "is_dir": e.is_dir()})
            except OSError:
                continue
        out.sort(key=lambda x: (not x["is_dir"], x["name"]))
        return {"cwd": str(d), "entries": out}

    return await _ext_fs_run(_work, vol=cwd)


_EXT_CODE_LINES = 400                 # 单次最多给多少行（手机一屏翻不了更多）
_EXT_CODE_MAX = 2 * 1024 * 1024       # 超过这个大小给「下载看」，别把会话卡住
_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".html": "html", ".css": "css", ".scss": "css", ".json": "json", ".md": "markdown",
    ".sh": "bash", ".zsh": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".m": "objectivec",
    ".rb": "ruby", ".php": "php", ".sql": "sql", ".xml": "xml", ".ini": "ini",
    ".txt": "text", ".log": "text",
}


async def _ext_code_payload(sid, path, line=1, n=None):
    """带行号的一段源码（批次三）：根 = 会话 cwd 子树，同 _ext_guard_path 的规矩。
    line 指到哪页给哪页；文件再大也只回 _EXT_CODE_LINES 行。"""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    if not cwd:
        raise _ExtPathError("会话不存在或没有 cwd")
    # 实时目录解不到再试出生目录：早于 cd 写的源码文件按出生 cwd 归位
    bases = [cwd]
    birth = _birth_cwd_of(sid, s)
    if birth:
        bases.append(birth)
    try:
        want = max(1, int(line or 1))
    except (TypeError, ValueError):
        want = 1
    try:
        take = max(1, min(int(n or _EXT_CODE_LINES), _EXT_CODE_LINES))
    except (TypeError, ValueError):
        take = _EXT_CODE_LINES

    def _work():
        nonlocal want   # 文件尾翻页要回写 clamp 后的 want（start 用它）
        p = None
        first_err = None
        for b in bases:
            try:
                cand = _ext_guard_path(b, path)
            except _ExtPathError as e:
                if first_err is None:
                    first_err = e
                continue
            if cand.is_file():
                p = cand
                break
        if p is None:
            raise first_err or _ExtPathError("不是文件：%s" % path)
        if p.stat().st_size > _EXT_CODE_MAX:
            raise _ExtPathError("超过 2MB，请下载后查看")
        text = p.read_text(encoding="utf-8", errors="replace")
        rows = text.splitlines()
        total = len(rows)
        if total and want > total:                 # 点到了文件尾：给最后一页
            want = max(1, total - take + 1)
        chunk = rows[want - 1:want - 1 + take]
        return {"path": path, "lang": _EXT_LANG.get(p.suffix.lower(), "text"),
                "total": total, "start": want,
                "lines": [{"no": want + i, "text": t} for i, t in enumerate(chunk)]}

    return await _ext_fs_run(_work, vol=cwd)


async def _ext_written_path(sid, s, path):
    """cwd 之外的文件只放行「这个会话自己在转录里写过的」绝对路径。
    工头的产出会列 worktree/记忆目录里的文件（2026-11-22）；别的一律越界。"""
    if not os.path.isabs(path):
        return None

    def _check():
        raw = Path(path)
        if raw.is_symlink():
            return None
        if any(x in raw.name.lower() for x in _EXT_DENY_PARTS):
            return None
        try:
            return raw.resolve()
        except Exception:
            return None

    want = await _ext_fs_run(_check, vol=path)
    if want is None:
        return None
    try:
        live = await _boss_live(sid, s)
    except Exception:
        return None

    def _match(live):
        for fp in (live or {}).get("files") or []:
            try:
                if Path(fp).resolve() == want:   # fp 常在 /Volumes 上，resolve 也会卡
                    return want
            except (OSError, ValueError):
                continue
        return None

    return await _ext_fs_run(_match, live, vol=path)


def _birth_cwd_of(sid, s=None):
    """会话转录头部记的出生 cwd（不随 cd 漂）。找不到/非法就空串。
    手头已有会话 dict 就传 s，别再查一遍；transcript_meta 自带 mtime 缓存，
    发现循环在用同一份，多调一次没成本。"""
    try:
        if s is None:
            s = next((x for x in _tick_sessions() if x["sid"] == sid), None)
        cwd = (s or {}).get("cwd") or ""
        ad = agents.get(s.get("agent")) or agents.default()
        tp = ad.transcript_path(cwd, sid) if ad else None
        if not tp:
            return ""
        birth, _t = ad.transcript_meta(tp)
        return birth if birth and birth != cwd and Path(birth).is_dir() else ""
    except Exception:
        return ""


async def _ext_file_meta(sid, path):
    """校验并返回 (真实路径, mime)；下载/预览 handler 拿它流式回。

    解析基座两个，都过 _ext_guard_path：①实时 cwd（agent 会 cd，晚写的相对路径
    归它）；②转录出生 cwd（头部 cwd 不随 cd 漂，早写的相对路径归它）。只按实时
    目录解的话，会话里 cd 过一次，之前消息里的文档/产物卡全都「不是文件」403
    （2026-11-25 用户实测）。"""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    if not cwd:
        raise _ExtPathError("会话不存在或没有 cwd")
    bases = [cwd]
    birth = _birth_cwd_of(sid, s)
    if birth:
        bases.append(birth)
    def _find_file():
        first_err = None
        for b in bases:
            try:
                cand = _ext_guard_path(b, path)
            except _ExtPathError as e:
                if first_err is None:
                    first_err = e
                continue
            if cand.is_file():
                return cand, None
        return None, first_err

    p, first_err = await _ext_fs_run(_find_file, vol=cwd)
    if p is None:
        p = await _ext_written_path(sid, s, path)
        if not p:
            raise first_err or _ExtPathError("不是文件")

    def _check_size():
        if not p.is_file():
            raise _ExtPathError("不是文件")
        if p.stat().st_size > _EXT_FILE_MAX:
            raise _ExtPathError("超过 %dMB 上限" % (_EXT_FILE_MAX // 1048576))
        return _EXT_MIME.get(p.suffix.lower(), "application/octet-stream")

    return p, await _ext_fs_run(_check_size, vol=str(p))


def _ext_err(e):
    """_ExtPathError → HTTP：不存在/没 cwd 答会话 404，其余 403。"""
    msg = str(e)
    status = 404 if ("不存在" in msg or "没有 cwd" in msg) else 403
    return web.json_response({"error": msg}, status=status)


_ART_SKIP = {".git", "node_modules", "dist", "__pycache__", ".venv", "venv",
             "build", ".next", "target", ".boss-uploads", ".idea", ".vscode",
             # TCC 保护目录：cwd=家目录的会话 walk 撞上就挂死（弹窗/静默拒绝），
             # 用户的产出物不会在这几个系统目录里
             "Desktop", "Documents", "Downloads", "Music", "Movies", "Pictures",
             "Library", "Public"}
_ART_MAX = 500            # 一次最多给这么多条：收件箱是给人翻的，不是全量索引
# 根 + 三层：设计文档就在 docs/superpowers/specs/x-design.md 这种第三层里。
# 以前只扫两层，agent 写了一整份设计文档，产出物却是空的（2026-11-20 用户）。
_ART_DEPTH = 4
_ART_SCAN_MAX = 20000     # 扫过的目录项上限：大仓库也不至于把一次请求拖住


def _born_at(p):
    """文件/目录的创建时间。macOS 有 st_birthtime；Linux 上 ctime 约等于创建。
    不能用 mtime：目录 mtime 每放进一个文件就刷新，产出物列表会跟着跳。"""
    try:
        st = Path(p).stat()
    except (OSError, TypeError):
        return 0.0
    return float(getattr(st, "st_birthtime", 0) or st.st_ctime or 0)


async def _ext_artifacts_payload(sid, sub="", timeout=2.0):
    """本次会话产出的文件（批次三）：mtime 晚于会话开始时间就算。

    发现出来的会话只有 sid/cwd，没有 started。回落顺序：转录文件的创建时间
    （≈会话开始）→ 目录创建时间。以前回落目录 **mtime**：每新建一个文件目录
    mtime 就刷新，在这之前的产出物全被滤掉——用户看到的就是「有时候有、
    有时候没有」。创建时间是不动的，历史产出物一直列得出来。"""
    _t0 = time.time()
    print("[ext] artifacts sid=%s enter" % sid[:8], flush=True)
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    print("[ext] artifacts sid=%s found=%s cwd=%r %.0fms" % (sid[:8], bool(s), cwd[:40], (time.time() - _t0) * 1000), flush=True)
    if not cwd:
        raise _ExtPathError("会话不存在或没有 cwd")

    def _work():
        root = _ext_guard_path(cwd, sub)
        if not root.is_dir():
            raise _ExtPathError("不是目录")
        try:
            since = float((s or {}).get("started") or 0)
        except (TypeError, ValueError):
            since = 0.0
        if since <= 0:
            ad = agents.adapter_of(sid, cwd)
            tp = ad.transcript_path(cwd, sid) if ad else None
            since = _born_at(tp) if tp else 0.0
        if since <= 0:
            since = _born_at(root)
        out, cut, scanned = [], [False], [0]

        def walk(d, depth):
            if cut[0]:
                return
            try:
                it = list(os.scandir(d))
            except OSError:
                return
            for e in it:
                if len(out) >= _ART_MAX or scanned[0] >= _ART_SCAN_MAX:
                    cut[0] = True
                    return
                scanned[0] += 1
                if e.name.startswith(".") or e.name in _ART_SKIP or e.is_symlink():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if e.is_dir():
                    if depth + 1 < _ART_DEPTH:
                        walk(Path(e.path), depth + 1)
                    continue
                if st.st_mtime >= since:
                    out.append({"name": e.name,
                                "rel": str(Path(e.path).relative_to(root)).replace(os.sep, "/"),
                                "size": st.st_size, "mtime": int(st.st_mtime),
                                "kind": _ext_kind(e.name)})

        walk(root, 0)
        out.sort(key=lambda x: -x["mtime"])
        return {"cwd": str(root), "since": int(since), "entries": out[:_ART_MAX],
                "truncated": cut[0] or len(out) > _ART_MAX}

    # 2026-11-26 第 4 次冻结定罪处：walk 的 scandir 曾直接跑在循环线程上
    return await _ext_fs_run(_work, vol=cwd, timeout=timeout)


def _ext_kind(name):
    """产出物类型：image / video / audio / model / page / doc / other（页面只按这个出图标）。"""
    e = Path(name).suffix.lower()
    if e in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return "image"
    if e in (".mp4", ".webm", ".mov"):
        return "video"
    if e in (".mp3", ".wav", ".m4a"):
        return "audio"
    if e == ".stl":          # 3D 打印模型：手机上没有应用能开，页面里自绘预览
        return "model"
    if e in (".html", ".htm"):   # 网页：在 App 里当页面渲染（/pvfile），不是贴源码
        return "page"
    if e in (".pdf", ".md", ".txt", ".csv", ".docx", ".xlsx", ".pptx", ".json", ".log"):
        return "doc"
    return "other"


async def _artifact_inbox_payload(sessions, limit=200):
    """产出物收件箱（B1）：跨会话把新产出收在一处，按 mtime 倒序。
    每条带 sid/title/agent——点开要回得到任务，预览要在对的会话里取文件。
    单个会话读失败（打不开/没 cwd）只跳过它，不许拖垮整页。

    2026-11-26 五次「白屏/转圈」的共同根因终于照出全貌：收件箱对**全部**
    会话逐个扫盘（实测 107 个 × 每个几百毫秒起 = 30 秒+），任何一个 cwd
    撞上 TCC 挂死再把 extfs 工人占满、余下全在排队白等。改为有界：
    只看最近 30 个会话 + 全程 3 秒预算，到点交卷（partial 标记），
    页面先渲染已有的，剩下的等下次刷新。"""
    out = []
    t0 = time.time()
    partial = False
    for s in sessions[:30]:
        # 剩余预算要传成单次扫描的超时：只查不传时，2.9s 开始的慢扫描照样跑满
        # 2s 池超时，总时长冲破预算（实测收件箱 5.1s）
        rem = 3.0 - (time.time() - t0)
        if rem <= 0:
            partial = True
            break
        try:
            d = await _ext_artifacts_payload(s["sid"], timeout=min(2.0, max(0.3, rem)))
        except Exception:
            continue
        for e in d.get("entries") or []:
            out.append(dict(e, sid=s["sid"], title=s.get("title") or "",
                            agent=s.get("agent") or ""))
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return out[:limit], partial


async def h_artifact_inbox(request):
    await require_token(request)
    items = await _ext_sessions_items(request)
    items = _face_only(items, _face_for(request))
    found, partial = await _artifact_inbox_payload(items)
    return _jsonz({"ok": True, "items": found, "partial": partial})


async def h_ext_files(request):
    await require_token(request)
    q = request.query
    try:
        if q.get("artifacts"):
            d = await _ext_artifacts_payload(q.get("sid") or "", q.get("sub") or "")
        else:
            d = await _ext_files_payload(q.get("sid") or "", q.get("sub") or "")
    except _ExtPathError as e:
        return _ext_err(e)
    return _jsonz(d)


async def h_ext_code(request):
    await require_token(request)
    q = request.query
    try:
        d = await _ext_code_payload(q.get("sid") or "", q.get("path") or "",
                                    q.get("line") or 1, q.get("n"))
    except _ExtPathError as e:
        return _ext_err(e)
    return _jsonz(d)


async def h_ext_artifact(request):
    await require_token(request)
    try:
        p, mime = await _ext_file_meta(request.query.get("sid") or "",
                                       request.query.get("path") or "")
    except _ExtPathError as e:
        return _ext_err(e)
    return web.FileResponse(p, headers={"Content-Type": mime})


async def _ext_art_file(sid, rel):
    """sid+rel → (真实路径, 根)。根 = 会话 cwd 子树（产出物页同款规矩：越界/凭据名拒）。
    实时 cwd 找不到再试出生 cwd（同 _ext_code_payload：早于 cd 写的文件也认），
    哪个根里真有这个文件就归哪个根——GET 和保存必须同根。"""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    bases = [x for x in [cwd, _birth_cwd_of(sid, s)] if x]

    def _work():
        first_err = None
        for b in bases:
            try:
                cand = _ext_guard_path(b, rel)
            except _ExtPathError as e:
                if first_err is None:
                    first_err = e
                continue
            if cand.is_file():
                return cand, b
        if not bases:
            raise _ExtPathError("会话不存在或没有 cwd")
        raise first_err or _ExtPathError("不是文件：%s" % rel)

    return await _ext_fs_run(_work, vol=cwd)


async def h_ext_art_text(request):
    """拉产出物全文给手机编辑（窗口核心 Phase 2）：文本+256KB 双闸在 art_edit。"""
    await require_token(request)
    try:
        p, _b = await _ext_art_file(request.query.get("sid") or "",
                                    request.query.get("rel") or "")
    except _ExtPathError as e:
        return _ext_err(e)
    return web.json_response(art_edit.load_text(p))


async def h_ext_art_edit(request):
    """保存手机编辑（窗口核心 Phase 2）：art_edit.save 落 diff/.new，
    回 ref_msg ≤120 字给会话注入（Task 4）；并发冲突 409，前端提示重开。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        d = {}
    sid = str(d.get("sid") or "")[:80]
    rel = str(d.get("rel") or "")
    try:
        p, root = await _ext_art_file(sid, rel)
    except _ExtPathError as e:
        return _ext_err(e)
    out = art_edit.save(root, rel, str(d.get("content") or ""), d.get("base_hash"))
    status = 200 if out.get("ok") else (409 if out.get("conflict") else 400)
    return web.json_response(out, status=status)


async def h_ext_upload(request):
    """拍照派活的落盘端点：multipart file → <cwd>/.boss-uploads/，服务端命名。
    只落盘不执行；喂给 agent 由用户发消息时附带路径（ext.html Task 10）。"""
    await require_token(request)
    sid = request.query.get("sid") or ""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    if not cwd:
        return web.json_response({"error": "会话不存在或没有 cwd"}, status=404)
    if not _vol_ok(cwd):   # mkdir/open 在挂死的卷上会卡进内核（2026-11-26）
        return web.json_response({"error": "项目盘不可用，稍后再试"}, status=503)
    up = Path(cwd) / ".boss-uploads"
    up.mkdir(parents=True, exist_ok=True)
    try:
        reader = await request.multipart()
        n = 0
        total = 0
        async for part in reader:
            if part.name != "file":
                continue
            ext = Path(part.filename or "").suffix.lower()
            if ext not in _EXT_MIME:
                return web.json_response({"error": "不支持的类型：%s" % (ext or "无")}, status=403)
            name = "%s-%s%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                "".join(random.choice("0123456789abcdef") for _ in range(4)), ext)
            dst = up / name
            with open(dst, "wb") as f:
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _EXT_UPLOAD_MAX:
                        f.close()
                        dst.unlink(missing_ok=True)
                        return web.json_response({"error": "超过 10MB 上限"}, status=413)
                    f.write(chunk)
            n += 1
        if not n:
            return web.json_response({"error": "没有 file 字段"}, status=400)
    except _ExtPathError as e:
        return _ext_err(e)
    except Exception as e:
        return web.json_response({"error": "上传失败：%s" % str(e)[:120]}, status=400)
    return _jsonz({"ok": True, "path": ".boss-uploads/" + name})


# 同一个「动作」可能被送达两次：直连超时（通道僵死）时手机侧会回落中继再发一遍，
# 而原来那一份其实已经到了。方向键重复执行就是「按一下走两格」、⏎ 就是「连按两次
# 回车」、发消息就是「同一句话发两遍、AI 答两遍」。cid 由手机给每次动作生成，
# 这里只执行一次。第二份到达时如果第一份还在执行，等它出结果再用（最多 3 秒）：
# 不能先回「成功」却其实没执行，也不能再按/再发一次。
_ONCE = {}                      # (kind, cid) -> {"at": 入队时刻, "ev": Event, "res": …或 None}
_ONCE_LOCK = threading.Lock()


def _once_claim(kind, cid):
    """登记执行权。None=由本请求执行；dict=同一 cid 的另一份（等它的结果）。"""
    if not cid:
        return None
    now = time.time()
    k = (kind, cid)
    with _ONCE_LOCK:
        it = _ONCE.get(k)
        if it:
            if it["res"] is None and not it["ev"].is_set() and now - it["at"] < 30:
                return it                  # 第一份还在跑
            if now - it["at"] < 300:
                return it                  # 已经有结果（或已卡死超 30 秒，也不再执行）
        if len(_ONCE) > 800:               # 兜底：只留 2 分钟内的
            for _k in [x for x, v in _ONCE.items() if now - v["at"] > 120]:
                _ONCE.pop(_k, None)
        it = {"at": now, "ev": threading.Event(), "res": None}
        _ONCE[k] = it
        return None


def _once_settle(kind, cid, res):
    if not cid:
        return
    k = (kind, cid)
    with _ONCE_LOCK:
        it = _ONCE.get(k)
        if not it:
            return
        it["res"] = res
    it["ev"].set()


async def h_ext_send(request):
    await require_token(request)
    data = await request.json()
    sid = data.get("sid")
    text = (data.get("text") or "").strip()
    cid = str(data.get("cid") or "")[:80]
    # 手机端发送排查：请求只要到达 host 就记一行（后面的早退分支不再打日志）
    print("[send] req sid=%s len=%d ip=%s" % ((sid or "-")[:8], len(text), request.remote or "-"), flush=True)
    if not text:
        return web.json_response({"ok": False, "error": "empty"})
    sid = _alias_sid(sid)
    # 直连超时后手机侧会带着同一个 cid 回中继补发一份；osascript 那条路能走十几秒，
    # 两份都执行就是「同一句话发两遍、AI 答两遍」。只执行第一份（见 _once_claim）。
    it = _once_claim("send", cid)
    if it is not None:
        # 发消息走 osascript 时可能十几秒，比按键多等一会儿：尽量回真实结果
        await asyncio.get_event_loop().run_in_executor(None, it["ev"].wait, 10.0)
        with _ONCE_LOCK:
            got = it["res"]
        out = dict(got) if got else {"ok": True, "error": ""}
        out["dup"] = 1
        print("[send] dup cid=%s sid=%s ok=%s" % (cid[:12], (sid or "-")[:8], int(bool(out.get("ok")))), flush=True)
        return web.json_response(out)
    t0 = time.time()
    aw = agent_acp.window(sid)
    if aw is not None:
        # ACP 协议源窗口：prompt 走 stdio JSON-RPC（不在 _tick_sessions 里）
        try:
            await aw.prompt(text)
            _once_settle("send", cid, {"ok": True, "error": ""})
            return web.json_response({"ok": True, "error": ""})
        except agent_acp.acp.AcpError as e:
            _once_settle("send", cid, {"ok": False, "error": str(e)})
            return web.json_response({"ok": False, "error": str(e)})
    if agent_acp.is_ghost(sid):
        # 重启后的鬼影：进程早没了，别让手机端猜——把话说破
        _once_settle("send", cid, {"ok": False, "error": "窗口已结束，请重新打开"})
        return web.json_response({"ok": False, "error": "窗口已结束，请重新打开"})
    s = next((x for x in await asyncio.get_event_loop().run_in_executor(None, _tick_sessions)
              if x["sid"] == sid), None)
    t1 = time.time()
    if not s:
        _once_settle("send", cid, {"ok": False, "error": "会话不存在或已结束"})
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    if s.get("desktop"):
        # 桌面版：优先让 Codex 守护进程自己跑这一轮——它能逐字吐增量（手机端才有
        # 「实时输出」）。线程被桌面版 App 占着写时 resume 会失败，退回队列整段送达。
        loop = asyncio.get_event_loop()
        _draft_begin(sid)
        sok, serr = await codex_daemon.start_turn(
            sid, text, lambda t: _draft_delta(sid, t),
            lambda st, t: _draft_finish(sid, st, t),
            lambda st: _draft_stat(sid, st))
        if sok:
            print("[send] desktop 流式 sid=%s" % sid[:8], flush=True)
            _once_settle("send", cid, {"ok": True, "error": ""})
            return web.json_response({"ok": True, "error": ""})
        print("[send] desktop 流式不可用（%s），回队列 sid=%s"
              % ((serr or "")[:100], sid[:8]), flush=True)
        # 自愈：App 在跑却没接上共享内核（自己又拉了一套内核、占着写入权）时，
        # 重启它再试一次。没有会话在跑才动手（见 codex_daemon.app_restart_if_idle）。
        ok2, why2 = await loop.run_in_executor(None, codex_daemon.app_restart_if_idle,
                                               serr or "")
        print("[heal] 重启桌面版：%s（%s）" % (ok2, why2), flush=True)
        if ok2:
            sok, serr = await codex_daemon.start_turn(
                sid, text, lambda t: _draft_delta(sid, t),
                lambda st, t: _draft_finish(sid, st, t),
                lambda st: _draft_stat(sid, st))
            if sok:
                print("[send] desktop 流式（重启 App 后）sid=%s" % sid[:8], flush=True)
                _once_settle("send", cid, {"ok": True, "error": ""})
                return web.json_response({"ok": True, "error": ""})
            print("[send] 重启后仍不可用（%s）" % (serr or "")[:100], flush=True)
        # 桌面版：没有终端可写，走 codex exec resume 把话送进同一个 thread
        ok, msg, note = await loop.run_in_executor(None, agent_codex.desktop_send,
                                                   s.get("cwd") or "", sid, text)
        print("[send] desktop sid=%s ok=%s %s" % (sid[:8], ok, ("" if ok else msg)), flush=True)
        if ok:
            # App 来跑这一轮：只落成品，先给手机端一个「正在处理」占位（见 _DRAFTS）
            _draft_begin(sid, waiting=True)
        else:
            _DRAFTS.pop(sid, None)
        _once_settle("send", cid, {"ok": ok, "error": "" if ok else msg, "note": note})
        return web.json_response({"ok": ok, "error": "" if ok else msg, "note": note})
    ok, msg = await _send_verified(s, text)
    print("[send] sid=%s tty=%s ok=%s %s | tick=%.0fms write=%.0fms%s" % (
          sid[:8], (s.get("tty") or "-")[:12], ok, ("" if ok else (msg or "")[:80]),
          (t1 - t0) * 1000, (time.time() - t1) * 1000,
          "" if ok else " | " + _sess_diag(s)), flush=True)   # 手机端「发送慢/失败」排查用
    _once_settle("send", cid, {"ok": ok, "error": "" if ok else msg})
    return web.json_response({"ok": ok, "error": "" if ok else msg})


async def h_ext_write(request):
    """电脑上的会话窗口敲键用：原样写进 host 托管的会话，不加回车。
    手机端发送/按键走 ext_send/ext_key（它们要处理终端窗口、核实提交），
    这里只服务 ptywin.py 那个本地窗口。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = _alias_sid(str(data.get("sid") or ""))
    text = str(data.get("data") or "")
    hs = SESSIONS.get(sid)
    if not isinstance(hs, Session) or not hs.alive:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    if text:
        hs.write(text.encode("utf-8"))
    return web.json_response({"ok": True})


async def h_ext_key(request):
    await require_token(request)
    data = await request.json()
    sid = _alias_sid(data.get("sid"))
    key = data.get("key") or ""
    cid = str(data.get("cid") or "")[:80]
    loop = asyncio.get_event_loop()
    # discover + osascript 都是阻塞的（子进程 / 脚本可能几秒），挪出事件循环，
    # 否则按一次键就把整个 host 冻住：其他连接断流、按键排队、中继心跳饿死。
    def _go():
        t0 = time.time()
        it = _once_claim("key", cid)
        if it is not None:
            it["ev"].wait(3.0)             # 重复送达：拿第一份的真实结果
            with _ONCE_LOCK:
                got = it["res"]
            if got is not None:
                return got[0], got[1], "dup", t0, None
            return True, "", "dup", t0, None   # 第一份还没跑完：绝不再按一次
        s = next((x for x in _tick_sessions() if x["sid"] == sid), None)
        if not s:
            # 缓存刚好没刷到（刚打开/刚 --resume 的会话）：强制刷新一次再找，
            # 否则刚开的会话按半天都是「会话不存在」，看着就是按键没反应。
            s = next((x for x in _tick_sessions(force=True) if x["sid"] == sid), None)
        if not s:
            _once_settle("key", cid, (False, "会话不存在或已结束", "none"))
            return False, "会话不存在或已结束", "none", t0, None
        if s.get("host"):
            meta = None                 # host 自管 ConPTY：没有 wrapper meta，字节直接写 Session
            via = "host"
        else:
            meta = _meta_for_pid(s.get("pid"))
            if not meta:
                # meta 缓存是每秒刷的只读快照，慢盘（Time Machine）/刚写出的 meta 都可能
                # 没刷到。这里读一次盘：不然这个会话的按键会整段退化成 osascript 注入，
                # 表现就是「方向键/回车按了没反应」。
                try:
                    _ansi_metas_refresh()
                except Exception:
                    pass
                meta = _meta_for_pid(s.get("pid"))
                if not meta:
                    m2 = _newest_meta_for_sid(sid)
                    if m2 and m2.get("sid") == sid and not m2.get("exited"):
                        meta = m2
            via = "ctl" if ((meta or {}).get("ctl") and os.path.exists(meta["ctl"])) else "plat"
            if via == "plat":
                # 走终端 API 时记清是哪个终端：同一句「没反应」，Terminal/iTerm/tmux 排查方法完全不同
                via = "term:%s" % (plat.term_label(s.get("tty"), s.get("window_id")) or "none")
        try:
            ok, msg = ext_send_special(s, key, meta)
        except Exception:
            _once_settle("key", cid, (False, "按键处理出错", "none"))
            raise
        _once_settle("key", cid, (ok, msg, via))
        return ok, msg, via, t0, s
    ok, msg, via, t0, s = await loop.run_in_executor(None, _go)
    # 按键出问题只能靠这一行定位（哪台机、哪个会话、走没走控制通道、多久）：
    # 手机上只看到「没反应」，开发机日志里得能对得上。
    print("[key] sid=%s key=%s ok=%s via=%s pid=%s ms=%d %s%s"
          % (str(sid)[:8], key, int(bool(ok)), via, (s or {}).get("pid"),
             int((time.time() - t0) * 1000), "" if ok else str(msg)[:120],
             "" if ok else " | " + _sess_diag(s)), flush=True)
    out = {"ok": ok, "error": "" if ok else msg}
    if ok and via == "plat" and plat.IS_MAC:
        # macOS 上没有 wrapper 控制通道的会话只能靠 osascript 注入：要辅助功能权限、
        # 会抢焦点，锁屏/无人在电脑前时会被静默丢弃——「有时候按了没反应」多半是它。
        # Linux 的 plat 路是 tmux send-keys，可靠，不提示。
        out["warn"] = "这个会话没有控制通道（不是从 App 列表里拉起的），按键可能没生效；建议转全彩"
    return web.json_response(out)


_ANSI_META_CACHE = {"at": 0.0, "data": []}

_LIVE_NOMETA_LOGGED = set()   # 「没有全彩镜像」已经打过日志的 sid（页面轮询会重复打）


def _ansi_metas_refresh():
    metas = []
    if ANSI_DIR.exists():
        for m in ANSI_DIR.glob("*.meta.json"):
            try:
                d = json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("pid") and d.get("log") and not d.get("exited"):
                lg = Path(d["log"])
                if not lg.exists():
                    cur = ANSI_DIR / Path(d["log"]).name
                    if cur.exists():
                        d["log"] = str(cur)   # 安装目录改名/迁移后，meta 里的旧绝对路径兜底到当前目录
                metas.append(d)
    _ANSI_META_CACHE.update(at=time.time(), data=metas)
    return metas


def _ansi_metas_loop():
    """后台线程刷缓存：96 个 meta 全量 read_text，Time Machine 打满磁盘时单次能到秒级，
    绝不能落在事件循环上（2026-11-04 faulthandler 抓到就是这里整站卡）。"""
    while True:
        try:
            _ansi_metas_refresh()
            _ppid_refresh()
        except Exception:
            pass
        time.sleep(1.0)


def _ansi_metas():
    """只读缓存，永不碰磁盘（由 _ansi_metas_loop 每秒刷新）。"""
    return _ANSI_META_CACHE["data"]


_PPID_CACHE = {"m": {}}    # pid -> ppid，后台线程每秒刷一次


def _ppid_refresh():
    """全机 pid->ppid 表。以前 _meta_for_pid 沿着父链一条条跑 ps（每个会话最多 3 次），
    手机进会话时那份会话列表在事件循环里一跑就是 7 秒（2026-11-11 客户机抓栈），
    整站冻住 =「会话打不开」。ps 只该在后台线程里跑。"""
    if IS_WIN:
        return
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    m = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                m[int(p[0])] = int(p[1])
            except ValueError:
                pass
    _PPID_CACHE["m"] = m


def _meta_for_pid(pid):
    if not pid:
        return None
    metas = _ansi_metas()
    m = next((m for m in metas if m.get("pid") == pid), None)
    if m:
        return m
    # codex 这类经 node shim 启动的 agent：wrapper 直接子进程是 shim，真正跑 TUI 的是它
    # 再往下 fork 的二进制（ps 里认出来的是后者）。沿父进程链找两三级，别让全彩镜像
    # 因为隔了一层 shim 就「无窗口 / 没内容」。
    pp = _PPID_CACHE["m"]
    if not pp:
        return None
    try:
        cur = int(pid)
        for _ in range(3):
            cur = pp.get(cur, 0)
            if cur <= 1:
                break
            m = next((x for x in metas if x.get("pid") == cur), None)
            if m:
                return m
    except Exception:
        return None
    return None


def _proc_chain(pid, depth=4):
    """父进程链（最多 4 级）：「zsh<Warp」——一眼看出这个会话挂在哪个终端下。
    终端窗口关了但 claude 还活着，链上就只剩 zsh<launchd。非 mac/linux 返回 ?。"""
    if IS_WIN or not pid:
        return "?"
    out = []
    cur = int(pid)
    for _ in range(depth):
        try:
            r = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(cur)],
                               capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            break
        parts = r.split(None, 1)
        if len(parts) < 2:
            break
        try:
            cur = int(parts[0])
        except ValueError:
            break
        out.append(parts[1].rsplit("/", 1)[-1][:16])
        if cur <= 1:
            break
    return "<".join(out) or "?"


def _sess_diag(s):
    """一行定位「这个会话为什么发不进 / 看不到」：进程还在不在、挂在哪个终端下、
    会话 tty 还在不在、有没有全彩镜像（没有 = 只能走 osascript，Warp/iTerm 根本没有
    AppleScript 窗口可找，报出来就是「终端窗口可能已关闭」）。
    只在打开实时视图 / 发送 / 按键失败时打——会话列表 5s 刷一次，打在列表里就是刷屏。"""
    s = s or {}
    pid = int(s.get("pid") or 0)
    chain = _proc_chain(pid)
    alive = "?" if IS_WIN else (0 if not pid else int(not _proc_gone(pid)))
    tty = (s.get("tty") or "").replace("/dev/", "")
    tty_on = int(bool(tty) and (IS_WIN or os.path.exists("/dev/" + tty)))
    meta = _meta_for_pid(pid)
    if meta:
        ago = -1
        try:
            ago = int(time.time() - os.stat(meta.get("log") or "").st_mtime)
        except OSError:
            pass
        ch = "full(ctl=%d,输出%ss前)" % (int(os.path.exists(meta.get("ctl") or "")), ago)
    else:
        ch = "gray"
    return "pid=%d alive=%s 父链=%s tty=%s 在=%d 通道=%s" % (
        pid, alive, chain, tty or "-", tty_on, ch)


async def h_ext_open(request):
    """在新终端里经 wrapper 启动一个 agent（输出 tee 出来给手机端全彩镜像）。
    macOS 是 Terminal.app 的新窗口，Linux 是新的 tmux 会话。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    cwd = (data.get("cwd") or "").strip() or str(Path.home())
    if not Path(cwd).is_dir():
        return web.json_response({"ok": False, "error": "目录不存在: %s" % cwd})
    ad, aerr = _pick_agent(data)
    if not ad:
        return web.json_response({"ok": False, "error": aerr})
    if getattr(ad, "protocol", False):
        # 协议源（ACP 等）：不开终端，host 直接 stdio 连 agent——PTY 包一个
        # 无头 JSON-RPC 进程只会得到黑窗。开窗/发消息/关闭全在 host 侧收口。
        try:
            w = await agent_acp.open_window(cwd)
        except agent_acp.acp.AcpError as e:
            return web.json_response({"ok": False, "error": "起不来：%s" % e})
        except Exception as e:                      # spawn 失败（命令不存在等）
            return web.json_response({"ok": False, "error": "起不来：%s" % e})
        print("[ext] open acp %s cwd=%s" % (w.sid[:12], cwd), flush=True)
        return web.json_response({"ok": True, "sid": w.sid})
    cmd = ad.launch_argv(cwd) if ad else None
    if not cmd:
        return web.json_response({"ok": False,
                                  "error": "这台电脑上没装 %s"
                                           % ((ad.label or ad.key) if ad else "这个 agent")})
    argv = [sys.executable, str(WRAPPER), "--"] + list(cmd)
    _wid, _tty, err = await asyncio.get_event_loop().run_in_executor(
        None, lambda: plat.open_wrapped(cwd, argv))
    if err:
        return web.json_response({"ok": False, "error": err[:200]})
    if _tty and not plat.IS_MAC:
        # 无头机器上没人能点「信任这个文件夹」——后台盯着，弹出来就替用户确认
        asyncio.ensure_future(_auto_trust_watch(_tty, _wid))
    return web.json_response({"ok": True})


# 手机看电脑上的图片/视频：只认媒体后缀，且必须落在会话目录 / 临时目录 / 几个
# 用户目录里。别的一律不发——token.txt、密钥、数据库这些绝对不能顺路被读走。
_MEDIA_CT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".heic": "image/heic",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
}
_MEDIA_MAX = 200 * 1024 * 1024


def _media_roots(sid):
    """允许发出去的目录：该会话的 cwd + 临时目录 + 常见产出目录。"""
    rs = [Path("/tmp"), Path("/private/tmp"), Path.home() / "Documents",
          Path.home() / "Movies", Path.home() / "Downloads", Path.home() / "Desktop"]
    try:
        s = next((x for x in _tick_sessions() if x["sid"] == sid), None)
        cwd = (s or {}).get("cwd") or ""
        if cwd:
            rs.append(Path(cwd))
        birth = _birth_cwd_of(sid)     # cd 漂移后，早先产出的媒体按出生目录放行
        if birth:
            rs.append(Path(birth))
    except Exception:
        pass
    out = []
    for r in rs:
        try:
            out.append(str(Path(r).resolve()))
        except Exception:
            pass
    return out


def _sess_cwd_of(sid):
    """会话的工作目录（相对路径按它解析）。找不到就空串。"""
    try:
        s = next((x for x in _tick_sessions() if x["sid"] == sid), None)
        return (s or {}).get("cwd") or ""
    except Exception:
        return ""


_MCACHE = Path.home() / ".codeboss" / "mediacache"


def _ffprobe_vcodec(fp, ff):
    pb = str(Path(ff).with_name("ffprobe"))
    if not os.path.exists(pb):
        pb = shutil.which("ffprobe") or ""
    if not pb:
        return ""
    try:
        p = subprocess.run([pb, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", fp],
                           capture_output=True, timeout=20)
        return p.stdout.decode("utf-8", "replace").strip().splitlines()[0].strip() if p.stdout else ""
    except Exception:
        return ""


def _media_web(fp):
    """手机能不能直接解：Mac 录屏多是 HEVC，安卓/Chrome 只出播放器不出画面（音频照有）。
    不是 H.264 就转一份 H.264，按「路径+大小+mtime」缓存，同一个文件只转一次。
    HEIC 同理（sips 转 jpg）。返回 (要发的路径, content-type)。"""
    ext = os.path.splitext(fp)[1].lower()
    if ext == ".heic":
        try:
            _MCACHE.mkdir(parents=True, exist_ok=True)
            st = os.stat(fp)
            key = hashlib.sha1(("%s|%d|%d" % (fp, st.st_size, int(st.st_mtime))).encode()).hexdigest()[:20]
            out = _MCACHE / (key + ".jpg")
            if not out.exists():
                tmp = _MCACHE / (key + ".part.jpg")
                subprocess.run(["sips", "-s", "format", "jpeg", fp, "--out", str(tmp)],
                               capture_output=True, timeout=60)
                os.replace(tmp, out)
            return str(out), "image/jpeg"
        except Exception as e:
            print("[media] HEIC 转换失败 %s: %s" % (os.path.basename(fp), str(e)[:100]), flush=True)
            return fp, _MEDIA_CT.get(ext, "application/octet-stream")
    if ext not in (".mp4", ".m4v", ".mov", ".webm"):
        return fp, _MEDIA_CT.get(ext, "application/octet-stream")
    ff = _ffmpeg()
    if not ff:
        return fp, _MEDIA_CT.get(ext, "video/mp4")
    if _ffprobe_vcodec(fp, ff) in ("h264", "vp8", "vp9", "av1", ""):
        return fp, _MEDIA_CT.get(ext, "video/mp4")
    try:
        _MCACHE.mkdir(parents=True, exist_ok=True)
        st = os.stat(fp)
        key = hashlib.sha1(("%s|%d|%d" % (fp, st.st_size, int(st.st_mtime))).encode()).hexdigest()[:20]
        out = _MCACHE / (key + ".mp4")
        if not out.exists() or out.stat().st_size == 0:
            print("[media] 转码 H.264：%s" % os.path.basename(fp), flush=True)
            tmp = _MCACHE / (key + ".part.mp4")
            p = subprocess.run([ff, "-y", "-loglevel", "error", "-i", fp,
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                "-movflags", "+faststart", str(tmp)],
                               capture_output=True, timeout=600)
            if p.returncode != 0 or not tmp.exists():
                raise RuntimeError(p.stderr.decode("utf-8", "replace")[:120])
            os.replace(tmp, out)
        return str(out), "video/mp4"
    except Exception as e:
        print("[media] 转码失败 %s: %s" % (os.path.basename(fp), str(e)[:120]), flush=True)
        return fp, _MEDIA_CT.get(ext, "video/mp4")


async def h_ext_merr(request):
    """手机端媒体打不开时上报（页面 mediaErr 的 beacon）：错误码 + src + 页面。
    不进鉴权——这条只为留痕，鉴权失败反而看不到现场；内容截断，别让人往里灌日志。"""
    def _t(k, n=180):
        return (request.query.get(k) or "").replace("\n", " ").replace("\r", " ")[:n]
    print("[media-err] b=%s e=%s m=%s s=%s u=%s ip=%s" % (
        _t("b", 24), _t("e", 8), _t("m", 12), _t("s"), _t("u", 120),
        request.remote or "-"), flush=True)
    return web.Response(status=204)


async def h_track(request):
    """局域网直连时页面把埋点发到这里（页面源是 host，不在中继同源），
    再用本机设备凭证转给中继 /r/dev_track。丢线程池里发，不许卡住页面请求；
    发不出去就算了——埋点不能影响功能。"""
    try:
        d = await request.json()
    except Exception:
        d = {}
    e = d.get("e") if isinstance(d, dict) else None
    if not isinstance(e, list) or not e:
        return web.json_response({"ok": False})
    body = {"e": e[:50],
            "ua": (request.headers.get("User-Agent") or "")[:300],
            "product": (request.cookies.get("cb_product") or "")[:20]}
    try:
        asyncio.get_running_loop().run_in_executor(
            None, lambda: _relay_dev_call("/r/dev_track", body, timeout=8))
    except Exception:
        pass
    return web.json_response({"ok": True})


async def h_ext_file(request):
    """GET /api/ext_file?p=<绝对路径>（+ &sid=）——把电脑上的图片/视频/音频原样发给手机。
    支持 Range（视频能拖进度条）。只读，走 P2P 直连也行。"""
    await require_token(request)
    raw = (request.query.get("p") or "").strip()
    if not raw or len(raw) > 1024:
        return web.json_response({"ok": False, "error": "路径不对"}, status=400)
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        # 产出物/文件列表给的是相对会话 cwd 的路径；按 host 进程 cwd 解析会 404
        sid_q = request.query.get("sid") or ""
        base = _sess_cwd_of(sid_q)
        rel = p
        if base:
            p = os.path.join(base, rel)
        if not os.path.isfile(p):
            birth = _birth_cwd_of(sid_q)   # cd 漂移：早写的相对路径归出生目录
            if birth:
                p = os.path.join(birth, rel)
    ct = _MEDIA_CT.get(os.path.splitext(p)[1].lower())
    if not ct:
        return web.json_response({"ok": False, "error": "只支持图片/视频/音频"}, status=403)
    try:
        fp = os.path.realpath(p)
        size = os.path.getsize(fp)
    except OSError:
        return web.json_response({"ok": False, "error": "文件不存在"}, status=404)
    if not os.path.isfile(fp) or size <= 0:
        return web.json_response({"ok": False, "error": "文件不存在"}, status=404)
    if size > _MEDIA_MAX:
        return web.json_response({"ok": False, "error": "文件太大（>200MB）"}, status=413)
    if not any(fp == r or fp.startswith(r + os.sep)
               for r in _media_roots(request.query.get("sid") or "")):
        return web.json_response({"ok": False, "error": "这个路径不允许读取"}, status=403)
    # 编码不兼容的先转一份（HEVC → H.264、HEIC → JPEG），转码缓存靠 mtime，只做一次
    try:
        fp, ct = await asyncio.get_event_loop().run_in_executor(None, _media_web, fp)
        size = os.path.getsize(fp)
    except OSError:
        return web.json_response({"ok": False, "error": "文件读不出来"}, status=404)
    rng = (request.headers.get("Range") or "").strip()
    start, end = 0, size - 1
    if rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        try:
            if a:
                start, end = int(a), (int(b) if b else size - 1)
            elif b:
                start, end = max(0, size - int(b)), size - 1
        except ValueError:
            pass
        start, end = max(0, min(start, size - 1)), max(0, min(end, size - 1))
        if end < start:
            start, end = 0, size - 1
    part = bool(rng) and (start != 0 or end != size - 1)
    # 同一路径的文件会被重新生成（视频重做覆盖同名）：ETag 用 大小+mtime，变了就换；
    # no-cache 而不是 no-store——没变时只回一句 304，Range 拖动不用重下整个文件
    hdr = {"Content-Type": ct, "Accept-Ranges": "bytes",
           "Cache-Control": "private, no-cache",
           "ETag": '"%x-%x"' % (size, int(os.path.getmtime(fp)))}
    if not rng and (request.headers.get("If-None-Match") or "").strip() == hdr["ETag"]:
        return web.Response(status=304, headers={"ETag": hdr["ETag"],
                                                 "Cache-Control": hdr["Cache-Control"]})
    if part:
        hdr["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    resp = web.StreamResponse(status=206 if part else 200, headers=hdr)
    resp.content_length = end - start + 1
    await resp.prepare(request)
    sent = 0
    try:
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(256 * 1024, left))
                if not buf:
                    break
                await resp.write(buf)
                sent += len(buf)
                left -= len(buf)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    try:
        await resp.write_eof()
    except Exception:
        pass
    print("[media] %s %dB -> %d/%d %s range=%s ip=%s"
          % (os.path.basename(fp), size, sent, end - start + 1, ct, rng or "-",
             request.remote or "-"), flush=True)
    return resp


async def h_ext_stream(request):
    """全彩镜像数据源：wrapper tee 的 ANSI 原始字节流。off<0 表示从尾部取"""
    await require_token(request)
    sid = request.match_info["sid"]
    s = await _find_session(sid)
    if not s:
        return web.json_response({"error": "会话不存在"}, status=404)
    meta = _meta_for_pid(s.get("pid"))
    if not meta:
        return web.json_response(
            {"error": "该会话未接入全彩镜像（需从列表页「在电脑终端打开」启动）"}, status=404)
    try:
        off = int(request.query.get("off", "0"))
    except ValueError:
        off = 0
    log = Path(meta["log"])
    def _go():
        try:
            size = log.stat().st_size
        except OSError:
            return None
        start = max(0, size + off) if off < 0 else min(off, size)
        data = b""
        if start < size:
            with open(log, "rb") as f:
                f.seek(start)
                data = f.read(1 << 20)
        ws = _tty_winsize(s.get("pid"))
        mst = _mirror_feed(sid, meta)
        return (data, size,
                "%d,%d" % (int(meta.get("cols") or (ws[0] if ws else 0) or 80),
                           int(meta.get("rows") or (ws[1] if ws else 0) or 24)),
                len(mst.get("hist") or []) if mst else 0)
    got = await asyncio.get_event_loop().run_in_executor(None, _go)
    if got is None:
        return web.json_response({"error": "日志不存在"}, status=404)
    data, size, winsize, nhist = got
    resp = web.Response(body=data)
    resp.headers["X-Log-Size"] = str(size)
    resp.headers["X-Winsize"] = winsize
    resp.headers["X-Hist-Count"] = str(nhist)
    return resp


# ---------------- 手机端重排实时视图 ----------------
# 用 pyte 维护权威屏幕（字节流从 0 完整回放，状态永远和 claude 的 TUI 对齐），
# 再把屏幕内容重排成适配手机的 HTML：正文自动换行、表格/代码块横向滚动、保留颜色。
# 原始字节流只留给「原样」视图（xterm 像素级镜像）。

_MIRROR = {}    # sid -> {"scr","strm","off","win","t"}
# 全局一把锁：feed/render 在 executor 线程里跑，屏幕状态要串行。试过「每个会话
# 一把锁」——几台手机同时开不同会话时几个 pyte 回放抢 GIL，实测每个从 1.1s 变 3.4s，
# 连事件循环都卡 2.7s（faulthandler 抓到全在 pyte）。串行反而稳：先来的 1.1s 出结果，
# 后面的在 worker 线程里排队，不拖累别人。请求放大由 h_ext_live 的合并 + 页面
# in-flight 闸挡住，不会堆成十几份。
_MIRROR_LOCK = threading.Lock()
_LIVE_INFLIGHT = {}   # sid -> asyncio.Future：同一会话的 ext_live 渲染合并成一份
MIRROR_HIST = 400
# 冷启动最多回放日志尾部这么多字节（见 _mirror_tail_start）：
# 屏幕只有几十行，前面几十兆历史只是用来算 scrollback，不值得把 host 卡住一分钟。
# 实测 pyte 约 1MB/s（纯 Python）；2MB=2.2s，手机第一次打开会话就干等这么久。
# 1MB 实测仍有 340~680 行 scrollback（远多于 MIRROR_HIST=400 的显示上限），
# 成本减半——「进会话半天不出来」主要就是这一刀。
MIRROR_TAIL_BYTES = 1024 * 1024
_MIRROR_REDRAW_AT = {}   # sid -> 上次补发 ⌃L 重绘的时间（尾部回放后的自愈，30s 节流）


# ⏺/● = claude 的工具调用行（TUI 用 ⏺，_runs_html 会换成 ●）。必须在标记里：
# 否则「… +19 lines (ctrl+o to expand)」这类缩进行会把下一行工具调用并进代码块，
# 实时视图里该换行的地方不换行，两行粘成一行。


def _tty_winsize(pid, ttl=15.0):
    """读会话真实终端尺寸（老 wrapper 的 meta 没记 cols/rows 时兜底）"""
    if not pid or IS_WIN:
        return None
    now = time.time()
    c = _TTY_SIZE_CACHE.get(pid)
    if c and now - c[0] < ttl:
        return c[1]
    size = None
    try:
        import fcntl
        import termios
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out and out != "??":
            fd = os.open("/dev/" + out, os.O_RDONLY | os.O_NOCTTY)
            try:
                r, co = struct.unpack("HH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)[:4])
                if co and r:
                    size = (int(co), int(r))
            finally:
                os.close(fd)
    except Exception:
        size = None
    _TTY_SIZE_CACHE[pid] = (now, size)
    return size


_TTY_SIZE_CACHE = {}


def _mirror_tail_start(log, size, tail=None):
    """日志很大时冷启动只从尾部回放。

    权威屏幕只有几十行，pyte 重建当前画面不需要把 60MB 历史全喂一遍——实测一个
    66MB 的长会话从头回放要 81 秒，全程占着这把锁，手机每秒一次的轮询全堵在
    后面，整个 host 的 executor 池被抽干，页面就「提醒出来了、内容不出来」。
    从 ESC 上切入（TUI 的绘制都以转义序列开头），免得从半个序列/半个汉字里开始。"""
    if tail is None:
        tail = MIRROR_TAIL_BYTES
    if size <= tail or tail <= 0:
        return 0
    base = size - tail
    try:
        with open(log, "rb") as f:
            f.seek(base)
            head = f.read(262144)
    except OSError:
        return base
    i = head.find(b"\x1b")
    return base + (i if i >= 0 else 0)


def _mirror_get(sid, cols, rows, rebuild=False, log=None, size=0):
    st = None if rebuild else _MIRROR.get(sid)
    lp = str(log) if log is not None else None
    # 换了日志文件（世代重生/续命搬家）必须重冷启：旧 off 位点对着新文件继续
    # 「增量」，会把整份合并历史从头喂一遍（61MB 喂 50 秒，轮询全堵死）
    if st and st["win"] == (cols, rows) and st.get("log") == lp:
        return st
    scr = pyte.HistoryScreen(cols, rows, history=MIRROR_HIST)
    start = 0
    if log is not None and size:
        start = _mirror_tail_start(log, size)
    # 重建时播种持久化的滚动历史：host 重启 / 缓存逐出后，比日志尾部回放更老的
    # 行（乃至日志被 GC 后的全部历史）从这里回来。日志重放的新提交走 _diff_commit
    # 的种子前缀去重（40 行滑窗只够小重建；长文日志的种子远超窗口，见
    # test_rebuild_prose_log_no_seed_duplicates）。
    try:
        seed = scrollback.load(sid)
    except Exception:
        seed = []
    st = {"scr": scr, "strm": _CleanStream(scr), "off": start,
          # start>0 = 尾部回放，屏幕可能缺静态行/错位：喂第一帧时补一次 ⌃L 全量重绘
          "cut": start > 0,
          "log": lp,
          "win": (cols, rows), "t": time.time(),
          "sid": sid, "hist": seed,
          # 播种行数：_diff_commit 拿它把重放提交对整个种子前缀去重（截断时递减）
          "_sb_seed_n": len(seed),
          # 持久化防抖账本：_sb_new = 未落盘行数，_sb_at = 上次落盘时刻
          "_sb_new": 0, "_sb_at": time.time(),
          # 连续性游标：累计入账 hist 的行数。手机侧拿它判「服务器重启过没有」
          # （回连后 seq 变小 = 历史重建过，整窗重画，别在缺口上硬接）
          "seq": 0}
    _MIRROR[sid] = st
    if len(_MIRROR) > 12:  # 只缓存最近看的会话
        try:
            for k in sorted(_MIRROR, key=lambda k: _MIRROR[k]["t"])[:-12]:
                if k != sid:
                    _MIRROR.pop(k, None)   # 防御：别因为并发删除抛 KeyError
        except KeyError:
            pass
    return st


def _safe_cut(b):
    """返回可安全送入仿真器的字节数：把尾部残缺的转义序列/多字节 UTF-8 截下留到下一轮"""
    n = len(b)
    cut = n
    i = n - 1
    cont = 0
    while i >= 0 and (b[i] & 0xC0) == 0x80:
        cont += 1
        i -= 1
    if i >= 0 and (b[i] & 0xC0) == 0xC0:
        lead = b[i]
        need = 2 if lead & 0xE0 == 0xC0 else 3 if lead & 0xF0 == 0xE0 else 4 if lead & 0xF8 == 0xF0 else 1
        if cont + 1 < need:
            cut = i
    j = b.rfind(0x1B)
    if j >= 0 and j < cut:
        seq = b[j:cut]
        if len(seq) >= 2 and seq[1:2] == b"[":
            k = 2
            while k < len(seq) and 0x20 <= seq[k] <= 0x3F:
                k += 1
            done = k < len(seq) and 0x40 <= seq[k] <= 0x7E
        elif len(seq) >= 2 and seq[1:2] == b"]":
            done = b"\x07" in seq[2:] or b"\x1b\\" in seq[2:]
        else:
            done = len(seq) >= 2
        if not done:
            cut = j
    return cut


def _mirror_feed(sid, meta):
    with _MIRROR_LOCK:
        return _mirror_feed_inner(sid, meta)


_SGR_RE = re.compile(rb"\x1b\[([0-9;]*)m")


def _dim_to_gray(b):
    """SGR 2（dim 暗淡）→ 显式灰字；SGR 22（去 dim）顺手补个前景重置。
    pyte 不认 dim（属性直接丢），"Worked for 12s"、press-up 提示这类暗淡行
    在手机上就成了全亮。重写成灰色前景，原本「比正文暗一档」的样式就回来了。
    22 只重置粗体/暗淡、不动前景——不补 39 的话灰色会粘到后面的正文上。"""
    def sub(m):
        parts = m.group(1).split(b";")
        out = []
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in (b"38", b"48", b"58"):
                step = 5 if (i + 1 < len(parts) and parts[i + 1] == b"2") \
                    else 3 if (i + 1 < len(parts) and parts[i + 1] == b"5") else 1
                out.extend(parts[i:i + step])
                i += step
            elif p == b"2":
                out.append(b"38;2;139;148;158")
                i += 1
            elif p == b"22":
                out.extend((b"22", b"39"))
                i += 1
            else:
                out.append(p)
                i += 1
        return b"\x1b[" + b";".join(out) + b"m"
    return _SGR_RE.sub(sub, b)


def _mirror_feed_inner(sid, meta):
    """把日志新增字节喂进权威屏幕；返回屏幕状态或 None"""
    cols = int(meta.get("cols") or 0)
    rows = int(meta.get("rows") or 0)
    if not (cols and rows):
        probed = _tty_winsize(meta.get("pid"))
        cols = cols or (probed[0] if probed else 80)
        rows = rows or (probed[1] if probed else 24)
    log = Path(meta["log"])
    try:
        size = log.stat().st_size
    except OSError:
        return None
    old = _MIRROR.get(sid)
    st = _mirror_get(sid, cols, rows, rebuild=bool(old and old["off"] > size),
                     log=log, size=size)
    if st.pop("cut", False):
        # 冷启动只回放了日志尾部（MIRROR_TAIL_BYTES）：TUI 只重画变化的行，早先画好
        # 的静态行（输入框下方的 auto mode 提示）永远补不回来，整屏还会错位一行。
        # 补一次 ⌃L 让 TUI 全量重绘（和用户手动按 ⌃L 同一条通道，不动会话内容）。
        # 别在锁里做 socket 连接：2s 超时会把所有看这个会话的轮询堵死。
        _now = time.time()
        if _now - _MIRROR_REDRAW_AT.get(sid, 0) > 30:
            _MIRROR_REDRAW_AT[sid] = _now
            threading.Thread(target=_pty_ctl_write, args=(meta, b"\x0c"),
                             daemon=True).start()
    if st["off"] >= size:
        # 没新字节也把攒着的 dirty 冲一次：上一轮采样在半帧上被搁置（光标还在
        # 正文区），这一轮日志静着 = 帧肯定画完了，正好补一次帧边界比对。
        _diff_commit(st)
        st["t"] = time.time()
        return st
    try:
        with open(log, "rb") as f:
            f.seek(st["off"])
            new = f.read(size - st["off"])
    except OSError:
        return st
    safe = _safe_cut(new)
    if safe > 0:
        _t0 = time.time()
        data = _dim_to_gray(new[:safe])
        CH = 4096  # 分块只为 _safe_cut/喂 pyte；位移比对在每块出口 + 本轮出口各一次，
        # 半帧状态由 _diff_commit 的光标闸自己搁置，不靠块边界碰运气
        for i in range(0, len(data), CH):
            try:
                st["strm"].feed(data[i:i + CH])
            except Exception:
                pass  # 单块解析失败只丢该块，屏幕在下次全量重绘时自愈
            _diff_commit(st)
        st["off"] += safe
        _diff_commit(st)   # 帧尾出口：本轮喂完的最后一屏再比一次（轮询节奏的帧边界）
        _dt = (time.time() - _t0) * 1000.0
        if _dt >= 400:   # 冷启动尾部回放才会上 400ms；常规增量是几毫秒
            print("[mirror] %s 回放 %.0fKB 用时 %.0fms tail=%dKB"
                  % (sid[:8], len(data) / 1024.0, _dt, MIRROR_TAIL_BYTES // 1024), flush=True)
    st["t"] = time.time()
    return st








# 手机端固定深底（页面 #0b0e14）+ 浅色默认字（#e6edf3）画终端。
# 但 TUI 会按**宿主终端**的配色自适应：wrapper 的 pty 对面就是用户真正的终端，
# OSC 11（查背景色）问得到答案，终端是浅色主题时 Codex CLI 之类就给输入框/用户
# 消息画浅底（浅色主题里是 #f9f9f9）。用户口径：**白底去掉，默认全深色**——
# 浅底那块整块拿掉，内容压成灰字（Codex 的 asked-for 提示框就是这种）。
_TERM_FG = "#e6edf3"      # 默认前景（浅）
_TERM_BG = "#0b0e14"      # 默认背景（深）
_TERM_GRAY_SGR = "38;2;139;148;158"












def _row_cells(row, cols):
    """一行 → [[前景, 背景, 粗, 斜, 下划线, 反显, 文本], ...]；Tk 会话窗口按游程上色用。
    和 _row_runs 同一套取格逻辑，只是不压成 CSS 字符串。"""
    last = -1
    for x in range(cols):
        ch = row.get(x)
        if ch is not None and (ch.data.strip() or ch.bg != "default" or ch.reverse):
            last = x
    runs, prev, buf = [], None, []
    for x in range(0, last + 1):
        ch = row.get(x)
        if ch is None:
            st, data = ("default", "default", False, False, False, False), " "
        else:
            st, data = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse), ch.data
        if st != prev and buf:
            fg, bg = _cells_fg_bg(prev)
            runs.append([fg, bg,
                         bool(prev[2]), bool(prev[3]), bool(prev[4]), bool(prev[5]),
                         "".join(buf)])
            buf = []
        buf.append(data)
        prev = st
    if buf and prev is not None:
        fg, bg = _cells_fg_bg(prev)
        runs.append([fg, bg,
                     bool(prev[2]), bool(prev[3]), bool(prev[4]), bool(prev[5]),
                     "".join(buf)])
    return runs
















def _row_sgr(row, cols):
    """dict 行 → 带 SGR 的 ANSI 字符串（原样视图序列化用）。
    列位置精确：pyte 权威屏里宽字符后面跟着占位空档，直接按格输出会让 xterm.js
    （宽字符进 2 列）越排越歪——跟踪渲染游标：占位半格跳过、真实空档用 CHA 定位。"""
    last = -1
    for x in range(cols):
        ch = row.get(x)
        if ch is not None and (ch.data.strip() or ch.bg != "default" or ch.reverse):
            last = x
    if last < 0:
        return ""
    out = []
    prev = None
    rx = 0  # xterm.js 渲染游标（宽字符进 2 列）
    for x in range(last + 1):
        ch = row.get(x)
        if x < rx:  # 上一个宽字符的占位半格
            continue
        if x > rx:  # 真实空档：CHA 定位（不用空格补，避免沾上前一格的背景色）
            out.append("\x1b[%dG" % (x + 1))
            rx = x
        if ch is None:
            data, attr = " ", ("default", "default", False, False, False, False)
        else:
            data = ch.data
            if data == "⏺":
                data = "●"   # 同宽等长的纯文本形；手机 WebView 会把 U+23FA/U+23F5 渲染成 emoji（带方块底）
            elif data == "⏵":
                data = "▸"
            attr = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse)
        if attr != prev:
            code = ["0"]
            if attr[2]:
                code.append("1")
            if attr[3]:
                code.append("3")
            if attr[4]:
                code.append("4")
            if attr[5]:
                # 反显别透传 7：xterm.js 会把主题的浅色当前景换成底 → 整行白条
                # （浅色宿主主题的 TUI 反显行就是这样变成「浅色底」的）。
                # 自己发高亮条配色，和重排视图一致（深底 + 白字）。
                code.append("38;2;255;255;255")
                code.append("48;2;58;65;80")
            elif _light_bg(_term_color(attr[1], _TERM_BG)):
                # 浅底整块去掉：灰字 + 不发背景（落回 xterm 主题的深底）
                code.append(_TERM_GRAY_SGR)
            else:
                code.append(_fg_sgr(attr[0], attr[1]))
                code.append(_bg(attr[1]))
            out.append("\x1b[" + ";".join(code) + "m")
            prev = attr
        out.append(data)
        rx += max(1, _disp_w(data))
    out.append("\x1b[0m")
    return "".join(out)


def _snap_rows(scr, cols, rows):
    """把屏幕 24 行快照成 row-dict 列表（pyte 单元格是不可变 namedtuple，可共享引用）"""
    return [{x: c for x in range(cols) if (c := scr.buffer[y].get(x)) is not None}
            for y in range(rows)]


MIRROR_HIST_MAX = 800   # 重建历史最多保留多少行（往上滑的历史上限）


def _diff_commit(st):
    """检测屏幕内容整体上移：上移 K 行 = 有 K 行移出屏顶 → 截留为终端风格历史。
    claude 的 TUI 从不滚动（日志 0 个 LF），这是恢复历史的唯一途径。

    关键事实（2026-11-14/15 用 5 份真实会话日志回放标定，oracle 口径丢失从
    94 事件/423 行降到 13/27）：字节按 ~60B/100ms 细水长流地到，读边界必然切在
    重绘帧中间，挑边界没有出路，只能把比对本身做鲁棒——
    - 位移搜索每次都跑（老代码「变化行数 ≤rows-5 就不找」整类丢掉了 K=2~4 的
      小滚动）；选优只在「非空匹配 ≥2」的候选里做——裸字典序会让满屏空白在
      任意 K 都对得上的长空游程压过真正的内容对齐，选出错误的 k。
    - 对齐证据够强（游程 ≥3 非空 ≥2）→ 提交 prev[:K]；滚动帧渐进到达，认出几行
      先提交几行，下帧接着认。
    - 有部分证据但不够提交（游程 =2，半帧到达中）→ 搁置：回灌会把半帧的重复块
      拷贝写进基线，补完帧就永远对不上；dirty 并入 _dacc 留到下一轮一起比，
      连续 8 次兜底强制重置。
    - 零对齐证据（整屏重画/原地重绘/转圈）→ 锁步回灌基线：屏幕就是事实，抱着
      陈旧基线在连续重绘里只会永远对不齐（真日志时间线证实这是老方案的主要
      丢失源）。"""
    cols, rows = st["win"]
    scr = st["scr"]
    # dirty 累积：垃圾态样本不清账（半帧的变化并进下一轮比对，等价于老代码
    # 「垃圾态不清 dirty」），比对成功才清。
    dacc = st.setdefault("_dacc", set())
    dacc |= scr.dirty
    scr.dirty.clear()
    if not dacc:
        return
    prev_t = st.get("prev_t")
    if prev_t is None:
        st["prev_t"] = [_row_plain(scr.buffer[y], cols) for y in range(rows)]
        st["_prev_rows"] = _snap_rows(scr, cols, rows)
        st["ambig"] = 0
        st["_dacc"] = set()
        return
    cur_t = list(prev_t)
    for y in dacc:
        if 0 <= y < rows:
            cur_t[y] = _row_plain(scr.buffer[y], cols)
    if cur_t == prev_t:
        st["ambig"] = 0
        st["_dacc"] = set()
        return
    top_changed = cur_t[0] != prev_t[0]
    # 选优只在「非空匹配 ≥2」的候选里做：裸 (run,nb) 字典序会让一长串空行匹配
    # （nb=0，满屏空白在任意 K 都对得上）压过真正的内容对齐，选出错误的 k。
    best = (0, 0, 0)  # (游程长, 非空行数, k)
    for k in range(1, rows):
        lim = rows - k
        run = nb = 0
        for j in range(lim):
            if cur_t[j] == prev_t[k + j]:
                run += 1
                if prev_t[k + j].strip():
                    nb += 1
            else:
                if nb >= 2 and (run, nb) > best[:2]:
                    best = (run, nb, k)
                run = nb = 0
        if nb >= 2 and (run, nb) > best[:2]:
            best = (run, nb, k)
    shift = 0
    if best[0] >= 3:
        shift = best[2]
    elif (best[2] >= rows - 6 and best[0] >= 2 and top_changed):
        # 大位移：一屏几乎全换时可比行只剩 rows-k 行，「游程 ≥3」物理上凑不满；
        # 对上 2 行非空且顶行真的换了内容就算。
        shift = best[2]
    plausible = best[0] >= 2
    prev_rows = list(st.get("_prev_rows") or [])
    if shift and any(t.strip() for t in prev_t[:shift]):
        commit_t = tuple(prev_t[:shift])
        if commit_t == st.get("_last_commit"):
            st["_dacc"] = set()
            return  # 半 redraw 的相邻帧可能对齐同一位移，别把同一段历史写两遍
        st["_last_commit"] = commit_t
        hist = st.setdefault("hist", [])
        commit = prev_rows[:shift]
        # 与近期 hist 按内容去重：过量位移会把还留在屏上的行先提交一遍，几帧后真滚出
        # 又提交一遍 → 历史区出现重复块。重复形态多样（整段重复/区间错位重叠/新行夹
        # 重复段），统一用滑窗贪心：commit 里凡作为连续段出现在近期 hist（40 行窗口）
        # 中的段落一律丢弃；非空行允许单行匹配（内容行精确撞车必是重复），空行须成段
        # ≥2 才去（避免误吃段落间距）。
        if hist and commit:
            cs = [_row_plain(r, cols) for r in commit]
            for _win in (hist[-40:],
                         # 播种前缀全程参与去重：重放早期块对上的正是种子前段，
                         # 40 行窗口盖不住（评审实测：长文日志每次重建混进重复行）
                         hist[:st.get("_sb_seed_n") or 0] if st.get("_sb_seed_n") else []):
                ws = [_row_plain(r, cols) for r in _win]
                nw, nc = len(ws), len(cs)
                if not nw:
                    continue
                ws_set = set(ws)
                kept = []
                i = 0
                while i < nc:
                    best = 0
                    for L in range(nc - i, 1, -1):
                        if any(ws[p:p + L] == cs[i:i + L] for p in range(nw - L + 1)):
                            best = L
                            break
                    if not best and cs[i].strip() and cs[i] in ws_set:
                        best = 1
                    if best:
                        i += best
                    else:
                        kept.append(commit[i])
                        i += 1
                commit = kept
                cs = [_row_plain(r, cols) for r in commit]
                if not commit:
                    break
        hist.extend(commit)
        if len(hist) > MIRROR_HIST_MAX:
            _cut_n = len(hist) - MIRROR_HIST_MAX
            del hist[:len(hist) - MIRROR_HIST_MAX]
            if st.get("_sb_seed_n"):
                st["_sb_seed_n"] = max(0, st["_sb_seed_n"] - _cut_n)   # 截掉的是前缀
        st["seq"] = st.get("seq", 0) + len(commit)   # 按实际入账行数累加（去重后）
        # 持久化防抖：攒够 16 行或离上次落盘 3s 才写盘（别每行一次原子替换）。
        # 失败不拖累镜像主路径——持久化是加固，不是命脉。
        if st.get("sid"):
            _sb_new = st.get("_sb_new", 0) + len(commit)
            if _sb_new >= 16 or time.time() - st.get("_sb_at", 0) >= 3.0:
                try:
                    scrollback.save(st["sid"], hist)
                    st["_sb_at"] = time.time()
                    _sb_new = 0
                except Exception:
                    pass
            st["_sb_new"] = _sb_new
        st["prev_t"] = [_row_plain(scr.buffer[y], cols) for y in range(rows)]
        st["_prev_rows"] = _snap_rows(scr, cols, rows)
        st["ambig"] = 0
        st["_dacc"] = set()
        return
    if plausible:
        # 有部分位移证据但不够提交（半帧到达中）：回灌基线会把半帧垃圾（顶部的
        # 重复块拷贝）写进去，补完帧就永远对不上；搁置，dirty 已并入 _dacc 留到
        # 下一轮一起比。8 次兜底强制重置（⌃L 重绘后基线彻底换血的场景）。
        st["ambig"] = st.get("ambig", 0) + 1
        if st["ambig"] >= 8:
            st["prev_t"] = [_row_plain(scr.buffer[y], cols) for y in range(rows)]
            st["_prev_rows"] = _snap_rows(scr, cols, rows)
            st["ambig"] = 0
            st["_dacc"] = set()
        return
    # 零对齐证据（整屏重画/原地重绘/转圈）：屏幕就是事实，锁步回灌基线——
    # 下一个样本才能看到干净的两帧差（重绘洪流里抱陈旧基线只会永远对不齐）。
    st["prev_t"] = cur_t
    st["_prev_rows"] = _snap_rows(scr, cols, rows)
    st["ambig"] = 0
    st["_dacc"] = set()


def _mirror_ansi(st):
    """原样视图全量快照（电脑原始尺寸）：重建历史（写进 xterm 回滚区）+ 整屏重绘 + 光标归位。
    客户端写入后与电脑端状态完全一致，此后的增量相对移动全部成立。"""
    cols, rows = st["win"]
    parts = []
    for r in (st.get("hist") or []):
        parts.append(_row_sgr(r, cols) + "\r\n")
    scr = st["scr"]
    parts.append("\x1b[H\x1b[2J")   # 只清屏幕，回滚区保留
    for y in range(rows):
        parts.append("\x1b[%d;1H" % (y + 1))
        parts.append(_row_sgr(scr.buffer[y], cols))
    parts.append("\x1b[%d;%dH" % (scr.cursor.y + 1, scr.cursor.x + 1))
    return "".join(parts).encode("utf-8", "replace")


def _rewrap_row(row, cin, cout, track_x=-1):
    """把 cin 列的一行按显示宽度重排进 cout 列，逐单元格保留 SGR 样式。
    返回 (输出行 dict 列表, 光标映射 (sub_row, col) 或 None)。零宽字符并入前格。"""
    out_rows = []
    cur = {}
    cx = 0
    sub = 0
    cur_pos = None

    def commit():
        nonlocal cur, cx, sub
        while cur:  # 掐掉行尾不可见格
            last = max(cur)
            c = cur[last]
            if c is not None and (c.data.strip() or c.bg != "default" or c.reverse):
                break
            del cur[last]
        out_rows.append(cur)
        cur = {}
        cx = 0
        sub += 1

    for x in range(cin):
        c = row.get(x)
        if c is None:
            w = 1  # 没写过的空列：保留占位，对齐不塌
        elif not c.data:
            continue  # pyte 宽字符占位格（''）：在宽字符脚印内，不消耗列宽
        else:
            w = _disp_w(c.data)
        if w and cx + w > cout and cx > 0:
            commit()
        if x == track_x:  # 光标在换行边界时要记到新行，所以先判定换行再记位置
            cur_pos = (sub, cx)
        if c is None:
            pass
        elif w == 0:  # 零宽组合字符：并入前格
            prev = cur.get(cx - 1) if cx > 0 else None
            if prev is not None:
                cur[cx - 1] = prev._replace(data=prev.data + c.data)
            continue
        if c is not None:
            cur[cx] = c
        cx += w
    if track_x == cin:
        cur_pos = (sub, cx)
    commit()
    return out_rows, cur_pos


def _rewrap_rows(rows, cin, cout):
    out = []
    for r in rows:
        out.extend(_rewrap_row(r, cin, cout)[0])
    return out


def _frame_ansi(st, cout, rout, full=True):
    """原样视图整帧（手机自适应网格）：权威屏幕+历史按 (cout, rout) 重排为 ANSI。
    网格 = 顶部垫空 + 尾部历史 + 重排后的屏幕（屏幕恒贴网格底部），
    full=1 再附带更早的历史行（客户端 reset 后写入 xterm 回滚区，上滑可见）。
    返回 (bytes, (cout, 实际 rout))——屏幕重排后高于请求行数时会自动扩网格。"""
    cin, rin = st["win"]
    scr = st["scr"]
    tail = []
    cur_pos = None
    cy, cx = scr.cursor.y, scr.cursor.x
    for y in range(rin):
        rows, pos = _rewrap_row(scr.buffer[y], cin, cout, track_x=(cx if y == cy else -1))
        tail.extend(rows)
        if pos is not None:
            cur_pos = pos
    if rout < len(tail):
        rout = len(tail)
    hist = _rewrap_rows(st.get("hist") or [], cin, cout)
    grid_n = rout - len(tail)
    grid_hist = hist[-grid_n:] if grid_n > 0 else []
    older = hist[:len(hist) - len(grid_hist)]
    pad = grid_n - len(grid_hist)
    parts = ["\x1b[1;1H"]  # 整帧从网格左上角写起（轮询模式没有 reset，光标停在上帧末尾）
    if full:
        for r in older:
            parts.append(_row_sgr(r, cout) + "\x1b[K\r\n")
    for _ in range(pad):
        parts.append("\x1b[K\r\n")
    flow = grid_hist + tail
    for i, r in enumerate(flow):
        parts.append(_row_sgr(r, cout) + "\x1b[K")
        parts.append("\r\n" if i < len(flow) - 1 else "")
    if cur_pos is not None:
        parts.append("\x1b[%d;%dH" % (rout - len(tail) + cur_pos[0] + 1, cur_pos[1] + 1))
    return "".join(parts).encode("utf-8", "replace"), (cout, rout)




# claude 的状态行（无 ⏺ 标记，但永远是独立一行）：折行回并时必须当边界，
# 否则「⎿ Shell cwd was reset…」后面的「Thought for 5s」会被并进上一行




async def _live_render(sid, meta, agent=""):
    """屏幕 feed + 重排整个搬进 executor（_MIRROR_LOCK 串行）。
    SSE 每 tick 每条连接都调：放事件循环上，多客户端 × 3000 行重排 =
    推送断流 + 中继心跳饿死（表现为「开发机离线」「终端没响应」）。"""
    ad = agents.get(agent) or agents.adapter_of(sid)
    hints = ad.dialog_hints() if ad else None
    prefixes = ad.dialog_prefixes() if ad else None
    def _go():
        with _MIRROR_LOCK:
            st = _mirror_feed_inner(sid, meta)
            if st is None:
                return None
            cols, rows = st["win"]
            hist = st.get("hist") or []
            return (_reflow_html(st, hints, prefixes), _reflow_rows(hist, cols),
                    cols, rows, len(hist))
    return await asyncio.get_event_loop().run_in_executor(None, _go)


# ── TUI 弹层（AskUserQuestion / 权限确认）──
# 重排是为「正文段落」设计的：相邻正文行默认并成一句，靠手机重新断行。可弹层是
# **对齐结构**——「❯ 1. 选项A / 缩进说明 / 2. 选项B …」，重排会把 2 的标题、说明和
# 3 的标题并进同一个 <div>，手机上看着就是一句长话：看不出有几项、更点不到。
# 原样页（xterm 逐行原样画）一直是对的，所以用户只在实时页「看不到、选不了」。
# 这一屏只要是弹层，就整屏走「按行原样渲染」：保留行结构、字号可读、选项可点。
# 弹层底栏由「几段按键说明用 · 连起来」组成，宽终端上一整行放得下时能有 90+ 字
# （四问的 AskUserQuestion：「Enter to select · ↑/↓ to navigate · n to add notes ·
# Tab to switch questions · Esc to cancel」94 字）。以前只认「以提示词开头 且 ≤72 字」：
# 120 列的开发机上这种底栏一条都认不出来 → 整屏当正文重排，选项和标题被并成一段话，
# 手机上看不到也点不到（原样页不受影响）。现在按「命中几个说明词 + 底栏形状」认。
# 只收「弹层底栏」上的说法。普通输出里的行内提示（"Backgrounded agent (↓ to manage ·
# ctrl+o to expand)"）不收——它是正文的一部分，认成底栏会把下面的正文列表当选项。
















_DLG_PICK_LOCKS = {}
_DLG_PICK_LOCKS_LOCK = threading.Lock()


def _dlg_pick_lock(sid):
    """同一个会话同时只允许一次点选：通知按钮和页面各点一下，不能两段按键交错。"""
    with _DLG_PICK_LOCKS_LOCK:
        lk = _DLG_PICK_LOCKS.get(sid)
        if lk is None:
            lk = _DLG_PICK_LOCKS[sid] = threading.Lock()
        if len(_DLG_PICK_LOCKS) > 200:
            for k in list(_DLG_PICK_LOCKS):
                if k != sid and not _DLG_PICK_LOCKS[k].locked():
                    _DLG_PICK_LOCKS.pop(k, None)
        return lk


def _dlg_snapshot_rows(st, cols, rows, hints=None, prefixes=None):
    """当前屏幕上的选项弹层 → {cur, opts, text, multi, sig}；没有弹层返回 None。
    sig 只算选项文字（不算光标）：挪动光标时 sig 不变，才能判断「选项内容有没有变」。"""
    rds = [st["scr"].buffer[y] for y in range(rows)]
    dlg = _dialog_screen(rds, cols, hints, prefixes)
    if not dlg:
        return None
    texts = {}
    for y, n, _sel in dlg["opts"]:
        t = _dlg_clean(_row_plain(rds[y], cols))
        t = _DLG_OPT_RE.sub("", t).strip()      # 去掉「❯ 1. 」前缀，只留选项本身
        texts[n] = t
    cur = next((n for _y, n, sel in dlg["opts"] if sel), 0)
    sig = hashlib.sha1(
        "\x1f".join(texts[n] for n in sorted(texts)).encode("utf-8")).hexdigest()[:16]
    # rows：弹层整段文字（含说明行）。只用来核对「hook 说的和屏幕问的是不是一件事」。
    rtexts = [_dlg_clean(_row_plain(rds[y], cols)).strip()
              for y in range(rows) if _row_plain(rds[y], cols).strip()]
    return {"cur": cur, "opts": [n for _y, n, _s in dlg["opts"]],
            "text": texts, "multi": bool(dlg["multi"]), "sig": sig,
            "rows": rtexts[-40:]}


# 通知按钮只说「允许/拒绝」：由 host 按屏幕上的文字认是哪一项。
# 认不出、或两个方向都命中同一项 → 不猜（权限弹层猜错就是放行）。
_DLG_ALLOW_RE = re.compile(
    r"^\s*(yes|allow|approve|proceed|accept|ok)\b|^\s*(允许|同意|继续|是)", re.I)
_DLG_DENY_RE = re.compile(
    r"^\s*(no|deny|reject|cancel|don'?t|decline|abort)\b|^\s*(拒绝|不允许|取消|否)", re.I)


def _dlg_resolve_action(st, action):
    """按文字把「允许/拒绝」映射到选项号。返回 (want, err)。"""
    rx = _DLG_ALLOW_RE if action == "allow" else _DLG_DENY_RE
    cands = [n for n in st["opts"] if rx.search(st["text"].get(n) or "")]
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return 0, "认不出「%s」是哪一项，请打开 App 处理" % (
            "允许" if action == "allow" else "拒绝")
    return 0, "有好几项都像「%s」，请打开 App 选" % (
        "允许" if action == "allow" else "拒绝")


def _dlg_pick_core(read, press, want, text="", deadline=15.0, action=""):
    """「选第 want 项」的核实版实现：每按一次都重读屏幕。

    页面点拨层和安卓通知按钮共用这一份（host 做，客户端只传「我要第几项」）。
    action="allow"|"deny" 时不用 want，由屏幕上的选项文字现认（通知按钮用）。
    三条硬规矩：
      1. 给了选项文字、跟屏幕对不上 → 不按（弹层换了）；
      2. 没挪到目标项 → 不回车（宁可不动作，也不按错）；
      3. 回车后重读一次，屏幕没变就如实说「没确认到」，不谎报成功。
    read() -> snapshot | None；press(key) -> (ok, err)。"""
    t0 = time.time()
    st = read()
    if not st:
        return {"ok": False, "reason": "gone", "error": "屏幕上没有选项弹层"}
    if action:
        want, err = _dlg_resolve_action(st, action)
        if not want:
            return {"ok": False, "reason": "ambiguous", "error": err, "snap": st}
        text = ""                     # 现认出来的项号，不再用旧文字校验
    if want not in st["opts"]:
        return {"ok": False, "reason": "noopt",
                "error": "第 %d 项已经不在屏幕上了" % want, "snap": st}
    if text:
        # 页面按渲染后的文字传回来（可能被截断/带实体），两边归一化后比前缀：
        # 对不上说明这一项已经换成别的内容了，不按。
        # 页面 .dopt 里画的是整行「❯ 1. Yes」（含标记和序号），快照这边已经剥掉了，
        # 传回来的也要同样剥一遍再比，否则永远「选项内容变了」。
        a = re.sub(r"\s+", " ", st["text"].get(want) or "").strip()
        b = _DLG_OPT_RE.sub("", re.sub(r"\s+", " ", text)).strip()
        if b and not (a.startswith(b) or b.startswith(a)):
            return {"ok": False, "reason": "changed",
                    "error": "选项内容变了，重新看一眼再点", "snap": st}
    # 有的弹层不动就不标 ❯（cur=0）：先按一下再看，按到能看清当前项为止
    for _ in range(3):
        if st.get("cur"):
            break
        ok, err = press("up")
        if not ok:
            return {"ok": False, "reason": "key", "error": err, "snap": st}
        time.sleep(0.12)
        st = read()
        if not st:
            return {"ok": False, "reason": "gone", "error": "弹层已经不在屏幕上了"}
        if want not in st["opts"]:
            return {"ok": False, "reason": "noopt", "error": "选项变了", "snap": st}
    if not st["cur"]:
        return {"ok": False, "reason": "nocursor",
                "error": "看不到当前选中项，请用 ↑/↓ 手动选", "snap": st}
    n = len(st["opts"])
    first = True
    stuck = 0
    while st["cur"] != want:
        if time.time() - t0 > deadline:
            return {"ok": False, "reason": "timeout",
                    "error": "电脑响应太慢，没能选上，再点一次", "snap": st}
        prev_cur = st["cur"]
        ci, ti = st["opts"].index(st["cur"]), st["opts"].index(want)
        d, u = (ti - ci) % n, (ci - ti) % n        # 弹层循环：上下两个方向比步数
        k, need = ("down", d) if d <= u else ("up", u)
        batch = min(need, 12) if first and need > 1 else 1   # 只在第一段批量，之后逐个核对
        first = False
        for _ in range(batch):
            ok, err = press(k)
            if not ok:
                return {"ok": False, "reason": "key", "error": err, "snap": st}
            time.sleep(0.07 if batch > 1 else 0.13)
        st = read()
        if not st:
            return {"ok": False, "reason": "gone", "error": "弹层已经不在屏幕上了"}
        if want not in st["opts"]:
            return {"ok": False, "reason": "noopt", "error": "选项变了", "snap": st}
        # 连按几轮光标都不动 = 键根本没进会话（通道半死），别再白按到超时
        stuck = stuck + 1 if st["cur"] == prev_cur else 0
        if stuck >= 3:
            return {"ok": False, "reason": "noeffect",
                    "error": "按键没让电脑动，可能通道断了，再试一次", "snap": st}
    if st["multi"]:
        before = st["text"].get(want) or ""
        ok, err = press("space")
        if not ok:
            return {"ok": False, "reason": "key", "error": err, "snap": st}
        time.sleep(0.18)
        st2 = read()
        after = (st2["text"].get(want) or "") if st2 else ""
        flipped = (("☑" in after) != ("☑" in before)) if st2 else None
        if flipped is False:
            return {"ok": False, "reason": "nocheck",
                    "error": "第 %d 项可能没勾上，再点一次" % want, "snap": st2 or st}
        return {"ok": True, "reason": "ok", "multi": True,
                "verified": bool(flipped), "snap": st2 or st}
    ok, err = press("enter")
    if not ok:
        return {"ok": False, "reason": "key", "error": err, "snap": st}
    time.sleep(0.25)
    st2 = read()
    if st2 is None:
        return {"ok": True, "reason": "ok", "verified": True, "snap": None}
    if st2["sig"] != st["sig"] or st2["cur"] != st["cur"]:
        return {"ok": True, "reason": "ok", "verified": True, "snap": st2}
    return {"ok": True, "reason": "unverified", "verified": False,
            "error": "已按回车，但屏幕没变化，请看电脑", "snap": st2}


async def h_ext_dlg_pick(request):
    """POST /api/ext_dlg_pick —— 服务端可核实的点选：{sid, want, text?, rid?, cid?}

    以前「点一项」是页面按自己那帧 DOM 算差值盲按（页面旧了/人在电脑上动过光标就挪错）。
    现在读屏、挪动、核对、回车全在 host 做：页面和安卓通知按钮走同一个入口，
    同一个 cid 重复送达只执行一次，同一个会话只允许一个点选在跑。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = _alias_sid(str(data.get("sid") or ""))
    try:
        want = int(data.get("want") or 0)
    except Exception:
        want = 0
    text = str(data.get("text") or "")[:120]
    rid = str(data.get("rid") or "")[:40]
    cid = str(data.get("cid") or "")[:80]
    action = str(data.get("action") or "")
    if action not in ("allow", "deny"):
        action = ""
    if not sid or (want <= 0 and not action):
        return web.json_response({"ok": False, "reason": "badreq", "error": "缺少会话或选项"})

    def _go():
        it = _once_claim("dlgpick", cid)
        if it is not None:
            it["ev"].wait(20.0)             # 重复送达：拿第一份的真实结果，绝不再按一次
            with _ONCE_LOCK:
                got = it["res"]
            return got if got else {"ok": False, "reason": "busy",
                                    "error": "上一次选择还在处理"}
        res = {"ok": False, "reason": "busy", "error": "这个会话正在处理另一个选择"}
        lk = _dlg_pick_lock(sid)
        try:
            if not lk.acquire(timeout=20.0):
                return res
            try:
                s = (next((x for x in _tick_sessions() if x["sid"] == sid), None)
                     or next((x for x in _tick_sessions(force=True) if x["sid"] == sid), None))
                if not s:
                    return {"ok": False, "reason": "nosess", "error": "会话不存在或已结束"}
                meta = None if s.get("host") else _meta_for_pid(s.get("pid"))
                if meta is None and not s.get("host"):
                    try:
                        _ansi_metas_refresh()
                    except Exception:
                        pass
                    meta = _meta_for_pid(s.get("pid"))
                if meta is None and not s.get("host"):
                    return {"ok": False, "reason": "nomirror",
                            "error": "这个会话没有接入全彩镜像，请在电脑上处理"}
                ad = agents.get(s.get("agent")) or agents.adapter_of(sid)
                hints = ad.dialog_hints() if ad else None
                prefixes = ad.dialog_prefixes() if ad else None

                def _read():
                    with _MIRROR_LOCK:
                        st = _mirror_feed_inner(sid, meta)
                        if st is None:
                            return None
                        cols, rows = st["win"]
                        return _dlg_snapshot_rows(st, cols, rows, hints, prefixes)

                def _press(k):
                    try:
                        return ext_send_special(s, k, meta)
                    except Exception as e:
                        return False, "按键出错: %s" % e

                # 决定卡：这次到底在确认什么、每个选项意味着什么、有什么风险。
                # 事实来自 hook 的结构化入参；屏幕用来认选项，也用来核对
                # 「通知说的和屏幕上问的是不是同一件事」。
                pm = dict(_PERM_META.get((sid, rid)) or {}) if rid else {}
                try:
                    card = _decision_card(pm, _read(),
                                          why=(_EV.get(sid) or {}).get("ptext") or "")
                except Exception:
                    card = None
                if action and card is not None and not card.get("verified"):
                    # 通知按钮只认「视频对得上」的决定：对不上就不给一键放行
                    print("[dlgcard] verified=0 sid=%s rid=%s target=%r"
                          % (sid[:8], (rid or "-")[:12],
                             (card.get("ask") or {}).get("target", "")[:80]), flush=True)
                    return {"ok": False, "reason": "unverified",
                            "error": "通知和屏幕上的请求没对上，请打开 App 处理",
                            "card": card}

                res = _dlg_pick_core(_read, _press, want, text, action=action)
                if card:
                    res["card"] = card
                res["sid"] = sid
                if res.get("snap"):
                    # 别把整屏文字回给客户端：只要当前项/项号，够页面和通知更新用
                    res["snap"] = {"cur": res["snap"]["cur"],
                                   "opts": res["snap"]["opts"],
                                   "multi": res["snap"]["multi"]}
                return res
            finally:
                lk.release()
        finally:
            _once_settle("dlgpick", cid, res)

    res = await asyncio.get_event_loop().run_in_executor(None, _go)
    ok = bool(res.get("ok"))
    # 决策留痕：成功的带上 request_id 走既有幂等审计；失败的单独记一笔（排查「点了没反应」）
    try:
        if ok and rid:
            _perm_answer_core(sid, rid, str(want or action), label=text, via="dlgpick")
        elif not ok:
            _perm_audit_write({"ts": round(time.time(), 3), "sid": sid,
                               "agent": agents.agent_of(sid) or "",
                               "request_id": rid, "option_id": str(want),
                               "label": text[:60], "via": "dlgpick",
                               "ok": False, "reason": res.get("reason") or ""})
    except Exception:
        pass
    print("[dlgpick] sid=%s want=%s ok=%s reason=%s rid=%s %s"
          % (sid[:8], want, int(ok), res.get("reason") or "",
             (rid or "-")[:12], (res.get("error") or "")[:120]), flush=True)
    return web.json_response(res)


_PENDING_WINDOW = 30 * 60


def _screen_question(rows):
    """弹层里像题面的那一行（有问号优先，否则第一行）。选项行后面另给。"""
    for t in rows or []:
        t = str(t or "").strip()
        if t and ("?" in t or "？" in t):
            return re.sub(r"\s+", " ", t)[:200]
    r0 = [str(t or "").strip() for t in (rows or []) if str(t or "").strip()]
    return re.sub(r"\s+", " ", r0[0])[:200] if r0 else ""


def _screen_pending(skip):
    """屏幕上正开着选项弹层的会话（不分 agent、无 hook/rid）：工作台也要看得到、点得动。
    点选项仍走 /api/ext_dlg_pick（读屏 → 挪光标 → 核对 → 回车全在 host）。"""
    out = []
    now = time.time()
    for s in _tick_sessions():
        sid = str(s.get("sid") or "")
        if (not sid or sid in skip or s.get("host") or s.get("state") == "exited"):
            continue                        # host 托管的会话没有本机镜像
        try:
            meta = _meta_for_pid(s.get("pid"))
            if meta is None:
                continue
            ad = agents.get(s.get("agent")) or agents.adapter_of(sid)
            hints = ad.dialog_hints() if ad else None
            prefixes = ad.dialog_prefixes() if ad else None
            with _MIRROR_LOCK:
                st = _mirror_feed_inner(sid, meta)
                if st is None:
                    continue
                cols, rows = st["win"]
                snap = _dlg_snapshot_rows(st, cols, rows, hints, prefixes)
        except Exception:
            continue
        if not snap or not snap.get("opts"):
            continue
        q = _screen_question(snap.get("rows"))
        opts = [{"id": str(n), "label": (snap["text"].get(n) or "")[:120],
                 "is_deny": bool(_DLG_DENY_RE.search(snap["text"].get(n) or ""))}
                for n in snap["opts"]]
        out.append({"sid": sid, "title": _sid_title(sid),
                    "agent": agents.agent_of(sid) or "",
                    "rid": "", "source": "screen", "act": "",
                    "text": q, "summary": (q or "屏幕上有选项等你选")[:120],
                    "ts": now,
                    "ask": {"action": "选一项", "target": q},
                    "risk": [], "why": "",
                    "options": opts, "tool": "",
                    "multi": bool(snap.get("multi")), "cur": snap.get("cur") or 0})
    return out


def _pending_list():
    """「待你决定」：一屏内能拍板的事，按会话取最新一条。

    主来源是 host 的提醒队列（perm）——和通知栏同一份。队列之外再补一路屏幕扫描：
    opencode/hermes 这类没有 hook 的 agent 只会在屏幕上等选项，不扫描就看不见。"""
    now = time.time()
    with _PERM_LOCK:
        answered = {(s, r) for (s, r, _o) in _PERM_DONE}
    out, seen, perm_sids = [], set(), set()
    for ev in reversed(_NOTIFY_Q):
        sid = ev.get("sid") or ""
        if not sid or sid in seen:
            continue
        seen.add(sid)                       # 每个会话只认最新一条：后面的已覆盖前面的
        if ev.get("kind") != "perm":
            continue
        if now - (ev.get("ts") or 0) > _PENDING_WINDOW:
            continue
        rid = ev.get("rid") or ""
        if rid and (sid, rid) in answered:
            continue
        perm_sids.add(sid)
        out.append({"sid": sid, "title": ev.get("title") or _sid_title(sid),
                    "agent": agents.agent_of(sid) or "", "rid": rid,
                    "act": ev.get("act") or "", "text": ev.get("text") or "",
                    "summary": ev.get("summary") or "", "ts": ev.get("ts") or 0,
                    # 业务语义：确认什么、有什么风险、为什么现在问
                    "ask": ev.get("ask") or {}, "risk": ev.get("risk") or [],
                    "why": ev.get("why") or "",
                    # 在选什么（选一项 vs 批准/拒绝）和题面类型：手机据此区分
                    # 「要你选」和「要你回话」，不再两种都长得一样。
                    "options": ev.get("options") or [],
                    "tool": ev.get("tool") or "", "source": "queue"})
    out.extend(_screen_pending(perm_sids))
    return out


async def h_ext_pending(request):
    """GET /api/ext_pending —— 所有等你拍板的事（列表页/通知分组用）。"""
    await require_token(request)
    items = _face_only(_pending_list(), _face_for(request))
    return web.json_response({"ok": 1, "n": len(items), "items": items})


# ---- 工头（新界面）事实层：三块板 / 一件活 / 验收 -------------------------
# 状态只由会话、审批、提醒历史算（见 boss.py）；这一层不调模型。
_BOSS_ART_CACHE = {}
_BOSS_ART_TTL = 120


def _boss_art_ok(rel):
    """cwd 扫描的兜底条目过一遍筛：logs/ 目录、.sock、数据库是运行时噪音，
    不该占「产出」的 6 个坑（写过的文件才是交付物）。"""
    r = str(rel or "").replace(os.sep, "/")
    parts = r.split("/")
    if "logs" in parts[:-1] or r.endswith(".sock"):
        return False
    return Path(r).name != "codeboss.db"


async def _boss_artifacts(sid):
    """一件活的产出物（最多 6 个，带绝对路径给手机缩略图用）。目录扫描有成本，缓存 2 分钟。"""
    now = time.time()
    hit = _BOSS_ART_CACHE.get(sid)
    if hit and now - hit[0] < _BOSS_ART_TTL:
        return hit[1]
    out = []
    try:
        d = await _ext_artifacts_payload(sid)
        root = d.get("cwd") or ""
        for e in (d.get("entries") or []):
            if len(out) >= 6:
                break
            if not _boss_art_ok(e.get("rel") or e.get("name") or ""):
                continue
            out.append(dict(e, path=str(Path(root) / e["rel"]) if root else ""))
    except Exception:
        out = []
    _BOSS_ART_CACHE[sid] = (now, out)
    if len(_BOSS_ART_CACHE) > 200:
        for k in sorted(_BOSS_ART_CACHE, key=lambda q: _BOSS_ART_CACHE[q][0])[:-100]:
            _BOSS_ART_CACHE.pop(k, None)
    return out


_BOSS_SAY_BUSY = set()


async def _boss_say(w):
    """给一件活换句人话（表达层）。慢也不能拖住板子：页面先拿事实句，下一轮换。"""
    key = boss.say_key(w)
    try:
        if not boss.budget_take(maxn=boss.brain_cfg().get("max", boss.BRAIN_MAX)):
            boss.put_say(key, "", "facts")       # 今天额度用完：这句就是事实句，别再试
            return
        t = await notify._llm_text(boss.SAY_PROMPT, boss.say_facts(w), "boss_say",
                                   timeout=6)
        say, src = boss.clean_say(t, w)
        boss.put_say(key, say, src)
        if src != "llm":
            print("[boss] 模型没答上，退回事实句 sid=%s" % str(w.get("sid"))[:8],
                  flush=True)
    except Exception:
        pass
    finally:
        _BOSS_SAY_BUSY.discard(key)


async def _boss_board_data(request):
    """三块板的数据。灰度：host 上没放 boss.json 就 enabled=false，页面连入口都不出现。"""
    dev = request.query.get("dev") or _hdr(request, "X-CB-Dev")
    if not boss.ui_enabled(dev):
        return {"ok": 0, "enabled": False, "brain": "off"}
    face = _face_for(request)
    sessions = _face_only(await _ext_sessions_items(request), face)
    pending = _face_only(_pending_list(), face)
    hist = _face_only(_nh_list(days=1, limit=300), face)
    st = boss.load_state()
    now = time.time()
    kw = dict(sessions=sessions, pending=pending, hist=hist,
              verdicts=st.get("verdicts") or {}, now=now,
              machine=socket.gethostname())
    b = boss.project(artifacts={}, **kw)
    arts = {}
    for w in b["done"][:5]:            # 只给前 5 件扫目录：别为了一屏板拖垮 host
        arts[w["sid"]] = await _boss_artifacts(w["sid"])
    b = boss.project(artifacts=arts, **kw)
    b["enabled"] = True
    works = [w for k in ("now", "decide", "idle", "done") for w in b.get(k) or []]
    try:
        boss.record_transitions(works, now=now)
    except Exception:
        pass
    # 表达层：页面开着才生成（省钱口径）；模型调用在后台跑，这一轮先给事实句。
    says = boss.load_says()
    boss.apply_says(b, says)
    if boss.brain_cfg().get("on"):
        for w in works:
            key = boss.say_key(w)
            if key in says or key in _BOSS_SAY_BUSY:
                continue
            _BOSS_SAY_BUSY.add(key)
            w["say_pending"] = 1
            try:
                asyncio.get_running_loop().create_task(_boss_say(w))
            except Exception:
                _BOSS_SAY_BUSY.discard(key)
                w.pop("say_pending", None)
    return b


async def h_boss_health(request):
    """GET /api/boss/health?dev= —— 这台手机要不要显示「工头（新）」入口。"""
    await require_token(request)
    dev = request.query.get("dev") or _hdr(request, "X-CB-Dev")
    en = boss.ui_enabled(dev)
    br = ("llm" if boss.brain_cfg().get("on") else "facts") if en else "off"
    return web.json_response({"ok": True, "enabled": en, "brain": br})


async def h_boss_board(request):
    """GET /api/boss/board?dev= —— 现在 / 等你定 / 已交活。"""
    await require_token(request)
    return _jsonz(await _boss_board_data(request))


_BOSS_LIVE = {}          # sid -> (ts, {"say","tool"})：一件活页每几秒轮一次，别每次都读盘
_BOSS_LIVE_TTL = 6


def _boss_live_pick(msgs, cap=300):
    """从转录尾巴里挑「它刚说了什么、手上在干什么」——不是终端回放，一屏能看懂。"""
    say, tool = "", ""
    for m in reversed(msgs or []):
        if not say and m.get("role") == "assistant":
            t = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
            if t:
                say = t if len(t) <= cap else t[:cap] + "…"
        if not tool:
            ts = m.get("tools") or []
            if ts:
                x = ts[-1]
                d = re.sub(r"\s+", " ", str(x.get("detail") or "")).strip()[:160]
                tool = (str(x.get("name") or "") + (" · " + d if d else "")).strip()
        if say and tool:
            break
    return {"say": say, "tool": tool}


async def _boss_live(sid, s):
    hit = _BOSS_LIVE.get(sid)
    if hit and time.time() - hit[0] < _BOSS_LIVE_TTL:
        return hit[1]
    ad = agents.get(s.get("agent")) or agents.adapter_of(sid)
    p = ad.transcript_path(s.get("cwd", ""), s.get("sid") or sid) if ad else None
    if not p:
        return {}

    def _read():
        try:
            if getattr(ad, "key", "") == "claude":
                return (transcript.parse_transcript(p, limit=8, maxbytes=320 * 1024),
                        transcript.recent_files(p))
            return ad.parse_transcript(p, limit=8), []
        except Exception:
            return [], []

    try:
        msgs, files = await asyncio.get_event_loop().run_in_executor(None, _read)
        d = _boss_live_pick(msgs)
        d["files"] = files
    except Exception:
        d = {}
    _BOSS_LIVE[sid] = (time.time(), d)
    if len(_BOSS_LIVE) > 100:
        for k in sorted(_BOSS_LIVE, key=lambda q: _BOSS_LIVE[q][0])[:-50]:
            _BOSS_LIVE.pop(k, None)
    return d


def _boss_file_entries(files, cwd, cap=6):
    """转录里写过的文件 → 产出物条目（新→旧）。不在会话 cwd 里的（worktree、
    记忆目录）给绝对路径；预览端点只认「这个会话真写过的绝对路径」。"""
    out, seen = [], set()
    try:
        root = Path(cwd).resolve() if cwd else None
    except (OSError, ValueError):
        root = None
    for fp in files or []:
        if len(out) >= cap:
            break
        try:
            p = Path(fp).resolve()
            st = p.stat()
        except (OSError, ValueError):
            continue
        if not p.is_file() or str(p) in seen:
            continue
        seen.add(str(p))
        rel = str(p)
        if root:
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                pass
        out.append({"name": p.name, "rel": rel, "size": st.st_size,
                    "mtime": int(st.st_mtime), "kind": _ext_kind(p.name),
                    "path": str(p)})
    return out


async def h_boss_work(request):
    """GET /api/boss/work/<sid> —— 一件活：事实时间线 + 产出物 + 它现在在干什么。"""
    await require_token(request)
    dev = request.query.get("dev") or _hdr(request, "X-CB-Dev")
    if not boss.ui_enabled(dev):
        return web.json_response({"ok": 0, "enabled": False}, status=403)
    sid = request.match_info["sid"]
    b = await _boss_board_data(request)
    work = next((w for k in ("now", "decide", "idle", "done") for w in b.get(k) or []
                 if w.get("sid") == sid), None)
    hist = _nh_list(sid=sid, days=2, limit=60)
    timeline = [{"ts": h.get("ts") or 0, "kind": h.get("kind") or "",
                 "text": h.get("summary") or h.get("text") or "",
                 "about": h.get("about") or "", "act": h.get("act") or "",
                 "state": h.get("state") or ""}
                for h in reversed(hist)]
    live, s = {}, None
    try:
        s = await _find_session(sid)
        if s:
            live = await _boss_live(sid, s)
    except Exception:
        live = {}
    # 产出以「它真写过的文件」为准；cwd 扫到的补在后头（旧会话/非 claude 没这份转录）
    arts = _boss_file_entries((live or {}).get("files"),
                              ((s or {}).get("cwd") or ""))
    for a in await _boss_artifacts(sid):
        if len(arts) >= 6:
            break
        if any(b.get("path") == a.get("path") for b in arts):
            continue
        arts.append(a)
    return _jsonz({"ok": 1, "enabled": True, "work": work,
                   "timeline": timeline, "live": live, "artifacts": arts})


async def h_boss_verdict(request):
    """POST /api/boss/verdict —— 老板收下/退回（{sid, verdict, text?}）。
    「退回」的话由页面走既有发消息通道，这里只记动作，供下次判断。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    sid = str(d.get("sid") or "")
    verdict = str(d.get("verdict") or "")
    if not boss.ui_enabled(str(d.get("dev") or "") or _hdr(request, "X-CB-Dev")):
        return web.json_response({"ok": False, "enabled": False}, status=403)
    ok = boss.set_verdict(sid, verdict, text=str(d.get("text") or ""))
    return web.json_response({"ok": bool(ok)}, status=(200 if ok else 400))


async def h_ext_live(request):
    """手机端重排实时视图：权威屏幕 → 自适应 HTML（正文换行、表格/代码横向滚动、保留颜色）"""
    await require_token(request)
    sid = request.match_info["sid"]
    s = await _find_session(sid)
    if not s:
        return web.json_response({"error": "会话不存在"}, status=404)
    if s.get("acp"):
        # 协议源窗口（gemini 等）：hist 就是结构化事件落的文本行，与 pty 同一
        # 个 _reflow_rows 渲染——手机端零改动；live 屏（html）没有对应物，留空。
        w = agent_acp.window(sid)
        if w is None:
            if not agent_acp.is_ghost(sid):
                return web.json_response({"error": "窗口不存在"}, status=404)
            rows = scrollback.load(sid)   # 重启后的鬼影：进程没了，hist 从仓里回来
            return _jsonz({"ok": 1, "html": "",
                           "hist": _reflow_rows(rows, 80),
                           "cols": 80, "rows": 24,
                           "seq": len(rows), "kind": "protocol"})
        return _jsonz({"ok": 1, "html": "",
                       "hist": _reflow_rows(w.hist, 80),
                       "cols": 80, "rows": 24,
                       "seq": w.seq, "kind": "protocol"})
    meta = _meta_for_pid(s.get("pid"))
    if not meta:
        # 手机打开会话走的就是这里；没有全彩镜像只能退回截图。同一会话会被反复轮询，
        # 每个 sid 只打一次（否则一次打开能刷几百行）。
        if sid not in _LIVE_NOMETA_LOGGED:
            if len(_LIVE_NOMETA_LOGGED) > 200:
                _LIVE_NOMETA_LOGGED.clear()
            _LIVE_NOMETA_LOGGED.add(sid)
            print("[live] sid=%s 没有全彩镜像，手机退回截图 | %s"
                  % (sid[:8], _sess_diag(s)), flush=True)
        return web.json_response({"error": "该会话未接入全彩镜像"}, status=404)
    ad = agents.get(s.get("agent")) or agents.adapter_of(sid)
    hints = ad.dialog_hints() if ad else None
    prefixes = ad.dialog_prefixes() if ad else None
    # 重排 3000 行历史 + 读镜像都在 executor 里做，别占事件循环
    def _render():
        with _MIRROR_LOCK:
            st = _mirror_feed_inner(sid, meta)
            if st is None:
                return None
            cols, rows = st["win"]
            return {"ok": 1, "html": _reflow_html(st, hints, prefixes),
                    "hist": _reflow_rows(st.get("hist") or [], cols),
                    "cols": cols, "rows": rows,
                    "seq": st.get("seq", 0)}

    # 同一个会话的渲染合并成一次：冷启动回放要 ~1s，页面轮询 + SSE 引导 + 两台手机
    # 同时打过来时，原来每个请求都排一次队，既堵 executor 线程池（别的接口跟着卡），
    # 又互相等同一把锁——实测一次 5s+。合并后全部等同一份结果，最多付一次回放的账。
    loop = asyncio.get_event_loop()
    fut = _LIVE_INFLIGHT.get(sid)
    if fut is None:
        fut = loop.create_future()
        _LIVE_INFLIGHT[sid] = fut
        try:
            payload = await loop.run_in_executor(None, _render)
        except Exception:
            payload = None
        finally:
            _LIVE_INFLIGHT.pop(sid, None)
        if not fut.done():
            fut.set_result(payload)
    else:
        payload = await fut
    if payload is None:
        return web.json_response({"error": "日志不存在"}, status=404)
    return _jsonz(payload)


async def h_ext_snap(request):
    """原样视图整帧快照：?cols=&rows= 指定网格（默认电脑尺寸；列数=电脑列数时为恒等重排，
    行数可大于电脑行数——上方填历史尾部、更早历史经 full=1 进 xterm 回滚区，屏幕恒贴网格底）"""
    await require_token(request)
    sid = request.match_info["sid"]
    s = await _find_session(sid)
    if not s:
        return web.json_response({"error": "会话不存在"}, status=404)
    meta = _meta_for_pid(s.get("pid"))
    if not meta:
        return web.json_response({"error": "该会话未接入全彩镜像"}, status=404)
    q = request.rel_url.query
    try:
        rout = min(max(int(q.get("rows") or 0), 0), 400)
    except ValueError:
        rout = 0
    full = (q.get("full") or "1") != "0"
    # pyte 解析 + 整帧重排都在 executor：原样视图每秒轮询，放循环上整站卡
    def _build():
        with _MIRROR_LOCK:
            st2 = _mirror_feed_inner(sid, meta)
            if st2 is None:
                return None
            cin2, rin2 = st2["win"]
            body2, (cout2, rout2) = _frame_ansi(st2, cin2, rout or rin2, full=full)
            return (body2, cout2, rout2, st2["off"], len(st2.get("hist") or []),
                    st2.get("seq", 0), st2["win"])
    got = await asyncio.get_event_loop().run_in_executor(None, _build)
    if got is None:
        return web.json_response({"error": "日志不存在"}, status=404)
    body, cin, rout, off, nhist, seq, win = got
    resp = web.Response(body=body)
    # 不压缩：客户端会主动 abort 掉被顶替的快照请求，压缩中途被取消会弄坏 zlib
    # 状态 → write_eof 报 inconsistent stream state → 500（今天日志里那批 500 就是它）。
    resp.headers["X-Log-Size"] = str(off)
    resp.headers["X-Hist-Count"] = str(nhist)
    resp.headers["X-Hist-Seq"] = str(seq)
    resp.headers["X-Winsize"] = "%d,%d" % (cin, rout)
    resp.headers["X-Host-Winsize"] = "%d,%d" % win
    import hashlib as _hashlib
    resp.headers["X-Frame-Hash"] = _hashlib.md5(body).hexdigest()[:12]
    return resp


def _newest_meta_for_sid(sid):
    """按 wrapper 的 cmd（claude --resume <sid>）找这个会话最新的 meta。

    fork 出来的窗口 cmd 里同样带着父会话的 sid（--resume 父 --fork-session），
    只按 cmd 找会认错窗口：所以先认 meta 自己的 sid 完全相同的（正常 wrapper 的
    meta sid 就是 claude 会话 id，fork 的是随机名），找不到再退回 cmd 匹配。"""
    best, bt, best_rank = None, 0.0, 0
    if not ANSI_DIR.exists():
        return None
    for f in ANSI_DIR.glob("*.meta.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("sid") == sid:
            rank = 2
        elif sid in (m.get("cmd") or []):
            rank = 1
        else:
            continue
        t = f.stat().st_mtime
        if rank > best_rank or (rank == best_rank and t > bt):
            best, bt, best_rank = m, t, rank
    return best


def _trust_pending(meta):
    """wrapper 日志尾部是不是「目录信任」框（--resume 在新 tty 会弹，默认 No, exit）。"""
    try:
        log = Path(meta.get("log") or "")
        if not log.exists():
            return False
        size = log.stat().st_size
        with open(log, "rb") as f:
            f.seek(max(0, size - 24576))
            tail = f.read().decode("utf-8", "ignore")
        txt = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", tail)
        # TUI 用光标定位排版，词间空格会丢（日志里是 "Yes,Itrustthisfolder"），
        # 所以去掉所有空白再匹配
        flat = re.sub(r"\s+", "", txt)
        return ("Itrustthisfolder" in flat or "No,exit" in flat
                or "Quicksafetycheck" in flat or "Doyoutrust" in flat)
    except Exception:
        return False


def _ansi_tail(meta, nbytes=24576):
    """wrapper 日志尾部（去掉 ANSI 控制序列），用来在读不到屏幕时判断界面状态。"""
    try:
        log = Path(meta.get("log") or "")
        if not log.exists():
            return ""
        size = log.stat().st_size
        with open(log, "rb") as f:
            f.seek(max(0, size - nbytes))
            tail = f.read().decode("utf-8", "ignore")
        return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", tail)
    except Exception:
        return ""


def _trust_yes_selected(text):
    """目录信任框里当前选中的是不是「Yes」。

    返回 True/False；看不出来返回 None（别乱按）。
    注意：不同 claude 版本的默认项不一样——老版默认「No, exit」，
    新版（2.1.x）默认「Yes, I trust this folder」。盲按可能把会话选成退出。"""
    yes = no = None
    for line in (text or "").splitlines():
        low = line.lower()
        mark = any(m in line[:10] for m in ("❯", "▶", "▸", "●", ">", "*"))
        if "yes" in low and "trust" in low and yes is None:
            yes = mark
        elif "no" in low and "exit" in low and no is None:
            no = mark
    if yes:
        return True
    if no:
        return False
    return None


def _trust_confirm(tty, wid, meta, force=False):
    """把目录信任框确认掉（选 Yes）。

    不同 claude 版本的默认项不一样（老版默认「No, exit」，2.1.x 默认
    「Yes, I trust this folder」），盲按可能把会话选成退出——所以先读一眼
    界面，看得准才动手；看不清（选项还没画出来）就等下一轮。

    优先走 wrapper 的控制 socket：原始字节直接进 claude 的 pty，最快、
    不抢焦点、无头机器上也能用。没有控制通道的老会话才退回终端通道
    （macOS = AppleScript，Linux = tmux）。"""
    txt = ""
    if wid:
        ok, t = plat.screen(wid, tty)
        if ok:
            txt = t
    picked = _trust_yes_selected(txt)
    if picked is None:
        picked = _trust_yes_selected(_ansi_tail(meta))
    _dbg("[trust] picked=%s force=%s screen=%d" % (picked, force, len(txt)))
    if picked is True or (picked is None and force):
        # 默认就是 Yes（或实在读不出来，按新版默认 Yes）：回车即可
        if _pty_ctl_write(meta, b"\r"):
            return True
        return plat.send_enter(tty)
    if picked is False:
        # 默认在 No，下移一格再回车。**必须分两次写、隔 100ms**：同一个包里
        # `\x1b[B\r` 这个 TUI 会吞掉回车，结果停在 No（2026-11-11 macmini4：
        # 两个会话召回都卡在信任框，一个被选中 No, exit 直接退出）。
        if not _pty_ctl_write(meta, b"\x1b[B"):
            plat.send_down(tty)
            return plat.send_enter(tty)
        time.sleep(0.12)
        return _pty_ctl_write(meta, b"\r") or plat.send_enter(tty)
    return False               # 选项还没画出来，这轮不按


def _meta_for_tty(tty):
    """按宿主终端的 tty 找 wrapper 的 meta。新会话还没注册进 claude agents 之前，
    只能靠 tty 认领（wrapper 自己记的 tty）。"""
    if not tty or not ANSI_DIR.exists():
        return None
    best, bt = None, 0.0
    for f in ANSI_DIR.glob("*.meta.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (m.get("tty") or "") != tty:
            continue
        t = f.stat().st_mtime
        if t > bt:
            best, bt = m, t
    return best


async def _auto_trust_watch(tty, wid, timeout=60):
    """无头机器（Linux 服务器/NAS）没人能点「信任这个文件夹」——
    后台盯着，弹出来就替用户确认掉，否则新会话永远卡在信任框上。
    macOS 上终端窗口是给人看的，不抢。"""
    t0 = time.time()
    seen = 0.0
    while time.time() - t0 < timeout:
        await asyncio.sleep(3)
        meta = _meta_for_tty(tty)
        if not meta or not _trust_pending(meta):
            continue
        if not seen:
            seen = time.time()
        # 界面还没画完就读不出选中项，等；超过 12s 还读不出来就只回车
        if _trust_confirm(tty, wid, meta, force=(time.time() - seen > 12)):
            return


def _mirror_seed_log(sid, ansi_dir=None):
    """给「按会话 sid 命名的新式镜像日志」找种子：升级那一刻已经分家的旧 hex 日志。

    以前 wrapper 每次重生（切模型/召回）都用随机 hex 名落日志，旧进程攒的终端
    滚动历史被撇在旧 hex 日志里；手机端全彩镜像按 pid 认 meta，永远只读新日志
    ——用户看到的就是「历史没了，只剩一页都不到」（2026-11-25 deskboss 2.9MB
    滚动历史被切模型撇下，文件其实还在盘上）。
    现在日志按 sid 命名、append 续写，重生后接着长；分家前留下的旧日志在
    第一次新式重生时搬一次家（新日志已存在就不再搬，续命全靠 append）。
    **整条前代链全要**（按日志死亡时间正序）：只挑最近一代，会把穷世代当独苗
    ——0.9.102 首版只续到 2KB，历史在它前面的祖辈日志里（2026-11-25 用户复核）。
    fork 的 cmd 里也带着父会话 sid，但它是另一条对话，绝不混进来。"""
    d = Path(ansi_dir) if ansi_dir else ANSI_DIR
    newlog = d / (sid + ".log")
    if newlog.exists():
        return []
    out, seen = [], set()
    try:
        for f in d.glob("*.meta.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            cmd = m.get("cmd") or []
            if sid not in cmd or "--fork-session" in cmd:
                continue
            lp = m.get("log")
            if not lp or Path(lp).name == newlog.name or lp in seen:
                continue
            try:
                t = Path(lp).stat().st_mtime
            except OSError:
                continue
            seen.add(lp)
            out.append((t, lp))
    except OSError:
        return []
    out.sort()
    return [lp for _, lp in out]


def _mirror_seed_merge(sid, seeds, ansi_dir=None, cap=8 * 1024 * 1024):
    """把前代链并成 <sid>.log，带上限：超限从最老头部丢、保最新尾巴。

    回放冷启只看最后 ~1MB（_mirror_tail_start），把 61MB 的祖辈日志整个并进来
    对画面毫无增益，却会让换日志后的增量位点错乱、首次喂屏把整份读一遍
    （2026-11-25 08:36 agentboss 喂 61MB 卡 host 50 秒、中继被拖断重连）。"""
    d = Path(ansi_dir) if ansi_dir else ANSI_DIR
    d.mkdir(parents=True, exist_ok=True)
    sizes, total = [], 0
    for p in seeds:
        try:
            n = Path(p).stat().st_size
        except OSError:
            n = 0
        sizes.append(n)
        total += n
    skip = max(0, total - cap)
    w = 0
    with open(d / (sid + ".log"), "ab") as b:
        for p, n in zip(seeds, sizes):
            o = min(skip, n)
            skip -= o
            if o >= n:
                continue
            with open(p, "rb") as a:
                a.seek(o)
                shutil.copyfileobj(a, b, 1 << 20)
            w += n - o
    return w


def _open_wrapped_terminal(cwd, sid, extra_meta=None, model=None):
    """新终端窗口/标签开 wrapper + <agent> --resume；返回 (win_id, tty, err)。
    macOS 是 Terminal.app 的新标签，Linux 是新的 tmux 面板。

    具体命令由这个会话所属的 agent 决定（agents.py）：Claude 是 `claude --resume <sid>`，
    Codex 是 `codex resume <sid>`。

    model 非空时加 `--model`：**换供应商必须带**。`claude --resume` 会恢复这个
    会话自己记着的模型名，它压过 settings.json 的 env；只换供应商不带 --model，
    会话就还是拿旧家的模型名（比如 MiniMax-M3）去打新家的端点 —— 两边都报错，
    用户只能进会话手动 /model 再选一次。"""
    ad = agents.get(agents.agent_of(sid, cwd)) or agents.default()
    cwd = _resume_cwd(sid, cwd)      # 认 agent 用原 cwd（转录在那就查得到），开窗口才降级
    cmd = ad.launch_argv(cwd, sid=sid, model=model) if ad else None
    if not cmd:
        return None, None, "这台电脑上没装 %s" % ((ad.label or ad.key) if ad else "这个 agent")
    _prep_cwd(ad, cwd)
    # 日志按会话 sid 命名（wrapper 的 meta.update 用 extra 覆盖自己的随机 hex）：
    # 切模型/召回/发版重启后 wrapper 重生，"ab" 追加写在同一份 <sid>.log 上，
    # 终端滚动历史不再清零。升级前分家的旧 hex 日志搬一次家（见 _mirror_seed_log）。
    seed = _mirror_seed_log(sid)
    if seed:
        try:
            n = _mirror_seed_merge(sid, seed)
            _dbg("[mirror] sid=%s 续命 %d 份世代日志 %dKB ← %s"
                 % (sid[:8], len(seed), n // 1024,
                    "、".join(Path(p).stem[:8] for p in seed)))
        except OSError as e:
            _dbg("[mirror] sid=%s 续命失败：%s" % (sid[:8], str(e)[:100]))
    argv = [sys.executable, str(WRAPPER)]
    _meta = dict(extra_meta or {})
    _meta.setdefault("sid", sid)
    argv.append("--meta")
    argv.append(json.dumps(_meta, ensure_ascii=False))
    argv += ["--"] + list(cmd)
    term = _term_for_sid(sid)
    w, t, err = plat.open_wrapped(cwd, argv, name=("codeboss-%s" % sid[:8]), term=term)
    # 召回卡在哪一环：agent 认错（命令不对）、终端没权限（osascript 被拒）、窗口没起来
    # 失败永远记（召回打不开是用户能感觉到的故障）；成功那行只是排查用，跟开关走
    _f = print if err else _dbg
    _f("[open] sid=%s agent=%s term=%r cmd=%s wid=%s tty=%s err=%s"
       % (sid[:8], ad.key if ad else "-", term,
          " ".join(list(cmd)[:3])[-60:], w, t, (err or "")[:160]), flush=True)
    return w, t, err


def _term_note(sid, term):
    """把「这个会话跑在哪个终端」记到盘上（sid → term）。

    wrapper 的 meta 是按 pid 存的，会话一关 pid 就查不到了；而换模型、关闭后召回
    手里只有 sid。所以发现会话时顺手落一份 <sid>.term，跟 meta 一起随会话删除。"""
    if not sid or not term:
        return
    f = ANSI_DIR / (sid + ".term")
    try:
        if f.exists() and f.read_text(encoding="utf-8").strip() == term:
            return
        f.write_text(term, encoding="utf-8")
    except OSError:
        pass


def _term_for_sid(sid):
    """这个会话原来跑在哪个终端；没有记录返回 ""（落回 Terminal.app）。

    换模型、关闭后召回都按它回到原终端，而不是一律塞进 Terminal.app。"""
    try:
        t = (ANSI_DIR / (sid + ".term")).read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    try:
        return (_newest_meta_for_sid(sid) or {}).get("term") or ""
    except Exception:
        return ""


def _term_from_pid(pid):
    """没走 wrapper 的会话跑在哪个终端：读进程环境里的 TERM_PROGRAM（Warp=WarpTerminal）。
    只在迁移前补 <sid>.term 用——没有它，Warp 里手敲 claude 起的会话转全彩会被甩到
    Terminal.app；ps eww 看自己的进程不需要额外权限，Windows 直接返回空。"""
    if IS_WIN or not pid:
        return ""
    try:
        out = _run(["ps", "eww", "-o", "command=", "-p", str(pid)])[1]
    except Exception:
        return ""
    for t in out.split():
        if t.startswith("TERM_PROGRAM="):
            return t.split("=", 1)[1].strip()[:32]
    return ""


def _wait_resume_ready(sid, win_id=None, tty=None, timeout=18):
    """核实召回的会话真的起来了（claude 在跑 + 有全彩 meta）。
    期间只在真看到信任框时才补按键，没框就绝不乱发字符；
    看到了但选项还没画全（读不出当前选中项）就继续等，别盲按——
    默认项各版本不同，按错就把会话选成「No, exit」直接退掉。"""
    t0 = time.time()
    fixed = False
    trust_seen = 0.0
    found = meta_seen = False
    while time.time() - t0 < timeout:
        s = next((x for x in discover(force=True) if x["sid"] == sid and x.get("pid")), None)
        found = found or bool(s)
        if s and _meta_for_pid(s.get("pid")):
            return True, ""
        if not fixed and win_id:
            # 只认「这个 tty 上最新」的 meta：老窗口残留的 meta 日志里也有信任框文案，
            # 按着它按键会把字符打进一个根本没有框的新会话（2026-11-11 macmini4）。
            m = _meta_for_tty(tty) or _newest_meta_for_sid(sid)
            meta_seen = meta_seen or bool(m)
            if m and _trust_pending(m):
                if not trust_seen:
                    trust_seen = time.time()
                # 界面还在画（读不出选中项）就等；等了 10s 还读不出来就只回车
                r = _trust_confirm(tty, win_id, m,
                                   force=(time.time() - trust_seen > 10))
                _dbg("[resume] 第 %.0fs meta=%s ctl=%s 确认=%s"
                     % (time.time() - t0, (m.get("sid") or "")[:8],
                        "y" if os.path.exists(m.get("ctl") or "") else "n",
                        "y" if r else "n"))
                if r:
                    fixed = True
        time.sleep(1.2)
    # 超时要能分清三种情况：进程压根没起来 / 起来了但没全彩 meta / 卡在信任框
    print("[resume] sid=%s 等 %ss 未就绪：发现=%s meta=%s 信任框=%s"
          % (sid[:8], int(timeout), "y" if found else "n",
             "y" if meta_seen else "n", "y" if trust_seen else "n"), flush=True)
    return False, "会话 %d 秒内没起来（可能卡在启动提示）" % int(timeout)


def _claude_pretrust(cwd):
    """开会话/召回前把 claude 的首次启动流程按掉：
    - ~/.claude.json 里没有 hasCompletedOnboarding 时，claude 第一件事是
      「Choose the text style…」选界面风格。Windows 上这时没有全彩镜像，手机端
      既看不见也点不着，新会话就永远建不出来（2026-11-11 DESKTOP-CMURT6R）。
    - 目录没信任过会弹「Do you trust this folder?」（默认 No, exit），召回时
      没人在电脑前点，会话直接退出。
    直接写 claude 自己的配置位，比事后按键确认稳（文案/默认项随版本变，按错还会
    退出）。写失败就返回 False，调用方照常开、走原来的按键兜底。"""
    cwd = (cwd or "").rstrip("/\\")
    p = Path.home() / ".claude.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        changed = False
        if d.get("hasCompletedOnboarding") is not True:
            d["hasCompletedOnboarding"] = True
            d.setdefault("theme", "dark")
            changed = True
        if cwd:
            keys = {cwd}
            try:
                keys.add(os.path.realpath(cwd))     # /tmp、/var 是软链，claude 按真实路径认
            except Exception:
                pass
            if IS_WIN:
                keys.add(cwd.replace("\\", "/"))    # claude 的项目键可能是斜杠形式
            ps = d.setdefault("projects", {})
            for k in keys:
                pr = ps.get(k) or {}
                if pr.get("hasTrustDialogAccepted") is not True:
                    pr["hasTrustDialogAccepted"] = True
                    ps[k] = pr
                    changed = True
        if changed:
            tmp = p.with_name(".claude.json.cbtmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False


def _reopen_in_terminal(cwd, sid, extra_meta=None, wait=18, model=None):
    """新 Terminal 窗口开 wrapper + claude --resume，并核实起来了。返回 (ok, err)。"""
    w, t, err = _open_wrapped_terminal(cwd, sid, extra_meta, model)
    if err:
        return False, err
    if IS_WIN:
        # Windows 的召回窗口里没有 wrapper（没有 meta / tty 可核实），窗口开出来就算成功；
        # 本机窗口里用户自己操作，手机端只读转录。
        return True, ""
    if wait and wait > 0:
        return _wait_resume_ready(sid, w, t, timeout=wait)
    return True, ""


def _wrapped_by_other_host(pid):
    """这个 claude 是不是被「别的 host 实例」的 wrapper 托管着（测试实例/旧安装目录）。
    两边各看自己 logs/ansi 里的 meta，会互相把对方会话判成「没全彩」而每分钟杀掉重开；
    自动迁移时必须跳过，否则就是死循环。"""
    try:
        ppid = int(_run(["ps", "-o", "ppid=", "-p", str(pid)])[1].strip())
        cmd = _run(["ps", "-o", "command=", "-p", str(ppid)])[1]
        return "wrapper.py" in cmd and str(WRAPPER) not in cmd
    except Exception:
        return False


_MIGRATE_LOCK = threading.Lock()


def migrate_sessions(except_sids=frozenset(), protect_cwd=True, only_sid=None):
    """串行化：守护循环和页面按钮可能同时触发，避免同一个会话被两边杀。"""
    if IS_WIN:
        return [], [{"sid": "*", "title": "",
                     "why": "Windows 没有可接管的终端窗口，会话都在 Codeboss 里直接开"}], []
    if not _MIGRATE_LOCK.acquire(blocking=False):
        return [], [{"sid": "*", "title": "", "why": "迁移进行中，稍后再试"}], []
    try:
        return _migrate_sessions(except_sids, protect_cwd, only_sid)
    finally:
        _MIGRATE_LOCK.release()


_MIG_PENDING = HERE / "logs" / "migrate_pending.json"


def _mig_pending(sid, cwd, on):
    """转全彩是「先 kill 再 --resume 重开」：中间这一二十秒里服务要是被自更新/守护
    重启打断，会话就再没人拉起来了（2026-11-14 linux-test：kill 完主进程被重启，
    整条会话从列表里消失）。杀之前先落一条待重开，开机后补一次。"""
    try:
        d = json.loads(_MIG_PENDING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if on:
        d[sid] = {"cwd": cwd, "at": time.time()}
    else:
        d.pop(sid, None)
    try:
        _MIG_PENDING.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _mig_pending_retry():
    """开机补开上次没来得及重开的会话（照 24 小时内、发现列表里没有的）。"""
    try:
        d = json.loads(_MIG_PENDING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not d:
        return []
    try:
        live = {x["sid"] for x in discover(force=True)}
    except Exception:
        live = set()
    done = []
    for sid, e in d.items():
        if sid in live or time.time() - float(e.get("at") or 0) > 86400:
            continue
        try:
            ok, err = _reopen_in_terminal(e.get("cwd") or str(Path.home()), sid, wait=25)
            if not ok:
                print("[migrate] 补开 %s 失败：%s" % (sid[:8], (err or "")[:100]), flush=True)
                continue
            done.append(sid[:8])
        except Exception as ex:
            print("[migrate] 补开 %s 出错：%s" % (sid[:8], str(ex)[:100]), flush=True)
    try:
        _MIG_PENDING.write_text("{}", encoding="utf-8")   # 只补一次，失败就交给「召回」
    except OSError:
        pass
    if done:
        print("[migrate] 开机补开：%s" % ",".join(done), flush=True)
    return done


def _migrate_sessions(except_sids=frozenset(), protect_cwd=True, only_sid=None):
    """把空闲的未接 wrapper 会话转全彩：kill（转录已逐条落盘，历史不丢）→ 关旧窗 → wrapper --resume 重开。
    正在运行的（state 非 idle）不动。only_sid=单会话迁移（页面按钮点名，基建保护不适用）。"""
    import time as _time
    done, skipped, failed = [], [], []
    hit = False              # only_sid 点名的会话到底在不在 discover 里（不在=页面列表过期）
    for x in discover(force=True):
        sid, wid = x["sid"], x.get("window_id")
        if only_sid and sid != only_sid:
            continue
        hit = True
        if (x.get("agent") or "claude") != "claude":
            if only_sid:
                skipped.append({"sid": sid[:8], "title": x["title"],
                                "why": "这是 %s 会话，转全彩只支持 Claude" % (x.get("agent") or "其他 agent")})
            continue  # 迁移是 Claude 专属流程（kill + claude --resume 重开），别的 agent 不动
        if sid in except_sids or _meta_for_pid(x.get("pid")):
            if only_sid:
                skipped.append({"sid": sid[:8], "title": x["title"],
                                "why": "已经是全彩镜像，无需再转"})
            continue  # 保护名单 / 已是全彩
        if not only_sid and _wrapped_by_other_host(x.get("pid")):
            continue  # 别的 host 实例托管的会话，别去抢（否则两边互相杀）
        if not only_sid and x.get("state") not in ("idle", ""):
            skipped.append({"sid": sid[:8], "title": x["title"],
                            "why": "正在运行(%s)，稍后自动重试" % (x.get("state") or "未知")})
            continue
        if not only_sid and protect_cwd and (x.get("cwd") or "").startswith(PROTECT_CWD_PREFIX):
            skipped.append({"sid": sid[:8], "title": x["title"], "why": "基建会话，保护不迁"})
            continue
        if not wid:
            # 第三方终端（Warp/iTerm/VS Code…）抓不到窗口 id。只要知道它原来跑在哪个终端
            # （meta 侧车，或者进程环境里的 TERM_PROGRAM），迁完能回到同一个终端，就跟
            # Terminal 的会话一样自动迁；判断不出来才跳过——那种迁完只会被甩到 Terminal.app。
            if not only_sid and not (_term_for_sid(sid) or _term_from_pid(x.get("pid"))):
                skipped.append({"sid": sid[:8], "title": x["title"],
                                "why": "第三方终端，判断不出原终端，自动跳过（可手动点 🔄 转全彩）"})
                continue
        cwd = x.get("cwd") or str(Path.home())
        # 卷不可用（TCC 弹窗没人点/盘休眠）时绝不迁：wrapper 里的 claude 会被同一张
        # 弹窗挡死，杀了活会话却拉不起来（2026-11-26 威刚盘 TCC 悬挂实测）。
        if not _vol_ok(cwd):
            skipped.append({"sid": sid[:8], "title": x["title"], "why": "项目盘不可用，稍后再迁"})
            continue
        # 没有任何转录的新会话，--resume 根本拉不起来（杀完就找不回来）——一律不迁
        jp = project_jsonl(cwd, sid)
        try:
            has_hist = bool(jp) and os.path.getsize(jp) > 200
        except OSError:
            has_hist = False
        if not has_hist:
            skipped.append({"sid": sid[:8], "title": x["title"], "why": "暂无转录（新会话），不迁"})
            continue
        # 不是在 App 里拉起的会话没有 meta（也就没有 <sid>.term）：迁移前从进程环境补一份，
        # 不然 Warp 里手敲 claude 起的会话一转就被甩到 Terminal.app 去。
        if not _term_for_sid(sid):
            _term_note(sid, _term_from_pid(x.get("pid")))
        # 直接 TERM 结束 claude（转录逐条落盘，不丢历史；/exit 会被 TUI 当普通输入提交，不可靠）
        _mig_pending(sid, cwd, True)
        _run(["kill", str(x["pid"])])
        gone = False
        for _ in range(8):
            _time.sleep(0.5)
            code, out, _ = _run(["ps", "-o", "pid=", "-p", str(x["pid"])])
            if not out.strip():
                gone = True
                break
        if not gone:
            _run(["kill", "-9", str(x["pid"])])
            _time.sleep(1)
            code, out, _ = _run(["ps", "-o", "pid=", "-p", str(x["pid"])])
            gone = not out.strip()
        if not gone:
            _mig_pending(sid, cwd, False)
            skipped.append({"sid": sid[:8], "title": x["title"], "why": "进程杀不掉，保守跳过"})
            continue
        # 关旧窗口（关不掉就留个日志，别把窗口留在屏幕上）
        _close_killed_window(x)
        ok, err = _reopen_in_terminal(cwd, sid, wait=(25 if only_sid else 10))
        if not ok:
            # 杀都杀了却没开起来：标记留着，下次启动补开一次（否则这条会话就没了）
            failed.append({"sid": sid[:8], "title": x["title"], "why": ((err or "") or "重开失败")[:120]})
            continue
        _mig_pending(sid, cwd, False)
        done.append({"sid": sid[:8], "title": x["title"]})
    if only_sid and not hit:
        # 页面列表是几秒前的快照，用户点的时候会话可能已经退出/重开了
        skipped.append({"sid": only_sid[:8], "title": "",
                        "why": "会话列表里没找到它（可能已退出或刚重开），刷新后再试"})
    return done, skipped, failed


async def h_ext_migrate(request):
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    done, skipped, failed = await loop.run_in_executor(
        None, lambda: migrate_sessions(except_sids=set(data.get("except") or [])))
    return web.json_response({"ok": True, "migrated": done, "skipped": skipped, "failed": failed})


async def h_ext_migrate_one(request):
    """单会话转全彩（列表页/会话页按钮；已全彩的不会有这个按钮）"""
    await require_token(request)
    sid = request.match_info["sid"]
    loop = asyncio.get_event_loop()
    done, skipped, failed = await loop.run_in_executor(
        None, lambda: migrate_sessions(only_sid=sid))
    return web.json_response({"ok": True, "migrated": done, "skipped": skipped, "failed": failed})


# ---- cc-switch 模型切换（db 只读；AUTH_TOKEN 只存在于进程内存与 0600 env 文件，
#      绝不进 API 响应 / 日志 / do script 命令行） ----
CC_DB = Path.home() / ".cc-switch" / "cc-switch.db"


def _cc_providers(with_secret=False):
    """cc-switch 的 claude provider 列表。默认只含 id/name/base_url/model/档位；
    with_secret=True 仅进程内切换路径使用（env 含 AUTH_TOKEN，写出接口即事故）。"""
    import sqlite3
    if not CC_DB.exists():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=3)
        rows = con.execute("SELECT id,name,settings_config FROM providers"
                           " WHERE app_type='claude' ORDER BY sort_index").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for pid, name, sc in rows:
        try:
            env = (json.loads(sc) or {}).get("env") or {}
        except Exception:
            continue
        bu = env.get("ANTHROPIC_BASE_URL") or ""
        if not bu:
            continue        # Claude Official 走登录态，没有 base_url，不参与切换
        p = {"id": pid, "name": name, "base_url": bu,
             "model": env.get("ANTHROPIC_MODEL") or "",
             "tiers": {t: env.get("ANTHROPIC_DEFAULT_%s_MODEL" % t.upper()) or ""
                       for t in ("haiku", "sonnet", "opus")}}
        if with_secret:
            p["env"] = env
        out.append(p)
    return out


# ---- Codex 的 provider（cc-switch codex 页）与当前模型 ----
# codex 的 settings_config 形状和 claude 不同：{"auth":{"OPENAI_API_KEY":…},
# "config":"<config.toml 全文>"}。base_url/model 要从那段 TOML 里读。

def _toml_provider_id(text):
    """config.toml 顶层 model_provider = "xxx" 的 xxx。"""
    m = re.search(r'(?m)^model_provider\s*=\s*"([^"]+)"', text or "")
    return m.group(1) if m else ""


def _toml_top(text, key, section=""):
    """读 TOML 里 [section] 下的 key = "值"（section 空 = 顶层）。

    只认 Codex 配置用得到的单行字符串，读不到就返回空串——这里只用来「看」，
    真正写配置时另行处理（见 _toml_set_top / _toml_block）。"""
    if section:
        m = re.search(r'(?m)^\[' + re.escape(section) + r'\]\s*$', text or "")
        if not m:
            return ""
        text = text[m.end():]
    nxt = re.search(r'(?m)^\[', text or "")
    if nxt:
        text = text[:nxt.start()]
    m = re.search(r'(?m)^' + re.escape(key) + r'\s*=\s*"([^"]*)"', text or "")
    return m.group(1) if m else ""


def _cc_codex_providers(with_secret=False):
    """cc-switch 的 codex provider 列表。默认只有 id/name/base_url/model。

    with_secret=True 时会带上 auth（OPENAI_API_KEY）与配置全文，只给进程内切换路径用，
    绝不进任何接口响应。"""
    import sqlite3
    if not CC_DB.exists():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=3)
        rows = con.execute("SELECT id,name,settings_config FROM providers"
                           " WHERE app_type='codex' ORDER BY sort_index").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for pid, name, sc in rows:
        try:
            cfg = json.loads(sc) or {}
        except Exception:
            continue
        text = cfg.get("config") or ""
        dev = _toml_provider_id(text)
        bu = _toml_top(text, "base_url", "model_providers." + dev) if dev else ""
        if not bu:
            continue        # 没有 base_url 的（官方登录态）不参与切换
        p = {"id": pid, "name": name, "base_url": bu,
             "model": _toml_top(text, "model"), "tiers": {}}
        if with_secret:
            p["auth"] = cfg.get("auth") or {}
            p["config"] = text
        out.append(p)
    return out


def _codex_machine_model():
    """这台机器上 codex 当前生效的 base_url/model（~/.codex/config.toml，只读）。"""
    try:
        text = (agent_codex.codex_home() / "config.toml").read_text(encoding="utf-8")
    except Exception:
        return {"base_url": "", "model": ""}
    dev = _toml_provider_id(text)
    return {"base_url": _toml_top(text, "base_url", "model_providers." + dev) if dev else "",
            "model": _toml_top(text, "model")}


def _toml_set_top(text, key, value):
    """把 config.toml 顶层的 key = "value" 换掉；没有就插到第一个 [表] 之前。
    其余内容（notify / projects / 别的 provider 块）一个字节不动。"""
    line = '%s = %s' % (key, json.dumps(value, ensure_ascii=False))
    m = re.search(r'(?m)^' + re.escape(key) + r'\s*=\s*"[^"]*"[^\n]*$', text)
    if m:
        return text[:m.start()] + line + text[m.end():]
    nxt = re.search(r'(?m)^\[', text)
    if nxt:
        head, tail = text[:nxt.start()], text[nxt.start():]
        if head and not head.endswith("\n"):
            head += "\n"
        return head + line + "\n\n" + tail
    return (text if text.endswith("\n") or not text else text + "\n") + line + "\n"


def _toml_block(text, header):
    """取 [header] 这一整块（含表头，到下一个顶层 [表] 为止）；没有返回空串。"""
    m = re.search(r'(?m)^\[' + re.escape(header) + r'\]\s*$', text or "")
    if not m:
        return ""
    rest = text[m.start():]
    nxt = re.search(r'(?m)^\[', rest[1:])
    return rest[:nxt.start() + 1] if nxt else rest


def _toml_put_block(text, header, block):
    """把 [header] 块换成 block；原来没有就在文件末尾追加。"""
    m = re.search(r'(?m)^\[' + re.escape(header) + r'\]\s*$', text or "")
    if not m:
        return (text.rstrip("\n") + "\n\n" + block.strip("\n") + "\n") if text.strip() \
            else block.strip("\n") + "\n"
    rest = text[m.start():]
    nxt = re.search(r'(?m)^\[', rest[1:])
    end = m.start() + (nxt.start() + 1 if nxt else len(rest))
    return text[:m.start()] + block.strip("\n") + "\n\n" + text[end:]


def _machine_model():
    """机器级 claude 当前实际生效的 base_url/model（~/.claude/settings.json，只读；
    密钥字段不出本函数）。
    model 取值顺序同 claude 自己：env.ANTHROPIC_MODEL 压过 /model 存的 settings.model
    （后者是 /model 命令写进去的「新会话默认」）。"""
    try:
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text()) or {}
        env = cfg.get("env") or {}
        return {"base_url": env.get("ANTHROPIC_BASE_URL") or "",
                "model": env.get("ANTHROPIC_MODEL") or cfg.get("model") or ""}
    except Exception:
        return {"base_url": "", "model": ""}


# ---- 没有 cc-switch 时的原生模型切换（等同 claude 自己的 /model）----
# /model 把选择写进 settings.json 的 model 字段（别名 default/opus/sonnet/haiku
# 或完整模型名）；env.ANTHROPIC_MODEL 优先级更高，设了它就压住 settings.model。
NATIVE_ID = "__native__"
NATIVE_TIERS = {"haiku": "最快", "sonnet": "均衡", "opus": "最强"}


def _native_provider():
    m = _machine_model()
    return {"id": NATIVE_ID, "name": "Claude 内置", "base_url": m["base_url"],
            "model": m["model"], "current": True,
            "tiers": dict(NATIVE_TIERS)}


def _native_apply(tier):
    """写 settings.json：有档位写 model（与 /model 同款），空档=清回默认。原子替换。"""
    sp = Path.home() / ".claude" / "settings.json"
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:
        cfg = {}
    env = dict(cfg.get("env") or {})
    if tier:
        cfg["model"] = tier
        if env.get("ANTHROPIC_MODEL"):
            # 它优先级更高，不同步就会继续压住 settings.model（改了也不生效）
            env["ANTHROPIC_MODEL"] = tier
    else:
        cfg.pop("model", None)
        env.pop("ANTHROPIC_MODEL", None)
    cfg["env"] = env
    sp.parent.mkdir(parents=True, exist_ok=True)   # 新机器还没跑过 claude：~/.claude 可能不存在
    tmp = sp.with_name("settings.json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, sp)


def session_model(sid):
    """会话当前生效模型：手动切换记录（每会话 env 注入）优先，否则机器默认。"""
    rec = _model_get(sid)
    if rec:
        return rec
    m = _machine_model()
    prov = next((p for p in _cc_providers() if p["base_url"] == m["base_url"]), None)
    return {"provider_id": (prov or {}).get("id") or "",
            "provider_name": (prov or {}).get("name") or ("官方登录" if not m["base_url"] else "自定义"),
            "base_url": m["base_url"], "model": m["model"], "tier": "", "via": "machine"}


_PV_LAST = [0]        # 上次打印[page_ver]时的版本：只在变了时打，别刷日志
_PV_BUILD = [None, ""]  # (v, PAGE_BUILD)：页面里的构建戳，随 v 缓存
_PV_STALE = {}        # 页面自报的旧构建 -> v：同一版本只报一次，别刷日志


def _page_build(v):
    """host 现在要下发的那份 ext.html 的构建戳（PAGE_BUILD='…'），按 v 缓存一次。"""
    if _PV_BUILD[0] == v:
        return _PV_BUILD[1]
    try:
        m = re.search(r"PAGE_BUILD='([^']*)'", _static_text("ext.html"))
        b = m.group(1) if m else ""
    except Exception:
        b = ""
    _PV_BUILD[0], _PV_BUILD[1] = v, b
    return b


async def h_page_ver(request):
    """ext.html 的版本（mtime_ns）：App 前端拿它判断内存里的页面是不是旧的，
    改过就在回前台/每分钟检查时自动 reload。"""
    await require_token(request)
    # no-store：这一个是「页面是不是旧的」的判据，被 WebView 缓存住就等于永远不更新
    #（2026-11 实测：开发机升了版本，手机上还一直拿旧的 v，页面停在老代码）。
    v = _static_ver()
    b = _page_build(v)
    pb = (request.query.get("b") or "")[:24]
    # 只在版本变了时打一行：每台手机每分钟一次全打会把日志刷满，但「手机跑的哪一版」
    # 出问题时必须一眼能看到（b= 是页面自报的构建戳，老页面没有）
    if v != _PV_LAST[0]:
        _PV_LAST[0] = v
        print("[pver] %s v=%s b=%s now=%s ip=%s" % (
            time.strftime("%m-%d %H:%M:%S"), v, pb or "-", b or "-",
            request.remote or "-"), flush=True)
    # 自报的构建和现在要下发的不一样 = 这一份页面是旧的。v 没再变它就永远等不到
    # reload（reload 之后又被中间缓存塞回旧 HTML 时就是这么卡死的），单独报一行。
    if pb and b and pb != b and _PV_STALE.get(pb) != v:
        _PV_STALE[pb] = v
        print("[pver] 旧页面 v=%s 跑=%s 现在=%s ip=%s" % (
            v, pb, b, request.remote or "-"), flush=True)
    return web.json_response({"ok": True, "v": v, "b": b},
                             headers={"Cache-Control": "no-store, must-revalidate"})




def _default_agent():
    """请求没指定 agent 时，按这台机器实际装了什么来定。

    Claude 装了就用 Claude（老行为）；没装 Claude 但装了别的（Codex）就用它——
    手机端只有一种 agent 时不带 ?agent=（切换条被隐藏），硬默认 Claude 会让
    「当前模型 / 切模型」显示一台根本没有 Claude 的机器上的 Claude。
    一个都没探测到（老版本 / 探测失败）照旧 Claude。"""
    on = []
    for ad in agents.adapters():
        try:
            if ad.installed():
                on.append(ad.key)
        except Exception:
            pass
    if agents.DEFAULT_AGENT in on or not on:
        return agents.DEFAULT_AGENT
    return on[0]


async def h_ext_model(request):
    """provider 列表 + 机器当前模型（全局一个供应商）。响应里只有非密钥字段。
    没装 cc-switch 时给一个原生 provider（档位就是 claude 自己的 /model 别名），
    这样「看当前模型 / 切模型」在不装 cc-switch 的机器上也能用。
    ?agent=codex 看 Codex 的（cc-switch codex 页 + ~/.codex/config.toml）。"""
    await require_token(request)
    agent = (request.query.get("agent") or "").strip().lower() or _default_agent()
    print("[model] 读取请求 agent=%s ip=%s ua=%s" % (agent, request.remote or "?",
          (request.headers.get("User-Agent") or "")[:60]), flush=True)
    ad = None
    if agent == "codex":
        provs = _cc_codex_providers()
        m = _codex_machine_model()
    elif agent in ("", "claude"):
        provs = _cc_providers()
        m = _machine_model()
        if not provs:
            provs = [_native_provider()]
    else:
        # 以后加的 agent（hermes / openclaw…）：只要适配器能报当前模型就先显示，
        # 供应商切换还没接的给空列表，页面会明说「还没配」。
        ad = agents.get(agent)
        provs = []
        try:
            m = {"base_url": "", "model": (ad.model_info() if ad else "") or ""}
        except Exception:
            m = {"base_url": "", "model": ""}
    for p in provs:
        p["current"] = (p["base_url"] == m["base_url"])
    # 同 base_url 可能挂多个供应商（两个百炼共用一个端点）：current 优先取
    # model 也对得上的那个，否则列表高亮/切换 sheet 预选会选错行。
    cands = [p for p in provs if p["current"]]
    cur = (next((p for p in cands
                 if p.get("model") == m["model"] or (p.get("tiers") or {}).get(p.get("tier") or "") == m["model"]
                 or (p.get("env_model") or "") == m["model"]), None)
           or (cands[0] if cands else None))
    if cands and cur is not None:
        for p in cands:
            p["current"] = (p is cur)
    cur_name = (cur or {}).get("name", "")
    if not cur_name:
        if agent in ("", "claude", "codex"):
            cur_name = "官方登录" if not m["base_url"] else "自定义"
        else:
            # 别的 agent 还没接供应商切换：有模型就把它的显示名摆出来，别冒充「官方登录」
            cur_name = (ad.label or agent) if m.get("model") else ""
    return web.json_response({"ok": True, "agent": agent, "providers": provs, "machine": m,
                              "current": {"provider_id": (cur or {}).get("id", ""),
                                          "provider_name": cur_name,
                                          "model": m["model"],
                                          "base_url": m["base_url"]},
                              "pending": len(_model_pending())})


def _cc_env_keys():
    """cc-switch 里所有 claude 供应商 env 出现过的键。
    这些键全是「供应商相关」的，切换时必须先整体清掉再加新的——否则上一家的
    键会残留（CLAUDE_CODE_SUBAGENT_MODEL、*_MODEL_NAME、API_TIMEOUT_MS…），
    新供应商拿旧模型名去请求，配置就「串味」了。"""
    keys = set()
    for p in _cc_providers(with_secret=True):
        keys.update((p.get("env") or {}).keys())
    return keys


def _cc_set_current(pid, app="claude"):
    """告诉 cc-switch「现在选中的是这个供应商」（app = claude / codex）。

    ⚠️ 这一步一步都不能省。cc-switch 有 sync_current_providers_live：它会把
    ~/.claude/settings.json 里的**实际 env** 回写进「它认为的当前供应商」记录。
    我们只改 settings.json、不改这个指针的话，cc-switch 会把新供应商的连接信息
    写进**上一个**供应商的记录里 —— 2026-11-06 实锤：Zhipu GLM 和
    Bailian For Glm5.3 的 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 被换成了
    DeepSeek 的（`provider_endpoints` 里还留着它们原本的 URL，说明记录被覆盖）。

    用应用自己那套字段写（providers.is_current + 应用设置里的 currentProviderClaude）。
    失败只记一行，不阻断切换（没装/没跑 cc-switch 时就是空操作）。
    """
    import sqlite3 as _sq
    ok = True
    try:
        con = _sq.connect(str(CC_DB), timeout=3)
        try:
            con.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (app,))
            con.execute("UPDATE providers SET is_current=1 WHERE id=? AND app_type=?",
                        (pid, app))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        ok = False
        print("[model] 回写 cc-switch 当前供应商失败（db）：%s" % str(e)[:100], flush=True)
    sp = CC_DB.parent / "settings.json"
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
        cfg["currentProviderClaude" if app == "claude" else "currentProviderCodex"] = pid
        tmp = sp.with_name("settings.json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, sp)
    except Exception as e:
        ok = False
        print("[model] 回写 cc-switch 当前供应商失败（设置）：%s" % str(e)[:100], flush=True)
    return ok


def _codex_apply_provider(prov):
    """把 cc-switch 的 codex 供应商落到这台机器上（~/.codex/config.toml + auth.json）。

    和 cc-switch 本体的差别：只换「当前供应商/模型」和那一个 [model_providers.x] 块，
    其余内容（notify 提醒、[projects] 信任目录、别的供应商块）一个字节不动——
    整份覆盖会把用户自己改过的东西冲掉。
    返回 (ok, 说明)。"""
    home = agent_codex.codex_home()
    cfg = home / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    except OSError as e:
        return False, "读不了 ~/.codex/config.toml：%s" % str(e)[:80]
    src = prov.get("config") or ""
    dev = _toml_provider_id(src) or "custom"
    block = _toml_block(src, "model_providers." + dev)
    if not block.strip():
        block = ('[model_providers.%s]\nname = %s\nbase_url = %s\n'
                 'wire_api = "responses"\nrequires_openai_auth = true\n'
                 % (dev, json.dumps(prov["name"], ensure_ascii=False),
                    json.dumps(prov["base_url"], ensure_ascii=False)))
    new = _toml_put_block(text, "model_providers." + dev, block)
    new = _toml_set_top(new, "model_provider", dev)
    model = (prov.get("model") or "").strip()
    if model:
        new = _toml_set_top(new, "model", model)
    if new != text:
        # 写坏了 codex 直接起不来（duplicate key 之类），宁可不切也不能把配置写死
        if not agent_codex._toml_valid(new):
            return False, "新配置解析不过（config.toml 里有重复段？），已放弃写入"
        try:
            home.mkdir(parents=True, exist_ok=True)
            tmp = cfg.with_name("config.toml.cbtmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, cfg)
        except OSError as e:
            return False, "写不了 ~/.codex/config.toml：%s" % str(e)[:80]
    auth = prov.get("auth") or {}
    if auth:
        ap = home / "auth.json"
        try:
            tmp = ap.with_name("auth.json.cbtmp")
            tmp.write_text(json.dumps(auth, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, ap)
        except OSError as e:
            return False, "写不了 ~/.codex/auth.json：%s" % str(e)[:80]
    _cc_set_current(prov["id"], "codex")
    return True, ""


def _codex_probe(prov):
    """落盘前验一眼这家 codex 供应商通不通（密钥/余额/线路）。
    返回 None=通，str=给手机端看的原因。发最小 responses 流式请求，HTTP 状态码
    一到就断开：不消费正文，花不了几个 token，也躲开各家流式实现的差异。
    （2026-11-14 实锤：DeepSeek 余额没了照样「切换成功」，之后 codex 每句话
    402 Insufficient Balance——手机上就是「切完模型不可用」。探测不过就不写盘。）"""
    import urllib.request
    import urllib.error
    key = (prov.get("auth") or {}).get("OPENAI_API_KEY") or ""
    body = json.dumps({"model": prov.get("model") or "", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "max_output_tokens": 16, "stream": True}).encode()
    req = urllib.request.Request(
        prov["base_url"].rstrip("/") + "/responses", data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        r.close()
        return None
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8", "replace"))
            detail = (d.get("error") or {}).get("message") or d.get("message") or ""
        except Exception:
            detail = ""
        why = {401: "密钥无效", 402: "余额不足", 403: "无权限", 404: "接口路径不对",
               429: "用量超限"}.get(e.code, "HTTP %s" % e.code)
        return why + (("：" + detail[:80]) if detail else "")
    except Exception as e:
        return "连不上（%s）" % str(e)[:60]


async def _codex_model_switch(data):
    """手机端切 Codex 的供应商/模型。

    和 Claude 不一样，这里**不重启**正在跑的会话：codex 在启动时读配置，
    中途打断有风险；新开/召回的会话直接用新供应商。返回里 via=new 让页面说清楚。
    切之前先探测（_codex_probe）：不通就不写盘，手机上当场看到原因。"""
    prov = next((p for p in _cc_codex_providers(with_secret=True)
                 if p["id"] == data.get("provider_id")), None)
    if not prov:
        return web.json_response({"ok": False, "error": "Codex 供应商不存在（cc-switch 未配？）"})
    m = _codex_machine_model()
    if prov["base_url"] == m.get("base_url") and (not prov["model"] or prov["model"] == m.get("model")):
        return web.json_response({"ok": True, "via": "noop", "agent": "codex",
                                  "error": "已是该模型", "provider_name": prov["name"],
                                  "model": m.get("model")})
    bad = await asyncio.get_event_loop().run_in_executor(None, _codex_probe, prov)
    if bad:
        print("[model] codex 探测未过 prov=%r %s" % (prov["name"], bad), flush=True)
        return web.json_response({"ok": False, "agent": "codex",
                                  "error": "%s 用不了：%s，没切" % (prov["name"], bad)})
    ok, why = _codex_apply_provider(prov)
    if not ok:
        return web.json_response({"ok": False, "error": why})
    m = _codex_machine_model()
    print("[model] codex 已切换 prov=%r model=%r" % (prov["name"], m.get("model")), flush=True)
    return web.json_response({"ok": True, "agent": "codex", "via": "new",
                              "provider_name": prov["name"], "model": m.get("model")})


def _apply_cc_env(env):
    """把目标供应商 env 写进 ~/.claude/settings.json（与 cc-switch 点切换同语义：
    机器级生效，之后新起的 claude 进程都用该供应商）。原子替换，密钥不落日志。

    不是「合并」：先把所有供应商相关的键整体清掉，再原样铺上目标的 env。
    以前的 `merged.update(env)` 只加不减，换供应商时上一家的私有键会留在这里
    （Bailian 的 CLAUDE_CODE_SUBAGENT_MODEL=qwen3.8-max 之类），也算一种配置损坏。
    """
    sp = Path.home() / ".claude" / "settings.json"
    try:
        cur = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:
        cur = {}
    merged = dict(cur.get("env") or {})
    for k in _cc_env_keys() | set(env):
        merged.pop(k, None)
    merged.update(env)
    cur["env"] = merged
    # settings.model 是「/model 留下的新会话默认」，模型名是跟着供应商走的，
    # 供应商一换它就作废；留着还会被 cc-switch 的 live 同步写回供应商记录。
    cur.pop("model", None)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name("settings.json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, sp)


_MODEL_SWITCH_LOCK = threading.Lock()


def _session_model_options(agent):
    """当前机器供应商下能选的模型档位（会话级切换只换模型，不换供应商）。
    agent 不支持（can_model_switch=False）→ 空：手机端把档位条藏掉。"""
    ad = agents.get(agent)
    if ad is not None and not getattr(ad, "can_model_switch", False):
        return []
    out = [{"tier": "", "label": "跟机器默认", "model": ""}]
    if agent == "codex":
        m = _codex_machine_model()
        if m.get("model"):
            out.append({"tier": "default", "label": m["model"], "model": m["model"]})
        return out
    prov = next((p for p in _cc_providers(with_secret=True)
                 if p["base_url"] == _machine_model()["base_url"]), None)
    if prov:
        for t in ("haiku", "sonnet", "opus"):
            mm = (prov.get("tiers") or {}).get(t) or ""
            if mm:
                out.append({"tier": t, "label": "%s（%s）" % (t, mm), "model": mm})
        env = prov.get("env") or {}
        base = env.get("ANTHROPIC_MODEL") or prov.get("model") or ""
        if base and all(x["model"] != base for x in out):
            out.append({"tier": "provider", "label": "供应商默认（%s）" % base, "model": base})
    return out


async def _find_any_session(sid):
    """先找活着/占位中的；找不到再从「已退出可召回」里找（会话级模型对已退出的也要能设）。"""
    s = await _find_session(sid)
    if s:
        return s
    loop = asyncio.get_event_loop()
    live = {x["sid"] for x in await loop.run_in_executor(None, _tick_sessions)}
    return await loop.run_in_executor(
        None, lambda: next((x for x in _closed_sessions(live) if x["sid"] == sid), None))


async def h_ext_model_session(request):
    """看/切「这一个会话」的模型（cc-switch B）。机器级供应商不动，只给会话传 --model。"""
    await require_token(request)
    sid = (request.query.get("sid") or "").strip()
    s = await _find_any_session(sid) if sid else None
    if not s:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    agent = s.get("agent") or "claude"
    rec = _model_get(sid) or {}
    machine = _codex_machine_model() if agent == "codex" else _machine_model()
    return web.json_response({"ok": True, "sid": sid, "agent": agent,
                              "state": s.get("state") or "",
                              "current": rec, "machine": machine,
                              "options": _session_model_options(agent),
                              "model": session_model(sid).get("model") or ""})


async def h_ext_model_session_set(request):
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    s = await _find_any_session(sid) if sid else None
    if not s:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    agent = s.get("agent") or "claude"
    ad = agents.get(agent)
    if ad is not None and not getattr(ad, "can_model_switch", False):
        return web.json_response(
            {"ok": False, "agent": agent,
             "error": "%s 暂不支持在手机端切模型" % (ad.label or agent)})
    prev = _model_get(sid)          # 回滚用：换失败时恢复原来的会话记录
    tier = (data.get("tier") or "").strip().lower()
    opts = _session_model_options(agent)
    model = (data.get("model") or "").strip()
    if tier:
        model = next((o["model"] for o in opts if o["tier"] == tier), "") or ""
    if not tier and not model:                       # 回到机器默认
        _model_clear(sid)
    else:
        if not model:
            return web.json_response({"ok": False, "error": "这个档位没有对应的模型"})
        machine = _codex_machine_model() if agent == "codex" else _machine_model()
        prov = next((p for p in (_cc_codex_providers() if agent == "codex" else _cc_providers())
                     if p["base_url"] == machine.get("base_url")), None)
        rec = {"provider_id": (prov or {}).get("id", ""),
               "provider_name": (prov or {}).get("name", "") or machine.get("base_url", ""),
               "base_url": machine.get("base_url", ""),
               "model": model, "tier": tier, "via": "session"}
        _model_set(sid, rec)
    target = model if (tier or model) else None
    st = (s.get("state") or "")
    if st in ("exited", "gone"):
        # 已退出的会话没有进程可重开：记下来，下次召回时用 --model 带上
        rec = _model_get(sid)
        if rec:
            _model_set(sid, dict(rec, via="session"))
        return web.json_response({"ok": True, "saved": True, "sid": sid,
                                  "model": target or "", "note": "已记录，召回时生效"})
    if st not in ("idle", ""):
        # 正在干活：不打断，记成 pending，由 _model_switch_loop 空闲后重开
        old = _model_get(sid)
        _model_set(sid, dict(old or {}, via="pending"))
        return web.json_response({"ok": True, "queued": True, "sid": sid, "model": target or "",
                                  "note": "会话在跑，空闲后自动换模型"})
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, _restart_session_for_model, s, target)
    if not ok and target:
        # 带 --model 重开失败（比如目录信任框卡住）：至少按原样把会话拉回来，别让人丢会话
        ok2, err2 = await loop.run_in_executor(None, _restart_session_for_model, s, None)
        if ok2:
            if prev:
                _model_set(sid, prev)
            else:
                _model_clear(sid)     # 新模型没生效，别让记录骗人
            return web.json_response({"ok": False,
                                      "error": "换模型失败（%s），已按原样拉回会话" % (err or "")[:80]})
        return web.json_response({"ok": False, "error": "换模型失败（%s），会话也没拉回来：%s"
                                  % ((err or "")[:60], (err2 or "")[:60])})
    if not ok:
        return web.json_response({"ok": False, "error": err or "重开会话失败"})
    rec = _model_get(sid)
    if rec:
        _model_set(sid, dict(rec, via="restart"))
    return web.json_response({"ok": True, "sid": sid, "model": target or ""})


async def h_ext_model_switch(request):
    """切换模型供应商（全局一个，cc-switch 同款语义）。

    实测（2026-11-03）：运行中的 claude **不会**重读 ~/.claude/settings.json ——
    改完文件继续打旧 base_url；只有新起的进程才认。所以「切服务商」必须让会话
    重启才会生效：写机器级 settings.json → kill → wrapper --resume 重开。
    转录在盘上，--resume 后历史不丢。

    忙会话（正在跑）不打断：排队等它空闲，由 _model_switch_loop 后台补切。
    """
    await require_token(request)
    data = await request.json()
    agent = (data.get("agent") or "").strip().lower() or _default_agent()
    if agent == "codex":
        return await _codex_model_switch(data)
    ad = agents.get(agent)
    if ad is not None and not getattr(ad, "can_model_switch", False):
        return web.json_response(
            {"ok": False, "agent": agent,
             "error": "%s 暂不支持在手机端切模型" % (ad.label or agent)})
    tier = (data.get("tier") or "").strip().lower()
    native = data.get("provider_id") == NATIVE_ID
    print("[model] 切换请求 prov=%r tier=%r" % (data.get("provider_id"), tier), flush=True)
    if native:
        # 没 cc-switch 的机器：等同 claude 自己的 /model——写 settings.json 的 model 字段
        if tier and tier not in NATIVE_TIERS:
            return web.json_response({"ok": False, "error": "未知档位: %s" % tier})
        prov = {"id": NATIVE_ID, "name": "Claude 内置", "base_url": _machine_model()["base_url"]}
        env, model = None, (tier or "默认")
    else:
        prov = next((p for p in _cc_providers(with_secret=True)
                     if p["id"] == data.get("provider_id")), None)
        if not prov:
            return web.json_response({"ok": False, "error": "provider 不存在（cc-switch 未装或未配 claude 供应商？）"})
        env = dict(prov["env"])
        model = prov["tiers"].get(tier, "") or env.get("ANTHROPIC_MODEL") or prov["model"]
        env["ANTHROPIC_MODEL"] = model
        if tier in ("haiku", "sonnet", "opus"):
            env["ANTHROPIC_DEFAULT_%s_MODEL" % tier.upper()] = model
        cur = _machine_model()
        if prov["base_url"] == cur.get("base_url") and model == cur.get("model"):
            return web.json_response({"ok": True, "via": "noop", "error": "已是该模型",
                                      "provider_name": prov["name"], "model": model})

    if not _MODEL_SWITCH_LOCK.acquire(blocking=False):
        # 客户端 dc 超时（60s）回退中继会再发一次同款请求；切换正在跑时必须立刻拒绝，
        # 否则两路并发 kill/--resume 同一批会话，互相踩。
        return web.json_response({"ok": False, "error": "上一次切换还在进行中，稍后再试"})
    try:
        def _do():
            if native:
                _native_apply(tier)     # /model 同款：写 settings.json 的 model 字段
            else:
                # 顺序要紧：先把 cc-switch 的「当前供应商」指针挪过去，再改
                # settings.json。反过来的话，cc-switch 的 live 同步会在中间那一瞬
                # 把新 env 写进旧供应商的记录里（配置串联就是这么来的）。
                _cc_set_current(prov["id"])
                _apply_cc_env(env)      # 机器级生效（cc-switch 同款语义）
            # 切供应商会整份重写 ~/.claude/settings.json（cc-switch 也会跟着写），
            # 提醒用的 hooks 常常就是这么没的。切完立刻查一次，别等 5 分钟的看护。
            _hooks_guard()
            return _switch_sessions_to_model(prov, model, tier)
        done, waiting, failed = await asyncio.get_event_loop().run_in_executor(None, _do)
    finally:
        _MODEL_SWITCH_LOCK.release()
    print("[model] 切换完成 prov=%s model=%s 重启=%s 等待=%s 失败=%s" %
          (prov["name"], model, done, waiting, failed), flush=True)
    return web.json_response({
        "ok": True, "via": "restart",
        "provider_name": prov["name"], "model": model,
        "restarted": done, "waiting": waiting, "failed": failed,
        "error": "" if not failed else "有 %d 个会话重开失败，详情见 failed" % len(failed)})


def _close_killed_window(s, tries=3):
    """杀掉进程后把它的终端窗口关掉，别留在屏幕上。

    window_id 是发现时的快照，可能已经失效；wrapper 也可能还没退干净导致
    Terminal 拒绝关闭。所以每次重试都按 tty 现查一次窗口，关掉为止。"""
    tty = s.get("tty") or ""
    wid = s.get("window_id")
    for _ in range(tries):
        if not wid and tty:
            wid = (_window_map() or {}).get(tty, (None, ""))[0]
        if wid and plat.close_window(wid):
            return True
        time.sleep(0.5)
    if wid:
        print("[ext] 关窗口失败 wid=%s sid=%s（可能已被手动关掉）"
              % (wid, (s.get("sid") or "")[:8]), flush=True)
    return False


def _restart_session_for_model(s, model=None):
    """kill 会话进程 → 关旧窗 → wrapper --resume 重开（新进程读新的机器级 settings.json）。

    model 会作为 `claude --model` 传进去：只换 settings.json 不够，--resume 恢复
    的会话模型优先级更高（见 _open_wrapped_terminal）。"""
    if IS_WIN:
        # Windows 的会话就在 host 自己的 ConPTY 里：原地重开进程即可（sid 不变）
        hs = SESSIONS.get(s.get("sid"))
        if not isinstance(hs, Session):
            return False, "会话不在 Codeboss 托管里"
        try:
            hs.restart(_claude_argv_for_model(hs.cmd, model))
        except Exception as e:
            return False, str(e)[:120]
        save_registry()
        return True, ""
    wid, pid = s.get("window_id"), s.get("pid")
    if pid:
        _run(["kill", str(pid)])
        for _ in range(8):
            time.sleep(0.5)
            if not _run(["ps", "-o", "pid=", "-p", str(pid)])[1].strip():
                break
        else:
            _run(["kill", "-9", str(pid)])
            time.sleep(1)
    _close_killed_window(s)
    return _reopen_in_terminal(s.get("cwd") or str(Path.home()), s["sid"], model=model)


WIN_BUSY_SEC = 12.0     # 最近这段时间还在吐输出 → 当作正在干活，不打断


def _is_claude_pty(s):
    """这条会话跑的是不是 claude（Windows 上会话都是 Codeboss 自管的 PTY）。"""
    c = list(getattr(s, "cmd", None) or [])
    if not c:
        return False
    return os.path.basename(str(c[0])).lower() in ("claude", "claude.exe", "claude.cmd")


def _claude_argv_for_model(cmd, model):
    """给 claude 的 argv 换上 --model（先去掉已有的），并补 --continue 接续对话。
    model 为空/「默认」= 回到默认档，此时只删不加。"""
    argv = [str(c) for c in (cmd or [])]
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--model" and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--model="):
            i += 1
            continue
        out.append(a)
        i += 1
    if model and model not in ("默认", "default"):
        out += ["--model", model]
    return _respawn_cmd(out)


def _win_switch_sessions(prov, model, tier):
    """Windows：discover() 抓不到终端，会话全在 SESSIONS 里（Codeboss 自管 PTY）。
    对跑着 claude 的会话走「同一个终端里重启进程」：--continue 接续对话，历史不丢。
    正在吐输出（多半在干活）的先记 pending，空闲后由 _model_switch_loop 补切。"""
    done, waiting, failed = [], [], []
    for s in sorted(SESSIONS.values(), key=lambda x: -getattr(x, "started", 0)):
        if getattr(s, "hidden", False) or getattr(s, "ghost", False):
            continue
        if not _is_claude_pty(s):
            continue
        sid, title = s.sid, getattr(s, "name", "") or s.sid[:8]
        if not getattr(s, "alive", False):
            continue        # 已退出：下次「重新拉起」自然用新档位
        rec = {"provider_id": prov["id"], "provider_name": prov["name"],
               "base_url": prov.get("base_url", ""), "model": model, "tier": tier}
        if time.time() - getattr(s, "last_out", 0) < WIN_BUSY_SEC:
            waiting.append({"sid": sid, "title": title, "state": "running"})
            _model_set(sid, dict(rec, via="pending"))
            continue
        try:
            s.restart(_claude_argv_for_model(s.cmd, model))
            ok, err = True, ""
        except Exception as e:
            ok, err = False, str(e)[:120]
        if ok:
            _model_set(sid, dict(rec, via="restart"))
            done.append({"sid": sid, "title": title})
        else:
            failed.append({"sid": sid, "title": title, "why": (err or "重开失败")[:120]})
    if done:
        save_registry()     # 新 argv（带 --model）落盘，服务重启后「重新拉起」也认
    print("[model] Windows 会话切换 done=%d waiting=%d failed=%d"
          % (len(done), len(waiting), len(failed)), flush=True)
    return done, waiting, failed


def _switch_sessions_to_model(prov, model, tier):
    """把当前所有会话切到目标供应商/模型。空闲的立刻重开；忙的排队等空闲。

    返回 (done, waiting, failed)：
      done    = [{sid,title}] 已重开
      waiting = [{sid,title,state}] 正在跑，等空闲后由后台补切
      failed  = [{sid,title,why}]    重开失败
    """
    if IS_WIN:
        return _win_switch_sessions(prov, model, tier)
    done, waiting, failed = [], [], []
    for s in discover(force=True):
        if (s.get("agent") or "claude") != "claude":
            continue  # 换的是 Claude 的供应商；codex 读 ~/.codex/config.toml，别杀别塞 --model
        sid, title = s["sid"], s.get("title") or sid[:8]
        state = (s.get("state") or "")
        if state not in ("idle", ""):
            # 正在跑：不打断，记成 pending，后台等它空闲再重开
            waiting.append({"sid": sid, "title": title, "state": state})
            _model_set(sid, {"provider_id": prov["id"], "provider_name": prov["name"],
                             "base_url": prov["base_url"], "model": model,
                             "tier": tier, "via": "pending"})
            continue
        try:
            ok, err = _restart_session_for_model(s, model)
        except Exception as e:
            ok, err = False, str(e)[:120]
        if ok:
            _model_set(sid, {"provider_id": prov["id"], "provider_name": prov["name"],
                             "base_url": prov["base_url"], "model": model,
                             "tier": tier, "via": "restart"})
            done.append({"sid": sid, "title": title})
        else:
            failed.append({"sid": sid, "title": title, "why": (err or "重开失败")[:120]})
    return done, waiting, failed


def _model_pending():
    """排队等空闲的切换任务（provider 换完后写进 model_state，via='pending'）。"""
    try:
        con = _db()
        rows = con.execute("SELECT sid,provider_id,provider_name,base_url,model,tier "
                            "FROM model_state WHERE via='pending'").fetchall()
        con.close()
        return [dict(zip(("sid", "provider_id", "provider_name", "base_url", "model", "tier"), r))
                for r in rows]
    except Exception:
        return []


_PEND_TRIES = {}   # Windows：sid → 已尝试补切次数（忙了很久的最后强制切，免得永远排队）


async def _model_switch_loop(app):
    """后台补切：排队中的会话一旦空闲就重开（跑着的活不打断）。

    每 20s 扫一次；只有真的还有排队任务时才动，普通时候零开销。"""
    async def _run():
        await asyncio.sleep(15)
        while True:
            try:
                pend = _model_pending()
                if pend:
                    def _do():
                        out = []
                        for r in pend:
                            if IS_WIN:
                                s = SESSIONS.get(r["sid"])
                                if (not s or getattr(s, "ghost", False) or
                                        not getattr(s, "alive", False) or not _is_claude_pty(s)):
                                    _PEND_TRIES.pop(r["sid"], None)
                                    _model_clear(r["sid"])
                                    continue
                                tries = _PEND_TRIES.get(r["sid"], 0) + 1
                                _PEND_TRIES[r["sid"]] = tries
                                if time.time() - getattr(s, "last_out", 0) < WIN_BUSY_SEC and tries < 15:
                                    continue        # 还在跑，下轮再看（最多等 5 分钟）
                                try:
                                    s.restart(_claude_argv_for_model(s.cmd, r.get("model")))
                                    _model_set(r["sid"], dict(r, via="restart"))
                                    save_registry()
                                    _PEND_TRIES.pop(r["sid"], None)
                                    out.append(r["sid"])
                                except Exception as e:
                                    print("[model] 补切失败 sid=%s: %s"
                                          % (r["sid"][:8], str(e)[:100]), flush=True)
                                continue
                            s = next((x for x in discover(force=True) if x["sid"] == r["sid"]), None)
                            if not s:
                                _model_clear(r["sid"])
                                continue
                            if (s.get("state") or "") not in ("idle", ""):
                                continue        # 还在跑，下轮再看
                            ok, err = _restart_session_for_model(s, r.get("model"))
                            if ok:
                                _model_set(r["sid"], dict(r, via="restart"))
                                out.append(r["sid"])
                            else:
                                print("[model] 补切失败 sid=%s: %s" % (r["sid"][:8], (err or "")[:100]),
                                      flush=True)
                        return out
                    done = await asyncio.get_event_loop().run_in_executor(None, _do)
                    if done:
                        print("[model] 排队补切完成: %s" % [d[:8] for d in done], flush=True)
            except Exception as e:
                print("[model] switch loop error:", e)
            await asyncio.sleep(20)

    app["model_switch_task"] = asyncio.create_task(_run())


def _transcript_cwd(sid):
    """从转录文件反查会话的工作目录（登记表里没有的老会话也能召回）。

    每个 agent 各自认自己的转录：Claude 在 ~/.claude/projects，Codex 在 ~/.codex/sessions。"""
    for ad in agents.adapters():
        try:
            p = ad.transcript_path("", sid) if ad else None
        except Exception:
            p = None
        if not p:
            continue
        try:
            cwd, _t = ad.transcript_meta(p)
        except Exception:
            cwd = ""
        return cwd or str(Path.home())
    return ""


async def h_seen(request):
    """手机上打开了某个会话：该会话的阻塞提醒不用再重复了。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    if sid:
        _BLOCK.pop(sid, None)
    return web.json_response({"ok": True})


def _agent_cli_missing(ad, cwd):
    """这台 Windows 机器上还找得到这个 agent 的命令吗？找得到返回空串。
    `installed()` 有「~/.claude 还在就算装过」的兜底，可 CLI 被卸掉之后
    ConPTY 里起 `claude` 会秒退 127，接口却照回 ok：手机端表现就是「唤起没反应」
    （2026-11-12 DESKTOP-CMURT6R 实机：claude 已不在 PATH，resume 返回 ok=True，
    三秒后会话又没了）。mac/Linux 的召回走终端，shell 会自己把话说清楚。"""
    if not IS_WIN or ad is None:
        return ""
    argv = ad.launch_argv(cwd) or []
    if not argv:
        return "这台电脑上没装 %s" % (ad.label or ad.key)
    b = str(argv[0])
    ok = os.path.exists(b) if os.path.isabs(b) else bool(shutil.which(b))
    if ok:
        return ""
    return "这台电脑上没装 %s（找不到 %s 命令）" % (ad.label or ad.key, b)


def _resume_cwd(sid, cwd):
    """会话的工作目录可能早被删了（/private/tmp 这类临时目录最常见）。
    原样 cd 会卡死在 "no such file or directory"，agent 根本没起来
    （2026-11-22 opencode）。换到 home：转录还在，历史照常恢复，
    只是不再在那个目录里——所以降级要打一行看得见的日志。"""
    if cwd and os.path.isdir(cwd):
        return cwd
    home = str(Path.home())
    print("[resume] sid=%s 原目录不存在（%s）→ 用 %s 召回"
          % (str(sid or "")[:8], cwd, home), flush=True)
    return home


async def h_ext_resume(request):
    """召回已退出会话：转录还在盘上，--resume 重开即恢复全部历史。
    登记表里找不到也不要紧——只要转录还在，从转录反查 cwd 照样召回。"""
    await require_token(request)
    data = await request.json()
    sid = (data.get("sid") or "").strip()
    live = {x["sid"] for x in await asyncio.get_event_loop().run_in_executor(None, _tick_sessions)}
    if sid in live:
        # 页面列表可能是旧的（刚被别的途径拉起来）：这里报错会让用户永远点不进去。
        # 当成成功交回，前端直接进会话。
        _dbg("[resume] sid=%s 已在运行，直接进" % sid[:8])
        return web.json_response({"ok": True, "already": True})
    ent = next((x for x in _closed_sessions(live) if x["sid"] == sid), None)
    cwd = (ent or {}).get("cwd") or _transcript_cwd(sid)
    if not cwd:
        print("[resume] sid=%s 找不到转录（登记表/历史目录都没有），无法召回" % sid[:8], flush=True)
        return web.json_response({"ok": False, "error": "找不到这个会话的转录，无法召回"})
    cwd = _resume_cwd(sid, cwd)
    rec = _model_get(sid) or {}
    # 来源决定唤起方式：桌面版来源回桌面版（手机发的消息进 codex 队列，
    # 不抢 App 的写入权）；只有终端来源才开终端窗口。把桌面版的会话塞进终端
    # 会撞上 App 正在占着的写入权，codex 直接进只读，手机端根本没法输入。
    if not IS_WIN and agent_codex.last_origin(sid) == "desktop":
        ok, err = await asyncio.get_event_loop().run_in_executor(
            None, agent_codex.open_desktop, sid)
        if ok:
            print("[resume] sid=%s 桌面版来源 → 桌面版打开（消息走队列）ok=y" % sid[:8],
                  flush=True)
            return web.json_response({"ok": True})
        print("[resume] sid=%s 桌面版打开失败（%s），回退开终端"
              % (sid[:8], (err or "")[:120]), flush=True)
    if IS_WIN:
        # Windows 没有可接管的终端窗口：在自己托管的 ConPTY 里 --resume，
        # 手机端照样能聊/能按键（外部终端窗口 host 看不见，等于白开）。
        ad = agents.get(agents.agent_of(sid, cwd)) or agents.default()
        cmd = ad.launch_argv(cwd, sid=sid, model=rec.get("model")) if ad else None
        miss = _agent_cli_missing(ad, cwd) if cmd else (
            "这台电脑上没装 %s" % ((ad.label or ad.key) if ad else "这个 agent"))
        if miss:
            print("[resume] sid=%s %s" % (sid[:8], miss), flush=True)
            return web.json_response({"ok": False, "error": miss})
        _prep_cwd(ad, cwd)
        name = Path(cwd).name or (ad.label or ad.key)
        try:
            old = SESSIONS.get(sid)
            if old is not None:
                try:
                    old.kill()
                except Exception:
                    pass
            SESSIONS[sid] = Session(sid, name, cwd, list(cmd))
        except Exception as e:
            return web.json_response({"ok": False, "error": "启动失败: %s" % str(e)[:160]})
        save_registry()
        _open_session_window(sid, name)
        print("[resume] sid=%s cwd=%s host=1 ok=y" % (sid[:8], (cwd or "")[-40:]), flush=True)
        return web.json_response({"ok": True})
    ok, err = await asyncio.get_event_loop().run_in_executor(
        None, _reopen_in_terminal, cwd or str(Path.home()), sid,
        None, 18, rec.get("model") or None)
    # 「已退出点进去召不回来」只能靠这行定位：终端没权限/agent 没装/wrapper 没起来
    _f = print if not ok else _dbg
    _f("[resume] sid=%s cwd=%s ok=%s err=%s"
       % (sid[:8], (cwd or "")[-40:], "y" if ok else "n", (err or "")[:160]), flush=True)
    return web.json_response({"ok": ok, "error": "" if ok else err})


# 会话 id 会拿去拼 ~/.claude/projects 下的路径，只认字母数字和横杠，别的一概拒绝
_SID_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}$")


def _ps_stat():
    """pid -> stat，一次拿全机。mac 上会留杀不掉也退不掉的卡死进程（stat 带 E =
    正在退出，11-06 起这台机器就有两个）：`claude agents` 还列着它们，只看 pid
    会当成活的——手机上一直显示开着，关闭永远「没关掉」。"""
    _, out, _ = _run(["ps", "-A", "-o", "pid=,stat="])
    m = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) < 2:
            continue
        try:
            m[int(p[0])] = p[1]
        except ValueError:
            pass
    return m


def _stat_dead(s):
    """Z 僵尸、带 E 的「正在退出」都算已经没了（父进程还没收拾而已）。"""
    return (not s) or s.startswith("Z") or "E" in s


def _proc_gone(pid):
    """进程是不是真没了。僵尸（stat 以 Z 开头）和「正在退出」（带 E）都算没了——
    父进程还没回收而已，它不占 CPU、不再写文件，ps 里却还挂着一行；
    只看 pid 会误判成「杀不掉」。"""
    _c, out, _e = _run(["ps", "-o", "stat=", "-p", str(pid)])
    return _stat_dead(out.strip())


def _kill_proc(pid, timeout=5.0):
    """结束一个 claude 进程：TERM → 等 → KILL。转录逐条落盘，杀进程不丢历史。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if IS_WIN:
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        return True
    _run(["kill", str(pid)])
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _proc_gone(pid):
            return True
        time.sleep(0.4)
    _run(["kill", "-9", str(pid)])
    time.sleep(0.8)
    return _proc_gone(pid)


def _desk_caps(x):
    """桌面版 Codex 能干什么：新版 CLI 支持 `exec resume` 就能从手机发消息，
    否则只读（原因写清楚，别让人点发送才看到失败）。"""
    if agent_codex.exec_resume_ok():
        return {"input"}, ""
    return set(), "Codex 桌面版会话：手机上看转录、收提醒；发消息要在电脑上的 Codex 里回复"


def _agents_installed(product=None):
    """这台电脑装了哪些 agent。product 给定则按产品线过滤（None=不过滤）。"""
    face = _agent_face(product) if product else None
    out = []
    for ad in agents.adapters():
        try:
            if ad.installed() and (face is None or ad.key in face):
                out.append({"key": ad.key, "label": ad.label,
                            "caps": [n for n in ("resume", "notify", "model_switch")
                                     if getattr(ad, "can_" + n, False)]})
        except Exception:
            pass
    return out


async def h_ext_agents(request):
    """这台电脑装了哪些 agent。手机端「＋」据此决定：只有一种就直接开，
    装了好几种就先问一句（别把打不开的选项摆出来）。"""
    await require_token(request)
    return _jsonz({"agents": _agents_installed(_face_product(request))})


def _prep_cwd(ad, cwd):
    """开 agent 会话前让它准备目录（Codex：记成 trusted，否则卡在信任框、会话建不出来）。
    失败只记一行，不挡着开会话——用户可能就坐在电脑前想自己点。"""
    if not ad:
        return
    try:
        if not ad.prepare_cwd(cwd):
            print("[ext] %s 准备目录失败（照常开）：%s" % (ad.key, cwd), flush=True)
    except Exception as e:
        print("[ext] %s prepare_cwd 异常（照常开）：%s" % (ad.key, str(e)[:120]), flush=True)


def _open_session_window(sid, title=""):
    """Windows 上给 host 托管的会话开一个电脑窗口（ConPTY 自己没有窗口）。
    手机上照旧能看能发；窗口只是同一块屏的另一个显示面，关掉不影响会话。"""
    if not IS_WIN:
        return
    try:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        argv = [str(pyw if pyw.exists() else sys.executable),
                str(HERE / "ptywin.py"), "--sid", sid]
        if title:
            argv += ["--title", title]
        subprocess.Popen(argv, cwd=str(HERE), env=os.environ.copy(),
                         close_fds=True,
                         creationflags=0x08000000 | 0x00000008)  # NO_WINDOW | DETACHED
    except Exception as e:
        print("[ext] 会话窗口没起来 sid=%s：%s" % (sid[:8], str(e)[:120]), flush=True)


async def h_ext_sess_new(request):
    """在某个目录里开一个新会话（手机端「目录 +」按钮）。
    电脑上弹一个新终端跑 claude，手机端几秒后从 claude agents 列表里看到它。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    return web.json_response(await _sess_new_core(data))


async def _sess_new_core(data):
    """建会话的实体（handler 和派单共用）：返回 payload，不碰 request/web。
    Mac 走 open_wrapped 不返回 sid（会话要靠发现认出来）；Windows 返回 sid。"""
    data = data if isinstance(data, dict) else {}
    cwd = str(data.get("cwd") or "").strip()
    if not cwd:
        # 手机端「没有会话时的 ＋」不传目录：默认开在电脑登录用户的 home
        # （登录用户就是跑 host 的这个账号，Path.home() 与它一致）
        cwd = str(Path.home())
    if not Path(cwd).is_dir():
        return ({"ok": False, "error": "目录不存在：%s" % cwd})
    ad, aerr = _pick_agent(data)
    if not ad:
        return ({"ok": False, "error": aerr})
    cmd = ad.launch_argv(cwd) if ad else None
    if not cmd:
        return ({"ok": False,
                                  "error": "这台电脑上没装 %s" % ((ad.label or ad.key) if ad else "这个 agent")})
    _prep_cwd(ad, cwd)
    if IS_WIN:
        # Windows 上没有可接管的终端窗口：会话必须由 host 自己用 ConPTY 托管，
        # 列表（/api/sessions）和手机端的终端页才看得见。走 open_wrapped 是另开一个
        # 外部终端，host 完全不知道它存在——手机上点了＋，列表里永远没有这条
        # （2026-11-11 DESKTOP-CMURT6R）。
        miss = _agent_cli_missing(ad, cwd)
        if miss:
            return ({"ok": False, "error": miss})
        cmd = list(cmd)
        flag = getattr(ad, "new_session_id_flag", None)
        if flag:
            # sid 用 UUID 并且直接指定给 agent：host 的会话 id == 转录 id，
            # /api/ext_msg 读得到聊天、召回也能按同一个 id --resume。
            sid = str(uuid.uuid4())
            cmd += flag + [sid]
        else:
            sid = uuid.uuid4().hex[:8]
        name = Path(cwd).name or (ad.label or ad.key)
        try:
            SESSIONS[sid] = Session(sid, name, cwd, cmd)
        except Exception as e:
            return ({"ok": False, "error": "启动失败: %s" % str(e)[:160]})
        save_registry()
        # ConPTY 没有窗口：电脑上另开一个镜像窗口，用户点＋之后能在桌面上看到
        # claude/codex 真的起来了（0.8.46，DESKTOP-CMURT6R「建了但无窗口」）
        _open_session_window(sid, name)
        # 带上 sid：手机端可以直接进这个新会话，不用等列表刷新再找
        return ({"ok": True, "sid": sid})
    # 不传 --meta：这是全新的会话，还没有 agent 会话 id；wrapper 自己生成日志名，
    # 等 agent 真起来、进了会话列表，全彩镜像按 pid 就能认上。
    argv = [sys.executable, str(WRAPPER), "--"] + list(cmd)
    wid, tty, err = await asyncio.get_event_loop().run_in_executor(
        None, lambda: plat.open_wrapped(cwd, argv))
    if err:
        return ({"ok": False, "error": (err or "")[:200]})
    if not getattr(ad, "eager_session", True) and (wid or tty):
        # Codex 这类「要等第一句话才有会话 id」的 agent：先按窗口占个位，
        # 不然手机上点了＋，列表里要等人在电脑前敲完第一句才出现。
        _PENDING_AG[_pending_key(wid, tty)] = {
            "agent": ad.key, "wid": wid, "tty": tty, "cwd": cwd,
            "at": time.time(), "title": Path(cwd).name or (ad.label or ad.key)}
    if tty and not plat.IS_MAC:
        # 无头机器没人点「信任这个文件夹」，后台盯着替用户确认
        asyncio.ensure_future(_auto_trust_watch(tty, wid))
    return ({"ok": True})


async def h_ext_sess_fork(request):
    """fork：`claude --resume <sid> --fork-session` 在同一个目录复制一份历史。
    新会话拿到新的 id，原会话一个字节都不动，两边可以各聊各的。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    if not _SID_RE.match(sid):
        return web.json_response({"ok": False, "error": "会话 ID 不对"})
    cwd = str(data.get("cwd") or "").strip()
    if not cwd or not Path(cwd).is_dir():
        cwd = _transcript_cwd(sid)
    if not cwd or not Path(cwd).is_dir():
        return web.json_response({"ok": False, "error": "找不到这个会话所在的目录"})
    if IS_WIN:
        # fork 出来的会话 id 由 claude 自己生成，host 拿不到 → 聊天/召回都会对不上；
        # 不如直接说清楚，别让用户点完等一个永远不会出现的会话。
        return web.json_response({"ok": False, "error": "Windows 版暂不支持 fork，直接新建会话即可"})
    ad = agents.get(agents.agent_of(sid, cwd)) or agents.default()
    cmd = ad.fork_argv(cwd, sid) if ad else None
    if not cmd:
        return web.json_response({"ok": False,
                                  "error": "%s 暂不支持 fork" % ((ad.label or ad.key) if ad else "这个 agent")})
    _prep_cwd(ad, cwd)
    jp = ad.transcript_path(cwd, sid) if ad else None
    try:
        has_hist = bool(jp) and os.path.getsize(jp) > 200
    except OSError:
        has_hist = False
    if not has_hist:
        return web.json_response({"ok": False, "error": "这个会话还没有可 fork 的历史"})
    # 不传 --meta：wrapper 的日志名绝不能复用父会话的 sid —— 两个进程写同一个
    # logs/ansi/<sid>.log，父会话的全彩镜像当场就被 fork 的输出搅乱。
    argv = [sys.executable, str(WRAPPER), "--"] + list(cmd)
    wid, tty, err = await asyncio.get_event_loop().run_in_executor(
        None, lambda: plat.open_wrapped(cwd, argv, name="codeboss-fork-%s" % sid[:8]))
    if err:
        return web.json_response({"ok": False, "error": (err or "")[:200]})
    if tty and not plat.IS_MAC:
        asyncio.ensure_future(_auto_trust_watch(tty, wid))
    return web.json_response({"ok": True})


async def h_ext_sess_close(request):
    """关掉一个会话：只结束进程（连同它的终端窗口），转录/历史一律保留。
    和 /api/ext_sess_del 的区别就是什么都不删——列表里变成「已退出」，随时能召回。
    手机上「关闭会话」走这个；删除是另一件事，别混。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    if not _SID_RE.match(sid):
        return web.json_response({"ok": False, "error": "会话 ID 不对"})
    aw = agent_acp.window(sid)
    if aw is not None:
        # ACP 协议源窗口：close = 结束进程、hist/仓保留（列表变「已退出」可召回）
        try:
            await aw.close()
        except Exception:
            pass
        print("[ext] close acp %s" % sid[:12], flush=True)
        return web.json_response({"ok": True, "killed": True})

    def _do():
        killed = False
        # host 自管的会话（Windows ConPTY）：从 SESSIONS 摘掉并杀整棵进程树
        hs = SESSIONS.pop(sid, None)
        if hs is not None:
            try:
                hs.kill()
                killed = True
            except Exception:
                pass
        live = next((x for x in _tick_sessions() if x.get("sid") == sid), None)
        if live is None:
            # 4s 的发现缓存里没有：强制重扫一次再认（刚开的会话常常还没进缓存）
            live = next((x for x in _tick_sessions(True) if x.get("sid") == sid), None)
        pid = (live or {}).get("pid")
        if pid:
            killed = _kill_proc(pid) or killed
            try:
                _close_killed_window(live)
            except Exception:
                pass
        # 杀完立刻重扫一遍：手机端关完马上要列表变「已退出」，
        # 吃 2~4s 的旧缓存会当场判「没关掉：进程还在」（2026-11-13 用户实测）。
        try:
            _tick_cache.update(at=time.time(), data=_tick_sessions(True))
        except Exception:
            pass
        print("[ext] close %s killed=%s pid=%s" % (sid[:8], killed, pid), flush=True)
        return killed

    killed = await asyncio.get_event_loop().run_in_executor(None, _do)
    return web.json_response({"ok": True, "killed": killed})


async def h_ext_sess_del(request):
    """删掉一个会话：还活着就先结束进程，再清转录、子代理目录、登记表和切模型记录。
    转录删了就真找不回来——页面那边必须二次确认。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    if not _SID_RE.match(sid):
        return web.json_response({"ok": False, "error": "会话 ID 不对"})
    # host 自己起的会话（桌宠的慢脑子等）：删了她就失忆。要删请在电脑上删，
    # 下一轮 host 发现记录没了会重建、重新登记。
    import owned_sessions
    if owned_sessions.has(sid):
        why = owned_sessions.why(sid) or "host 自己开的会话"
        return web.json_response({
            "ok": False, "owned": True,
            "error": "这是「%s」：手机上删不了，要删请在电脑上删" % why})
    if sid.startswith("acp-"):
        # ACP 协议源窗口：转录=滚动仓，关进程 + 摘窗口表 + 清仓即全删
        aw = agent_acp.window(sid)
        if aw is not None:
            try:
                await aw.close()
            except Exception:
                pass
        agent_acp.forget(sid)
        try:
            scrollback.delete(sid)
        except Exception:
            pass
        print("[ext] del acp %s" % sid[:12], flush=True)
        return web.json_response({"ok": True, "killed": aw is not None, "removed": 1})

    def _do():
        killed = False
        # host 自管的会话（Windows）：直接从 SESSIONS 摘掉，网页终端/列表里也不再挂着
        hs = SESSIONS.pop(sid, None)
        if hs is not None:
            try:
                hs.kill()
                killed = True
            except Exception:
                pass
        live = next((x for x in _tick_sessions() if x.get("sid") == sid), None)
        if live and live.get("pid"):
            killed = _kill_proc(live["pid"])
            try:
                _close_killed_window(live)
            except Exception:
                pass
        removed = 0
        ad = agents.get(agents.agent_of(sid))
        if ad:
            try:
                removed = ad.delete_transcript(sid)
            except Exception:
                removed = 0
        for suf in (".log", ".meta.json", ".sock", ".term"):
            try:
                f = ANSI_DIR / (sid + suf)
                if f.exists():
                    f.unlink()
            except OSError:
                pass
        try:
            scrollback.delete(sid)   # 删除才清滚动历史；「关闭」按语义保留（转录同理）
        except Exception:
            pass
        try:
            con = _db()
            con.execute("DELETE FROM ext_registry WHERE sid=?", (sid,))
            con.execute("DELETE FROM model_state WHERE sid=?", (sid,))
            con.execute("DELETE FROM sess_title WHERE sid=?", (sid,))
            con.commit()
            con.close()
        except Exception:
            pass
        return killed, removed

    killed, removed = await asyncio.get_event_loop().run_in_executor(None, _do)
    return web.json_response({"ok": True, "killed": killed, "removed": removed})


async def h_ext_title(request):
    """改会话名：手机列表、推送标题、电脑终端 tab（Terminal.app）一起变。
    title 给空 = 恢复自动起名（ai-title/窗口名/首句话那套）。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = str(data.get("sid") or "").strip()
    # 换行别进来：终端 tab 名是单行的，手机端 dialog 里粘贴多行也只留一行
    title = " ".join(str(data.get("title") or "").split())[:60]
    if not _SID_RE.match(sid):
        return web.json_response({"ok": False, "error": "会话 ID 不对"})
    try:
        con = _db()
        if title:
            con.execute("INSERT INTO sess_title(sid,title,at) VALUES(?,?,?)"
                        " ON CONFLICT(sid) DO UPDATE SET title=excluded.title,at=excluded.at",
                        (sid, title, time.time()))
        else:
            con.execute("DELETE FROM sess_title WHERE sid=?", (sid,))
        con.commit()
        con.close()
    except Exception as e:
        return web.json_response({"ok": False, "error": "存不了：%s" % str(e)[:80]})
    # 电脑那头一起变（尽力而为）：wrapper 会话由发现循环经 ctl socket 重推新名
    # （≤2 秒）；Windows 托管会话直接改 Session.name；没经 wrapper 的第三方终端
    # 没有通道，手机上的名字照生效。
    try:
        if IS_WIN:
            hs = SESSIONS.get(sid)
            if hs is not None:
                hs.name = title
        _TAB_SYNC.pop(sid, None)
    except Exception:
        pass
    return web.json_response({"ok": True, "title": title})


# ---- 版本与更新（参考 BambuDeck app_update/gh_mirror：api.github.com 直连 + 前缀镜像兜底） ----
# 版本号唯一事实源 = 同目录的 VERSION 文件（DMG/中继发布、Mac 客户端检查更新都读它）
def _read_version():
    try:
        v = (HERE / "VERSION").read_text(encoding="utf-8").strip()
        if v:
            return v
    except Exception:
        pass
    return "0.4.0"


APP_VERSION = _read_version()
STARTED_AT = time.time()   # 进程启动时刻：手机/客户端判断「磁盘版本已更新但服务还在跑旧代码」
# 0.4.0 = 规划里程碑对齐版：v0.2 模型切换 + v0.3 中继 MVP + v0.4 手动切换/召回/自动更新 全含
_RELEASE_REPO = "example/codeboss"
_GH_API = "https://api.github.com/repos/%s/releases/latest" % _RELEASE_REPO
# 前缀镜像与 BambuDeck gh_mirror.dart 同款：直连永远第一。迁移主因：本机到
# github.com:443 半通（git fetch 报 SSL_ERROR_SYSCALL）时旧实现整个检查就挂了；
# tag 只用于版本比较、无信任根问题，镜像内容错了顶多提示不准。
_GH_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")
_update_state = {"at": 0.0, "git": False, "behind": 0, "commits": [], "err": "", "note": "",
                 "relay_ver": "", "relay_pkg": None, "gh_tag": ""}


_CAPS_CACHE = {"ts": 0.0, "val": None, "ok": False}


def _relay_caps(timeout=3.0, ttl=600.0):
    """自建中继能力表（中继 /r/caps 公开 JSON，Task: selfhost-relay）。拉不到
    （官方中继老版本没这端点/断网/超时）→ None：brand 不带 caps 键，页面 fail-open
    当官方全功能。成功缓存 10 分钟；失败落 60 秒负缓存——中继黑洞化时不能每次
    brand 都真打一发（打不到还要等满 timeout）。"""
    import time as _t
    import urllib.request
    now = _t.time()
    if now - _CAPS_CACHE["ts"] < (ttl if _CAPS_CACHE["ok"] else 60.0):
        return _CAPS_CACHE["val"]
    base = (_relay_cfg().get("url") or "").rstrip("/")
    if not base:
        return None
    try:
        req = urllib.request.Request(base + "/r/caps",
                                     headers={"User-Agent": "codeboss"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(d, dict) and d:
            _CAPS_CACHE.update(ts=now, val=d, ok=True)
            return d
    except Exception:
        pass
    _CAPS_CACHE.update(ts=now, val=None, ok=False)
    return None


def _relay_host_ver(timeout=8):
    """中继上发布的最新 host 包（publish-host.sh 推的）。更新源优先于 GitHub：
    不依赖 gh 登录态，手机/中继链路本来就得通。
    带上自己的设备 ID：中继按灰度名单决定这台机器能拿到哪个版本。"""
    import urllib.request
    import urllib.parse
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    if not base:
        return None
    dev = (rc.get("device_id") or "").strip()[:32]
    qs = ("?dev=" + urllib.parse.quote(dev)) if dev else ""
    try:
        req = urllib.request.Request(base + "/api/host_ver" + qs,
                                     headers={"User-Agent": "codeboss"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("ok") and d.get("ver"):
            return d
    except Exception:
        pass
    return None


SUPERVISED_FLAG = HERE / "logs" / ".supervised"


def _under_win_supervisor():
    """Windows：当前进程是否被 run-server.vbs 的循环守护看着。

    新版启动脚本在开始跑 cmd 之前会写 logs\\.supervised（循环结束/用户停止服务时删掉），
    所以这个文件在=「外面有个脚本会在我退出后把我拉回来」。老安装包的一次性启动脚本
    不写它——那种情况下退出就真的没人管了，得自己拉替代进程。
    标记可能因脚本被强杀而残留，所以真到要退出时再核实一次「有个 wscript 正跑着
    run-server.vbs」；核实不了（PowerShell 不可用）就信标记。"""
    if not IS_WIN or not SUPERVISED_FLAG.exists():
        return False
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='wscript.exe'\" | "
          "Where-Object { $_.CommandLine -like '*run-server.vbs*' } | "
          "Select-Object -First 1 -ExpandProperty ProcessId")
    try:
        code, out, _e = _run(["powershell", "-NoProfile", "-Command", ps], timeout=10)
        if code == 0:
            return bool((out or "").strip())
    except Exception:
        pass
    return True


def _win_spawn_successor():
    """Windows 上没有守护时，自己拉一个脱离本进程的新服务顶上来。

    CreateProcess 没有等价的「替换当前进程」，只能起子进程再自杀；stdout 接回
    logs/server.log（继承的句柄在新进程里继续有效）。"""
    try:
        logp = HERE / "logs" / "server.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        lf = open(logp, "a", buffering=1, encoding="utf-8", errors="replace")
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        subprocess.Popen([sys.executable] + sys.argv, cwd=str(HERE),
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                         close_fds=True, creationflags=flags)
        return True
    except Exception as e:
        print("[server] 拉起替代进程失败：%s" % e, flush=True)
        return False


def _read_vbs(p):
    """读 run-server.vbs：新版 UTF-16，老版 ANSI/GBK，两种都认。"""
    try:
        b = p.read_bytes()
    except OSError:
        return ""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return b.decode("utf-16")
        except Exception:
            return ""
    try:
        return b.decode("mbcs")
    except Exception:
        return b.decode("utf-8", "replace")


def _win_upgrade_supervisor():
    """把老安装包的一次性 run-server.vbs 升级为循环守护版（托盘没开也能升级）。

    只写磁盘上的脚本，不动当前正在跑的进程——本次会话的自更新仍按老规则
    （自己拉替代进程），下次由计划任务/托盘拉起时就是循环版了。"""
    if not IS_WIN:
        return
    cmd = HERE / "run-server.cmd"
    vbs = HERE / "run-server.vbs"
    if not cmd.is_file():
        return
    if ".supervised" in _read_vbs(vbs):
        return
    want = ('Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.CurrentDirectory = "%s"\r\n'
            'If Not fso.FolderExists("%s\\logs") Then fso.CreateFolder("%s\\logs")\r\n'
            "' 循环守护标记：服务进程看到它就知道自己\"有人看着\"，重启时直接退出（码 75）交给本脚本重拉\r\n"
            'fso.CreateTextFile("%s\\logs\\.supervised", True).Close\r\n'
            'Do\r\n'
            '  If fso.FileExists("%s\\logs\\.stopped") Then Exit Do\r\n'
            '  rc = sh.Run("cmd /c ""%s""", 0, True)\r\n'
            '  If rc = 0 Then Exit Do\r\n'
            '  WScript.Sleep 5000\r\n'
            'Loop\r\n'
            'On Error Resume Next\r\n'
            'fso.DeleteFile "%s\\logs\\.supervised", True\r\n') % (
                HERE, HERE, HERE, HERE, HERE, cmd, HERE)
    try:
        vbs.write_text(want, encoding="utf-16")   # WSH 认 BOM；非 ANSI 路径才不会变 "?"
    except Exception:
        pass


_MENU_BAR_APPS = ("/Applications/BossHost.app", "/Applications/CodeBoss.app",
                  "~/Applications/BossHost.app", "~/Applications/CodeBoss.app")


def _menu_bar_app_path(paths=None):
    """装好的菜单栏 App 在哪：没有 App、没有图形会话（SSH）、源码部署
    （开发机 git 检出，跑一次 server.py 就弹 App 太烦）都返回空。"""
    if sys.platform != "darwin":
        return ""
    if _run(["git", "-C", str(HERE), "rev-parse", "--git-dir"], timeout=10)[0] == 0:
        return ""
    for p in (paths or _MENU_BAR_APPS):
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            return p
    return ""


def _app_bundle_version(path):
    try:
        import plistlib
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            d = plistlib.load(f)
        return str(d.get("CFBundleShortVersionString") or "")
    except Exception:
        return ""


def _app_running(path):
    c, _o, _e = _run(["/usr/bin/pgrep", "-f",
                      os.path.join(path, "Contents", "MacOS") + "/"], timeout=10)
    return c == 0


_APP_VER_SEEN = {"ver": ""}


def _app_update_watch(paths=None, opener=None, seen=None):
    """App 自己更新完没起来时补开一次。

    App 更新是「换包 → 重启自己」；0.9.63 那版先 open 后退出，重启失败直接消失，
    而装 0.9.64 这一跳又是旧代码执行的（2026-11-20 用户第二次反馈）。
    host 每分钟看一眼：盘上版本变了、App 又没在跑 → open。版本没变不动，
    用户主动退出的 App 不会被反复弹出来。"""
    st = _APP_VER_SEEN if seen is None else seen
    p = _menu_bar_app_path(paths)
    if not p:
        return ""
    v = _app_bundle_version(p)
    if not v:
        return ""
    if not st.get("ver"):
        st["ver"] = v          # 第一次只记基线：启动时那一步已经开过了
        return ""
    if v == st["ver"]:
        return ""
    st["ver"] = v
    if _app_running(p):
        return ""
    (opener or _open_app)(p)
    return p


async def _app_update_watch_startup(app):
    async def loop():
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.get_event_loop().run_in_executor(None, _app_update_watch)
            except Exception:
                pass
    app["_app_watch"] = asyncio.get_event_loop().create_task(loop())


def _open_menu_bar_app(paths=None, opener=None):
    """Mac 上把菜单栏 App 拉起来：装完/更新完/重启后图标就该在，不用自己去
    「应用程序」找。App 已经在跑时 open 只是激活。

    自动更新（_apply_relay_pkg）只覆盖文件再重启，从不跑 install.sh，所以
    install.sh 里那一步救不了「更新完 App 不见了」；启动时补这一次才盖得住。"""
    p = _menu_bar_app_path(paths)
    if not p:
        return ""
    (opener or _open_app)(p)
    return p


def _open_app(p):
    """open 失败就当没有（SSH 里没图形会话、权限不对——不该影响服务启动）。"""
    try:
        subprocess.Popen(["/usr/bin/open", p], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


async def _open_menu_bar_app_startup(app):
    await asyncio.get_event_loop().run_in_executor(None, _open_menu_bar_app)


def _self_restart(delay=1.5):
    """delay 秒后用同一解释器与参数原地重启。

    Unix：走 execv——不再依赖 launchd（手动启动的进程、被 bootout 过的 job 也能
    回来）；监听 socket 带 CLOEXEC，exec 时自动关闭，新进程立即接管端口。
    Windows：有循环守护（logs\\.supervised）就直接退出码 75，让 run-server.vbs 五秒后
    重拉，新进程仍在守护链里；没有守护（老安装包的一次性脚本）只能起一个 detached
    子进程再自杀，stdout 接回 logs/server.log（计划任务只负责开机拉起）。"""
    def _go():
        print("[update] 原地重启 pid=%d" % os.getpid(), flush=True)
        if IS_WIN:
            if _under_win_supervisor():
                print("[update] 交给启动脚本循环重启", flush=True)
                os._exit(75)
            _win_spawn_successor()
            os._exit(0)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print("[update] execv 失败，退出等 launchd 拉起：%s" % e, flush=True)
            # 75 = 「拉起我」：KeepAlive=SuccessfulExit=false 只认非零退出。
            os._exit(75)
    threading.Timer(delay, _go).start()


def _venv_py(root):
    """虚拟环境里的解释器：Unix 是 bin/python，Windows 是 Scripts\\python.exe。"""
    root = Path(root)
    return root / "Scripts" / "python.exe" if IS_WIN else root / "bin" / "python"


def _venv_pip(root):
    root = Path(root)
    p = root / "Scripts" / "pip.exe" if IS_WIN else root / "bin" / "pip"
    if IS_WIN and not p.exists():
        # Windows 安装包自带解释器（没有 .venv）：补依赖用它自己的 pip
        b = HERE / "python" / "Scripts" / "pip.exe"
        if b.exists():
            return b
    return p


def _update_py():
    """更新时给哪个解释器装依赖：优先安装目录的 .venv，退到服务正在用的解释器。"""
    p = _venv_py(HERE / ".venv")
    if p.exists():
        return p
    if IS_WIN and (HERE / "python" / "python.exe").exists():
        return HERE / "python" / "python.exe"
    return Path(sys.executable)


def _deps_install(py, req, timeout=900):
    """装 requirements 并确认真能 import：镜像优先（国内直连 PyPI 常被掐）→ uv 兜底。

    返回 (ok, why)。**调用方必须在换代码/重启之前调用**：装不上就放弃这次更新，
    让旧进程继续活着。2026-11-21 的教训——0.9.54 那台先被覆盖了代码，补依赖
    的 pip 失败又没人看返回值，重启后 `ModuleNotFoundError: pet`，进程死了就再也
    没人跑自动更新循环，机器永久离线。"""
    py, req = str(py), str(req)
    if not os.path.exists(py):
        return False, "找不到解释器 %s（先跑安装目录里的 install 脚本）" % py
    ok = False
    for src in PYPI_MIRRORS:
        extra = ["-i", src] if src else []
        if _run([py, "-m", "pip", "-q", "install", "--timeout", "8", "--retries", "1",
                 "--only-binary=av", "-r", req] + extra, timeout=timeout)[0] == 0:
            ok = True
            break
    if not ok:
        uv = _uv_bin()
        for src in PYPI_MIRRORS:
            if not uv:
                break
            extra = ["--default-index", src] if src else []
            if _run([uv, "pip", "install", "-q", "--python", py,
                     "--only-binary=av", "-r", req] + extra, timeout=timeout)[0] == 0:
                ok = True
                break
    if not ok:
        # 没有 pip / uv 的机器（uv 建的 venv 常见）：依赖本来就在就放行，别把人永久卡在旧版
        if _run([py, "-c", "import aiohttp, pyte, qrcode"], timeout=60)[0] == 0:
            return True, ""
        return False, "依赖装不上（连不上 PyPI / 镜像，需要联网后再更新）"
    c, _o, e = _run([py, "-c", "import aiohttp, pyte, qrcode"], timeout=60)
    if c != 0:
        return False, "依赖装完仍导入失败：%s" % (str(e or "").strip()[:120])
    return True, ""


def _venv_pyver(py):
    """解释器的小版本（'3.11'）；拿不到返回空串。"""
    c, out, _ = _run([str(py), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=30)
    return out if c == 0 else ""


def _uv_bin():
    import shutil as _sh
    for c in ("uv", str(Path.home() / ".local/bin/uv"), "/opt/homebrew/bin/uv",
              "/usr/local/bin/uv", str(Path.home() / ".cargo/bin/uv")):
        p = c if os.path.isabs(c) else _sh.which(c)
        if p and os.path.exists(p):
            return p
    return ""


# 下载源：默认走国内镜像（清华 PyPI / npmmirror 的 Python 本体），直连放最后。
# 有的网络连 astral.sh / GitHub 会直接掐断 TLS，不换源就会「安装卡住 / 装不出 Python」。
PYPI_MIRRORS = ("https://pypi.tuna.tsinghua.edu.cn/simple",
                "https://mirrors.aliyun.com/pypi/simple", "")
PY_GIT_MIRRORS = (
    "https://registry.npmmirror.com/-/binary/python-build-standalone",
    "",
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/releases/download",
    "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download",
)


def _uv_get():
    """拿一个可用的 uv：已有 → pip 从可达的 PyPI 源装 → 官方脚本兜底。"""
    uv = _uv_bin()
    if uv:
        return uv
    import shutil as _sh
    py = _sh.which("python3")
    if py:
        print("[update] 没装 uv，先装下载器（清华/阿里 PyPI 镜像优先）…", flush=True)
        for src in PYPI_MIRRORS:
            args = [py, "-m", "pip", "install", "-q", "--user",
                    "--timeout", "8", "--retries", "1", "uv"]
            if src:
                args += ["-i", src]
            if _run(args, timeout=180)[0] == 0 and _uv_bin():
                return _uv_bin()
    _run(["/bin/sh", "-c",
          "curl -LsSf --max-time 20 https://astral.sh/uv/install.sh | sh"], timeout=180)
    return _uv_bin()


def _uv_py_install(uv, ver):
    """uv 装指定小版本 Python：国内先走 npmmirror（清华没有 Python 本体镜像），
    再试 GitHub 直连和两个代理。"""
    for m in PY_GIT_MIRRORS:
        if m:
            print("[update] 装 Python %s（镜像 %s）…" % (ver, m), flush=True)
            rc = _run([uv, "python", "install", ver], timeout=600,
                      env_extra={"UV_PYTHON_INSTALL_MIRROR": m,
                                 "UV_HTTP_TIMEOUT": "60"})[0]
        else:
            print("[update] 换 GitHub 直连装 Python %s…" % ver, flush=True)
            rc = _run([uv, "python", "install", ver], timeout=600,
                      env_extra={"UV_HTTP_TIMEOUT": "60"})[0]
        if rc == 0:
            return True
    return False


def _python_for(ver):
    """找一个 ver 小版本的 Python；没有就用 uv 装一个。找不到返回空串。"""
    import shutil as _sh
    names = (str(_venv_py(HERE / ".venv")), "python" + ver,
             "python" if IS_WIN else "python3")
    for c in names:
        p = c if os.path.isabs(c) else _sh.which(c)
        if p and os.path.exists(p) and _venv_pyver(p) == ver:
            return p
    uv = _uv_get()
    if not uv:
        return ""
    _uv_py_install(uv, ver)
    c, out, _ = _run([uv, "python", "find", ver], timeout=60)
    return out if c == 0 and out and _venv_pyver(out) == ver else ""


def _ensure_venv_version(ver):
    """让 HERE/.venv 跑在 Python ver 上：已经对就跳过；不对就**先建好新的再切换**，
    任何一步失败都保留旧环境原样返回 False。返回 (ok, why)。"""
    import shutil as _sh
    # 安装包自带的解释器（Windows）：版本对就不用管 .venv
    bpy = HERE / "python" / "python.exe"
    if bpy.exists() and _venv_pyver(bpy) == ver:
        return True, ""
    cur = _venv_pyver(_venv_py(HERE / ".venv"))
    if cur == ver:
        return True, ""
    if IS_WIN:
        # Windows 上 .venv\Scripts\python.exe 正被自己占用，重命名/删除都会失败，
        # 而且在跑着的进程里换解释器本来就不稳——交给安装脚本（它先停服务再重建）。
        return False, ("这台机器的安装包要求 Python %s（当前 %s）："
                       "重新运行安装目录里的 install.ps1 即可升级（数据保留）"
                       % (ver, cur or "未知"))
    py = _python_for(ver)
    if not py:
        return False, ("这台机器装不出 Python %s（需要联网）；"
                       "请重新下载安装包覆盖安装（数据保留）" % ver)
    print("[update] 重建虚拟环境：Python %s → %s" % (cur or "?", ver), flush=True)
    new = HERE / ".venv.new"
    _sh.rmtree(new, ignore_errors=True)
    c, _o, _e = _run([py, "-m", "venv", str(new)], timeout=300)
    np = _venv_py(new)
    if c != 0 or not np.exists():
        uv = _uv_bin()
        if uv:   # python -m venv 不可用（缺 ensurepip）时退到 uv venv
            _run([uv, "venv", "--python", py, str(new)], timeout=300)
    np = _venv_py(new)
    if not np.exists():
        _sh.rmtree(new, ignore_errors=True)
        return False, "建不出新虚拟环境（Python %s）；请重新下载安装包覆盖安装" % ver
    pip = _venv_pip(new)
    if not pip.exists():
        _run([str(np), "-m", "ensurepip", "--upgrade"], timeout=300)
    req = str(HERE / "requirements.txt")
    okdep = False
    mirrors = ([], ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
               ["-i", "https://mirrors.aliyun.com/pypi/simple"])
    if pip.exists():
        for extra in mirrors:
            c, _o, _e = _run([str(pip), "-q", "install", "--timeout", "8", "--retries", "1",
                              "--only-binary=av", "-r", req] + extra, timeout=900)
            if c == 0:
                okdep = True
                break
        # P2P 可选依赖：装不上不影响主功能
        for extra in mirrors:
            if _run([str(pip), "-q", "install", "--timeout", "8", "--retries", "1",
                     "--only-binary=av", "aiortc>=1.10"] + extra, timeout=900)[0] == 0:
                break
    if not okdep:
        uv = _uv_bin()
        if uv:
            for extra in ([], ["--default-index", "https://pypi.tuna.tsinghua.edu.cn/simple"]):
                c, _o, _e = _run([uv, "pip", "install", "-q", "--python", str(np),
                                  "--only-binary=av", "-r", req] + extra, timeout=900)
                if c == 0:
                    okdep = True
                    break
            _run([uv, "pip", "install", "-q", "--python", str(np),
                  "--only-binary=av", "aiortc>=1.10"], timeout=900)
    c, _o, _e = _run([str(np), "-c", "import aiohttp, pyte, qrcode"], timeout=60)
    if c != 0:
        _sh.rmtree(new, ignore_errors=True)
        return False, "新虚拟环境依赖装不上（需要联网）；请重新下载安装包覆盖安装（数据保留）"
    old = HERE / ".venv.old"
    _sh.rmtree(old, ignore_errors=True)
    try:
        if (HERE / ".venv").exists():
            (HERE / ".venv").rename(old)
        new.rename(HERE / ".venv")
    except Exception as ex:
        return False, "切换虚拟环境失败：%s" % str(ex)[:120]
    print("[update] 虚拟环境已切到 Python %s" % ver, flush=True)
    return True, ""


def _ensure_cli_link():
    """Linux 发行包安装的机器升级后，也要有 `codeboss` 命令（命令行客户端）。
    和 install-linux.sh 装的是同一个软链；已有指向别处的软链就改指过来，
    但绝不覆盖用户自己的同名真文件。macOS 用 Codeboss.app，不碰。"""
    if plat.IS_MAC or IS_WIN:
        return   # macOS 用 Codeboss.app；Windows 没有 codeboss-cli（用手机/网页端）
    cli = HERE / "codeboss-cli"
    if not cli.exists():
        return
    try:
        os.chmod(cli, 0o755)
    except OSError:
        pass
    for d in ("/usr/local/bin", str(Path.home() / ".local/bin")):
        try:
            if os.path.abspath(d) == "/usr/local/bin" and not os.access(d, os.W_OK):
                continue
            os.makedirs(d, exist_ok=True)
            lk = Path(d) / "codeboss"
            if lk.is_symlink():
                if lk.resolve() == cli.resolve():
                    return
                lk.unlink()
            elif lk.exists():
                return          # 用户自己的真文件，别动
            os.symlink(cli, lk)
            return
        except OSError:
            continue


# ---- 更新包签名（Ed25519）----
# 中继给的 sha256 和中继给的包是同一份数据，中继被拿下就能换包 → 必须有独立锚。
# 公钥写死在这里（发布私钥只在维护者 Mac 的 ~/.codeboss/host-signing.seed），
# 签名消息 = "<version>|<sha256>"，版本号一起签，防止拿旧签名配新版本重放。
# 实现与 tools/ed25519.py 同源；这里只保留验签（256 次点加 ≈ 20ms，一次更新跑一次）。
_SIG_PUB = bytes.fromhex("ee1054cd686afd1c6c3a8f0ad4450059f4aa6f612449be4969c53c3b7858c9f3")
_EDP = 2 ** 255 - 19
_EDL = 2 ** 252 + 27742317777372353535851937790883648493
_EDD = -121665 * pow(121666, _EDP - 2, _EDP) % _EDP
_EDI = pow(2, (_EDP - 1) // 4, _EDP)


def _ed_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _EDP
    B = (y1 + x1) * (y2 + x2) % _EDP
    C = t1 * 2 * _EDD * t2 % _EDP
    Dd = z1 * 2 * z2 % _EDP
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _EDP, G * H % _EDP, F * G % _EDP, E * H % _EDP)


def _ed_mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        s >>= 1
    return q


def _ed_xrecover(y):
    xx = (y * y - 1) * pow(_EDD * y * y + 1, _EDP - 2, _EDP) % _EDP
    x = pow(xx, (_EDP + 3) // 8, _EDP)
    if (x * x - xx) % _EDP:
        x = x * _EDI % _EDP
    if (x * x - xx) % _EDP:
        return None
    return _EDP - x if x % 2 else x


def _ed_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _EDP:
        return None
    x = _ed_xrecover(y)
    if x is None or (x & 1) != sign:
        x = _EDP - x if x is not None else None
    if x is None:
        return None
    return (x, y, 1, x * y % _EDP)


def _ed_compress(pt):
    x, y, z, _ = pt
    zi = pow(z, _EDP - 2, _EDP)
    x, y = x * zi % _EDP, y * zi % _EDP
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


_ED_BY = 4 * pow(5, _EDP - 2, _EDP) % _EDP
_ED_BX = _ed_xrecover(_ED_BY)
_ED_B = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _EDP)


def _sig_verify(pub, msg, sig):
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        A = _ed_decompress(pub)
        R = _ed_decompress(sig[:32])
        if A is None or R is None:
            return False
        S = int.from_bytes(sig[32:], "little")
        if S >= _EDL:
            return False
        k = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _EDL
        return (_ed_compress(_ed_mul(S, _ED_B)) ==
                _ed_compress(_ed_add(R, _ed_mul(k, A))))
    except Exception:
        return False


def _pkg_msg(ver, sha):
    return ("%s|%s" % (ver or "", sha or "")).encode("utf-8")


def _purge_stale_src(src, here):
    """无源码包把模块编成了 .pyc：安装目录里同名的旧 .py 会压过新 .pyc
    （Python 导入优先源码），机器「更新成功」却一直跑老代码——2026-11-23
    新加坡那台 /api/brand 一直 500，就是老 branding.py 赖着不走。"""
    for py in src.rglob("*.pyc"):
        rel = py.relative_to(src).with_suffix(".py")
        if rel.stem in ("server", "wrapper"):
            continue                     # 入口薄壳永远是包里的真源码，别误删
        try:
            (here / rel).unlink()
        except OSError:
            pass


def _apply_relay_pkg(rp):
    """下载中继上的 host 包 → 校验 sha256 → 解包 → 备份旧程序文件 → 覆盖 → 补依赖。
    数据文件（token/config/logs/.venv/notify_policy）不在包里，天然保留。
    成功返回 (True, "")；调用方负责 _self_restart() 拉起新代码。"""
    import hashlib, shutil, tarfile, tempfile, urllib.request
    base = (_relay_cfg().get("url") or "").rstrip("/")
    url = str(rp.get("url") or "/codeboss-host.tar.gz")
    if url.startswith("/"):
        url = base + url
    c, out, _e = _run(["git", "-C", str(HERE), "status", "--porcelain"], timeout=20)
    if c == 0 and any(ln.strip() and not ln.startswith("?? ")
                      for ln in (out or "").splitlines()):
        return False, "开发机有本地未提交改动，跳过更新以免覆盖（处理后自动继续）"
    tmp = Path(tempfile.mkdtemp(prefix="codeboss-relay-upd-"))
    try:
        tgz = tmp / "host.tgz"
        with urllib.request.urlopen(url, timeout=60) as r, open(tgz, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        sha = hashlib.sha256(tgz.read_bytes()).hexdigest()
        want = str(rp.get("sha256") or "")
        if want and sha.lower() != want.lower():
            return False, "sha256 校验失败（下载不完整或被替换）"
        # 签名锚：sha 和包都来自中继，单靠 sha 挡不住「中继被拿下后整套换掉」
        sig = str(rp.get("sig") or "")
        if _SIG_PUB:
            try:
                raw = bytes.fromhex(sig)
            except ValueError:
                raw = b""
            if not raw:
                return False, "更新包没有签名，拒绝安装（等中继重新发布带签名的版本）"
            if not _sig_verify(_SIG_PUB, _pkg_msg(rp.get("ver"), sha), raw):
                return False, "更新包签名校验失败（签名与版本/内容不匹配）"
        with tarfile.open(tgz) as t:
            for m in t.getmembers():
                nm = m.name.replace("\\", "/")
                if nm.startswith("/") or nm.startswith("../") or "/../" in nm:
                    return False, "包内路径不安全：%s" % nm[:80]
            try:
                t.extractall(tmp, filter="data")
            except TypeError:          # 老 Python 没 filter 参数（已做上面的路径检查）
                t.extractall(tmp)
        src = None
        for p in tmp.iterdir():
            if p.is_dir() and ((p / "install.sh").exists() or (p / "install.ps1").exists()):
                src = p
                break
        if src is None:
            return False, "包里没有安装脚本（install.sh / install.ps1）"
        got = (src / "VERSION").read_text(encoding="utf-8").strip() if (src / "VERSION").exists() else ""
        if got and str(rp.get("ver") or "") and got != str(rp.get("ver")):
            return False, "包内版本 %s 与中继声明 %s 不一致" % (got, rp.get("ver"))
        # 无源码包（.pyc）要求解释器小版本一致，且不能覆盖源码部署的工作区
        pv = src / "PYC_VER"
        if pv.exists():
            cg, _o, _e = _run(["git", "-C", str(HERE), "rev-parse", "--git-dir"], timeout=20)
            if cg == 0:
                return False, "源码部署（git 检出），跳过无源码包，改走 git 更新"
            okv, whyv = _ensure_venv_version(pv.read_text(encoding="utf-8").strip())
            if not okv:
                return False, whyv
        # 新版本的依赖先装好，再动代码：装不上就放弃这次更新，旧版本照常跑。
        # 反过来的顺序就是 0.9.54 那台：代码换了、依赖没装上、照样重启 → 起不来。
        req_new = src / "requirements.txt"
        if req_new.exists():
            okd, whyd = _deps_install(_update_py(), req_new)
            if not okd:
                return False, whyd
        # 备份当前程序文件（不含数据），万一新版本有问题可手抄回去
        bak = Path.home() / ".codeboss" / "host-backup" / time.strftime("%Y%m%d-%H%M%S")
        try:
            bak.mkdir(parents=True, exist_ok=True)
            for name in ("server.py", "wrapper.py", "relay.py", "requirements.txt",
                         "VERSION", "install.sh", "uninstall.sh", "dist.sh",
                         "plat.py", "pty_backend.py", "agents.py", "agent_codex.py",
                         "install-linux.sh",
                         "uninstall-linux.sh", "install.ps1", "uninstall.ps1",
                         "server_impl.pyc", "wrapper_impl.pyc", "plat.pyc",
                         "pty_backend.pyc", "agents.pyc", "agent_codex.pyc", "relay.pyc",
                         "static_pack.pyc", "PYC_VER"):
                f = HERE / name
                if f.exists():
                    shutil.copy2(f, bak / name)
            if (HERE / "static").exists():
                shutil.copytree(HERE / "static", bak / "static", dirs_exist_ok=True)
        except Exception as e:
            return False, "备份旧版本失败：%s" % str(e)[:120]
        for item in src.iterdir():
            dst = HERE / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
        _purge_stale_src(src, HERE)
        # 新包把网页资源编进了 static_pack.pyc：老安装留下的 static/ 目录要清掉，
        # 否则安装目录里还摆着一份可读的前端源码
        if not (src / "static").exists():
            shutil.rmtree(HERE / "static", ignore_errors=True)
        _ensure_cli_link()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:140])
    finally:
        try:
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def _gh_latest_tag(timeout=15):
    """GitHub Releases 最新 tag：直连失败依次试镜像。返回 (tag, err)。"""
    import urllib.request
    last = "no candidate"
    for u in (_GH_API,) + tuple(m + _GH_API for m in _GH_MIRRORS):
        try:
            req = urllib.request.Request(u, headers={"Accept": "application/vnd.github+json",
                                                     "User-Agent": "codeboss"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            tag = (d.get("tag_name") or "").strip().lstrip("vV")
            if tag:
                return tag, ""
            last = "Release 无 tag_name"
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:60])
    return "", last[:120]


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def update_check(force=False):
    """主通道 = 中继 host 包（正式发版都推到这里，快且不被墙）；
    中继拿不到结果时才回退 Releases API / git fetch，避免 GitHub 不通时一次检查
    卡 30-60 秒、手机端 15s 超时只能靠手动重查。6 小时节流。"""
    now = time.time()
    if not force and _update_state["at"] and now - _update_state["at"] < 6 * 3600:
        return _update_state
    _update_state.update(at=now, note="", err="", behind=0, commits=[],
                         relay_ver="", relay_pkg=None, gh_tag="")
    try:
        rp = _relay_host_ver()
        if rp:
            rv = str(rp.get("ver") or "")
            _update_state["relay_ver"] = rv
            _update_state["relay_pkg"] = {"ver": rv, "sha256": rp.get("sha256") or "",
                                          # sig 必须一起带上：_apply_relay_pkg 拿它验签，
                                          # 漏了就会被自己的签名检查拒掉（0.8.23 的教训）
                                          "sig": str(rp.get("sig") or ""),
                                          "url": rp.get("url") or "/codeboss-host.tar.gz"}
            if _ver_tuple(rv) > _ver_tuple(APP_VERSION):
                _update_state["behind"] = 1
                _update_state["note"] = "可更新到 v%s（当前 v%s）" % (rv, APP_VERSION)
        if _update_state["relay_ver"]:
            tag, api_err = "", ""
        else:
            tag, api_err = _gh_latest_tag()
        if tag:
            _update_state["gh_tag"] = tag
            if _ver_tuple(tag) > _ver_tuple(APP_VERSION):
                _update_state["behind"] = 1
                if _ver_tuple(tag) > _ver_tuple(_update_state["relay_ver"]):
                    _update_state["note"] = "可更新到 v%s（当前 v%s）" % (tag, APP_VERSION)
        elif not _update_state["relay_ver"]:
            _update_state["err"] = "检查更新失败：%s" % api_err
        code, root, _ = _run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"])
        _update_state["git"] = code == 0
        if code == 0 and not _update_state["relay_ver"]:
            root = root.strip()
            if _run(["git", "-C", root, "fetch", "--quiet"], timeout=30)[0] == 0:
                c2, out, _ = _run(["git", "-C", root, "rev-list", "--count", "HEAD..@{u}"])
                behind = int(out.strip() or 0) if c2 == 0 else 0
                if behind > 0:
                    c2, out, _ = _run(["git", "-C", root, "log", "--oneline", "-8", "HEAD..@{u}"])
                    _update_state["commits"] = [l for l in (out or "").splitlines() if l.strip()]
                    _update_state["note"] = _update_state["note"] or ("落后 %d 个提交，可更新" % behind)
                _update_state["behind"] = max(_update_state["behind"], behind)
                _update_state["err"] = ""    # git 通道出得了结果，API 失败也不算检查失败
    except Exception as e:
        _update_state["err"] = str(e)[:120]
    return _update_state


async def h_version(request):
    await require_token(request)
    # update_check 里有 git fetch / gh api 网络调用，必须丢进线程池，
    # 否则同步执行会卡住事件循环——实时/原样视图的秒级轮询会整体冻结
    force = request.query.get("force") == "1"
    st = await asyncio.get_event_loop().run_in_executor(None, update_check, force)
    return web.json_response({"ok": True, "version": APP_VERSION,
                              # disk：VERSION 文件的当前内容。自更新完成后它会先变，
                              # 服务没重启成功时仍跑旧代码（version != disk），界面要能看出来。
                              "disk": _read_version(), "pid": os.getpid(),
                              "started": STARTED_AT, "git": st["git"],
                              "update": st["behind"] > 0, "behind": st["behind"],
                              "commits": st["commits"], "note": st.get("note", ""),
                              "relay_ver": st.get("relay_ver", ""), "gh_tag": st.get("gh_tag", ""),
                              "err": st["err"]})


_UPD_BUSY = {"at": 0.0}   # 一次只跑一份更新：两个「立即更新」同时套包会互相覆盖文件，
                          # 重启被打断后服务要停一两分钟（2026-11-11 开发机 Windows 实测）。


async def _apply_update(st):
    """执行更新：git 部署 pull --ff-only / 发行包部署 gh 覆盖 → 补依赖 → 原地重启。
    h_update（手动）与 _auto_update_loop（自动）共用。
    返回 (ok, why, http_status)。"""
    if time.time() - _UPD_BUSY["at"] < 300:
        return False, "更新已经在进行中，等它重启完再看", 202
    _UPD_BUSY["at"] = time.time()
    ok, why, status = await _apply_update_once(st)
    if not ok:
        _UPD_BUSY["at"] = 0.0      # 失败要放开；成功就让它带着标记直接重启
    return ok, why, status


async def _apply_update_once(st):
    loop = asyncio.get_event_loop()
    rp = st.get("relay_pkg") or {}
    # 源码检出（开发机）永远走 git：中继包对它只有「无源码包 → 跳过」一条路，
    # 白白下一个 700KB 的包，还会先撞「本地有未提交改动」——开发机上这就是常态，
    # 每次更新都报一句失败。直接跳过，省时间也不误导人。
    if (rp and not st.get("git") and _ver_tuple(rp.get("ver")) > _ver_tuple(APP_VERSION)
            and _ver_tuple(rp.get("ver")) >= _ver_tuple(st.get("gh_tag") or "")):
        ok, why = await loop.run_in_executor(None, _apply_relay_pkg, rp)
        if ok:
            _self_restart()
            return True, "已更新到 v%s（中继发布），服务重启中，几秒后自动恢复" % rp.get("ver"), 200
        if not st["git"]:
            return False, why, 400
        print("[update] 中继包更新失败，改用原通道：%s" % why, flush=True)
    if not st["git"]:
        # 发行包安装：gh 拉最新 Release 包 → 覆盖程序文件（数据不在包里，天然保留）→ 补装依赖 → 重启
        import tempfile

        def _dl():
            tmp = Path(tempfile.mkdtemp(prefix="codeboss-upd-"))
            c, o, e = _run(["gh", "release", "download", "--repo", _RELEASE_REPO,
                            "--pattern", "codeboss-*.tar.gz", "--dir", str(tmp),
                            "--clobber"], timeout=300)
            return c, (e or o).strip(), tmp

        code, out, tmp = await loop.run_in_executor(None, _dl)
        if code != 0:
            hint = ("请到官网下载最新的 BossHost-Setup.exe，双击覆盖安装即可（配置和会话都会保留）"
                    if IS_WIN else
                    "请浏览器打开 Releases 页下载最新安装包，双击 安装.command 覆盖安装")
            return False, ("gh 下载失败（未装 gh 或未登录）：" + hint +
                           "。详情: " + out[:120]), 400
        if not list(tmp.glob("codeboss-*.tar.gz")):
            return False, "Release 里没找到 codeboss-*.tar.gz 附件", 400

        def _apply():
            import shutil
            tgz = next(tmp.glob("codeboss-*.tar.gz"))
            c, _o, e = _run(["tar", "-xzf", str(tgz), "-C", str(tmp)], timeout=120)
            if c != 0:
                return False, "解压失败: " + e[:120]
            src = next(p for p in tmp.iterdir() if p.is_dir() and p.name.startswith("codeboss-"))
            # 依赖先装好再覆盖代码（装不上就别碰旧版本）
            req_new = src / "requirements.txt"
            if req_new.exists():
                okd, whyd = _deps_install(_update_py(), req_new)
                if not okd:
                    return False, whyd
            for item in src.iterdir():
                dst = HERE / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            return True, ""

        ok, why = await loop.run_in_executor(None, _apply)
        if not ok:
            return False, why, 400
        _self_restart()
        return True, "已更新到最新版，服务重启中，几秒后自动恢复", 200
    if st["behind"] <= 0:
        # 不是失败：按「更新」但本来就没新版本。HTTP 200 + uptodate 让页面提示成中性
        # 文案，别再弹一个红色的「更新失败：已是最新版本」。
        return False, "已是最新版本", 200
    code, root, _ = _run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"])
    root = root.strip()
    code, out, _ = _run(["git", "-C", root, "status", "--porcelain"])
    mod = [l[3:] for l in (out or "").splitlines()
           if l.strip() and not l.startswith("?? ")]
    if mod:
        # 开发机的典型情形：自己改的东西**已经在上游这一版里**（改完、提交推送了，
        # 只是本地 HEAD 还停在旧提交上），工作区与上游目标逐字节相同。这时 pull
        # --ff-only 会被「本地有改动」挡住，但其实一个字符都不会丢 —— 直接对齐上游。
        # 工作区和上游确实不同（真的还有没提交的活儿）才拒绝，见下面的 409。
        _run(["git", "-C", root, "fetch", "--quiet"], timeout=30)
        dc, _do, _de = _run(["git", "-C", root, "diff", "--quiet", "@{u}"], timeout=60)
        if dc == 0:
            def _ff():
                c, o, _e = _run(["git", "-C", root, "rev-parse", "HEAD"], timeout=30)
                old = o.strip() if c == 0 else ""
                c, _o, e = _run(["git", "-C", root, "reset", "--hard", "@{u}"], timeout=120)
                if c != 0:
                    return c, e
                okd, whyd = _deps_install(_update_py(), HERE / "requirements.txt")
                if not okd:
                    if old:
                        _run(["git", "-C", root, "reset", "--hard", old], timeout=120)
                    return 1, "依赖装不上，已退回原版本：" + whyd
                return 0, e
            code, err = await loop.run_in_executor(None, _ff)
            if code == 0:
                print("[update] 本地改动与上游一致，直接对齐上游（无内容丢失）", flush=True)
                _self_restart()
                return True, "更新完成（源码检出，已对齐到上游），服务重启中，几秒后自动恢复", 200
            if err and "依赖" in err:
                return False, err.strip()[:200], 500
        return False, ("有本地改动，更新会被覆盖，请先提交或撤销：" +
                       "；".join(m.strip()[:40] for m in mod[:4])), 409

    def _do():
        c, o, _e = _run(["git", "-C", root, "rev-parse", "HEAD"], timeout=30)
        old = o.strip() if c == 0 else ""
        c, _o, e = _run(["git", "-C", root, "pull", "--ff-only", "--quiet"], timeout=180)
        if c != 0:
            return c, e
        # 依赖装不上就退回原提交：拉下来的新代码重启后可能直接起不来
        okd, whyd = _deps_install(_update_py(), HERE / "requirements.txt")
        if not okd:
            if old:
                _run(["git", "-C", root, "reset", "--hard", old], timeout=120)
            return 1, "依赖装不上，已退回原版本：" + whyd
        return 0, e

    code, err = await loop.run_in_executor(None, _do)
    if code != 0:
        return False, ("更新失败: " + (err or "")).strip()[:200], 500
    _self_restart()
    return True, "更新完成，服务重启中，几秒后自动恢复", 200


async def h_update(request):
    """手动更新入口（页面按钮）；自动更新见 _auto_update_loop。"""
    await require_token(request)
    st = await asyncio.get_event_loop().run_in_executor(None, update_check, True)
    ok, why, status = await _apply_update(st)
    d = {"ok": ok, "why": why}
    if status == 200 and not ok:      # 「已是最新版本」：中性结果，不是报错
        d["uptodate"] = 1
    return web.json_response(d, status=status)


async def _admin_update():
    """后台「立即更新」按钮（中继发 {"t":"update"}）：不等 30 分钟的自动轮询。
    跟自动更新同一条链路——先检查，有新版本才套用并重启。"""
    st = await asyncio.get_event_loop().run_in_executor(None, update_check, True)
    print("[update] 后台触发检查：%s" % (st.get("note") or "behind=%d" % st["behind"]), flush=True)
    if not st.get("behind"):
        return
    ok, why, _st = await _apply_update(st)
    print("[update] 后台触发 %s: %s" % ("OK" if ok else "SKIP/FAIL", why), flush=True)


async def _auto_update_loop(app):
    """自动更新：启动 90s 后首查，之后每 30min 查一次；发现新版本即自动套用并重启。
    有本地未提交改动时跳过本轮（开发期不被覆盖）。config.json {"auto_update": false} 可关。"""
    if not _CFG.get("auto_update", True):
        return

    async def _run():
        await asyncio.sleep(90)
        while True:
            try:
                st = await asyncio.get_event_loop().run_in_executor(None, update_check, True)
                if st["behind"] > 0:
                    print("[auto-update] 发现新版本：%s —— 自动更新" % (st.get("note") or "behind=%d" % st["behind"]),
                          flush=True)
                    ok, why, _st = await _apply_update(st)
                    print("[auto-update] %s: %s" % ("OK" if ok else "SKIP/FAIL", why), flush=True)
                    if ok:
                        return   # 1.5s 后原地重启，新代码接管
            except Exception as e:
                print("[auto-update] error:", str(e)[:120], flush=True)
            await asyncio.sleep(1800)

    app["auto_update_task"] = asyncio.create_task(_run())


_SHELL_BLOCK_HEAD = ("# >>> codeboss 全彩镜像：手敲的 claude 透明过一层 pty 代理"
                     "（终端显示不变，多 tee 一份彩色流给手机端；撤销=删这 3 行）")
_SHELL_BLOCK_TAIL = "# <<< codeboss 全彩镜像 <<<"


def _shell_alias_sync():
    """把「手敲的 claude 也走 wrapper」对齐到登录 shell 的 rc（幂等；路径变了就重写）。

    install.sh 只在源码安装时写过一次：DMG 装的用户根本没跑过 install.sh，app 更新
    换了安装路径后旧 alias 也会指丢——两种情况的表象都是「Warp 里手敲 claude 起的
    会话没有全彩镜像，手机端无窗口打不开」。放进每次服务启动，装机/升级都不用用户动手。

    用函数而不是 alias：wrapper 不见了（app 被删/挪）时还能落回真正的 claude。"""
    if not plat.IS_MAC or not _CFG.get("shell_alias", True):
        return
    py, wr = sys.executable, str(WRAPPER)
    if not (py and Path(wr).is_file()):
        return
    line = ("claude() { if [ -x %s ] && [ -f %s ]; then %s %s -- claude \"$@\"; "
            "else command claude \"$@\"; fi }"
            % (shlex.quote(py), shlex.quote(wr), shlex.quote(py), shlex.quote(wr)))
    block = "%s\n%s\n%s\n" % (_SHELL_BLOCK_HEAD, line, _SHELL_BLOCK_TAIL)
    try:
        import pwd
        shell = pwd.getpwuid(os.getuid()).pw_shell or ""
    except Exception:
        shell = ""
    rc = Path.home() / (".bash_profile" if shell.rsplit("/", 1)[-1] == "bash" else ".zshrc")
    try:
        cur = rc.read_text(encoding="utf-8") if rc.exists() else ""
    except OSError:
        return
    new, n = re.subn(r"(?ms)^# >>> codeboss 全彩镜像.*?^# <<< codeboss 全彩镜像 <<<\n?",
                     lambda m: block, cur)
    if not n:   # 没有块：追加（空文件就不加多余空行）
        new = cur + ("" if not cur or cur.endswith("\n") else "\n") + ("\n" if cur.strip() else "") + block
    if new == cur:
        return
    tmp = rc.with_name(rc.name + ".cb-tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(rc)
    print("[shell] 已%s %s：手敲 claude 走全彩镜像（新开终端生效）"
          % ("更新" if n else "写入", rc.name), flush=True)


async def _shell_alias_startup(app):
    # 失败不能影响服务启动：rc 写不进去（只读/沙箱）就先算了，下次启动再试
    try:
        _shell_alias_sync()
    except Exception as e:
        print("[shell] error:", str(e)[:120], flush=True)


async def _auto_migrate_loop(app):
    """全彩守护：启动 15s 后巡检，之后每 30s 把「空闲但没走 wrapper」的本机会话转全彩。
    只动确定空闲的会话（正在跑 turn 的不碰）；已是全彩的不动；基建目录和别的 host 实例
    托管的会话跳过，避免两边互杀。转完后会核实新会话真的起来了，没起来记失败日志。"""
    import asyncio

    async def _run():
        if IS_WIN:
            print("[guardian] Windows 无终端窗口可接管，全彩守护跳过", flush=True)
            return
        await asyncio.sleep(15)
        # 上次转全彩杀完没来得及重开就被重启打断的，这里补开（跟 auto_migrate 无关）
        try:
            await asyncio.get_event_loop().run_in_executor(None, _mig_pending_retry)
        except Exception as e:
            print("[guardian] 补开待重开会话出错：%s" % str(e)[:120], flush=True)
        if not AUTO_MIGRATE:
            print("[guardian] auto_migrate=false，全彩守护未启动")
            return
        print("[guardian] 全彩守护已启动：每 30s 巡检本机 claude 会话", flush=True)
        last_sig = None
        while True:
            try:
                loop = asyncio.get_event_loop()
                done, skipped, failed = await loop.run_in_executor(None, migrate_sessions)
                sig = tuple((s.get("sid"), s.get("why")) for s in skipped)
                if done or failed or (sig and sig != last_sig):
                    print("[guardian] +%d 全彩 %s；失败 %d %s；跳过 %d %s" % (
                        len(done), [d["sid"] for d in done], len(failed),
                        [f["sid"] for f in failed], len(skipped),
                        [(s.get("sid"), s.get("why")) for s in skipped][:3]), flush=True)
                last_sig = sig
            except Exception as e:
                print("[guardian] error:", e, flush=True)
            await asyncio.sleep(30)

    app["auto_migrate_task"] = asyncio.create_task(_run())
    # 事件循环卡顿看门狗也在这拉起（on_startup 有运行中的循环；模块级 create_task 会炸）。
    # 2026-11-26 教训：插代码把它挤进 _loop_stall_dump 的函数体死区后，看门狗线程
    # 整个不存在，冻结时一个栈都抓不到。AST 回归测试 test_watchdog_not_swallowed 钉死。
    app["loop_watch_task"] = asyncio.create_task(_loop_watch())


_LOOP_DUMP_AT = [0.0]   # 上次抓栈时刻（节流）：同一段卡顿只刷一次，恢复后重新武装


def _dump_main_stack():
    """主线程（事件循环）当前栈打一行进 server.log。"""
    try:
        import traceback
        frames = sys._current_frames()
        main_id = threading.main_thread().ident
        st = "".join(traceback.format_stack(frames[main_id])[-10:])
        print("[loop] 卡顿抓栈（事件循环线程，另有 %d 线程）：\n%s"
              % (len(frames) - 1, st), flush=True)
    except Exception:
        try:
            import faulthandler
            faulthandler.dump_traceback(file=sys.stdout, all_threads=False)
        except Exception:
            pass


def _loop_stall_dump(beat, now, threshold=1.5, min_gap=60.0, dump=None):
    """事件循环冻结时抓主线程栈。返回是否抓了。

    以前只在后台开了日志上报才抓——2026-11-26 整站冻结 20 分钟（recent_dirs 在
    循环上 stat 外置盘路径，USB 盘休眠窗口卡进内核）本地排查没有现场栈，逼得
    人工 sample。抓栈必须无条件，靠 min_gap 节流防刷屏；恢复后解封节流。"""
    if now - (beat.get("t") or 0.0) <= threshold:
        _LOOP_DUMP_AT[0] = 0.0
        return False
    if now - _LOOP_DUMP_AT[0] < min_gap:
        return False
    _LOOP_DUMP_AT[0] = now
    (dump or _dump_main_stack)()
    return True


async def _loop_watch():
    # 事件循环卡顿探针：>1.5s 的停顿都记一行。「推送中断已切回轮询」若再频发，
    # 看这里有没有对应卡顿——没有就说明卡点不在 host（中继隧道/DC 质量/客户端）。
    # 另起线程 dump 全部线程栈：卡住的那一刻事件循环线程的 Python 栈直接指出是谁在等。
    import threading

    beat = {"t": time.monotonic()}

    async def _pulse():
        # 心跳必须由事件循环自己打：另起线程打没用，线程照跑就看不出循环被卡。
        while True:
            beat["t"] = time.monotonic()
            await asyncio.sleep(0.2)

    def _watch():
        while True:
            time.sleep(1.0)
            if _loop_stall_dump(beat, time.monotonic(), threshold=1.5, min_gap=60.0):
                time.sleep(3)   # 别把同一段卡顿重复刷屏

    asyncio.create_task(_pulse())
    threading.Thread(target=_watch, daemon=True).start()
    threading.Thread(target=_ansi_metas_loop, daemon=True).start()

    last = time.monotonic()
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        beat["t"] = now
        lag = now - last - 5
        if lag > 1.5:
            print("[loop] 卡顿 %.1fs" % lag, flush=True)
        last = now


_screen_cache = {"wid": None, "at": 0.0, "ok": False, "text": ""}


def ext_screen(s, force=False):
    """读终端的当前屏幕文本（含转圈等过程态；0.8s 缓存）

    第三方终端（Warp/iTerm/VS Code…）没有 AppleScript 那种可读窗口，window_id 为空；
    有全彩镜像的会话直接用镜像的当前一屏，别让「没窗口」退化成「什么都干不了」。"""
    import time as _time
    wid = s.get("window_id")
    key = wid or ("mirror:%s" % (s.get("sid") or ""))
    now = _time.time()
    if not force and _screen_cache["wid"] == key and now - _screen_cache["at"] < 0.8:
        return _screen_cache["ok"], _screen_cache["text"]
    hs = _win_host_session(s)
    if hs is not None:
        cols = hs.screen.columns
        text = "\n".join(_row_plain(hs.screen.buffer[y], cols)
                         for y in range(hs.screen.lines))
        _screen_cache.update(wid=key, at=now, ok=True, text=text)
        return True, text
    if not wid:
        meta = _meta_for_pid(s.get("pid"))
        if meta:
            with _MIRROR_LOCK:
                st = _mirror_feed_inner(s.get("sid") or "", meta)
            if st:
                cols, rin = st["win"]
                text = "\n".join(_row_plain(st["scr"].buffer[y], cols) for y in range(rin))
                _screen_cache.update(wid=key, at=now, ok=True, text=text)
                return True, text
        return False, "未找到对应的终端窗口（可能已关闭）"
    ok, text = plat.screen(wid, s.get("tty") or "")
    _screen_cache.update(wid=key, at=now, ok=ok, text=text)
    return ok, text


# ---- 会话命令识别：长按会话行 → 扫两屏终端文本提 shell 命令 → 存 cmd_hist → 弹层选一条在新标签跑 ----
_CMD_MAX = 500   # 单条候选的长度上限：长 && 链（挂快照/拷文件）200 字根本不够
_CMD_WORDS = set(("npm pnpm yarn bun node npx deno python python3 pip pip3 uv poetry cargo rustc go make cmake "
                  "gcc g++ clang javac java mvn gradle docker docker-compose kubectl helm brew port "
                  "curl wget ssh scp sftp rsync git hg svn terraform ansible vagrant "
                  "ffmpeg sox convert magick sqlite3 psql mysql redis-cli "
                  "pytest tox black ruff flake8 pylint swift xcodebuild pod gem composer php "
                  "sh bash zsh ls cp mv rm mkdir touch chmod chown tar zip unzip gzip grep awk sed find which "
                  "echo cat head tail wc sort uniq cut tr tee xargs date sleep kill ps top lsof df du "
                  "ifconfig ipconfig netstat ping ping6 dig nslookup traceroute scutil networksetup "
                  "launchctl defaults osascript open pbcopy pbpaste sw_vers uname sysctl system_profiler "
                  "systemctl journalctl service apt apt-get dpkg yum dnf rpm pacman snap flatpak tmux screen "
                  "export source alias cd pwd env nproc sw_vers").split())


def _scan_cmds(text):
    """终端文本 → shell 命令候选：claude 命令卡（⏺ Bash(cmd)、新版 TUI 的 ⎿ $ cmd）、
    提示符行（$/❯/➜/>）、常见命令词开头的行；去重保序。截断的命令（… 结尾）不可运行，丢弃。
    ponytail: 启发式识别，误报由弹层里的人工选择兜底，不值得上模型。"""
    out = []
    for raw in (text or "").splitlines():
        t = raw.rstrip()
        # 聊天里给的「在会话里执行」命令常带 `! ` 前缀（本产品一直这么写）。
        # 识别时按去掉前缀的内容判断，但**显示/入库保留原样**——用户可能就是要把
        # `! 命令` 贴回 Claude 输入框在会话里跑；▶ 执行时再剥掉（见 h_ext_cmdrun）。
        bang = bool(re.match(r"^\s*!\s+\S", t))
        t = re.sub(r"^\s*!\s+(?=\S)", "", t)
        card = False   # ⎿ $ 卡片：结构上是命令本体，放宽中文过滤
        m = re.match(r"^\s*[⏺●]\s*Bash\((.*)\)\s*$", t)
        cand = m.group(1).strip() if m else ""
        if not cand:
            m = re.match(r"^\s*⎿\s*\$\s*(\S.*)$", t)   # 新版 TUI：● 标题行下面 ⎿  $ cmd
            if m:
                card, cand = True, m.group(1).strip()
        if not cand:
            m = re.match(r"^\s*[$❯➜>]\s*(\S.*)$", t)
            cand = m.group(1).strip() if m else ""
        if not cand:
            first = t.strip().split(" ", 1)[0]
            if first in ("sudo", "nohup", "time", "watch", "env") and t.strip().count(" ") >= 1:
                cand = t.strip()   # 包装词开头（sudo tcpdump …）
            elif first and (first in _CMD_WORDS or first.startswith(("./", "../"))):
                cand = t.strip()
        if not cand or len(cand) > _CMD_MAX:
            continue
        if re.match(r"^\d+[.)]\s", cand):   # TUI 编号选项行（❯ 1. 打开…）
            continue
        # 校验统一交给 _cmd_ok（截断 / heredoc 没结束 / 引号没闭合 / 中文说明都在那里），
        # 这里只管「从哪一行抠出候选」。
        # 中文字符只有在引号外面才算说明文字：`echo "== 地址 =="` 是正经命令。
        if not _cmd_ok(cand):
            continue
        if bang:
            cand = "! " + cand
        if cand not in out:
            out.append(cand)
    return out[:30]


_CJK = re.compile(r"[\u4e00-\u9fff]")
_TRUNC_TAIL = ("&&", "||", "|", ";", "-c \"", "-c '", "...", "…", "\\")


def _quotes_closed(s):
    """引号是不是在结尾之前都闭合了。截断的命令最常见的样子就是停在引号里。"""
    in_s = in_d = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and not in_s:
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        i += 1
    return not (in_s or in_d)


def _cmd_ok(c):
    """终审：只有「能直接跑」的才算命令。模型、正文提取、终端屏幕、转录四条来源
    都过这一道，进历史之前也过 —— 截断的、只有包装词的、带中文说明的一律不要。
    聊天里给的 `! 命令` 前缀（在本会话里直接执行）不算命令本体，按去掉后的内容审。"""
    s = (c or "").strip()
    if re.match(r"^!\s+\S", s):
        s = re.sub(r"^!\s+", "", s)
    if not s or len(s) > _CMD_MAX:
        return False
    first = s.splitlines()[0].strip()
    if not first:
        return False
    # 中文字符只有在**引号外面**才算「说明文字」。`echo "== 地址 =="` 是正经命令，
    # 不能因为带中文就被丢掉（检查 IPv6 那种场景一堆 echo 中文标题）。
    outside = re.sub(r"'[^']*'|\"[^\"]*\"", "", first)
    if _CJK.search(outside):
        return False
    if first[0] in "|>-*#+•·":
        return False
    if s.endswith(_TRUNC_TAIL):
        return False
    if re.search(r"\s-[A-Za-z]$", s):        # 停在选项上："... curl -s -o"
        return False
    if not _quotes_closed(s):                 # 停在引号里
        return False
    if "<<" in s:
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", s)
        if not m:
            return False
        tail = [l.strip() for l in s.splitlines() if l.strip()]
        if not tail or tail[-1] != m.group(1):   # heredoc 没有结束标记 = 截断
            return False
    tok = first.split(" ", 1)[0].strip("`\"'")
    if not tok:
        return False
    if len(s.split()) == 1 and tok in ("sudo", "nohup", "time", "watch", "env"):
        return False                              # 只有包装词
    return (tok in _CMD_WORDS or tok.startswith(("./", "../", "/"))
            or bool(re.match(r"^[A-Za-z0-9_.+-]+$", tok)))


def _inline_cmds(text):
    """正文里用反引号括起来的命令：`netstat -rn -f inet6 | grep '^default'`
    这种命令行首是「另外这个也行：」，按行抠抠不到，但它确实是命令。
    只收带参数的（纯单词更可能是文件名/字段名，噪声太大）。"""
    out = []
    for m in re.finditer(r"`([^`\n]{3,%d})`" % _CMD_MAX, text or ""):
        c = m.group(1).strip()
        if len(c.split()) >= 2 and _cmd_ok(c) and c not in out:
            out.append(c)
    return out


def _verbatim_in(cmd, text):
    """这条候选能在原文里逐字找到吗（只放宽空格/制表符，**不跨行**）。

    模型提取命令时是在「重建」而不是「复制」，于是会把相邻两行粘成一条、
    或者顺手改引号 —— 这种改写出来的命令跑起来必然不对。用它筛一道：
    模型给的、以及任何来源给的，只要原文里找不到就丢掉。
    """
    def nz(x):
        return re.sub(r"[ \t]+", " ", (x or "")).strip()
    return bool(cmd) and nz(cmd) in nz(text)


def _cmd_key(c):
    """去重用的归一化：空白压平、去掉聊天里的 `! ` 前缀、去掉结尾的 `;` 和 `; echo`
    （那只是让命令回显的尾巴）。"""
    s = re.sub(r"\s+", " ", (c or "").strip())
    s = re.sub(r"^!\s+", "", s)
    s = re.sub(r"\s*;\s*echo\s*$", "", s).rstrip(";").strip()
    return s


def _cmd_hist_rows(sid, limit=50):
    con = _db()
    rows = con.execute("SELECT cmd,ts FROM cmd_hist WHERE sid=? ORDER BY ts DESC LIMIT ?",
                       (sid, limit)).fetchall()
    con.close()
    return [{"cmd": c, "ts": t} for c, t in rows]


LLM_SCAN_PROMPT = (
    "从下面的对话/终端文字里提取所有「需要人工手动执行」的 shell 命令"
    "（比如 sudo、系统配置、监听抓包、cc 自己不方便跑的），以及其中明确完整出现的普通命令。"
    "只要命令本身一行文本；跳过命令输出、解释文字、编号选项、以…结尾的截断片段、heredoc(<<)片段。"
    "没有就返回[]。只输出 JSON 数组，不要任何其他文字。")


def _assistant_text(msgs, cap=6000):
    """转录里 assistant 消息正文（新→旧截到 cap）——「让人跑的命令」藏在这。"""
    parts = []
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        parts.append(m.get("text") or "")
        if sum(len(x) for x in parts) >= cap:
            break
    return "\n".join(parts)[:cap]


_LLM_COOL = [0]   # 上次失败时间；中继没部署 /api/llm 时请求会经隧道绕回自己，熔断一阵别再绕
_LLM_COOL_SEC = 120   # 10 分钟太长：中继重启/网络抖动一次，识别功能会「静默死」十分钟


async def _scan_cmds_llm(text):
    """大模型兜底：key 只在中继，host 走 /api/llm。失败返回 []，识别降级为启发式结果。"""
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec) or not (text or "").strip():
        return []
    if time.time() - _LLM_COOL[0] < _LLM_COOL_SEC:
        return []
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm",
                              json={"prompt": LLM_SCAN_PROMPT, "text": text[-6000:]},
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                d = await r.json(content_type=None)
    except Exception as e:
        _LLM_COOL[0] = time.time()
        print("[scan] 大模型调用失败（%s），%d 秒内只用启发式识别"
              % (str(e)[:100], _LLM_COOL_SEC), flush=True)
        return []
    if not d.get("ok"):
        _LLM_COOL[0] = time.time()
        print("[scan] 大模型返回失败（%s），%d 秒内只用启发式识别"
              % (str(d.get("error") or "")[:100], _LLM_COOL_SEC), flush=True)
        return []
    m = re.search(r"\[.*\]", d.get("text") or "", re.S)
    try:
        arr = json.loads(m.group(0)) if m else []
    except Exception:
        return []
    return [c.strip() for c in arr if isinstance(c, str) and c.strip() and len(c) <= _CMD_MAX][:30]


_TR_CACHE = {"k": None, "msgs": []}


def _tr_load(path, limit=400):
    """转录解析带缓存（同一文件 mtime/size 没变直接回内存副本），并且只在 executor 线程跑——
    大 jsonl 全量 readlines 曾把事件循环卡住数秒，push 心跳断流，客户端集体报「直连中断」。"""
    try:
        st = path.stat()
    except (OSError, AttributeError):
        return []
    k = (str(path), st.st_mtime_ns, st.st_size)
    if _TR_CACHE["k"] == k:
        return _TR_CACHE["msgs"]
    ad = agents.default()
    msgs = ad.parse_transcript(path, limit) if ad else []
    _TR_CACHE.update(k=k, msgs=msgs)
    return msgs


def _human_cmds(msgs, limit=10):
    """cc 在对话正文里让「人」去跑的命令（sudo/networksetup/抓包监听等 cc 自己执行不了的）。
    来源=转录 jsonl 里 assistant 消息的文本：反引号代码段 + 白名单开头的行。这是长按识别的重点——
    工具卡片是 cc 自己跑过的，人要跑的只出现在正文里。"""
    words = set(_CMD_WORDS) | {"sudo", "nohup", "time", "watch", "env"}
    out = []

    def _ok(c):
        if not c or len(c) > _CMD_MAX or c.endswith(("…", "...")) or "<<" in c:
            return False
        first = c.split(" ", 1)[0].strip("`\"'")
        if not first or first == c:   # 裸词不算（正文里枚举 sudo/nohup/… 的行）
            return False
        return first in words or first.startswith(("./", "../"))

    for m in reversed(msgs):                        # 新消息在前
        if m.get("role") != "assistant":
            continue
        txt = m.get("text") or ""
        cands = []
        for seg in re.findall(r"`([^`\n]{3,%d})`" % _CMD_MAX, txt):   # 行内代码段
            cands.append(seg.strip())
        for raw in txt.splitlines():                        # 整行命令（代码块行）
            t = raw.strip()
            if t.startswith("`"):
                continue
            cands.append(t)
        for c in cands:
            c = re.sub(r"^!\s+", "", c.strip())   # 聊天里的「在会话里执行」前缀
            if _ok(c) and c not in out:
                out.append(c)
                if len(out) >= limit:
                    return out
    return out




async def h_ext_scan(request):
    await require_token(request)
    # 识别命令是 Pro / Max 功能（会调大模型）：免费版不给。真正的主闸门在中继，
    # 这里挡的是局域网直连 / P2P（中继看不到的那条路）。
    if not _feat_on("terminal"):
        return web.json_response(
            {"ok": False, "feature": "terminal",
             "error": "识别命令是 Pro / Max 套餐的功能，开通后使用"}, status=403)
    sid = request.match_info["sid"]
    s = next((x for x in await asyncio.get_event_loop().run_in_executor(None, _tick_sessions)
              if x["sid"] == sid), None)
    if not s:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    # 页面把「它自己正显示的内容」发上来才是准的：用户在手机上看的是全彩消息流，
    # 和开发机终端那一屏（pyte 镜像）不是同一个东西，拿终端屏去识别必然对不上。
    # 只有页面没给（老页面/原样视图 xterm 读不出文字）才回退到抓终端。
    client_text = ""
    try:
        body = await request.json()
        client_text = str((body or {}).get("text") or "")[:12000]
    except Exception:
        client_text = ""
    meta = _meta_for_pid(s.get("pid"))
    text = None
    llm_text = ""
    if client_text:
        text = llm_text = client_text
    elif meta:
        def _grab():
            with _MIRROR_LOCK:
                st = _mirror_feed_inner(sid, meta)
                if not st:
                    return None
                cols, rin = st["win"]
                # 只取当前这一屏：连「上一屏」都不要 —— 用户说的就是「识别我眼前看到的」，
                # 混进上一屏会让结果里冒出他已经滚过去、屏幕上根本看不到的命令。
                lines = [_row_plain(st["scr"].buffer[y], cols) for y in range(rin)]
                # 模型只看当前一屏：输入量自己可控，别把转录/回滚也塞进去
                cur = "\n".join(_row_plain(st["scr"].buffer[y], cols) for y in range(rin))
                return "\n".join(lines), cur
        got = await asyncio.get_event_loop().run_in_executor(None, _grab)
        if got:
            text, llm_text = got
    if text is None:      # 非全彩会话退化为当前一屏（AppleScript contents）
        ok, text = ext_screen(s)
        if not ok:
            return web.json_response({"ok": False, "error": text})
        llm_text = text
    # 优先级：大模型挑的（人要跑的为主）> 正文白名单 > 屏幕卡片/提示符 > cc 自己跑过的
    cwd = s.get("cwd") or ""
    p = (agents.get(s.get("agent")) or agents.default()).transcript_path(cwd, sid)
    t0 = time.time()
    msgs = await asyncio.get_event_loop().run_in_executor(None, _tr_load, p) if p else []
    llm = await _scan_cmds_llm(llm_text[-6000:])
    # 只认「这一屏」：以前把整份转录的 Bash 卡片和正文命令全并进来（各最多 10 条），
    # 于是经常冒出十几条屏幕上根本看不到的命令。
    # 但也不能纯看屏幕：Claude 的 TUI 会把多行命令截成 `● Bash(xxx…`，屏幕上那半截
    # 是跑不了的（带 … 的一律丢弃）。所以转录只取**最近两条 assistant 消息**——
    # 也就是当前这一轮，屏幕上正显示的就是它，能拿到完整命令。
    recent = [m for m in msgs if m.get("role") == "assistant"][-2:]
    # 页面送来的是它正在显示的内容：那就**只用这份**，不要再从转录里补。
    # （转录那两路本来是为了补终端截断，手机上的全彩视图是完整文本，不需要。）
    # 顺序有讲究：正则按**行**抠，不会把相邻两行粘成一条；模型偶尔会把
    # `echo "标题"` 和下一行的命令拼成一条（实测把 IPv6 那段拼坏了）。
    # 所以正则结果在前，模型只作补充。
    # 模型那一路额外加一条：**必须逐字出现在屏幕上**。它是在「重建」命令，
    # 会粘行、会改引号，改写出来的东西跑起来必然不对；原文里找不到就不可信。
    llm_ok = [x for x in llm if _cmd_ok(x) and _verbatim_in(x, text)]
    if client_text:
        # 页面送来的「屏上文字」来自终端镜像，长行是按终端宽度折过的（实测把
        # `/…/vibecoding-terminal` 折成 `vibecoding-ter minal`，跑起来路径就不对了）。
        # 转录里的原文是准的：把最近两条 assistant 消息里的命令也并进来兜底，
        # 用户能在弹层里看到「同一条命令的正确版本」。
        sources = (_scan_cmds(text) + _inline_cmds(text)
                   + _human_cmds(recent, 4) + llm_ok)
    else:
        sources = (_scan_cmds(text) + llm_ok
                   + _transcript_cmds(recent, 6) + _human_cmds(recent, 4))
    cmds = []
    seen = set()
    for c in sources:
        if not _cmd_ok(c):          # 四条来源统一过终审，进历史之前就挡住垃圾
            continue
        k = _cmd_key(c)
        if k and k not in seen:      # 去重：空白归一、去掉结尾的 ; / ; echo
            seen.add(k)
            cmds.append(c)
    # 模型偶尔把相邻两行粘成一条（"echo \"标题\" ifconfig …"）。这种候选必然
    # 以另一条候选开口，直接丢掉，只留干净的那条。
    cmds = [c for c in cmds
            if not any(o != c and c.startswith(o + " ") for o in cmds)]
    print("[scan] sid=%s %.2fs cmds=%d llm=%d 页面送屏=%d字 回退抓屏=%d字"
          % (sid[:8], time.time() - t0, len(cmds), len(llm),
             len(client_text), 0 if client_text else len(text or "")), flush=True)
    if not cmds:   # 没认出来时把收到的开头打出来，下次一眼就对上了
        _src = client_text or (text or "")
        print("[scan]   没认出命令，收到的是（前 240 字）：%r" % _src[:240], flush=True)
    if request.query.get("dbg"):   # 排查识别不到命令：回传服务端实际看到的原文
        return _jsonz({"ok": True, "cmds": cmds, "hist": [], "dbg": text[-3000:]})
    ts = time.time()
    con = _db()
    for c in cmds:
        con.execute("INSERT INTO cmd_hist(sid,cmd,ts,cnt) VALUES(?,?,?,1)"
                    " ON CONFLICT(sid,cmd) DO UPDATE SET ts=excluded.ts,cnt=cnt+1", (sid, c, ts))
    con.commit()
    con.close()
    return _jsonz({"ok": True, "cmds": cmds, "hist": _cmd_hist_rows(sid)})


async def h_ext_cmddel(request):
    """从命令历史里删一条（按 sid + 命令原文精确匹配），返回删完后的历史。"""
    await require_token(request)
    sid = request.match_info["sid"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"})
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return web.json_response({"ok": False, "error": "命令为空"})
    try:
        con = _db()
        con.execute("DELETE FROM cmd_hist WHERE sid=? AND cmd=?", (sid, cmd))
        con.commit()
        con.close()
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:120]})
    return _jsonz({"ok": True, "hist": _cmd_hist_rows(sid)})


async def h_ext_cmdhist(request):
    await require_token(request)
    return _jsonz({"ok": True, "hist": _cmd_hist_rows(request.match_info["sid"])})


async def h_ext_cmdrun(request):
    """在 host 自管的 PTY 会话里跑命令（不动 Terminal：本机 do script in window
    实测不建新标签而是打进当前选中 tab，会把命令当 prompt 注入会话——绝不能再用）。
    命令跑完后 exec zsh -i 留着交互 shell，输出和提示符都留在屏幕上给手机端轮询。"""
    await require_token(request)
    sid = request.match_info["sid"]
    data = await request.json()
    cmd = (data.get("cmd") or "").strip()
    # 聊天里的 `! 命令` 是「在会话里直接执行」的前缀，不是 shell 语法：
    # ▶ 在真实 shell 里跑时必须剥掉，否则 `!sudo …` 会 command not found。
    cmd_run = re.sub(r"^!\s+", "", cmd) if re.match(r"^!\s+\S", cmd) else cmd
    if not cmd_run or len(cmd_run) > 500:
        return web.json_response({"ok": False, "error": "命令为空或过长"})
    s = await _find_session(sid)
    if not s:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    cwd = s.get("cwd") or str(Path.home())
    # 先把命令本身打一行（青色 $ 提示），否则终端里只有输出，看不出跑的是哪条。
    # 非零退出码再补一行红字 —— 像 `curl -6 -s` 这种「静默失败」的，
    # 否则终端里一片空白，用户会以为根本没执行。
    # 结束标记每次都打：
    #   有输出 → 输出 + (完成)
    #   没输出 → 只有 (完成)    ← 否则一片空白，用户以为没执行（curl -s 静默失败就是这样）
    #   失败   → (退出码 N) 红字
    _cwd_q = shlex.quote(Path(cwd).as_posix() if IS_WIN else cwd)
    _sh_q = shlex.quote(_user_shell())
    shell = ("cd %s && printf '\\033[36m$ %%s\\033[0m\\n' %s; %s; "
             "rc=$?; if [ $rc -eq 0 ]; then printf '\\033[90m(完成)\\033[0m\\n'; "
             "else printf '\\033[31m(退出码 %%d)\\033[0m\\n' $rc; fi; exec %s -i"
             % (_cwd_q, shlex.quote(cmd_run), cmd_run, _sh_q))
    csid = uuid.uuid4().hex[:8]
    print("[cmdrun] sid=%s 新建终端 csid=%s 命令=%r" % (sid[:8], csid, cmd_run[:80]), flush=True)
    argv = (pty_backend.shell_wrap(shell) if IS_WIN
            else [_user_shell(), "-c", shell])
    try:
        sess = Session(csid, "cmd: " + cmd_run[:24], cwd, argv)
    except Exception as e:
        return web.json_response({"ok": False, "error": "启动失败: %s" % e})
    sess.hidden = True   # 不进 🌐 会话列表（手机列表只留 claude 会话）
    SESSIONS[csid] = sess
    _CMD_RUNS.append({"sid": sid, "csid": csid, "cmd": cmd_run, "ts": time.time()})
    while len(_CMD_RUNS) > 10:   # 上限 10 个运行终端，最老的杀掉防泄漏
        old = _CMD_RUNS.pop(0)
        old_s = SESSIONS.pop(old["csid"], None)
        if old_s:
            try:
                old_s.kill()
            except Exception:
                pass
    con = _db()   # 跑过也算用过：刷 ts 让历史按最近使用排
    con.execute("INSERT INTO cmd_hist(sid,cmd,ts,cnt) VALUES(?,?,?,1)"
                " ON CONFLICT(sid,cmd) DO UPDATE SET ts=excluded.ts,cnt=cnt+1",
                (sid, cmd, time.time()))
    con.commit()
    con.close()
    return web.json_response({"ok": True, "csid": csid})


# ── 测试红绿卡（2026-11-19 计划 A1）────────────────────────────────────
# 只认验证类命令（复用 outcome 的判定：pytest / npm test|build|lint / ./x-test.sh …），
# 且不许 shell 串联——卡片重跑只跑一条，跑完按退出码落红绿。
_TEST_BAD_SHELL = re.compile(r"[;&|`$><\n\r]")
_TEST_TAIL_MAX = 2048


def _test_cmd_allowed(cmd):
    c = str(cmd or "").strip()
    if not c or len(c) > 500 or _TEST_BAD_SHELL.search(c):
        return False
    c = outcome._EXEC_PREFIX_RX.sub("", c)   # 去掉 FOO=1 sudo env 前缀再判定
    return bool(outcome._VERIFY_HEAD_RX.match(c) or outcome._PY_M_RX.match(c)
                or outcome._SH_RX.match(c))


def _test_exec(cmd, cwd, timeout=600):
    """同步跑一条验证命令：返回 (退出码, 输出含 stderr)。超时算失败。"""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + "\n(超时 %ds)" % timeout
    except Exception as e:
        return 126, str(e)


def _test_cards_payload(sid, limit=5):
    con = _db()
    rows = con.execute("SELECT cmd,state,rc,ts,dur,tail,src FROM test_runs"
                       " WHERE sid=? ORDER BY ts DESC LIMIT ?", (sid, limit)).fetchall()
    con.close()
    return [{"cmd": r[0], "state": r[1], "rc": r[2], "ts": r[3], "dur": r[4],
             "tail": (r[5] or "")[-_TEST_TAIL_MAX:], "src": r[6] or ""} for r in rows]


def _record_agent_run(sid, oc):
    """agent 自己跑过的验证命令，自动落一张红绿卡（src=agent）。

    数据来自 outcome.classify 从转录里认出的退出码——不是猜的。
    120 秒内同命令同尾巴只记一次（Stop 和 agent_completed 挨着来会重复上报）。
    没有验证证据（done_unverified）不写：宁缺勿假。
    """
    oc = oc or {}
    cmd = str(oc.get("cmd") or "").strip()
    state = oc.get("state")
    if not cmd or state not in ("done_verified", "failed"):
        return False
    tail = str(oc.get("tail") or "")[-_TEST_TAIL_MAX:]
    rc = oc.get("rc")
    rc = (0 if state == "done_verified" else 1) if rc is None else int(rc)
    st = "pass" if rc == 0 else "fail"
    now = time.time()
    con = _db()
    try:
        dup = con.execute("SELECT 1 FROM test_runs WHERE sid=? AND cmd=? AND tail=?"
                          " AND ts>?", (sid, cmd, tail, now - 120)).fetchone()
        if dup:
            return False
        con.execute("INSERT INTO test_runs(sid,cmd,state,rc,ts,dur,tail,src)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sid, cmd, st, rc, now, 0, tail, "agent"))
        con.commit()
        return True
    finally:
        con.close()


async def _test_run_payload(sid, cmd):
    cmd = str(cmd or "").strip()
    if not _test_cmd_allowed(cmd):
        return {"ok": False, "error": "不在白名单：只跑 lint/test/build 命令"}
    s = await _find_session(sid)
    if not s:
        return {"ok": False, "error": "会话不存在或已结束"}
    cwd = s.get("cwd") or str(Path.home())
    t0 = time.time()
    rc, out = await asyncio.get_event_loop().run_in_executor(None, _test_exec, cmd, cwd, 600)
    dur = time.time() - t0
    state = "pass" if rc == 0 else "fail"
    tail = out[-_TEST_TAIL_MAX:]
    con = _db()
    con.execute("INSERT INTO test_runs(sid,cmd,state,rc,ts,dur,tail) VALUES(?,?,?,?,?,?,?)",
                (sid, cmd, state, rc, t0, dur, tail))
    con.commit()
    con.close()
    return {"ok": True, "item": {"cmd": cmd, "state": state, "rc": rc, "ts": t0,
                                 "dur": dur, "tail": tail}}


# ---- 网页预览（/pv）：agent 起的 dev server，手机上直接打开看真页面 -------------
# 只连 127.0.0.1、端口只认数字——这个口子是给人看自己机器的网页的，不是通用的翻墙代理。
_PV_SKIP_PORTS = {22, 8790}          # sshd / 本机 host 自己：不给看
_PV_ATTR_RX = re.compile(r'(\b(?:src|href|action)\s*=\s*["\'])(/(?!/)[^"\']*)', re.I)
_PV_CSS_RX = re.compile(r'url\(\s*(["\']?)(/(?!/)[^)"\']*)')


def _pv_port(v):
    try:
        n = int(str(v or "").strip())
    except Exception:
        return 0
    return n if 1 <= n <= 65535 and n not in _PV_SKIP_PORTS else 0


def _pv_rewrite_prefix(html, pre):
    """页面里的绝对路径 /x 改成 <pre>/x：手机上是走中继的，
    不重写就会去中继根上找资源，整页 404。//cdn 这种协议相对地址不动。"""

    def fix(p):
        return p if p.startswith(pre + "/") or p.startswith("/pv/") else pre + p

    html = _PV_ATTR_RX.sub(lambda m: m.group(1) + fix(m.group(2)), html)
    html = _PV_CSS_RX.sub(lambda m: "url(" + m.group(1) + fix(m.group(2)), html)
    return _pv_inject_shim(html, pre)


def _pv_rewrite(html, port):
    return _pv_rewrite_prefix(html, "/pv/%d" % port)


# 静态改写（上面两个正则）管不到页面 JS 在运行时自己拼的同源绝对路径：
# deeptutor 这类 SPA 的登录 fetch 是 new URL('/api/auth/login', location.origin)
# 拼出来的，在手机上会打到中继根上（表现为登录页画得出来、"request failed"）。
# 这段小脚本随 HTML 注入，把 fetch/XHR/WebSocket/EventSource 的同源绝对路径
# 统一补上预览前缀。行为测试：tests/test_pv_shim.mjs。
# WebView 70 口径：全 ES5 写法。协议相对(//)和 wss 绝对地址因 origin 串不相等天然放行。
_PV_SHIM_JS = r'''function __pvShim(pre) {
  if (window.__pvShimmed) return;
  window.__pvShimmed = true;
  function u(x) {
    try {
      var s = (typeof x === 'string') ? x : String(x);
      var a = new URL(s, window.location.href);
      if (a.origin !== window.location.origin) return null;
      var p = a.pathname;
      if (p.charAt(0) !== '/') return null;
      if (p === pre || p.lastIndexOf(pre + '/', 0) === 0) return null;
      return pre + p + a.search + a.hash;
    } catch (e) { return null; }
  }
  var of = window.fetch;
  if (of) window.fetch = function (i, init) {
    var f = u(i);
    return of.call(this, f === null ? i : f, init);
  };
  var XHR = window.XMLHttpRequest;
  if (XHR) {
    var oo = XHR.prototype.open;
    XHR.prototype.open = function (m, x) {
      var f = u(x);
      if (f === null) return oo.apply(this, arguments);
      return oo.apply(this, [m, f].concat([].slice.call(arguments, 2)));
    };
  }
  var OW = window.WebSocket;
  if (OW) {
    var NW = function (u2, pr) {
      var f = u(u2);
      if (f !== null)
        f = (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + f;
      else f = u2;
      return pr === undefined ? new OW(f) : new OW(f, pr);
    };
    NW.prototype = OW.prototype;
    try { NW.CONNECTING = OW.CONNECTING; NW.OPEN = OW.OPEN;
          NW.CLOSING = OW.CLOSING; NW.CLOSED = OW.CLOSED; } catch (e) {}
    window.WebSocket = NW;
  }
  var OE = window.EventSource;
  if (OE) {
    var NE = function (u2, cfg) {
      var f = u(u2);
      return cfg === undefined ? new OE(f === null ? u2 : f) : new OE(f === null ? u2 : f, cfg);
    };
    NE.prototype = OE.prototype;
    try { NE.CONNECTING = OE.CONNECTING; NE.OPEN = OE.OPEN; NE.CLOSED = OE.CLOSED; } catch (e) {}
    window.EventSource = NE;
  }
}
window.__pvShim = __pvShim;'''


def _pv_inject_shim(html, pre):
    """把运行时 shim 注到 <head> 开头（没有 head 就顶到文档最前）——
    必须先于应用脚本执行。注入过的不重复（幂等）。"""
    if "__pvShim" not in html:
        tag = "<script>" + _PV_SHIM_JS + '</script><script>__pvShim("' + pre + '")</script>'
        m = re.search(r"<head\b[^>]*>", html, re.I)
        i = m.end() if m else 0
        html = html[:i] + tag + html[i:]
    return html


def _pv_scope_cookie(v, pre):
    """Set-Cookie 的 Path 钉到预览前缀上：backend 设的 Path=/ 会话 cookie 若原样
    落到手机，就成了整个中继源的 cookie，会跟着 App 自己的请求到处发。
    没写 Path 的不动——浏览器默认按响应 URL 目录收窄，本来就出不了前缀。"""
    return re.sub(
        r"(?i)(;\s*path\s*=\s*)(?!" + re.escape(pre) + r")([^;]*)",
        lambda m: m.group(1) + pre +
        (("/" + m.group(2).lstrip("/")) if m.group(2).strip("/") else "/"),
        v, count=1)


_PV_DEV_PROC_RX = re.compile(
    r"^(node|nodejs|node\.\w+|npm|npx|next|vite|nuxt|python[\d.]*|ruby|php[\d.]*|java|"
    r"dotnet|go|bun|deno|caddy|nginx|httpd|hugo|jekyll|mkdocs|uvicorn|gunicorn)", re.I)
_PV_KNOWN_PORTS = {3000, 3001, 4200, 4321, 5000, 5173, 5174, 5500, 7000,
                   7777, 8000, 8080, 8081, 8888, 9000}
_PV_MAX_PROBE = 40       # 最多探这么多个端口：机器上的系统 RPC 端口再多也不至于卡住手机


def _pv_ports_of(text):
    """lsof 输出 → [(端口, 进程名, 局域网可达)]（去掉 sshd/本机 host；1024 以下不探）。
    以前只留「常见端口 + 像开发服务器的进程」，3D 预览这类冷门服务就漏了
    （2026-11-19 反馈：电脑上打开的网页在 App 里看不到）。现在全量交给探测：
    h_pv_index 逐个访问，只有真答 text/html 的才显示；进程名先带着，慢服务也不丢。
    局域网可达 = 监听地址不是回环（*: 通配 / [::]: / 绑在具体局域网地址上）——
    同网段的手机能直连 http://<ip>:<port>/，WS 这类反带走不通的路径就靠它。"""
    seen = {}
    for ln in str(text or "").splitlines():
        m = re.search(r"^(\S+).*?\s(\S+):(\d+)\s+\(LISTEN\)\s*$", ln)
        if not m:
            continue
        proc, addr, p = m.group(1), m.group(2), int(m.group(3))
        if p in _PV_SKIP_PORTS or not (1024 <= p <= 65535):
            continue
        e = seen.setdefault(p, [proc, False])
        e[1] = e[1] or addr not in ("127.0.0.1", "::1", "[::1]")
    # 探测名额有限：开发服务/常见端口先探，系统端口排后面，别把真要看的挤出去
    dev = lambda kv: kv[0] in _PV_KNOWN_PORTS or bool(_PV_DEV_PROC_RX.match(kv[1] or ""))
    return sorted(((p, v[0], v[1]) for p, v in seen.items()),
                  key=lambda kv: (not dev(kv), kv[0]))[:_PV_MAX_PROBE]


def _pv_show_port(port, proc, html_ok):
    """探测确认是网页的留下；没答 HTML 的只有「常见端口/开发进程」才保留
    （服务刚起/第一次编译慢，探不到标题不等于不是网页）。"""
    return bool(html_ok) or port in _PV_KNOWN_PORTS or bool(_PV_DEV_PROC_RX.match(proc or ""))


def _pv_title_of(html):
    """从网页 HTML 里抠 <title>：比进程名更能说明「这是哪个页面」。"""
    import html as _html
    m = re.search(r"<title[^>]*>(.*?)</title>", str(html or ""), re.I | re.S)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
    return t[:40]


async def _pv_label_of(port, proc):
    """起得快的服务回个页面标题，起不来/不是网页就只显示进程名。
    html=True 表示真答了一个网页——列表靠它过滤掉一堆系统端口。"""
    from aiohttp import ClientSession, ClientTimeout
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.5)) as s:
            async with s.get("http://127.0.0.1:%d/" % port, allow_redirects=False) as r:
                if "text/html" not in (r.headers.get("Content-Type") or ""):
                    return {"port": port, "proc": proc, "title": "", "html": False}
                body = await r.content.read(65536)
        return {"port": port, "proc": proc, "html": True,
                "title": _pv_title_of(body.decode("utf-8", "replace"))}
    except Exception:
        return {"port": port, "proc": proc, "title": "", "html": False}


# ---- 电脑浏览器当前网址 ----
_PV_BROWSER_APPS = ("Google Chrome", "Safari", "Brave Browser", "Microsoft Edge", "Arc")


def _front_browser_url():
    """电脑浏览器正开着的那一页（Chrome 系 / Safari）。取不到回空串。
    只问正在跑的 app：AppleScript 的 tell 会把没启动的浏览器拉起来。
    走 lsappinfo/System Events 判「最前面」要额外授权，这里按常见度挨个问，
    第一个答得上来的就是用户正看的那个。"""
    for app in _PV_BROWSER_APPS:
        try:
            if subprocess.run(["pgrep", "-x", app], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=2).returncode != 0:
                continue
        except Exception:
            continue
        prop = ("URL of current tab of front window" if app == "Safari"
                else "URL of active tab of front window")
        try:
            r = subprocess.run(["osascript", "-e", "with timeout of 3 seconds",
                                "-e", 'tell application "%s" to get %s' % (app, prop),
                                "-e", "end timeout"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            continue
        u = r.stdout.decode("utf-8", "replace").strip()
        if u.startswith(("http://", "https://", "file://")):
            return u
    return ""


_PV_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


def _pv_browse_entry(url, sid=""):
    """浏览器地址 → App 里点得开的 {"href","label","local"}；开不了的回 None。
    本机网页转成 /pv 反代，file:// 转成 /pvfile，外部站点原样交给系统浏览器。"""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(str(url or "").strip())
        port = u.port
    except ValueError:
        return None
    if u.scheme in ("http", "https"):
        host = (u.hostname or "").lower()
        path = u.path or "/"
        tail = path.lstrip("/") + (("?" + u.query) if u.query else "")
        if host in _PV_LOCAL_HOSTS:
            p = port or (443 if u.scheme == "https" else 80)
            if p in _PV_SKIP_PORTS:
                return None
            return {"href": "/pv/%d/%s" % (p, tail), "local": True,
                    "label": "%s%s" % (host, path)}
        return {"href": str(url), "local": False, "label": "%s%s" % (host, path)}
    if u.scheme == "file":
        from urllib.parse import unquote, quote
        p = unquote(u.path)
        rel = _pvfile_rel(p, sid)
        if not rel:
            return None
        return {"href": "/pvfile/%s/%s" % (
                    sid or "-", "/".join(quote(s, safe="") for s in rel.split("/"))),
                "local": True, "label": p.rsplit("/", 1)[-1]}
    return None


def _pv_listen_ports():
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6)
        return _pv_ports_of(r.stdout.decode("utf-8", "replace"))
    except Exception:
        return []


def _pv_row_html(port, label, lan_ok, lan_ip):
    """预览列表的一行。局域网可达且拿得到本机局域网 IP 时带 data-lan：
    页面脚本先探直连（壳的同网闸门管着，跨网段探针必败），通了整层留在浮层里
    直开——WS/会话流这类反带走不通的路径就活了；探不通回退 /pv 反代。"""
    lab = _html_mod.escape(str(label or "")) or _html_mod.escape(str(port))
    lan = ""
    if lan_ok and lan_ip:
        lan = ' data-lan="%s"' % _html_mod.escape("http://%s:%d/" % (lan_ip, port), quote=True)
    return '<a class="p" href="/pv/%d/"%s><b>:%d</b><span>%s</span></a>' % (port, lan, port, lab)


_PV_LAN_PROBE_JS = r'''function __pvLan(lans, hp) {
  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll('a[data-lan]'));
  }
  function toProxy() {
    rows().forEach(function (a) { a.removeAttribute('data-lan'); });
  }
  function toLan(ip) {
    rows().forEach(function (a) {
      var m = (a.getAttribute('href') || '').match(/\/pv\/(\d+)/);
      if (m) a.setAttribute('href', 'http://' + ip + ':' + m[1] + '/');
      a.removeAttribute('data-lan');
    });
  }
  var ls = [];
  (lans || []).forEach(function (x) { if (x) ls.push(String(x)); });
  if (!ls.length) { toProxy(); return; }
  var nonce = (Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2)).slice(0, 32);
  var done = false;
  var to = setTimeout(function () {
    if (!done) { done = true; toProxy(); }
  }, 1500);
  function settle(ok, ip) {
    if (done) return;
    done = true;
    clearTimeout(to);
    if (ok) toLan(ip); else toProxy();
  }
  var pending = ls.length;
  function giveUp() {
    pending -= 1;
    if (pending <= 0) settle(false, '');
  }
  ls.forEach(function (ip) {
    var x = new XMLHttpRequest();
    x.open('GET', 'http://' + ip + ':' + hp + '/api/ping?pr=' + nonce, true);
    x.onreadystatechange = function () {
      if (x.readyState !== 4 || done) return;
      /* 200 ≠「是开发机」：壳的同网闸门对跨网段会代答 404（曾把 fetch 当成
         「通」→ 浮层直跳够不着的地址全黑屏），运营商劫持会答 200。和主页面
         probePing 同款三重验证：status 200 + ok:1 + nonce 回声。 */
      if (x.status !== 200) { giveUp(); return; }
      var d = null;
      try { d = JSON.parse(x.responseText); } catch (e) {}
      if (d && d.ok === 1 && (d.pr === undefined || d.pr === nonce)) settle(true, ip);
      else giveUp();
    };
    x.onerror = function () { if (!done) giveUp(); };
    try { x.send(null); } catch (e) { if (!done) giveUp(); }
  });
}'''


async def h_pv_index(request):
    """/pv：这台电脑上正看着的东西 + 开着的网页服务，点一个就看（在 App 的浮层里）。
    每行带上进程名和网页标题，光一个端口号用户不知道自己点的是什么。
    最上面那条是「电脑浏览器当前这一页」——手机上接着看，不用再找地址。"""
    await require_token(request)
    sid = (request.query.get("sid") or "").strip()
    loop = asyncio.get_event_loop()
    ports, browse, lans = await asyncio.gather(
        loop.run_in_executor(None, _pv_listen_ports),
        loop.run_in_executor(None, _front_browser_url),
        loop.run_in_executor(None, _lan_ips))
    lan_ip = (lans or [""])[0]
    it = [i for i in await asyncio.gather(*[_pv_label_of(p, proc) for p, proc, _lan in ports])
          if _pv_show_port(i["port"], i["proc"], i.get("html"))]
    lan_of = dict((p, lan) for p, _proc, lan in ports)
    pages = ""
    if sid:                       # 产出物里的网页：点一个就在这儿看效果（文件里看的是源码）
        try:
            from urllib.parse import quote as _q
            # 产出物扫描是同步 os.walk：放大仓库里能跑几十秒，整页别等它
            d = await asyncio.wait_for(loop.run_in_executor(
                None, lambda: asyncio.run(_ext_artifacts_payload(sid))), 3.0)
            es = [e for e in d["entries"]
                  if e["name"].lower().endswith((".html", ".htm"))][:10]
            if es:
                pages = ('<h3>产出物里的网页（点开自动找服务 / 起服务）</h3>' + "".join(
                    '<a class="p" href="/api/ext_web?open=1&sid=%s&path=%s">'
                    '<b>🌐</b><span>%s</span></a>'
                    % (_q(sid), _q(e["rel"]), _html_mod.escape(e["rel"])) for e in es))
        except Exception:
            pages = ""
    rows = "".join(
        _pv_row_html(i["port"], i["title"] or i["proc"],
                     lan_of.get(i["port"], False), lan_ip) for i in it) or \
        '<div class="e">没发现本机开着的网页服务——先让 agent 把服务起起来（npm run dev / python -m http.server）</div>'
    entry = _pv_browse_entry(browse, sid) if browse else None
    top = ('<h3>电脑上正在看</h3><a class="p" href="%s"><b>💻</b><span>%s</span></a>'
           % (_html_mod.escape(entry["href"], quote=True),
              _html_mod.escape(entry["label"]))) if entry else ""
    body = ('<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<title>预览</title><style>'
            'body{margin:0;padding:16px;background:#0b0e14;color:#c9d1d9;'
            'font:14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}'
            'h3{margin:4px 0 12px;font-size:15px;color:#e6edf3}'
            '.p{display:flex;align-items:baseline;gap:10px;padding:12px 14px;margin-bottom:8px;'
            'border:1px solid #30363d;border-radius:10px;background:#1b2230;color:#79c0ff;'
            'text-decoration:none;font-size:15px}'
            '.p b{font-size:15px;white-space:nowrap}'
            '.p span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
            'font-size:13px;color:#8b949e;text-align:right}'
            '.e{padding:14px;border:1px dashed #30363d;border-radius:10px;color:#8b949e;line-height:1.6}'
            'form{margin-top:14px;display:flex;gap:8px}'
            'input{flex:1;min-width:0;padding:10px;border-radius:8px;border:1px solid #30363d;'
            'background:#0d1117;color:#e6edf3;font-size:15px}'
            'button{padding:10px 16px;border-radius:8px;border:1px solid #30363d;'
            'background:#238636;color:#fff;font-size:15px}'
            '</style>' + top + pages + '<h3>这台电脑上开着的网页服务</h3>' + rows +
            '<form onsubmit="location.href=\'/pv/\'+this.p.value+\'/\';return false">'
            '<input name="p" inputmode="numeric" placeholder="端口，比如 3000">'
            '<button>打开</button></form>'
            + '<script>' + _PV_LAN_PROBE_JS + '</script>'
            + '<script>__pvLan(%s,%d)</script>'
            % (json.dumps(lans or []), PORT))
    return web.Response(text=body, content_type="text/html", charset="utf-8")


async def h_pv(request):
    """把 /pv/<端口>/<剩下的路径> 原样转给 127.0.0.1:<端口>，HTML 顺手改绝对路径。"""
    await require_token(request)
    port = _pv_port(request.match_info.get("port"))
    if not port:
        raise web.HTTPNotFound()
    tail = request.match_info.get("tail") or ""
    from aiohttp import ClientSession, ClientTimeout
    url = "http://127.0.0.1:%d/%s" % (port, tail)
    qs = request.query_string
    if qs:
        url += "?" + qs
    data = await request.read()
    hdr = {}
    for k in ("Accept", "Content-Type", "Accept-Language"):
        if request.headers.get(k):
            hdr[k] = request.headers[k]
    # 会话 cookie 两头都要通：登录是 cookie 会话（dt_token），不转发的话
    # Set-Cookie 到不了手机、后续请求也带不上，登录成功下一步就 401。
    if request.headers.get("Cookie"):
        hdr["Cookie"] = request.headers["Cookie"]
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as s:
            async with s.request(request.method, url, data=data or None, headers=hdr) as r:
                body = await r.read()
                ct = r.headers.get("Content-Type") or "application/octet-stream"
                if "text/html" in ct:
                    body = _pv_rewrite(body.decode("utf-8", "replace"), port).encode("utf-8")
                out = web.Response(body=body, status=r.status)
                out.headers["Content-Type"] = ct
                out.headers["Cache-Control"] = "no-store"
                for k in ("Location", "Content-Disposition"):
                    if r.headers.get(k):
                        out.headers[k] = r.headers[k]
                for v in r.headers.getall("Set-Cookie", []):
                    out.headers.add("Set-Cookie", _pv_scope_cookie(v, "/pv/%d" % port))
                return out
    except Exception as e:
        msg = ('<div style="font:14px -apple-system,sans-serif;color:#c9d1d9;background:#0b0e14;'
               'padding:20px;line-height:1.7"><b>连不上 :%d</b><br>服务没起、或端口不对。'
               '<br><span style="color:#8b949e">%s</span></div>'
               % (port, str(e)[:160]))
        return web.Response(text=msg, content_type="text/html", charset="utf-8", status=502)


# ---- 电脑里的网页文件（/pvfile）：浏览器用 file:// 打开的 HTML，手机上也当网页看 ----
def _pvfile_rel(path, sid=""):
    """本地绝对路径 → 相对某个允许目录的路径；不在允许目录里就返回空。"""
    try:
        p = os.path.realpath(os.path.expanduser(str(path or "")))
    except Exception:
        return ""
    for r in _media_roots(sid):
        if p.startswith(r + os.sep):
            return p[len(r) + 1:]
    return ""


def _pvfile_path(sid, tail):
    """URL 里的相对路径 → (文件, 允许的根)；越界/凭据名/不存在都回 (None, "")。"""
    from urllib.parse import unquote
    rel = unquote(str(tail or "")).lstrip("/")
    for root in _media_roots(sid):
        try:
            p = _ext_guard_path(root, rel)
        except _ExtPathError:
            continue
        if p.is_dir():
            p = p / "index.html"
        if p.is_file():
            return p, root
    return None, ""


_PVFILE_MIME = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
                ".svg": "image/svg+xml", ".wasm": "application/wasm"}


async def h_pvfile(request):
    """GET /pvfile/<sid>/<rel>：把电脑里的 HTML 当网页打开（不是贴源码）。
    同目录的 css/js/图片按原结构取；HTML 里的绝对路径改写到本文件所在目录。
    Range 照做，页面里的 <video> 能拖进度条。"""
    await require_token(request)
    sid = request.match_info.get("sid") or ""
    if sid == "-":
        sid = ""
    tail = request.match_info.get("tail") or ""
    p, _root = _pvfile_path(sid, tail)
    if not p:
        raise web.HTTPNotFound()
    size = p.stat().st_size
    if size > _EXT_FILE_MAX:
        raise web.HTTPRequestEntityTooLarge(max_size=_EXT_FILE_MAX, actual_size=size)
    ext = p.suffix.lower()
    if ext in (".html", ".htm"):
        # 绝对路径 /x 映到允许目录的根（和 /pv 一样按「站点根」算）；
        # 相对路径本来就按当前 URL 解析，不用动。
        pre = "/pvfile/%s" % (sid or "-")
        body = _pv_rewrite_prefix(p.read_text("utf-8", "replace"), pre).encode("utf-8")
        return web.Response(body=body, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
    ct = _PVFILE_MIME.get(ext) or _EXT_MIME.get(ext) or \
        (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
    start, end = 0, size - 1
    rng = (request.headers.get("Range") or "").strip()
    if rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        try:
            if a:
                start, end = int(a), (int(b) if b else size - 1)
            elif b:
                start, end = max(0, size - int(b)), size - 1
        except ValueError:
            pass
        start, end = max(0, min(start, size - 1)), max(0, min(end, size - 1))
    part = bool(rng) and (start != 0 or end != size - 1)
    hdr = {"Content-Type": ct, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if part:
        hdr["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    resp = web.StreamResponse(status=206 if part else 200, headers=hdr)
    resp.content_length = end - start + 1
    await resp.prepare(request)
    with open(p, "rb") as f:
        f.seek(start)
        left = end - start + 1
        while left > 0:
            buf = f.read(min(256 * 1024, left))
            if not buf:
                break
            await resp.write(buf)
            left -= len(buf)
    return resp


# ---- 产出物里的网页：文件看源码，预览看效果（2026-11-20）--------------------
# 点网页文件在「文件」里出源码；要看效果就来这儿：已经在跑的服务答得出这个文件就用
# 它的端口，否则按文件所在目录起一个只绑 127.0.0.1 的静态服务，统一走 /pv 反代。
_WEB_SRV = {}            # 目录 → {"port","proc"}；同一目录复用，别每点一次起一个
_WEB_SRV_MAX = 8         # 最多同时开 8 个：超了先关最老的（都是临时的）


def _web_free_port():
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _web_static_stop(d):
    e = _WEB_SRV.pop(d, None)
    if e:
        try:
            e["proc"].terminate()
        except Exception:
            pass


def _web_static_stop_all():
    for d in list(_WEB_SRV):
        _web_static_stop(d)


def _web_static_ensure(d):
    """目录 → 静态服务端口（已起就复用；进程死了重起）。"""
    e = _WEB_SRV.get(d)
    if e:
        if e["proc"].poll() is None:
            return e["port"]
        _WEB_SRV.pop(d, None)
    while len(_WEB_SRV) >= _WEB_SRV_MAX:
        _web_static_stop(next(iter(_WEB_SRV)))
    port = _web_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port),
         "--bind", "127.0.0.1", "--directory", d],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _WEB_SRV[d] = {"port": port, "proc": proc}
    return port


def _web_wait(port, secs=2.0):
    """等静态服务真的在听：不然紧接着的 iframe 会先拿到 502。"""
    import socket
    import time
    end = time.time() + secs
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


@atexit.register
def _web_static_atexit():
    _web_static_stop_all()


async def _pv_serves(port, rel):
    """这个端口是不是答得出这个文件（200 + text/html）。"""
    from urllib.parse import quote as _q
    from aiohttp import ClientSession, ClientTimeout
    url = "http://127.0.0.1:%d/%s" % (port, _q(rel))
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.5)) as s:
            async with s.get(url) as r:
                return r.status == 200 and "text/html" in (r.headers.get("Content-Type") or "")
    except Exception:
        return False


async def _ext_web_payload(sid, path):
    """产出物里的网页 → 预览里的查看地址 {mode, port, href, url}。"""
    from urllib.parse import quote as _q
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or ""
    if not cwd:
        raise _ExtPathError("会话不存在或没有 cwd")
    p = _ext_guard_path(cwd, path)
    if not p.is_file() or p.suffix.lower() not in (".html", ".htm"):
        raise _ExtPathError("不是网页文件：%s" % path)
    rel = p.relative_to(Path(cwd).resolve()).as_posix()
    loop = asyncio.get_event_loop()
    for port, _proc in await loop.run_in_executor(None, _pv_listen_ports):
        if await _pv_serves(port, rel):
            return {"mode": "running", "port": port,
                    "href": "/pv/%d/%s" % (port, _q(rel)),
                    "url": "http://127.0.0.1:%d/%s" % (port, rel)}
    port = await loop.run_in_executor(None, _web_static_ensure, str(p.parent))
    await loop.run_in_executor(None, _web_wait, port)
    return {"mode": "static", "port": port,
            "href": "/pv/%d/%s" % (port, _q(p.name)),
            "url": "http://127.0.0.1:%d/%s" % (port, p.name)}


async def h_ext_web(request):
    """GET /api/ext_web?sid=&path=[&open=1]
    open=1 直接 302 到 /pv/<端口>/…（预览列表里的链接就这么点）；
    不带则回 JSON（文件页里「看效果」先把地址亮出来）。"""
    await require_token(request)
    q = request.query
    try:
        d = await _ext_web_payload(q.get("sid") or "", q.get("path") or "")
    except _ExtPathError as e:
        if q.get("open"):
            return web.Response(text=str(e), status=403)
        return _ext_err(e)
    if q.get("open"):
        raise web.HTTPFound(d["href"])
    return _jsonz(d)


# ---- 装到手机（APK）：会话项目里构建出来的安装包，手机上一键下载安装 -----------
_APK_MAX = 300 * 1024 * 1024
_APK_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".gradle", ".idea"}


def _find_apks(root, limit=6, max_depth=5):
    """会话 cwd 里最新的几个 .apk（build/dist 不跳过——APK 就在那儿出的）。"""
    out, seen = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        seen += 1
        if seen > 20000:
            break
        depth = dirpath[len(root):].count(os.sep)
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _APK_SKIP_DIRS]
        if depth >= max_depth:
            dirnames[:] = []
        for f in filenames:
            if not f.lower().endswith(".apk"):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size <= 0 or st.st_size > _APK_MAX:
                continue
            out.append({"rel": os.path.relpath(p, root), "name": f,
                        "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


async def h_apk_find(request):
    """这个会话的项目里有没有构建好的 APK（手机点一下就装）。"""
    await require_token(request)
    sid = request.query.get("sid") or ""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd")
    if not cwd:
        return _jsonz({"ok": False, "error": "会话不存在或已结束"})
    items = await asyncio.get_event_loop().run_in_executor(None, _find_apks, cwd)
    from urllib.parse import quote
    for it in items:
        it["url"] = "/api/apk_get?sid=%s&rel=%s" % (quote(sid), quote(it["rel"]))
    return _jsonz({"ok": True, "items": items})


async def h_apk_get(request):
    """把会话项目里的 .apk 流给手机（壳收到 vnd.android.package-archive 会走安装）。"""
    await require_token(request)
    sid = request.query.get("sid") or ""
    rel = request.query.get("rel") or ""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd")
    if not cwd:
        raise web.HTTPNotFound()
    try:
        p = _ext_guard_path(cwd, rel)
    except _ExtPathError as e:
        return _ext_err(e)
    if not p.is_file() or p.suffix.lower() != ".apk":
        raise web.HTTPNotFound()
    if p.stat().st_size > _APK_MAX:
        return web.json_response({"error": "APK 超过 %dMB" % (_APK_MAX // 1048576)}, status=413)
    return web.FileResponse(p, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="%s"' % p.name.replace('"', "")})


_SHOT_VIEW_HTML = (
    '<!doctype html><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">'
    '<title>拍屏</title><style>'
    'html,body{margin:0;height:100%;background:#000;overflow:hidden}'
    '#wrap{position:fixed;top:0;left:0;right:0;bottom:0;overflow:hidden;touch-action:none}'
    '#im{position:absolute;left:0;top:0;width:100%;transform-origin:0 0;will-change:transform}'
    '#hint{position:fixed;left:0;right:0;bottom:10px;text-align:center;color:#8b949e;'
    'font:12px -apple-system,sans-serif;pointer-events:none;transition:opacity .4s}'
    '</style><div id="wrap"><img id="im" src="__SRC__"></div>'
    '<div id="hint">双指放大 · 双击复位 · 放大后单指拖动</div>'
    '<script>(function(){'
    'var im=document.getElementById("im"),wrap=document.getElementById("wrap"),'
    'hint=document.getElementById("hint");'
    'var s=1,tx=0,ty=0,x0=0,y0=0,d0=0,s0=1;'
    'function ap(){im.style.transform="translate("+tx+"px,"+ty+"px) scale("+s+")";'
    'hint.style.opacity=s>1.02?"0":"1";}'
    'function d2(t){var a=t[0],b=t[1];return Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);}'
    'wrap.addEventListener("touchstart",function(e){'
    'if(e.touches.length===2){d0=d2(e.touches);s0=s;e.preventDefault();}'
    'else if(e.touches.length===1){x0=e.touches[0].clientX-tx;y0=e.touches[0].clientY-ty;}'
    '},{passive:false});'
    'wrap.addEventListener("touchmove",function(e){'
    'if(e.touches.length===2&&d0>0){'
    's=Math.min(6,Math.max(1,s0*d2(e.touches)/d0));'
    'if(s<1.02){s=1;tx=0;ty=0;}ap();e.preventDefault();}'
    'else if(e.touches.length===1&&s>1.02){'
    'tx=e.touches[0].clientX-x0;ty=e.touches[0].clientY-y0;ap();e.preventDefault();}'
    '},{passive:false});'
    'wrap.addEventListener("touchend",function(e){if(e.touches.length<2)d0=0;});'
    'var lt=0;'
    'wrap.addEventListener("click",function(){var n=Date.now();'
    'if(n-lt<300){s=s>1.02?1:2.5;tx=ty=0;ap();}lt=n;});'
    '})();</script>')


# ── 模型路由（2026-11-26）：magpie 底座 ──────────────────────────────
# 手机上看/切各 agent 的模型。本机 magpie（模型路由网关 127.0.0.1:3425）在就用它；
# 不在且包内自带了 magpie 就拉起 headless 网关；都没有就 available=false 降级可见。
# 只做「路由状态 + 切换」两件事——供应商/key 管理、订阅、插件留给 magpie 本机界面。

_MAGPIE_URL = "http://127.0.0.1:3425"
_MAGPIE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._\-\[\]]{0,80}$")
# magpie 的 agent 名全是 Title Case（Claude Code / Codex / Hermes Agent…），
# 小写注入（a b / env FS）在参数关就被拒；后面还有「必须在 ls 里」二道门
_MAGPIE_AGENT_RE = re.compile(r"^[A-Z][A-Za-z0-9]*(?: [A-Z][A-Za-z0-9]*){0,3}$")
_MAGPIE_SPAWN_TS = [0.0]


def _magpie_bin():
    """可执行解析：PATH → ~/.local/bin/magpie（官方安装默认位；launchd 的 PATH 短，
    用户装了也常只落在这里）→ 包内自带（magpie-bin/，MIT 单二进制可随包分发）。"""
    p = shutil.which("magpie")
    if p:
        return p
    local = Path.home() / ".local" / "bin" / ("magpie.exe" if sys.platform == "win32" else "magpie")
    if local.exists():
        return str(local)
    bundled = _shared("magpie-bin") / ("magpie.exe" if sys.platform == "win32" else "magpie")
    if bundled.exists():
        return str(bundled)
    return None


def _magpie_cooling():
    """拉起冷却：10 分钟内不重复 spawn（探测离线≠拉起失败，可能在启动中）。"""
    return (time.time() - _MAGPIE_SPAWN_TS[0]) < 600


def _magpie_spawn(bin_):
    """自带兜底：headless 网关形态（web --gateway --no-open）——不弹窗口不抢 TTY。
    脱离本会话独立跑，数据目录与用户自装的 magpie 共用（~/.config/magpie）。"""
    try:
        subprocess.Popen([bin_, "web", "--gateway", "--no-open"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        _MAGPIE_SPAWN_TS[0] = time.time()
    except Exception:
        pass


async def _magpie_snapshot():
    """GET /v1/magpie/quotas：200+JSON 即在线（官方脚本化端点），顺带拿额度。
    任何失败=(False, None)——探测超时 2s，不拖慢手机请求。"""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
            async with s.get(_MAGPIE_URL + "/v1/magpie/quotas") as r:
                if r.status != 200:
                    return False, None
                return True, await r.json(content_type=None)
    except Exception:
        return False, None


def _magpie_run(*args, timeout=20):
    """跑 magpie CLI 回 stdout；找不到可执行/非零退出/超时都回 None。
    stderr 是启动日志噪声（plugin: host starts with…），丢弃。"""
    bin_ = _magpie_bin()
    if not bin_:
        return None
    try:
        r = subprocess.run([bin_, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def _parse_magpie_ls(text):
    """magpie ls 表格 → {agent: 模型或None}。双空格分列：parts[0]=名，parts[1]=模型字段；
    字段含 em-dash「—」（—/provider —/auth —）视为未设。"""
    out = {}
    for line in (text or "").splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2 or not parts[0]:
            continue
        name, field = parts[0], parts[1]
        if name.startswith("~") or name.startswith("/"):
            continue
        out[name] = None if "—" in field else field
    return out


def _parse_magpie_models(text):
    """magpie models <agent> 表格 → [{id,name,group}]。含「/」的行是模型条目，
    否则是组标题（Routing groups / DeepSeek / Zhipu GLM…）。"""
    out, group = [], None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "/" not in s:
            group = s
            continue
        parts = re.split(r"\s{2,}", s)
        if len(parts) >= 2:
            out.append({"id": parts[0], "name": parts[1], "group": group or ""})
    return out


async def h_model_router(request):
    """模型路由总览：各 agent 当前模型 + 各供应商额度窗 + bundled 标记。
    magpie 离线且包内自带时顺带拉起（带冷却）——用户下次拉就是在线的。"""
    await require_token(request)
    online, quotas = await _magpie_snapshot()
    agents, bundled = [], False
    if not online:
        bin_ = _magpie_bin()
        bundled = bin_ is not None
        if bundled and not _magpie_cooling():
            _magpie_spawn(bin_)
    else:
        bundled = _magpie_bin() is not None
        ls = await asyncio.to_thread(_magpie_run, "ls")
        if ls:
            agents = [{"name": n, "model": m} for n, m in _parse_magpie_ls(ls).items()]
    return _jsonz({"ok": 1, "available": online, "agents": agents,
                   "quotas": (quotas or {}).get("data") or [], "bundled": bundled})


async def h_model_router_models(request):
    """某个 agent 可选的模型清单（按供应商分组）。"""
    await require_token(request)
    agent = (request.query.get("agent") or "").strip()
    if not _MAGPIE_AGENT_RE.match(agent):
        return web.json_response({"ok": 0, "error": "agent 不合法"}, status=400)
    out = await asyncio.to_thread(_magpie_run, "models", agent)
    return _jsonz({"ok": 1, "models": _parse_magpie_models(out or "")})


async def h_model_router_set(request):
    """切换某个 agent 的模型。argv 直传不经 shell；model 过白名单正则（含 ../ 拒），
    agent 过 Title Case 正则 + 必须在 ls 结果里两道门。
    CLI 在线程池跑（magpie 一次要 1~3s，直接 await 会卡住整个 host 事件循环）；
    App 忙时（app-server 重启/批量迁移）CLI 偶发非零退出——1.5s 后重试一次。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": 0, "error": "bad json"}, status=400)
    agent = str(data.get("agent") or "").strip()
    model = str(data.get("model") or "").strip()
    if not _MAGPIE_AGENT_RE.match(agent) or ".." in model or not _MAGPIE_MODEL_RE.match(model):
        return web.json_response({"ok": 0, "error": "参数不合法"}, status=400)
    if not _magpie_bin():
        return web.json_response({"ok": 0, "error": "本机没有模型路由（magpie）"}, status=503)

    def _do():
        if agent not in _parse_magpie_ls(_magpie_run("ls") or ""):
            return None, "不认识的 agent：" + agent
        out = _magpie_run(agent, model)
        if out is None:
            time.sleep(1.5)
            out = _magpie_run(agent, model)
        return out, None if out is not None else "切换失败"

    out, err = await asyncio.to_thread(_do)
    if err:
        return web.json_response({"ok": 0, "error": err}, status=500)
    return _jsonz({"ok": 1, "agent": agent, "model": model})


_ROUTER_UI_CSS = """
:root{--bg:#0e1116;--card:#161b22;--fg:#e6edf3;--dim:#8b949e;--line:#21262d;--acc:#2f81f7;--ok:#3fb950}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:14px 12px 40px}
h2{font-size:15px;margin-bottom:10px;color:var(--dim);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-bottom:9px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.name{font-weight:600}
.model{color:var(--dim);font-size:13px;max-width:60%;text-align:right;word-break:break-all}
.cur{color:var(--ok);font-size:12px;margin-top:2px}
.grp{color:var(--dim);font-size:12px;margin:10px 2px 4px;font-weight:600}
.mrow{padding:9px 11px;border-radius:8px;border:1px solid var(--line);margin-top:6px;cursor:pointer;background:var(--bg)}
.mrow:active{background:var(--card)}
.list{display:none;margin-top:4px}
.bar{height:5px;border-radius:3px;background:var(--line);margin-top:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc)}
.qd{color:var(--dim);font-size:12px;margin-top:3px}
.tip{color:var(--dim);font-size:13px}
button.cl{background:none;border:none;color:var(--fg);font-size:17px;padding:2px 6px;cursor:pointer}
#toast{position:fixed;left:50%;bottom:36px;transform:translateX(-50%);background:#263041;color:var(--fg);
padding:8px 16px;border-radius:20px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;white-space:nowrap;z-index:9}
#toast.on{opacity:1}
"""

_ROUTER_UI_JS = """
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function toast(msg,bad){var t=document.getElementById('toast');t.textContent=msg;
t.style.background=bad?'#5d1a1a':'#263041';t.classList.add('on');
clearTimeout(t.__h);t.__h=setTimeout(function(){t.classList.remove('on')},2200);}
function closeSheet(){try{parent.postMessage('cb-close','*')}catch(e){}}
var AGENTS=%AGENTS%,QUOTAS=%QUOTAS%,AVAIL=%AVAIL%,BUNDLED=%BUNDLED%;
function render(){
  var h='';
  if(!AVAIL){
    h+='<div class="card"><div class="name">模型路由不在线</div><div class="tip" style="margin-top:6px">'
      +(BUNDLED?'本机自带的网关正在启动，稍等几秒重新打开这个浮层。'
               :'这台电脑没有装模型路由（magpie）。在本机跑 <b>curl -fsSL https://usemagpie.ai/install.sh | sh</b> 即可接入。')
      +'</div></div>';
    document.getElementById('body').innerHTML=h;return;
  }
  h+='<h2>AGENT</h2>';
  AGENTS.forEach(function(a,i){
    h+='<div class="card" onclick="toggle('+i+')"><div class="row"><div><div class="name">'+esc(a.name)
      +'</div>'+(a.model?'<div class="cur">● '+esc(a.model)+'</div>':'<div class="qd">未选模型</div>')
      +'</div><div class="model">'+(a.model?'点击切换':'—')+'</div></div>'
      +'<div class="list" id="l'+i+'"><div class="tip">载入中…</div></div></div>';
  });
  h+='<h2 style="margin-top:16px">额度</h2>';
  (QUOTAS||[]).forEach(function(q){
    h+='<div class="card"><div class="row"><div class="name">'+esc(q.name||q.provider)
      +'</div><div class="model">'+esc(q.display||(q.balance?q.balance+' 余额':(q.plan||'')))+'</div></div>';
    (q.windows||[]).forEach(function(w){
      var r=Math.max(0,Math.min(100,w.remaining==null?0:w.remaining));
      h+='<div class="qd">'+esc(w.name||'')+' · '+esc(w.display||'')+'</div>'
        +'<div class="bar"><i style="width:'+r+'%"></i></div>';
    });
    h+='</div>';
  });
  document.getElementById('body').innerHTML=h;
}
function toggle(i){
  var el=document.getElementById('l'+i);
  if(el.style.display==='block'){el.style.display='none';return;}
  el.style.display='block';
  if(el.__loaded){return;}
  var a=AGENTS[i];
  fetch('/api/model_router/models?agent='+encodeURIComponent(a.name)).then(function(r){return r.json()}).then(function(j){
    if(!j.ok){el.innerHTML='<div class="tip">载入失败</div>';return;}
    var h='',g=null;
    (j.models||[]).forEach(function(m){
      if(m.group!==g){g=m.group;h+='<div class="grp">'+esc(g)+'</div>';}
      h+='<div class="mrow" onclick="pick('+i+',\\''+encodeURIComponent(m.id)+'\\',\\''+esc(m.name)+'\\')">'
        +'<div class="row"><div>'+esc(m.name)+'</div><div class="qd">'+esc(m.id)+'</div></div></div>';
    });
    el.innerHTML=h||'<div class="tip">没有可选模型（先在本机 magpie 里添加供应商）</div>';
    el.__loaded=1;
  }).catch(function(){el.innerHTML='<div class="tip">载入失败</div>';});
}
function pick(i,idEnc,name){
  var a=AGENTS[i];
  fetch('/api/model_router/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent:a.name,model:decodeURIComponent(idEnc)})}).then(function(r){return r.json()}).then(function(j){
    if(j.ok){a.model=j.model;toast('已切换 '+a.name+' → '+name);render();
      setTimeout(function(){var el=document.getElementById('l'+i);if(el){el.style.display='none';}},600);}
    else toast(j.error||'切换失败',true);
  }).catch(function(){toast('网络失败',true);});
}
render();
"""


async def h_model_router_ui(request):
    """模型路由浮层页（手机 sheetOpen 盖层打开）：agent 当前模型 + 额度窗 + 页内切换。
    离线时明说降级原因，不静默空白——和拍屏一样按需拉，不自动推。"""
    await require_token(request)
    online, quotas = await _magpie_snapshot()
    agents, bundled = [], False
    if not online:
        bundled = _magpie_bin() is not None
        if bundled and not _magpie_cooling():
            _magpie_spawn(_magpie_bin())
    else:
        bundled = _magpie_bin() is not None
        ls = await asyncio.to_thread(_magpie_run, "ls")
        if ls:
            agents = [{"name": n, "model": m} for n, m in _parse_magpie_ls(ls).items()]
    js = (_ROUTER_UI_JS
          .replace("%AGENTS%", json.dumps(agents, ensure_ascii=False))
          .replace("%QUOTAS%", json.dumps((quotas or {}).get("data") or [], ensure_ascii=False))
          .replace("%AVAIL%", "true" if online else "false")
          .replace("%BUNDLED%", "true" if bundled else "false"))
    html = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">'
            '<title>模型路由</title><style>' + _ROUTER_UI_CSS + '</style></head><body>'
            '<div class="row" style="margin-bottom:10px"><h2 style="margin:0">模型路由</h2>'
            '<button class="cl" onclick="closeSheet()">✕</button></div>'
            '<div id="body"></div><div id="toast"></div>'
            '<script>' + js + '</script></body></html>')
    return web.Response(text=html, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


def _set_win_dpi():
    """Windows 高缩放屏（125%/150% 很常见）不设 DPI 感知，ImageGrab 抓出来
    是缩小的图——手机放大后字糊。失败（老系统/已设过）无所谓，吞掉。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _screenshot_to(path):
    """拍一张全屏 JPEG 到 path。回 (0, "") 成功；否则 (HTTP状态码, 用户能看懂的文案)。
    macOS 走 screencapture；Windows 走 PIL ImageGrab（包里 Pillow 只在 win32 装，
    requirements 的环境标记保证它只在这条路上被 import）。"""
    if sys.platform == "darwin":
        r = subprocess.run(["screencapture", "-x", "-t", "jpg", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 1024:
            return 500, "拍屏失败（系统可能没给「屏幕录制」权限）"
        return 0, ""
    if sys.platform == "win32":
        try:
            _set_win_dpi()
            from PIL import ImageGrab
            img = ImageGrab.grab()
            if img is None:
                return 500, "拍屏失败（屏幕可能已锁定）"
            img.convert("RGB").save(path, "JPEG", quality=85)
            return 0, ""
        except Exception as e:
            return 500, "拍屏失败：" + str(e)[:80]
    return 400, "这台机器不是 macOS/Windows，暂不支持拍屏"


async def h_shot(request):
    """拍一张开发机的屏幕（macOS screencapture / Windows ImageGrab）。按需拍，不自动推——
    整屏里有别人的窗口/聊天，自动推到手机是泄私；要看得自己点。

    默认回一个能双指放大/拖动的查看页（整屏缩到手机上根本看不清字）；
    ?raw=1 回原始 JPEG。
    """
    await require_token(request)
    import base64
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="cb-shot-")
    os.close(fd)
    try:
        status, err = _screenshot_to(path)
        if status:
            return web.json_response({"ok": False, "error": err}, status=status)
        with open(path, "rb") as f:
            jpg = f.read()
        if (request.query.get("raw") or "") == "1":
            # 整张读进内存再回：FileResponse 是流式的，finally 里删了临时文件就发不出去
            return web.Response(body=jpg, content_type="image/jpeg",
                                headers={"Cache-Control": "no-store"})
        b64 = base64.b64encode(jpg).decode()
        return web.Response(text=_SHOT_VIEW_HTML.replace("__SRC__", "data:image/jpeg;base64," + b64),
                            content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def h_test_cards(request):
    await require_token(request)
    return _jsonz({"ok": True, "items": _test_cards_payload(request.query.get("sid") or "")})


_TEST_RUNNING = set()   # 正在跑的 sid（同一会话同时只跑一条；跑测试可能要几分钟）


async def h_test_run(request):
    """POST 只负责「开跑」并立刻返回——验证命令可能跑几分钟，不能把 HTTP 挂那么久
    （中继/壳都有超时）。结果由页面轮询 /api/test_cards 拿。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"})
    sid, cmd = str(data.get("sid") or ""), data.get("cmd") or ""
    if not _test_cmd_allowed(cmd):
        return web.json_response({"ok": False, "error": "不在白名单：只跑 lint/test/build 命令"})
    if not await _find_session(sid):
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    if sid in _TEST_RUNNING:
        return web.json_response({"ok": False, "error": "这个会话已有测试在跑"})
    _TEST_RUNNING.add(sid)

    async def job():
        try:
            await _test_run_payload(sid, cmd)
        finally:
            _TEST_RUNNING.discard(sid)

    asyncio.get_event_loop().create_task(job())
    return _jsonz({"ok": True, "running": True})


# ── 交付链（2026-11-19 计划 A4）：发布 → 健康卡 → 回滚 ────────────────
# 跑什么由 products/<线>/product.json 的 deploy 定；App 只触发，传不进命令。
# 配置里的命令是运维写的，但仍禁 shell 串联：手机点一下只该跑一条。
_DEPLOY_CACHE = {}


def _deploy_cfg(product):
    product = branding.valid_product(product)
    if product in _DEPLOY_CACHE:
        return _DEPLOY_CACHE[product]
    cfg = {}
    try:
        with open(_shared("products", product) / "product.json", encoding="utf-8") as f:
            raw = json.load(f).get("deploy") or {}
        for k in ("cmd", "rollback", "health_url"):
            if isinstance(raw.get(k), str) and raw[k].strip():
                cfg[k] = raw[k].strip()
    except Exception:
        pass
    _DEPLOY_CACHE[product] = cfg
    return cfg


def _deploy_cmd_ok(cmd):
    c = str(cmd or "").strip()
    return bool(c) and len(c) <= 300 and not _TEST_BAD_SHELL.search(c)


async def _deploy_exec(sid, kind, cmd):
    s = await _find_session(sid)
    if not s:
        return {"ok": False, "error": "会话不存在或已结束"}
    cwd = s.get("cwd") or str(Path.home())
    t0 = time.time()
    rc, out = await asyncio.get_event_loop().run_in_executor(None, _test_exec, cmd, cwd, 900)
    dur = time.time() - t0
    state = "pass" if rc == 0 else "fail"
    tail = out[-_TEST_TAIL_MAX:]
    con = _db()
    con.execute("INSERT INTO deploys(sid,kind,cmd,state,rc,ts,dur,tail) VALUES(?,?,?,?,?,?,?,?)",
                (sid, kind, cmd, state, rc, t0, dur, tail))
    con.commit()
    con.close()
    return {"ok": True, "item": {"kind": kind, "cmd": cmd, "state": state, "rc": rc,
                                 "ts": t0, "dur": dur, "tail": tail}}


async def _deploy_cmd_for(sid, kind, product):
    """发布/回滚命令：会话项目里配了就用项目的，否则回落产品线配置。"""
    s = await _find_session(sid)
    cwd = (s or {}).get("cwd") or str(Path.home())
    cmd = _proj_cmds(cwd).get(kind)
    if cmd:
        return cmd
    return _deploy_cfg(product).get("cmd" if kind == "deploy" else "rollback")


async def _deploy_run_payload(sid, product):
    cmd = await _deploy_cmd_for(sid, "deploy", product)
    if not cmd:
        return {"ok": False, "error": "这个项目没配发布命令（package.json scripts.deploy，"
                "或 products/%s/product.json 的 deploy.cmd）" % branding.valid_product(product)}
    if not _deploy_cmd_ok(cmd):
        return {"ok": False, "error": "发布命令不许夹带 shell 串联：%r" % cmd[:80]}
    return await _deploy_exec(sid, "deploy", cmd)


def _deploy_rows(limit=5):
    con = _db()
    rows = con.execute("SELECT kind,cmd,state,rc,ts,dur,tail FROM deploys"
                       " ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"kind": r[0], "cmd": r[1], "state": r[2], "rc": r[3],
             "ts": r[4], "dur": r[5], "tail": (r[6] or "")[-_TEST_TAIL_MAX:]} for r in rows]


async def _deploy_rollback_payload(sid, product):
    cmd = await _deploy_cmd_for(sid, "rollback", product)
    if not cmd:
        return {"ok": False, "error": "这个项目没配回滚命令（package.json scripts.deploy:rollback，"
                "或 deploy.rollback）"}
    if not _deploy_cmd_ok(cmd):
        return {"ok": False, "error": "回滚命令不许夹带 shell 串联：%r" % cmd[:80]}
    con = _db()
    ok_n = con.execute("SELECT COUNT(1) FROM deploys WHERE kind='deploy' AND state='pass'"
                       ).fetchone()[0]
    con.close()
    if not ok_n:
        return {"ok": False, "error": "还没有成功发布过，没有可回滚的发布点"}
    return await _deploy_exec(sid, "rollback", cmd)


def _health_probe(url, timeout=6):
    """探活一次（同步，调用方放 executor）：HTTP 4xx/5xx 算不健康，连不上也算。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cb-health"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = int(getattr(r, "status", 0) or r.getcode() or 0)
        return {"ok": 200 <= code < 400, "code": code, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": int(e.code), "ms": int((time.time() - t0) * 1000),
                "error": str(e)[:120]}
    except Exception as e:
        return {"ok": False, "code": 0, "ms": int((time.time() - t0) * 1000),
                "error": str(e)[:120]}


async def _health_card_payload(product):
    url = _deploy_cfg(product).get("health_url") or ""
    if url:
        health = await asyncio.get_event_loop().run_in_executor(None, _health_probe, url)
        health["url"] = url
    else:
        health = {"ok": False, "code": 0, "ms": 0, "url": "", "error": "没配健康检查地址"}
    return {"ok": True, "health": health, "deploys": _deploy_rows(5)}


async def h_deploy_run(request):
    """POST {sid} → 后台发布，页面轮询 /api/health_card 看结果（可能跑几分钟）。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"})
    sid = str(data.get("sid") or "")
    if sid in _TEST_RUNNING:
        return web.json_response({"ok": False, "error": "这个会话已有命令在跑"})
    if not await _find_session(sid):
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    product = _client_product_for(request)
    _TEST_RUNNING.add(sid)

    async def job():
        try:
            await _deploy_run_payload(sid, product)
        finally:
            _TEST_RUNNING.discard(sid)

    asyncio.get_event_loop().create_task(job())
    return _jsonz({"ok": True, "running": True})


async def h_deploy_rollback(request):
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"})
    sid = str(data.get("sid") or "")
    if sid in _TEST_RUNNING:
        return web.json_response({"ok": False, "error": "这个会话已有命令在跑"})
    if not await _find_session(sid):
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    product = _client_product_for(request)
    # 同步校验一遍「有没有可回滚的发布点」，给页面立刻的答案；真跑放后台。
    pre = _deploy_cfg(product)
    if not pre.get("rollback"):
        return web.json_response({"ok": False, "error": "这条产品线没配回滚命令（deploy.rollback）"})
    con = _db()
    ok_n = con.execute("SELECT COUNT(1) FROM deploys WHERE kind='deploy' AND state='pass'"
                       ).fetchone()[0]
    con.close()
    if not ok_n:
        return web.json_response({"ok": False, "error": "还没有成功发布过，没有可回滚的发布点"})
    _TEST_RUNNING.add(sid)

    async def job():
        try:
            await _deploy_rollback_payload(sid, product)
        finally:
            _TEST_RUNNING.discard(sid)

    asyncio.get_event_loop().create_task(job())
    return _jsonz({"ok": True, "running": True})


async def h_health_card(request):
    await require_token(request)
    return _jsonz(await _health_card_payload(_client_product_for(request)))


# ── 验收摘要（2026-11-19 计划 A5）──────────────────────────────────────
async def _accept_summary_payload(sid):
    """验收浮层用的材质：最近三轮 assistant 原文 + 最近验证红绿 → 三句摘要。
    模型不行就回落原文拼接（notify.accept_summary 负责兜底）。"""
    s = await _find_session(sid)
    if not s:
        return {"ok": False, "error": "会话不存在或已结束"}
    cwd = s.get("cwd") or ""
    try:
        ad = agents.get(s.get("agent")) or agents.adapter_of(sid)
        p = ad.transcript_path(cwd, sid) if ad else ""
    except Exception:
        p = ""
    msgs = await asyncio.get_event_loop().run_in_executor(None, _tr_load, p) if p else []
    asst = [str(m.get("text") or "").strip() for m in msgs
            if m.get("role") == "assistant" and str(m.get("text") or "").strip()][-3:]
    if not asst:
        return {"ok": False, "error": "这一轮还没有可验收的记录"}
    rounds = "\n\n".join("【第 %d 轮】%s" % (i + 1, t[-1500:]) for i, t in enumerate(asst))
    cards = _test_cards_payload(sid, 3)
    if cards:
        rounds += "\n\n【最近验证】" + "；".join(
            "%s → %s" % (c["cmd"], "通过" if c["state"] == "pass"
                         else ("失败" if c["state"] == "fail" else c["state"]))
            for c in cards)
    fallback = "\n".join(asst)[-600:]
    r = await notify.accept_summary(sid, rounds, fallback)
    return {"ok": True, "text": r["text"], "src": r["src"]}


async def h_accept_summary(request):
    await require_token(request)
    return _jsonz(await _accept_summary_payload(request.query.get("sid") or ""))


# ── 命令白名单（2026-11-19 计划 A2）────────────────────────────────────
# App 只传 kind，跑什么由 products/<线>/product.json 的 cmds 定——这个口子塞不进任意命令。
_CMDS_KINDS = ("lint", "test", "build")
_DEPLOY_KINDS = ("deploy", "rollback")


def _proj_cmds(cwd):
    """会话项目目录里能一键跑什么 {kind: cmd}，kind 见 _CMDS_KINDS/_DEPLOY_KINDS。
    package.json scripts 优先；Python 项目看 pyproject / Makefile；都没有 = {}。
    只认标准脚本名，不解析脚本内容——命令仍要过 _test_cmd_allowed 白名单。"""
    out = {}
    root = str(cwd or "")
    if not root:
        return out

    def _read(name):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    try:
        scripts = (json.loads(_read("package.json") or "{}").get("scripts") or {})
    except Exception:
        scripts = {}
    if isinstance(scripts, dict):
        for k in _CMDS_KINDS:
            if isinstance(scripts.get(k), str) and scripts[k].strip():
                out[k] = "npm run " + k
        if isinstance(scripts.get("deploy"), str) and scripts["deploy"].strip():
            out["deploy"] = "npm run deploy"
        if isinstance(scripts.get("deploy:rollback"), str) and scripts["deploy:rollback"].strip():
            out["rollback"] = "npm run deploy:rollback"

    # host 进程的 PATH 里通常没有 pytest/ruff（只在项目 .venv 里）：
    # 项目带 .venv 就跑它自己的，没带才退回 python3 -m（PATH 上有什么算什么）。
    def _tool(name):
        exe = os.path.join(root, ".venv", "bin", name)
        return (".venv/bin/" + name) if os.access(exe, os.X_OK) else None

    if "test" not in out and _tool("pytest"):
        out["test"] = _tool("pytest") + " -q"
    if "lint" not in out and _tool("ruff"):
        out["lint"] = _tool("ruff") + " check ."
    py = _read("pyproject.toml")
    if py:
        if "test" not in out and "[tool.pytest" in py:
            out["test"] = "python3 -m pytest -q"
        if "lint" not in out and "[tool.ruff" in py:
            out["lint"] = "ruff check ."

    mk = _read("Makefile")
    if mk:
        for k in _CMDS_KINDS:
            if k not in out and re.search(r"^%s\s*:" % k, mk, re.M):
                out[k] = "make " + k
    return out


async def _cmd_run_payload(sid, kind, product=None):
    kind = str(kind or "")
    if kind not in _CMDS_KINDS:
        return {"ok": False, "error": "只认 kind=lint|test|build"}
    s = await _find_session(sid)
    if not s:
        return {"ok": False, "error": "会话不存在或已结束"}
    cwd = s.get("cwd") or str(Path.home())
    cmd = _proj_cmds(cwd).get(kind)
    if not cmd:
        return {"ok": False,
                "error": "这个项目没配 %s 命令（package.json scripts / pyproject / Makefile）" % kind}
    if not _test_cmd_allowed(cmd):
        return {"ok": False, "error": "项目命令不在白名单：%r" % cmd[:80]}
    t0 = time.time()
    rc, out = await asyncio.get_event_loop().run_in_executor(None, _test_exec, cmd, cwd, 600)
    dur = time.time() - t0
    state = "pass" if rc == 0 else "fail"
    tail = out[-_TEST_TAIL_MAX:]
    con = _db()
    con.execute("INSERT INTO test_runs(sid,cmd,state,rc,ts,dur,tail) VALUES(?,?,?,?,?,?,?)",
                (sid, cmd, state, rc, t0, dur, tail))
    con.commit()
    con.close()
    return {"ok": True, "item": {"cmd": cmd, "state": state, "rc": rc, "ts": t0,
                                 "dur": dur, "tail": tail}}


async def h_cmd_run(request):
    """POST {sid, kind}：后台跑配置里的 lint/test/build，页面轮询 /api/test_cards。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"})
    sid = str(data.get("sid") or "")
    if str(data.get("kind") or "") not in _CMDS_KINDS:
        return web.json_response({"ok": False, "error": "只认 kind=lint|test|build"})
    if sid in _TEST_RUNNING:
        return web.json_response({"ok": False, "error": "这个会话已有命令在跑"})
    if not await _find_session(sid):
        return web.json_response({"ok": False, "error": "会话不存在或已结束"})
    product = _client_product_for(request)
    _TEST_RUNNING.add(sid)

    async def job():
        try:
            await _cmd_run_payload(sid, data.get("kind"), product)
        finally:
            _TEST_RUNNING.discard(sid)

    asyncio.get_event_loop().create_task(job())
    return _jsonz({"ok": True, "running": True})


async def h_cmds(request):
    """这个会话的项目能跑什么（前端据此显隐「测试/发布」入口）。"""
    await require_token(request)
    sid = request.query.get("sid") or ""
    s = await _find_session(sid)
    if not s:
        return _jsonz({"ok": False, "error": "会话不存在或已结束"})
    cwd = s.get("cwd") or str(Path.home())
    cmds = _proj_cmds(cwd)
    cfg = _deploy_cfg(_client_product_for(request))
    return _jsonz({"ok": True,
                   "cmds": {k: cmds[k] for k in _CMDS_KINDS if k in cmds},
                   "deploy": {"run": bool(cmds.get("deploy") or cfg.get("cmd")),
                              "rollback": bool(cmds.get("rollback") or cfg.get("rollback")),
                              "health_url": cfg.get("health_url") or ""}})


_CMD_RUNS: list = []   # [{sid,csid,cmd,ts}] 最近运行（新在后）；内存态，host 重启即清


def _cmd_runs():
    return sorted(_CMD_RUNS, key=lambda r: -r["ts"])


async def h_ext_cmdruns(request):
    await require_token(request)
    return _jsonz({"ok": True, "runs": _cmd_runs()})


async def h_ext_cmdout(request):
    """读该会话最近一次运行的 PTY 屏幕文本（pyte 权威屏，锁屏/关窗都照常更新）"""
    await require_token(request)
    sid = request.match_info["sid"]
    r = next((x for x in reversed(_CMD_RUNS) if x["sid"] == sid), None)
    if not r:
        return web.json_response({"ok": False, "error": "没有运行过的命令"})
    sess = SESSIONS.get(r["csid"])
    if not sess:
        return web.json_response({"ok": False, "cmd": r["cmd"], "error": "运行终端已清理"})
    snap = sess.snapshot()
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", snap).replace("\r\n", "\n").replace("\r", "\n")
    if not sess.alive and sess.exit_code is not None:
        text = text.rstrip("\n") + "\n（已退出，code %d）" % sess.exit_code
    return _jsonz({"ok": True, "out": text.rstrip("\n"), "cmd": r["cmd"], "ts": r["ts"],
                   "alive": bool(sess.alive)})


async def h_ext_screen(request):
    await require_token(request)
    sid = request.match_info["sid"]
    s = await _find_session(sid)
    if not s:
        return web.json_response({"ok": False, "screen": "会话不存在或已结束"})
    ok, text = await asyncio.get_event_loop().run_in_executor(None, ext_screen, s)
    if not ok and sid not in _LIVE_NOMETA_LOGGED:
        # 没有窗口也没有全彩镜像 = 手机端「打不开」。这条路以前不打日志，客户报「无窗口」
        # 时开发机日志里查不到任何痕迹；每个 sid 只打一次（轮询会一直调）。
        if len(_LIVE_NOMETA_LOGGED) > 200:
            _LIVE_NOMETA_LOGGED.clear()
        _LIVE_NOMETA_LOGGED.add(sid)
        print("[live] sid=%s 读不到屏幕（无窗口/无全彩镜像，手机端打不开） | %s"
              % (sid[:8], _sess_diag(s)), flush=True)
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return _jsonz({"ok": ok, "screen": "\n".join(lines).replace("⏺", "●").replace("⏵", "▸"),
                              "state": s["state"]})


CELLS_HIST_MAX = 200   # 电脑端窗口最多往回看多少行（pyte 历史 3000 行，Tk 画不动那么多）


def _cells_payload(s):
    """电脑端会话窗口（ptywin.py）要的那份：当前屏 + 最近回滚，带颜色游程。
    和 ext_screen 同源（Windows 托管会话的 pyte 屏 / 全彩镜像），只是不丢样式。"""
    hs = _win_host_session(s)
    if hs is not None:
        scr, cols, rows = hs.screen, hs.screen.columns, hs.screen.lines
        hist = list(hs.screen.history.top)[-CELLS_HIST_MAX:]
    else:
        meta = _meta_for_pid(s.get("pid"))
        if not meta:
            return None
        with _MIRROR_LOCK:
            st = _mirror_feed_inner(s.get("sid") or "", meta)
        if not st:
            return None
        scr, cols, rows = st["scr"], st["win"][0], st["win"][1]
        hist = (st.get("hist") or [])[-CELLS_HIST_MAX:]
    cells = [_row_cells(r, cols) for r in hist]
    cells += [_row_cells(scr.buffer[y], cols) for y in range(rows)]
    return {"ok": 1, "cols": cols, "rows": rows, "nhist": len(hist),
            "cursor": [scr.cursor.y, scr.cursor.x], "cells": cells}


async def h_ext_cells(request):
    await require_token(request)
    s = await _find_session(request.match_info["sid"])
    if not s:
        return web.json_response({"ok": False, "error": "会话不存在或已结束"}, status=404)
    got = await asyncio.get_event_loop().run_in_executor(None, _cells_payload, s)
    if got is None:
        return web.json_response({"ok": False, "error": "该会话未接入全彩镜像"}, status=404)
    return _jsonz(got)


# ---- 实时推送（SSE）：手机端轮询的增量替代 ----
# 客户端用 XHR 流式读（老 WebView 唯一实证通道），按 \n\n 自分帧；心跳 ": hb" 15s；
# 连上即全量 bootstrap（重连=catch-up，无内容洞）；断流客户端自动回轮询。
# 经中继时由 _relay_serve_stream 按 chunk 帧转发（req 帧 st:1 + X-CB-Stream-Id）。

PUSH_TICK = 0.35
PUSH_HB = 15.0
PUSH_MAX_LIFE = 55 * 60          # 长连主动退役（bye→客户端立即重连），防僵死
PUSH_MAX_CONN = 16   # 多客户端（手机×N + 桌面浏览器标签页）：8 太紧，满员即 503 → 客户端「推送中断已回轮询」
PUSH_LIVE_GAP = 0.6              # live 事件限频（屏重排渲染不便宜）
PUSH_HIST_GAP = 2.0              # hist 是全量重排（最多 800 行），变化合并限频
PUSH_SESS_GAP = 2.0
PUSH_MSG_GAP = 0.8               # transcript 防抖：流式写盘期间合并
PUSH_SCR_GAP = 1.2
_push_conns = [0]                # /api/ext_push 常连数（页面/桌面客户端）
_bg_conns = [0]                  # /api/bg_push 常连数（安卓后台提醒服务）
_PUSH_STREAMS = {}               # rid(str) → asyncio.Event（relay cancel 的路由键）

# ---- 事件层：Claude Code hooks → SSE notify ----
# hook.sh（--hooks install 生成）把 hook 事件 POST 到 /api/session_event；
# 这里归并成「该看手机了」的通知，走同一条 SSE 推给页面（toast/角标/提示音）
# 和安卓壳（系统通知）。只做提醒，绝不回决策。
_EV = {}            # sid → {"prompt": ts, "stop": ts, "perm": ts}
_NOTIFY_TTL = 600


def _sid_title(sid):
    for x in _cached_sessions():
        if x["sid"] == sid:
            return x.get("title") or x.get("short") or sid[:8]
    # 不在当前会话列表里（已经退出的老会话、或者 Codex 这种只在跑完时上报一次的）：
    # 从转录里取一眼标题，别让提醒只显示一串 id
    try:
        ad = agents.get(agents.agent_of(sid))
        p = ad.transcript_path("", sid) if ad else None
        if p:
            _cwd, t = ad.transcript_meta(p)
            if t:
                return t
    except Exception:
        pass
    return sid[:8]


# 阻塞类提醒：人不动它就不消失，所以要持续提醒（间隔递增，别太吵）。
# 用户回一句话（UserPromptSubmit）或在手机上打开会话（/api/seen）就停。
_BLOCKING = ("perm", "ask", "input", "limit")
_BLOCK_STEPS = (180, 600, 1200, 1800)     # 首次 3 分钟，之后 10/20/30 分钟，封顶 30 分钟
_BLOCK = {}                               # sid -> {kind,text,title,cwd,sound,src,n,next}
_DONE_SENT = {}                           # sid -> ts（Stop 与 agent_completed 去重）


def _block_set(sid, it, title, cwd, src):
    _BLOCK[sid] = {"kind": it.get("kind"), "text": it.get("text") or "",
                   "title": it.get("title") or title, "cwd": cwd or "",
                   "sound": it.get("sound"), "src": src,
                   "kind_rule": it.get("kind_rule") or "",
                   "sound_rule": it.get("sound_rule") or "",
                   "rid": it.get("request_id") or "",
                   "ask": it.get("ask"), "risk": it.get("risk"),
                   "why": it.get("why") or "",
                   "n": 0, "next": time.time() + _BLOCK_STEPS[0]}


# ---- 提醒历史（持久化到 codeboss.db）：智能识别的进度/概括 ----
# 以前只存在内存 _NOTIFY_Q（50 条、重启即失），用户看不到「这轮到底走到哪、说过什么」。
# 现在每条提醒落一行；重复提醒（rep>0）只给原记录加次数，不刷屏。留 30 天 / 最多 3000 条。

# 提醒「怎么判的」留痕：这一轮是规则说的还是模型说的、待确认问句有没有被丢掉。
# 只在内存里活一轮（silent、不加锁），随本轮提醒落进 notify_hist.trace，
# 手机「提醒历史 → 提醒健康」据此算账；model 挂掉/超时也要能看出是「没判」而不是「判了 NO」。
_TRACE = {}


def _tr_set(sid, **kv):
    """记一笔本轮判定来源。sid 空（老事件）就不记。"""
    if not sid:
        return
    _TRACE.setdefault(str(sid), {}).update({k: v for k, v in kv.items() if v is not None})
    if len(_TRACE) > 200:                 # 只服务「这一轮」，别当缓存养大
        for k in list(_TRACE)[:-50]:
            _TRACE.pop(k, None)


def _trace_load(s):
    try:
        d = json.loads(s) if s else None
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}












# ---- 语音概括：「这一轮在做什么」 -------------------------------------------
# 语音只念状态词（「完成了」）时，人得先想起是哪个会话；补半句概括就够。
# 概括在开发机算：用户这一轮的原话 → 中继 /api/llm（qwen-flash）压成一句短话。
# key 只在中继；这里跟命令识别一样走 /api/llm。算不出来/没网/额度用完 → 原话截取。
# 开不开、用不用模型：手机设置页「语音播报」里可以配（存开发机 prefs.json）。
_PREF_FILE = HERE / "prefs.json"


def _pref_load():
    try:
        d = json.loads(_PREF_FILE.read_text(encoding="utf-8"))
        ab = (d or {}).get("about") if isinstance(d, dict) else None
        if isinstance(ab, dict):
            _PREF["about"].update({k: ab[k] for k in ("on", "llm", "max") if k in ab})
        rp = (d or {}).get("reply") if isinstance(d, dict) else None
        if isinstance(rp, dict):
            _PREF["reply"].update({k: rp[k] for k in ("on", "max") if k in rp})
        sy = (d or {}).get("say") if isinstance(d, dict) else None
        if isinstance(sy, dict) and sy.get("style"):
            _PREF.setdefault("say", {})["style"] = str(sy["style"])[:16]
        for key in ("smart", "na"):
            v = (d or {}).get(key) if isinstance(d, dict) else None
            if isinstance(v, dict) and "on" in v:
                _PREF.setdefault(key, {})["on"] = v["on"] is not False
        sv = (d or {}).get("smart") if isinstance(d, dict) else None
        if isinstance(sv, dict) and isinstance(sv.get("devs"), dict):
            devs = {}
            for k, x in list(sv["devs"].items())[:64]:
                if isinstance(x, dict):
                    try:
                        ts = float(x.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    devs[str(k)[:40]] = {"on": x.get("on") is not False, "ts": ts}
            _PREF.setdefault("smart", {})["devs"] = devs
        vv = (d or {}).get("voice") if isinstance(d, dict) else None
        if isinstance(vv, dict) and isinstance(vv.get("devs"), dict):
            devs = {}
            for k, x in list(vv["devs"].items())[:64]:
                if isinstance(x, dict):
                    try:
                        ts = float(x.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    devs[str(k)[:40]] = {"on": x.get("on") is not False, "ts": ts}
            _PREF.setdefault("voice", {})["devs"] = devs
        cv = (d or {}).get("coding") if isinstance(d, dict) else None
        if isinstance(cv, dict):
            if "on" in cv:
                _PREF.setdefault("coding", {})["on"] = cv["on"] is not False
            if isinstance(cv.get("devs"), dict):
                devs = {}
                for k, x in list(cv["devs"].items())[:64]:
                    if isinstance(x, dict):
                        try:
                            ts = float(x.get("ts") or 0)
                        except (TypeError, ValueError):
                            ts = 0.0
                        devs[str(k)[:40]] = {"on": x.get("on") is not False, "ts": ts}
                _PREF.setdefault("coding", {})["devs"] = devs
    except Exception:
        pass


def _pref_save():
    try:
        _PREF_FILE.write_text(json.dumps(_PREF, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
    except OSError:
        pass


_pref_load()


def _about_pref():
    ab = _PREF.get("about") or {}
    try:
        mx = int(ab.get("max") or 12)
    except (TypeError, ValueError):
        mx = 12
    return {"on": ab.get("on", True) is not False,
            "llm": ab.get("llm", True) is not False,
            "max": max(6, min(20, mx))}




_SMART_DEV_TTL = 30 * 86400      # 一台手机 30 天没上报就当不在了，别让它把智能提醒一直钉在开


def _smart_devs():
    s = _PREF.setdefault("smart", {})
    devs = s.get("devs")
    if not isinstance(devs, dict):
        devs = s["devs"] = {}
    now = time.time()
    for k in [k for k, v in devs.items()
              if not isinstance(v, dict) or now - float(v.get("ts") or 0) > _SMART_DEV_TTL]:
        devs.pop(k, None)
    return devs


def _smart_pref():
    """智能提醒要不要生成：**任何一台设备**开着就开——手机各管各的开关，
    开发机不能因为某台关了就把别的手机也一起掐掉。没有任何设备上报时退回 on。"""
    devs = _smart_devs()
    if devs:
        return any(v.get("on") is not False for v in devs.values())
    return (_PREF.get("smart") or {}).get("on", True) is not False


def _smart_dev_set(dev, on):
    """记某台手机的智能提醒开关。不带 dev 的老请求只改 on（兼容老页面）。"""
    s = _PREF.setdefault("smart", {})
    if not dev:
        s["on"] = bool(on)
        return
    devs = _smart_devs()
    if dev not in devs and len(devs) >= 32:      # 只留最近上报的 32 台
        for k in sorted(devs, key=lambda k: devs[k].get("ts") or 0)[:len(devs) - 31]:
            devs.pop(k, None)
    devs[dev] = {"on": bool(on), "ts": time.time()}
    s["on"] = _smart_pref()                      # 老页面读到的也是聚合后的真值




def _voice_devs():
    """每台手机的「语音播报」开关（页面每次打开/加解锁切换都会上报）。"""
    s = _PREF.setdefault("voice", {})
    devs = s.get("devs")
    if not isinstance(devs, dict):
        devs = s["devs"] = {}
    now = time.time()
    for k in [k for k, v in devs.items()
              if not isinstance(v, dict) or now - float(v.get("ts") or 0) > _SMART_DEV_TTL]:
        devs.pop(k, None)
    return devs


def _voice_pref():
    """要不要合成语音：**任何一台手边设备**要听就合成（每台手机按自己的开关决定放不放）。
    没有任何设备上报时退回「要」，兼容还没升级的老页面。"""
    devs = _voice_devs()
    if devs:
        return any(v.get("on") is not False for v in devs.values())
    return True


def _voice_dev_set(dev, on):
    """记某台手机的语音播报开关（on 是**实际生效值**：智能提醒关掉时也算关）。"""
    if not dev:
        return
    devs = _voice_devs()
    if dev not in devs and len(devs) >= 32:      # 只留最近上报的 32 台
        for k in sorted(devs, key=lambda k: devs[k].get("ts") or 0)[:len(devs) - 31]:
            devs.pop(k, None)
    devs[dev] = {"on": bool(on), "ts": time.time()}


def _coding_devs():
    """每台手机的「编码模式」开关（编码产品的 pv 预览/产出物编辑入口跟着它显隐）。"""
    s = _PREF.setdefault("coding", {})
    devs = s.get("devs")
    if not isinstance(devs, dict):
        devs = s["devs"] = {}
    return devs


def _coding_dev_set(dev, on):
    """记某台手机的编码模式开关。不带 dev 的老请求改共享值（兼容无 devId 的浏览器）。"""
    s = _PREF.setdefault("coding", {})
    if not dev:
        s["on"] = bool(on)
        return
    devs = _coding_devs()
    if dev not in devs and len(devs) >= 32:      # 只留最近上报的 32 台
        for k in sorted(devs, key=lambda k: devs[k].get("ts") or 0)[:len(devs) - 31]:
            devs.pop(k, None)
    devs[dev] = {"on": bool(on), "ts": time.time()}


def _coding_on(dev, product):
    """编码模式生效值：这台手机的显式开关 ?? 产品线默认（codeboss 开 / agentboss 关）。
    没上报过 dev 的老壳/浏览器按线走；显式关过的不会因为升级被悄悄打开。"""
    d = _coding_devs().get(dev) if dev else None
    if d is not None:
        return bool(d.get("on"))
    shared = _PREF.setdefault("coding", {}).get("on")
    if shared is not None:
        return bool(shared)
    return product == "codeboss"


# ── 「有没有人在收」：智能提醒的第二道闸（省钱）──────────────────────
# 模型概括（中继 LLM 配额）和语音合成（DASHSCOPE）都按次花钱。手机没连、页面没开、
# 桌面客户端也没开的时候，生成了也送不到人手上，纯烧钱（用户 2026-11-08 提出的漏洞：
# 「那台开着智能提醒的手机不用了，开发机还在调模型」）。
# 于是要求「至少有一个客户端正连着」：
#   · /api/ext_push、/api/bg_push 的常连（页面 / 安卓后台提醒服务）
#   · 中继推过来的在线客户端数（远程手机、浏览器都算）
# 断线重连有一两秒空档，留 2 分钟宽限，别让提醒正好卡在缝里丢掉概括。
# 注意：闸门只影响「增值那部分」，基础提醒（规则文字/角标/系统通知）不受影响。
_LISTEN_AT = [0.0]
_LISTEN_GRACE = 120.0
_NO_LISTEN_LOG = [0.0]
_NO_VOICE_LOG = [0.0]


def _listen_n():
    """当前正在收的客户端数（本机常连 + 中继 presence）。"""
    try:
        relay_n = int(_HOST_ST.get("apps") or 0) + int(_HOST_ST.get("webs") or 0)
    except Exception:
        relay_n = 0
    return _push_conns[0] + _bg_conns[0] + relay_n


def _listen_now():
    if _listen_n() > 0:
        _LISTEN_AT[0] = time.time()
        return True
    return time.time() - _LISTEN_AT[0] < _LISTEN_GRACE


def _smart_ok():
    """智能提醒是否可用：套餐给（features.remind）、本机开关没关、且有人在收。
    这是所有模型概括 / 语音合成调用点的唯一总闸；免费档 / 没人连着 -> False。"""
    if not (bool(_feat_on("remind")) and _smart_pref()):
        return False
    if not _listen_now():
        if time.time() - _NO_LISTEN_LOG[0] > 300:
            _NO_LISTEN_LOG[0] = time.time()
            print("[smart] 没有客户端在收，跳过模型概括/语音合成", flush=True)
        return False
    return True


def _voice_ok():
    """「语音那一路」要不要跑：TTS 合成 + 「在做什么」+「AI 回复概括」+「是否在等你」。
    在 _smart_ok 之上再按每台手机的「语音播报」开关收一道：手边没有一台设备要听，
    合成了也没人放，模型概括更是纯花钱（用户 2026-11-08：「语音播报关掉后，所有跟
    语音播报相关的判断、总结都不需要执行」）。「在等你」的开关本身也是语音区的一环。"""
    if not _smart_ok():
        return False
    if not _voice_pref():
        if time.time() - _NO_VOICE_LOG[0] > 300:
            _NO_VOICE_LOG[0] = time.time()
            print("[voice] 设备都关了语音播报，跳过合成/概括", flush=True)
        return False
    return True


_LLM_ABOUT_PROMPT = (
    "你是手机语音提醒的播报助手。根据【会话标题】和【用户这一轮说的话】，"
    "用不超过 14 个汉字概括这一轮在做什么、解决什么问题，必须是完整的短语（不能说到一半）。"
    "只输出这半句概括本身：不要引号、不要标点、不要「关于/这一轮」之类的开头、不要解释。"
    "用户的话没有信息量（比如只是「继续」「好」「嗯」）时，就用会话标题概括。"
)


def _about_clean(s, maxn=12):
    """模型偶尔不听话：带引号、带句号、前面加「关于」。都削掉，只要那半句。"""
    t = re.sub(r"\s+", "", str(s or ""))
    t = re.split(r"[，,。！？!?；;：:\n]", t)[0]
    t = t.strip("「」『』\"'“”‘’【】[]()（）:：,，。.！!？?;；、…-— ")
    t = re.sub(r"^(?:关于|这一轮|本轮|概括)[:：]?", "", t)
    return _say_clip(t, maxn)


_ABOUT_OPEN = re.compile(
    r"^(?:嗯+|哦+|啊+|那个+|你好+|好的?|再|先|然后|现在|帮我|帮忙|请|麻烦|"
    r"能不能|可不可以|可以)[\s，,。.、：:]+")
_ABOUT_NOINFO = ("继续", "好", "嗯", "可以", "开始", "接着", "然后", "来吧", "干吧")


def _about_rule(text, title, maxn=12):
    """兜底：不调模型，直接从这一轮的原话里截一句开头。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.split(r"[。！？!?；;\n]", t)[0].strip()
    for _ in range(3):                      # 「嗯，那个…」这种叠了好几个开头
        nt = _ABOUT_OPEN.sub("", t)
        if nt == t:
            break
        t = nt
    t = t.strip("，,、:： 「」\"'“”")
    if t in _ABOUT_NOINFO or len(t) < 3:    # 只说了「继续」→ 用会话标题
        t = ""
    t = _say_clip(t, maxn)
    return t or re.sub(r"\s+", "", str(title or ""))[:maxn]


def _cause_clean(t, maxn=60):
    """用户当轮的原话 → 一行「前因」。刻意**不调模型**：
    提醒历史要的是指令本身（针对什么问题/让干什么），模型压成 12 字的 about
    只适合念出来，放进时间线会把细节压没。"""
    t = re.split(r"[\r\n]", str(t or "").strip())[0]   # 只取第一行（原话常带换行贴日志）
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:/[A-Za-z][\w-]*\s*)+", "", t)     # 去掉开头的 /compact 之类
    t = re.split(r"[。！？!?]", t)[0].strip(" ，,、:：;；「」\"'“”")
    if t in _ABOUT_NOINFO:                             # 「继续」「好」这种：没有前因可言
        return ""
    if len(t) <= maxn:
        return t
    head = t[:maxn + 10]                               # 超长：优先在有停顿的地方收
    cut = -1
    for ch in "，,、；;：:":
        i = head.rfind(ch)
        if i > cut:
            cut = i
    return (head[:cut] if cut >= maxn // 2 else t[:maxn]).strip(" ，,、:：;；")


def _cause_for(sid):
    """这一轮的前因：用户在 UserPromptSubmit 里说的话（_ev_local 已存 ptext）。
    没有就空——卡住/重复提醒这类不是用户发起的，本来就没有起因。"""
    st = _EV.get(sid) or {}
    return _cause_clean(st.get("ptext") or "")


# 只是应答/催一句，没有任何实质内容：拿它当「起因」只会误导（「起因：好的」）。
_CAUSE_WEAK = re.compile(
    r"^(?:好的?|好滴|好吧|嗯+|行|行吧|可以|继续|收到|明白|了解|没问题|是的?|对|ok|okay|go|"
    r"去吧|搞吧|来吧|上吧|冲|嗯嗯)[。.!！~～]*$", re.I)


def _cause_weak(s):
    t = re.sub(r"[\s。，,.!！?？~～]+", "", str(s or ""))
    return len(t) <= 8 and bool(_CAUSE_WEAK.match(t))


_MAIN_LOOP = None         # 主事件循环（on_startup 记下）：工作线程里排协程用
_ABOUT = {}              # (sid, 原话前 300 字) -> (概括, 时间)
_ABOUT_BUSY = set()
_ABOUT_TTL = 1800
_ABOUT_KINDS = ("done", "perm", "ask")   # 只有这三种带概括；额度/卡住维持原话术


def _about_key(sid, pt):
    return (sid, str(pt or "")[:300])


def _about_for(sid):
    """通知里带的那半句概括：有算好的用算好的，没有就按原话截（关掉则空）。"""
    p = _about_pref()
    if not p["on"]:
        return ""
    st = _EV.get(sid) or {}
    pt = st.get("ptext") or ""
    hit = _ABOUT.get(_about_key(sid, pt))
    if hit and time.time() - hit[1] < _ABOUT_TTL:
        return hit[0]
    return _about_rule(pt, _sid_title(sid), p["max"])


def _about_kick(sid, pt, title):
    """用户一发话就把概括算起来：等 Claude 跑完（至少几秒）时早就好了，
    通知路径上零等待。算不出来也没关系，_about_for 会回落截取。"""
    if not _voice_ok():          # 免费档/智能提醒关掉/没人要听：不花模型调用
        return
    p = _about_pref()
    if not (p["on"] and p["llm"]):
        return
    if not str(pt or "").strip():
        return
    k = _about_key(sid, pt)
    hit = _ABOUT.get(k)
    if hit and time.time() - hit[1] < _ABOUT_TTL:
        return
    if k in _ABOUT_BUSY:
        return
    try:
        loop = _MAIN_LOOP or asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(_about_llm(k, pt, title, p["max"]), loop)
    except RuntimeError:
        pass


async def _about_llm(key, pt, title, maxn):
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return
    _ABOUT_BUSY.add(key)
    try:
        import aiohttp
        body = {"prompt": _LLM_ABOUT_PROMPT, "purpose": "remind_about",
                "text": "【会话标题】%s\n【用户这一轮说的话】%s" % (title or "", str(pt)[:400])}
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm", json=body,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=12)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict) and d.get("ok"):
            t = _about_clean(d.get("text"), maxn)
            if t:
                _ABOUT[key] = (t, time.time())
                if len(_ABOUT) > 400:
                    for q in sorted(_ABOUT, key=lambda q: _ABOUT[q][1])[:-200]:
                        _ABOUT.pop(q, None)
    except Exception:
        pass
    finally:
        _ABOUT_BUSY.discard(key)


# ---- 语音概括：「AI 这轮回复了什么」 -----------------------------------------
# about 说的是「用户让干什么」（这一轮的主题），reply 说的是「AI 干成什么样、
# 还要你确认什么」。回复往往很长（markdown/代码/列表），直接念没法听；用小模型
# 压成一句口语：发生了什么 + 需要你确认/选择/动手的事。
# 与 about 的区别：about 在用户发话时就算好了，reply 只有等这轮结束才知道内容，
# 所以在 Stop 事件里和「是不是在等你」那个判断并行跑，通知发出前等最多 2.5 秒。


# 已发布 / 已经完成 / 已成功推送 / 已将查看器发布为网页 / 已把改动推到远端：
# 中间只允许「将/把 + 短宾语」这种说法，不让「已确认无误，可以发布」这种（有逗号挡住）误伤。










# ---- 没有朗读引擎的手机：开发机把整句念出来 ---------------------------------
# 「X，完成了」是动态句子，页面/壳只能靠系统 TTS 念；多亲这类机器一个 TTS 引擎
# 都没装，只能退回预录音（只有「完成了」三个字）。开发机本来是 Mac，用系统嗓音
# 合成一小段音频放在这里，手机只负责放——不用引擎，也不花云端 TTS 的钱。
# 播报风格：同一件事，不同的说法 + 不同的嗓门。风格只换结尾那半句和 TTS 音色，
# 开头「这轮在做什么」（about）不变——那是按会话内容算的，和语气无关。
# 音色用中继的 qwen3-tts（DashScope）音色：Cherry 亲切女声 / Ethan 沉稳男声 /
# Sunny 四川话晴儿 / Chelsie 二次元甜妹。中继不支持这个音色时会自动退回 Cherry。








# 冷却只是「别让每条提醒都去等一次超时」，不是降级开关：这段时间里提醒**不念**，
# 不会换成别的嗓音。恢复得快一点，免得中继偶尔一抖就哑几分钟。














async def h_say(request):
    """取一段合成好的语音（手机上没 TTS 引擎时用）。"""
    await require_token(request)
    if not _smart_ok():          # 降级/关掉后不再吐缓存音频
        raise web.HTTPNotFound()
    i = request.match_info.get("name", "")
    if not re.fullmatch(r"[0-9a-f]{24}", i):
        raise web.HTTPNotFound()
    m4a, wav = _say_files(i)
    p = m4a if m4a.exists() else (wav if wav.exists() else None)
    if p is None:
        ent = _SAY_TEXTS.get(i)
        if ent and time.time() - ent[2] < _SAY_TTL:
            loop = asyncio.get_event_loop()
            # 通知发出时后台已经在合成：先等它写完（最多 6 秒），别两个 say 同时写一个文件
            for _ in range(24):
                if m4a.exists() or wav.exists():
                    break
                await asyncio.sleep(0.25)
            p = m4a if m4a.exists() else (wav if wav.exists() else None)
            if p is None:
                got = await loop.run_in_executor(None, _say_gen, ent[0], str(m4a), ent[1])
                if got:
                    p = Path(got)
    if p is None:
        raise web.HTTPNotFound()
    # 壳/页面来取播报音频 = 说明它确实收到了一条带语音的提醒（排查「提醒全哑」时
    # 这条日志和 [bg] 连上/断开对着看，能分清「没收到」和「收到了没放出音」）。
    print("[say] 取播报音频 ip=%s name=%s" % (request.remote or "?", i), flush=True)
    ct = "audio/mp4" if p.suffix == ".m4a" else "audio/wav"
    return web.FileResponse(p, headers={"Content-Type": ct,
                                        "Cache-Control": "max-age=%d" % _SAY_TTL})


def _feedback_post(raw, ct):
    """把页面提交的 multipart 原样转给中继（不落本机、不碰内容）。"""
    import urllib.request
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id") or "", rc.get("device_secret") or ""
    if not (base and dev and sec):
        return {"ok": False, "error": "本机没连上中继，反馈暂时发不出去"}
    req = urllib.request.Request(
        base + "/r/feedback", data=raw, method="POST",
        headers={"Content-Type": ct, "X-CB-Dev": dev, "X-CB-Sec": sec})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        try:
            import urllib.error
            if isinstance(e, urllib.error.HTTPError):
                return json.loads(e.read().decode("utf-8"))
        except Exception:
            pass
        return {"ok": False, "error": "提交失败：%s" % str(e)[:100]}


async def h_feedback(request):
    """手机设置页「问题与建议」：文字 + 截图/视频，原样转中继存档。"""
    await require_token(request)
    raw = await request.read()
    if not raw:
        return web.json_response({"ok": False, "error": "空内容"}, status=400)
    ct = request.headers.get("Content-Type") or ""
    if not ct.lower().startswith("multipart/"):
        return web.json_response({"ok": False, "error": "格式不对"}, status=400)
    loop = asyncio.get_event_loop()
    d = await loop.run_in_executor(None, _feedback_post, raw, ct)
    return web.json_response(d)


# ── 「问题建议」工单：手机直连中继时走 /r/feedback/*，局域网直连时由本机代转 ──
def _fb_relay(path, method="GET", body=None, timeout=30):
    """以本机设备凭证调中继的工单接口。返回 (status, bytes, content_type)。

    归属账号由设备凭证在中继那边认（relay 的 _fb_owner：带 X-CB-Dev/X-CB-Sec 就是
    这台机器的账号），所以手机不用另外带登录 cookie 也能看到自己的工单。"""
    import urllib.request
    import urllib.error
    from urllib.parse import quote
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id") or "", rc.get("device_secret") or ""
    if not (base and dev and sec):
        return (503, json.dumps({"ok": False, "error": "本机没连上中继"}).encode(),
                "application/json")
    data = None
    hdrs = {"X-CB-Dev": dev, "X-CB-Sec": sec}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.status, r.read(), (r.headers.get("Content-Type") or "application/json"))
    except urllib.error.HTTPError as e:
        return (e.code, e.read(), (e.headers.get("Content-Type") or "application/json"))
    except Exception as e:
        return (502, json.dumps({"ok": False, "error": "中继不可达：%s" % str(e)[:80]}).encode(),
                "application/json")


def _ct_clean(ct):
    """aiohttp 不许 content_type 里带 charset 参数，剥掉。"""
    return ((ct or "application/json").split(";")[0].strip() or "application/json")


async def h_feedback_list(request):
    """GET /api/feedback_list —— 我的问题建议列表（局域网直连时代中继取）。"""
    await require_token(request)
    loop = asyncio.get_event_loop()
    st, raw, ct = await loop.run_in_executor(None, _fb_relay, "/r/feedback/list", "GET", None)
    return web.Response(body=raw, status=st, content_type=_ct_clean(ct))


async def h_feedback_detail(request):
    """GET /api/feedback_detail?id=N —— 一条工单的主帖 + 全部回复。"""
    await require_token(request)
    from urllib.parse import quote
    fid = quote(str(request.query.get("id") or "0"))
    loop = asyncio.get_event_loop()
    st, raw, ct = await loop.run_in_executor(
        None, _fb_relay, "/r/feedback/detail?id=" + fid, "GET", None)
    return web.Response(body=raw, status=st, content_type=_ct_clean(ct))


async def h_feedback_reply(request):
    """POST /api/feedback_reply {id,text} —— 在工单里接着回（关了就不收）。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "参数不对"}, status=400)
    loop = asyncio.get_event_loop()
    st, raw, ct = await loop.run_in_executor(None, _fb_relay, "/r/feedback/reply", "POST", d)
    return web.Response(body=raw, status=st, content_type=_ct_clean(ct))


async def h_feedback_media(request):
    """GET /api/feedback_media/{id}/{name} —— 工单里的截图/视频，原样代转。"""
    await require_token(request)
    from urllib.parse import quote
    fid = quote(str(request.match_info.get("id") or "0"))
    nm = quote(str(request.match_info.get("name") or ""))
    loop = asyncio.get_event_loop()
    st, raw, ct = await loop.run_in_executor(
        None, _fb_relay, "/r/feedback/media/%s/%s" % (fid, nm), "GET", None)
    return web.Response(body=raw, status=st, content_type=_ct_clean(ct),
                        headers={"Cache-Control": "private, max-age=3600"})


async def h_notify_hist(request):
    """GET /api/notify_hist —— 提醒历史（智能识别出的进度/概括），手机时间线页用。
    ?sid= 只看某个会话；?days= 天数（1-90，默认 30）；?limit= 条数（默认 500）。"""
    await require_token(request)
    sid = (request.query.get("sid") or "").strip()[:64]
    try:
        days = int(request.query.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    try:
        limit = int(request.query.get("limit") or 500)
    except (TypeError, ValueError):
        limit = 500
    items = _nh_list(sid, days, limit)
    # 工作台的「等你回话/今天干完」就读这里：和会话列表同一个能力面
    return web.json_response({"ok": True, "items": _face_only(
        items, _face_for(request))})


def _nh_answered(sid):
    """用户已经回话了：这个会话里还没处理的提醒不再算「等你」。

    以前只有手机页面自己会 POST /api/notify_act 标掉那一条。从电脑上直接敲、
    或者页面在后台/刚重载没来得及标，那行就一直留在 state=''，重进会话时
    pendLoad 又把它捞回来——浮条从此一直挂着（2026-11-18 用户报）。"""
    if not sid:
        return 0
    try:
        con = _db()
        try:
            cur = con.execute(
                "UPDATE notify_hist SET state='sent'"
                " WHERE sid=? AND COALESCE(state,'')=''", (sid,))
            con.commit()
            return cur.rowcount or 0
        finally:
            con.close()
    except Exception:
        return 0


async def h_notify_act(request):
    """POST /api/notify_act —— 提醒历史里某条的处置状态。
    {n: 行号, act: "sent"}：用户在页面上点了「确认」并把消息发出去 → 标「已确认，等结果」。
    之后同一会话再报「完成」时，_nh_add 会把这些行自动标成 done。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    try:
        n = int(d.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    act = str(d.get("act") or "")[:16]
    if not n or act != "sent":
        return web.json_response({"ok": False, "error": "bad args"}, status=400)
    try:
        con = _db()
        try:
            cur = con.execute(
                "UPDATE notify_hist SET state='sent' WHERE n=? AND COALESCE(state,'')=''",
                (n,))
            con.commit()
            state = "sent" if cur.rowcount else ""
            row = con.execute("SELECT state FROM notify_hist WHERE n=?", (n,)).fetchone()
            if row:
                state = row[0] or state
        finally:
            con.close()
        return web.json_response({"ok": True, "state": state})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:120]}, status=500)


async def h_perm_answer(request):
    """POST /api/perm_answer —— 手机对某条权限请求给出的处理结果。
    {sid, request_id, option_id, label}：同一个决策重复上报只记一次（幂等），
    落 logs/perm_audit.jsonl。P3 起同一函数先唤醒挂起的阻塞钩子。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    r = _perm_answer_core(str(d.get("sid") or "")[:64],
                          str(d.get("request_id") or "")[:40],
                          str(d.get("option_id") or "")[:40],
                          label=str(d.get("label") or ""), via="keys")
    return web.json_response(r, status=(200 if r.get("ok") else 400))


async def h_ext_pref(request):
    """手机设置页读写提醒偏好。
    一个开关分两层：智能提醒总开关跟**每台手机**走（POST 带 dev 记进 smart.devs），
    开发机只在「一台都不剩」时才停掉模型/合成；其它（说明在做什么/概括回复/风格）
    是开发机行为，仍存这台机器上。"""
    await require_token(request)
    dev = ""
    if request.method != "POST":       # GET 带 dev 才能读回每台手机的编码模式
        try:
            dev = str(request.query.get("dev") or "")[:40]
        except AttributeError:
            dev = ""
    if request.method == "POST":
        try:
            d = await request.json()
        except Exception:
            d = {}
        dev = str(d.get("dev") or "")[:40]
        ab = _PREF.setdefault("about", {})
        if "on" in d:
            ab["on"] = bool(d.get("on"))
        if "llm" in d:
            ab["llm"] = bool(d.get("llm"))
        if "max" in d:
            try:
                ab["max"] = max(6, min(20, int(d.get("max"))))
            except (TypeError, ValueError):
                pass
        rp = _PREF.setdefault("reply", {})
        if "reply" in d:
            rp["on"] = bool(d.get("reply"))
        if "reply_max" in d:
            try:
                rp["max"] = max(16, min(80, int(d.get("reply_max"))))
            except (TypeError, ValueError):
                pass
        if "say_style" in d:
            st = str(d.get("say_style") or "")
            if st in _SAY_STYLES:
                _PREF.setdefault("say", {})["style"] = st
        if "smart_on" in d:
            _smart_dev_set(dev, d.get("smart_on"))
        if "voice_on" in d:
            _voice_dev_set(dev, d.get("voice_on"))
        if "na_on" in d:
            _PREF.setdefault("na", {})["on"] = bool(d.get("na_on"))
        if "coding_on" in d:
            _coding_dev_set(dev, d.get("coding_on"))
        _pref_save()
    devs = _smart_devs()
    mine = devs.get(dev) if dev else None
    return web.json_response({"ok": True, "about": _about_pref(), "reply": _reply_pref(),
                              "say": _say_pref(),
                              "smart": {"on": _smart_pref(),
                                        "dev_on": (None if mine is None
                                                   else bool(mine.get("on", True)))},
                              "na": {"on": _na_pref()},
                              "coding": {"on": _coding_on(dev, _client_product_for(request))}})


async def h_say_demo(request):
    """试听当前播报风格（设置页点风格/试听时取）：合成一句「完成了」。"""
    await require_token(request)
    kind = request.query.get("kind") or "done"
    if kind not in _SAY_STYLES[SAY_STYLE_DEFAULT]["tails"]:
        kind = "done"
    say = _say_prepare(kind, "")
    if not say:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True, "say": say, "text": _say_text(kind, "")})


# 策略默认值与中继 relay.py 的 NOTIFY_POLICY_DEFAULT 同构——中继是规则的家，
# 这里只是为了「中继不可达也能照常提醒」保留一份本地兜底（定期从中继拉最新的）。
_NP_DEFAULT = {
    "ver": 0,
    "enabled": True,
    "kinds": {"done": True, "perm": True, "input": True,
              "ask": True, "limit": True, "stuck": True},
    "sound": {"done": "done", "perm": "alert", "input": "ping",
              "ask": "ping", "limit": "alert", "stuck": "ping"},
    "voice": {"done": True, "perm": True, "input": False,
              "ask": True, "limit": True, "stuck": False},
    "perm_throttle": 4,
    "idle_after": 0,
    "stuck_min": 10,             # running 但转录 N 分钟没动静 → 「可能卡住」，0=关
    "max_text": 160,
    "done_text": "{msg}",
    "perm_text": "{tool} 需要确认：{brief}",
    "input_text": "等待你的回复",
    "ask_text": "{msg}",
    "limit_text": "{msg}",
    "stuck_text": "{title} 已 {min} 分钟没有输出，可能卡住了",
}
_NP = {"data": dict(_NP_DEFAULT), "ver": 0, "at": 0.0}
_NP_FILE = HERE / "notify_policy.json"
_NP_PULL_S = 300


def _np_get():
    return _NP["data"]


def _np_norm(data):
    for key in ("kinds", "sound", "voice"):
        base = dict(_NP_DEFAULT[key])
        for k, v in (data.get(key) or {}).items():
            base[k] = bool(v) if key in ("kinds", "voice") else str(v)[:20]
        data[key] = base
    return data


def _np_save(pol):
    data = dict(_NP_DEFAULT)
    if isinstance(pol, dict):
        data.update({k: v for k, v in pol.items() if k in _NP_DEFAULT})
    _np_norm(data)
    _NP["data"] = data
    _NP["ver"] = data.get("ver", 0)
    _NP["at"] = time.time()
    try:
        _NP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _np_load_disk():
    try:
        _np_save(json.loads(_NP_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def _np_pull():
    """同步拉中继策略（executor 里跑）。没登记中继/LAN 部署就保持本地那份。"""
    rc = _relay_cfg()
    if not (rc.get("device_id") and rc.get("url")):
        return False
    r = _relay_dev_call("/r/dev/policy", {})
    if not r.get("ok"):
        return False
    _np_save(r.get("policy") or {})
    return True


async def _np_loop():
    """定期从中继取策略：中继改规则 → 各开发机的本地兜底最多 5 分钟跟上。"""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(3)
    while True:
        try:
            await loop.run_in_executor(None, _np_pull)
        except Exception:
            pass
        await asyncio.sleep(_NP_PULL_S)


def _np_fmt(tpl, **kw):
    out = str(tpl or "")
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v or ""))
    return out


def _ask_q(ti):
    """AskUserQuestion 的问句（第一个问题）。「回答一个问题」等于没说，问句才是要判断的东西。"""
    if not isinstance(ti, dict):
        return ""
    qs = ti.get("questions")
    if not isinstance(qs, list) or not qs:
        return ""
    q0 = qs[0] if isinstance(qs[0], dict) else {}
    t = str(q0.get("question") or q0.get("header") or "").strip()
    if t:
        return re.sub(r"\s+", " ", t)
    return ("%d 个问题" % len(qs)) if len(qs) > 1 else ""


def _ask_opts(ti):
    """AskUserQuestion 的可选项标签（第一个问题）：手机上看得到在选什么。"""
    if not isinstance(ti, dict):
        return []
    qs = ti.get("questions")
    if not isinstance(qs, list) or not qs:
        return []
    q0 = qs[0] if isinstance(qs[0], dict) else {}
    out = []
    for o in (q0.get("options") or []):
        if not isinstance(o, dict):
            continue
        t = str(o.get("label") or "").strip()
        if t:
            out.append(re.sub(r"\s+", " ", t)[:60])
    return out


def _tool_brief(ti):
    """工具调用的「一句话说明」：命令/文件路径，够用户判断要不要去看。"""
    if not isinstance(ti, dict):
        return ""
    q = _ask_q(ti)
    if q:
        return q[:80]
    for k in ("command", "file_path", "path", "pattern", "url", "prompt"):
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:80]
    return ""


# ---- 任务台账：任务=sid 生命周期聚合（批次二 #5，不加新表） ----------------
# 五态机：running(在跑) / deciding(等你拍板) / done(办结待验收)
#         accepted(已通过) / rejected(已打回)
# 数据 = 在线会话列表（ext 列表条目） + notify_hist 历史行（重启不丢）。
# 验收态记账在办结行（kind=done）的 state 列：'' 待验收 / accepted / rejected；
# 'sent'(已确认等结果) 和 'done'(被后续完成清账) 是轮次处置记账，不影响任务态。
_LEDGER_STATES = ("running", "deciding", "done", "accepted", "rejected")


def _task_rows(online, hist, now=None):
    """(在线会话列表, notify_hist 行列表) -> 台账条目列表。纯函数，直测友好。
    hist 行至少含 n/ts/sid/kind/state/summary/about；两路输入都容缺键。"""
    now = int(now if now is not None else time.time())
    by_sid = {}
    for h in hist or []:
        if not isinstance(h, dict):
            continue
        by_sid.setdefault(str(h.get("sid") or ""), []).append(h)
    for rows in by_sid.values():
        rows.sort(key=lambda r: int(r.get("ts") or 0))   # 旧→新
    out = []
    seen = set()
    for x in online or []:
        if not isinstance(x, dict):
            continue
        sid = str(x.get("sid") or "")
        if not sid:
            continue
        seen.add(sid)
        rows = by_sid.get(sid) or []
        st = str(x.get("state") or "")
        last_pend = next((r for r in reversed(rows)
                          if r.get("kind") in ("perm", "ask", "input")), None)
        last_done = next((r for r in reversed(rows) if r.get("kind") == "done"), None)
        # 清账窗口只有 6h(perm 挂过夜后用户在终端放行,旧 perm 的 state 仍是空)——
        # 判「挂着」还要求它比末条办结行新:后面都跑完新一轮了,旧拍板必然已被处理
        pending = (last_pend is not None and not str(last_pend.get("state") or "")
                   and (last_done is None or int(last_pend.get("ts") or 0) > int(last_done.get("ts") or 0)))
        if pending:
            task_state = "deciding"
            waited = max(0, now - int(last_pend.get("ts") or 0))
        elif st == "busy" or not rows:
            task_state = "running"                   # 空历史在线会话：开屏可见，别消失
            waited = 0
        else:
            # 末条办结行定验收态（rows[-1] 可能是已被清账的 perm 行，不能拿来判态）
            last_done = next((r for r in reversed(rows) if r.get("kind") == "done"), None)
            task_state = _task_state_of_done(last_done) if last_done else "running"
            waited = 0
        out.append(_task_row(x, rows, task_state, waited))
    # 离线会话（exited/不在 online）：纯历史聚合——转录一直在，台账不能少行
    for sid, rows in by_sid.items():
        if sid in seen or not rows:
            continue
        last_done = next((r for r in reversed(rows) if r.get("kind") == "done"), None)
        task_state = _task_state_of_done(last_done) if last_done else "running"
        # agent 空着让调用方按 sid 推；cwd 一并带上——没有它推不出是哪个 agent 的会话
        out.append(_task_row({"sid": sid, "title": rows[-1].get("title") or "",
                              "cwd": rows[-1].get("cwd") or "",
                              "agent": "", "state": "exited"}, rows, task_state, 0))
    return out


def _task_state_of_done(row):
    """办结行 -> 任务态：'' 待验收；accepted/rejected 照抄；其余（sent/done 清账记账）归 done。"""
    s = str((row or {}).get("state") or "")
    return s if s in ("accepted", "rejected") else "done"


def _task_row(x, rows, task_state, waited):
    sid = str(x.get("sid") or "")
    done_rows = [r for r in rows if r.get("kind") == "done"]
    # 验收摘要=末三轮轮次概括拼接（概括已是模型产出，这里不再调模型）；
    # 零办结回落末条 about（「在做什么」），全空就是空串。
    sums = [str(r.get("summary") or "").strip()[:100]
            for r in done_rows if str(r.get("summary") or "").strip()]
    last_sum = "；".join(sums[-3:])[:320]
    if not last_sum and rows:
        last_sum = str(rows[-1].get("about") or "").strip()[:100]
    return {
        "sid": sid,
        "title": str(x.get("title") or (rows[-1].get("title") if rows else "") or ""),
        "agent": str(x.get("agent") or ""),
        "cwd": str(x.get("cwd") or ""),      # 离线行靠它推 agent（能力面过滤）
        "state": task_state,
        "waited": int(waited or 0),
        "first_ts": int(rows[0].get("ts") or 0) if rows else 0,
        "last_ts": int(rows[-1].get("ts") or (x.get("last") or 0)) if rows else int(x.get("last") or 0),
        "rounds": len(done_rows),
        "last_summary": last_sum,
    }


# ---- 派单（B2）：任务先落库，再在电脑上开个会话把话发进去 --------------------
# 以前台账只能看「电脑上已经开着的会话」——手机能看能回，不能派活。现在一条任务
# 从派出去那一刻就有身份（tid），sid 只是它临时的执行面：开会话失败/发送失败都
# 记在任务上，台账里看得见，不会因为「没有会话」就静默消失。
_TASK_POLL = 1.5          # 等会话出现/等会话可发的轮询间隔（秒）


def _task_add(title, agent, cwd, body):
    tid = "t" + uuid.uuid4().hex[:12]
    now = time.time()
    con = _db()
    try:
        con.execute("INSERT INTO tasks(tid,title,agent,cwd,sid,body,state,err,created,updated)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, title, agent, cwd, "", body, "dispatching", "", now, now))
        con.commit()
    finally:
        con.close()
    return tid


def _task_get(tid):
    con = _db()
    try:
        cur = con.execute("SELECT tid,title,agent,cwd,sid,body,state,err,created,updated"
                          " FROM tasks WHERE tid=?", (tid,))
        r = cur.fetchone()
    finally:
        con.close()
    if not r:
        return None
    return dict(zip(("tid", "title", "agent", "cwd", "sid", "body",
                     "state", "err", "created", "updated"), r))


def _task_update(tid, **f):
    f["updated"] = time.time()
    ks = list(f)
    con = _db()
    try:
        con.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in ks) + " WHERE tid=?",
                    [f[k] for k in ks] + [tid])
        con.commit()
    finally:
        con.close()


def _tasks_recent(days=30):
    try:
        con = _db()
        try:
            cur = con.execute("SELECT tid,title,agent,cwd,sid,body,state,err,created,updated"
                              " FROM tasks WHERE created>=? ORDER BY created",
                              (time.time() - days * 86400,))
            return [dict(zip(("tid", "title", "agent", "cwd", "sid", "body",
                              "state", "err", "created", "updated"), r)) for r in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        return []      # 读不到任务不该拖垮整个台账（会话那半边照样能看）


def _task_overlay(rows, tasks):
    """台账合并：有 sid 的任务盖在同一条会话行上（标题用任务标题，派单时用户说的
    才是这单活）；还没 sid 的（派单中/失败）自己占一行，sid="task:<tid>"——
    前端按会话处理的那套（点开、收起）对它无害。纯函数，直测友好。"""
    by_sid = {}
    for t in tasks or []:
        sid = str(t.get("sid") or "")
        if sid:
            by_sid.setdefault(sid, t)
    out, used = [], set()
    for r in rows:
        t = by_sid.get(str(r.get("sid") or ""))
        if t:
            r = dict(r)
            r["tid"] = t["tid"]
            r["title"] = t.get("title") or r.get("title") or ""
            r["task_created"] = int(t.get("created") or 0)
            used.add(t["tid"])
        out.append(r)
    for t in tasks or []:
        if t.get("tid") in used:
            continue
        out.append({
            "sid": "task:" + str(t.get("tid")), "tid": t.get("tid"),
            "title": t.get("title") or "", "agent": t.get("agent") or "",
            "cwd": t.get("cwd") or "",
            "state": "failed" if t.get("state") == "failed" else "dispatching",
            "waited": 0,
            "first_ts": int(t.get("created") or 0),
            "last_ts": int(t.get("updated") or t.get("created") or 0),
            "rounds": 0,
            "last_summary": str(t.get("err") or ""),
        })
    return out


# ---- 跨机器台账（B2.1）：本机 + 中继汇总的其他机器 ---------------------------
_REMOTE_LEDGER = {"at": 0.0, "data": []}


async def _relay_ledger_peers():
    """中继汇总的各机器台账（10 秒缓存）。没登记中继 / 不可达都当没有——
    工作台本机那半边照常，不能因为中继抽风整页空掉。"""
    rc = _relay_cfg()
    if not (rc.get("url") and rc.get("device_id") and rc.get("device_secret")):
        return []
    now = time.time()
    if now - float(_REMOTE_LEDGER["at"] or 0) < 10:
        return _REMOTE_LEDGER["data"]
    loop = asyncio.get_event_loop()
    machines = []
    try:
        d = await asyncio.wait_for(loop.run_in_executor(
            None, lambda: _relay_dev_call("/r/dev_ledger", {}, timeout=6)), 3.5)
        if isinstance(d, dict) and d.get("ok"):
            machines = d.get("machines") or []
    except Exception:
        machines = []
    _REMOTE_LEDGER["at"] = now
    _REMOTE_LEDGER["data"] = machines
    return machines


def _merge_remote_ledger(rows, machines, local_dev, face=None):
    """别的机器的台账行并进来：标 remote + machine（点开才知道去哪台看）。
    本机自己那份跳过——本机这份是刚算的，别出现两遍。"""
    out = list(rows)
    for m in machines or []:
        if str((m or {}).get("dev") or "") == str(local_dev or ""):
            continue
        items = [dict(it) for it in ((m or {}).get("items") or []) if isinstance(it, dict)]
        if face is not None:
            items = _face_only(items, face)
        name = str((m or {}).get("name") or (m or {}).get("dev") or "")
        for it in items:
            it["remote"] = 1
            it["machine"] = name
            it["dev"] = str((m or {}).get("dev") or "")
        out.extend(items)
    out.sort(key=lambda r: -(int(r.get("last_ts") or 0)))
    return out


# ---- 全文检索：手机上一句话找回「哪个会话里说过/改过这个」 -------------------
_SEARCH_FILES_MAX = 300            # 最多扫多少个转录文件（最近的优先）
_SEARCH_BYTES_MAX = 80 * 1024 * 1024   # 单次搜索读盘预算
_SEARCH_FILE_MAX = 64 * 1024 * 1024    # 单个转录文件超过这个就跳过


def _line_text(line):
    """一条转录消息里用户能读的文本（正文 + 工具入参）；没有就返回 ""。
    工具输出（tool_result）不收：又长又抖，搜出来也没用。"""
    try:
        o = json.loads(line)
    except Exception:
        return ""
    if not isinstance(o, dict):
        return ""
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    c = msg.get("content")
    parts = []
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        for x in c:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                parts.append(x["text"])
            elif x.get("type") == "tool_use":
                parts.append(json.dumps(x.get("input") or {}, ensure_ascii=False))
    return "\n".join(parts)[:4000]


def _snip_text(s, q):
    """在一段文本里给命中词截一小窗：前后共 ~100 字，折叠空白。"""
    i = max(s.lower().find(q.lower()), 0)
    a, b = max(0, i - 40), min(len(s), i + len(q) + 60)
    return re.sub(r"\s+", " ", s[a:b]).strip()


def _line_snippet(line, q):
    """命中的那一行 → 用户能读的片段。优先消息正文/工具入参，取不到才退回原始行。"""
    s = _line_text(line) or line
    return _snip_text(s, q)


# ---- 搜索索引：只读新追加的字节，查询走 sqlite（别每搜一次重解析所有 jsonl） ----
_SEARCH_QUERY_LINES = 5000         # 一次查询最多看多少条命中行（够组几十个会话）
_SEARCH_SCAN_BYTES = 20 * 1024 * 1024  # 单次索引最多读这么多新字节，剩下的下次再来


def _head_hash(p):
    """文件开头一小段的指纹：转录被重写（不是追加）时靠它认出来。"""
    try:
        with open(p, "rb") as fh:
            return hashlib.sha1(fh.read(4096)).hexdigest()[:16]
    except OSError:
        return ""


def _idx_migrate(con):
    con.execute("CREATE TABLE IF NOT EXISTS msg_file("
                "path TEXT PRIMARY KEY, size INTEGER, at REAL, head TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS msg_idx("
                "path TEXT, off INTEGER, ts REAL, sid TEXT, agent TEXT, title TEXT,"
                " txt TEXT, tl TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS msg_idx_sid ON msg_idx(sid)")


def _idx_update(con, cands, max_bytes=_SEARCH_SCAN_BYTES):
    """把候选转录里新追加的部分收进索引。文件变短（被重写）就当新文件重扫。
    返回本轮读了多少新字节（0 = 全部追平）。

    每个文件本轮最多读 budget/文件数：热会话一直在长，不按文件分公平的话预算
    全被它吃掉，老会话永远进不了索引（2026-11-21：31 个转录只索引了 5 个，
    用户「找会话没什么用」）。"""
    cands = sorted(cands or [], key=lambda r: -(int(r.get("last") or 0)))
    if not cands:
        return 0
    per = max(4 * 1024, max_bytes // len(cands))
    budget = max_bytes
    read_total = 0
    for f in cands:
        if budget <= 0:
            break
        p = str(f.get("path") or "")
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        row = con.execute("SELECT size,head FROM msg_file WHERE path=?", (p,)).fetchone()
        off = int(row[0]) if row else 0
        head = _head_hash(p)
        if row and (off > size or head != (row[1] or "")):
            con.execute("DELETE FROM msg_idx WHERE path=?", (p,))   # 重写：老行作废
            off = 0
        if off == size:
            continue
        try:
            with open(p, "rb") as fh:
                fh.seek(off)
                data = fh.read(min(size - off, budget, per))
        except OSError:
            continue
        budget -= len(data)
        read_total += len(data)
        cut = data.rfind(b"\n")              # 只收完整行；半行等下次追加
        if cut < 0:
            continue
        chunk = data[:cut + 1]
        pos = off
        rows = []
        for ln in chunk.split(b"\n")[:-1]:
            pos += len(ln) + 1
            txt = _line_text(ln.decode("utf-8", "ignore"))
            if not txt:
                continue
            rows.append((p, pos, int(f.get("last") or 0), str(f.get("sid") or ""),
                         str(f.get("agent") or ""), str(f.get("title") or ""),
                         txt, txt.lower()))
        if rows:
            con.executemany("INSERT INTO msg_idx(path,off,ts,sid,agent,title,txt,tl)"
                            " VALUES(?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO msg_file(path,size,at,head) VALUES(?,?,?,?)",
                    (p, off + len(chunk), time.time(), head))
    # 文件没了（会话被删/归档）：索引里也别留着搜得到
    gone = [r[0] for r in con.execute("SELECT path FROM msg_file LIMIT 5000").fetchall()
            if not os.path.exists(r[0])]
    for p in gone:
        con.execute("DELETE FROM msg_idx WHERE path=?", (p,))
        con.execute("DELETE FROM msg_file WHERE path=?", (p,))
    con.commit()
    return read_total


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _idx_query(con, q, limit=30, per=3):
    """按「这个会话有多像是在说这件事」排：命中条数多的在前，同条数按最近活跃。
    以前按「最新一条命中行」排——常开的大会话永远第一，搜出来跟没搜一样
    （2026-11-21 用户）。"""
    like = "%" + _escape_like(q.lower()) + "%"
    try:
        rows = con.execute(
            "SELECT sid, agent, title, MAX(ts) last, COUNT(*) c FROM msg_idx"
            " WHERE tl LIKE ? ESCAPE '\\' GROUP BY sid"
            " ORDER BY c DESC, last DESC LIMIT ?", (like, limit)).fetchall()
    except Exception:
        return []
    out = []
    for sid, agent, title, last, _c in rows:
        sid = str(sid or "")
        if not sid:
            continue
        snips = []
        for (txt,) in con.execute(
                "SELECT txt FROM msg_idx WHERE sid=? AND tl LIKE ? ESCAPE '\\'"
                " ORDER BY ts DESC LIMIT ?", (sid, like, per * 12)):
            sn = _snip_text(txt, q)
            if sn and sn not in snips:
                snips.append(sn)
                if len(snips) >= per:
                    break
        out.append({"sid": sid, "title": str(title or ""), "agent": str(agent or ""),
                    "last": int(last or 0), "snips": snips})
    return out


def _search_index(cands, q, limit=30):
    con = _db()
    try:
        _idx_migrate(con)
        _idx_update(con, cands)
        return _idx_query(con, q, limit)
    finally:
        try:
            con.close()
        except Exception:
            pass


_SEARCH_WARM_BYTES = 64 * 1024 * 1024   # 后台回填：每轮最多读这么多新字节


def _search_warm_round():
    """后台回填一轮，返回本轮读了多少新字节（0 = 追平了）。
    请求路径每次只肯花 20MB，存量两三个 G 靠用户搜是永远追不上的——
    夜里没人搜也要慢慢补齐，第二天「找会话」才找得到老会话。"""
    cands = _search_candidates()
    if not cands:
        return 0
    con = _db()
    try:
        _idx_migrate(con)
        return _idx_update(con, cands, max_bytes=_SEARCH_WARM_BYTES)
    finally:
        try:
            con.close()
        except Exception:
            pass


async def _search_warm_loop_startup(app):
    """追平前每 2 秒一轮；追平后每 10 分钟看一眼有没有新会话。

    只 spawn 任务、立刻返回：on_startup 钩子是逐个 await 的，钩子里 await 死循环
    会让站点永远不 bind（2026-11-21 事故：进程活着、8790 不监听、中继也连不上，
    整个机队离线；和 2026-11-18 那次 0.9.13 是同一类错）。"""
    async def _run():
        loop = asyncio.get_event_loop()
        while True:
            try:
                n = await loop.run_in_executor(None, _search_warm_round)
            except Exception:
                n = 0
            await asyncio.sleep(2 if n else 600)
    app["search_warm_task"] = asyncio.create_task(_run())


def _search_file(path, q, per=3):
    """一个转录文件里找关键词，返回最多 per 条干净片段。"""
    try:
        if os.path.getsize(path) > _SEARCH_FILE_MAX:
            return []
        txt = Path(path).read_bytes().decode("utf-8", "ignore")
    except OSError:
        return []
    ql = q.lower()
    snips = []
    for line in txt.splitlines():
        if ql not in line.lower():
            continue
        sn = _line_snippet(line, q)
        if sn and sn not in snips:
            snips.append(sn)
            if len(snips) >= per:
                break
    return snips


def _search_files(files, q, limit=30):
    """files: [{sid,path,title,agent,last}]。最近活跃的在前，带文件数/字节预算。"""
    out, total = [], 0
    for f in sorted(files or [], key=lambda r: -(int(r.get("last") or 0))):
        if len(out) >= limit:
            break
        p = str(f.get("path") or "")
        try:
            total += os.path.getsize(p)
        except OSError:
            continue
        if total > _SEARCH_BYTES_MAX:
            break
        snips = _search_file(p, q)
        if snips:
            out.append({"sid": str(f.get("sid") or ""), "title": str(f.get("title") or ""),
                        "agent": str(f.get("agent") or ""), "last": int(f.get("last") or 0),
                        "snips": snips})
    return out


def _search_candidates(limit=_SEARCH_FILES_MAX):
    """所有 agent 的会话转录（含在跑的）：候选集，按活跃排的工作交给索引/直扫。"""
    cands, seen = [], set()
    for ad in agents.adapters():
        try:
            items = ad.closed_sessions(set(), limit) or []
        except Exception:
            items = []
        for x in items:
            sid = str((x or {}).get("sid") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                p = ad.transcript_path(str((x or {}).get("cwd") or ""), sid)
            except Exception:
                p = None
            if not p:
                continue
            cands.append({"sid": sid, "path": str(p),
                          "title": str(x.get("title") or ""),
                          "agent": str(x.get("agent") or getattr(ad, "key", "") or ""),
                          "last": int(x.get("last") or 0)})
    return cands


def _search_all(q, limit=30):
    """本机所有会话里搜：索引优先；索引坏了（db 不可用等）退回直扫，功能不能整块没了。"""
    cands = _search_candidates()
    try:
        return _search_index(cands, q, limit)
    except Exception:
        return _search_files(cands, q, limit)


async def _relay_search_peers(q):
    """中继汇总的其他机器的命中（不缓存：每次查询的词都不一样）。"""
    rc = _relay_cfg()
    if not (rc.get("url") and rc.get("device_id") and rc.get("device_secret")):
        return []
    loop = asyncio.get_event_loop()
    try:
        d = await asyncio.wait_for(loop.run_in_executor(
            None, lambda: _relay_dev_call("/r/dev_search", {"q": q}, timeout=8)), 6)
    except Exception:
        return []
    if isinstance(d, dict) and d.get("ok"):
        return d.get("machines") or []
    return []


def _merge_remote_search(items, machines, local_dev):
    """别的机器的命中并进来。本机自己那台跳过（本地这份更新鲜，别出现两遍）。"""
    out = list(items)
    for m in machines or []:
        if str((m or {}).get("dev") or "") == str(local_dev or ""):
            continue
        name = str((m or {}).get("name") or (m or {}).get("dev") or "")
        for it in ((m or {}).get("items") or []):
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it["remote"] = 1
            it["machine"] = name
            it["dev"] = str((m or {}).get("dev") or "")
            out.append(it)
    out.sort(key=lambda r: -(int(r.get("last") or 0)))
    return out


async def _search_everywhere(request, q):
    """本机 + 别的机器。中继来要本机结果（remote=0）时别再问中继——会递归到超时。"""
    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(None, lambda: _search_all(q))
    if str((getattr(request, "query", None) or {}).get("remote") or "") == "0":
        return items
    machines = await _relay_search_peers(q)
    if not machines:
        return items
    return _merge_remote_search(items, machines, _relay_cfg().get("device_id"))


async def h_search(request):
    """GET /api/search?q= —— 跨会话全文检索（手机工作台上的「找会话」）。"""
    await require_token(request)
    q = str((getattr(request, "query", None) or {}).get("q") or "").strip()[:60]
    if not q:
        return web.json_response({"ok": False, "error": "给个关键词"}, status=400)
    items = await _search_everywhere(request, q)
    return web.json_response({"ok": True, "q": q, "items": items})


async def _task_wait_sid(cwd, known, t0, secs=60):
    """Mac 的 open_wrapped 不返回 sid：按「新出现的、同目录的会话」认。"""
    loop = asyncio.get_event_loop()
    while time.time() - t0 < secs:
        listed = await loop.run_in_executor(None, lambda: _tick_sessions(True))
        for x in listed or []:
            if str(x.get("sid") or "") in known:
                continue
            if str(x.get("cwd") or "") != cwd:
                continue
            return str(x["sid"])
        await asyncio.sleep(_TASK_POLL)
    return ""


async def _task_dispatch(tid):
    """后台壳：任何意外都要落成「派单失败」——卡在 dispatch 中比失败更糟。"""
    try:
        await _task_dispatch_impl(tid)
    except Exception as e:
        try:
            _task_update(tid, state="failed", err=("异常：%s" % str(e))[:200])
        except Exception:
            pass


async def _task_dispatch_impl(tid):
    """后台：开会话 → 等它出现 → 把那句话发进去 → 状态记成 running。
    任何一步失败都落到 tasks.state/err 上（台账可见），不静默丢单。"""
    t = _task_get(tid)
    if not t:
        return
    cwd = t["cwd"]
    known = {str(x.get("sid")) for x in _cached_sessions()}
    try:
        d = await _sess_new_core({"cwd": cwd, "agent": t["agent"]})
    except Exception as e:
        _task_update(tid, state="failed", err=str(e)[:200])
        return
    if not d.get("ok"):
        _task_update(tid, state="failed", err=str(d.get("error") or "会话没开起来")[:200])
        return
    sid = str(d.get("sid") or "")
    if not sid:
        sid = await _task_wait_sid(cwd, known, time.time())
    if not sid:
        _task_update(tid, state="failed", err="会话没起来（没等到新会话）")
        return
    _task_update(tid, sid=sid)
    ok, msg = False, ""
    for _ in range(12):
        x = await _find_session(sid)
        if x:
            ok, msg = await _send_verified(x, t["body"])
            if ok:
                break
        await asyncio.sleep(_TASK_POLL)
    _task_update(tid, state=("running" if ok else "failed"),
                 err=("" if ok else ("没发进去：%s" % (msg or "会话没就绪"))[:200]))


async def _task_new_core(data):
    """落一条任务并立刻返回 tid（开会话/发送在后台）。校验都在这里：
    没内容、目录不存在都当场拒绝——建了再失败只会让台账多一行看不懂的。"""
    body = str((data or {}).get("content") or "").strip()
    if not body:
        return {"ok": False, "error": "写点要干的事再派"}
    cwd = str((data or {}).get("cwd") or "").strip() or str(Path.home())
    if not Path(cwd).is_dir():
        return {"ok": False, "error": "目录不存在：%s" % cwd}
    agent = str((data or {}).get("agent") or "").strip()
    if not agent:
        # 没指定就现在定下来（默认 agent）：台账行要带 agent 才过产品线的能力面过滤
        ad, _err = _pick_agent({})
        agent = ad.key if ad else ""
    title = (str((data or {}).get("title") or "").strip()
             or body.splitlines()[0].strip()[:60])
    tid = _task_add(title, agent, cwd, body)
    asyncio.ensure_future(_task_dispatch(tid))
    return {"ok": True, "tid": tid}


async def h_task_new(request):
    """POST /api/task_new {content, agent?, cwd?, title?} —— 手机派单。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    r = await _task_new_core(d)
    return web.json_response(r, status=(200 if r.get("ok") else 400))


async def _ledger_items(request):
    online = await _ext_sessions_items(request)
    try:
        con = _db()
        try:
            cur = con.execute(
                "SELECT n,ts,sid,title,kind,state,summary,about,act,cwd FROM notify_hist"
                " WHERE ts>=? ORDER BY ts", (int(time.time()) - 30 * 86400,))
            hist = [dict(zip(("n", "ts", "sid", "title", "kind", "state",
                              "summary", "about", "act", "cwd"), r)) for r in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        hist = []
    # 产品线能力面：工作台和会话列表一个口径（codeboss 不见 hermes）
    face = _face_for(request)
    rows = _face_only(_task_overlay(_task_rows(online, hist), _tasks_recent()), face)
    # 跨机器（B2.1）：同一个账号下别的机器在干什么。拿不到就只看本机。
    # remote=0 是中继来要「你这台本机台账」的标记：它自己汇总别人的，别回来再问中继
    # （否则 A 问中继、中继问 A，递归到超时）。
    if str((getattr(request, "query", None) or {}).get("remote") or "") == "0":
        return rows
    try:
        machines = await _relay_ledger_peers()
    except Exception:
        machines = []
    if not machines:
        return rows
    return _merge_remote_ledger(rows, machines, _relay_cfg().get("device_id"), face)


# ---- 工作台整理：模型判断「值不值得看」，用户可隐藏/删除 --------------------
# 30 天的台账会把测试会话、废弃的临时会话全堆上来，一条条翻很累（2026-11-19
# 用户）。判断交给模型：按 sid 缓存，任务有新动静才重判；用户手动隐藏/删除
# 永远压过模型结论。所有「收起」都只影响工作台——会话和历史本身不动。
TV_FILE = HERE / "task_view.json"
_TV = {}
_TRIAGE_AT = [0.0]
_TRIAGE_PROMPT = (
    "你在帮用户整理手机上的「工作台」：每一行是一个 agent 会话（=一件事/一个任务）。"
    "判断它现在对用户还有没有价值。\n"
    "keep=1：用户自己发起的真实任务、有进展或结果、在等你处理的。\n"
    "keep=0：测试/试探会话、空会话、明显废弃不再用的、重复的、没有实质内容的临时会话。\n"
    "act：keep=0 时给 hide（不确定，先收起）或 delete（明确是废弃垃圾）。\n"
    "只回 JSON 数组，每项 {\"i\": 序号, \"keep\": 0或1, \"why\": \"不超过12字\", "
    "\"act\": \"hide|delete|\"}，不要任何别的文字。")


def _tv():
    if not _TV:
        try:
            d = json.loads(TV_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("v"), dict):
                _TV.update(d["v"])
        except Exception:
            pass
    return _TV


def _tv_save():
    try:
        tmp = TV_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": _tv()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(TV_FILE)
    except Exception:
        pass


def _tv_of(sid, last_ts):
    """对一条任务的判断只在「最近一次动静」之前有效：任务有了新进展就翻篇，
    藏起来的不蒙住新活，废弃的会被重新判断。"""
    e = _tv().get(str(sid or ""))
    if not isinstance(e, dict) or int(e.get("at") or 0) < int(last_ts or 0):
        return {}
    return e


def _tv_apply(row):
    e = _tv_of(row.get("sid"), row.get("last_ts"))
    row["hidden"] = 1 if e.get("hide") else 0
    row["deleted"] = 1 if e.get("del") else 0
    row["keep"] = e.get("keep")          # 1 有用 / 0 没用 / None 没判过
    row["vwhy"] = str(e.get("why") or "")
    row["vact"] = str(e.get("act") or "")
    return row


async def h_task_ledger(request):
    """GET /api/task_ledger —— 任务（=sid）生命周期聚合视图（两条线同一页：工作台）。
    每行带 hidden/keep/vwhy：模型判断与用户隐藏都由前端决定要不要透出。"""
    await require_token(request)
    items = [_tv_apply(it) for it in await _ledger_items(request)]
    return web.json_response({"ok": True,
                              "items": [it for it in items if not it["deleted"]]})


async def h_task_view(request):
    """POST /api/task_view {sid, action: hide|unhide|delete|keep}
    delete = 从工作台删除（会话和历史不动）；keep = 保留，压过模型结论。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        d = {}
    sid = str(d.get("sid") or "")
    act = str(d.get("action") or "")
    if not sid or act not in ("hide", "unhide", "delete", "keep"):
        return web.json_response({"ok": False, "error": "参数不对"}, status=400)
    v = _tv()
    e = v.get(sid)
    if not isinstance(e, dict):
        e = v[sid] = {}
    if act == "hide":
        e["hide"] = 1
    elif act == "unhide":
        e["hide"] = 0
    elif act == "delete":
        e["del"] = 1
    else:
        e["keep"] = 1
        e["hide"] = 0
        e.pop("del", None)
    e["at"] = int(time.time())
    _tv_save()
    return web.json_response({"ok": True})


def _triage_text(rows):
    now = int(time.time())
    lines = []
    for i, it in enumerate(rows):
        age = max(0, now - int(it.get("last_ts") or 0))
        ago = ("%d小时前" % (age // 3600)) if age >= 3600 else ("%d分钟前" % (age // 60))
        lines.append("%d. 状态=%s 标题=%s 助手=%s %s 轮次=%d 最近=%s" % (
            i, it.get("state") or "-", (it.get("title") or "")[:60],
            it.get("agent") or "-", ago, int(it.get("rounds") or 0),
            (it.get("last_summary") or "")[:60]))
    return "\n".join(lines)


def _triage_parse(txt):
    s = str(txt or "")
    a, b = s.find("["), s.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = json.loads(s[a:b + 1])
    except Exception:
        return []
    return arr if isinstance(arr, list) else []


async def _triage_call(text):
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return None
    try:
        import aiohttp
        body = {"prompt": _TRIAGE_PROMPT, "purpose": "task_triage", "text": text}
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm", json=body,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict) and d.get("ok"):
            return str(d.get("text") or "")
    except Exception:
        pass
    return None


async def h_task_triage(request):
    """POST /api/task_triage —— 让模型过一遍还没判断过的任务（120 秒节流）。
    永远不判「等你拍板」的行：那是一句话的事，不许被收起来。"""
    await require_token(request)
    now = int(time.time())
    if now - int(_TRIAGE_AT[0]) < 120:
        return web.json_response({"ok": True, "judged": 0})
    _TRIAGE_AT[0] = now
    rows = [_tv_apply(it) for it in await _ledger_items(request)]
    cand = [it for it in rows
            if not it["deleted"] and not it["hidden"]
            and it["state"] != "deciding" and it.get("keep") is None][:60]
    if not cand:
        return web.json_response({"ok": True, "judged": 0})
    txt = await _triage_call(_triage_text(cand))
    if txt is None:
        return web.json_response({"ok": False, "error": "模型没应答"})
    v = _tv()
    n = 0
    for o in _triage_parse(txt):
        if not isinstance(o, dict):
            continue
        try:
            i = int(o.get("i"))
        except Exception:
            continue
        if not (0 <= i < len(cand)):
            continue
        e = v.get(cand[i]["sid"])
        if not isinstance(e, dict):
            e = v[cand[i]["sid"]] = {}
        e["keep"] = 1 if int(o.get("keep") or 0) else 0
        e["why"] = str(o.get("why") or "")[:40]
        if not e["keep"]:
            e["act"] = "delete" if str(o.get("act") or "") == "delete" else "hide"
        e["at"] = now
        n += 1
    _tv_save()
    return web.json_response({"ok": True, "judged": n})


def _task_accept_core(sid, verdict, note):
    """验收记账（批次二 #6）。accept **不携带正文**——误触通过绝不放行 note；
    reject 返回 send_text，由 handler 经既有发送链回注会话。结论以最后一次
    点的为准（打回后改主意=允许覆盖）。"""
    sid = str(sid or "").strip()
    if not sid:
        return {"ok": False, "error": "bad sid"}
    if verdict not in ("accept", "reject"):
        return {"ok": False, "error": "bad verdict"}
    try:
        con = _db()
        try:
            row = con.execute(
                "SELECT n FROM notify_hist WHERE sid=? AND kind='done'"
                " ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
            if not row:
                return {"ok": False, "error": "no_done_row"}
            con.execute("UPDATE notify_hist SET state=? WHERE n=?",
                        ("accepted" if verdict == "accept" else "rejected", row[0]))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    out = {"ok": True, "state": "accepted" if verdict == "accept" else "rejected"}
    if verdict == "reject":
        out["send_text"] = "老板打回：" + str(note or "").strip()
    return out


async def h_task_accept(request):
    """POST /api/task_accept —— 老板验收：{sid, verdict:"accept"|"reject", note?}。
    reject 的 note 发回会话（前缀「老板打回：」）；accept 不带任何正文。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    r = _task_accept_core(d.get("sid"), str(d.get("verdict") or ""), str(d.get("note") or ""))
    if r.get("ok") and r.get("send_text"):
        try:
            x = await _find_session(str(d.get("sid") or ""))
            if x:
                ok, msg = await _send_verified(x, r["send_text"])
                if not ok:
                    r["send_error"] = str(msg or "")[:120]   # 记账已落,发送失败只带错误
            else:
                r["send_error"] = "会话不在线"
        except Exception as e:
            r["send_error"] = str(e)[:120]
    return web.json_response(r, status=(200 if r.get("ok") else 400))


# ---- 决定的业务语义：到底在确认什么、在选什么 --------------------------------
# 手机上的通知不能只说「Bash 需要确认」——用户要判断的是「要不要让它跑这条命令、
# 这条命令会不会出事」。事实全部来自结构化输入（hook 的 tool_name/tool_input），
# 不靠模型猜；屏幕上的选项只用来认「这一项是允许一次还是始终允许」。
_TOOL_ACT = {
    "Bash": "运行命令", "BashOutput": "读取命令输出", "Edit": "修改文件",
    "MultiEdit": "修改文件", "Write": "写入文件", "NotebookEdit": "修改笔记本",
    "Read": "读取文件", "Glob": "查找文件", "Grep": "搜索内容",
    "WebFetch": "访问网址", "WebSearch": "联网搜索", "Task": "派出子任务",
    "TodoWrite": "更新任务清单", "ExitPlanMode": "确认执行计划",
    "AskUserQuestion": "回答一个问题",
}

# (正则, 说明, 级别)。只认「真出过事」的形状，宁可少报也别把普通命令全标红。
_RISK_RULES = (
    (r"\brm\s+(?:-[a-zA-Z]*[rf][a-zA-Z]*\s+)+\S", "删除文件/目录，不可撤销", "high"),
    (r"\bgit\s+(?:push\b[^|;]*--force|reset\s+--hard|clean\s+-[a-z]*f)", "改写/丢弃 git 历史或未跟踪文件", "high"),
    (r"\b(?:sudo|doas)\b", "以管理员权限执行", "high"),
    (r"(?:curl|wget)\b[^|;]*\|\s*(?:sh|bash|zsh)\b", "从网络下载并直接执行脚本", "high"),
    (r"\b(?:DROP|TRUNCATE)\s+TABLE\b|\bDELETE\s+FROM\b", "删除数据库数据", "high"),
    (r"\b(?:kubectl\s+(?:delete|apply)|terraform\s+apply|docker\s+push|npm\s+publish)\b", "发布/变更线上环境", "high"),
    (r">\s*/dev/(?:sd|disk)", "直接写块设备", "high"),
    (r"\bchmod\s+(?:-R\s+)?777\b|\bchown\s+-R\b", "放开/变更文件权限", "medium"),
    (r"\bscp\b|\brsync\b[^|;]*(?:@|::)|\bcurl\b[^|;]*(?:-d|--data|-F)\b", "把数据发往外部", "medium"),
)

_OPT_RULES = (
    ("allow_always", r"don'?t ask again|always allow|always|不再询问|始终|总是|一直允许"),
    ("allow_once", r"^(?:yes|allow|approve|proceed|accept|ok)\b|允许|批准|同意|继续|确认"),
    ("deny", r"^(?:no|deny|reject|cancel|decline|abort)\b|拒绝|取消|不允许|否"),
    ("edit", r"\bedit\b|修改|编辑"),
)
_OPT_EFFECT = {
    "allow_once": "只这一次执行",
    "allow_always": "以后同类请求不再询问（会写进权限配置）",
    "deny": "不执行，把理由告诉 AI",
    "edit": "先改一下再执行",
    "unknown": "看不懂这一项，请打开会话确认",
}


def _ti_target(tool, ti):
    """从工具入参里取「这次要动的东西」：命令 / 文件 / 网址 / 要回答的问题。"""
    if not isinstance(ti, dict):
        return ""
    if tool == "AskUserQuestion":
        q = _ask_q(ti)
        if q:
            return q[:300]
        opts = _ask_opts(ti)
        if opts:
            return " / ".join(opts[:6])[:300]
    for k in ("command", "file_path", "notebook_path", "path", "pattern",
              "url", "query", "prompt"):
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:300]
    return ""


def _scan_risk(tool, target):
    """确定性风险扫描：返回 [{level, text}]，按 high→medium 排序。"""
    if not target:
        return []
    out, seen = [], set()
    for rx, why, lvl in _RISK_RULES:
        if re.search(rx, target, re.I) and why not in seen:
            seen.add(why)
            out.append({"level": lvl, "text": why})
    out.sort(key=lambda x: 0 if x["level"] == "high" else 1)
    return out


def _opt_kind(text):
    s = (text or "").strip()
    for kind, rx in _OPT_RULES:
        if re.search(rx, s, re.I):
            return kind
    return "unknown"


def _card_verified(target, snap):
    """hook 说的是不是屏幕上正在问的这件事。

    取目标里的前一段做探针（去掉引号/空白），在弹层文字里找。找不到 = 没对上：
    可能通知晚到、弹层已经换了、或者屏幕上问的是另一件事。宁可不给快捷按钮。"""
    if not target or not snap:
        return False
    probe = re.sub(r"[\s'\"`]+", "", target)[:12]
    if len(probe) < 3:
        return False
    hay = re.sub(r"[\s'\"`]+", "", "".join(snap.get("text", {}).values())
                 + "".join(snap.get("rows") or []))
    return probe in hay


def _decision_card(meta, snap=None, why=""):
    """一张「决定卡」：确认什么、为什么、每个选项实际意味着什么、有什么风险。

    meta = {"tool":…, "ti": tool_input, "desc":…}；snap = _dlg_snapshot_rows()。
    snap 为空时只给事实（hook 的结构化输入），选项要等屏幕现读。"""
    meta = meta or {}
    tool = str(meta.get("tool") or "")
    target = _ti_target(tool, meta.get("ti")) or str(meta.get("desc") or "")
    card = {"ask": {"tool": tool or "工具",
                    "action": _TOOL_ACT.get(tool, "调用工具"),
                    "target": target},
            "why": re.sub(r"\s+", " ", str(why or "")).strip()[:200],
            "risk": _scan_risk(tool, target),
            "options": [], "verified": None}
    # 风险 agent 二审（窗口核心 Phase 2）：不确定区才问，8s 上限，降级留痕。
    # 阻塞预算在 executor 里（调用点已挪到线程，不卡事件循环）。
    try:
        rr = risk_review.grade(tool, target, meta.get("ti"), card["risk"],
                               audit_path=_PERM_AUDIT)
    except Exception:
        rr = None
    card["risk_review"] = rr
    if rr and rr.get("level") == "high":
        # 高风险一律撤一键放行（下面 verified 分支），note 说明原因——
        # 降级要看得见，别让用户只看到快捷按钮消失却不知道为什么
        card["ask"]["note"] = ("agent 评级：高——请在会话里确认" if rr.get("src") == "agent"
                               else "风险评级：高——请在会话里确认")
    if snap:
        for n in snap.get("opts") or []:
            t = (snap.get("text", {}).get(n) or "").strip()
            kind = _opt_kind(t)
            card["options"].append({"n": n, "text": t[:120], "kind": kind,
                                    "effect": _OPT_EFFECT.get(kind, "")})
        # agent 评了 high：撤一键放行（错误成本不对称——宁可多问一次）
        card["verified"] = (False if (rr and rr.get("level") == "high")
                            else _card_verified(target, snap))
    if not card["options"]:               # 没有现读屏幕：至少把 hook 里的选项列出来
        card["options"] = [{"n": i + 1, "text": t, "kind": "choose", "effect": ""}
                           for i, t in enumerate(_ask_opts((meta or {}).get("ti")))]
    return card


# ---- 权限请求号与决策回传（P2）---------------------------------------------
# Claude 的 PermissionRequest 钩子不带任何 id（只有 session/tool），手机上的「这条是
# 哪次请求」和事后对账都得由 host 生成一个。P3 的阻塞式批准也靠它对上挂起的等待者。
_PERM_META = {}      # (sid, rid) -> {tool, desc, text, ts}
_PERM_DONE = {}      # (sid, rid, option_id) -> via：同一个决策只记一次
_PERM_LOCK = threading.Lock()
_PERM_AUDIT = HERE / "logs" / "perm_audit.jsonl"


def _perm_new(sid, tool, desc, text, ti=None):
    rid = "pm_" + uuid.uuid4().hex[:12]
    with _PERM_LOCK:
        if len(_PERM_META) > 400:
            for k in list(_PERM_META)[:100]:
                _PERM_META.pop(k, None)
        _PERM_META[(sid, rid)] = {"tool": tool, "desc": desc, "text": text,
                                  "ti": ti if isinstance(ti, dict) else {},
                                  "ts": time.time()}
    return rid


def _perm_audit_write(row, path=None):
    """决策留痕：本机 logs/perm_audit.jsonl（追加、5MB 轮转）。不写中继——
    这是设备侧的行为记录，中继的 admin_audit 只记管理员操作。"""
    p = Path(path) if path else _PERM_AUDIT
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
            p.replace(p.with_name(p.name + ".1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def _perm_answer_core(sid, rid, oid, label="", via="keys", path=None):
    """记一条权限决策，幂等：同 (sid, request_id, option_id) 只落一次审计。
    P3 起这里先唤醒挂起的阻塞钩子，再记（同一函数，手机端接口不变）。"""
    if not rid or not oid:
        return {"ok": False, "error": "missing"}
    key = (sid, rid, oid)
    with _PERM_LOCK:
        prev = _PERM_DONE.get(key)
        if prev is not None:
            return {"ok": True, "dup": True, "via": prev}
        _PERM_DONE[key] = via
        if len(_PERM_DONE) > 400:
            for k in list(_PERM_DONE)[:100]:
                _PERM_DONE.pop(k, None)
        meta = dict(_PERM_META.get((sid, rid)) or {})
    _perm_audit_write({
        "ts": round(time.time(), 3), "sid": sid,
        "agent": agents.agent_of(sid) or "",
        "request_id": rid, "option_id": oid, "label": str(label or "")[:60],
        "via": via, "tool": meta.get("tool") or "",
        "desc": (meta.get("desc") or "")[:160],
        "text": (meta.get("text") or "")[:160]}, path=path)
    return {"ok": True, "via": via}


def _ev_local(d):
    """本地兜底：与中继 _np_eval 同一套语义（规则以中继那份为准）。
    不管中继是否可用都跑一遍，本地状态机就不会落后。"""
    pol = _np_get()
    sid = d.get("session_id") or ""
    st = _EV.setdefault(sid, {})
    now = time.time()
    name = d.get("hook_event_name") or ""
    out = []
    maxn = int(pol.get("max_text") or 160)
    if name == "UserPromptSubmit":
        st["prompt"] = now
        st["stop"] = 0
        # 人已经回话了（电脑上敲的、手机上发的都在这里）：上一轮「等你点头」的提醒
        # 当场作废，页面浮条和提醒历史都不再挂着（见 _nh_answered）。
        _nh_answered(sid)
        # 这一轮的原话：语音概括「在做什么」的来源。趁 Claude 还在跑就先算好。
        pt = re.sub(r"\s+", " ", str(d.get("prompt") or "")).strip()[:400]
        st["ptext"] = pt
        _about_kick(sid, pt, _sid_title(sid))
    elif name == "Stop":
        if d.get("stop_hook_active"):
            return out                       # 自动续跑：不是真停
        # Codex 没有「用户发话」这个事件（notify 只在一轮跑完时给一次，payload 里带着
        # 这一轮的原话）。补上 ptext：语音概括「在做什么」和卡住提醒都靠它。
        if not st.get("ptext"):
            p0 = re.sub(r"\s+", " ", str(d.get("prompt") or "")).strip()[:400]
            if p0:
                st["ptext"] = p0
                _about_kick(sid, p0, _sid_title(sid))
        st["stop"] = now
        st["done_at"] = now                 # 本轮已完成：idle_prompt 不再补发
        msg = re.sub(r"\s+", " ", (d.get("last_assistant_message") or "").strip())
        out.append({"kind": "done",
                    "text": _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")})
    elif name == "PermissionRequest":
        if now - st.get("perm", 0) < float(pol.get("perm_throttle") or 0):
            return out                       # 同一批工具调用只报一次
        st["perm"] = now
        st["perm_hook"] = now              # 这条是权威的（带 rid/选项），见 Notification 分支
        tool = d.get("tool_name") or "工具"
        ti = d.get("tool_input")
        brief = _tool_brief(ti)
        text = _np_fmt(pol.get("perm_text") or "{tool} 需要确认：{brief}",
                       tool=tool, brief=brief)
        # 业务语义：说清在确认什么 + 风险。通知栏上就能判断，不用先开 App。
        card = _decision_card({"tool": tool, "ti": ti, "desc": brief},
                              why=st.get("ptext") or "")
        if card["risk"] and card["risk"][0]["level"] == "high":
            text = "⚠️ " + text + " · " + card["risk"][0]["text"]
        out.append({"kind": "perm", "text": text,
                    "request_id": _perm_new(sid, tool, brief, text, ti),
                    "tool": tool, "description": brief,
                    "ask": card["ask"], "risk": card["risk"], "why": card["why"],
                    # 在选什么：AskUserQuestion 的选项来自 hook 入参（选一项 ≠ 批准/拒绝）；
                    # 普通权限确认是批准/拒绝两个方向。屏幕上真实几个选项以现读的屏幕为准。
                    "options": ([{"id": str(i + 1), "label": t, "is_deny": False}
                                 for i, t in enumerate(_ask_opts(ti))]
                                or [{"id": "allow", "label": "批准", "is_deny": False},
                                    {"id": "deny", "label": "拒绝", "is_deny": True}]),
                    "expires_at": None, "ingress": "hook"})
    elif name == "StopFailure":
        err = str(d.get("error") or "unknown")
        st["stop"] = 0                       # 出错停了，不再按「等待回复」重复提醒
        if err in ("rate_limit", "billing_error") and now - st.get("limit", 0) > 60:
            st["limit"] = now
            msg = re.sub(r"\s+", " ", str(d.get("error_details") or "").strip())
            if not msg:
                msg = re.sub(r"\s+", " ", str(d.get("last_assistant_message") or "").strip())
            if not msg:
                msg = "模型额度用完了" if err == "rate_limit" else "额度或账单异常"
            out.append({"kind": "limit",
                        "text": _np_fmt(pol.get("limit_text") or "{msg}", msg=msg[-maxn:])})
    elif name == "Notification":
        nt = str(d.get("notification_type") or "")
        msg = re.sub(r"\s+", " ", str(d.get("message") or "").strip())
        if nt in ("permission_prompt", "worker_permission_prompt"):
            # 一次权限请求会来两条 hook：PermissionRequest（上面那条，带 rid/选项）和
            # 6 秒后 Claude Code 自己的这条镜像——"Claude needs your permission"。
            # 4 秒的 perm_throttle 拦不住，手机就被问两遍（2026-11-19 用户实报）。
            # 同一轮里已经报过 PermissionRequest 的，这条只是回声，吞掉；
            # 只发 Notification 的旧版客户端（没 perm_hook）照旧提醒。
            hook = float(st.get("perm_hook") or 0)
            mirrored = hook and hook > float(st.get("prompt") or 0)
            if not mirrored and now - st.get("perm", 0) >= float(pol.get("perm_throttle") or 0):
                st["perm"] = now
                out.append({"kind": "perm", "text": (msg or "需要你确认")[-maxn:]})
        elif nt == "idle_prompt":
            # 完成→空闲：Stop 已经报过「完成」就不再补发；空闲提醒只报一次、不持续追
            if st.get("done_at", 0) > st.get("prompt", 0):
                return out
            if (st.get("stop", 0) > st.get("prompt", 0)
                    and now - st.get("stop", 0) >= float(pol.get("idle_after") or 0)):
                st["prompt"] = now           # 一轮只提醒一次
                st["done_at"] = now
                out.append({"kind": "done",
                            "text": _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")})
        elif nt in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            if now - st.get("ask", 0) >= 4:
                st["ask"] = now
                out.append({"kind": "ask",
                            "text": _np_fmt(pol.get("ask_text") or "{msg}",
                                            msg=(msg or "需要你输入或执行命令")[-maxn:])})
        elif nt == "agent_completed":
            if now - st.get("done_at", 0) >= 10:   # Stop 和 agent_completed 可能前后脚都来
                st["done_at"] = now
                out.append({"kind": "done",
                            "text": _np_fmt(pol.get("done_text") or "{msg}", msg=msg[-maxn:] or "已完成")})
    kinds = pol.get("kinds") or {}
    snd = pol.get("sound") or {}
    voi = pol.get("voice") or {}
    return [dict(x, sound=snd.get(x["kind"]) or x["kind"], voice=bool(voi.get(x["kind"])))
            for x in out if kinds.get(x["kind"], True)]


# ---- 「完成」其实在等你动手 --------------------------------------------------
# 助手把活干完，最后一句常常是「要不要现在发布？」「确认后我就推送」
# 「这一步需要你手动执行 ./publish-host.sh」——状态机看是 done，人却得回一句/动一下手。
# 这种报「需要确认」（kind=perm）：通知标题/角标/语音都跟着变，不再安静地报「完成」。






_OC_MARK = {"done_verified": "✅ ", "failed": "❌ ", "blocked": "⛔ "}


def _outcome_of(sid, d):
    """这一轮的结论：看转录证据，不看模型怎么说。算不出来就 None（不瞎判）。"""
    try:
        p = agents.adapter_of(sid).transcript_path(d.get("cwd") or "", sid)
        if not p:
            return None
        rs = outcome.analyze_file(str(p), tail_bytes=1024 * 1024, turns=1)
        return rs[-1] if rs else None
    except Exception:
        return None


def _oc_text(oc, text):
    """把证据结论放到提醒最前面；未核实的保持原样（不冒充成功）。"""
    mark = _OC_MARK.get((oc or {}).get("state") or "")
    if not mark:
        return text
    lab = re.sub(r"\s+", " ", (oc.get("label") or "")).strip()
    msg = (text or "").strip()
    return mark + lab + (" · " + msg if msg and lab not in msg else "")


async def _ev_remote(d, sid, title, local):
    """中继算：把原始事件转给 /r/dev/ev，用返回的提醒替代本地结果。
    中继不可达/超时 → 用本地兜底，提醒不会丢。"""
    items = local
    src = "local"
    _name = d.get("hook_event_name") or ""
    if _name == "Stop":
        _TRACE.pop(str(sid), None)     # 上一轮的判定来路别串到这轮
    _msg = (re.sub(r"\s+", " ", (d.get("last_assistant_message") or "").strip())
            if _name == "Stop" else "")
    # 「完成其实在等你」是智能判断（付费）；免费档不做这一步，保持规则结果。
    na_task = (asyncio.ensure_future(_na_need(_msg, sid))
               if (_name == "Stop" and _msg and _voice_ok() and _na_pref()) else None)
    # 语音概括「AI 这轮回复了什么」：和「是不是在等你」并行算，别串行等两次模型。
    # 通知路径上最多等 2.5 秒（见下面 rp_out）；算不出来就不念概括，照常提醒。
    # 只在 Stop 上算：Notification 里的旧回复可能已经是上一轮的，宁可不念也别念错。
    # 这一轮根本不会念语音（完成类提醒关了/语音关了）就别花模型调用。
    _voice_done = any((x or {}).get("kind") == "done" and (x or {}).get("voice")
                      for x in (local or []))
    # 前因：这一轮用户到底让干什么（原话，不花钱）。既喂给概括模型（不然它只看到
    # 回复尾巴，开头会断在半截），也随提醒落进「提醒历史」。
    cause_out = _cause_for(sid)
    rp_task = (asyncio.ensure_future(_reply_get(sid, _msg, cause_out))
               if (_name == "Stop" and _msg and _voice_done and _voice_ok()) else None)
    rc = _relay_cfg()
    if local is not None and rc.get("device_id") and rc.get("url"):
        try:
            loop = asyncio.get_event_loop()
            payload = {"ev": d, "title": title}
            r = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _relay_dev_call("/r/dev/ev", payload, 3)), 4)
            if r.get("ok"):
                items = r.get("notify") or []
                src = "relay"
                if isinstance(r.get("remind"), bool):   # 中继是计费方：顺手校准本地套餐缓存
                    _USE_ST.setdefault("feat", {})["remind"] = r["remind"]
                    _USE_ST["feat_ts"] = time.time()
        except Exception:
            pass
    need_act = False
    if na_task is not None:
        try:
            need_act = bool(await na_task)
        except Exception:
            need_act = False
    if _name in ("UserPromptSubmit", "Stop"):
        _BLOCK.pop(sid, None)        # 人回话了 / 一轮正常结束：阻塞提醒停止
    # 完成→空闲：本轮已报过完成，就不让中继那份再补一条「等待回复」，也不重复追
    st = _EV.setdefault(sid, {})
    if (_name == "Notification" and d.get("notification_type") == "idle_prompt"
            and st.get("done_at", 0) > st.get("prompt", 0)):
        items = []
    pol = _np_get()
    # 这轮其实在等人：把「完成」改名成「需要确认」（perm 的标题/角标/语音都是现成的）
    na_ok = need_act and bool((pol.get("kinds") or {}).get("perm", True))
    ab_out = _about_for(sid) if _voice_ok() else ""   # 语音开头那半句（关掉/没算出来就空）
    rp_out = ""
    if rp_task is not None:
        try:
            # shield：等超时了也让任务继续跑完、把结果存进缓存，下一轮/重连还能用
            rp_out = await asyncio.wait_for(asyncio.shield(rp_task), 2.5)
        except Exception:
            rp_out = ""
    _rp = rp_out if isinstance(rp_out, dict) else {}
    # 概括模型单独给的「要你拍板的那件事」比 na 的 YES/NO 更具体：它写出了一句
    # 「要不要发布 0.7.39 版本？」这种待决问句，就说明这轮确实停在用户这里。
    # 两个模型意见相反时以它为准（2026-11-09 真实漏报：na 判 NO，act 明明有）。
    act_wait = bool((_rp.get("act") or "").strip())
    wait_user = na_ok or act_wait
    # 这一轮怎么判出来的：na 是规则/模型/模型没答上，act 是原话摘抄还是被丢掉。
    # 随「跑完」那条提醒落库，手机「提醒健康」直接算账（不另开接口、不另存一份）。
    tr = _TRACE.pop(str(sid), {}) if _name == "Stop" else {}
    if tr:
        tr["wait"] = 1 if wait_user else 0
    # 起因和这轮要拍板的事对不上（模型判「无关」，比如用户只回了「好的」）就不带起因：
    # 宁可没有背景，也别塞一句会误导人的「起因：好的」。
    cause_send = cause_out
    if cause_send and _cause_weak(cause_send):
        print("[cause] %s 起因只是应答词，丢掉 sid=%s"
              % (time.strftime("%m-%d %H:%M:%S"), str(sid)[:8]), flush=True)
        cause_send = ""
    elif cause_send and act_wait and _rp.get("cause_ok") is not True:
        # 有待确认事项时，起因必须被模型确认过是它的来由；判无关/没给判断（超时）
        # 都不带——免得「起因」和「要确认的事」是两件不相干的事。
        print("[cause] %s 起因与待确认事项对不上，丢掉 sid=%s"
              % (time.strftime("%m-%d %H:%M:%S"), str(sid)[:8]), flush=True)
        cause_send = ""
    # 这一批事件里只要有一条「done」，这轮就算跑完了（不管下面会不会被改报 perm）。
    round_end = any((x.get("kind") or "") == "done" for x in (items or []))
    # 结果核验：完成/失败要有证据（测试通过、退出码、权限被拒），模型说「好了」不算。
    _oc = (_outcome_of(sid, d)
           if (_name == "Stop" and round_end and d.get("session_id")) else None)
    if _oc:
        print("[outcome] %s %s sid=%s %s"
              % (time.strftime("%m-%d %H:%M:%S"), _oc.get("state"),
                 str(sid)[:8], (_oc.get("label") or "")[:60]), flush=True)
        # agent 自己跑过的测试，不用人再点：落一张红绿卡进测试记录
        try:
            if _record_agent_run(sid, _oc):
                print("[test] %s 自动记卡 sid=%s %s"
                      % (time.strftime("%m-%d %H:%M:%S"), str(sid)[:8],
                         (_oc.get("cmd") or "")[:60]), flush=True)
        except Exception as _e:
            print("[test] 自动记卡失败：%r" % (_e,), flush=True)
    # 中继 _np_eval 只回 kind/text/sound/voice，权限请求号是本机生成的：按顺序补回去，
    # 否则走中继的那条提醒到手机上就没有 rid，点完选项也没法回传。
    _rids = [x.get("request_id") for x in (local or [])
             if x.get("kind") == "perm" and x.get("request_id")]
    for it0 in items or []:
        it = it0
        if (it.get("kind") == "perm" and not it.get("request_id") and _rids):
            it = dict(it, request_id=_rids.pop(0))
        if wait_user and it.get("kind") == "done":
            tr["k"] = "perm"
            print("[na] %s 这轮其实在等用户 → 完成改报「需要确认」 sid=%s（%s）"
                  % (time.strftime("%m-%d %H:%M:%S"), str(sid)[:8],
                     "模型概括带待确认事项" if act_wait else "na 判断 YES"), flush=True)
            # kind_rule/sound_rule：本机智能提醒关着的手机要按规则结果提示
            # （智能判断是「别人的开关」换来的，不能把结论也塞给它）
            it = dict(it, kind="perm", round_end=True, kind_rule="done",
                      sound=(pol.get("sound") or {}).get("perm") or "alert",
                      sound_rule=(pol.get("sound") or {}).get("done") or "done",
                      voice=bool((pol.get("voice") or {}).get("perm", True)))
        # 证据结论（✅完成/❌失败/⛔被拒）放在提醒最前面；改报 perm 的那条不加
        if _oc and it.get("kind") == "done":
            it = dict(it, text=_oc_text(_oc, it.get("text") or ""))
        # 改报 perm 的这条也是「跑完」的落点：Stop 和 agent_completed 挨着来时同样要去重，
        # 否则同一轮会推两条需要确认。
        if it.get("kind") == "done" or it.get("round_end"):
            if time.time() - _DONE_SENT.get(sid, 0) < 10:   # Stop + agent_completed 去重
                continue
            _DONE_SENT[sid] = time.time()
        _nev = _notify_add(sid, it.get("kind"), it.get("text"), d.get("cwd") or "",
                           title=it.get("title") or title, src=src,
                           sound=it.get("sound"), voice=it.get("voice"),
                           kind_rule=it.get("kind_rule") or "",
                           sound_rule=it.get("sound_rule") or "",
                           about=ab_out if it.get("kind") in _ABOUT_KINDS else "",
                           reply=(_rp.get("summary") or "") if it.get("kind") in _ABOUT_KINDS else "",
                           act=(_rp.get("act") or "") if it.get("kind") in _ABOUT_KINDS else "",
                           cause=cause_send, round_end=bool(it.get("round_end")),
                           rid=it.get("request_id") or "",
                           ask=it.get("ask"), risk=it.get("risk"),
                           why=it.get("why") or "",
                           trace=(tr if (it.get("round_end") or it.get("kind") == "done") else None))
        # 2.5 秒没等到概括（模型慢/刚好赶上忙）：这条历史先落的兜底句，等任务算完补写
        if (rp_task is not None and not rp_out and (_nev or {}).get("nh")
                and it.get("kind") in _ABOUT_KINDS):
            asyncio.ensure_future(_nh_fill(_nev["nh"], rp_task,
                                           ab_out if it.get("kind") in _ABOUT_KINDS else ""))
        if it.get("kind") in _BLOCKING and _name not in ("UserPromptSubmit", "Stop"):
            _block_set(sid, it, title, d.get("cwd") or "", src)


_STUCK = {}          # sid -> 已提醒过的那次转录 mtime（同一「无输出点」只报一次）


def _stuck_scan(pol, now, sess):
    """跑着（busy）但转录 N 分钟没新增 → 可能卡住。等确认/等输入的阻塞提醒在追的会话跳过。"""
    out = []
    kinds = pol.get("kinds") or {}
    if not pol.get("enabled", True) or not kinds.get("stuck", True):
        return out
    try:
        mins = float(pol.get("stuck_min") or 0)
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        return out
    for s in sess or []:
        sid = s.get("sid") or ""
        if not sid:
            continue
        if s.get("state") != "busy" or sid in _BLOCK:
            _STUCK.pop(sid, None)
            continue
        p = (agents.get(s.get("agent")) or agents.default()).transcript_path(
            s.get("cwd") or "", sid)
        try:
            mt = p.stat().st_mtime if p else 0
        except OSError:
            mt = 0
        if not mt or now - mt < mins * 60:
            _STUCK.pop(sid, None)
            continue
        if _STUCK.get(sid) == mt:
            continue
        _STUCK[sid] = mt
        title = s.get("title") or sid[:8]
        out.append({"sid": sid, "title": title, "cwd": s.get("cwd") or "",
                    "text": _np_fmt(pol.get("stuck_text") or "{title} 已 {min} 分钟没有输出，可能卡住了",
                                    title=title, min=int(mins))})
    return out


async def _stuck_loop(app):
    """每 60s 巡一次：busy 且转录长时间不动 → 提醒一次（不持续追）。"""
    await asyncio.sleep(60)
    loop = asyncio.get_event_loop()
    while True:
        try:
            pol = _np_get()
            if (pol.get("kinds") or {}).get("stuck", True):
                sess = await loop.run_in_executor(None, discover)
                for it in _stuck_scan(pol, time.time(), sess):
                    _notify_add(it["sid"], "stuck", it["text"], it["cwd"],
                                title=it["title"], src="local",
                                sound=(pol.get("sound") or {}).get("stuck") or "ping",
                                voice=bool((pol.get("voice") or {}).get("stuck")),
                                cause=_cause_for(it["sid"]))
        except Exception as e:
            print("[stuck] error:", str(e)[:120], flush=True)
        await asyncio.sleep(60)


async def _block_loop():
    """持续提醒：阻塞类提醒没被处理就按递增间隔再发一次（不带语音，别太吵）。"""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for sid, b in list(_BLOCK.items()):
            if now < b.get("next", 0):
                continue
            b["n"] = int(b.get("n") or 0) + 1
            b["next"] = now + _BLOCK_STEPS[min(b["n"], len(_BLOCK_STEPS) - 1)]
            _notify_add(sid, b["kind"], b["text"], b.get("cwd") or "",
                        title="仍未处理 · " + (b.get("title") or ""),
                        src=b.get("src") or "local", sound=b.get("sound"),
                        voice=False, rep=b["n"],
                        kind_rule=b.get("kind_rule") or "",
                        sound_rule=b.get("sound_rule") or "",
                        about=(_about_for(sid)
                               if (_voice_ok() and b.get("kind") in _ABOUT_KINDS) else ""),
                        cause=_cause_for(sid), rid=b.get("rid") or "",
                        ask=b.get("ask"), risk=b.get("risk"),
                        why=b.get("why") or "")


async def _np_on_startup(app):
    _np_load_disk()
    app["np_task"] = asyncio.create_task(_np_loop())
    app["block_task"] = asyncio.create_task(_block_loop())
    app["stuck_task"] = asyncio.create_task(_stuck_loop(app))


async def h_session_event(request):
    """Claude Code hook 入口（本机回环，hook.sh 自带 Cookie）。
    业务逻辑/策略在中继：这里只转发原始事件 + 兜底计算，然后立刻返回。
    永远不输出决策：PermissionRequest 里输出任何 decision 都等于替用户点了允许。"""
    await require_token(request)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    if isinstance(d, dict) and d.get("type") == "agent-turn-complete":
        # Codex 的跑完提醒（config.toml 的 notify）长这样，翻成 Claude 的 Stop 之后
        # 提醒/语音/中继那条链路一整套复用，不用给 Codex 再写一遍。
        d = agent_codex.notify_event(d)
        if not d:
            return web.Response(status=204)
    elif isinstance(d, dict) and d.get("type") == agent_opencode.SESSION_EVENT_TYPE:
        # opencode 插件（~/.config/opencode/plugins/agentboss-notify.js）的跑完提醒，
        # 同一条链路再复用一次。
        d = agent_opencode.notify_event(d)
        if not d:
            return web.Response(status=204)
    elif isinstance(d, dict) and (d.get("type") == agent_hermes.SESSION_EVENT_TYPE
                                  or d.get("hook_event_name") == agent_hermes.SESSION_HOOK_EVENT):
        # hermes shell hook（config.yaml hooks: → hook.sh hermes，stdin 原样转发），
        # 同一条链路再复用一次。
        d = agent_hermes.notify_event(d)
        if not d:
            return web.Response(status=204)
    sid = d.get("session_id") or ""
    if not sid:
        return web.Response(status=204)
    title = _sid_title(sid)
    try:
        # 线程里做本地增强（决定卡含 agent 二审，最多 8s）：别卡事件循环
        local = await asyncio.to_thread(_ev_local, d)
    except Exception:
        local = []
    asyncio.create_task(_ev_remote(d, sid, title, local))
    return web.Response(status=204)


def _sse(ev, obj):
    return ("event: %s\ndata: %s\n\n" % (
        ev, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))).encode("utf-8")


def _acp_rows():
    """ACP 协议源窗口行：活窗口 + 重启后的鬼影。列表两路（pull/push）共用——
    SSE 推送是手机端列表的主通道，哪路缺了 ACP 行，另一路给的「协议」徽标
    几秒内就会被推送冲掉。"""
    out = [dict(sid=w.sid, short=w.sid[:8], kind="computer", pid=None,
                cwd=w.cwd, title=w.title, window_id=None, tty="",
                state=w.state, last=w.last_at, agent="gemini", acp=1)
           for w in agent_acp.windows()]
    out.extend(agent_acp.ghosts())
    return out


def _push_sessions_payload(face=None):
    """同 h_ext_sessions 的响应体，但只吃 discover() 2s 热缓存——
    discover(force=True) 是阻塞 subprocess（最长 15s），绝不能进 SSE 循环。
    face 给定则按产品线过滤（每连接一次，SSE 循环里按客户端产品线裁剪）。"""
    out = []
    mstate = _model_all()
    mm = _machine_model()
    seen_proj = set()
    mdflt = {}          # agent key -> 该 agent 的默认模型名（每个 agent 每轮只算一次）
    for x in _cached_sessions():
        ad = agents.get(x.get("agent"))
        p = ad.transcript_path(x.get("cwd", ""), x["sid"]) if ad else None
        mtime = 0
        try:
            if p:
                mtime = p.stat().st_mtime
        except OSError:
            pass
        proj = _sess_proj(x, seen_proj)
        akey = x.get("agent") or agents.DEFAULT_AGENT
        if akey not in mdflt:
            mdflt[akey] = (ad.model_info() if ad else "") or mm["model"]
        mdl = mstate.get(x["sid"]) or mdflt[akey]
        host = bool(x.get("host"))          # Windows 上 host 自管 ConPTY 的会话
        meta = None if host else _meta_for_pid(x.get("pid"))
        if meta and meta.get("term"):
            _term_note(x["sid"], meta["term"])   # 关掉后召回/换模型还要回到原终端
        link, caps, why = _link_caps(x, ad, host, meta)
        out.append(dict(x, kind="computer", last=mtime or x.get("last") or 0,
                        full_color=meta is not None,
                        host=host,
                        link=link, caps=sorted(caps), why=why,
                        term="Codeboss" if host else
                             ("Codex 桌面版" if x.get("desktop") else
                              (meta or {}).get("term") or plat.term_label(x.get("tty"), x.get("window_id"))),
                        model=mdl,
                        proj=proj, agent=x.get("agent") or "claude"))
    for c in _closed_sessions({x["sid"] for x in out}):
        c["proj"] = _sess_proj(c, seen_proj)
        out.append(c)
    # ACP 行 + 来源标注：与 h_ext_sessions 同一套（别让推送把「协议」徽标冲掉）
    out.extend(_acp_rows())
    for r in out:
        r.setdefault("source", window_source.source_of(r["sid"], r))
    return {"sessions": _face_only(out, face)}


def _msg_diff(prev, cur):
    """消息前缀 diff：cur[:k] 与 prev 完全一致。返回 (k, tail)。
    流式典型形态：末条气泡增长/追加 1-2 条 → k ≥ len(prev)-1，发尾部即可；
    窗口截断位移（120 条上限）/列表收缩 → k=0，发全量。"""
    if prev is None:
        return 0, cur
    n = min(len(prev), len(cur))
    k = 0
    while k < n and prev[k] == cur[k]:
        k += 1
    if len(cur) >= len(prev) and k >= len(prev) - 1:
        return k, cur[k:]
    return 0, cur


_DRAFTS = {}   # sid -> {text, done, err, waiting}：桌面版这一轮（SSE 每 tick 看变化）
# waiting：话已经交给桌面版 App，这一轮由它来跑——它只把成品写盘，手机端拿不到增量，
# 先挂个「正在处理」占位，等转录里冒出新消息再撤。真流式见 codex_daemon。


def _draft_begin(sid, waiting=False):
    _DRAFTS[sid] = {"text": "", "done": False, "err": "", "at": time.time(),
                    "waiting": waiting, "st": "正在处理" if waiting else "",
                    "t0": time.time()}
    if waiting:
        try:
            asyncio.get_event_loop().call_later(900, _draft_expire, sid)
        except Exception:
            pass


def _draft_expire(sid):
    d = _DRAFTS.get(sid)
    if d is not None and d.get("waiting"):
        _DRAFTS.pop(sid, None)


_WATCHED = {}   # sid -> at：正在旁观共享内核里 App 自己跑的那一轮（rejoin 拿增量）


def _watch_running(sid):
    """桌面版 App 正在跑这一轮，而且两边是同一个内核：挂上旁观，逐字推给手机。
    内核不支持 rejoin / 这一轮不在共享内核里时静默放弃（还是等整段消息）。"""
    if sid in _WATCHED or sid in _DRAFTS:
        return
    _draft_begin(sid)   # 起表：这一轮是 App 自己跑的，计时从挂上旁观算起

    def done(status, text):
        _WATCHED.pop(sid, None)
        if status == "ok" and text:
            _draft_finish(sid, "ok", text)
        else:
            _DRAFTS.pop(sid, None)

    async def go():
        ok, err = await codex_daemon.watch_turn(
            sid, lambda t: _draft_delta(sid, t), done,
            lambda st: _draft_stat(sid, st))
        if not ok:
            _WATCHED.pop(sid, None)
            _DRAFTS.pop(sid, None)   # 刚起的表要撤，不然这个 sid 以后再也挂不上旁观
            print("[watch] sid=%s 旁观这一轮失败（%s）"
                  % (sid[:8], (err or "")[:80]), flush=True)

    _WATCHED[sid] = time.time()
    asyncio.ensure_future(go())


def _draft_delta(sid, text):
    d = _DRAFTS.get(sid)
    if d is None:
        _draft_begin(sid)
        d = _DRAFTS[sid]
    d["text"] = text
    d["waiting"] = False
    d["st"] = "正在输出"
    d["at"] = time.time()


def _draft_stat(sid, st):
    """这一轮在思考/跑命令/改文件…：状态行文字，「用时」由手机端自己走表。"""
    d = _DRAFTS.get(sid)
    if d is None:
        _draft_begin(sid)
        d = _DRAFTS[sid]
    d["st"] = st
    d["waiting"] = False
    d["at"] = time.time()


def _draft_finish(sid, status, text):
    d = _DRAFTS.get(sid) or {"text": "", "done": False, "err": "", "at": time.time()}
    _DRAFTS[sid] = d
    d["text"] = text or d.get("text") or ""
    d["done"] = True
    d["waiting"] = False
    d["st"] = ""
    d["err"] = "" if status == "ok" else status
    # 整段消息紧跟着会从转录里推过来；草稿留几秒再撤，别让两层气泡闪一下
    try:
        asyncio.get_event_loop().call_later(4, _DRAFTS.pop, sid, None)
    except Exception:
        pass   # 没有事件循环（测试里）：留着 done 态也没事，下次 _draft_begin 覆盖


def _draft_frame(sid, now=None):
    """这一轮的草稿帧 + 变更签名（手机端靠它显示/清掉「正在思考」）。
    没有草稿了（这一轮早就结束、_DRAFTS 已撤）**也要发一帧 done**：否则断线重连
    后的手机永远停在那句「正在思考」上，自己走的表一直加（用户：codex 早就跑完了，
    app 还显示正在思考 65 分钟）。"""
    now = now or time.time()
    dr = _DRAFTS.get(sid)
    dn = dr is None
    sig = None if dn else (len(dr.get("text") or ""), bool(dr.get("done")),
                           dr.get("st") or "")
    ev = {"text": "" if (dn or dr.get("done")) else (dr.get("text") or ""),
          "done": bool(dn or dr.get("done")),
          "waiting": bool((not dn) and dr.get("waiting")),
          "st": (dr or {}).get("st") or "",
          "el": int(max(0, now - ((dr or {}).get("t0") or now))),
          "err": (dr or {}).get("err") or ""}
    return sig, ev


async def _push_msg_ev(s, sid, c, prev_none=False):
    """解析 transcript 并与上次比对，返回 msg 事件 dict（全量或 inc 尾巴）"""
    ad = agents.get(s.get("agent")) or agents.default()
    p = ad.transcript_path(s.get("cwd", ""), sid) if ad else None
    # opencode/hermes 的 parse 是最长 15s 的 export 子进程——和 discover 同一守则，
    # 绝不能在事件循环线程里跑（否则全端心跳断流），进 executor。
    cur = (await asyncio.get_event_loop().run_in_executor(
        None, ad.parse_transcript, p, 400)) if (ad and p) else []
    prev = None if prev_none else c["msg_prev"]
    c["msg_prev"] = cur
    base = {"total": len(cur), "state": s["state"],
            "full_color": _meta_for_pid(s.get("pid")) is not None}
    # 推送模式下手机端只吃这个事件：桌面版标记必须带上，否则不会切到对话视图
    if s.get("desktop"):
        dcap, dwhy = _desk_caps(s)
        base.update(desktop=True, caps=sorted(dcap), why=dwhy)
    if prev is None:
        return dict(base, messages=cur, inc=0)
    k, tail = _msg_diff(prev, cur)
    if k > 0 and not tail:
        # rollout 长了（工具输出/思考），解析出的消息表没变。别推空尾巴：
        # 手机端本地可能刚垫了一条自己发的消息，按 inc_from 截断会把它截掉。
        return None
    if k > 0:
        _dr = _DRAFTS.get(sid)
        if _dr is not None and _dr.get("waiting"):
            _draft_finish(sid, "ok", "")   # 桌面版答完了：撤「正在处理」占位
        return dict(base, messages=tail, inc=1, inc_from=k)
    return dict(base, messages=cur, inc=0)


def _push_screen_ev(s, ok, txt):
    lines = txt.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return {"ok": ok, "screen": "\n".join(lines).replace("⏺", "●").replace("⏵", "▸"), "state": s["state"]}


async def h_app_rpc(request):
    """桌面版 App 连共享内核的通道：原样转发 websocket 帧。

    App 默认自己拉一套 codex 内核（会话写入权在它手里，只把成品落盘 → 手机端永远
    整段出现）。把 App 指过来（CODEX_APP_SERVER_WS_URL=ws://127.0.0.1:PORT/rpc）
    之后，App 和 host 是同一个内核：谁跑的一轮，另一方都能实时看到增量。
    只在本机回环放行——这是 codex 内核的完整控制通道。
    """
    if request.remote not in ("127.0.0.1", "::1"):
        return web.json_response({"error": "local only"}, status=403)
    loop = asyncio.get_event_loop()
    sp = codex_daemon.shared_sock() or await loop.run_in_executor(
        None, codex_daemon.ensure_shared_server)
    if not sp:
        return web.json_response({"error": "no engine"}, status=503)
    import aiohttp
    ws = web.WebSocketResponse(compress=False, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    sess = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=sp))
    try:
        up = await sess.ws_connect("http://localhost/", compress=0,
                                   max_msg_size=64 * 1024 * 1024)
    except Exception:
        await sess.close()
        await ws.close()
        return ws
    _APP_RPC[0] += 1
    _APP_RPC[1] = True
    print("[share] App 已连上共享内核", flush=True)

    async def pump(src, dst):
        try:
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                else:
                    break
        except Exception:
            pass
        finally:
            try:
                await dst.close()
            except Exception:
                pass

    await asyncio.gather(pump(ws, up), pump(up, ws))
    await sess.close()
    _APP_RPC[1] = False
    print("[share] App 断开共享内核", flush=True)
    asyncio.ensure_future(_app_left_check(_APP_RPC[0], loop))
    return ws


_APP_RPC = [0, False]   # [连接代次, 现在是否连着]：App 掉线后等一会儿看它回不回来


async def _app_left_check(gen, loop):
    """App 和共享内核的通道断了：不是用户关掉的话，说明它又自己拉了一套内核
    （会话写入权被它抢走，手机端拿不到增量）。等 8 秒没回来就自愈重启，
    重启前会检查有没有会话在跑（见 codex_daemon.app_restart_if_idle）。"""
    await asyncio.sleep(8)
    if _APP_RPC[1] or _APP_RPC[0] != gen:
        return
    ok, why = await loop.run_in_executor(None, codex_daemon.app_restart_if_idle,
                                         "App 没连上共享内核")
    print("[heal] App 掉线自愈：%s（%s）" % (ok, why), flush=True)


async def h_ext_push(request):
    """SSE 推送端点。sid 可空＝只推会话列表；每连接独立 350ms tick，只推变更。"""
    await require_token(request)
    if not _CFG.get("ext_push", True):
        return web.json_response({"error": "push disabled"}, status=503)
    if _push_conns[0] >= PUSH_MAX_CONN:
        print("[push] 拒绝连接：已达上限 %d" % PUSH_MAX_CONN, flush=True)
        return web.json_response({"error": "push busy"}, status=503)
    sid = request.match_info.get("sid", "")
    hdrs = {"Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no"}
    if request.get("cors"):   # P2P 直连跨源（_cors_mw 预先算好放行来源）
        hdrs.update(request["cors"])
    resp = web.StreamResponse(headers=hdrs)
    # SSE 绝不压缩（gz=1 的老客户端也忽略）：aiohttp 的 zlib 在每次 write() 后
    # 不 flush（只有 write_eof 才 flush），压缩流会把 hello/15s 心跳全憋在缓冲里
    # ——客户端一个字节收不到，22-30s 后被看门狗掐掉 =「推送中断已回轮询」无限循环。
    # 带宽交给中继边缘压（host↔relay 是 WS 帧隧道，不经 HTTP 压缩）。
    t0 = time.time()          # prepare 也可能抛（连接中途断开），所以放最前：
    last_hb = t0              # finally 的收尾日志引用 t0/last_hb，晚赋值 = UnboundLocalError
    try:
        await resp.prepare(request)
    except Exception:
        return resp           # 头都没发出去，连接已死，直接收尾
    _push_conns[0] += 1
    _LISTEN_AT[0] = time.time()
    loop = asyncio.get_event_loop()
    cancel = asyncio.Event()
    rid = request.headers.get("X-CB-Stream-Id") or ""
    if rid:
        _PUSH_STREAMS[rid] = cancel
    c = {"sess": None, "sess_at": 0.0, "state": None,
         "live_html": None, "live_at": 0.0, "hist_n": 0, "hist_html": None,
         "hist_at": 0.0, "hist_dirty": False,
         "msg_sig": None, "msg_prev": None, "msg_at": 0.0, "draft_sig": None,
         "scr_sig": None, "scr_at": 0.0, "notify_id": 0}

    def feed(sig):
        """日志增量信号：size 变了置脏（轮询里 hist/live 都从它派生）"""
        try:
            size = Path(sig).stat().st_size
        except OSError:
            return False
        if size != c["size"]:
            c["size"] = size
            return True
        return False

    async def send(ev, obj):
        obj["ts"] = time.time()   # 端到端延迟测量用（服务端时刻随事件下发）
        _pl = _sse(ev, obj)
        _p2p_note_lan(request, len(_pl))
        await resp.write(_pl)

    try:
        await send("hello", {"proto": 1, "sid": sid, "seq": _NOTIFY_SEQ[0]})
        _tb = time.time()
        _face = _face_for(request)   # 本连接的产品线能力面
        sess = _push_sessions_payload(_face)
        c["sess"] = json.dumps(sess, ensure_ascii=False, sort_keys=True)
        c["sess_at"] = time.time()
        await send("sessions", sess)
        _tsess = time.time()
        # 已退出会话也要发 state/msg（否则 app 点开已退出会话只收到 gone，打不开）；
        # 活会话照样缓存直出，零额外开销
        s = await _find_session(sid) if sid else None
        c["state"] = (s["state"] if s else "gone") if sid else None
        if sid:
            await send("state", {"state": c["state"], "gone": s is None})
        if s:
            await send("msg", await _push_msg_ev(s, sid, c, prev_none=True))
            # 草稿帧每条连接都要发一帧（哪怕这一轮早就结束、_DRAFTS 已经撤了）：
            # 手机端断线重连后只靠这一帧清状态，没有它就一直挂着上一轮的「正在思考」。
            c["draft_sig"], _dev = _draft_frame(sid)
            await send("draft", _dev)
            _tmsg = time.time()
            meta = _meta_for_pid(s.get("pid"))
            if meta:
                got = await _live_render(sid, meta, s.get("agent") or "")
                _tlive = time.time()
            else:
                ok, txt = await loop.run_in_executor(None, ext_screen, s)
                _tlive = time.time()
            # 进会话「很慢」就看这一行：sess=列表首包、msg=历史消息、live=全彩镜像
            # 首帧；哪段几百毫秒以上就是那一段的锅。
            print("[pushboot] sid=%s sess=%.0fms msg=%.0fms live=%.0fms meta=%s"
                  % ((sid or "")[:8], (_tsess - _tb) * 1000, (_tmsg - _tsess) * 1000,
                     (_tlive - _tmsg) * 1000, "y" if meta else "n"), flush=True)
            if meta:
                if got:
                    html, hist_rows, cols, rows, hn = got
                    c["live_html"], c["live_at"] = html, time.time()
                    await send("live", {"ok": 1, "html": html, "cols": cols, "rows": rows})
                    c["hist_n"], c["hist_html"] = hn, hist_rows
                    c["hist_at"] = time.time()
                    await send("hist", {"ok": 1, "hist": hist_rows, "cols": cols, "rows": rows})
                    try:
                        c["size"] = Path(meta["log"]).stat().st_size
                    except OSError:
                        c["size"] = 0
            else:
                c["scr_sig"] = (ok, txt)
                c["scr_at"] = time.time()
                await send("screen", _push_screen_ev(s, ok, txt))
        # ---- tick 主循环：变更才组事件，每 tick 聚帧一次 write ----
        while not cancel.is_set():
            await asyncio.sleep(PUSH_TICK)
            now = time.time()
            blobs = []
            if now - c["sess_at"] >= PUSH_SESS_GAP:
                c["sess_at"] = now
                sess = _push_sessions_payload(_face)
                j = json.dumps(sess, ensure_ascii=False, sort_keys=True)
                if j != c["sess"]:
                    c["sess"] = j
                    sess["ts"] = now
                    blobs.append(_sse("sessions", sess))
            if sid:
                # 只读 2s 热缓存，不跑子进程——SSE 循环里任何阻塞都会冻住整条事件循环
                # （推送断流 + 中继心跳饿死 → 开发机离线）。
                s = next((x for x in _cached_sessions() if x["sid"] == sid), None)
                stt = s["state"] if s else "gone"
                if stt != c["state"]:
                    c["state"] = stt
                    blobs.append(_sse("state", {"state": stt, "gone": s is None, "ts": now}))
                if s:
                    if s.get("desktop") and stt == "busy":
                        _watch_running(sid)   # App 自己跑的一轮也要逐字到手机
                    meta = _meta_for_pid(s.get("pid"))
                    if meta:
                        if feed(meta["log"]):
                            c["dirty"] = True
                        if c.get("dirty") and now - c["live_at"] >= PUSH_LIVE_GAP:
                            c["dirty"] = False
                            got = await _live_render(sid, meta, s.get("agent") or "")
                            if got:
                                html, hist_rows, cols, rows, hn = got
                                if html != c["live_html"]:
                                    c["live_html"] = html
                                    c["live_at"] = now
                                    blobs.append(_sse("live", {"ok": 1, "html": html,
                                                               "cols": cols,
                                                               "rows": rows, "ts": now}))
                                # 用重排结果判变化：hist 满了 800 行以后长度不再增长，
                                # 只比行数会导致历史区永远不再推送（实时比原样少内容）
                                if hist_rows != c.get("hist_html"):
                                    c["hist_n"], c["hist_html"] = hn, hist_rows
                                    c["hist_dirty"] = True
                                if c["hist_dirty"] and now - c["hist_at"] >= PUSH_HIST_GAP:
                                    c["hist_dirty"] = False
                                    c["hist_at"] = now
                                    blobs.append(_sse("hist", {"ok": 1,
                                                               "hist": hist_rows,
                                                               "cols": cols,
                                                               "rows": rows, "ts": now}))
                    else:
                        if now - c["scr_at"] >= PUSH_SCR_GAP:
                            c["scr_at"] = now
                            ok, txt = await loop.run_in_executor(None, ext_screen, s)
                            if (ok, txt) != c["scr_sig"]:
                                c["scr_sig"] = (ok, txt)
                                ev = _push_screen_ev(s, ok, txt)
                                ev["ts"] = now
                                blobs.append(_sse("screen", ev))
                    ad = agents.get(s.get("agent"))
                    p = ad.transcript_path(s.get("cwd", ""), sid) if ad else None
                    msig = None
                    if p:
                        try:
                            pst = p.stat()
                            msig = (pst.st_mtime, pst.st_size)
                        except (OSError, AttributeError):
                            pass   # AttributeError：适配器 Ref 替身缺属性，别崩 SSE 连接
                    if msig != c["msg_sig"] and now - c["msg_at"] >= PUSH_MSG_GAP:
                        c["msg_sig"] = msig
                        c["msg_at"] = now
                        _ev = await _push_msg_ev(s, sid, c)
                        if _ev is not None:
                            blobs.append(_sse("msg", _ev))
                    dsig, _dev = _draft_frame(sid, now)
                    if dsig != c.get("draft_sig"):
                        c["draft_sig"] = dsig
                        _dev["ts"] = now
                        blobs.append(_sse("draft", _dev))
            if _NOTIFY_SEQ[0] > c["notify_id"]:
                for e in _NOTIFY_Q:
                    if e["id"] > c["notify_id"] and now - e["ts"] < _NOTIFY_TTL:
                        blobs.append(_sse("notify", e))
                c["notify_id"] = _NOTIFY_SEQ[0]
            if now - last_hb >= PUSH_HB:
                last_hb = now
                blobs.append(_sse("hb", {"t": int(now)}))   # 真事件：EventSource 靠它保活，sseOpen 端 pushFeed 直接忽略
            if blobs:
                _bl = b"".join(blobs)
                _p2p_note_lan(request, len(_bl))
                await resp.write(_bl)
            if now - t0 >= PUSH_MAX_LIFE:
                await send("bye", {})
                break
    except (asyncio.CancelledError, ConnectionResetError, OSError):
        pass
    except Exception:
        try:
            await resp.write(_sse("err", {"why": "internal"}))
        except Exception:
            pass
    finally:
        _push_conns[0] -= 1
        print("[push] %s 连接结束 rid=%s sid=%s 在线=%d 时长=%.1fs"
              % (time.strftime("%m-%d %H:%M:%S"), rid or "-", sid[:8], _push_conns[0],
                 time.time() - t0), flush=True)
        if rid:
            _PUSH_STREAMS.pop(rid, None)
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def h_bg_push(request):
    """后台提醒专用 SSE（安卓前台服务用）：只推 notify + hb，不做任何渲染/发现。
    与 /api/ext_push 分开是两件事：
    1) 便宜——黑屏时不需要会话/终端内容，别把整包 sessions 和 live 渲染推过去；
    2) 中继对 /api/bg_push 豁免「远程使用时长」（relay.tunnel）——提醒不算在用。
    since=上次收到的 id，重连补发（10 分钟 TTL 内）；不带 since 从当前开始。"""
    await require_token(request)
    if not _CFG.get("ext_push", True):
        return web.json_response({"error": "push disabled"}, status=503)
    try:
        since = int(request.query.get("since") or -1)
    except ValueError:
        since = -1
    hdrs = {"Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no"}
    if request.get("cors"):
        hdrs.update(request["cors"])
    resp = web.StreamResponse(headers=hdrs)
    t0 = time.time()
    try:
        await resp.prepare(request)
    except Exception:
        return resp
    cancel = asyncio.Event()
    rid = request.headers.get("X-CB-Stream-Id") or ""
    if rid:
        _PUSH_STREAMS[rid] = cancel
    _bg_conns[0] += 1
    _LISTEN_AT[0] = time.time()
    # 这条流以前一行日志都没有：手机上报「提醒全哑」时，主机这边看不出壳到底连没连上
    # （2026-11-19 排查现场就是黑盒）。连上和断开各打一行，认证失败另有 [err] 403。
    print("[bg] 连上 ip=%s since=%s ua=%s" % (request.remote or "?", since,
          (request.headers.get("User-Agent") or "")[:60]), flush=True)
    # host 重启后序号归零：旧 since 比当前最大 id 还大 → 从当前开始，否则永远等不到
    cur = _NOTIFY_SEQ[0] if (since < 0 or since > _NOTIFY_SEQ[0]) else since
    last_hb = 0.0
    try:
        _pl = _sse("hello", {"proto": 1, "seq": _NOTIFY_SEQ[0]})
        _p2p_note_lan(request, len(_pl))
        await resp.write(_pl)
        while not cancel.is_set():
            now = time.time()
            blobs = []
            if _NOTIFY_SEQ[0] > cur:
                for e in _NOTIFY_Q:
                    if e["id"] > cur and now - e["ts"] < _NOTIFY_TTL:
                        blobs.append(_sse("notify", e))
                cur = _NOTIFY_SEQ[0]
            if now - last_hb >= PUSH_HB:
                last_hb = now
                blobs.append(_sse("hb", {"t": int(now)}))
            if blobs:
                _bl = b"".join(blobs)
                _p2p_note_lan(request, len(_bl))
                await resp.write(_bl)
            await asyncio.sleep(0.5)
            if now - t0 >= PUSH_MAX_LIFE:
                await resp.write(_sse("bye", {}))
                break
    except (asyncio.CancelledError, ConnectionResetError, OSError):
        pass
    except Exception:
        try:
            await resp.write(_sse("err", {"why": "internal"}))
        except Exception:
            pass
    finally:
        _bg_conns[0] -= 1
        print("[bg] 断开 ip=%s 时长=%.0fs" % (request.remote or "?", time.time() - t0),
              flush=True)
        if rid:
            _PUSH_STREAMS.pop(rid, None)
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def _push_sessions_loop(app):
    """后台 discover 供给：阻塞 subprocess 在 executor 里跑，SSE/轮询只吃 2s 热缓存。
    on_startup 句柄只 spawn 任务立即返回——handler 里 await 死循环会卡住整个站点启动。"""

    last = {"k": None}

    async def _run():
        loop = asyncio.get_event_loop()
        await asyncio.sleep(3)
        while True:
            try:
                d = await loop.run_in_executor(None, _discover, True)
                # 列表变了才打一行：「拉不到会话」时先看这里——是发现结果变了，还是手机端没拿到
                k = tuple(sorted(str(x.get("sid") or "") for x in d))
                if k != last["k"]:
                    last["k"] = k
                    _dbg("[disc] 会话 %d 条：%s" % (len(d), " ".join(
                        "%s/%s" % (str(x.get("sid") or "?")[:8], x.get("agent") or "claude")
                        for x in d) or "（无）"))
            except Exception:
                pass
            await asyncio.sleep(2.0)

    app["push_sessions_task"] = asyncio.create_task(_run())


# ---- relay host-link（规划-中继与多机账号.md v0.3）----
# 出站 WS 连 relay，零端口暴露：relay 把手机的 HTTP 请求打成 {"t":"req",id,m,p,headers,body(b64)}
# 帧发过来，这里翻译成本机 127.0.0.1:PORT 的真实 HTTP 调用（注入本机 token，手机侧
# 永不接触 token.txt），响应原样打回 {"t":"res"} 帧。断线指数退避重连（2s→120s）。

_LEGACY_RELAY_RE = re.compile(r"codeboss\.example\.top", re.I)

# 备用域名：同一个中继、同一张证书（SAN 里两个域名都在）。新域名解析/连接出问题时
# 临时切过来接着连；只放内存，不写回 config.json（下次启动还是先试新域名）。
RELAY_ALT_URL = "https://relay.example.com"
_RELAY_ALT = {"on": False}


def _relay_cfg():
    """读配置时也把旧域名换掉：_migrate_relay_cfg 只在进程启动时跑一次，
    服务还没重启（刚升级完）时 CLI/托盘读到的仍是旧地址。协议和端口原样保留。"""
    rc = _CFG.get("relay") or {}
    u = str(rc.get("url") or "")
    if _LEGACY_RELAY_RE.search(u):
        rc = dict(rc)
        rc["url"] = _LEGACY_RELAY_RE.sub(branding.RELAY_HOST_DEFAULT, u)
    if _RELAY_ALT["on"] and rc.get("url"):
        rc = dict(rc)
        rc["url"] = RELAY_ALT_URL
    return rc


# 链路实时状态（Mac 客户端读 /api/host_stat 显示，避免自己去探测中继导致状态闪烁）
# relay 会在客户端数量变化时推 {"t":"presence","n":N}；断链即归零。
_HOST_ST = {"relay": False, "clients": 0, "apps": 0, "webs": 0, "unbound": False}


def _relay_dev_call(path, extra, timeout=12):
    """以本机设备凭证调中继（设备自证接口），返回 relay 的 JSON dict。
    局域网直连的手机没有 relay 用户会话：host 代理 /r/* 设备管理请求，
    owner 由设备凭证隐含（架构：账号/设备归中继，会话内容才走直连）。"""
    import urllib.request
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id") or "", rc.get("device_secret") or ""
    if not (base and dev and sec):
        return {"ok": False, "error": "本机未登记中继（LAN 直连下无法管理设备）"}
    body = {"dev": dev, "sec": sec}
    body.update(extra or {})
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as e:
        return {"ok": False, "error": "中继不可达：%s" % str(e)[:80]}


def _osa_str(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def _tk_dialog(who, ttl):
    """本机弹窗问一句「让不让这个账号把本机接走」。True 允许 / False 明确拒绝 / None 没表态。

    默认按钮是「拒绝」；等不到表态（超时/关掉）返回 None 而不是 False——不能替用户拒绝，
    原账号手机 App 那条确认路还得留着。mac 用 osascript，Windows 用 PowerShell 弹窗。"""
    msg = ("账号「%s」想把这台电脑绑定到自己名下。\n\n"
           "同意后这台电脑会从你现在的账号里移走（机器上的会话和历史不动），"
           "原账号将无法再远程控制它。" % who)
    try:
        if IS_WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$r=[System.Windows.Forms.MessageBox]::Show('%s','Codeboss 绑定确认',"
                  "[System.Windows.Forms.MessageBoxButtons]::YesNo,"
                  "[System.Windows.Forms.MessageBoxIcon]::Warning,"
                  "[System.Windows.Forms.MessageBoxDefaultButton]::Button2);"
                  "if($r -eq 'Yes'){'yes'}else{'no'}"
                  % msg.replace("'", "''").replace("\n", "`n"))
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=ttl)
            return "yes" in (out.stdout or "").lower()
        if sys.platform != "darwin":
            return None
        out = subprocess.run(
            ["osascript", "-e",
             "display dialog %s with title \"Codeboss 绑定确认\" "
             "buttons {\"拒绝\", \"允许\"} default button \"拒绝\" with icon caution "
             "giving up after %d" % (_osa_str(msg), int(ttl))],
            capture_output=True, text=True, timeout=ttl + 15)
        s = out.stdout or ""
        if "gave up:true" in s:
            return None
        return "button returned:允许" in s
    except Exception as e:
        print("[takeover] 本机弹窗失败：%s" % str(e)[:100], flush=True)
        return None


async def _tk_ask(d):
    """中继推来「别的账号要接管本机」：本机弹窗，用户点了明确答复才回中继。"""
    rid = str(d.get("id") or "")
    if not rid:
        return
    who = str(d.get("who") or "另一个账号")
    ttl = int(d.get("ttl") or 0)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _tk_dialog, who, max(30, min(ttl, 600)))
    if ok is None:
        print("[takeover] 本机没表态（%s 想接管）→ 等原账号手机确认 rid=%s" % (who, rid), flush=True)
        return
    r = await loop.run_in_executor(
        None, _relay_dev_call, "/r/takeover_act", {"id": rid, "approve": 1 if ok else 0})
    print("[takeover] 本机%s接管 rid=%s → %s" % ("同意" if ok else "拒绝", rid,
                                            (r or {}).get("msg") or r), flush=True)


async def h_r_devices(request):
    await require_token(request)
    loop = asyncio.get_event_loop()
    d = await loop.run_in_executor(None, _relay_dev_call, "/r/dev_devices", {})
    # relay_base 无论 ok 与否都带上：页面在「未绑定账号」时也要知道中继地址（跳接入页/日志用）
    if isinstance(d, dict):
        d.setdefault("relay_base", (_relay_cfg().get("url") or "").rstrip("/"))
    return web.json_response(d)


async def h_r_pair(request):
    await require_token(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    extra = {"code": (data.get("code") or "").strip(),
             "takeover": bool(data.get("takeover"))}
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/dev_pair", extra))


async def h_r_pair_status(request):
    """新账号查接管进度：手机每隔几秒问一次，直到原账号/本机确认或 10 分钟作废。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/pair_status", {"id": str(data.get("id") or "")[:64]}))


async def h_r_takeover_list(request):
    """原账号：这台机器名下待确认的接管请求（手机轮询）。"""
    await require_token(request)
    loop = asyncio.get_event_loop()
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/takeover_list", {}))


async def h_r_takeover_act(request):
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/takeover_act",
        {"id": str(data.get("id") or "")[:64], "approve": 1 if data.get("approve") else 0}))


async def h_r_account_info(request):
    """账号信息 + 已登录客户端列表（设备自证代理到中继）。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    d = await loop.run_in_executor(None, _relay_dev_call, "/r/acct_info",
                                   {"cid": str(data.get("cid") or "")[:64]})
    return web.json_response(d)


async def h_r_account_send(request):
    """给「踢客户端」发二次验证码（短信/邮箱二选一）。"""
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    d = await loop.run_in_executor(None, _relay_dev_call, "/r/acct_send_code",
                                   {"cid": str(data.get("cid") or "")[:64],
                                    "via": str(data.get("via") or "")[:8]})
    return web.json_response(d)


async def h_r_account_kick(request):
    await require_token(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    loop = asyncio.get_event_loop()
    d = await loop.run_in_executor(None, _relay_dev_call, "/r/acct_kick",
                                   {"cid": str(data.get("cid") or "")[:64],
                                    "cur": str(data.get("cur") or "")[:64],
                                    "code": str(data.get("code") or "")[:16],
                                    "via": str(data.get("via") or "")[:8]})
    return web.json_response(d)


async def h_r_dev_rename(request):
    await require_token(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    extra = {"id": (data.get("id") or "").strip(),
             "name": (data.get("name") or "").strip()}
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/dev_rename_self", extra))


async def h_r_dev_del(request):
    await require_token(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    extra = {"id": (data.get("id") or "").strip()}
    return web.json_response(await loop.run_in_executor(
        None, _relay_dev_call, "/r/dev_del_self", extra))


async def h_api_term_new(request):
    """命令终端页的交互终端：开一个 pty 会话（zsh），页面 iframe 载入 /s/<sid>。"""
    await require_token(request)
    data = await request.json()
    cwd = (data.get("cwd") or "").strip() or str(Path.home())
    if not Path(cwd).is_dir():
        cwd = str(Path.home())
    cmd = (data.get("cmd") or "").strip()
    sid = uuid.uuid4().hex[:8]
    try:
        argv = _split_cmd(cmd) if cmd else [_user_shell()]
        SESSIONS[sid] = Session(sid, "终端 %s" % sid[:4], cwd, argv)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:120]})
    SESSIONS[sid].hidden = True   # 只进命令终端页的终端卡片，不进 🌐 会话列表
    save_registry()
    return web.json_response({"ok": True, "sid": sid})


def _term_tail(s, n=4):
    """终端卡片预览：取当前屏幕最后几行有内容的纯文本（不含 ANSI）。"""
    try:
        lines = [ln.rstrip() for ln in s.screen.display if ln.strip()]
        return [ln[-140:] for ln in lines[-n:]]
    except Exception:
        return []


async def h_api_terms(request):
    """列出命令终端页的终端卡片：手开的 zsh（hidden）+ ▶ 识别命令跑出来的运行终端。"""
    await require_token(request)
    out = [{"sid": s.sid, "name": s.name, "alive": bool(s.alive),
            "cwd": s.cwd, "started": s.started, "tail": _term_tail(s)}
           for s in SESSIONS.values()
           if getattr(s, "hidden", False) and not getattr(s, "ghost", False)]
    out.sort(key=lambda t: -t["started"])
    return web.json_response({"ok": True, "terms": out})


async def h_ext_term_out(request):
    """终端输出增量（长轮询）。中继只转普通 HTTP，浏览器↔中继的 WS 升级过不去，
    手机走中继时终端没画面 —— 改用 HTTP 拉字节：
      ?snap=1 → 取整屏快照 + 当前字节偏移
      ?c=<已收字节偏移> → 取增量（没新数据就最多挂 25 秒）"""
    await require_token(request)
    import base64 as _b64
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if not s or getattr(s, "ghost", False):
        return web.json_response({"ok": False, "error": "终端不存在"})
    try:
        c = int(request.query.get("c") or 0)
    except Exception:
        c = 0
    if request.query.get("snap") == "1":
        print("[term] snap sid=%s ua=%s c=%s" % (sid, request.headers.get("User-Agent", "")[:60], c),
              flush=True)
        snap = s.snapshot()      # 屏 + 偏移必须原子取：中间不能 await
        off = s.out_off()
        return _jsonz({"ok": True, "snap": _b64.b64encode(snap.encode()).decode(),
                       "c": off, "alive": bool(s.alive)})
    deadline = time.time() + 25
    while True:
        off = s.out_off()
        if c < s.out_start:      # 落后太多（缓冲被修剪）→ 让客户端重取整屏
            return _jsonz({"ok": True, "need_snap": 1, "c": s.out_start})
        if off > c:
            chunk = bytes(s.out_buf[c - s.out_start:off - s.out_start])
            return _jsonz({"ok": True, "d": _b64.b64encode(chunk).decode(),
                           "c": off, "alive": bool(s.alive)})
        if not s.alive:
            return _jsonz({"ok": True, "d": "", "c": off, "alive": False,
                           "exit": s.exit_code})
        if time.time() > deadline:
            return _jsonz({"ok": True, "d": "", "c": off, "alive": bool(s.alive)})
        ev = s.out_ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def h_ext_term_in(request):
    """终端输入：写字节进 pty；顺便带尺寸变更（中继无 WS 时也走这里）。"""
    await require_token(request)
    import base64 as _b64
    sid = request.match_info["sid"]
    s = SESSIONS.get(sid)
    if not s or getattr(s, "ghost", False):
        return web.json_response({"ok": False, "error": "终端不存在"})
    try:
        data = await request.json()
    except Exception:
        data = {}
    dg = data.get("diag")
    if dg:
        print("[term] diag sid=%s %s" % (sid, dg), flush=True)
    # 客户端网络抖了会重发同一条：按 (页面标识 pq, 序号 q) 去重，别把字写两遍
    pq, q = str(data.get("pq") or ""), int(data.get("q") or 0)
    if pq and q:
        if pq != s.in_pq:
            s.in_pq, s.in_q = pq, 0
        if q <= s.in_q:
            return _jsonz({"ok": True, "c": s.out_off(), "dup": 1})
        s.in_q = q
    d = data.get("d") or ""
    if data.get("resize") or len(d) > 4:   # 单键不记，不然每敲一下一行日志
        print("[term] in sid=%s bytes=%d rz=%s" % (sid, len(d), data.get("resize")), flush=True)
    if d:
        try:
            s.write(_b64.b64decode(d))
        except Exception:
            pass
    rz = data.get("resize")
    if rz:
        try:
            s.resize(int(rz[0]), int(rz[1]))
        except Exception:
            pass
    return _jsonz({"ok": True, "c": s.out_off()})


async def h_api_term_close(request):
    """关掉一个终端：杀进程组、从会话表和 sessions.json 里移除。"""
    await require_token(request)
    data = await request.json()
    sid = (data.get("sid") or "").strip()
    s = SESSIONS.get(sid)
    if not s or getattr(s, "ghost", False):
        return web.json_response({"ok": False, "error": "终端不存在"})
    s.kill()
    del SESSIONS[sid]
    _CMD_RUNS[:] = [r for r in _CMD_RUNS if r["csid"] != sid]
    save_registry()
    return web.json_response({"ok": True})


async def h_api_term_mac(request):
    """「打开电脑终端」：在开发机上开个终端窗口（先 cd 到目录，可带一条命令）。
    macOS = Terminal.app；Linux = tmux 会话（用 `tmux attach -t <名字>` 进去看）。"""
    await require_token(request)
    data = await request.json()
    cwd = (data.get("cwd") or "").strip() or str(Path.home())
    if not Path(cwd).is_dir():
        return web.json_response({"ok": False, "error": "目录不存在: %s" % cwd})
    cmd = (data.get("cmd") or "").strip()
    if len(cmd) > 500:
        cmd = ""
    ok, err, target = await asyncio.get_event_loop().run_in_executor(
        None, lambda: plat.open_terminal(cwd, cmd))
    if not ok:
        return web.json_response({"ok": False, "error": (err or "打开失败")[:200]})
    return web.json_response({"ok": True, "target": target})


async def h_host_stat(request):
    await require_token(request)
    return web.json_response({"ok": True, "relay": bool(_HOST_ST["relay"]),
                              "unbound": bool(_HOST_ST.get("unbound")),
                              "clients": int(_HOST_ST["clients"]),
                              "apps": int(_HOST_ST.get("apps") or 0),
                              "webs": int(_HOST_ST.get("webs") or 0),
                              # 额度账快照 + 最近一次额度判定：手机上「明明没到额度却说
                              # 用完了」时，这里能直接看出这台开发机拿到的账是什么。
                              "use": _use_snapshot(),
                              "registered": bool(_relay_cfg().get("device_id"))})


# ---------- P2P 直连（relay 只牵线）：局域网 HTTP 直达 + 广域网 WebRTC DataChannel ----------
# 票据：客户端拿不到 host token（那是 relay 注入的），relay 改签短命票据 dt =
# b64url("dev|exp|nonce[|fp]|sig")，sig=HMAC(k, 同样的 msg) 前 32 hex，
# k=HMAC(device_secret, "cb-direct-v1")——两端各持一半材料（host 有 secret，
# relay 库里有 secret），票据放 query 参数可免 CORS 预检（老 WebView 最稳）。
# fp 是开发机指纹：录了指纹的设备，票据只在同一台机器上有效。
DT_TTL = 600   # 秒；客户端提前 3 分钟滚动换新
# dt 票据只授权只读重流量路径（与 ext.html DIRECT_GET 严格一致）。
# 写操作（发消息/按键/PTT/APK）永远走中继授权面——票据即使被截获，
# 10 分钟 TTL 内也只能读，动不了会话。dcReq 的任意路径请求同样被拦。
DT_PATHS = ("/api/ext_push", "/api/ext_msg/", "/api/ext_live/", "/api/ext_screen/", "/api/ext_cells/",
            "/api/ext_snap/", "/api/ext_sessions", "/api/ext_stream/", "/api/ext_file",
            "/api/ext_key", "/api/ext_send", "/api/ptt",
            "/api/ext_scan/", "/api/ext_model", "/api/ext_model_switch",
            "/api/ext_cmdhist/", "/api/ext_cmddel/", "/api/ext_cmdrun/", "/api/ext_cmdruns",
            "/api/ext_cmdout/", "/api/plan", "/api/notify_hist")   # 按键/发送/语音/扫描/模型/命令走 P2P 直连
            # /api/plan 只读：中继断链时页面靠它拿开发机的本地估算继续读秒（只读，动不了会话）


def _dt_path_ok(p):
    p = p or "/"
    if not p.startswith("/"):
        p = "/" + p
    return any(p.startswith(s) for s in DT_PATHS)


def _dt_key():
    import hashlib as _hl
    import hmac as _hmac
    sec = (_relay_cfg().get("device_secret") or "").encode()
    return _hmac.new(sec, b"cb-direct-v1", _hl.sha256).digest()


def _dt_claims(dt):
    """票据验签：通过返回 (dev, exp)，否则 None（DC 需要 exp 做定期复验）。"""
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hmac
    try:
        raw = _b64.urlsafe_b64decode(str(dt) + "=" * (-len(str(dt)) % 4)).decode()
        parts = raw.split("|")
        if len(parts) == 4:            # 老票据：没绑指纹（老设备 fp 为空）
            dev, exp, nonce, sig = parts
            msg = "%s|%s|%s" % (dev, exp, nonce)
        elif len(parts) == 5:          # 新票据：dev|exp|nonce|fp|sig
            dev, exp, nonce, fp, sig = parts
            if fp != (plat.machine_fp() or ""):
                return None            # 换机器/指纹变了：同 secret 的旧票也不认
            msg = "%s|%s|%s|%s" % (dev, exp, nonce, fp)
        else:
            return None
        if dev != (_relay_cfg().get("device_id") or ""):
            return None
        if int(exp) < time.time() - 30:
            return None
        want = _hmac.new(_dt_key(), msg.encode(), _hl.sha256).hexdigest()[:32]
        return (dev, int(exp)) if _hmac.compare_digest(sig, want) else None
    except Exception:
        return None


def _dt_verify(dt):
    return _dt_claims(dt) is not None


_LAN_CACHE = {"t": 0.0, "ips": []}      # 缓存 60 秒，见 _lan_ips


def _lan_ips():
    """本机局域网 IPv4 列表（主路由 IP 优先）。探路由用 UDP connect，不真发包。

    ⚠️ gethostbyname_ex(gethostname()) 在 macOS 走 mDNS，网络不顺时能卡十几秒
    （用户机器实测 2~19s）。这个函数以前在事件循环线程里被直接调用，整台 host
    跟着卡——日志里 `[loop] 卡顿` 的栈全是它。现在结果缓存 60 秒（局域网 IP
    一分钟内变不了），异步侧的调用方一律放 executor，别在循环里做解析。"""
    now = time.time()
    if _LAN_CACHE["ips"] and now - _LAN_CACHE["t"] < 60:
        return list(_LAN_CACHE["ips"])
    import socket as _so
    ips = []
    try:
        s = _so.socket(_so.AF_INET, _so.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for ip in _so.gethostbyname_ex(_so.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    ips = [ip for ip in ips
           if not ip.startswith("127.") and not ip.startswith("169.254.")][:6]
    _LAN_CACHE["t"], _LAN_CACHE["ips"] = now, ips
    return list(ips)


_RTC_STUN = ["stun:stun.miwifi.com:3478", "stun:stun.chat.bilibili.com:3478",
             "stun:stun.l.google.com:19302"]
# aiortc 的 iceServers 必须给对象，裸字符串会在建连接时报
# "'str' object has no attribute 'urls'"——这个坑让广域网打洞**一次都没成功过**
# （日志里 2900+ 次应答失败、0 次成功），手机一直显示中转。
_RTC_ICE = [{"urls": u} for u in _RTC_STUN]
# 开发机自己在对称 NAT 后面时也要 TURN 才能被连上；中继下发的浏览器 ICE 不覆盖这里。
for _u in [x.strip() for x in os.environ.get("CB_TURN_URLS", "").split(",") if x.strip()]:
    _e = {"urls": _u}
    if os.environ.get("CB_TURN_USER"):
        _e["username"] = os.environ["CB_TURN_USER"]
        _e["credential"] = os.environ.get("CB_TURN_PASS", "")
    _RTC_ICE.append(_e)
_RTC = {"ok": None, "why": "", "last": 0.0, "fixing": False, "fix_err": ""}
RTC_FIX_MARK = HERE / "logs" / ".rtc-fix-tried"


def _rtc_selfheal():
    """aiortc 缺了就自己补装（后台，不阻塞启动）。

    为什么放在服务里而不是让用户跑 install.sh：aiortc 带 av 原生扩展，
    在部分 Python/架构上第一次装会失败；而「装没装上」用户是无感的 ——
    手机上只会少一条直连路径。要求终端用户去跑安装脚本不现实，所以由服务
    自己后台补一次，装好之后**不用重启**：_rtc_avail 会定期重新 import。

    只在失败后隔 24 小时再试一次（上限），避免每次启动都对着没网的机器空转。
    """
    import subprocess as _sp
    try:
        # 失败后 24 小时内不再试（防对着没网的机器空转）；但**换了版本就立刻重试**
        # —— 补装命令本身可能被修过（0.5.39 就是：av 改成只认二进制轮子）。
        if RTC_FIX_MARK.exists() and time.time() - RTC_FIX_MARK.stat().st_mtime < 86400:
            if RTC_FIX_MARK.read_text().strip() == APP_VERSION:
                return
    except OSError:
        pass
    if _RTC["fixing"]:
        return
    _RTC["fixing"] = True

    def _work():
        try:
            print("[rtc] 缺 aiortc，后台补装（不影响其他功能）…", flush=True)
            # --only-binary=av：av 是原生扩展，最新版常常只有源码包，pip 会挑它去
            # 编译，缺 pkg-config 必挂（实测 3.9.6）。强制只考虑有轮子的版本，
            # pip 会自动退到和当前 Python 匹配的那一版。
            cmd = [sys.executable, "-m", "pip", "install", "-q", "--only-binary=av",
                   "aiortc>=1.10"]
            r = _sp.run(cmd, capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                # uv 建的 venv 没有 pip：退到 uv
                import shutil as _sh
                if _sh.which("uv"):
                    r = _sp.run(["uv", "pip", "install", "-q", "--python", sys.executable,
                                 "--only-binary=av", "aiortc>=1.10"],
                                capture_output=True, text=True, timeout=900)
            if r.returncode == 0:
                print("[rtc] aiortc 补装完成，下次探测即启用 P2P（无需重启）", flush=True)
                try:
                    RTC_FIX_MARK.unlink()
                except OSError:
                    pass
            else:
                tail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
                _RTC["fix_err"] = tail[:200]
                print("[rtc] 自动补装 aiortc 失败：%s" % tail[:160], flush=True)
                print("[rtc]   这台机器的广域网 P2P 关闭（局域网直连/中继不受影响）；"
                      "24 小时后会再试一次", flush=True)
                try:
                    RTC_FIX_MARK.parent.mkdir(parents=True, exist_ok=True)
                    RTC_FIX_MARK.write_text(APP_VERSION)
                except OSError:
                    pass
        except Exception as e:
            print("[rtc] 自动补装 aiortc 异常：%s" % str(e)[:120], flush=True)
        finally:
            _RTC["fixing"] = False

    threading.Thread(target=_work, daemon=True, name="rtc-selfheal").start()


def _rtc_avail():
    # 失败之后每 2 分钟重试一次 import：后台补装完成时，P2P 能自己活过来，
    # 不用等下一次重启（launchd 的 KeepAlive 不会因为依赖装好了就重启进程）。
    if _RTC["ok"] is not True and time.time() - _RTC["last"] < 120:
        return _RTC["ok"]
    _RTC["last"] = time.time()
    if _RTC["ok"] is None or _RTC["ok"] is False:
        try:
            import aiortc  # noqa: F401
            _RTC["ok"] = True
            print("[rtc] aiortc 可用：广域网 P2P 打洞已启用", flush=True)
            # aioice 的 INFO 级日志会打「mDNS 候选解析成/没解析出来」——
            # 之前这条信息全被吞了，看不出打洞到底卡在哪一步。
            try:
                import logging

                def _cb_log(name, level, tag, fmt):
                    """装一个自己的 handler。
                    ⚠️ 不能用「if not logger.handlers」判断：aioice/aiortc 自己在
                    __init__ 里就挂了 NullHandler，于是我们的 handler 永远装不上，
                    日志全被 NullHandler 吃掉（一行都看不到）。按标记认自己的。"""
                    lg = logging.getLogger(name)
                    lg.setLevel(level)
                    lg.propagate = False
                    if not any(getattr(h, "_cb_tag", None) == tag for h in lg.handlers):
                        h = logging.StreamHandler(sys.stdout)
                        h.setFormatter(logging.Formatter(fmt))
                        h._cb_tag = tag
                        lg.addHandler(h)

                # aioice 的 INFO 级日志会打「mDNS 候选解析成/没解析出来」——
                # 之前这条信息全被吞了，看不出打洞到底卡在哪一步。
                _cb_log("aioice", logging.INFO, "cb-ice", "[ice] %(message)s")
                # aiortc 的 DEBUG 日志能看出 ICE 到底起没起（connectionState 卡在 new
                # 时，靠它能看到 __connect 里抛了什么）。
                _cb_log("aiortc", logging.DEBUG, "cb-aiortc", "[aiortc] %(message)s")
                # 但 SCTP 那一层是逐 chunk 打印的（一条推送能刷几百行），
                # 单独降级到 INFO：保留状态变化，不刷屏。
                # ⚠️ logger 名是模块全名（aiortc.rtcsctptransport），不是 "aiortc.sctp"。
                _cb_log("aiortc.rtcsctptransport", logging.INFO, "cb-sctp", "[sctp] %(message)s")
            except Exception:
                pass
        except Exception as e:
            _RTC["ok"] = False
            _RTC["why"] = str(e)[:160] or e.__class__.__name__
            print("[rtc] aiortc 不可用（广域网 P2P 关闭，仅局域网直连）: %s" % _RTC["why"],
                  flush=True)
            _rtc_selfheal()   # 后台自己补装，不等用户去跑脚本
    return _RTC["ok"]


def _cors_from(request):
    """直连跨源响应头：仅当 Origin 是中继域名时放行（页面从 relay 域加载，
    直连请求发往 host 的局域网地址）。主/备两个域名都认——手机可能从备用域名
    打开页面，host 这边只会连其一。精确比 host：旧写法是子串包含，
    仿冒域（中继域名 + .evil.com 后缀）也能混进来。"""
    o = (request.headers.get("Origin") or "").rstrip("/")
    m = re.match(r"^[a-z]+://([^/:]+)(?::\d+)?$", o, re.I)
    if not m:
        return None
    hosts = set()
    for u in (_relay_cfg().get("url") or "", RELAY_ALT_URL):
        mm = re.match(r"^[a-z]+://([^/:]+)", u or "", re.I)
        if mm:
            hosts.add(mm.group(1).lower())
    if m.group(1).lower() not in hosts:
        return None
    return {"Access-Control-Allow-Origin": o, "Vary": "Origin"}


@web.middleware
async def _direct_bytes_mw(request, handler):
    """局域网直连（非 P2P dc=1）的请求，把请求体+响应体算进直连字节。
    P2P 的字节在 _dc_serve 里按帧计，这里跳过避免重复。"""
    try:
        dc = request.query.get("dc") == "1"
        direct = (not dc) and (not _peer_is_local(request))
    except Exception:
        direct = False
    resp = await handler(request)
    if direct:
        try:
            n = int(request.content_length or 0)
            body = getattr(resp, "body", None)
            if body:
                n += len(body)
            cid = (request.query.get("c") or request.headers.get("X-CB-Cid") or "")[:64]
            _p2p_bytes_add(n, cid)
        except Exception:
            pass
    return resp


@web.middleware
async def _cors_mw(request, handler):
    """给放行来源的普通响应补 CORS 头；SSE 等流式响应在 prepare 前自行取
    request["cors"]（prepare 之后头就发走了，中间件改不动）。"""
    request["cors"] = _cors_from(request)
    if request.method == "OPTIONS" and request["cors"] \
            and request.headers.get("Access-Control-Request-Method"):
        # 跨源 POST 的预检：路由没注册 OPTIONS，aiohttp 回 405 且不带 CORS 头，
        # 预检必挂、页面只好整条回落中继（ext_send 一天 149 次 405 就是它）。
        # 这里直接替路由答 204；真正的请求照样要过 token，预检不带权不泄密。
        h = dict(request["cors"])
        h["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        h["Access-Control-Allow-Headers"] = request.headers.get(
            "Access-Control-Request-Headers") or "Content-Type"
        h["Access-Control-Max-Age"] = "86400"
        return web.Response(status=204, headers=h)
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    hdrs = request.get("cors")
    if hdrs and not resp.prepared:  # prepared=流已 prepare（SSE 头已发走改不动）；普通响应在此补头
        resp.headers.update(hdrs)
    return resp


# 慢请求诊断（2026-11-09 手机「进会话内容加载很慢」排查）：只盯会话页那几条，
# ≥300ms 才落一行——手机侧到底是哪一条慢，日志里直接能看，不用再猜。
_SLOW_WHITELIST = ("/api/ext_sessions", "/api/ext_msg/", "/api/ext_live/",
                   "/api/ext_screen/", "/api/ext_cells/", "/api/ext_snap/", "/api/plan")


_ERRLOG_AT = {}     # (路径模板, 状态码) -> 上次打印时刻：同一处失败 60 秒只记一行


@web.middleware
async def _errlog_mw(request, handler):
    """手机端「打不开/发不进」最常见的疑问是「请求到底到没到 host、host 答了什么」。

    以前只有慢请求（>=300ms）留痕，快速失败的请求什么都不打——用户说会话显示不出来，
    日志里一片安静。这里把所有 4xx/5xx 记一行（路径里的会话 id 归一，同一处每分钟一行）。
    """
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    st = getattr(resp, "status", 200)
    if st >= 400:
        p = re.sub(r"/[0-9a-fA-F-]{8,}", "/<id>", request.path or "")[:120]
        key = (p, st)
        now = time.time()
        if now - _ERRLOG_AT.get(key, 0) > 60:
            if len(_ERRLOG_AT) > 500:
                _ERRLOG_AT.clear()
            _ERRLOG_AT[key] = now
            print("[err] %s %d %s %s ip=%s" % (
                time.strftime("%m-%d %H:%M:%S"), st, request.method, p,
                request.remote or "-"), flush=True)
    return resp


@web.middleware
async def _slow_mw(request, handler):
    p = request.path or ""
    if not any(p.startswith(x) for x in _SLOW_WHITELIST):
        return await handler(request)
    t0 = time.time()
    try:
        return await handler(request)
    finally:
        dt = (time.time() - t0) * 1000.0
        if dt >= 300:
            print("[slow] %s %.0fms %s %s ip=%s" % (
                time.strftime("%m-%d %H:%M:%S"), dt, request.method, p[:160],
                request.remote or "-"), flush=True)


def _rtc_why_safe(s):
    """把内部报错压成一句能放在未鉴权接口上的人话：去掉本机路径、截断。
    /api/ping 是局域网免鉴权的探针，只该出现「缺什么」，不该出现堆栈和目录。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    home = os.path.expanduser("~")
    if home and home != "/":
        t = t.replace(home, "~")
    t = re.sub(r"(/[\w.\-]+){3,}", "<路径>", t)   # 剩下的绝对路径一律打码
    return t[:200]


async def h_ping(request):
    """直连探针：免鉴权（只回存在性，token/票据等敏感面一概不涉及）。

    顺带回一个 P2P 可用位 + 一句话原因：这台机器「能不能打洞」以前只能从
    中继那一侧看，开发机的日志还在 ~/.codeboss/logs 里，人在别的机器上
    根本排查不了。这里只回布尔和人话，不回堆栈、不回路径。"""
    # v 是给「端口上有谁」的探测用的（_serving_version）：免鉴权、不算更新，同版本实例
    # 互相认出来就别抢端口。别换成 /api/version——那个要跑 update_check。
    _hp = _host_product()
    body = {"ok": 1, "v": APP_VERSION, "p": _hp,
            # 只加不删：老 APK 不认这两个字段也不会坏
            "series": branding.SERIES, "plabel": branding.product_name(_hp)["label"]}
    # 探针回声：页面发 /api/ping?pr=<nonce>，这里原样带回——代理/运营商劫持的
    # 200 页面给不出这个回声，页面据此认定「答话的不是开发机」（2026-11-18 aliyun）。
    pr = (request.query.get("pr") or "").strip().lower()
    if re.match(r"^[0-9a-f]{4,64}$", pr):
        body["pr"] = pr
    try:
        body["rtc"] = 1 if _rtc_avail() else 0
        body["py"] = "%d.%d" % sys.version_info[:2]
        if not body["rtc"]:
            body["why"] = _rtc_why_safe(_RTC.get("why"))
            if _RTC.get("fix_err"):
                body["fix"] = _rtc_why_safe(_RTC["fix_err"])
    except Exception:
        pass
    return web.json_response(body, headers=_cors_from(request) or {})


async def h_brand(request):
    """系列/端名（免鉴权，首屏就要用，不涉及账号）：页面标题、关于页、页脚认它。
    拿不到（离线/老 host）时页面回落内置常量——这里只负责「说清楚我是谁」。"""
    p = _client_product_for(request)
    n = branding.product_name(p)
    body = {"series": branding.SERIES, "product": p,
            "product_label": n["label"], "product_app": n["app"],
            "ver": APP_VERSION,
            "site": branding.SITE_URL}
    # 自建中继能力表（拉不到 = 不带键，页面 fail-open）。_relay_caps 是同步
    # urlopen(timeout=3)，必须丢线程池——直调会把整个 host 事件循环卡 3 秒
    # （同机所有 SSE/WS/agent IO 一起停），纪律同 update_check。
    caps = await asyncio.get_event_loop().run_in_executor(None, _relay_caps)
    if caps:
        body["caps"] = caps
    return web.json_response(body)


def _acct_products_payload():
    """这账号能用哪些端：问中继（设备自证），拿不到就 fail-open 给双端。
    真墙在中继（产品槽 403），host 只做展示——中继挂了不能把人关在门外。"""
    d = _relay_dev_call("/r/acct_info", {}, timeout=6) or {}
    raw = d.get("allowed")
    if not d.get("ok") or not isinstance(raw, list):
        return {"ok": False, "allowed": list(branding.PRODUCTS), "source": "unknown"}
    allowed = [p for p in raw if p in branding.PRODUCTS]
    if not allowed and raw:      # 全是脏值：当成没拿到，别据此锁人
        return {"ok": False, "allowed": list(branding.PRODUCTS), "source": "unknown"}
    return {"ok": True, "allowed": allowed,
            "source": str(d.get("source") or "plan")}


async def h_acct_products(request):
    await require_token(request)
    loop = asyncio.get_event_loop()
    return web.json_response(await loop.run_in_executor(None, _acct_products_payload))


_DC_FRAME = 32768   # 非流式大响应的分帧大小（base64 字符数，4 的倍数：每帧各自 atob 解码）


def _dc_frames(rid, code, ct, out, raw):
    """把一次非流式响应拍成帧：小响应整帧下发；大响应 res(空体) + chunk* 分帧。

    对端 SDP 里的单条 DataChannel 消息上限（iOS/Safari 常见 64 KB）挡在中间：61 KB 的
    /api/notify_hist 整帧发过去会整段收不到，页面只能等到超时——表现就是「提醒历史
    永远是空的」。页面 onChunk 本来就按序拼接（流式通道一直用它），分帧对老页面也兼容。
    返回 (frames, 分帧数)。"""
    import base64
    b64 = base64.b64encode(raw).decode()
    if len(b64) <= _DC_FRAME:
        return ([{"t": "res", "id": rid, "code": code, "ct": ct,
                  "headers": out, "body": b64}], 1)
    fr = [{"t": "res", "id": rid, "code": code, "ct": ct, "headers": out, "body": ""}]
    for i in range(0, len(b64), _DC_FRAME):
        fr.append({"t": "chunk", "id": rid, "b64": b64[i:i + _DC_FRAME]})
    return (fr, len(fr) - 1)


def _dc_serve(ch, local, cid=None):
    """接线一个已打开的 WebRTC DataChannel：票据 auth 栅栏 + 帧循环。
    帧语义与 relay 隧道完全同构（req → res/chunk*/res_end → cancel），
    host 侧把请求映射为对 127.0.0.1 的本地调用——端点与鉴权零改动。"""
    import aiohttp
    import base64 as _b64
    from urllib.parse import quote
    st = {"auth": False, "exp": 0, "tasks": {}, "cid": cid}

    def send(o):
        try:
            ch.send(json.dumps(o))
        except Exception:
            pass

    def on_end(rid, err=None):
        o = {"t": "res_end", "id": rid}
        if err:
            o["err"] = err[:200]
        send(o)

    async def serve(d):
        rid = d.get("id")
        p = d.get("p") or "/"
        if not p.startswith("/"):
            p = "/" + p
        if not _dt_path_ok(p):   # DC 与 HTTP 同一张只读白名单（写操作只走中继）
            send({"t": "res", "id": rid, "code": 403,
                  "ct": "text/plain; charset=utf-8", "headers": {},
                  "body": _b64.b64encode(b"dt only allows read paths").decode()})
            on_end(rid)          # 每个 req 都必须有终帧，页面据此收尾
            return
        # dc=1：让 require_token 认得出「这是 P2P 直连来的」——它和中继转发一样
        # 都从 127.0.0.1 进来，不加标记就分不清：发消息的条数会漏计/重复计。
        url = "http://127.0.0.1:%d%s%st=%s&dc=1" % (PORT, p, "&" if "?" in p else "?", quote(TOKEN))
        body = _b64.b64decode(d["body"]) if d.get("body") else None
        _hdrs = d.get("headers") or d.get("h") or {}
        _dc_cid = str(next((v for k, v in _hdrs.items()
                            if str(k).lower() == "x-cb-cid"), ""))[:64]
        _p2p_bytes_add(len(body) if body else 0, _dc_cid)
        # 头字段：host 这边读 "headers"，老版本页面发的是 "h"，两个都认
        headers = {k: v for k, v in ((d.get("headers") or d.get("h") or {}).items())
                   if k.lower() not in ("host", "cookie", "content-length", "connection",
                                        "accept-encoding")}
        headers["accept-encoding"] = "identity"
        try:
            async with local.request(d.get("m") or "GET", url, data=body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=None,
                                                                   sock_connect=10,
                                                                   sock_read=90)) as r:
                out = {k: v for k, v in r.headers.items()
                       if k.lower() in ("x-frame-hash", "x-host-winsize", "x-winsize",
                                        "x-hist-count", "cache-control")}
                ct = r.headers.get("Content-Type", "")
                if d.get("st") == 1:   # 流式（SSE）：头帧 → chunk* → 终帧
                    send({"t": "res", "id": rid, "st": 1, "code": r.status,
                          "ct": ct, "headers": out, "body": ""})
                    n = 0
                    async for chunk in r.content.iter_chunked(16384):
                        n += 1
                        _p2p_bytes_add(len(chunk), _dc_cid)
                        send({"t": "chunk", "id": rid, "n": n,
                              "b64": _b64.b64encode(chunk).decode()})
                    on_end(rid)
                else:
                    raw = await r.read()
                    fr, nf = _dc_frames(rid, r.status, ct, out, raw)
                    for _f in fr:
                        send(_f)
                    if nf > 1:
                        _rtc_log("dc 大响应 cid=%s %s bytes=%d frames=%d"
                                 % (str(st.get("cid") or "")[:16], p.split("?")[0],
                                    len(raw), nf))
                    # 非流式也要终帧：以前这里漏了 res_end，页面 dcReq 永远等不到
                    # 收尾，只能等超时（语音是 60 秒）再回落中继——表现就是
                    # 「转写中」卡住一分钟。流式那边一直是发的，所以只有非流式接口受害。
                    on_end(rid)
        except asyncio.CancelledError:
            on_end(rid, "cancelled")
            raise
        except Exception as e:
            if d.get("st") == 1:
                on_end(rid, str(e))
            else:
                send({"t": "res", "id": rid, "code": 502, "ct": "text/plain; charset=utf-8",
                      "headers": {}, "body": _b64.b64encode(("dc: %s" % e).encode()).decode()})
                on_end(rid)
        finally:
            st["tasks"].pop(str(rid), None)

    async def on_msg(msg):
        try:
            d = json.loads(msg if isinstance(msg, str) else msg.decode())
        except Exception:
            return
        t = d.get("t")
        if t == "auth":
            claims = _dt_claims(str(d.get("dt") or ""))
            st["auth"] = claims is not None
            st["exp"] = claims[1] if claims else 0
            # 只记 dev/exp，不记票据本身（票据=免鉴权直连凭证，不能落日志）
            _rtc_log("dc 票据校验 cid=%s ok=%s dev=%s exp=%s"
                     % (str(st.get("cid") or "")[:16], st["auth"],
                        (claims[0] if claims else "-"), (claims[1] if claims else 0)))
            send({"t": "auth_ok" if st["auth"] else "auth_bad"})
            return
        if not st["auth"]:
            return   # 未认证一概不答（DTLS 之上再加票据栅栏）
        # 票据过期即断：以前只验一次、永久有效，中继断掉后 P2P 可以无限期白嫖。
        # 客户端每 5 分钟用中继续签的新 dt 复验一次；中继断了续不到，最迟 exp+60s 断开。
        if st["exp"] and time.time() > st["exp"] + 60:
            st["auth"] = False
            send({"t": "auth_expired"})
            try:
                ch.close()
            except Exception:
                pass
            return
        if t == "cancel":
            task = st["tasks"].pop(str(d.get("id")), None)
            if task:
                task.cancel()
            return
        if t == "req":
            rid = str(d.get("id"))
            _p = str(d.get("p") or "")
            _pure = _p.split("?", 1)[0]
            _dm = str(d.get("m") or "GET").upper()
            _wr = _dm not in ("GET", "HEAD", "OPTIONS")
            # 直连/P2P 墙（中继看不见 DC 流量，只能 host 自己挡）：条数墙 + 功能墙。
            # 时长墙已摘（2026-11-26：收费墙只按条数）。「看不拦」：读请求不拦。
            if _wr and _feat_blocked(_pure):
                _fm = json.dumps({"ok": False, "feature": "terminal",
                                  "error": "识别命令是 Pro / Max 套餐的功能，开通后使用"},
                                 ensure_ascii=False).encode()
                send({"t": "res", "id": rid, "code": 403,
                      "ct": "application/json; charset=utf-8", "headers": {},
                      "body": _b64.b64encode(_fm).decode()})
                on_end(rid)
                return
            if _wr and _is_send_req(_pure) and _send_over():
                _msg = json.dumps(
                    {"ok": False, "limit": "sends",
                     "sends": {"used": max(int(_USE_ST.get("sends") or 0),
                                           int(_USE_ST.get("sends_local") or 0)),
                               "cap": int(_USE_ST.get("sends_cap") or 0)},
                     "error": "今天的免费消息额度已用完，明天恢复"},
                    ensure_ascii=False).encode()
                send({"t": "res", "id": rid, "code": 403,
                      "ct": "application/json; charset=utf-8", "headers": {},
                      "body": _b64.b64encode(_msg).decode()})
                on_end(rid)
                return
            # P2P/DC 流量不经过中继，使用时长只能在这里记。口径和 HTTP 一致：
            # 后台提醒/静态资源不算；页面没人碰（a=0）也不算；读请求不算（_use_touch 里判）。
            _pas = "a=0" in (_p.split("?", 1)[1].split("&") if "?" in _p else ())
            if (not _pas and _pure != USE_BG_PATH and not _pure.startswith("/static/")
                    and _pure not in USE_PROBE_PATHS
                    and not _pure.startswith(USE_STREAM_PATHS)
                    and _pure not in USE_LOCAL_PATHS):
                _dc_h = d.get("headers") or d.get("h") or {}
                _dc_cid = str(_dc_h.get("X-CB-Cid") or _dc_h.get("x-cb-cid") or "")[:64]
                if not _dc_cid and "?" in _p:
                    from urllib.parse import parse_qs
                    _dc_cid = (parse_qs(_p.split("?", 1)[1]).get("c") or [""])[0][:64]
                _use_touch(path=_pure, method=_dm, cid=_dc_cid)
            # 直连（DC）到底有没有在跑：非流式请求打一行，通话/推送这种长连接不打。
            if not d.get("st"):
                _rtc_log("dc 请求 cid=%s %s %s" % (str(st.get("cid") or "")[:16],
                                                  d.get("m") or "GET", (d.get("p") or "")[:80]))
            st["tasks"].pop(rid, None)   # 同 id 重入：旧的自然被 cancel 掉落 finally
            st["tasks"][rid] = asyncio.get_event_loop().create_task(serve(d))

    try:
        ch.on("message")(lambda m: asyncio.get_event_loop().create_task(on_msg(m)))
    except Exception:
        pass


def _rtc_log(msg):
    """[rtc] 日志统一带 epoch 毫秒。
    手机端（ext.html）打的也是 epoch 秒，两边能直接对齐同一秒的时间线，
    不用再靠猜哪次打洞对应哪条日志。"""
    print("[rtc %.3f] %s" % (time.time(), msg), flush=True)


def _mdns_resolve(name):
    """把 <uuid>.local 换成真 IP。

    浏览器（Safari/Chrome）在网页没有媒体授权时，会把 WebRTC 的本机地址藏成
    mDNS 名字。aiortc 自带的 mDNS 解析器只发 A 查询，浏览器不一定答 →
    候选永远配不上对 → 打洞 100% 失败。系统解析器（macOS 的 mDNSResponder）
    是完整的 DNS-SD 实现，同一个局域网里基本都能问到。"""
    try:
        for _i in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_DGRAM):
            _ip = _i[4][0]
            if _ip and not _ip.startswith("127."):
                return _ip
    except Exception:
        pass
    return None


def _sdp_unmask(sdp):
    """把 offer 里的 .local 主机候选换成真 IP（同局域网 → 必然查得到）。
    换不掉的原样留着，交给 aiortc 自己的 mDNS 解析器再试一次。"""
    sdp = sdp or ""
    if ".local" not in sdp:
        return sdp, 0
    sep = "\r\n" if "\r\n" in sdp else "\n"
    out = []
    n = 0
    for line in sdp.split(sep):
        p = line.split()
        if len(p) >= 8 and p[0].startswith("a=candidate:") \
                and p[7] == "host" and p[4].endswith(".local"):
            ip = _mdns_resolve(p[4])
            if ip:
                p[4] = ip
                line = " ".join(p)
                n += 1
        out.append(line)
    return sep.join(out), n


async def _rtc_answer(ws, lock, local, d):
    """广域网 P2P：浏览器发 offer，经 relay 信令到此；answer 原路送回。
    ICE 走公共 STUN 打洞；打不通客户端自动回落 relay 隧道，零回归。"""
    if not _rtc_avail():
        return
    from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                        RTCSessionDescription)
    cid = d.get("id")
    pc = RTCPeerConnection(RTCConfiguration(
        iceServers=[RTCIceServer(**s) for s in _RTC_ICE]))
    try:
        # DC 由浏览器侧建（offer 里已带 SCTP m 行），host 只收 "datachannel" 事件
        _sdp_in = d.get("sdp") or ""
        # .local 解析走系统解析器（getaddrinfo），一个查不到的名字能卡 5 秒以上：
        # 在事件循环里做，手机一进会话整台开发机就冻住（2026-11-11 抓栈 7.9s）。
        _sdp, _fixed = await asyncio.get_event_loop().run_in_executor(
            None, _sdp_unmask, _sdp_in)
        if _fixed:
            _rtc_log("cid=%s 系统解析器解开了 %d 个 .local 候选" % (str(cid)[:16], _fixed))
        await pc.setRemoteDescription(RTCSessionDescription(
            sdp=_sdp, type=d.get("type") or "offer"))
        # 诊断：浏览器给的候选长什么样（.local = mDNS，aiortc 解析不了，打洞必失败）
        _sdp = _sdp_in
        _cb = re.findall(r"a=candidate:.*? typ (\w+)", _sdp)
        _cbr = re.findall(r"^a=candidate:.*$", _sdp, re.M)
        _mdns = [c for c in _cbr if ".local" in c]
        _rtc_log("收到 offer cid=%s 浏览器候选=%s mDNS=%d" % (str(cid)[:16], _cb or "无", len(_mdns)))
        # m 行：aiortc 只有在 offer 里看到 m=application 才会建 SCTP 传输，
        # 缺了这一行 → __connect 里直接跳过 → ICE 压根不起（状态永远 new）。
        _ml = re.findall(r"^m=.*$", _sdp_in, re.M)
        _rtc_log("  offer m行: %s" % (" | ".join(_ml) or "无"))
        for _c1 in _cbr[:6]:
            _rtc_log("  浏览器候选 %s" % _c1[:150])
        await pc.setLocalDescription(await pc.createAnswer())   # aiortc 此处等齐 ICE 候选
        chs = []

        @pc.on("datachannel")
        def _on_ch(ch):
            chs.append(ch)
            _rtc_log("dc 通道打开 cid=%s" % str(cid)[:16])
            _dc_serve(ch, local, cid)

        async with lock:
            await ws.send_json({"t": "rtc_ans", "id": cid,
                                "sdp": pc.localDescription.sdp, "type": "answer"})
        # 打洞诊断：把各阶段状态打出来，卡在哪一步一眼可见
        import re as _re2
        _c = _re2.findall(r"^a=candidate:.*? typ (\w+)", pc.localDescription.sdp, _re2.M)
        _rtc_log("已应答 cid=%s 本机候选=%s，等待打洞…" % (str(cid)[:16], _c or "无"))
        for _c2 in _re2.findall(r"^a=candidate:.*$", pc.localDescription.sdp, _re2.M)[:6]:
            _rtc_log("  本机候选 %s" % _c2[:150])
        t0 = time.time()
        last = ""
        while pc.connectionState not in ("connected", "closed", "failed"):
            cur = "%s/ice=%s" % (pc.connectionState, pc.iceConnectionState)
            if cur != last:
                last = cur
                _rtc_log("cid=%s 状态 %s (+%.0fs)" % (str(cid)[:16], cur, time.time() - t0))
            if time.time() - t0 > 45:   # 45s 还没连上：判失败，让客户端回中继
                _rtc_log("cid=%s 打洞超时（本机候选=%s）" % (str(cid)[:16], _c or "无"))
                break
            await asyncio.sleep(1)
        if pc.connectionState == "connected":
            _rtc_log("P2P 已连接 cid=%s" % str(cid)[:16])
            while pc.connectionState == "connected":
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _rtc_log("应答失败 cid=%s: %s" % (str(cid)[:16], str(e)[:160]))
    finally:
        try:
            await pc.close()
        except Exception:
            pass


async def _relay_serve_stream(ws, lock, local, d):
    """流式 req（st:1，SSE 等长连）：头帧 → chunk 帧* → 终帧，边收边发不缓冲。
    任何异常/cancel 都保证终帧（res_end）必达，relay 侧据此收尾。"""
    import aiohttp
    import base64 as _b64
    from urllib.parse import quote
    rid = d.get("id")
    n = 0

    async def _end(err=None):
        try:
            async with lock:
                await ws.send_json({"t": "res_end", "id": rid, **({"err": err} if err else {})})
        except Exception:
            pass

    try:
        p = d.get("p") or "/"
        if not p.startswith("/"):
            p = "/" + p
        url = "http://127.0.0.1:%d%s%st=%s" % (PORT, p, "&" if "?" in p else "?", quote(TOKEN))
        body = _b64.b64decode(d["body"]) if d.get("body") else None
        headers = {k: v for k, v in (d.get("headers") or {}).items()
                   if k.lower() not in ("host", "cookie", "content-length", "connection",
                                        "accept-encoding")}
        # x-cb-stream-id 必须透传：h_ext_push 靠它注册 cancel 路由，丢了就永远掐不掉
        headers["accept-encoding"] = "identity"   # 转发原始字节流，别让本地连接悄悄压缩
        headers["X-CB-Via"] = "relay"             # 内部标记：这不是本机桌面窗口（见 _desktop_window）
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=90)
        async with local.request(d.get("m") or "GET", url, data=body, headers=headers,
                                 timeout=timeout) as r:
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in ("transfer-encoding", "content-length", "connection",
                                        "set-cookie", "content-type")}
            async with lock:
                await ws.send_json({"t": "res", "id": rid, "st": 1, "code": r.status,
                                    "ct": r.headers.get("Content-Type", ""),
                                    "headers": out, "body": ""})
            async for chunk in r.content.iter_chunked(65536):
                n += 1
                async with lock:
                    await ws.send_json({"t": "chunk", "id": rid, "n": n,
                                        "b64": _b64.b64encode(chunk).decode()})
        await _end()
    except asyncio.CancelledError:
        await _end("cancelled")   # relay 已断开浏览器侧；终帧尽力而为
        raise
    except Exception as e:
        await _end(str(e)[:200])


def _tunnel_body(raw, req_headers, status):
    """隧道内压缩完整响应。2026-11-18：aliyun(境外)→中继(上海)上行实测 ~17KB/s，
    653KB 页面（base64 后 871KB）要 51s，手机等不到就显示断网。gzip 后 ~130KB，8 秒。
    relay 收到 Content-Encoding 就不再做边缘压缩（否则二次压缩，页面全坏）。"""
    if status != 200 or not raw or len(raw) < 2048:
        return raw, {}
    h = {str(k).lower(): v for k, v in (req_headers or {}).items()}
    if "gzip" not in str(h.get("accept-encoding", "")).lower():
        return raw, {}
    # 必须是 gzip 容器（wbits=31）。zlib.compress() 出的是 zlib 流（\x78\x9c），
    # 标成 gzip 浏览器解不开也不报错，页面永远加载不完 —— 0.9.16 全站断网就是它。
    co = zlib.compressobj(6, zlib.DEFLATED, 31)
    return co.compress(raw) + co.flush(), {"content-encoding": "gzip"}


async def _relay_serve(ws, lock, local, d):
    """处理一个隧道 req 帧：本地 HTTP 调用 → res 帧回给 relay。"""
    import aiohttp
    import base64 as _b64
    from urllib.parse import quote
    rid = d.get("id")
    if d.get("st") == 1:   # 流式请求走分块转发
        await _relay_serve_stream(ws, lock, local, d)
        return
    try:
        p = d.get("p") or "/"
        if not p.startswith("/"):
            p = "/" + p
        url = "http://127.0.0.1:%d%s%st=%s" % (PORT, p, "&" if "?" in p else "?", quote(TOKEN))
        body = _b64.b64decode(d["body"]) if d.get("body") else None
        headers = {k: v for k, v in (d.get("headers") or {}).items()
                   if k.lower() not in ("host", "cookie", "content-length", "connection")}
        headers["X-CB-Via"] = "relay"             # 内部标记：不是本机桌面窗口（见 _desktop_window）
        async with local.request(d.get("m") or "GET", url, data=body, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=150)) as r:
            raw = await r.read()          # aiohttp 已自动解压：body 是明文
            # content-encoding 必须丢掉：r.headers 里还留着本地的 deflate/gzip 头，
            # 原样转发就是「头说压缩、体是明文」→ 浏览器 ERR_CONTENT_DECODING_FAILED。
            # 需要压缩时由 _tunnel_body 重新压并写回正确的 gzip 头。
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in ("transfer-encoding", "content-length", "connection",
                                        "set-cookie", "content-type", "content-encoding")}
            raw, extra = _tunnel_body(raw, d.get("headers"), r.status)
            out.update(extra)
        frame = {"t": "res", "id": rid, "code": r.status,
                 "ct": r.headers.get("Content-Type", ""), "headers": out,
                 "body": _b64.b64encode(raw).decode()}
    except Exception as e:
        frame = {"t": "res", "id": rid, "code": 502, "ct": "text/plain; charset=utf-8",
                 "body": _b64.b64encode(("host-link: %s" % e).encode()).decode()}
    async with lock:
        await ws.send_json(frame)


def _link_deny_wipe(status):
    """中继拒连的原因里，哪些要清本机注册（= 凭证失效/设备已被删）。
    401/403 是终局；423 冻结、429 开发机数超额都是可恢复的——清掉注册等于把
    那台机器永久注销，解冻/升级之后也回不来。"""
    return status in (401, 403)


def _relay_wipe(why):
    """注册已失效（relay 侧删除设备/凭证被拒）：清掉本机 config.json 的 relay 配置，
    停止自动重连。要重新接入就再跑一次 --enroll。"""
    cfg = _load_cfg()
    if cfg.pop("relay", None) is not None:
        (HERE / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    # 内存里的 _CFG 是启动时读的，不清掉的话 registered 一直为真 → 客户端会显示
    # 「开发机 离线」（像是网络断了），而实际是这台机器已经不在账号里了。
    _CFG.pop("relay", None)
    _HOST_ST["unbound"] = False
    _HOST_ST["clients"] = 0
    print("[relay] %s → 已清除本机注册并停止重连" % why, flush=True)


APK_WATCH_S = 60   # 链路常驻期间 APK 重 build 的补探周期


async def _push_apk(ws, lock):
    """连上中继就把客户端 APK 推过去落盘：壳的版本检查/下载从此由中继本地答，
    开发机离线也能自更新。单 host 双产物：本机 build 目录里哪线的包在就推哪线
    （尾帧带 product，中继按包落槽——不再按设备产品线猜）。
    成功返回 {线: (mtime_ns, size)}（_apk_watch 拿它对比是否需要补推）。
    失败不影响链路；旧中继不认识 apk 帧，忽略即可（向前兼容）。"""
    out = {}
    for p in branding.PRODUCTS:
        try:
            r = await _push_apk_one(ws, lock, p)
        except Exception as e:
            print("[relay] %s APK 推送失败: %s" % (p, str(e)[:120]), flush=True)
            continue
        if r:
            out[p] = r
    return out


def _apk_state_path():
    """每次现算：测试用 monkeypatch 改 server.HERE 时必须跟着走。
    曾经写成模块级常量，pytest 于是读到并改写了真实仓库里的 apk_pushed.json
    ——跑一次测试就给生产 host 记上一小时「推送退避」，新包推不出去。"""
    return HERE / "apk_pushed.json"

# 跨境长活流的持续上行会被掐（2026-11-18 aliyun→中继实测：上传 ~150KB 后黑洞，
# 同机上传海外 500KB 完整成功——排除出站/安全组）。1.2MB APK 每次重连都硬推 =
# 每次必卡死，APK 帧还把后续所有请求帧堵在管道里（手机切到这台全白屏）。
# 所以：推成功过且文件没变 → 跳（中继 APK 库是全局的，别机推过=中继已有）；
# 推送中途链路死 → 记失败退避（翻倍封顶 24h），别让每次重连都重复卡死一遍。
_APK_FAIL_BASE_S = 3600
_APK_FAIL_MAX_S = 24 * 3600
_APK_INFLIGHT = {}   # product -> (mtime_ns, size)：推送进行中（链路死亡时补记失败用）


def _apk_state_load():
    try:
        d = json.loads(_apk_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _apk_state_save(d):
    try:
        _apk_state_path().write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def _apk_skip_reason(product, mtime_ns, size):
    """该推还是该跳：None=推；字符串=跳（原因，写日志用）。"""
    st = _apk_state_load().get(product) or {}
    ok = st.get("ok")
    if ok and ok.get("mtime_ns") == mtime_ns and ok.get("size") == size:
        return "中继已有此包（本机曾推成功）"
    f = st.get("fail_at")
    if f:
        n = int(st.get("fail_n") or 0)
        wait = min(_APK_FAIL_BASE_S * (2 ** min(n, 8)), _APK_FAIL_MAX_S)
        left = wait - (time.time() - float(f))
        if left > 0:
            return "上次推送卡死，退避中（还剩 %ds/%ds）" % (int(left), wait)
    return None


def _apk_note_fail(product):
    """推送没走完（链路死/被打断）：记失败，退避期内不再重推同一份。"""
    st = _apk_state_load()
    d = st.get(product) or {}
    d["fail_at"] = time.time()
    d["fail_n"] = int(d.get("fail_n") or 0) + 1
    d.pop("ok", None)
    st[product] = d
    _apk_state_save(st)
    _APK_INFLIGHT.pop(product, None)


def _apk_mark_inflight_fail():
    """链路死亡时，把还在推的包补记失败——send 只是进缓冲不报错，
    卡死的推送只有等看门狗杀链时在这里显式记账。"""
    for p in list(_APK_INFLIGHT):
        _apk_note_fail(p)


_APK_SEND_TIMEOUT_S = 15   # 单帧 192KB 的发送上限。健康链路毫秒级；跨境黑洞链路
# send 永远 pending 且不抛错——0.9.13 时 aliyun/Mac 卡死在 await send 上，
# 协程堵死、看门狗排在后面永远轮不到 → 设备离线。超时→落退避→重连。

async def _apk_send(ws, lock, frame):
    """带超时的单帧发送：send 只是进缓冲、永远 pending 也不报错，必须自己掐。"""
    async with lock:
        await asyncio.wait_for(ws.send_json(frame), _APK_SEND_TIMEOUT_S)


_APK_PKGS = tuple("com.%s.terminal" % p for p in branding.PRODUCTS)


def _apk_pkg(path):
    """读包内 AndroidManifest.xml 认包名（二进制 XML，包名是字符串池里的明文）。
    认不出返回 None——老包/坏包不拦，只靠中继侧那条校验兜着。"""
    import zipfile
    try:
        raw = zipfile.ZipFile(str(path)).read("AndroidManifest.xml")
    except Exception:
        return None
    for pkg in _APK_PKGS:
        if pkg.encode() in raw:
            return pkg
    return None


async def _push_apk_one(ws, lock, product):
    """推一条线的包；无包/半截包/失败返回 None。"""
    import base64 as _b64
    apk = _android_dir() / "build" / product / (product + "-terminal.apk")
    if not apk.exists():
        return None
    st0 = apk.stat()
    if st0.st_size < 10240:
        return None
    why = _apk_skip_reason(product, st0.st_mtime_ns, st0.st_size)
    if why:
        return None   # 跳过：省 1.2MB 上行，中继库里本来就有这份
    blob = apk.read_bytes()
    st1 = apk.stat()
    if (st1.st_mtime_ns, st1.st_size) != (st0.st_mtime_ns, st0.st_size):
        return None   # 读取期间文件还在变（正在 build），下轮再推，避免半截包落中继
    ver, code = "?", 0
    try:
        m = json.loads((apk.parent / "apk.json").read_text())   # 打包后真值（build.sh 落盘）
        ver, code = str(m.get("ver") or "?"), int(m.get("code") or 0)
    except Exception:
        try:    # 回退模板 manifest（未 sed，版本偏旧——仅 apk.json 缺失时兜底）
            mf = (_android_dir() / "AndroidManifest.xml").read_text()
            code = int(re.search(r'android:versionCode="(\d+)"', mf).group(1))
            ver = re.search(r'android:versionName="([^"]+)"', mf).group(1)
        except Exception:
            pass
    CH = 192 * 1024
    n = 0
    _APK_INFLIGHT[product] = (st0.st_mtime_ns, st0.st_size)
    try:
        for i in range(0, len(blob), CH):
            await _apk_send(ws, lock, {"t": "apk", "n": n, "b64": _b64.b64encode(blob[i:i + CH]).decode()})
            n += 1
        await _apk_send(ws, lock, {"t": "apk_end", "n": n, "code": code, "ver": ver,
                                   "product": product,
                                   # 自证：中继拿包名核对槽，对不上一律拒收
                                   "pkg": _apk_pkg(apk),
                                   "sha256": hashlib.sha256(blob).hexdigest()})
        st = _apk_state_load()
        d = st.get(product) or {}
        d["ok"] = {"mtime_ns": st0.st_mtime_ns, "size": st0.st_size}
        d.pop("fail_at", None)
        d.pop("fail_n", None)
        st[product] = d
        _apk_state_save(st)
        print("[relay] %s APK 已推中继 code=%s ver=%s size=%d" % (product, code, ver, len(blob)),
              flush=True)
        return (st0.st_mtime_ns, st0.st_size)
    except (asyncio.CancelledError, Exception):
        _apk_note_fail(product)   # 推送没走完：退避期内别再重推同一份
        raise
    finally:
        _APK_INFLIGHT.pop(product, None)


async def _apk_watch(ws, lock):
    """链路常驻时 APK 可能被重新 build：先立即推一次（连接即新），之后每
    APK_WATCH_S 秒对一次各线 stat，变了就补推该线——中继副本最迟一分钟
    追平最新构建，不再依赖断线重连才刷新。"""
    last = await _push_apk(ws, lock)
    # 连着的是这条连接的 ws：断了就收手。否则旧 watcher 拿死 ws 每轮醒一次、
    # 每次记一次推送失败刷退避，新连接永远推不上去（2026-11-18）。
    while not ws.closed:
        await asyncio.sleep(APK_WATCH_S)
        if ws.closed:
            return
        for p in branding.PRODUCTS:
            try:
                apk = _android_dir() / "build" / p / (p + "-terminal.apk")
                if not apk.exists():
                    continue
                st = apk.stat()
                if (st.st_mtime_ns, st.st_size) == last.get(p):
                    continue
            except OSError:
                continue
            try:
                r = await _push_apk_one(ws, lock, p)
                if r:
                    last[p] = r
            except Exception:
                pass


async def _push_ios(ws, lock):
    """连上中继就把 iOS 客户端包（.ipa）推过去落盘：iOS 壳的「检查更新」从
    此由中继本地答，开发机离线也能更新。装法跟安卓不一样——iOS 只认
    itms-services 清单：中继 /ios-install 给安装页，/ios.plist 指到 .ipa。
    ver/code 取自构建产物的 Info.plist（和壳里显示的是同一个数）。
    成功返回所推文件的 (mtime_ns, size)（_ios_watch 拿它对比是否需要补推）。"""
    import base64 as _b64
    try:
        ipa = HERE / "ios" / "build" / "codeboss.ios.ipa"
        if not ipa.exists():
            return None
        st0 = ipa.stat()
        if st0.st_size < 10240:
            return None
        why = _apk_skip_reason("ios", st0.st_mtime_ns, st0.st_size)
        if why:
            return None   # 跳过：中继已有这份（推过成功/退避期内）
        blob = ipa.read_bytes()
        st1 = ipa.stat()
        if (st1.st_mtime_ns, st1.st_size) != (st0.st_mtime_ns, st0.st_size):
            return None   # 读取期间文件还在变（正在 build），下轮再推
        ver, code = "?", 0
        try:
            import plistlib, zipfile
            # 版本以「真正要发出去的那个 .ipa」为准：Ad Hoc 走 archive + export 之后，
            # Build/Products 下那份 .app 还是上一次 build 的（实测会报 0.2.9 而包里是
            # 0.2.10，手机就永远看不到更新）。
            with zipfile.ZipFile(ipa) as z:
                info = plistlib.loads(z.read("Payload/Codeboss.app/Info.plist"))
            ver = str(info.get("CFBundleShortVersionString") or "?")
            code = int(info.get("CFBundleVersion") or 0)
        except Exception:
            try:
                import plistlib
                info = plistlib.loads((HERE / "ios" / "build" / "Build" / "Products" /
                                       "Release-iphoneos" / "Codeboss.app" / "Info.plist").read_bytes())
                ver = str(info.get("CFBundleShortVersionString") or "?")
                code = int(info.get("CFBundleVersion") or 0)
            except Exception:
                pass
        CH = 192 * 1024
        n = 0
        _APK_INFLIGHT["ios"] = (st0.st_mtime_ns, st0.st_size)
        try:
            for i in range(0, len(blob), CH):
                await _apk_send(ws, lock, {"t": "ios", "n": n, "b64": _b64.b64encode(blob[i:i + CH]).decode()})
                n += 1
            await _apk_send(ws, lock, {"t": "ios_end", "n": n, "code": code, "ver": ver})
            st = _apk_state_load()
            d = st.get("ios") or {}
            d["ok"] = {"mtime_ns": st0.st_mtime_ns, "size": st0.st_size}
            d.pop("fail_at", None)
            d.pop("fail_n", None)
            st["ios"] = d
            _apk_state_save(st)
            print("[relay] iOS 包已推中继 code=%s ver=%s size=%d" % (code, ver, len(blob)), flush=True)
            return (st0.st_mtime_ns, st0.st_size)
        except (asyncio.CancelledError, Exception):
            _apk_note_fail("ios")
            raise
        finally:
            _APK_INFLIGHT.pop("ios", None)
    except Exception as e:
        print("[relay] iOS 包推送失败: %s" % str(e)[:120], flush=True)
        return None


async def _ios_watch(ws, lock):
    """链路常驻时 .ipa 可能被重新构建：先立即推一次，之后定期 stat，变了就补推。"""
    last = await _push_ios(ws, lock)
    while not ws.closed:
        await asyncio.sleep(APK_WATCH_S)
        if ws.closed:
            return
        try:
            ipa = HERE / "ios" / "build" / "codeboss.ios.ipa"
            if not ipa.exists():
                continue
            st = ipa.stat()
            if (st.st_mtime_ns, st.st_size) == last:
                continue
        except OSError:
            continue
        last = await _push_ios(ws, lock) or last


_HOST_LINK_IDLE_S = 120   # 链路多久没收到中继任何消息就判死。正常链路每分钟有 use_ack；
# 120s 毫无来讯 = 只可能是静默黑洞

def _ws_pending(ws):
    """发送缓冲里还有多少字节没出去。拿不到就返回 -1（按「有积压」处理）。"""
    try:
        return ws._writer.transport.get_write_buffer_size()
    except Exception:
        return -1


async def _ws_recv_or_dead(ws, timeout, max_idle_s=6 * 3600):
    """收一条 ws 消息；判死条件 = 超时无来讯 **且** 发送缓冲有积压。

    2026-11-18 事故一：aliyun→中继的 ws 被中间设备静默黑洞（包被丢、无 RST），
    recv 永远阻塞，heartbeat 帧堆在 186KB 发不出去的缓冲区里，重连逻辑永远不
    触发——中继还以为它在线，手机切过去全部断网。判死后 abort 底层 transport。

    事故二（同日晚）：中继平时不给 host 发任何消息，只看「无来讯」会把健康
    空闲链路每 120s 误杀一次（全部设备每 2 分钟重连一轮）。空闲 ≠ 死亡：
    缓冲空说明没有东西卡着，继续等就好。

    事故三（2026-11-23 新加坡机）：上限原来设 600s——中继只在 host 报用量时
    才发 use_ack，空着的机器一句话都不来，于是**健康链路每 10 分钟被自己掐
    一次**（日志 426 次空原因断开、间隔恰好 10:03，手机每次断几秒到几十秒，
    发消息撞上就失败）。真正的判死信号是发送缓冲积压（黑洞）；空闲的链路交给
    aiohttp 的 heartbeat（30s 无 PONG 自己会断），这里的小时级上限只是兜底。"""
    waited = 0.0
    while True:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout)
            break
        except asyncio.TimeoutError:
            waited += timeout
            if _ws_pending(ws) == 0 and waited < max_idle_s:
                continue     # 只是闲着，没积压：继续等
            try:
                ws._writer.transport.abort()   # aiohttp 私有面；拿不到就靠下面的 close 兜底
            except Exception:
                pass
            try:
                await asyncio.wait_for(ws.close(), timeout=5)
            except Exception:
                pass
            raise
    if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
        # CLOSE/CLOSING/CLOSED/EOF/ERROR = 对端关闭或协议错，链路已终结。
        # 0.9.13 事故：中继重启发的 CLOSE 被外层当普通消息 continue，而 aiohttp
        # 对已关 ws 的 receive() 立即返回 CLOSED → while True 忙循环烧死事件循环
        # （aliyun 实测 81% CPU），重连/自动更新全饿死。`async for` 原本自动终止
        # 迭代，改 while True 后这层语义必须在收包处补上。abort 让所有阻塞在
        # 这条连接上的发送瞬间惊醒，再向外抛错走既有重连循环。
        try:
            ws._writer.transport.abort()
        except Exception:
            pass
        try:
            await asyncio.wait_for(ws.close(), timeout=5)
        except Exception:
            pass
        raise ConnectionError("relay 链路终结(%s)，走重连" % getattr(msg.type, "name", msg.type))
    return msg


async def _send_watch(ws, interval=5.0, stall_n=3):
    """上行黑洞自愈：发送缓冲连续 15s 一个字节都没排出去 → abort 走重连。

    2026-11-23 新加坡机 15:53–15:59 手机「根本连不上」就是这个：跨境上行被
    静默黑洞（下行正常），被卡住的那条 send 拿着锁发不出去，后续所有响应排队，
    中继侧 120s 后逐个 504。tunnel 请求帧照常进来，旧的收包看门狗
    （_ws_recv_or_dead，只在**收不到消息**时才查积压）完全看不见——链路活了
    6 分钟才靠别的重启恢复。

    判据用「冻结」而不是「有积压」：正常慢链路（跨境 ~17KB/s）大帧只是排得
    慢，缓冲在持续下降；TCP 被黑洞重传时写缓冲会完全冻住，一个字节都出不去。"""
    prev = -1
    stuck = 0
    while True:
        await asyncio.sleep(interval)
        n = _ws_pending(ws)
        if n <= 0:
            prev, stuck = -1, 0
            continue
        stuck = stuck + 1 if (prev > 0 and n >= prev) else 0
        prev = n
        if stuck >= stall_n:
            print("[relay] 发送缓冲 %d 字节冻结 %ds → 判上行黑洞，重连"
                  % (n, int(interval * (stuck + 1))), flush=True)
            try:
                ws._writer.transport.abort()
            except Exception:
                pass
            return


async def _host_link(app):
    """on_startup 挂钩：配置了 relay 才连；后台常驻，不阻塞启动。"""
    import aiohttp

    # 使用时长：不管有没有接中继都要定期落盘（离线也要能计数），链路在就顺带补报。
    app["use_task"] = asyncio.create_task(_use_loop())

    cfg = _relay_cfg()
    url, dev, sec = cfg.get("url"), cfg.get("device_id"), cfg.get("device_secret")
    if not (url and dev and sec):
        return

    async def _run():
        backoff = 2
        fails = 0
        while True:
            tasks = set()   # 本次连接起的子任务；连接结束必须在 finally 里全部取消
            u0 = (_relay_cfg().get("url") or url).rstrip("/")
            link = (u0.replace("https://", "wss://").replace("http://", "ws://")
                    + "/link?dev=%s&sec=%s&fp=%s" % (dev, sec, plat.machine_fp()))
            try:
                async with aiohttp.ClientSession() as cs, \
                        aiohttp.ClientSession() as local, \
                        cs.ws_connect(link, heartbeat=30) as ws:
                    print("[relay] 已连接 %s（dev=%s）" % (u0, dev[:8]), flush=True)
                    _HOST_ST["relay"] = True
                    _HOST_ST["unbound"] = False
                    backoff = 2
                    fails = 0
                    lock = asyncio.Lock()
                    _LINK["ws"], _LINK["lock"] = ws, lock
                    bye = False
                    t = asyncio.create_task(_apk_watch(ws, lock))   # 连上即推 APK，且 build 后自动补推
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    t = asyncio.create_task(_ios_watch(ws, lock))   # iOS .ipa 同款（老 relay 不认识会忽略）
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    async def _send_direct():
                        # _lan_ips 会解析主机名（macOS 走 mDNS，可能卡十几秒）——
                        # 决不能在事件循环线程里直接调，整台 host 都会跟着卡。
                        loop_ = asyncio.get_running_loop()
                        lan, rtc = await asyncio.gather(
                            loop_.run_in_executor(None, _lan_ips), loop_.run_in_executor(None, _rtc_avail))
                        async with lock:
                            await ws.send_json({
                                "t": "direct", "lan": lan, "port": PORT,
                                "rtc": rtc,
                                # 打不开 P2P 时把原因带过去：手机上原来只有一句
                                # 「aiortc 不可用」，分不出是没装、装坏了、还是架构不匹配。
                                "rtc_why": "" if rtc else (_RTC.get("why") or ""),
                                "ver": APP_VERSION})

                    async def _direct_state():
                        loop_ = asyncio.get_running_loop()
                        lan = await loop_.run_in_executor(None, _lan_ips)
                        return (_rtc_avail(), tuple(lan))

                    try:   # 上报局域网直连候选（relay 只牵线；老 relay 不认识 direct 帧会忽略）
                        await _send_direct()
                    except Exception:
                        pass
                    # 状态变了要**重发**：以前只在连上那一刻报一次，而 aiortc 是后台补装的
                    # ——补装完成的瞬间 rtc 变成 true，但 relay 手里还是那份 rtc=false，
                    # 手机就一直显示「开发机没启用 P2P」，直到 host 重连（可能是几天后）。
                    _dstate = await _direct_state()

                    async def _direct_watch():
                        nonlocal _dstate
                        while True:
                            await asyncio.sleep(20)
                            cur = await _direct_state()
                            if cur == _dstate:
                                continue
                            _dstate = cur
                            try:
                                await _send_direct()
                                print("[rtc] 直连能力变化，已重新上报（rtc=%s）" % cur[0], flush=True)
                            except Exception:
                                return

                    t = asyncio.create_task(_direct_watch())
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)

                    # ---- 远程日志上报：默认关闭，后台（中继）给这台开发机打开才推 ----
                    # 排查用户机器上的问题时，不用让用户找文件、发微信：后台点开，
                    # 这台机每 15 秒把 server.log 尾部推过去；点关就停。
                    _LOGS_ON["on"] = False     # 每次连上都等中继确认，避免连错线一直推

                    async def _logs_once():
                        txt = _log_tail()
                        if not txt:
                            return
                        async with lock:
                            await ws.send_json({"t": "logs", "txt": txt, "ver": APP_VERSION})

                    async def _logs_watch():
                        while True:
                            await asyncio.sleep(15)
                            if not _LOGS_ON.get("on"):
                                continue
                            txt = _log_delta()      # 只推新增，小帧，不灌满慢链路
                            if not txt:
                                continue
                            try:
                                async with lock:
                                    await ws.send_json({"t": "logs", "txt": txt,
                                                        "ver": APP_VERSION})
                            except Exception:
                                return

                    t = asyncio.create_task(_logs_watch())
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    # 上行黑洞看门狗：收包看门狗看不见「收得到、发不出去」的僵死链路
                    t = asyncio.create_task(_send_watch(ws))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    while True:
                        msg = await _ws_recv_or_dead(ws, _HOST_LINK_IDLE_S)
                        if msg.type != WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                        except ValueError:
                            continue
                        if d.get("t") == "rtc":   # 广域网 P2P：浏览器 offer 到了，生成 answer
                            t = asyncio.create_task(_rtc_answer(ws, lock, local, d))
                            tasks.add(t)
                            t.add_done_callback(tasks.discard)
                            continue
                        if d.get("t") == "bye":   # relay 侧删除了本机：注销并停重连
                            bye = True
                            break
                        if d.get("t") == "cancel":   # relay：浏览器侧断开，掐掉本地流式处理
                            ev = _PUSH_STREAMS.pop(str(d.get("id")), None)
                            print("[push] %s cancel 收到 rid=%s → %s"
                                  % (time.strftime("%m-%d %H:%M:%S"), d.get("id"),
                                     "已掐" if ev else "无此流(已结束)"), flush=True)
                            if ev:
                                ev.set()
                            continue
                        if d.get("t") == "presence":   # relay：当前连着的客户端数（区分 App / 浏览器）
                            try:
                                _HOST_ST["clients"] = int(d.get("n") or 0)
                                _HOST_ST["apps"] = int(d.get("app") or 0)
                                _HOST_ST["webs"] = int(d.get("web") or 0)
                            except (TypeError, ValueError):
                                pass
                            continue
                        if d.get("t") == "use_ack":   # relay：额度同步 + 已入库分钟的确认
                            _use_ack(d)
                            continue
                        if d.get("t") == "update":   # relay：后台「立即更新」按钮
                            print("[update] 中继要求立即检查更新", flush=True)
                            t = asyncio.create_task(_admin_update())
                            tasks.add(t)
                            t.add_done_callback(tasks.discard)
                            continue
                        if d.get("t") == "logs_on":   # relay：后台打开/关闭这台机的日志上报
                            on = bool(d.get("on"))
                            _LOGS_ON["on"] = on
                            agent_codex.DBG[0] = on   # 子模块的细日志也同一个开关
                            plat.DBG[0] = on
                            print("[logs] 中继%s日志上报" % ("开启了" if on else "关闭了"), flush=True)
                            if on:
                                t = asyncio.create_task(_logs_once())
                                tasks.add(t)
                                t.add_done_callback(tasks.discard)
                            continue
                        if d.get("t") == "takeover":
                            # 本机已被别的账号拿码：默认不改归属，本机弹窗让在场的人确认
                            t = asyncio.create_task(_tk_ask(d))
                            tasks.add(t)
                            t.add_done_callback(tasks.discard)
                            continue
                        if d.get("t") != "req":
                            continue
                        t = asyncio.create_task(_relay_serve(ws, lock, local, d))
                        tasks.add(t)
                        def _serve_done(t):   # 异常打出来：静默 GC 会让链路死得连个痕迹都没有
                            try:
                                e = t.exception()
                            except asyncio.CancelledError:
                                return
                            if e:
                                print("[relay] serve 异常: %s" % str(e)[:160], flush=True)
                            tasks.discard(t)
                        t.add_done_callback(_serve_done)
                    if bye:
                        _relay_wipe("relay 侧已删除本机")
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _link_deny_wipe(getattr(e, "status", None)):
                    _relay_wipe("relay 拒绝设备凭证（HTTP %s）" % getattr(e, "status", ""))
                    return
                if getattr(e, "status", None) == 409:          # 设备还没绑定账号
                    if not _HOST_ST.get("unbound"):
                        print("[relay] 这台开发机还没绑定账号，暂不接入中继；手机配对后自动连上", flush=True)
                    _HOST_ST["unbound"] = True
                    backoff = 5    # 配对完成后要尽快接上，别等 120s
                else:
                    print("[relay] 链路断开: %s（%ds 后重连）" % (str(e)[:120], backoff), flush=True)
                    # 连续三次都连不上就换备用域名再试（同一个中继、同一张证书）：
                    # 新域名解析失败/证书校验不过时，起码别让这台开发机整段离线。
                    fails += 1
                    if fails >= 3:
                        fails = 0
                        _RELAY_ALT["on"] = not _RELAY_ALT["on"]
                        print("[relay] 改试 %s" % _relay_cfg().get("url"), flush=True)
            finally:
                # 这条连接起的 watcher 必须跟着连接一起死：留着 = 拿死 ws 每轮记一次
                # 推送失败，退避永远清零不了，新连接推不上去（2026-11-18 APK 卡死根因）
                for t in list(tasks):
                    t.cancel()
                _apk_mark_inflight_fail()   # 链路死时还在推的包：记失败退避，下次连上别再推
                _HOST_ST["relay"] = False   # 断链即「断开」，重连过程不对外显示
                _HOST_ST["clients"] = 0
                if _LINK.get("ws") is not None and _LINK["ws"].closed:
                    _LINK["ws"], _LINK["lock"] = None, None   # 断链期间分钟留在 use_pending
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    app["host_link_task"] = asyncio.create_task(_run())


def _relay_enroll(base, name=None, as_json=False):
    """CLI：python3 server.py --enroll <relay_base> [机器名] [--json]
    向 relay 登记本机 → device_id/secret 写进 config.json → 打印配对 URL 与二维码。
    --json：只输出一行 JSON（Mac 客户端解析用），不打 ASCII 二维码。"""
    import socket
    import urllib.request
    base = base.rstrip("/")
    name = name or socket.gethostname()

    # 幂等：同一台中继已经登记过，就不再申请新设备——重装/重复跑安装脚本不该换
    # device_id（换了手机上的设备记录就失效，等于要重新配对）。直接沿用原设备换张码。
    _cur = _relay_cfg()
    if ((_cur.get("url") or "").rstrip("/") == base and _cur.get("device_id")
            and _cur.get("device_secret")):
        if not as_json:
            print("这台机器已经登记过（device_id=%s），沿用原设备" % _cur["device_id"])
        if _relay_paircode(as_json) == 0:
            return 0
        if not as_json:
            print("换码失败，重新登记新的设备…")

    def _out(ok, **kw):
        if not as_json:
            return
        d = {"ok": ok}
        d.update(kw)
        print(json.dumps(d, ensure_ascii=False), flush=True)

    req = urllib.request.Request(
        base + "/r/enroll", data=json.dumps(
            # fp：本机硬件指纹（SHA-256 摘要）。中继按它认「同一台开发机」，
            # 重装/删 config 重新登记不会重置免费额度；旧中继忽略这个字段。
            {"name": name, "product": _host_product(),
             "fp": plat.machine_fp(),
             # 已绑定账号的设备换 secret 要旧 secret；没用过/没配过的传空
             "old_secret": (_cur.get("device_secret") or "")}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        print("登记失败：%s（relay 地址可达？%s）" % (e, base))
        _out(False, error="登记失败：%s" % str(e)[:120])
        return 1
    if not d.get("ok"):
        print("登记被拒：%s" % d.get("error"))
        _out(False, error=d.get("error") or "登记被拒")
        return 1
    cfg = _load_cfg()
    cfg["relay"] = {"url": base, "device_id": d["device_id"],
                    "device_secret": d["device_secret"]}
    (HERE / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    pair = _pair_full(base, d["pair_url"])
    if as_json:
        _out(True, relay=base, device_id=d["device_id"],
             pair_code=d["pair_code"], pair_url=pair, name=name)
        return 0
    print("已登记：device_id=%s（secret 已写入 config.json，勿外传）" % d["device_id"])
    # 配对 URL 不打印：它带着中继域名（用户面对的是「掏手机扫码」，不是网址）。
    # 二维码里仍然是这个 URL，扫一下就绑上；要 URL 的（Mac 客户端）走 --json。
    print("配对码（15 分钟有效）：%s" % d["pair_code"])
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(pair)
        q.print_ascii(invert=True)
    except Exception:
        pass
    if plat.IS_MAC:
        print("重启 codeboss 服务后生效：launchctl kickstart -k gui/$(id -u)/com.xuxingyu.codeboss")
    else:
        print("重启 codeboss 服务后生效：systemctl --user restart codeboss")
    return 0


def _relay_paircode(as_json=False):
    """CLI：python3 server.py --paircode [--json]  已登记的开发机换一张新配对码。
    走 relay /r/devpair（device_id+secret 自证），旧的一次性码作废。"""
    import urllib.request
    relay = _relay_cfg()
    base = (relay.get("url") or "").rstrip("/")
    dev = relay.get("device_id") or ""
    sec = relay.get("device_secret") or ""
    if not (base and dev and sec):
        print("本机还没登记：先执行 server.py --enroll <relay_base_url> [机器名]")
        if as_json:
            print(json.dumps({"ok": False, "error": "本机还没登记"}, ensure_ascii=False), flush=True)
        return 1
    req = urllib.request.Request(
        base + "/r/devpair",
        data=json.dumps({"dev": dev, "sec": sec}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        print("换码失败：%s" % str(e)[:120])
        if as_json:
            print(json.dumps({"ok": False, "error": "换码失败：%s" % str(e)[:120]},
                             ensure_ascii=False), flush=True)
        return 1
    if not d.get("ok"):
        print("换码被拒：%s" % d.get("error"))
        if as_json:
            print(json.dumps({"ok": False, "error": d.get("error") or "换码被拒"},
                             ensure_ascii=False), flush=True)
        return 1
    pair = _pair_full(base, d["pair_url"])
    if as_json:
        print(json.dumps({"ok": True, "relay": base, "device_id": dev,
                          "pair_code": d["pair_code"], "pair_url": pair},
                         ensure_ascii=False), flush=True)
        return 0
    print("新的配对码（15 分钟有效）：%s" % d["pair_code"])
    # 同上：配对 URL 不打印（带中继域名），二维码里已经包含它。
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(pair)
        q.print_ascii(invert=True)
    except Exception:
        pass
    return 0




async def _accept_guard_startup(app):
    """接住 asyncio 的「Accept failed」：Windows 上网络接口一换（VPN/网卡重连），
    监听 socket 会被系统作废，asyncio 把 socket 关掉就再也不收连接——进程还活着、
    8790 却连不上，托盘显示「已停止」、手机连不上（用户实测：启动服务报这串错后
    客户端提示启动超时）。这种状态无法在原地恢复（socket 已关闭，aiohttp 站点
    也不会重新 bind），只能立刻退出，交给守护进程重启一个新进程。
    退出码 75 = 可重试的临时故障：launchd KeepAlive / systemd Restart /
    Windows 计划任务 + 托盘看门狗 都会重新拉起。"""
    loop = asyncio.get_running_loop()
    prev = loop.get_exception_handler()
    done = []

    def _h(lp, ctx):
        if str(ctx.get("message") or "") == "Accept failed on a socket":
            if done:
                return
            done.append(1)
            print("[server] 监听 socket 被系统作废（%r），重启服务"
                  % (ctx.get("exception"),), flush=True)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            if IS_WIN and not _under_win_supervisor():
                # 老安装包没有循环守护：自己拉一个替代进程，别让服务就这么死了
                _win_spawn_successor()
                os._exit(0)
            os._exit(75)
        if prev:
            prev(lp, ctx)          # loop 级 handler 的签名是 (loop, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(_h)


async def _win_supervisor_startup(app):
    """老安装包升级：把一次性 run-server.vbs 换成循环守护版（下次拉起服务即生效）。"""
    _win_upgrade_supervisor()


def _startup_guard(fn, timeout=30):
    """把 on_startup 钩子包一层超时：不返回就取消，别让站点永远起不来。"""
    async def _hook(app):
        try:
            await asyncio.wait_for(fn(app), timeout)
        except asyncio.TimeoutError:
            print("[startup] %s 超过 %ds 没返回，已取消；站点继续启动"
                  % (getattr(fn, "__name__", "hook"), timeout), flush=True)
    _hook.__name__ = getattr(fn, "__name__", "hook")
    return _hook


def main():
    if IS_WIN:
        # 日志重定向到文件时 Windows 默认按 GBK 编码 stdout，中文/方块字符会抛
        # UnicodeEncodeError 把服务打死——统一按 UTF-8 写（errors=replace 兜底）
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    as_json = "--json" in sys.argv
    if "--hooks" in sys.argv:
        i = sys.argv.index("--hooks")
        return _hooks_cli(sys.argv[i + 1] if len(sys.argv) > i + 1 else "status")
    if "--paircode" in sys.argv:
        return _relay_paircode(as_json)
    if "--enroll" in sys.argv:
        i = sys.argv.index("--enroll")
        if len(sys.argv) <= i + 1:
            print("用法: server.py --enroll <relay_base_url> [机器名] [--json]")
            return 2
        rest = [a for a in sys.argv[i + 2:] if not a.startswith("--")]
        return _relay_enroll(sys.argv[i + 1], rest[0] if rest else None, as_json)
    _ts_logs()                  # 每行日志带日期时间（--json 等脚本模式在上面已 return）
    _log_rotate_at_start()      # server.log 超 20MB 先挪走（launchd 的 fd 轮不了）
    n = load_registry()
    if n:
        print("[codeboss] 恢复 %d 个失效会话，可在列表页「重新拉起」" % n, flush=True)
    # 反馈附件（截图/视频）走这个端口：默认 1MB 会把手机截图直接挡掉
    async def _main_loop_set(_app):
        global _MAIN_LOOP
        _MAIN_LOOP = asyncio.get_running_loop()
    app = web.Application(middlewares=[_errlog_mw, _slow_mw, _cors_mw, _direct_bytes_mw], client_max_size=32 * 1024 * 1024)
    app.on_startup.append(_main_loop_set)
    app.router.add_get("/api/ping", h_ping)
    app.router.add_get("/api/brand", h_brand)               # 系列/端名（免鉴权）
    app.router.add_get("/api/acct_products", h_acct_products)   # 账号能用哪些端
    # 兜住所有 OPTIONS：aiohttp 对没注册的方法在路由解析时就回 405，中间件根本
    # 不执行——跨源预检（relay 来源页面打局域网直连 POST）必挂。给了这条路，
    # 预检才能落进 _cors_mw 答成 204（真请求仍要过 token）。
    app.router.add_route("OPTIONS", "/{t:.*}", lambda r: web.Response(status=204))
    app.router.add_get("/api/host_stat", h_host_stat)
    app.router.add_post("/api/term_new", h_api_term_new)
    app.router.add_get("/api/terms", h_api_terms)
    app.router.add_post("/api/term_close", h_api_term_close)
    app.router.add_post("/api/term_mac", h_api_term_mac)
    # 局域网直连的设备管理代理（真身在中继；/d 切台仍走中继）
    app.router.add_get("/r/devices", h_r_devices)
    app.router.add_post("/r/pair", h_r_pair)
    app.router.add_post("/r/pair_status", h_r_pair_status)
    app.router.add_post("/r/takeover_list", h_r_takeover_list)
    app.router.add_post("/r/takeover_act", h_r_takeover_act)
    app.router.add_post("/r/dev_rename", h_r_dev_rename)
    app.router.add_post("/r/account_info", h_r_account_info)
    app.router.add_post("/r/account_send_code", h_r_account_send)
    app.router.add_post("/r/account_kick", h_r_account_kick)
    app.router.add_post("/r/dev_del", h_r_dev_del)
    app.router.add_get("/", handle_list)
    app.router.add_get("/s/{sid}", handle_term)
    app.router.add_get("/ws/{sid}", handle_ws)
    app.router.add_post("/new", handle_new)
    app.router.add_post("/kill/{sid}", handle_kill)
    app.router.add_post("/remove/{sid}", handle_remove)
    app.router.add_post("/respawn/{sid}", handle_respawn)
    app.router.add_get("/ext/{sid}", h_ext_page)
    app.router.add_get("/api/ext_sessions", h_ext_sessions)
    app.router.add_get("/api/ext_files", h_ext_files)
    app.router.add_get("/api/ext_code", h_ext_code)          # 带行号的一段源码
    app.router.add_get("/api/ext_artifact", h_ext_artifact)
    app.router.add_get("/api/ext_art_text", h_ext_art_text)   # 产出物编辑：拉全文
    app.router.add_post("/api/ext_art_edit", h_ext_art_edit)  # 产出物编辑：保存落 diff
    app.router.add_post("/api/ext_upload", h_ext_upload)
    app.router.add_get("/api/ext_diag", h_ext_diag)
    app.router.add_get("/api/ext_agents", h_ext_agents)
    app.router.add_post("/api/ext_sess_new", h_ext_sess_new)
    app.router.add_post("/api/ext_sess_fork", h_ext_sess_fork)
    app.router.add_post("/api/ext_sess_close", h_ext_sess_close)
    app.router.add_post("/api/ext_sess_del", h_ext_sess_del)
    app.router.add_post("/api/ext_title", h_ext_title)
    app.router.add_get("/rpc", h_app_rpc)      # 桌面版 App ↔ 共享内核（只回环）
    app.router.add_get("/api/ext_push", h_ext_push)
    app.router.add_get("/api/ext_push/{sid}", h_ext_push)
    app.router.add_get("/api/bg_push", h_bg_push)   # 安卓前台服务：只收提醒（中继不计时长）
    app.router.add_get("/api/sessions", h_api_sessions)
    app.router.add_get("/api/ext_msg/{sid}", h_ext_msg)
    app.router.add_get("/api/ext_screen/{sid}", h_ext_screen)
    app.router.add_get("/api/ext_cells/{sid}", h_ext_cells)
    app.router.add_post("/api/ext_send", h_ext_send)
    app.router.add_post("/api/ext_key", h_ext_key)
    app.router.add_post("/api/ext_dlg_pick", h_ext_dlg_pick)
    app.router.add_get("/api/ext_pending", h_ext_pending)
    # 工头（新界面）：灰度入口 + 三块板 + 一件活 + 验收；全部新增，老接口一个不改
    app.router.add_get("/api/boss/health", h_boss_health)
    app.router.add_get("/api/boss/board", h_boss_board)
    app.router.add_get("/api/boss/work/{sid}", h_boss_work)
    app.router.add_post("/api/boss/verdict", h_boss_verdict)
    app.router.add_post("/api/ext_write", h_ext_write)
    app.router.add_post("/api/ext_scan/{sid}", h_ext_scan)
    app.router.add_get("/api/ext_cmdhist/{sid}", h_ext_cmdhist)
    app.router.add_get("/api/ext_term_out/{sid}", h_ext_term_out)
    app.router.add_post("/api/ext_term_in/{sid}", h_ext_term_in)
    app.router.add_post("/api/ext_cmddel/{sid}", h_ext_cmddel)
    app.router.add_post("/api/ext_cmdrun/{sid}", h_ext_cmdrun)
    app.router.add_get("/api/ext_cmdruns", h_ext_cmdruns)
    app.router.add_get("/api/ext_cmdout/{sid}", h_ext_cmdout)
    app.router.add_get("/api/test_cards", h_test_cards)     # 测试红绿卡（A1）
    app.router.add_post("/api/test_run", h_test_run)        # 白名单重跑
    app.router.add_post("/api/cmd_run", h_cmd_run)          # 一键 lint/test/build（A2）
    app.router.add_get("/api/cmds", h_cmds)                 # 项目能跑什么（入口显隐）
    app.router.add_get("/api/apk_find", h_apk_find)         # 会话项目里构建出的 APK
    app.router.add_get("/api/apk_get", h_apk_get)           # 下载安装（壳走系统安装）
    app.router.add_get("/api/shot", h_shot)                 # 拍一张开发机屏幕
    app.router.add_get("/api/model_router", h_model_router)          # 模型路由总览
    app.router.add_get("/api/model_router/models", h_model_router_models)  # 可选模型清单
    app.router.add_post("/api/model_router/set", h_model_router_set)  # 切换模型
    app.router.add_get("/api/model_router/ui", h_model_router_ui)    # 路由浮层页
    app.router.add_get("/pv", h_pv_index)                    # 网页预览：在听的端口
    app.router.add_get("/pv/", h_pv_index)                   # 带尾斜杠也认（浮层里就这么打开）
    app.router.add_route("*", "/pv/{port}", h_pv)           # 反代到 127.0.0.1:<port>
    app.router.add_route("*", "/pv/{port}/{tail:.*}", h_pv)
    app.router.add_get("/pvfile/{sid}/{tail:.*}", h_pvfile)  # 电脑里的 HTML 当网页看
    app.router.add_get("/api/ext_web", h_ext_web)            # 产出物网页 → 预览地址（复用/起服务）
    app.router.add_get("/api/accept_summary", h_accept_summary)  # 验收三句摘要（A5）
    app.router.add_post("/api/deploy_run", h_deploy_run)         # 发布（A4）
    app.router.add_post("/api/deploy_rollback", h_deploy_rollback)  # 回滚（A4）
    app.router.add_get("/api/health_card", h_health_card)        # 健康卡（A4）
    app.router.add_get("/api/artifact_inbox", h_artifact_inbox)   # 产出物收件箱（B1）
    app.router.add_post("/api/ext_open", h_ext_open)
    app.router.add_get("/api/ext_file", h_ext_file)
    app.router.add_get("/api/ext_merr", h_ext_merr)
    app.router.add_post("/api/track", h_track)          # 直连页面的埋点 → 转中继
    app.router.add_get("/api/ext_stream/{sid}", h_ext_stream)
    app.router.add_get("/api/ext_live/{sid}", h_ext_live)
    app.router.add_get("/api/ext_snap/{sid}", h_ext_snap)
    app.router.add_post("/api/ext_migrate", h_ext_migrate)
    app.router.add_post("/api/ext_migrate/{sid}", h_ext_migrate_one)
    app.router.add_post("/api/ext_resume", h_ext_resume)
    app.router.add_post("/api/seen", h_seen)
    app.router.add_post("/api/ptt", h_ext_ptt)
    app.router.add_post("/api/ptt_fix", h_ptt_fix)
    app.router.add_post("/api/session_event", h_session_event)
    app.router.add_get("/agentboss.apk", h_apk)
    app.router.add_get("/codeboss.apk", h_apk)   # 旧路径别名：老缓存页面/书签不断链
    app.router.add_get("/api/apk_ver", h_apk_ver)
    app.router.add_get("/api/plan", h_plan)
    app.router.add_post("/api/pay_create", h_pay_create)
    app.router.add_post("/api/pay_status", h_pay_status)
    app.router.add_get("/api/ext_model", h_ext_model)
    app.router.add_get("/api/ext_pref", h_ext_pref)
    app.router.add_post("/api/ext_pref", h_ext_pref)
    app.router.add_get("/api/notify_hist", h_notify_hist)
    app.router.add_get("/api/task_ledger", h_task_ledger)
    app.router.add_post("/api/task_new", h_task_new)
    app.router.add_get("/api/search", h_search)
    app.router.add_post("/api/task_accept", h_task_accept)
    app.router.add_post("/api/task_view", h_task_view)
    app.router.add_post("/api/task_triage", h_task_triage)
    app.router.add_post("/api/notify_act", h_notify_act)
    app.router.add_post("/api/perm_answer", h_perm_answer)
    app.router.add_get("/api/say_demo", h_say_demo)  # 试听当前播报风格
    app.router.add_get("/api/say/{name}", h_say)   # 手机没 TTS 引擎时取整句合成好的语音
    app.router.add_post("/api/feedback", h_feedback)
    app.router.add_get("/api/feedback_list", h_feedback_list)
    app.router.add_get("/api/feedback_detail", h_feedback_detail)
    app.router.add_post("/api/feedback_reply", h_feedback_reply)
    app.router.add_get("/api/feedback_media/{id}/{name}", h_feedback_media)
    app.router.add_get("/api/page_ver", h_page_ver)
    pet.add_routes(app)   # 桌宠线：/api/pet/* + /pet/sim（自带 token 校验）
    app.router.add_post("/api/ext_model_switch", h_ext_model_switch)
    app.router.add_get("/api/ext_model_session", h_ext_model_session)
    app.router.add_post("/api/ext_model_session", h_ext_model_session_set)
    app.router.add_get("/api/version", h_version)
    app.router.add_post("/api/update", h_update)
    app.on_startup.append(_accept_guard_startup)   # 监听 socket 被系统掐掉时自杀，让守护重启
    app.on_startup.append(_open_menu_bar_app_startup)   # 更新只覆盖文件不跑 install.sh，这里补打开 App
    app.on_startup.append(_app_update_watch_startup)    # App 自更新没重启成功时，盘上版本变了就拉起来
    app.on_startup.append(_win_supervisor_startup)  # Windows：老安装包的启动脚本升级成循环守护
    app.on_startup.append(_shell_alias_startup)    # 手敲 claude 也走 wrapper（DMG 装的用户没人跑过 install.sh）
    app.on_startup.append(_auto_migrate_loop)
    app.on_startup.append(_auto_update_loop)
    app.on_startup.append(_push_sessions_loop)
    app.on_startup.append(_np_on_startup)
    app.on_startup.append(_reap_startup)       # 已退出的终端自动关掉，收回内存
    app.on_startup.append(_model_switch_loop)
    app.on_startup.append(_hooks_guard_loop)   # 提醒 hooks 被外部改写掉了自己接回来
    app.on_startup.append(_say_cache_loop)     # 合成语音缓存定期清（免得占盘）
    app.on_startup.append(_logs_gc_loop)       # logs/ansi 镜像日志按 7 天/300MB 清
    app.on_startup.append(_search_warm_loop_startup)   # 找会话索引：后台把存量转录补齐
    app.on_startup.append(_host_link)
    app.on_startup.append(pet.start)   # 桌宠导演：开 tick 循环 + 接管语音分流
    app.on_cleanup.append(pet.stop)
    # 启动钩子统一加超时（2026-11-21 事故）：on_startup 是逐个 await 的，任何一个不返回，
    # 站点就不会 bind、_host_link 也轮不到——进程活着但整台机离线。超时的钩子取消并记
    # 一笔，其余照常启动；谁也别想再用一个死循环把机队带走（0.9.13 之后第二次）。
    app.on_startup[:] = [_startup_guard(f) for f in app.on_startup]
    app.router.add_get("/static/{name}", h_static)
    # 记下自己的 PID：托盘靠它找到「还在跑但不听 8790」的僵尸（socket 被系统作废后
    # 进程不退、却占着 server.log 句柄，新进程连日志都打不开 → 客户端「启动超时」）。
    # 命令行里可能只有相对路径，PID 文件是唯一可靠的线索。
    try:
        pf = HERE / "logs" / "server.pid"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    _heal_keepalive_plist()
    _heal_systemd_unit()
    _run_when_port_free(app)


def _port_busy(port, host="0.0.0.0"):
    """真的去绑一下，而不是 connect 127.0.0.1。

    以前 connect 探测只问环回：占用者绑在别的地址（或环回上连不通）时就判「空闲」，
    起来绑 0.0.0.0 必失败 → 崩溃退出 → 守护拉起 → 再崩，日志里刷满 EADDRINUSE。
    试绑和 aiohttp 起服务是同一个动作（同样开 SO_REUSEADDR），结论不会错。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _same_self(s):
    """端口上那个实例算不算「同版本自己」：版本+产品线都对才算。
    统一仓两线同一份代码版本号相同——只比版本会让两条线在同机上互杀
    （谁后起谁退出）。老实例 ping 不带 p（空），落回只比版本。"""
    return bool(s) and s.get("v") == APP_VERSION and s.get("p", "") in ("", _host_product())


def _serving_version(port, timeout=1.5):
    """本机 8790 上是否已有一个健康的自己；返回它 /api/ping 报的 {v, p}，没有就 {}。

    只问 /api/ping：免鉴权、不做更新检查。以前用 /api/version，那个里头跑
    update_check（git fetch / gh api），这台机出网一慢就超时，探测永远是空 ——
    健康的同版本实例被当成「旧实例」杀掉，被守护拉起后再杀回去，互杀不止
    （2026-11-14 linux-test 实测：30 分钟重启 25 次，一半时间是死的）。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port,
                                    timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8")) or {}
            return {"v": d.get("v") or "", "p": d.get("p") or ""}
    except Exception:
        return {}


_LOG_POS = {"pos": 0}   # 已推给中继的日志字节偏移（跨重连保持）


def _log_delta(limit=8192):
    """只推上次之后新增的日志字节（最多 limit）。

    原来每 15s 重发 64KB 尾部：跨境慢链路（aliyun→上海实测 ~16KB/s）上
    队列被顶满，手机请求排在十几万字节后面直接超时——「切到 aliyun 就断网」。"""
    try:
        p = HERE / "logs" / "server.log"
        size = p.stat().st_size
        pos = _LOG_POS["pos"]
        if size < pos:                      # 日志被清/轮转：从头来
            pos = 0
        if size - pos > 65536:              # 落后太多：只补最后 64KB，中间跳过
            pos = size - 65536
        if size <= pos:
            return ""
        n = min(limit, size - pos)
        with open(p, "rb") as f:
            f.seek(pos)
            txt = f.read(n).decode("utf-8", "replace")
        _LOG_POS["pos"] = pos + n
        return txt
    except OSError:
        return ""


def _log_tail(limit=65536):
    """server.log 尾部（默认 64KB，约几百行）。「中继打开日志上报」时推给后台。"""
    try:
        with open(HERE / "logs" / "server.log", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


class _TsStream:
    """给每行日志加时间戳。launchd 直接把 stdout/stderr 追加进 logs/server.log，
    原来一行时间都没有——事后看不出先后顺序，中继后台拉走的日志也一样。"""
    __slots__ = ("_s", "_at")

    def __init__(self, s):
        self._s = s
        self._at = True          # 下一段输出落在行首

    def write(self, t):
        if not t:
            return 0
        n = 0
        for part in t.splitlines(True):
            if self._at:
                self._s.write(time.strftime("[%Y-%m-%d %H:%M:%S] "))
                self._at = False
            self._s.write(part)
            n += len(part)
            if part.endswith("\n"):
                self._at = True
        return n

    def flush(self):
        try:
            self._s.flush()
        except Exception:
            pass

    def __getattr__(self, k):
        return getattr(self._s, k)


def _ts_logs():
    """--json / --hooks 这类给脚本读的输出不能加前缀，所以只在真正起服务时装。"""
    for nm in ("stdout", "stderr"):
        s = getattr(sys, nm)
        if not isinstance(s, _TsStream):
            setattr(sys, nm, _TsStream(s))


def _heal_keepalive_plist():
    """老安装包的 launchd plist 是 KeepAlive=true：连干净退出都会被拉起来。
    自更新只换程序文件、不重跑 install.sh，所以在这里就地把这项改成
    SuccessfulExit=false——下次 launchd 读 plist（重启/重新登录）生效。
    ponytail: 不动 job 本身，bootout 会把正在服务的自己杀掉，不值得。"""
    if sys.platform != "darwin":
        return
    p = Path.home() / "Library" / "LaunchAgents" / "com.xuxingyu.codeboss.plist"
    try:
        s = p.read_text(encoding="utf-8")
        if "<key>KeepAlive</key><true/>" not in s:
            return
        p.write_text(s.replace(
            "<key>KeepAlive</key><true/>",
            "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"),
            encoding="utf-8")
    except OSError:
        pass


def _heal_systemd_unit():
    """Linux 版的 _heal_keepalive_plist：单元里写 Restart=always 的话，正常退出
    （同版本实例让路）也会被拉起来，变成「起了又退、退了又起」的空转；再叠上双方
    都会杀「旧实例」就是互杀不止。改成 on-failure——崩溃/非零退出（75）照旧拉起。
    自更新只换程序文件不重跑 install-linux.sh，所以在这里就地改。
    ponytail: 只认默认的 codeboss.service；改过 CB_LABEL 的机器要自己改一行。"""
    if not plat.IS_LINUX:
        return
    cands = [
        (Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
         / "systemd" / "user" / "codeboss.service", True),
        (Path("/etc/systemd/system/codeboss.service"), False),
    ]
    for p, user in cands:
        try:
            s = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if "Restart=always" not in s:
            continue
        try:
            p.write_text(s.replace("Restart=always", "Restart=on-failure"), encoding="utf-8")
            _run(["systemctl"] + (["--user"] if user else []) + ["daemon-reload"], timeout=10)
            print("[server] %s：Restart=always → on-failure（正常退出不再空转）" % p,
                  flush=True)
        except OSError as e:
            print("[server] 改 %s 失败：%s" % (p, str(e)[:80]), flush=True)


def _print_once(msg):
    """同一句已经出现在日志结尾就不再打：老 plist 会把重复实例每 10 秒拉一次。"""
    try:
        with open(HERE / "logs" / "server.log", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            if msg.encode("utf-8") in f.read():
                return
    except OSError:
        pass
    print(msg, flush=True)


def _port_holder_pid(port):
    """谁在监听这个端口（0 = 查不到）。mac/linux 用 lsof，Windows 用 netstat。"""
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                 text=True, timeout=8).stdout
            for line in out.splitlines():
                if (":%d" % port) in line and "LISTEN" in line.upper():
                    return int(line.split()[-1])
            return 0
        out = subprocess.run(["lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=8).stdout.split()
        return int(out[0]) if out else 0
    except Exception:
        return 0


def _is_own_server(pid):
    """这个 pid 是不是**本安装目录**的 server.py。

    只认命令行里同时有 server.py 和本目录路径：宁可判成「别人的程序」去换端口，
    也绝不误杀别的 Python/服务。"""
    if not pid:
        return False
    try:
        if IS_WIN:
            out = subprocess.run(["wmic", "process", "where", "ProcessId=%d" % pid,
                                  "get", "CommandLine", "/format:list"],
                                 capture_output=True, text=True, timeout=8).stdout
        else:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return False
    c = out.lower()
    return "server.py" in c and str(HERE).lower() in c


def _write_port_file(port):
    """当前实际端口写进 logs/port.txt：Mac 客户端/托盘的本地状态探测、安装脚本的二维码
    都按它来（默认仍是 8790，文件不存在时各处都回退到 8790）。"""
    try:
        p = HERE / "logs" / "port.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def _heal_hook_port():
    """换过端口的话，已生成的 hook.sh 里还是旧端口（提醒事件就上报不到了）：重写一份。
    hook.sh 是纯生成物，丢了也能由 --hooks install 再现。"""
    try:
        if ("127.0.0.1:%d/" % PORT) not in HOOK_SH.read_text(encoding="utf-8"):
            _write_hook_sh()
    except OSError:
        pass


def _run_when_port_free(app, timeout=20.0):
    """自更新/重启时旧进程可能还没放开端口：等它退出再绑。

    Windows 上没有循环守护的老安装包是「先起新进程再自杀」（_win_spawn_successor），
    新进程一起来就去绑端口，必输给还没退干净的旧进程——之前表现为「更新完服务起不来」。
    有守护时 run-server.vbs 会等 5 秒，但托盘/手动重启也可能撞上。mac/linux 的
    守护重启同样是先退后拉，偶尔也会抢。这里最多等 20 秒。

    等完还不让开的，分两种情况（2026-11-10 用户机器上就是前者刷了一整屏）：
      · 占用者是**本目录的 server.py**（旧版本卡住/僵尸）：结束它，自己接管——
        手机连的还是 8790，什么都不用改。
      · 占用者是**别的程序**：换到下一个空闲端口（8790+1…+10），写 logs/port.txt，
        客户端/托盘/二维码都按这个文件找端口。手机经中继拿到的端口是 host 自报的，
        不受影响。

    已经有一个**同版本**实例在服务（托盘/快捷方式重复打开了第二个，或 launchd 与
    手动进程并存）：安静退出，别去抢端口——抢输了就是崩溃 + 守护重启的无限循环。
    版本不同则继续等：多半是旧进程正在自更新，等它 execv 完就放开端口。"""
    global PORT
    t0 = time.time()
    waited = killed = False
    while _port_busy(PORT):
        v = _serving_version(PORT)
        if _same_self(v):
            _print_once("[server] %d 上已有同版本实例在服务（v%s），本进程退出" % (PORT, v["v"]))
            sys.stdout.flush()
            # 退出码 0：launchd 的 KeepAlive=SuccessfulExit=false 就不再拉它，
            # 否则这行日志会跟着重启刷屏。
            os._exit(0)
        if time.time() - t0 < timeout:
            if not waited:
                print("[server] 端口 %d 还被上一个进程占着，等它释放…" % PORT, flush=True)
                waited = True
            time.sleep(0.4)
            continue
        pid = _port_holder_pid(PORT)
        if pid and not killed and _is_own_server(pid):
            # 杀之前耐心再问一次版本：机器一忙 /api/version 也可能 >1.5s 才回，上面的
            # 快速探测就把健康的同版本实例当成「旧实例」杀了。被杀的那个又被守护拉起，
            # 起来再杀对方——两边互杀不止（2026-11-14 linux-test 实测：30 分钟重启 25 次，
            # 每 45 秒有 24 秒端口是死的）。
            v2 = _serving_version(PORT, timeout=8.0)
            if _same_self(v2):
                _print_once("[server] %d 上的实例只是慢，版本相同（v%s），本进程退出"
                            % (PORT, v2["v"]))
                sys.stdout.flush()
                os._exit(0)
            print("[server] 端口 %d 被旧实例（pid %d，v%s）占着，结束它接管"
                  % (PORT, pid, v2.get("v") or v.get("v") or "?"), flush=True)
            _kill_proc(pid)
            killed = True
            t0 = time.time()          # 杀完再给它几秒退干净（端口要一会儿才放）
            continue
        if not _is_own_server(pid):
            new = next((p for p in range(PORT + 1, PORT + 11) if not _port_busy(p)), 0)
            if new:
                print("[server] 端口 %d 被其他程序占用（pid %s），改用 %d"
                      % (PORT, pid or "?", new), flush=True)
                PORT = new
                _heal_hook_port()
                break
        print("[server] 端口 %d 等了 %.0fs 仍被占用，直接启动（绑定可能失败）"
              % (PORT, timeout), flush=True)
        break
    _write_port_file(PORT)
    print("[codeboss] http://127.0.0.1:%d/ （token 见 token.txt，勿进日志）" % PORT, flush=True)
    if not IS_WIN:
        # 共享内核：host 起一套 codex app-server，桌面版 App 通过 /rpc 连同一个内核
        # （另一条腿在 App 端：CODEX_APP_SERVER_WS_URL=ws://127.0.0.1:PORT/rpc）
        _sp = codex_daemon.ensure_shared_server()
        print("[share] 共享内核 %s" % (_sp or "未启动（App 会自己拉内核，行为照旧）"), flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    sys.exit(main())
