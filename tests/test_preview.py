# SPDX-License-Identifier: AGPL-3.0-or-later

"""网页预览（2026-11-19 用户口径：agent 做的网页/App，手机上要能直接看到）。

- host 把本机 dev server 反代到 /pv/<端口>/，手机里打开真页面；
- 端口号严格校验（只认 1~65535），只连 127.0.0.1，不许拿来打外网；
- HTML 里的绝对路径（src/href/action/url()）重写成 /pv/<端口>/ 前缀，
  否则手机上的绝对路径会跑到中继根上，页面一片 404；
- /api/ports 列本机在听的端口（给「选哪个」用）。

跑法：.venv/bin/python -m pytest tests/test_preview.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def test_port_validation():
    assert server._pv_port("3000") == 3000
    for bad in ("0", "65536", "-1", "abc", "3000/x", "", None, "22"):
        assert server._pv_port(bad) == 0, bad


def test_rewrites_absolute_paths():
    html = ('<link href="/assets/a.css"><script src="/main.js"></script>'
            '<a href="/about">x</a><form action="/save"></form>'
            '<style>body{background:url(/bg.png)}</style>')
    out = server._pv_rewrite(html, 3000)
    assert 'href="/pv/3000/assets/a.css"' in out
    assert 'src="/pv/3000/main.js"' in out
    assert 'href="/pv/3000/about"' in out
    assert 'action="/pv/3000/save"' in out
    assert "url(/pv/3000/bg.png)" in out


def test_rewrite_leaves_protocol_urls_alone():
    html = '<script src="https://cdn.x/a.js"></script><a href="//cdn.x/b">y</a>'
    out = server._pv_rewrite(html, 3000)
    assert '<script src="https://cdn.x/a.js"></script><a href="//cdn.x/b">y</a>' in out
    assert '__pvShim("/pv/3000")' in out


def test_rewrite_not_twice():
    """已改写过的页面（连 shim 都已带上）再过一遍必须原样——静态不双改、shim 不重注。"""
    html = '<script src="/pv/3000/main.js"></script><script>__pvShim("/pv/3000")</script>'
    assert server._pv_rewrite(html, 3000) == html


def test_ports_parser():
    out = """COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
