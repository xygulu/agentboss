# SPDX-License-Identifier: AGPL-3.0-or-later

"""后端去二元：_pick_agent 显式报错、_agent_of_cmd 动态集合、模型分支能力门控。

跑法： .venv/bin/python -m pytest tests/test_server_debinary.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agents
import agent_opencode
import server


def test_pick_agent_empty_defaults():
    ad, err = server._pick_agent({})
    assert err == ""
    assert ad is agents.default()


def test_pick_agent_unknown_errors():
    ad, err = server._pick_agent({"agent": "junk"})
    assert ad is None
    assert err and "junk" in err


def test_pick_agent_known():
    saved = dict(agents._ADAPTERS)

    class _FakeOC(agents.AgentAdapter):
        key = "opencode"
        label = "OpenCode"

    try:
        agents.register(_FakeOC())
        ad, err = server._pick_agent({"agent": "opencode"})
        assert err == "" and ad is not None and ad.key == "opencode"
    finally:
        agents._ADAPTERS.clear()
        agents._ADAPTERS.update(saved)


def test_agent_of_cmd_dynamic():
    assert server._agent_of_cmd(["/usr/local/bin/opencode"]) == "opencode"
    assert server._agent_of_cmd(["claude"]) == "claude"
    assert server._agent_of_cmd(["/bin/zsh"]) is None


def test_claude_has_session_id_flag():
    assert getattr(agents.get("claude"), "new_session_id_flag", None) == ["--session-id"]
    assert getattr(agents.get("codex"), "new_session_id_flag", None) is None


def test_model_options_gated_by_capability():
    saved = dict(agents._ADAPTERS)

    class _NoSwitch(agents.AgentAdapter):
        key = "opencode"
        label = "OpenCode"
        can_model_switch = False

    try:
        agents.register(_NoSwitch())
        assert server._session_model_options("opencode") == []
        assert any(o["label"] == "跟机器默认"
                   for o in server._session_model_options("claude"))
    finally:
        agents._ADAPTERS.clear()
        agents._ADAPTERS.update(saved)


def test_notify_translated_to_stop():
    d = agent_opencode.notify_event(
        {"type": agent_opencode.SESSION_EVENT_TYPE, "sid": "s1", "cwd": "/w", "last": "done"})
    assert d["hook_event_name"] == "Stop" and d["agent"] == "opencode"


def test_hermes_hook_payload_translated_to_stop():
    """hook.sh 原样转发的 hermes stdin payload（无 type，hook_event_name 认领）。"""
    import agent_hermes
    d = agent_hermes.notify_event(
        {"hook_event_name": "on_session_end", "session_id": "t1",
         "cwd": "/w", "extra": {"completed": True}})
    assert d["hook_event_name"] == "Stop" and d["agent"] == "hermes"
    assert d["session_id"] == "t1" and d["cwd"] == "/w"
    # claude 自己的 hook payload 不受影响（hook_event_name=Stop 不是我们的分支）
    assert agent_hermes.notify_event({"hook_event_name": "Stop"}) == {}


def test_push_msg_ev_parse_off_event_loop(monkeypatch):
    """C2：观看 opencode/hermes 时 parse_transcript（export 子进程，最长 15s）
    不许在事件循环线程里同步跑——整个 aiohttp 循环停摆，所有客户端心跳断流
    （server.py 8821 写过的自家故障模式，discover 守住了、parse 漏了）。"""
    import asyncio
    import threading

    seen = {}

    class _Ad:
        key = "hermes"

        def transcript_path(self, cwd, sid):
            return "fake-path"

        def parse_transcript(self, p, limit=400):
            seen["thread"] = threading.current_thread()
            return [{"role": "assistant", "text": "hi", "tools": []}]

    monkeypatch.setattr(server.agents, "get", lambda k: _Ad())
    ev = asyncio.run(server._push_msg_ev(
        {"agent": "hermes", "state": "busy", "pid": None},
        "s1", {"msg_prev": None}, prev_none=True))
    assert ev["total"] == 1 and ev["messages"][0]["text"] == "hi"
    assert seen["thread"] is not threading.main_thread(), \
        "parse_transcript 跑在了事件循环线程上"
