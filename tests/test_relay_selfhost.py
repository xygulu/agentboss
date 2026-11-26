#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""自建中继（RELAY_SELFHOST=1）能力协商自检：python3 -m pytest tests/test_relay_selfhost.py -v
   caps 是页面/host 隐藏官方专属入口的唯一事实源：官方=全开（fail-open），
   自建=配了依赖的亮（asr/llm/register），tts/分发/计费/后台强制关（spec 分线表）。"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import relay
from aiohttp import web


def test_caps_official_all_open(monkeypatch):
    monkeypatch.setattr(relay, "SELFHOST", False)
    monkeypatch.setattr(relay, "SMS_ON", True)
    monkeypatch.setattr(relay, "SMTP_HOST", "")
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    c = relay.caps_payload()
    assert c == {"selfhost": False, "register": True, "llm": True, "asr": True,
                 "tts": True, "plans": True, "promo": True, "apk": True,
                 "ios": True, "admin": True}


def test_caps_selfhost_forced_off(monkeypatch):
    # 配齐依赖的自建机：asr/llm/register 照配置亮；tts/分发/计费/后台强制关（spec 裁决 7）
    monkeypatch.setattr(relay, "SELFHOST", True)
    monkeypatch.setattr(relay, "SMS_ON", False)
    monkeypatch.setattr(relay, "SMTP_HOST", "smtp.x")
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    c = relay.caps_payload()
    assert c["selfhost"] is True and c["register"] is True
    assert c["llm"] is True and c["asr"] is True and c["tts"] is False
    assert c["plans"] is False and c["promo"] is False
    assert c["apk"] is False and c["ios"] is False and c["admin"] is False


def test_caps_register_sms_or_smtp(monkeypatch):
    monkeypatch.setattr(relay, "SELFHOST", True)
    monkeypatch.setattr(relay, "ASR_KEY", "")
    for sms, smtp, want in ((False, "", False), (True, "", True), (False, "smtp.x", True)):
        monkeypatch.setattr(relay, "SMS_ON", sms)
        monkeypatch.setattr(relay, "SMTP_HOST", smtp)
        assert relay.caps_payload()["register"] is want


def test_caps_route_registered():
    # 路由必须真注册（源里能找到，防「写了 handler 忘注册」）
    src = open(os.path.join(os.path.dirname(__file__), "..", "relay", "relay.py"),
               encoding="utf-8").read()
    assert 'add_get("/r/caps", r_caps)' in src


def test_selfhost_gated_truth_table():
    gated = ["/r/plans", "/r/plans_ver", "/r/promo", "/r/tts",
             "/api/plan", "/api/pay_create", "/api/pay_status",
             "/r/pay/create", "/r/pay/notify", "/r/pay/status",
             "/api/apk_ver", "/api/ios_ver", "/ios-install", "/ios.plist",
             "/codeboss.ipa", "/codeboss.apk", "/agentboss.apk",
             "/codeboss-host.tar.gz", "/codeboss-host-0.9.93.tar.gz",
             "/codeboss-mac.dmg", "/codeboss-linux.tar.gz",
             "/r/admin/board", "/r/admin_api/users", "/r/admin_api/orders"]
    keep = ["/r/admin_api/devices", "/r/admin_api/device"]
    core = ["/r/login", "/r/register", "/r/enroll", "/r/pair", "/r/terms",
            "/r/caps", "/r/update", "/r/feedback", "/api/ptt", "/api/llm",
            "/api/bg_ticket", "/api/direct_info", "/api/pet/turn"]
    for p in gated:
        assert relay._selfhost_gated(p), p
    for p in keep + core:
        assert not relay._selfhost_gated(p), p


def test_selfhost_gate_official_noop(monkeypatch):
    # 官方模式 middleware 原样放行（Review Focus 1：官方零变化）
    monkeypatch.setattr(relay, "SELFHOST", False)

    async def handler(request):
        return "passed"

    import asyncio
    assert asyncio.run(relay._selfhost_gate(object(), handler)) == "passed"


def test_selfhost_gate_selfhost_404(monkeypatch):
    # 自建模式：官方专属路径 404，核心路径放行
    monkeypatch.setattr(relay, "SELFHOST", True)
    import asyncio

    async def handler(request):
        return "passed"

    class _R:
        path = "/r/plans"
    try:
        asyncio.run(relay._selfhost_gate(_R(), handler))
        raise SystemError("/r/plans 在自建模式不该放行")
    except web.HTTPNotFound:
        pass

    class _R2:
        path = "/r/login"
    assert asyncio.run(relay._selfhost_gate(_R2(), handler)) == "passed"


# ---------- Task 3：页面改动 ----------

def _relay_src():
    return open(os.path.join(os.path.dirname(__file__), "..", "relay", "relay.py"),
                encoding="utf-8").read()


