# SPDX-License-Identifier: AGPL-3.0-or-later

"""中继 500 必须留得下证据（2026-11-21 手机一直 500，后台却什么都没有）。

aiohttp 的兜底 500 是「没有响应对象」的那些异常，req_fail 只记有响应的 4xx/5xx，
所以异常路径在线上完全隐形。最外层加一个记录中间件：每个请求留一笔结局
（路径/UA/IP/状态/耗时），异常把堆栈一起留下，`/r/admin_api/failures` 能直接看。
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, ua="Mozilla/5.0", path="/d", method="GET",
                 remote="203.0.113.9"):
        self.headers = {"User-Agent": ua}
        self.path, self.method, self.remote = path, method, remote


def _call(handler, req=None):
    fn = getattr(relay._err_tap_mw, "__wrapped__", relay._err_tap_mw)
    return asyncio.run(fn(req or _Req(), handler))


def _clear():
    relay._ERRS.clear()
    relay._RECENT.clear()


def test_exception_is_recorded_with_traceback():
    _clear()
    from aiohttp import web

    async def boom(request):
        raise ValueError("boom-500")

    try:
        _call(boom, _Req(path="/d", ua="Dalvik/2.1.0 CodebossShell/0.11.32"))
    except ValueError:
        pass
    else:
        raise AssertionError("异常必须照常抛出去（中间件只记录，不吞）")
    e = relay._ERRS[0]
    assert "ValueError: boom-500" in e["tb"]
    assert e["path"] == "/d" and e["ua"].startswith("Dalvik/")
    assert relay._RECENT[0]["status"] == 0, "崩掉的请求状态记 0，好和 500 响应区分"


def test_normal_request_is_recorded_with_status():
    _clear()
    from aiohttp import web

    async def ok(request):
        return web.Response(text="ok")
    resp = _call(ok, _Req(path="/api/list", ua="Qin/1.0"))
    assert resp.status == 200
    r = relay._RECENT[0]
    assert (r["path"], r["status"], r["ua"]) == ("/api/list", 200, "Qin/1.0")
    assert r["ip"] == "203.0.113.9" and r["ms"] >= 0
    assert not relay._ERRS


def test_http_exception_is_status_not_error():
    _clear()
    from aiohttp import web

    async def go(request):
        raise web.HTTPFound("/r/")
    try:
        _call(go)
    except web.HTTPFound:
        pass
    assert relay._RECENT[0]["status"] == 302
    assert not relay._ERRS, "302/403 这类是正常结局，不该按异常记堆栈"


def test_app_actually_registers_the_tap():
    src = Path(relay.__file__).read_text(encoding="utf-8")
    # 两个都要挂：_svc_dim_mw 在外层记 (uid,dev,cid) 并做踢人/req_fail 收尾，
    # _err_tap_mw 在里面记结局和堆栈（2026-11-21 一起注册）。
    assert "middlewares=[_svc_dim_mw, _err_tap_mw, _selfhost_gate]" in src, \
        "中间件定义了不挂上（_svc_dim_mw 的前车之鉴）"
