# SPDX-License-Identifier: AGPL-3.0-or-later

"""设备会话：免注册也能带身份进隧道，但只能代表自己这台开发机。

跑法： .venv/bin/python -m pytest tests/test_relay_dev_sess.py -q
"""
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
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,product,frozen) "
               "VALUES('d1','m','s','ff',1,'codeboss',0)")
    db.commit()
    return db


def _req(db, cookies):
    return types.SimpleNamespace(app={"db": db}, cookies=cookies)


def test_roundtrip(tmp_path):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    assert relay._dev_sess_read(db, tok) == "d1"


def test_tamper_rejected(tmp_path):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    bad = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    assert relay._dev_sess_read(db, bad) is None


def test_expired_rejected(tmp_path):
    db = _db(tmp_path)
    assert relay._dev_sess_read(db, relay._dev_sess_make(db, "d1", ttl=-10)) is None


def test_frozen_or_deleted_rejected(tmp_path):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    db.execute("UPDATE devices SET frozen=1 WHERE id='d1'")
    db.commit()
    assert relay._dev_sess_read(db, tok) is None


def test_principal_prefers_account(tmp_path, monkeypatch):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    monkeypatch.setattr(relay, "uid_of", lambda r: 7)
    uid, dev, anon = relay._tunnel_principal(_req(db, {"cb_dev_sess": tok}))
    assert (uid, dev, anon) == (7, None, False)


def test_principal_anonymous_device(tmp_path, monkeypatch):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    monkeypatch.setattr(relay, "uid_of", lambda r: None)
    uid, dev, anon = relay._tunnel_principal(_req(db, {"cb_dev_sess": tok}))
    assert (uid, dev, anon) == (None, "d1", True)


def test_principal_none(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "uid_of", lambda r: None)
    assert relay._tunnel_principal(_req(db, {})) == (None, None, False)


def test_account_endpoints_reject_dev_sess(tmp_path):
    """账号接口只认 r_sess：拿设备会话去冒充账号必须失败。"""
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    req = _req(db, {"cb_dev_sess": tok})
    req.app["key"] = b"k"
    assert relay.uid_of(req) is None


def test_tunnel_accepts_dev_sess_without_login(tmp_path):
    """只有设备会话 → 隧道放行（走到「开发机离线」而不是「先在 /r/ 登录」）。"""
    import asyncio

    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    req = types.SimpleNamespace(
        app={"db": db, "key": b"k", "links": {}},
        cookies={"cb_dev_sess": tok}, path="/d", method="GET",
        query={}, headers={}, scheme="http", remote="127.0.0.1",
        rel_url=types.SimpleNamespace(raw_path_qs="/d"),
    )
    res = asyncio.run(relay.tunnel(req))
    assert getattr(res, "status", 200) != 401