node    1234 me   23u  IPv4 0x1      0t0  TCP *:5173 (LISTEN)
python  5678 me   10u  IPv6 0x2      0t0  TCP 127.0.0.1:8000 (LISTEN)
codeboss 99 me   20u  IPv4 0x3      0t0  TCP 127.0.0.1:8790 (LISTEN)
Identity 77 me    9u  IPv4 0x4      0t0  TCP *:17092 (LISTEN)
ControlC 78 me    9u  IPv4 0x5      0t0  TCP *:9227 (LISTEN)
ruby    5679 me   10u  IPv4 0x6      0t0  TCP *:4000 (LISTEN)"""
    # 全量列出（只跳过 sshd/本机 host）：3D 预览这类冷门服务以前被进程名规则漏掉，
    # 是不是网页交给 h_pv_index 逐个探测（2026-11-19 反馈）。进程名照带。
    assert server._pv_ports_of(out) == [
        (4000, "ruby", True), (5173, "node", True), (8000, "python", False),
        (9227, "ControlC", True), (17092, "Identity", True)]


def test_show_port_only_keeps_real_pages():
    """系统 RPC 端口探不到网页就不要；常见端口/开发进程慢一拍也保留。"""
    assert server._pv_show_port(5173, "node", False)
    assert server._pv_show_port(62222, "custom3d", True)      # 真答了 HTML
    assert not server._pv_show_port(9227, "ControlC", False)
    assert not server._pv_show_port(17092, "Identity", False)


def test_title_of():
    assert server._pv_title_of("<html><head><TITLE> Vite + React\n </TITLE></head>") == "Vite + React"
    assert server._pv_title_of('<title>A &amp; B &#x1f600;</title>') == "A & B 😀"
    assert server._pv_title_of("no title here") == ""
    assert len(server._pv_title_of("<title>" + "x" * 200 + "</title>")) == 40


def test_pv_index_labels_ports(monkeypatch):
    """「端口的服务能识别出来吗」：列表里每个端口旁边带网页标题 + 进程名。"""
    import asyncio
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [(5173, "node", False)])
    monkeypatch.setattr(server, "_front_browser_url", lambda: "")    # 不碰真浏览器
    monkeypatch.setattr(server, "_lan_ips", lambda: [])              # 不碰真网卡

    async def _lab(p, proc):
        return {"port": p, "proc": proc, "title": "Vite <App>", "html": True}
    monkeypatch.setattr(server, "_pv_label_of", _lab)
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        r = asyncio.run(server.h_pv_index(make_mocked_request("GET", "/pv")))
    finally:
        server.require_token = old
    assert ":5173" in r.text
    assert "Vite &lt;App&gt;" in r.text       # 标题里的尖括号要转义


def test_pv_index_shows_front_browser_page(monkeypatch):
    """「电脑端打开的网页也在 App 上能看」：列表最上面一条就是电脑浏览器当前这页。"""
    import asyncio
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [])
    monkeypatch.setattr(server, "_front_browser_url", lambda: "http://localhost:8000/x.html")
    monkeypatch.setattr(server, "_lan_ips", lambda: [])              # 不碰真网卡
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        r = asyncio.run(server.h_pv_index(make_mocked_request("GET", "/pv?sid=s1")))
    finally:
        server.require_token = old
    assert "电脑上正在看" in r.text
    assert 'href="/pv/8000/x.html"' in r.text


def test_shot_view_has_pinch_zoom():
    """拍屏整屏缩到手机上字看不清：查看页要能双指放大、放大后拖动、双击复位。"""
    h = server._SHOT_VIEW_HTML
    assert "__SRC__" in h                       # 由 handler 塞 base64 data URI
    assert "touches.length===2" in h
    assert "Math.hypot" in h                    # 双指间距
    assert "touchmove" in h
    assert "maximum-scale=1" in h               # 别让 WebView 自己缩放，交给脚本


def test_shot_handler_html_by_default_raw_on_demand(monkeypatch):
    """默认回查看页（一次拍摄内嵌成 data URI），?raw=1 还是原始 JPEG。"""
    import asyncio
    from aiohttp.test_utils import make_mocked_request

    class _R:
        returncode = 0

    def _run(argv, **kw):
        Path(argv[-1]).write_bytes(b"\xff\xd8" + b"x" * 4000)
        return _R()
    monkeypatch.setattr(server.subprocess, "run", _run)
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        r = asyncio.run(server.h_shot(make_mocked_request("GET", "/api/shot")))
        raw = asyncio.run(server.h_shot(make_mocked_request("GET", "/api/shot?raw=1")))
    finally:
        server.require_token = old
    assert r.content_type == "text/html"
    assert "data:image/jpeg;base64," in r.text
    assert raw.content_type == "image/jpeg"
    assert raw.body.startswith(b"\xff\xd8")        # 临时文件删了也发得出去（整张读进内存）


def test_find_apks_picks_newest_and_skips_junk(tmp_path):
    import os, time
    (tmp_path / "dist").mkdir()
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    a = tmp_path / "dist" / "old.apk"
    b = tmp_path / "dist" / "new.apk"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"x" * 200)
    os.utime(a, (time.time() - 600, time.time() - 600))
    (tmp_path / "node_modules" / "x" / "junk.apk").write_bytes(b"x" * 300)
    items = server._find_apks(str(tmp_path))
    assert [i["name"] for i in items] == ["new.apk", "old.apk"]
    assert items[0]["rel"] == os.path.join("dist", "new.apk")
    assert items[0]["size"] == 200


def test_sheet_back_from_proxied_page_returns_to_port_list():
    """预览里点进某个端口后，浮层的「返回」应该先回端口列表，而不是整个收起
    落回会话页（2026-11-19 反馈）。"""
    import re
    js = (HERE / "host" / "static" / "src" / "js" / "07_account.js").read_text()
    m = re.search(r"function sheetClose\(\)\{[\s\S]*?\n\}", js)
    assert m, "找不到 sheetClose()"
    body = m[0]
    assert "contentWindow" in body and "location.pathname" in body, \
        "sheetClose 要读 iframe 当前路径，才知道停在端口页上没有"
    assert "f.src='/pv'" in body, "停在端口页时「返回」要把 iframe 拉回 /pv 列表"
    assert body.index("f.src='/pv'") < body.index("about:blank"), \
        "预览判断要在整个收起之前"


def test_pv_index_route_accepts_trailing_slash():
    """浮层里打开的是 /pv（不带尾斜杠）；以前只注册了 /pv，
    /pv/ 落到 405，手机上「预览」里一片空白（2026-11-19 实测）。"""
    src = (HERE / "host" / "server.py").read_text()
    assert 'add_get("/pv", h_pv_index)' in src
    assert 'add_get("/pv/", h_pv_index)' in src
    js = (HERE / "host" / "static" / "src" / "js" / "05_cmd.js").read_text()
    assert "sheetOpen(base+'/pv?sid='" in js
    assert "sheetOpen(base+'/pv/'" not in js


def test_browse_entry_maps_local_external_and_file(tmp_path, monkeypatch):
    """「电脑浏览器当前这一页」→ App 里点得开的地址。"""
    root = str(tmp_path.resolve())
    monkeypatch.setattr(server, "_media_roots", lambda sid: [root])
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("x")

    e = server._pv_browse_entry("http://localhost:8000/3d/viewer.html?x=1")
    assert e["href"] == "/pv/8000/3d/viewer.html?x=1" and e["local"]

    e = server._pv_browse_entry("https://example.com/a")
    assert e["href"] == "https://example.com/a" and not e["local"]

    e = server._pv_browse_entry("file://%s/site/index.html" % root, "s1")
    assert e["href"] == "/pvfile/s1/site/index.html" and e["local"]

    # 允许目录以外 / 不是网页的地址：不列出来（点了也打不开）
    assert server._pv_browse_entry("file:///etc/hosts", "s1") is None
    assert server._pv_browse_entry("data:text/html,hi") is None
    assert server._pv_browse_entry("http://127.0.0.1:8790/") is None


def test_pvfile_serves_html_with_rewritten_assets(tmp_path, monkeypatch):
    """电脑里的 HTML 当网页渲染：同目录资源按原结构取，绝对路径改写到所在目录。"""
    import asyncio
    from aiohttp.test_utils import make_mocked_request
    root = str(tmp_path.resolve())
    monkeypatch.setattr(server, "_media_roots", lambda sid: [root])
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        '<link href="/a.css"><script src="app.js"></script>'
        '<style>body{background:url(/bg.png)}</style>')
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        req = make_mocked_request("GET", "/pvfile/s1/site/index.html",
                                  match_info={"sid": "s1", "tail": "site/index.html"})
        r = asyncio.run(server.h_pvfile(req))
    finally:
        server.require_token = old
    assert r.content_type == "text/html"
    assert 'href="/pvfile/s1/a.css"' in r.text         # 绝对路径 → 允许目录的根
    assert 'src="app.js"' in r.text                    # 相对路径不动
    assert "url(/pvfile/s1/bg.png)" in r.text


def test_pvfile_matches_session_root_and_rejects_escape(tmp_path, monkeypatch):
    """会话 cwd 外的 / 带 .. 的路径一律 404；Range 照做（页面里的视频能拖）。"""
    import asyncio
    import pytest
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    root = str(tmp_path.resolve())
    monkeypatch.setattr(server, "_media_roots", lambda sid: [root])
    (tmp_path / "a.css").write_text("body{}")
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        req = make_mocked_request("GET", "/pvfile/s1/a.css",
                                  headers={"Range": "bytes=0-3"},
                                  match_info={"sid": "s1", "tail": "a.css"})
        r = asyncio.run(server.h_pvfile(req))
        assert r.status == 206 and r.content_type == "text/css"
        bad = make_mocked_request("GET", "/pvfile/s1/../../etc/hosts",
                                  match_info={"sid": "s1", "tail": "../../etc/hosts"})
        with pytest.raises(web.HTTPNotFound):
            asyncio.run(server.h_pvfile(bad))
    finally:
        server.require_token = old


# ---- 运行时请求 shim（2026-11-24）：静态改写管不到页面 JS 在运行时拼的 fetch/XHR/WS ----
# deeptutor 这类 SPA：登录 fetch 是 JS 里 new URL('/api/auth/login', location.origin)
# 拼的，静态属性改写碰不到，登录请求就打在中继根上（"request failed"）。
def test_rewrite_injects_runtime_shim():
    html = ('<html><head><title>x</title><script src="/app.js"></script></head>'
            '<body></body></html>')
    out = server._pv_rewrite(html, 3000)
    assert '__pvShim("/pv/3000")' in out
    # 必须先于应用脚本执行，否则应用先发出去的请求补不到前缀
    assert out.index('__pvShim') < out.index('<script src="/pv/3000/app.js">')


def test_rewrite_shim_injection_idempotent():
    html = '<html><head></head><body>x</body></html>'
    once = server._pv_rewrite(html, 3000)
    assert server._pv_rewrite(once, 3000) == once


def test_rewrite_shim_prefix_follows_pvfile_pre():
    html = '<html><head></head><body></body></html>'
    out = server._pv_rewrite_prefix(html, "/pvfile/s1")
    assert '__pvShim("/pvfile/s1")' in out


def test_scope_session_cookie_to_preview_path():
    """Set-Cookie 的 Path=/ 钉到预览前缀：会话 cookie 不能变成整个中继源的，
    跟着 App 自己的请求到处发。"""
    f = server._pv_scope_cookie
    assert f("dt_token=abc; Path=/; HttpOnly", "/pv/3000") == \
        "dt_token=abc; Path=/pv/3000/; HttpOnly"
    assert f("dt_token=abc; Path=/", "/pv/3000") == "dt_token=abc; Path=/pv/3000/"
    assert f("sid=x", "/pv/3000") == "sid=x"                       # 没 Path 不动（浏览器默认按响应目录收窄）
    assert f("a=1; Path=/api; Max-Age=9", "/pv/3000") == \
        "a=1; Path=/pv/3000/api; Max-Age=9"


def test_h_pv_forwards_cookies_both_ways():
    """请求带 Cookie 头转发、响应 Set-Cookie 逐条带回并钉路径——
    不通的话登录成功了会话也存不住（手机端登录后下一步就 401）。"""
    import inspect
    src = inspect.getsource(server.h_pv)
    assert '"Cookie"' in src.replace("'", '"') or "Cookie" in src
    assert "Set-Cookie" in src
    assert "_pv_scope_cookie" in src


def test_pv_shim_js_behavior_via_node():
    """shim 的真实行为（fetch/XHR/WS/ES 改写规则）用 node 跑一遍：
    tests/test_pv_shim.mjs。没装 node 的机器跳过。"""
    import shutil
    import subprocess
    import sys
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("no node runtime")
    r = subprocess.run([node, str(HERE / "tests" / "test_pv_shim.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "pv shim 行为测试失败:\n%s%s" % (r.stdout, r.stderr)


# ---- 局域网直连预览（2026-11-24）：同网段时手机直接开 http://<lan-ip>:<port>/ ----
# WS/会话流在 /pv 子路径反代里走不通（隧道剥 upgrade），直连则一切原生可用；
# 探不通（跨网段/服务挂了）自动回退现有 /pv 路径。壳的私网闸门+trustedNav 已放行。
def _lsof(*lines):
    return "\n".join(lines)


def test_ports_parser_flags_lan_reachable():
    """通配/局域网地址监听 = 手机直连可达；只绑 127.0.0.1 = 不可达。"""
    out = server._pv_ports_of(_lsof(
        "node    1234 me   23u  IPv4 0x1      0t0  TCP *:5173 (LISTEN)",
        "python  5678 me   10u  IPv6 0x2      0t0  TCP 127.0.0.1:8000 (LISTEN)",
        "uvicorn 5679 me   20u  IPv4 0x3      0t0  TCP 192.168.24.160:8001 (LISTEN)",
        "node    5680 me   21u  IPv6 0x4      0t0  TCP [::]:3782 (LISTEN)"))
    got = dict((p, (proc, lan)) for p, proc, lan in out)
    assert got[5173] == ("node", True)          # *: 通配
    assert got[8000] == ("python", False)       # 只绑回环
    assert got[8001][1] is True                 # 绑在局域网地址上
    assert got[3782] == ("node", True)          # [::]: ipv6 通配


def test_pv_row_carries_lan_url():
    """局域网可达的行带 data-lan（http://<ip>:<port>/）；不可达的不带。"""
    row = server._pv_row_html(5173, "vite dev", True, "192.168.24.160")
    assert 'href="/pv/5173/"' in row
    assert 'data-lan="http://192.168.24.160:5173/"' in row
    row2 = server._pv_row_html(8000, "python", False, "192.168.24.160")
    assert "data-lan" not in row2
    row3 = server._pv_row_html(3000, "x", True, "")   # 拿不到本机局域网 IP：退化成纯 /pv
    assert "data-lan" not in row3


