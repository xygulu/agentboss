# SPDX-License-Identifier: AGPL-3.0-or-later

"""整页缓存 + 整页压缩（2026-11-18 全站断网事故）。

事故账本：一台手机卡在「加载失败 → 3 秒后整页重载」的循环里，3 小时拉 977 次
整页、吃掉中继 97% 的页面流量（632MB），把 3Mbps 出口打满，其他账号全部
120s 隧道超时 504 → 也跟着进循环。两道闸：每次整页压成 gzip；同一台设备
60 秒内只向开发机要一次整页（其余从内存回）。

跑法： .venv/bin/python -m pytest tests/test_relay_page_throttle.py -q
"""
import gzip
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

BIG = b"<html>" + b"x" * 600_000 + b"</html>"


class _Req:
    def __init__(self, ae="gzip, deflate"):
        self.headers = {} if ae is None else {"Accept-Encoding": ae}


def _resp(n=BIG):
    from aiohttp import web
    return web.Response(status=200, body=n)


def _clear():
    relay._PAGE_CACHE.clear()


def test_page_cache_hit_within_ttl_then_miss():
    _clear()
    assert relay._page_cache_get("k") is None
    relay._page_cache_put("k", _resp())
    hit = relay._page_cache_get("k")
    assert hit is not None and hit[2] == BIG                 # TTL 内直接回内存里的页
    assert relay._page_cache_get("other") is None             # 别的设备不串
    relay._PAGE_CACHE["k"] = (time.time() - relay.PAGE_CACHE_TTL - 1,) + hit[1:]
    assert relay._page_cache_get("k") is None                 # 过期后要重新问开发机


def test_maybe_gzip_shrinks_page_and_marks_header():
    _clear()
    out = _resp()
    relay._maybe_gzip(_Req(), out)
    assert out.headers.get("Content-Encoding") == "gzip"
    assert len(out.body) < 5000
    assert gzip.decompress(out.body) == BIG


def test_repair_fake_gzip_fixes_0916_host_zlib_stream():
    """0.9.16 host 发的是 zlib 流却标 gzip：Chromium 永远加载不完（全站断网真凶）。"""
    import zlib
    fake = zlib.compress(BIG, 6)
    assert fake[:2] != b"\x1f\x8b"              # 确实是 zlib 流，不是 gzip 容器
    out = _resp(fake)
    out.headers["Content-Encoding"] = "gzip"
    relay._repair_fake_gzip(out)
    assert out.headers.get("Content-Encoding") is None
    assert out.body == BIG                       # 还原成原始页面，后面再正经压

    # 真 gzip 不能被误伤
    co = zlib.compressobj(6, zlib.DEFLATED, 31)
    real = co.compress(BIG) + co.flush()
    out = _resp(real)
    out.headers["Content-Encoding"] = "gzip"
    relay._repair_fake_gzip(out)
    assert out.headers["Content-Encoding"] == "gzip" and out.body == real


def test_page_cache_maybe_stores_plaintext_from_gzip_tunnel_body():
    """0.9.16 host 在隧道里压过（真 gzip）时也必须缓存：以前只有「没压缩头」才存，
    结果缓存一次都没生效，切到 aliyun 每次都从开发机重拉 611KB、等十几秒。"""
    import zlib
    _clear()
    co = zlib.compressobj(6, zlib.DEFLATED, 31)
    out = _resp(co.compress(BIG) + co.flush())
    out.headers["Content-Encoding"] = "gzip"
    out.headers["Content-Type"] = "text/html; charset=utf-8"
    relay._page_cache_maybe("k", out)
    hit = relay._page_cache_get("k")
    assert hit is not None and hit[2] == BIG                  # 存的是明文
    assert not hit[3].get("Content-Encoding")                 # 压缩头已摘掉
    # 非整页（key 空）或非 html 不缓存
    relay._page_cache_maybe(None, _resp())
    out = _resp()
    out.headers["Content-Type"] = "application/json"
    relay._page_cache_maybe("j", out)
    assert relay._page_cache_get("j") is None


