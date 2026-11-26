# SPDX-License-Identifier: AGPL-3.0-or-later

"""壳取播报音频（/api/say/<id>）也要能在中继上认人：cookie 这条壳读不到，就带票据。

2026-11-19 S24 mini：壳的 Java 端 CookieManager.getCookie() 只回一个 cb_product
（19 字节），bg_push 与 say 全 401；页面自己带 cookie 正常。局域网直连的 dt 票据
本来就是「中继签给这台设备、10 分钟有效」的短命凭据，壳把 URL 原样 GET，
所以把 dt 放进 query 就能认人——在家（Wi-Fi）和在外面（流量）一样走中继。

规则（本测试钉住）：
1. 有效的 dt → 认出设备：owner 有就带 uid，没有就是匿名设备。
2. 过期/签名被改/设备冻结 → 一律不认。
3. dt 只对 /api/say 生效（含 /api/say/<id>）；别把它变成通用钥匙。
4. /api/say 是前缀匹配：/api/say/<id> 算，/api/say_demo 不算。
"""
import base64
import hashlib
import hmac
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

SECRET = "s3cret"


class _Req:
    def __init__(self, db, path="/api/say/abc123", cookies=None, query=None):
        self.app = {"db": db, "key": b"k"}
        self.path = path
        self.cookies = cookies or {}
        self.query = query or {}
        self.headers = {}
        self.scheme = "http"


def _db(tmp_path, owner=None, frozen=0):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac',?,?  ,0,?,'codeboss',?)",
               (SECRET, "a" * 64, owner, frozen))
    db.commit()
    return db


def _mk(secret, dev, exp, fp=""):
    msg = "%s|%d|%s" % (dev, exp, "deadbeef")
    if fp:
        msg += "|" + fp
    key = hmac.new(secret.encode(), b"cb-direct-v1", hashlib.sha256).digest()
    sig = hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode((msg + "|" + sig).encode()).decode().rstrip("=")


def test_dt_identifies_device_on_say_path(tmp_path):
    db = _db(tmp_path, owner=7)
    dt = _mk(SECRET, "d1", int(time.time()) + 300, fp="a" * 64)
    req = _Req(db, query={"dt": dt})
    assert relay.remind_auth(req) == (7, "d1")


def test_dt_anon_device(tmp_path):
    db = _db(tmp_path)
    dt = _mk(SECRET, "d1", int(time.time()) + 300)
    assert relay.remind_auth(_Req(db, query={"dt": dt})) == (None, "d1")


def test_dt_rejects_expired_tampered_frozen(tmp_path):
    db = _db(tmp_path, owner=7)
    old = _mk(SECRET, "d1", int(time.time()) - 5)
    assert relay.remind_auth(_Req(db, query={"dt": old})) is None
    good = _mk(SECRET, "d1", int(time.time()) + 300)
    # 改签名的第一个字符（b64 末位可能只带填充位，改了也解出同一串）
    raw = base64.urlsafe_b64decode(good + "=" * (-len(good) % 4)).decode()
    msg, sig = raw.rsplit("|", 1)
    bad = base64.urlsafe_b64encode(
        (msg + "|" + ("0" if sig[0] != "0" else "1") + sig[1:]).encode()
    ).decode().rstrip("=")
    assert relay.remind_auth(_Req(db, query={"dt": bad})) is None
    db.execute("UPDATE devices SET frozen=1")
    db.commit()
    assert relay.remind_auth(_Req(db, query={"dt": good})) is None
    assert relay.remind_auth(_Req(db, query={"dt": "garbage"})) is None


def test_dt_not_a_general_key(tmp_path):
    db = _db(tmp_path, owner=7)
    dt = _mk(SECRET, "d1", int(time.time()) + 300)
    assert relay.remind_auth(_Req(db, path="/api/ext_send", query={"dt": dt})) is None
    assert relay.remind_auth(_Req(db, path="/api/bg_push", query={"dt": dt})) is None, \
        "dt 只管取音频；后台提醒流另走 cb_bgd"


def test_say_path_is_prefix_match(tmp_path):
    db = _db(tmp_path, owner=7)
    val = relay.make_bgd(b"k", "d1")
    bgd = {relay.BGD_COOKIE: val}
    assert relay.remind_auth(_Req(db, path="/api/say", cookies=bgd)) == (7, "d1")
    assert relay.remind_auth(_Req(db, path="/api/say/abc", cookies=bgd)) == (7, "d1")
    assert relay.remind_auth(_Req(db, path="/api/say_demo", cookies=bgd)) is None
