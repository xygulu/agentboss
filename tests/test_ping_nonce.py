#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""/api/ping 探针回声：页面带着 nonce 来，host 原样带回。

2026-11-18 事故：aliyun 开发机的机房内网 IP 探针被代理/运营商劫持答了 200，
页面误建局域网直连 → 全页半死。探针必须能验证「答话的是开发机本人」：
page 发 /api/ping?pr=<nonce>，host 把 pr 原样放进 JSON——伪造的 200 页面
给不出这个回声。老 host 不回 pr（字段缺失）由页面按 ok:1 兜底兼容。

跑法：.venv/bin/python -m pytest tests/test_ping_nonce.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _req(pr=None):
    q = {} if pr is None else {"pr": pr}
    return SimpleNamespace(query=q, headers={})


def _body(resp):
    return json.loads(resp.body)


def _ping(pr=None):
    return asyncio.run(server.h_ping(_req(pr)))


def test_valid_nonce_echoed():
    d = _body(_ping("a1b2c3d4e5"))
    assert d.get("pr") == "a1b2c3d4e5", "合法 nonce 必须原样回声"
    assert d.get("ok") == 1


def test_absent_nonce_omitted():
    assert "pr" not in _body(_ping()), "没带 nonce 就别回这个字段"


def test_dirty_nonce_not_echoed():
    for bad in ("<script>x</script>", "a" * 65, "XYZ-not-hex", "../../etc"):
        assert "pr" not in _body(_ping(bad)), "不合法的 nonce 不能回显（免鉴权接口防反射）：%r" % bad
