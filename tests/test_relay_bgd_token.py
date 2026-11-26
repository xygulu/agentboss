# SPDX-License-Identifier: AGPL-3.0-or-later

"""设备版提醒凭据 cb_bgd：壳（Java）只能出示非 httponly cookie，而账号版 cb_bg
只在「中继登录态」下种；局域网直连配对出来的设备（只有设备会话）永远拿不到任何
壳能读的凭据 → /api/bg_push 每 60 秒 401、语音播报取不到音频（2026-11-19 S24 mini）。

规则（本测试钉住）：
1. 签名/过期/设备冻结/设备不存在 → 一律不认。
2. 认出来的是「设备」：owner 有就带上 uid，没有就是匿名设备（uid=None）。
3. 只对提醒那两条路（/api/bg_push、/api/say）生效，别的一概不算。
4. 种 cookie 时 ok：非 httponly（壳要读）、path=/api/（页面 JS 在 / 和 /ext/ 下读不到）。

跑法： .venv/bin/python -m pytest tests/test_relay_bgd_token.py -q
"""
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, path="/api/bg_push", cookies=None):
        self.app = {"db": db, "key": b"k"}
        self.path = path
        self.cookies = cookies or {}
        self.query = {}
        self.headers = {}
        self.scheme = "http"


def _db(tmp_path, owner=None, frozen=0):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac','s','a'*64,0,?,'codeboss',?)", (owner, frozen))
    db.commit()
    return db


class _Resp:
    def __init__(self):
        self.cookies = {}

    def set_cookie(self, k, v, **kw):
        self.cookies[k] = (v, kw)


def _cookie_val(db, dev="d1"):
    r = _Resp()
    relay.bgd_cookie(r, _Req(db), dev)
    return r.cookies[relay.BGD_COOKIE]


def test_bgd_roundtrip_anon_device(tmp_path):
    db = _db(tmp_path)
    val, kw = _cookie_val(db)
    assert kw["httponly"] is False, "壳读不到 httponly，这条令牌就没用了"
    assert kw["path"] == "/api/", "页面 JS 在 / 和 /ext/ 下不该读得到它"
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: val})) == (None, "d1")


def test_bgd_owned_device_returns_owner(tmp_path):
    db = _db(tmp_path, owner=7)
    val, _ = _cookie_val(db)
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: val})) == (7, "d1")


def test_bgd_rejects_bad_sig_and_expiry(tmp_path):
    db = _db(tmp_path)
    val, _ = _cookie_val(db)
    head, sig = val.rsplit(".", 1)
    bad = head + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: bad})) is None
    body = "bgd.d1.%d" % (int(time.time()) - 5)
    old = body + "." + __import__("hmac").new(b"k", body.encode(),
                                              __import__("hashlib").sha256).hexdigest()
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: old})) is None
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: ""})) is None


def test_bgd_rejects_frozen_or_gone_device(tmp_path):
    db = _db(tmp_path, frozen=1)
    val, _ = _cookie_val(db)
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: val})) is None
    db.execute("UPDATE devices SET frozen=0")
    db.execute("DELETE FROM devices WHERE id='d1'")
    db.commit()
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: val})) is None


def test_remind_auth_only_for_remind_paths(tmp_path):
    db = _db(tmp_path, owner=7)
    val, _ = _cookie_val(db)
    assert relay.remind_auth(_Req(db, cookies={relay.BGD_COOKIE: val})) == (7, "d1")
    say = _Req(db, path="/api/say", cookies={relay.BGD_COOKIE: val})
    assert relay.remind_auth(say) == (7, "d1")
    other = _Req(db, path="/api/ext_send", cookies={relay.BGD_COOKIE: val})
    assert relay.remind_auth(other) is None, "别把提醒凭据变成通用钥匙"
