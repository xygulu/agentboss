# SPDX-License-Identifier: AGPL-3.0-or-later

"""终端被移除 = 这台要重新登录（2026-11-21 用户口径）。

以前后台「移除客户端」只删 clients 行，手机上的 r_sess 照样有效——下一次请求
又自动登记回来，用户看到的是「移除了没反应」。现在移除会记一笔 client_kick：
带旧 cookie 回来一律按未登录处理，页面导航直接回登录页（不是停在断网页）。
浏览器闲置清扫、账号页自己踢设备走同一条路。
"""
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

QIN_UA = ("Mozilla/5.0 (Linux; Android 9; Qin 2 Pro Build/PPR1.180610.011; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/66 Mobile Safari/537.36 "
          "CodebossShell/0.11.16")
MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")


def _db(tmp_path):
    relay._CID_MEM.clear()
    relay._CLI_GATE.clear()
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.commit()
    return db


class _Req:
    def __init__(self, ua, db, cookies=None, path="/d", method="GET",
                 accept="text/html,application/xhtml+xml"):
        self.headers = {"User-Agent": ua, "Accept": accept}
        self.cookies = cookies or {}
        self.remote = "127.0.0.1"
        self.app = {"db": db}
        self.query = {}
        self.path = path
        self.method = method
        self.scheme = "https"


async def _call_mw(req, handler=None):
    from aiohttp import web

    async def ok(request):
        return web.Response(text="ok")

    fn = getattr(relay._svc_dim_mw, "__wrapped__", relay._svc_dim_mw)
    return await fn(req, handler or ok)


def _removed(db, cid="cmwqin2pro", uid=7):
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(?,?,?,?,?)",
               (uid, cid, "android|Qin 2 Pro", time.time() - 60, time.time()))
    db.commit()
    relay._delete_client(db, uid, cid)


def test_removing_a_client_records_that_it_must_login_again(tmp_path):
    db = _db(tmp_path)
    _removed(db)
    assert db.execute("SELECT 1 FROM clients WHERE uid=7").fetchone() is None
    assert db.execute("SELECT 1 FROM client_kick WHERE cid='cmwqin2pro'").fetchone()


def test_removed_app_client_page_goes_back_to_login(tmp_path):
    """App 带旧登录态回来：页面导航回 /r/ 登录页，而不是停在断网页。"""
    db = _db(tmp_path)
    _removed(db)
    resp = asyncio.run(_call_mw(_Req(QIN_UA, db, cookies={"cb_cid": "cmwqin2pro",
                                                          "r_sess": "u.0.sig"})))
    assert resp.status == 302 and resp.headers.get("Location") == "/r/", resp.status
    m = resp.cookies.get("r_sess")
    assert m is not None and m["max-age"] == "0", dict(m)


def test_removed_app_client_api_just_drops_the_cookie(tmp_path):
    """接口不该 302（页面 JS 会当成数据解析失败）：只清 cookie，下次导航回登录。"""
    db = _db(tmp_path)
    _removed(db)
    req = _Req(QIN_UA, db, cookies={"cb_cid": "cmwqin2pro", "r_sess": "u.0.sig"},
               path="/api/list", method="POST", accept="application/json")
    resp = asyncio.run(_call_mw(req))
    assert resp.status == 200
    m = resp.cookies.get("r_sess")
    assert m is not None and m["max-age"] == "0", dict(m)


def test_normal_app_client_keeps_its_week_session(tmp_path):
    """没被移除的 App 不受影响：cookie 不被改写（照旧 7 天）。"""
    db = _db(tmp_path)
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(7,'cmwqin2pro',"
               "'android|Qin 2 Pro',?,?)", (time.time(), time.time()))
    db.commit()
    resp = asyncio.run(_call_mw(_Req(QIN_UA, db, cookies={"cb_cid": "cmwqin2pro",
                                                          "r_sess": "u.0.sig"})))
    assert resp.status == 200
    assert resp.cookies.get("r_sess") is None, "没被踢的 App 不该被改写登录态"


def test_logging_in_again_lifts_the_restriction(tmp_path):
    """重新登录成功（响应里有新 r_sess）就解除，可以正常再用。"""
    from aiohttp import web

    db = _db(tmp_path)
    _removed(db)

    async def login(request):
        out = web.Response(text="ok")
        out.set_cookie("r_sess", "u.0.new", max_age=86400 * 7, httponly=True)
        return out

    req = _Req(QIN_UA, db, cookies={"cb_cid": "cmwqin2pro", "r_sess": "u.0.sig"},
               path="/r/login", method="POST", accept="application/json")
    asyncio.run(_call_mw(req, login))
    assert not db.execute("SELECT 1 FROM client_kick WHERE cid='cmwqin2pro'").fetchone()
    assert db.execute("SELECT 1 FROM clients WHERE uid=7").fetchone() is None
