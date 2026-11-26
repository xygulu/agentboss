#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""host 推包按产品线分文件分槽 + 配对 URL 产品线参数 + ping/端口守卫产品线维度。

统一仓两线同机共存（2026-11-18 发版前置）：每条线 host 推自己线的包落自己线
的槽；ping 带产品线，端口守卫「同版本自己」判定必须版本+产品线都对——
两线同一份代码同版本号，不分会互杀。

跑法： .venv/bin/python -m pytest tests/test_server_push_apk.py -q
asyncio 范式照 tests/test_server_debinary.py。
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def test_push_apk_uses_agentboss_filename(tmp_path, monkeypatch):
    (tmp_path / "android" / "build" / "agentboss").mkdir(parents=True)
    (tmp_path / "android" / "AndroidManifest.xml").write_text(
        '<manifest android:versionCode="2" android:versionName="0.1.0">')
    apk = tmp_path / "android" / "build" / "agentboss" / "agentboss-terminal.apk"
    apk.write_bytes(b"A" * 20480)
    (tmp_path / "android" / "build" / "agentboss" / "apk.json").write_text(
        '{"ver": "0.1.1", "code": 3}')     # 打包后真值优先于模板 manifest
    monkeypatch.setattr(server, "HERE", tmp_path)
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    sent = []

    class _Ws:
        async def send_json(self, d):
            sent.append(d)

    last = asyncio.run(server._push_apk(_Ws(), asyncio.Lock()))
    assert last.get("agentboss") is not None      # 推送成功（按线记录）
    end = [d for d in sent if d.get("t") == "apk_end"][-1]
    assert end["code"] == 3 and end["ver"] == "0.1.1"
    assert end["product"] == "agentboss"          # 尾帧带线：中继按包落槽
    assert any(d.get("t") == "apk" for d in sent)


def test_push_apk_pushes_both_lines(tmp_path, monkeypatch):
    """单 host 双产物：两线的包都在就两线都推（工头槽不再没人喂）。"""
    for p, code in (("agentboss", 3), ("codeboss", 190)):
        d = tmp_path / "android" / "build" / p
        d.mkdir(parents=True)
        (d / (p + "-terminal.apk")).write_bytes(bytes(p, "utf-8") * 20480)
        (d / "apk.json").write_text(json.dumps({"ver": "x", "code": code}))
    monkeypatch.setattr(server, "HERE", tmp_path)
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    sent = []

    class _Ws:
        async def send_json(self, d):
            sent.append(d)

    last = asyncio.run(server._push_apk(_Ws(), asyncio.Lock()))
    assert sorted(last) == ["agentboss", "codeboss"]
    ends = [d for d in sent if d.get("t") == "apk_end"]
    assert sorted((e["product"], e["code"]) for e in ends) == [("agentboss", 3), ("codeboss", 190)]


def test_pair_url_carries_product(monkeypatch):
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    assert server._pair_full("https://relay.example.com",
                             "/r/?p=abc") == "https://relay.example.com/r/?p=abc&product=agentboss"
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    assert server._pair_full("https://relay.example.com",
                             "/r/?p=abc") == "https://relay.example.com/r/?p=abc"


# ---- 统一仓语义：每条线推自己线的包（落自己线的槽，relay 按 dev 产品线分槽）----
# （原 I-5「codeboss 线不推包」在统一后反转：不推则工头槽永远停在存量 189）

def test_push_apk_pushes_codeboss_pkg(tmp_path, monkeypatch):
    (tmp_path / "android" / "build" / "codeboss").mkdir(parents=True)
    (tmp_path / "android" / "AndroidManifest.xml").write_text(
        '<manifest android:versionCode="2" android:versionName="0.1.0">')
    apk = tmp_path / "android" / "build" / "codeboss" / "codeboss-terminal.apk"
    apk.write_bytes(b"B" * 20480)
    (tmp_path / "android" / "build" / "codeboss" / "apk.json").write_text(
        '{"ver": "0.11.25", "code": 190}')
    monkeypatch.setattr(server, "HERE", tmp_path)
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    sent = []

    class _Ws:
        async def send_json(self, d):
            sent.append(d)

    last = asyncio.run(server._push_apk(_Ws(), asyncio.Lock()))
    assert sorted(last) == ["codeboss"]           # 只推在的那条线
    end = [d for d in sent if d.get("t") == "apk_end"][-1]
    assert end["code"] == 190 and end["product"] == "codeboss"


# ---- ping 带产品线 + 端口守卫「同版本自己」= 版本+产品线都对（两线同机不互杀）----

def test_ping_carries_product(monkeypatch):
    import types
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    monkeypatch.setattr(server, "_rtc_avail", lambda: True)
    req = types.SimpleNamespace(headers={}, query={})
    resp = asyncio.run(server.h_ping(req))
    import json as _json
    body = _json.loads(resp.text)
    assert body["p"] == "codeboss" and body["v"] == server.APP_VERSION


def test_same_self_needs_same_line(monkeypatch):
    monkeypatch.setattr(server, "APP_VERSION", "0.9.11")
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    assert server._same_self({"v": "0.9.11", "p": "codeboss"})       # 同版本同线=自己
    assert not server._same_self({"v": "0.9.11", "p": "agentboss"})  # 同版本异线=别的程序
    assert not server._same_self({"v": "0.9.10", "p": "codeboss"})   # 异版本=等更新/旧实例
    assert not server._same_self({})                                  # 探不到不算


# ---- 本地答包口（h_apk/_apk_meta）也按线分文件 ----

def test_apk_meta_by_product(tmp_path, monkeypatch):
    (tmp_path / "android" / "build" / "codeboss").mkdir(parents=True)
    (tmp_path / "android" / "build" / "codeboss" / "codeboss-terminal.apk").write_bytes(b"X" * 20480)
    (tmp_path / "android" / "build" / "codeboss" / "apk.json").write_text(
        '{"ver": "0.11.25", "code": 190}')
    monkeypatch.setattr(server, "HERE", tmp_path)
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    m = server._apk_meta()
    assert m["code"] == 190 and m["url"] == "/codeboss.apk"


# ---- I-1：host 没有本地安装包时问中继，必须带 product（否则 agentboss 壳被引导装 codeboss）----

def test_apk_ver_relay_fallback_carries_product(monkeypatch):
    import json, types

    captured = {}

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"ver": "9.9", "code": 5}          # 无 url → 走产品线缺省名

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Sess:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, **kw):
            captured["url"] = url
            return _Ctx()

    async def _noop(req):
        pass

    fake = types.SimpleNamespace(
        ClientSession=_Sess,
        ClientTimeout=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    monkeypatch.setattr(server, "require_token", _noop)
    monkeypatch.setattr(server, "_apk_meta", lambda product=None: None)
    monkeypatch.setattr(server, "_relay_cfg",
                        lambda: {"url": "https://relay.example", "device_id": "dev123"})
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    req = types.SimpleNamespace(headers={}, query={})
    resp = asyncio.run(server.h_apk_ver(req))
    assert captured["url"].startswith("https://relay.example/api/apk_ver")
    assert "product=agentboss" in captured["url"] and "dev=dev123" in captured["url"]
    assert json.loads(resp.text)["url"] == "https://relay.example/agentboss.apk"
