# SPDX-License-Identifier: AGPL-3.0-or-later

"""模型路由（2026-11-26）：以 magpie（本机模型路由网关）为底座的手机切换能力。

- 探测：GET 127.0.0.1:3425/v1/magpie/quotas 成功即在线（顺带拿额度 JSON）；
- magpie 不在：available=false 功能降级可见；包内自带 magpie 时拉起 headless 网关
  （`<bin> web --gateway --no-open`），10 分钟冷却防轰炸；
- 可执行解析顺序：PATH → ~/.local/bin/magpie（launchd PATH 短，官装默认落这里）
  → 包内自带；
- ls/models 是人读表格，解析器贴真实输出样本；
- 切换走 CLI argv 直传不经 shell；model 必须匹配白名单正则，agent 必须在 ls 里。

跑法：.venv/bin/python -m pytest tests/test_model_router.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

_DASH = "—"

# magpie ls 的真实输出样本（2026-11-26 实测；em-dash、双空格分列、行尾配置路径）
_LS_SAMPLE = (
    f"  Claude Code     {_DASH}  ·  effort {_DASH}" + " " * 60 + "~/.claude/settings.json\n"
    f"  Claude Desktop  provider {_DASH}  ·  gateway {_DASH}" + " " * 30
    + "~/Library/Application Support/Claude/claude_desktop_config.json\n"
    + "  Codex           MiniMax-M3  ·  effort high" + " " * 44 + "~/.codex/config.toml\n"
    + f"  Gemini CLI      auth {_DASH}  ·  {_DASH}" + " " * 52 + "~/.gemini/settings.json\n"
    + f"  OpenCode        {_DASH}  ·  small {_DASH}" + " " * 52 + "~/.config/opencode/opencode.jsonc\n"
    + "  VS Code         " + _DASH + " " * 80
    + "~/Library/Application Support/Code/User/settings.json\n"
    + f"  Hermes Agent    deepseek-v4-flash  ·  effort {_DASH}  ·  ai-manager deepseek-v4-flash"
    + " " * 24 + "~/.hermes/config.yaml\n"
    + "  OpenClaw        newcli/claude-sonnet-4-5" + " " * 56 + "~/.openclaw/openclaw.json\n"
)

_MODELS_SAMPLE = (
    "\n"
    "  Routing groups\n"
    "  auto/glm-5-turbo                   GLM-5-Turbo  high\n"
    "  auto/glm-5.3-flash                 GLM-5.3-Flash  low/high/max\n"
    "  DeepSeek\n"
    "  deepseek/deepseek-flash            DeepSeek-V4.1-Flash  none/low/high/max\n"
    "  deepseek/deepseek-v4-pro           DeepSeek-V4-Pro  none/low/high/max\n"
    "  Zhipu GLM\n"
    "  zhipu/glm-5.3                      GLM-5.3  low/high/max\n"
)


def test_parse_magpie_ls():
    got = server._parse_magpie_ls(_LS_SAMPLE)
    assert got["Codex"] == "MiniMax-M3"
    assert got["Claude Code"] is None          # 未设模型
    assert got["Claude Desktop"] is None       # provider —
    assert got["Gemini CLI"] is None           # auth — · —
    assert got["OpenCode"] is None
    assert got["VS Code"] is None
    assert got["Hermes Agent"] == "deepseek-v4-flash"
    assert got["OpenClaw"] == "newcli/claude-sonnet-4-5"


def test_parse_magpie_models():
    got = server._parse_magpie_models(_MODELS_SAMPLE)
    by_id = {m["id"]: m for m in got}
    assert by_id["auto/glm-5-turbo"]["group"] == "Routing groups"
    assert by_id["auto/glm-5-turbo"]["name"] == "GLM-5-Turbo"
    assert by_id["deepseek/deepseek-v4-pro"]["group"] == "DeepSeek"
    assert by_id["deepseek/deepseek-v4-pro"]["name"] == "DeepSeek-V4-Pro"
    assert by_id["zhipu/glm-5.3"]["group"] == "Zhipu GLM"
    assert len(got) == 5


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


def _no_token(monkeypatch):
    async def _ok(_r):
        return None
    monkeypatch.setattr(server, "require_token", _ok)


def test_status_online(monkeypatch):
    _no_token(monkeypatch)

    async def _snap():
        return True, {"data": [{"provider": "deepseek", "display": "¥17.06 left"}]}
    monkeypatch.setattr(server, "_magpie_snapshot", _snap)
    monkeypatch.setattr(server, "_magpie_run", lambda *a, **k: _LS_SAMPLE)

    resp = asyncio.run(server.h_model_router(_Req()))
    body = json.loads(resp.text)
    assert body["ok"] == 1 and body["available"] is True
    agents = {a["name"]: a["model"] for a in body["agents"]}
    assert agents["Codex"] == "MiniMax-M3"
    assert body["quotas"][0]["provider"] == "deepseek"


def test_status_offline_no_bundled(monkeypatch):
    _no_token(monkeypatch)

    async def _snap():
        return False, None
    monkeypatch.setattr(server, "_magpie_snapshot", _snap)
    monkeypatch.setattr(server, "_magpie_bin", lambda: None)
    spawned = []
    monkeypatch.setattr(server, "_magpie_spawn", lambda bin_: spawned.append(bin_))

    resp = asyncio.run(server.h_model_router(_Req()))
    body = json.loads(resp.text)
    assert body["available"] is False
    assert body["agents"] == [] and body["quotas"] == []
    assert body["bundled"] is False
    assert spawned == []


def test_status_offline_spawns_bundled(monkeypatch):
    _no_token(monkeypatch)

    async def _snap():
        return False, None
    monkeypatch.setattr(server, "_magpie_snapshot", _snap)
    monkeypatch.setattr(server, "_magpie_bin", lambda: "/x/magpie")
    monkeypatch.setattr(server, "_magpie_cooling", lambda: False)
    spawned = []
    monkeypatch.setattr(server, "_magpie_spawn", lambda bin_: spawned.append(bin_))

    resp = asyncio.run(server.h_model_router(_Req()))
    body = json.loads(resp.text)
    assert body["available"] is False
    assert body["bundled"] is True
    assert spawned == ["/x/magpie"]           # 拉起 headless 网关


def test_spawn_argv_is_headless_gateway(monkeypatch):
    """自带拉起必须是 headless 形态：web --gateway --no-open，不弹窗口不抢 TTY。"""
    seen = {}

    class _P:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw

    monkeypatch.setattr(server.subprocess, "Popen", _P)
    server._magpie_spawn("/x/magpie")
    assert seen["argv"] == ["/x/magpie", "web", "--gateway", "--no-open"]
    assert seen["kw"].get("start_new_session") is True


def test_bin_resolution_order(monkeypatch, tmp_path):
    """PATH 命中 → ~/.local/bin → 包内自带 → None。"""
    monkeypatch.setattr(server.shutil, "which", lambda n: "/usr/local/bin/magpie")
    assert server._magpie_bin() == "/usr/local/bin/magpie"

    monkeypatch.setattr(server.shutil, "which", lambda n: None)
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    local = tmp_path / ".local" / "bin" / "magpie"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert server._magpie_bin() == str(local)

    local.unlink()
    bundled = tmp_path / "magpie-bin" / "magpie"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setattr(server, "_shared", lambda *a: tmp_path / "magpie-bin")
    assert server._magpie_bin() == str(bundled)

    bundled.unlink()
    assert server._magpie_bin() is None


def test_models_endpoint(monkeypatch):
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_run", lambda *a, **k: _MODELS_SAMPLE)

    resp = asyncio.run(server.h_model_router_models(_Req(query={"agent": "Codex"})))
    body = json.loads(resp.text)
    assert body["ok"] == 1
    ids = [m["id"] for m in body["models"]]
    assert "deepseek/deepseek-v4-pro" in ids


def _set_req(agent="Codex", model="deepseek/deepseek-v4-pro"):
    return _Req(body={"agent": agent, "model": model})


def test_set_happy_path(monkeypatch):
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_bin", lambda: "/x/magpie")
    monkeypatch.setattr(server, "_parse_magpie_ls", lambda t: {"Codex": "MiniMax-M3"})

    seen = {}

    def _run(*args, **kw):
        seen["args"] = args
        return "✓ Codex model deepseek/deepseek-v4-pro"
    monkeypatch.setattr(server, "_magpie_run", _run)

    resp = asyncio.run(server.h_model_router_set(_set_req()))
    body = json.loads(resp.text)
    assert body["ok"] == 1
    assert seen["args"] == ("Codex", "deepseek/deepseek-v4-pro")


def test_set_rejects_injection(monkeypatch):
    """argv 虽不经 shell，参数仍过白名单：空格/分号/引号/以-开头的 model 一律拒。"""
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_bin", lambda: "/x/magpie")
    monkeypatch.setattr(server, "_parse_magpie_ls", lambda t: {"Codex": "MiniMax-M3"})
    called = []
    monkeypatch.setattr(server, "_magpie_run", lambda *a, **k: called.append(a) or "")

    for bad in ("a b", "x;rm", "y'z", '-n', "", "m/../../etc"):
        resp = asyncio.run(server.h_model_router_set(_set_req(model=bad)))
        assert json.loads(resp.text)["ok"] == 0, bad
    resp = asyncio.run(server.h_model_router_set(_set_req(agent="a b")))
    assert json.loads(resp.text)["ok"] == 0
    assert called == []


def test_set_rejects_unknown_agent(monkeypatch):
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_bin", lambda: "/x/magpie")
    monkeypatch.setattr(server, "_parse_magpie_ls", lambda t: {"Codex": "MiniMax-M3"})
    monkeypatch.setattr(server, "_magpie_run", lambda *a, **k: "")

    resp = asyncio.run(server.h_model_router_set(_set_req(agent="NotReal")))
    assert json.loads(resp.text)["ok"] == 0


def test_set_retries_once_on_transient_failure(monkeypatch):
    """magpie App 忙（app-server 重启/批量迁移）时 CLI 偶发非零退出——重试一次再报失败。"""
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_bin", lambda: "/x/magpie")
    monkeypatch.setattr(server, "_parse_magpie_ls", lambda t: {"Codex": "MiniMax-M3"})
    calls = []

    def _run(*a, **kw):
        calls.append(a)
        return None if len(calls) == 2 else "✓"   # 第2次（set 首试）失败，第3次（重试）成
    monkeypatch.setattr(server, "_magpie_run", _run)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    resp = asyncio.run(server.h_model_router_set(_set_req()))
    assert json.loads(resp.text)["ok"] == 1
    assert len(calls) == 3                        # ls + set + 重试


def test_set_without_magpie_is_503(monkeypatch):
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_magpie_bin", lambda: None)
    resp = asyncio.run(server.h_model_router_set(_set_req()))
    assert resp.status == 503


def test_probe_detects_gateway(monkeypatch):
    """真 HTTP：起一个回 quotas JSON 的 aiohttp 服务 → 在线；瞎端口 → 离线。"""
    from aiohttp import web

    async def run():
        async def quotas(_r):
            return web.json_response({"data": [{"provider": "x"}]})
        app = web.Application()
        app.router.add_get("/v1/magpie/quotas", quotas)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            monkeypatch.setattr(server, "_MAGPIE_URL", f"http://127.0.0.1:{port}")
            ok, data = await server._magpie_snapshot()
            assert ok is True and data["data"][0]["provider"] == "x"

            monkeypatch.setattr(server, "_MAGPIE_URL", "http://127.0.0.1:1")
            ok, data = await server._magpie_snapshot()
            assert ok is False and data is None
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_ui_page_online(monkeypatch):
    """路由浮层页：在线时渲染 agent 列表 + 额度窗 + 切换交互所需的数据端点。"""
    _no_token(monkeypatch)

    async def _snap():
        return True, {"data": [{"provider": "deepseek", "display": "¥17.06 left",
                                "windows": [{"name": "5 hours", "display": "2488 / 12000",
                                             "remaining": 79.3}]}]}
    monkeypatch.setattr(server, "_magpie_snapshot", _snap)
    monkeypatch.setattr(server, "_magpie_run", lambda *a, **k: _LS_SAMPLE)

    resp = asyncio.run(server.h_model_router_ui(_Req()))
    html = resp.text
    assert "模型路由" in html
    assert "Codex" in html and "MiniMax-M3" in html
    assert "deepseek" in html and "¥17.06" in html
    assert "/api/model_router" in html          # 页内 fetch 的数据端点
    assert "cb-close" in html                    # 浮层关闭走壳的消息协议


def test_ui_page_offline(monkeypatch):
    """离线降级可见：说明 magpie 不在线，别静默空白。"""
    _no_token(monkeypatch)

    async def _snap():
        return False, None
    monkeypatch.setattr(server, "_magpie_snapshot", _snap)
    monkeypatch.setattr(server, "_magpie_bin", lambda: None)

    resp = asyncio.run(server.h_model_router_ui(_Req()))
    html = resp.text
    assert "不在线" in html
    assert "magpie" in html.lower()
