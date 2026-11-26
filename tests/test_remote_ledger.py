# SPDX-License-Identifier: AGPL-3.0-or-later

"""跨机器台账（B2.1，host 侧）：本机台账 + 中继汇总的其他机器。

- `_relay_ledger_peers`：拿本机设备凭证问中继 /r/dev_ledger，10 秒缓存；
  没登记中继 / 中继不可达 / 超时 → []（工作台本机那半边照常）；
- `_merge_remote_ledger`：别的机器的行并进来，标 remote + machine，按最近动静排序；
  本机自己那台跳过（本机这份更新鲜，别出现两遍）。

跑法： .venv/bin/python -m pytest tests/test_remote_ledger.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _row(sid, ts=100):
    return {"sid": sid, "title": sid, "agent": "claude", "state": "running", "last_ts": ts}


def test_merge_marks_remote_rows():
    rows = [_row("L1", 50)]
    machines = [{"dev": "d2", "name": "macbook", "items": [_row("R1", 90)]}]
    out = server._merge_remote_ledger(rows, machines, "d1")
    assert [r["sid"] for r in out] == ["R1", "L1"], "新的在前"
    r = out[0]
    assert r["remote"] == 1 and r["machine"] == "macbook" and r["dev"] == "d2"


def test_merge_skips_own_machine():
    rows = [_row("L1", 50)]
    machines = [{"dev": "d1", "name": "mac-mini", "items": [_row("L1b", 60)]},
                {"dev": "d2", "name": "macbook", "items": [_row("R1", 90)]}]
    out = server._merge_remote_ledger(rows, machines, "d1")
    assert [r["sid"] for r in out] == ["R1", "L1"], "本机自己那份不要重复出现"


def test_merge_face_filters_remote_rows():
    rows = []
    machines = [{"dev": "d2", "name": "x", "items": [
        _row("R1"), {"sid": "H1", "agent": "hermes", "state": "running", "last_ts": 1}]}]
    out = server._merge_remote_ledger(rows, machines, "d1", face={"claude", "codex"})
    assert [r["sid"] for r in out] == ["R1"], "产品线能力面对异机同样生效"


@pytest.fixture
def relay_cfg(monkeypatch):
    monkeypatch.setattr(server, "_relay_cfg", lambda: {
        "url": "https://r", "device_id": "d1", "device_secret": "s"})
    server._REMOTE_LEDGER["at"] = 0
    server._REMOTE_LEDGER["data"] = []
    yield


def test_peers_calls_relay_and_caches(relay_cfg, monkeypatch):
    calls = []

    def fake_call(path, extra, timeout=6):
        calls.append(path)
        return {"ok": True, "machines": [{"dev": "d2", "name": "mb", "items": []}]}
    monkeypatch.setattr(server, "_relay_dev_call", fake_call)
    a = asyncio.run(server._relay_ledger_peers())
    b = asyncio.run(server._relay_ledger_peers())
    assert a[0]["dev"] == "d2" and b == a
    assert calls == ["/r/dev_ledger"], "10 秒内复用：台账是一页一页轮询的"


def test_peers_without_relay_is_empty(monkeypatch):
    monkeypatch.setattr(server, "_relay_cfg", lambda: {})
    assert asyncio.run(server._relay_ledger_peers()) == []


def test_ledger_skips_peer_fetch_for_relay_requests(tmp_path, monkeypatch):
    """中继来要「这台机器的本机台账」（remote=0）时不能再反过来问中继——
    否则两台机器互相要，递归到超时。"""
    import sqlite3
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    server._db_migrate(con)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))

    async def _sess(request):
        return []
    monkeypatch.setattr(server, "_ext_sessions_items", _sess)

    async def _boom():
        raise AssertionError("中继来要本机台账时不能再问中继")
    monkeypatch.setattr(server, "_relay_ledger_peers", _boom)

    class Req:
        headers = {}
        cookies = {}
        query = {"remote": "0"}
    assert asyncio.run(server._ledger_items(Req())) == []


def test_peers_survive_relay_failure(relay_cfg, monkeypatch):
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda p, e, timeout=6: {"ok": False, "error": "中继不可达"})
    assert asyncio.run(server._relay_ledger_peers()) == []
