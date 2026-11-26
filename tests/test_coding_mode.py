# SPDX-License-Identifier: AGPL-3.0-or-later

"""编码模式开关（窗口核心 Phase 2 Task 2）：默认跟设备产品线，显式开关落盘。

跑法： .venv/bin/python -m pytest tests/test_coding_mode.py -q
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


@pytest.fixture()
def pref_env(tmp_path, monkeypatch):
    """prefs.json 指到临时目录，coding 段清干净——别碰开发机的真偏好。"""
    monkeypatch.setattr(server, "_PREF_FILE", tmp_path / "prefs.json")
    server._PREF.pop("coding", None)
    yield tmp_path
    server._PREF.pop("coding", None)


def _get(path="/api/ext_pref"):
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request("GET", path)


def _post(path, obj):
    from aiohttp.streams import StreamReader
    from aiohttp.test_utils import make_mocked_request

    class _FakeProto:
        def __getattr__(self, k):
            return lambda *a, **kw: None

    sr = StreamReader(_FakeProto(), 2 ** 16)
    sr.feed_data(json.dumps(obj).encode())
    sr.feed_eof()
    return make_mocked_request("POST", path, payload=sr)


async def _ok(request):
    return None


def _body(resp):
    return json.loads(resp.body)


# ---- 默认值：存储值 ?? 产品线（codeboss 开 / agentboss 关）----

def test_default_codeboss_on(pref_env, monkeypatch):
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_client_product_for", lambda r: "codeboss")
    out = {}

    async def run():
        out["d"] = _body(await server.h_ext_pref(_get()))

    import asyncio
    asyncio.run(run())
    assert out["d"]["coding"]["on"] is True


def test_default_agentboss_off(pref_env, monkeypatch):
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_client_product_for", lambda r: "agentboss")
    out = {}

    async def run():
        out["d"] = _body(await server.h_ext_pref(_get()))

    import asyncio
    asyncio.run(run())
    assert out["d"]["coding"]["on"] is False


# ---- 显式开关：POST 落盘，GET 读回 ----

def test_explicit_off_persists(pref_env, monkeypatch):
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_client_product_for", lambda r: "codeboss")
    out = {}

    async def run():
        await server.h_ext_pref(_post("/api/ext_pref", {"dev": "d1", "coding_on": False}))
        out["d"] = _body(await server.h_ext_pref(_get("/api/ext_pref?dev=d1")))

    import asyncio
    asyncio.run(run())
    assert out["d"]["coding"]["on"] is False
    disk = json.loads((pref_env / "prefs.json").read_text(encoding="utf-8"))
    assert disk["coding"]["devs"]["d1"]["on"] is False


def test_explicit_on_beats_line_default(pref_env, monkeypatch):
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_client_product_for", lambda r: "agentboss")
    out = {}

    async def run():
        await server.h_ext_pref(_post("/api/ext_pref", {"dev": "d2", "coding_on": True}))
        out["d"] = _body(await server.h_ext_pref(_get("/api/ext_pref?dev=d2")))

    import asyncio
    asyncio.run(run())
    assert out["d"]["coding"]["on"] is True


# ---- 32 台上限：只留最近上报的 32 台（同 smart/voice 先例）----

def test_dev_cap_32(pref_env, monkeypatch):
    for i in range(40):
        server._coding_dev_set("dev%02d" % i, True)
    devs = server._coding_devs()
    assert len(devs) == 32
    assert "dev00" not in devs and "dev39" in devs   # 最老的被挤掉
