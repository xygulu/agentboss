# SPDX-License-Identifier: AGPL-3.0-or-later

"""跨机器台账（B2.1，中继侧）：一台手机的「工作台」要看到同一个账号下
所有在线开发机上在干什么。

- `_link_api`：借 host-link 调一台开发机的任意 GET 接口（整包 req 帧，超时就 None）；
- `_dev_ledger_payload`：owner 名下在线的机器各要一份 /api/task_ledger，
  合并成 machines[]（每行带 machine/dev，前端据此标「在另一台机器上」）；
  离线机器不问；结果缓存 10 秒（工作台轮询一轮会同时问好几台）。

跑法： .venv/bin/python -m pytest tests/test_relay_ledger.py -q
"""
import asyncio
import base64
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    rows = [
        ("d1", "mac-mini", "s1", "f1", 0, "agentboss", 0, 7, 300),
        ("d2", "macbook", "s2", "f2", 0, "agentboss", 0, 7, 200),
        ("d3", "win", "s3", "f3", 0, "agentboss", 0, 7, 100),     # 离线：没有 link
        ("d4", "别人的", "s4", "f4", 0, "agentboss", 0, 8, 999),  # 别的账号：不问
    ]
    db.executemany("INSERT INTO devices(id,name,secret,fp,anon,product,frozen,owner,last_seen)"
                   " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return db


class _WS:
    """够用的假 link：send_json 时立刻把 pending 的 future 兑现。"""
    closed = False

    def __init__(self, app, payload):
        self.app = app
        self.payload = payload
        self.sent = []
        self._cb_lock = asyncio.Lock()

    async def send_json(self, frame):
        self.sent.append(frame)
        body = base64.b64encode(json.dumps(self.payload).encode()).decode()
        self.app["pending"][frame["id"]][0].set_result({"body": body})


def _app(links):
    return {"links": links, "pending": {}, "rid": 0}


def test_link_api_roundtrip():
    app = _app({})
    ws = _WS(app, {"ok": True, "items": [{"sid": "S1"}]})
    app["links"]["d1"] = ws
    d = asyncio.run(relay._link_api(app, "d1", "/api/task_ledger"))
    assert d["items"][0]["sid"] == "S1"
    assert ws.sent[0]["p"] == "/api/task_ledger" and ws.sent[0]["m"] == "GET"
    assert app["pending"] == {}


def test_link_api_offline_is_none():
    assert asyncio.run(relay._link_api(_app({}), "d1", "/api/task_ledger")) is None


def test_merges_online_machines_only(tmp_path, monkeypatch):
    db = _db(tmp_path)
    app = _app({})
    for dev, name in (("d1", "mac-mini"), ("d2", "macbook"), ("d4", "别人的")):
        app["links"][dev] = _WS(app, {"ok": True, "items": [
            {"sid": "sid-" + dev, "title": name, "state": "running", "last_ts": 5}]})
    d = asyncio.run(relay._dev_ledger_payload(app, db, 7, ttl=0))
    assert [m["dev"] for m in d["machines"]] == ["d1", "d2"], "离线/别人的机器不问"
    assert [m["name"] for m in d["machines"]] == ["mac-mini", "macbook"]
    it = d["machines"][0]["items"][0]
    assert it["machine"] == "mac-mini" and it["dev"] == "d1", "行要带机器，点开才知道去哪看"


def test_cached_within_ttl(tmp_path, monkeypatch):
    db = _db(tmp_path)
    app = _app({})
    calls = []

    async def fake_link(app_, dev, path, m="GET", body=None, secs=6):
        calls.append(dev)
        return {"ok": True, "items": []}
    monkeypatch.setattr(relay, "_link_api", fake_link)
    app["links"]["d1"] = type("L", (), {"closed": False})()
    asyncio.run(relay._dev_ledger_payload(app, db, 7, ttl=10))
    asyncio.run(relay._dev_ledger_payload(app, db, 7, ttl=10))
    assert calls == ["d1"], "工作台每几秒轮询一次，不能每轮都去敲机器的门"


def test_at_most_four_machines(tmp_path, monkeypatch):
    db = _db(tmp_path)
    for i in range(5, 10):
        db.execute("INSERT INTO devices(id,name,secret,fp,anon,product,frozen,owner,last_seen)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   ("x%d" % i, "m%d" % i, "s", "f%d" % i, 0, "agentboss", 0, 7, i))
    db.commit()
    app = _app({})
    for i in range(5, 10):
        app["links"]["x%d" % i] = type("L", (), {"closed": False})()

    async def fake_link(app_, dev, path, m="GET", body=None, secs=6):
        return {"ok": True, "items": []}
    monkeypatch.setattr(relay, "_link_api", fake_link)
    d = asyncio.run(relay._dev_ledger_payload(app, db, 7, ttl=0))
    assert len(d["machines"]) == 4
