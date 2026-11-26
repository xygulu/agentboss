#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""安卓壳 CB.setBase 桥自检：python3 -m pytest tests/test_android_bridge.py -v
   登录页「使用自建服务器」靠它切服。桥挂在整个 WebView 上——任何页面都能调，
   所以：只收 http(s) + 原生 AlertDialog 确认（信任边界，不许省）。"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "clients", "android",
                   "src", "com", "agentboss", "terminal", "MainActivity.java")


def _src():
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _setbase_body(src):
    i = src.find("public void setBase(")
    assert i > 0, "MainActivity 里没有 setBase 桥方法"
    return src[i:i + 2000]


def test_setbase_bridge_exists():
    src = _src()
    i = src.find("public void setBase(")
    assert i > 0, "setBase 桥方法不存在"
    head = src[max(0, i - 200):i]
    assert "@JavascriptInterface" in head, "setBase 没挂 @JavascriptInterface，页面调不到"
    assert re.search(r"public void setBase\(final String url\)", src) is not None


def test_setbase_validates_scheme():
    body = _setbase_body(_src())
    assert '"http://"' in body and '"https://"' in body, \
        "setBase 没校验 scheme：javascript:/content: 会被当服务器地址"
    assert "startsWith" in body, "scheme 校验要用 startsWith（拒绝任意前缀注入）"


def test_setbase_confirms_natively():
    body = _setbase_body(_src())
    assert "AlertDialog" in body, "setBase 没有原生确认框：任何页面都能静默把壳切到自己的服务器"
    assert "KEY_URL" in body and "saveBase" in body, \
        "setBase 落盘不对：要写 KEY_URL（默认接入地址）+ saveBase（配对 base 与 cookie）"
