# SPDX-License-Identifier: AGPL-3.0-or-later

"""客户端 seat 与开发机数：没有 cb_cid 也要占位；降档后超额设备重连被拒。

跑法： .venv/bin/python -m pytest tests/test_seat_and_hosts.py -q
"""
import asyncio
import sqlite3
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.commit()
    return db


def _dev(db, did, owner=7, seen=0):
    db.execute("INSERT INTO devices(id,name,secret,last_seen,owner,product)"
               " VALUES(?,?,'s',?,?,'codeboss')", (did, did, seen, owner))
    db.commit()


def _req(db, path="/d"):
    return types.SimpleNamespace(
        app={"db": db, "key": b"k", "links": {}},
        cookies={"cb_dev": "d1"}, path=path, method="GET",
        query={}, headers={"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; Qin 2 Pro)"
                                         " CodebossShell/0.11.27"}, scheme="http",
        remote="203.0.113.9",
        rel_url=types.SimpleNamespace(raw_path_qs=path),
    )


def test_tunnel_without_cb_cid_occupies_seat(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _dev(db, "d1")
    monkeypatch.setattr(relay, "uid_of", lambda r: 7)
    monkeypatch.setattr(relay, "clients_gate", relay.clients_gate)   # 用真实现
    asyncio.run(relay.tunnel(_req(db)))
    rows = db.execute("SELECT cid FROM clients WHERE uid=7").fetchall()
    assert len(rows) == 1, "没有 cb_cid 的请求也必须占一个 seat"
    assert rows[0][0].startswith("i:")


def test_second_seat_blocked_on_free(tmp_path):
    db = _db(tmp_path)
    assert relay.clients_gate(db, 7, "i:a|UA1") is None
    relay.clients_touch(db, 7, "i:a|UA1")
    gate = relay.clients_gate(db, 7, "i:b|UA2")
    assert gate is not None and gate == (1, 1)


def test_anon_device_seat_is_counted(tmp_path):
    db = _db(tmp_path)
    subj = relay.subject_of(None, None, "ab12")
    assert relay.clients_gate(db, subj, "i:a|UA1") is None
    relay.clients_touch(db, subj, "i:a|UA1")
    assert relay.clients_gate(db, subj, "i:b|UA2") is not None


def test_downgrade_rejects_second_host(tmp_path):
    db = _db(tmp_path)
    now = time.time()
    _dev(db, "old", seen=now - 100)
    _dev(db, "new", seen=now)
    bad, msg = relay._hosts_gate(db, "old", 7)
    assert bad is False and "开发机" in msg, "最近活跃的 new 留下，旧的 old 被拒"
    assert relay._hosts_gate(db, "new", 7)[0] is True


def test_hosts_gate_within_cap(tmp_path):
    db = _db(tmp_path)
    _dev(db, "only")
    assert relay._hosts_gate(db, "only", 7)[0] is True
    assert relay._hosts_gate(db, "whatever", None)[0] is True   # 匿名设备不占账号名额
