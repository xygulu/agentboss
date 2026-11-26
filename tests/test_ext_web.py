# SPDX-License-Identifier: AGPL-3.0-or-later

"""产出物里的网页：文件里看源码，预览里看效果（2026-11-20）。

点 ext.html 以前是 /pvfile 当页面渲染——那文件正好是 App 自己，预览里等于把 App
再启动一遍，用户看到的就是「打不开」。现在：源码走文件浮层；效果由 /api/ext_web
给出预览地址——已在跑的服务答得出这个文件就用它，否则按文件目录起本机静态服务。

跑法： .venv/bin/python -m pytest tests/test_ext_web.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "demo.html").write_text("<b>x</b>", encoding="utf-8")

    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    return tmp_path


def _web(path="index.html"):
    return asyncio.run(server._ext_web_payload("s1", path))


def test_running_service_that_serves_the_file(cwd, monkeypatch):
    """项目本来就在跑（vite 之类）：给的是它的地址，不再起服务。"""
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [(5173, "node")])
    served = {}

    async def _s(port, rel):
        served[(port, rel)] = True
        return True
    monkeypatch.setattr(server, "_pv_serves", _s)
    d = _web()
    assert d["mode"] == "running" and d["port"] == 5173
    assert d["href"] == "/pv/5173/index.html"
    assert set(served) == {(5173, "index.html")}


def test_starts_static_service_when_none_running(cwd, monkeypatch):
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [])
    monkeypatch.setattr(server, "_web_static_ensure", lambda d: 8765)
    monkeypatch.setattr(server, "_web_wait", lambda p, secs=2.0: True)
    d = _web()
    assert d["mode"] == "static" and d["port"] == 8765
    assert d["href"] == "/pv/8765/index.html"
    assert d["url"] == "http://127.0.0.1:8765/index.html"


def test_nested_file_keeps_relative_path(cwd, monkeypatch):
    """子目录里的网页：路径带着走，静态服务从 cwd 根算起也能指到。"""
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [])
    monkeypatch.setattr(server, "_pv_serves", lambda p, r: _false())
    monkeypatch.setattr(server, "_web_static_ensure", lambda d: 9000)
    monkeypatch.setattr(server, "_web_wait", lambda p, secs=2.0: True)
    d = _web("sub/demo.html")
    assert d["href"] == "/pv/9000/demo.html"      # 静态服务的根 = 文件所在目录


def test_only_web_pages(cwd):
    with pytest.raises(server._ExtPathError):
        _web("a.py")


def test_traversal_denied(cwd):
    with pytest.raises(server._ExtPathError):
        _web("../../etc/passwd.html")


def test_open_redirects_to_preview_view(cwd, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [])
    monkeypatch.setattr(server, "_web_static_ensure", lambda d: 8765)
    monkeypatch.setattr(server, "_web_wait", lambda p, secs=2.0: True)
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        req = make_mocked_request("GET", "/api/ext_web?open=1&sid=s1&path=index.html")
        with pytest.raises(web.HTTPFound) as ei:
            asyncio.run(server.h_ext_web(req))
    finally:
        server.require_token = old
    assert ei.value.location == "/pv/8765/index.html"


def test_pv_index_lists_produced_pages(cwd, monkeypatch):
    """预览页里单列「产出物里的网页」：点一下就去 /api/ext_web 找地址/起服务。"""
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server, "_pv_listen_ports", lambda: [])
    monkeypatch.setattr(server, "_front_browser_url", lambda: "")
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        r = asyncio.run(server.h_pv_index(make_mocked_request("GET", "/pv?sid=s1")))
    finally:
        server.require_token = old
    assert "产出物里的网页" in r.text
    assert "/api/ext_web?open=1&sid=s1&path=index.html" in r.text


async def _false():
    return False
