# SPDX-License-Identifier: AGPL-3.0-or-later

"""_svc_dim_mw 的收尾动作（2026-11-21 用户：注册上、发中继）。

它以前写好了却从没注册（middlewares=[_err_tap_mw]），所以生产上这些都不生效：
被踢的客户端带着旧 cookie 还能继续用、浏览器关掉还留着登录态、4xx/5xx 一条不记。
注册本身是行为变化，先把每条收尾动作钉住。

跑法： .venv/bin/python -m pytest tests/test_svc_dim_mw.py -q
"""
import asyncio
import hashlib
import hmac
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

KEY = b"test-key"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
APP_UA = "Dalvik/2.1.0 (Linux; U; Android 9; MI 8 Build/PKQ1)"


@pytest.fixture
def app(tmp_path):
    db = relay.db_init(str(tmp_path / "relay.db"))
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,sess_epoch)"
               " VALUES(7,'u','x','y',0,0)")
    db.execute("INSERT INTO devices(id,name,secret,owner,created) VALUES('d1','pc','s',7,0)")
    db.commit()
    a = {"db": db, "key": KEY}
    yield a
    db.close()


def token(uid=7, epoch=0, key=KEY):
    body = "%d.%d" % (uid, epoch)
    return "%s.%s" % (body, hmac.new(key, body.encode(), hashlib.sha256).hexdigest())


def req(app, path="/d", method="GET", cookies=None, ua=BROWSER_UA, accept="text/html"):
    h = {"User-Agent": ua}
    if accept is not None:
        h["Accept"] = accept
    return types.SimpleNamespace(app=app, path=path, method=method,
                                 scheme="https", headers=h,
                                 cookies=dict(cookies or {}))


def run(request, resp):
    async def handler(_r):
        return resp
    return asyncio.run(relay._svc_dim_mw(request, handler))


def test_not_kicked_normal_request_passes_through(app):
    r = run(req(app, cookies={"r_sess": token(), "cb_cid": "i:x"}), web.Response(text="ok"))
    assert r.status == 200 and r.text == "ok"


def test_kicked_client_html_navigation_goes_to_login(app):
    app["db"].execute("INSERT INTO client_kick(cid,ts) VALUES('i:abc',0)")
    app["db"].commit()
    r = run(req(app, path="/d", cookies={"r_sess": token(), "cb_cid": "i:abc"}),
            web.Response(text="ok"))
    assert r.status == 302, "被踢的客户端再打开页面要回登录页"
    assert r.headers["Location"] == "/r/"
    assert r.cookies["r_sess"].value == "", "旧 cookie 一并清掉"


def test_kicked_client_api_call_only_clears_cookie(app):
    app["db"].execute("INSERT INTO client_kick(cid,ts) VALUES('i:abc',0)")
    app["db"].commit()
    resp = web.Response(text='{"ok":true}', content_type="application/json")
    r = run(req(app, path="/api/sessions", method="POST", accept="application/json",
                cookies={"r_sess": token(), "cb_cid": "i:abc"}), resp)
    assert r.status == 200, "接口不跳转（客户端要看到真正的结局）"
    assert r.cookies["r_sess"].value == ""


def test_relogin_clears_kick_record(app):
    app["db"].execute("INSERT INTO client_kick(cid,ts) VALUES('i:abc',0)")
    app["db"].commit()
    resp = web.Response(text="ok")
    resp.set_cookie("r_sess", token(), httponly=True)
    r = run(req(app, path="/r/login", method="POST",
                cookies={"cb_cid": "i:abc"}), resp)
    assert r.status == 200
    left = app["db"].execute("SELECT COUNT(*) FROM client_kick WHERE cid='i:abc'").fetchone()[0]
    assert left == 0, "重新登录成功要解除限制，不然下次又被踢"


def test_browser_session_cookie_drops_max_age(app):
    r = run(req(app, cookies={"r_sess": token()}), web.Response(text="ok"))
    m = r.cookies["r_sess"]
    assert m.value == token()
    assert not m.get("max-age") and not m.get("expires"), "浏览器关掉就该重新登录"


def test_shell_keeps_7_day_cookie(app):
    r = run(req(app, ua=APP_UA, cookies={"r_sess": token()}), web.Response(text="ok"))
    assert "r_sess" not in r.cookies, "App/壳的登录态不在这里动"


def test_4xx_4xx_recorded_in_req_fail(app):
    r = run(req(app, path="/api/nope", method="POST", ua=APP_UA,
                cookies={"r_sess": token(), "cb_cid": "i:z", "cb_dev": "d1"}),
            web.Response(status=404))
    assert r.status == 404
    row = app["db"].execute(
        "SELECT path,method,status,uid,cid FROM req_fail ORDER BY id DESC LIMIT 1").fetchone()
    assert row == ("/api/nope", "POST", 404, 7, "i:z")


def test_registered_in_app_middlewares():
    src = Path(relay.__file__).read_text(encoding="utf-8")
    assert "middlewares=[_svc_dim_mw, _err_tap_mw, _selfhost_gate]" in src, \
        "中间件要注册进应用，否则这些收尾动作全是死代码"
