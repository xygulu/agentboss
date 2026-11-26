# SPDX-License-Identifier: AGPL-3.0-or-later

"""跨机器搜索：这台手机能搜到别的机器上的会话。

- host `_relay_search_peers`：拿本机设备凭证问中继 /r/dev_search，失败当没有；
- host `_merge_remote_search`：别的机器的命中并进来，标 remote + machine，
  本机自己那台跳过，按最近活跃排；
- 中继来要「本机结果」（remote=0）时不能再问中继——会互相递归到超时。

跑法： .venv/bin/python -m pytest tests/test_remote_search.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _hit(sid, last=100):
    return {"sid": sid, "title": sid, "agent": "claude", "last": last, "snips": ["..."]}


def test_merge_marks_remote_hits():
    machines = [{"dev": "d2", "name": "macbook", "items": [_hit("R1", 90)]}]
    out = server._merge_remote_search([_hit("L1", 50)], machines, "d1")
    assert [x["sid"] for x in out] == ["R1", "L1"], "新的在前"
    assert out[0]["remote"] == 1 and out[0]["machine"] == "macbook" and out[0]["dev"] == "d2"
    assert "remote" not in out[1]


def test_merge_skips_own_machine():
    machines = [{"dev": "d1", "name": "me", "items": [_hit("L1b", 60)]},
                {"dev": "d2", "name": "mb", "items": [_hit("R1", 90)]}]
    out = server._merge_remote_search([_hit("L1", 50)], machines, "d1")
    assert [x["sid"] for x in out] == ["R1", "L1"]


@pytest.fixture
def relay_cfg(monkeypatch):
    monkeypatch.setattr(server, "_relay_cfg", lambda: {
        "url": "https://r", "device_id": "d1", "device_secret": "s"})


def test_peers_ask_relay_with_query(relay_cfg, monkeypatch):
    calls = []

    def fake_call(path, extra, timeout=6):
        calls.append((path, extra))
        return {"ok": True, "machines": [{"dev": "d2", "name": "mb", "items": [_hit("R1")]}]}
    monkeypatch.setattr(server, "_relay_dev_call", fake_call)
    out = asyncio.run(server._relay_search_peers("部署"))
    assert out[0]["dev"] == "d2"
    assert calls == [("/r/dev_search", {"q": "部署"})]


def test_peers_without_relay_empty(monkeypatch):
    monkeypatch.setattr(server, "_relay_cfg", lambda: {})
    assert asyncio.run(server._relay_search_peers("x")) == []


def test_peers_survive_failure(relay_cfg, monkeypatch):
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda p, e, timeout=6: {"ok": False, "error": "不可达"})
    assert asyncio.run(server._relay_search_peers("x")) == []


def test_local_search_skips_peers_for_relay(monkeypatch):
    monkeypatch.setattr(server, "_search_all", lambda q, limit=30: [_hit("L1", 1)])

    async def _boom(q):
        raise AssertionError("中继来要本机结果时不能再问中继")
    monkeypatch.setattr(server, "_relay_search_peers", _boom)

    class Req:
        query = {"q": "部署", "remote": "0"}
    out = asyncio.run(server._search_everywhere(Req(), "部署"))
    assert [x["sid"] for x in out] == ["L1"]


def test_everywhere_merges_peers(monkeypatch):
    monkeypatch.setattr(server, "_search_all", lambda q, limit=30: [_hit("L1", 1)])
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"device_id": "d1"})

    async def _peers(q):
        return [{"dev": "d2", "name": "mb", "items": [_hit("R1", 5)]}]
    monkeypatch.setattr(server, "_relay_search_peers", _peers)

    class Req:
        query = {"q": "部署"}
    out = asyncio.run(server._search_everywhere(Req(), "部署"))
    assert [x["sid"] for x in out] == ["R1", "L1"]