def test_login_page_selfhost_entry():
    src = _relay_src()
    assert "使用自建服务器" in src                     # 入口链接
    assert "CB.setBase" in src and "CB.scanQr" in src  # 桥调用（Task 6 提供桥）
    assert "window.CB&&CB.setBase" in src              # 浏览器（无桥）下入口隐藏


def test_register_page_no_channel_hint():
    src = _relay_src()
    assert "未配置验证码通道" in src
    # 提示必须挂在「短信和 SMTP 都没有」的条件下，不能无条件渲染
    assert re.search(r"SMS_ON\s+or\s+SMTP_HOST", src)


def test_plans_anchors_conditional_constants():
    # 「开通」购买锚必须挂在 SELFHOST 条件下——条件在渲染点（可 monkeypatch），
    # 常量本身保持原样字符串（官方模式逐字节不变）
    src = _relay_src()
    for frag in ('"__PLANS_A__", "" if SELFHOST else _ANON_PLANS_A',
                 '"__PLANS_A__", "" if SELFHOST else _ANONDEV_PLANS_A',
                 '"__PLANS_UP__", _LOCKED_NOPLANS if SELFHOST else _LOCKED_PLANS_A'):
        assert frag in src, frag[:40] + " 渲染点没挂 SELFHOST 条件"
    for name in ("_ANON_PLANS_A", "_ANONDEV_PLANS_A", "_LOCKED_PLANS_A"):
        assert name + " = " in src, name + " 常量不存在"


def test_anon_landing_official_vs_selfhost(monkeypatch):
    # 官方：购买锚在；自建：不出现，且占位符必须被替换干净（不许把 __PLANS_A__ 漏给用户）
    monkeypatch.setattr(relay, "SELFHOST", False)
    html = relay._anon_landing_html()
    assert "开通 Pro / Max" in html and "__PLANS_A__" not in html
    monkeypatch.setattr(relay, "SELFHOST", True)
    html2 = relay._anon_landing_html()
    assert "开通 Pro" not in html2 and "__PLANS_A__" not in html2
    assert "使用自建服务器" in html2            # 入口两边都在（用户能切回去）


# ---------- 终审修复：自建不限量 / 关停清单补全 / 扫码钩子清理 ----------

def test_selfhost_plan_limits_unlimited(monkeypatch):
    # 自建没有套餐运营：裸机不吃 _PLANS_DEFAULT 免费档（15 条/天就断 = 核心链路卡死）。
    # 全表口径 0 = 不限；台数交 RELAY_MAX_DEVICES（0 = 不设限）。
    monkeypatch.setattr(relay, "SELFHOST", True)
    lim = relay.plan_limits(None, 1)          # 自建分支不碰 db
    assert lim["sends_day"] == 0 and lim["llm_day"] == 0
    assert lim["asr_sec_day"] == 0 and lim["asr_sec_month"] == 0
    assert lim["hosts"] == relay.MAX_DEVICES
    monkeypatch.setattr(relay, "MAX_DEVICES", 0)
    assert relay.plan_limits(None, 1)["hosts"] == 0


def test_selfhost_plan_features_open(monkeypatch):
    # 免费档 features 把 terminal/remind/auto 锁死（16489 拦 terminal 路径）——
    # 自建功能不按档位锁，三个全开
    monkeypatch.setattr(relay, "SELFHOST", True)
    f = relay.plan_features(None, 1)
    assert f.get("terminal") is True and f.get("remind") is True and f.get("auto") is True


def test_gate_covers_official_family():
    # 终审 #4：路由表里同族官方专属端点不许漏（「藏起来但接口还在」是 spec 反目标）。
    # _selfhost_gated 是纯路径分类器（SELFHOST 判断在 middleware，official no-op
    # 已由 test_selfhost_gate_official_noop 盖住），这里只管分类全不全。
    for p in ("/api/host_ver", "/api/mac_ver", "/agentboss-941.apk",
              "/bosshost-mac.dmg", "/BossHost-Setup.exe", "/CodeBoss-Setup.exe"):
        assert relay._selfhost_gated(p), p


def test_scan_cancel_clears_hook():
    # 终审 #7：取消扫码后 _qrBack 必须清掉，否则下次配对扫码被劫持进已隐藏的错误区
    src = _relay_src()
    i = src.find("function showSelf(")
    assert i > 0 and "_qrBack=null" in src[i:i + 300], "showSelf(false) 没清 _qrBack"


def test_scheme_check_strict_case():
    # 终审 #8：页面 scheme 校验必须区分大小写——Java 侧 startsWith 是严格的，
    # 页面放 /i 会两头不一致（页面放行、壳静默无视 = 静默吞）
    src = _relay_src()
    assert "//i.test(" not in src, "页面 scheme 正则还挂着 i 标志（与壳不一致）"
    assert "//.test(" in src
