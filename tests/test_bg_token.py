# SPDX-License-Identifier: AGPL-3.0-or-later

"""息屏/后台那条通道：壳读不到 cookie 的机器（S24 mini）要能自己带一把长期凭据。

2026-11-19：前台播报已经修了（页面把 dt 票据放在 URL 上，见 test_say_dt.py）。
黑屏时页面被冻结、没人能给壳换票，dt 只有 10 分钟——后台服务得有一把长期的：
页面用登录态向中继要一张设备凭据（/api/bg_ticket，90 天，同 cb_bgd 的格式），
经 bridge 交给壳，壳存在 prefs 里，之后每个请求带 `X-CB-Bgd` 头。

规则（本测试钉住）：
1. X-CB-Bgd 头在 /api/bg_push 和 /api/say/<id> 上认人；别处不认。
2. 冻死的设备不认；坏签名不认。
3. /api/bg_ticket 只给「有身份的那台设备」签票，签出来的票必须能过第 1 条。
"""
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, path="/api/bg_push", cookies=None, headers=None, query=None):
        self.app = {"db": db, "key": b"k"}
        self.path = path
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.query = query or {}
        self.scheme = "http"


def _db(tmp_path, owner=7, frozen=0):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac','s','a'*64,0,?,'codeboss',?)", (owner, frozen))
    db.commit()
    return db


def test_bgd_header_opens_bg_and_say(tmp_path):
    db = _db(tmp_path)
    tok = relay.make_bgd(b"k", "d1")
    h = {"X-CB-Bgd": tok}
    assert relay.remind_auth(_Req(db, path="/api/bg_push", headers=h)) == (7, "d1")
    assert relay.remind_auth(_Req(db, path="/api/say/abc", headers=h)) == (7, "d1")
    assert relay.remind_auth(_Req(db, path="/api/ext_send", headers=h)) is None, \
        "长期凭据只开提醒两条路，别变通用钥匙"


def test_bgd_header_rejects_bad_and_frozen(tmp_path):
    db = _db(tmp_path)
    tok = relay.make_bgd(b"k", "d1")
    head, sig = tok.rsplit(".", 1)
    bad = head + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert relay.remind_auth(_Req(db, headers={"X-CB-Bgd": bad})) is None
    assert relay.remind_auth(_Req(db, headers={"X-CB-Bgd": ""})) is None
    db.execute("UPDATE devices SET frozen=1")
    db.commit()
    assert relay.remind_auth(_Req(db, headers={"X-CB-Bgd": tok})) is None


def test_bg_ticket_mints_token_for_selected_device(tmp_path, monkeypatch):
    db = _db(tmp_path)
    req = _Req(db, path="/api/bg_ticket", cookies={"cb_dev": "d1"})
    monkeypatch.setattr(relay, "uid_of", lambda r: 7)
    resp = asyncio.run(relay.h_bg_ticket(req))
    assert resp.status == 200, resp.text
    import json
    d = json.loads(resp.text)
    assert d.get("ok") and d.get("tok")
    assert relay.bgd_dev(_Req(db, cookies={relay.BGD_COOKIE: d["tok"]})) == (7, "d1"), \
        "签出来的票得能过提醒通道"


def test_bg_ticket_refuses_without_identity(tmp_path, monkeypatch):
    db = _db(tmp_path)
    req = _Req(db, path="/api/bg_ticket")
    monkeypatch.setattr(relay, "uid_of", lambda r: None)
    monkeypatch.setattr(relay, "dev_of", lambda r: "")
    resp = asyncio.run(relay.h_bg_ticket(req))
    assert resp.status == 401