def test_tunnel_plain_caches_gzip_page_end_to_end():
    """走一遍真的隧道整页路径：host 回 gzip 整页 → relay 存明文 → 第二次直接命中，
    不再向开发机发帧（aliyun 切台十几秒就是每次都在发这个帧）。"""
    import asyncio
    import base64
    import zlib
    _clear()

    class _WS:
        def __init__(self, app, res):
            self.app, self.res, self._cb_lock, self.sent = app, res, asyncio.Lock(), 0

        async def send_json(self, frame):
            self.sent += 1
            fut, _dev = self.app["pending"].pop(frame["id"])
            fut.set_result(self.res)

    class _Req:
        method, path, headers = "GET", "/", {"Accept-Encoding": "gzip"}
        rel_url = type("U", (), {"raw_path_qs": "/"})()
        cookies = {}
        query = {}

        def __init__(self, app):
            self.app = app

        def get(self, k, d=None):
            return d

        async def read(self):
            return b""

    class _DB:                                  # 设备版本表：只有 0.9.17+ 才跨设备共用缓存
        ver = "0.9.17"

        def execute(self, *_a):
            return self

        def fetchone(self):
            return (self.ver,)

    async def run():
        db = _DB()
        app = {"pending": {}, "db": db}
        co = zlib.compressobj(6, zlib.DEFLATED, 31)
        gz = co.compress(BIG) + co.flush()
        res = {"code": 200, "ct": "text/html; charset=utf-8",
               "body": base64.b64encode(gz).decode(),
               "headers": {"content-encoding": "gzip"}}
        ws = _WS(app, res)
        out = await relay._tunnel_plain(_Req(app), ws, "dev1")
        assert out.headers.get("Content-Encoding") == "gzip"
        assert zlib.decompress(out.body, 31) == BIG      # 给客户端的仍是可解的真 gzip
        hit = relay._page_cache_get("None|0.9.17|||/")   # 键末段是产品线（本用例没带）
        assert hit is not None and hit[2] == BIG         # 缓存里是明文
        out2 = await relay._tunnel_plain(_Req(app), ws, "dev1")
        assert ws.sent == 1                              # 第二次没再找开发机
        assert zlib.decompress(out2.body, 31) == BIG
        # 跨设备：另一个 dev 也直接吃缓存（页面 HTML 已与设备无关），这才有「切台秒开」
        out3 = await relay._tunnel_plain(_Req(app), ws, "dev2")
        assert ws.sent == 1 and zlib.decompress(out3.body, 31) == BIG
        # pverCheck 的硬刷（?pv=）必须绕过缓存去拿新页，并刷新缓存
        r = _Req(app)
        r.query = {"pv": "1"}
        await relay._tunnel_plain(r, ws, "dev2")
        assert ws.sent == 2
        # 老 host（<0.9.17）页面里还嵌着本机目录：不能跨设备共用
        db.ver = "0.9.16"
        _clear()
        await relay._tunnel_plain(_Req(app), ws, "dev1")
        await relay._tunnel_plain(_Req(app), ws, "dev2")
        assert ws.sent == 4          # 两台各拉一次

    asyncio.run(run())


def test_maybe_gzip_skips_when_client_cant_or_host_already_did():
    out = _resp()
    relay._maybe_gzip(_Req(ae=None), out)          # 不带 Accept-Encoding：原样发
    assert not out.headers.get("Content-Encoding") and len(out.body) == len(BIG)

    out = _resp()
    out.headers["Content-Encoding"] = "gzip"       # host 已在隧道里压过：不能压第二遍
    relay._maybe_gzip(_Req(), out)
    assert out.headers["Content-Encoding"] == "gzip" and len(out.body) == len(BIG)

    out = _resp(b"tiny")                           # 小响应不值得压
    relay._maybe_gzip(_Req(), out)
    assert not out.headers.get("Content-Encoding")
