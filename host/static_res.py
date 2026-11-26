# SPDX-License-Identifier: AGPL-3.0-or-later

"""网页资源读取与提供（/static/<name>）。

资源在发布包里编进 static_pack.pyc（安装目录里翻不到可读的前端源码），开发检出
直接读 static/ 目录。缓存口径：URL 带内容哈希，手机可以长期缓存；不带就 no-cache
+ ETag 回 304 —— 页面冷加载不用每次从中继拉几百 KB 的 xterm。
"""
import base64
import hashlib
import mimetypes
import re
import time
import zlib
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent


STATIC = HERE / "static"


_STATIC_CACHE = {}   # 包里的资源解压一次就留着（发布包里不会再变）


def _static_bytes(name):
    """网页资源取字节：发布包里编在 static_pack.pyc（安装目录下翻不到可读的前端源码），
    开发检出直接读 static/ 目录。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise web.HTTPNotFound()
    try:
        import static_pack
        b = static_pack.FILES.get(name)
        if b is not None:
            raw = _STATIC_CACHE.get(name)
            if raw is None:
                raw = zlib.decompress(base64.b64decode(b))
                _STATIC_CACHE[name] = raw
            return raw
    except ImportError:
        pass
    try:
        return (STATIC / name).read_bytes()
    except OSError:
        raise web.HTTPNotFound()


_STATIC_REV = {}    # 资源名 -> (文件时间, 内容哈希短)：URL 带它，变了就换 URL


def _static_rev(name):
    """静态资源的内容版本（内容哈希前 10 位）。变了 URL 就变，手机不会拿旧的。

    开发检出里文件随时会改：只按内容缓存一次的话，host 会一直吐内存里的老页面——
    改完 ext.html、page_ver 都变了、手机每分钟 reload 也一样是旧页面（2026-11-13
    实锤：状态行/用时在手机上死活看不到）。所以缓存键带上文件时间。"""
    try:
        mtime = (STATIC / name).stat().st_mtime_ns
    except OSError:
        mtime = 0              # 发布包里资源编在 static_pack，永远不会变
    hit = _STATIC_REV.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        body = _static_bytes(name)
    except Exception:
        return "0"
    rev = hashlib.sha1(body).hexdigest()[:10]
    _STATIC_REV[name] = (mtime, rev)
    return rev


_STATIC_URL_RE = re.compile(r"/static/([A-Za-z0-9_.\-]+)")


def _static_rewrite(tpl):
    """把页面里的 /static/xterm.js 改成 /static/xterm.js?v=<内容哈希>。

    带版本号的 URL 可以被手机长期缓存（immutable）：xterm 几百 KB 只在开发机
    换版本时才重下一次，平时页面冷加载不再从中继拉一遍静态资源。"""
    def _r(m):
        nm = m.group(1)
        try:
            return "/static/%s?v=%s" % (nm, _static_rev(nm))
        except Exception:
            return m.group(0)
    return _STATIC_URL_RE.sub(_r, tpl)


def _static_text(name):
    return _static_bytes(name).decode("utf-8")


def _static_ver():
    """ext.html 的版本号：内容变就变（发布包用打包时算的哈希，开发检出用文件时间）。"""
    try:
        import static_pack
        return int(static_pack.VER)
    except (ImportError, AttributeError, ValueError):
        pass
    try:
        return (STATIC / "ext.html").stat().st_mtime_ns
    except OSError:
        return 0



async def h_static(request):
    """静态资源（xterm 等）：发布包里从 static_pack 取，开发检出读 static/ 目录。

    缓存策略：
      · URL 带 ?v=<内容哈希> 且和当前一致 → immutable，一年，手机只下一次；
      · 没带或对不上 → no-cache + ETag，手机会拿 ETag 来问一句（304，不传 body）。
    这样页面冷加载不再每次从中继拉一遍几百 KB 的 xterm。"""
    name = request.match_info["name"]
    body = _static_bytes(name)
    ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
    rev = _static_rev(name)
    etag = '"%s"' % rev
    want = (request.query.get("v") or "").strip()
    hdrs = {"Content-Type": ct, "ETag": etag,
            "Last-Modified": time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT", time.gmtime())}
    if want and want == rev:
        hdrs["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        hdrs["Cache-Control"] = "no-cache"
        if (request.headers.get("If-None-Match") or "").strip() == etag:
            return web.Response(status=304, headers=hdrs)
    return web.Response(body=body, headers=hdrs)
