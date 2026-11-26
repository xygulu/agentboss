# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口核心 Phase 1 · Task 5：ACP stdio JSON-RPC 内核（假服务器钉契约）。

契约要点：NDJSON 每行一帧；request 按 id 关联；session/update 通知走回调；
子进程崩溃时在途 request 立刻以 AcpError 拒绝（终止路径枚举进 acp.py docstring，
2026-11-18 全灭教训：异步控制流的每条终止路径都要有人唤醒等待者）。

跑法： .venv/bin/python -m pytest tests/test_acp_core.py -q
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from acp import AcpConn, AcpError

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "acp_fake.py")]


def test_handshake_new_prompt_updates_close():
    async def run():
        c = AcpConn(FAKE)
        await c.start()                                  # (a) initialize 握手成功

        got = []
        c.on_update(lambda m, p: got.append((m, p)))
        r = await c.request("session/new", {})
        assert r["sessionId"] == "s1"                    # (b)

        r2 = await c.request("session/prompt", {"prompt": "hi"})
        assert r2["stopReason"] == "end_turn"            # (c)
        # 假服务器先发两条 update 再回结果；reader 顺序派发，回调必然已收到
        assert len(got) >= 2
        assert got[0][0] == "session/update"
        assert got[0][1]["update"]["sessionUpdate"] == "agent_message_chunk"
        assert got[1][1]["update"]["sessionUpdate"] == "tool_call"

        await c.close()                                  # (e)
        assert c.proc.returncode is not None   # asyncio.Process 无 poll()
    asyncio.run(run())


def test_notify_roundtrip_no_reply_expected():
    """notify 只发不收：不该造出无人认领的等待者。"""
    async def run():
        c = AcpConn(FAKE)
        await c.start()
        await c.notify("session/cancel", {"sessionId": "s1"})
        r = await c.request("session/new", {})           # 通道还健康
        assert r["sessionId"] == "s1"
        await c.close()
    asyncio.run(run())


def test_crash_rejects_inflight_request_not_hangs(monkeypatch):
    """(d) ACPM_FAKE_DIE=1：prompt 中途子进程死 → AcpError，≤5s，不挂死。"""
    async def run():
        monkeypatch.setenv("ACPM_FAKE_DIE", "1")
        c = AcpConn(FAKE)
        await c.start()
        t0 = time.time()
        with pytest.raises(AcpError):
            await c.request("session/prompt", {"prompt": "x"})
        assert time.time() - t0 <= 5
        await c.close()
        assert c.proc.returncode is not None   # asyncio.Process 无 poll()
    asyncio.run(run())


def test_rpc_error_raises_acperror():
    """服务端回 error 对象 → AcpError（不是挂起也不是裸 dict）。"""
    async def run():
        c = AcpConn(FAKE)
        await c.start()
        with pytest.raises(AcpError):
            await c.request("session/nope", {})
        await c.close()
    asyncio.run(run())


def test_agent_request_gets_error_reply(tmp_path, monkeypatch):
    """(评审 #11) agent→client 请求（session/request_permission）必须得到回帧：
    Phase 1 没有权限 UI，但静默丢弃会让真适配器挂死整回合——回 -32601 让它
    立刻失败。假服务器读我们的回帧，按有没有 error 回不同 chunk。"""
    monkeypatch.setenv("ACPM_FAKE_ASK", "1")
    chunks = []

    async def run():
        c = AcpConn(FAKE)
        c.on_update(lambda m, p: chunks.append(
            ((p.get("update") or {}).get("content") or {}).get("text") or ""))
        caps = await c.start()
        assert caps.get("protocolVersion") == "1"
        r = await c.request("session/new", {})
        assert r["sessionId"] == "s1"
        r2 = await c.request("session/prompt", {"prompt": "hi"})
        assert r2["stopReason"] == "end_turn"
        await c.close()
    asyncio.run(run())

    assert any("ASK_HANDLED_OK" in t for t in chunks), \
        "agent 的请求帧要被 -32601 应答，而不是静默丢弃：%r" % chunks
