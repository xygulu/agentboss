# SPDX-License-Identifier: AGPL-3.0-or-later

"""APK 分发跟着「谁在要包」走，不跟本机 host 的线走（2026-11-20 用户反馈）。

同一台手机装了两条线，打开 AgentBoss 却和 CodeBoss 一模一样，连图标都没换——
根因之一：一台 codeboss 线的开发机，给 agentboss 壳答 /api/apk_ver 时返回的是
**自己那条线**的包（url 也指向本线），壳按 url 下载、装上就是第二个口袋工头，
图标当然一模一样。一个 host 同时服务两条线，供包必须认「客户端/路径点名的线」。

跑法： .venv/bin/python -m pytest tests/test_apk_client_product.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _mk(tmp, product, body, ver, code):
    d = tmp / "build" / product
    d.mkdir(parents=True)
    (d / (product + "-terminal.apk")).write_bytes(body)
    (d / "apk.json").write_text(json.dumps({"ver": ver, "code": code}), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    _mk(tmp_path, "agentboss", b"AGENT-APK", "0.1.10", 12)
    _mk(tmp_path, "codeboss", b"CODING-APK", "0.11.31", 196)
    monkeypatch.setattr(server, "_android_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")

    async def _ok(request):
        return None
    monkeypatch.setattr(server, "require_token", _ok)
    return tmp_path


class _Req:
    def __init__(self, path="/", query=None, cookies=None, headers=None):
        self.path = path
        self.query = query or {}
        self.cookies = cookies or {}
        self.headers = headers or {}


def _apk(path="/", **kw):
    r = asyncio.run(server.h_apk(_Req(path, **kw)))
    return r.body, r.headers.get("Content-Disposition", "")


def _ver(query=None, **kw):
    r = asyncio.run(server.h_apk_ver(_Req("/api/apk_ver", query, **kw)))
    return json.loads(r.text)


# ---- 下载：路径点名的线优先（/agentboss.apk 就发老板的包） ----

def test_apk_download_by_path_not_host_line(env):
    body, cd = _apk("/agentboss.apk")
    assert body == b"AGENT-APK", "codeboss 开发机上的 /agentboss.apk 发了工头的包"
    assert "agentboss-terminal-0.1.10.apk" in cd
    body, cd = _apk("/codeboss.apk")
    assert body == b"CODING-APK" and "codeboss-terminal-0.11.31.apk" in cd


def test_apk_download_by_client_cookie(env):
    # 老壳/别名路径：路径看不出线，就认客户端自己声明的线
    body, _ = _apk("/", cookies={"cb_product": "agentboss"})
    assert body == b"AGENT-APK"


def test_apk_download_by_query(env):
    body, _ = _apk("/", query={"product": "agentboss"})
    assert body == b"AGENT-APK"


def test_apk_download_without_signal_follows_host(env):
    body, _ = _apk("/")
    assert body == b"CODING-APK", "没有任何产品线信号时维持老行为（跟本机线）"


# ---- 版本检查：壳带的 ?product= / cookie 决定答哪条线 ----

def test_apk_ver_follows_client_product(env):
    d = _ver({"product": "agentboss"})
    assert d["code"] == 12 and d["ver"] == "0.1.10"
    assert d["url"] == "/agentboss.apk", "url 指错线，壳就会下到另一个 App"

    d = _ver(cookies={"cb_product": "agentboss"})
    assert d["code"] == 12 and d["url"] == "/agentboss.apk"


def test_apk_ver_without_signal_follows_host(env):
    d = _ver()
    assert d["code"] == 196 and d["url"] == "/codeboss.apk"


def test_apk_ver_missing_that_line_404s(env):
    # 本机没建 agentboss 包：要说清楚缺哪条线，而不是拿另一条线的包顶上
    (env / "build" / "agentboss" / "agentboss-terminal.apk").unlink()
    r = asyncio.run(server.h_apk_ver(_Req("/api/apk_ver", {"product": "agentboss"})))
    assert r.status == 404
    assert "agentboss" in r.text