def test_pv_index_has_lan_probe_script(monkeypatch):
    """列表页带直连探测：局域网可达的行渲染出 data-lan，页面调 __pvLan 用
    /api/ping 三重验证（status 200 + ok:1 + pr 回声，主页面 probePing 同款）。
    不能用 no-cors fetch：壳的同网闸门对跨网段代答 404，fetch 把任何 HTTP
    响应都当成功 → 手机不在家网段时误判「局域网通」→ 浮层直跳够不着的地址
    全黑屏（2026-11-24）。"""
    import asyncio
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [(5173, "node", True)])
    monkeypatch.setattr(server, "_front_browser_url", lambda: "")
    monkeypatch.setattr(server, "_lan_ips", lambda: ["192.168.24.160"])

    async def _lab(p, proc):
        return {"port": p, "proc": proc, "title": "vite", "html": True}
    monkeypatch.setattr(server, "_pv_label_of", _lab)
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        r = asyncio.run(server.h_pv_index(make_mocked_request("GET", "/pv")))
    finally:
        server.require_token = old
    assert 'data-lan="http://192.168.24.160:5173/"' in r.text
    assert "__pvLan" in r.text and "api/ping" in r.text
    assert "no-cors" not in r.text
    assert '__pvLan(["192.168.24.160"],%d)' % server.PORT in r.text
