# SPDX-License-Identifier: AGPL-3.0-or-later

"""登记按硬件指纹幂等：重装/删 config 再来登记，还是同一台设备，额度不重置。

跑法： .venv/bin/python -m pytest tests/test_relay_enroll_fp.py -q
"""
import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, body, ip):
        self.app = {"db": db}
        self._body = body
        self.remote = ip

    async def json(self):
        return self._body


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    return sqlite3.connect(p)


def _enroll(db, body):
    ip = "10.0.%d.%d" % (uuid.uuid4().int % 250, uuid.uuid4().int % 250)
    r = asyncio.run(relay.r_enroll(_Req(db, body, ip)))
    return json.loads(r.text)


def test_same_fp_reuses_device(tmp_path):
    db = _db(tmp_path)
    fp = "a" * 64
    a = _enroll(db, {"name": "m1", "product": "codeboss", "fp": fp})
    b = _enroll(db, {"name": "m1-重装", "product": "codeboss", "fp": fp})
    assert a["ok"] and b["ok"]
    assert a["device_id"] == b["device_id"]
    assert a["device_secret"] != b["device_secret"]
    n = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    assert n == 1
    assert db.execute("SELECT anon FROM devices").fetchone()[0] == 1


def test_new_fp_creates_device(tmp_path):
    db = _db(tmp_path)
    a = _enroll(db, {"name": "m1", "fp": "a" * 64})
    b = _enroll(db, {"name": "m2", "fp": "b" * 64})
    assert a["device_id"] != b["device_id"]


def test_enroll_without_fp_keeps_old_behavior(tmp_path):
    db = _db(tmp_path)
    a = _enroll(db, {"name": "old-client"})
    assert a["ok"]
    row = db.execute("SELECT fp, anon FROM devices").fetchone()
    assert row == ("", 0), "老客户端没有指纹：不要标匿名，保持配对流程"


def test_claimed_device_needs_old_secret_to_rebind(tmp_path):
    db = _db(tmp_path)
    fp = "c" * 64
    a = _enroll(db, {"name": "paid", "fp": fp})
    db.execute("UPDATE devices SET owner=7, anon=0 WHERE id=?", (a["device_id"],))
    db.commit()
    b = _enroll(db, {"name": "attacker", "fp": fp})
    assert b["ok"] is False
    assert "secret" in b["error"] or "解绑" in b["error"]


def test_claimed_device_rebind_with_old_secret(tmp_path):
    db = _db(tmp_path)
    fp = "d" * 64
    a = _enroll(db, {"name": "paid", "fp": fp})
    db.execute("UPDATE devices SET owner=7, anon=0 WHERE id=?", (a["device_id"],))
    db.commit()
    b = _enroll(db, {"name": "paid", "fp": fp, "old_secret": a["device_secret"]})
    assert b["ok"] and b["device_id"] == a["device_id"]
    assert db.execute("SELECT owner FROM devices WHERE id=?",
                      (a["device_id"],)).fetchone()[0] == 7, "换 secret 不改归属"
