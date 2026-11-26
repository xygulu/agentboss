# SPDX-License-Identifier: AGPL-3.0-or-later

"""匿名配对：没账号也能扫开发机二维码进隧道；已绑定账号的机器仍要登录。

跑法： .venv/bin/python -m pytest tests/test_relay_pair_anon.py -q
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, body, cookies=None):
        self.app = {"db": db, "key": b"k"}
        self._body = body
        self.cookies = cookies or {}
        self.remote = "127.0.0.1"
        self.scheme = "http"

    async def json(self):
        return self._body


def _db(tmp_path, owner=None, fp="a" * 64):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac','s',?,0,?,'codeboss',0)", (fp, owner))
    db.commit()
    return db


def _code(db, ttl=600):
    now = time.time()
    db.execute("INSERT INTO pair_codes(code,device_id,created,expires,used) "
               "VALUES('c1','d1',?,?,0)", (now, now + ttl))
    db.commit()


def _pair(db, code="c1", cookies=None):
    r = asyncio.run(relay.r_pair(_Req(db, {"code": code}, cookies)))
    return r, json.loads(r.text)


def test_anon_pair_without_login_sets_cookie(tmp_path):
    db = _db(tmp_path)
    _code(db)
    r, body = _pair(db)
    assert body == {"ok": True, "anon": True}
    tok = r.cookies[relay.DEV_SESS_COOKIE].value
    assert relay._dev_sess_read(db, tok) == "d1"
    assert db.execute("SELECT anon FROM devices WHERE id='d1'").fetchone()[0] == 1


def test_code_is_one_shot(tmp_path):
    db = _db(tmp_path)
    _code(db)
    _pair(db)
    r, body = _pair(db)
    assert r.status == 400 and body["ok"] is False


def test_owned_device_code_requires_login(tmp_path):
    db = _db(tmp_path, owner=7)
    _code(db)
    r, body = _pair(db)
    assert r.status == 401 and body["ok"] is False


def test_expired_code_400(tmp_path):
    db = _db(tmp_path)
    _code(db, ttl=-10)
    r, body = _pair(db)
    assert r.status == 400


def test_logged_in_pair_still_works(tmp_path, monkeypatch):
    db = _db(tmp_path, owner=7)
    _code(db)
    monkeypatch.setattr(relay, "uid_of", lambda r: 7)
    r, body = _pair(db)
    assert body == {"ok": True}
