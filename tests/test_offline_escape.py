#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""壳离线页的逃生通道:切到不可达的开发机被拦进网络页后,必须能回去。

2026-11-18 事故:切到 aliyun 开发机 → 误建直连 → 整页半死 → 壳断网页,
App 整个不能用,没有「切回原来开发机」的入口。钉两条:
  1. offline.html 有「返回」按钮,走 CB.back()
  2. 壳 Bridge.back() 真的 goBack()(只在 canGoBack 时)

跑法:.venv/bin/python -m pytest tests/test_offline_escape.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

HTML = (HERE / "clients" / "android" / "assets" / "offline.html").read_text(encoding="utf-8")
JAVA = (HERE / "clients" / "android" / "src" / "com" / "agentboss"
        / "terminal" / "MainActivity.java").read_text(encoding="utf-8")


def test_offline_page_has_back_button():
    assert "act('back')" in HTML, "离线页缺「返回」按钮"
    assert "返回上一台" in HTML, "按钮文案要点明是回上一台开发机"


def test_bridge_back_does_goback():
    assert "public void back()" in JAVA, "Bridge 缺 back()"
    # back 必须在 canGoBack 门后 goBack,且退出离线态(否则回到页面又被看门狗拉回来)
    i = JAVA.index("public void back()")
    body = JAVA[i:i + 700]
    assert "canGoBack()" in body
    assert "goBack()" in body
    assert "offlineShown = false" in body


# ── 2026-11-18 用户要求:断网页里要能切到别的开发机 ──────────────────
# 「返回上一台」只能沿 history 退一格(退回去还是那台坏的开发机页面),到不了
# 会话列表。所以两壳的断网页都要有「开发机列表」:直接开 <origin>/,
# 中继的 _pick_home 会落到在线的那台或出设备列表。

IOS_BRIDGE = (HERE / "clients" / "ios" / "Codeboss" / "Bridge.swift").read_text(encoding="utf-8")
IOS_MAIN = (HERE / "clients" / "ios" / "Codeboss" / "MainViewController.swift").read_text(encoding="utf-8")


def test_android_offline_page_has_device_list_button():
    assert "act('home')" in HTML, "离线页缺「开发机列表」入口"
    assert "开发机列表" in HTML, "按钮文案要写清楚是开发机列表"
    i = HTML.index("act('home')")
    # act() 里必须真的调 CB.home()
    j = HTML.index("function act(k)")
    body = HTML[j:j + 1200]
    assert "CB.home()" in body, "act('home') 没接 CB.home()"


def test_android_bridge_home_loads_list_page():
    assert "public void home()" in JAVA, "Android Bridge 缺 home()"
    i = JAVA.index("public void home()")
    body = JAVA[i:i + 1400]
    assert '"/?product="' in body or "origin()" in body, "home() 要回到中继根路径"
    assert "offlineShown = false" in body, "进列表要退出离线态,否则探测循环又拉回断网页"
    assert "loadUrl(" in body


def test_ios_bridge_has_home():
    assert "home: function(){ post({m:'home'}); }" in IOS_BRIDGE, "iOS JS 桥缺 home()"
    assert 'case "home":' in IOS_BRIDGE, "iOS 原生桥缺 home 消息处理"
    assert "onHome" in IOS_MAIN, "MainViewController 没接 onHome"
    i = IOS_BRIDGE.index("static let offlineHTML")
    assert "开发机列表" in IOS_BRIDGE[i:], "iOS 离线页缺「开发机列表」入口"
    assert "CB.home()" in IOS_BRIDGE[i:], "iOS 离线页按钮没接 CB.home()"
