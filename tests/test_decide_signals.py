#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""三路拍板信号归一基线(claude/opencode/hermes)。

批次二(#4 统一拍板队列)的前置事实钉子:
- claude:hooks 直送 PermissionRequest → server 规则层 kind=perm
- opencode / hermes:只产 Stop(跑完)→ server 规则层 kind=done;
  「完成其实在等你」由付费智能层(na_need)改报 perm,不在本基线内
- 三路帧形状必须一致:server 侧只认 session_id/hook_event_name/agent 等
  统一字段,形状漂了队列就漏。

跑法:.venv/bin/python -m pytest tests/test_decide_signals.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_opencode
import agent_hermes
import server


def test_opencode_stop_frame_shape():
    d = agent_opencode.notify_event({
        "type": agent_opencode.SESSION_EVENT_TYPE,
        "sid": "oc-1", "last": "已改完三处", "prompt": "帮我改样式", "cwd": "/tmp/x"})
    assert d["hook_event_name"] == "Stop"
    assert d["agent"] == "opencode"
    assert d["session_id"] == "oc-1"
    assert d["last_assistant_message"] == "已改完三处"
    # 形状漂移守卫:server 翻译入口只认这几个统一字段
    assert set(d) == {"session_id", "hook_event_name", "agent", "cwd",
                      "last_assistant_message", "prompt"}


def test_hermes_stop_frame_shape():
    d = agent_hermes.notify_event({
        "type": agent_hermes.SESSION_EVENT_TYPE,
        "sid": "he-1", "last": "跑完了", "prompt": "跑测试", "cwd": "/tmp/y"})
    assert d["hook_event_name"] == "Stop"
    assert d["agent"] == "hermes"
    assert d["session_id"] == "he-1"


def test_hermes_hook_payload_shape():
    d = agent_hermes.notify_event({
        "type": agent_hermes.SESSION_HOOK_EVENT,
        "hook_event_name": agent_hermes.SESSION_HOOK_EVENT,
        "session_id": "he-2", "extra": {"completed": True}})
    assert d["hook_event_name"] == "Stop"
    assert d["agent"] == "hermes"
    assert d["session_id"] == "he-2"


def test_claude_permission_request_yields_perm(monkeypatch):
    """claude 的「等你拍板」是唯一原生信号:规则层必须直出 perm。"""
    monkeypatch.setattr(server, "_np_get", lambda: {"max_text": 160})
    out = server._ev_local({
        "session_id": "cc-1", "hook_event_name": "PermissionRequest",
        "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "cwd": "/tmp/z"})
    assert out, "PermissionRequest 没有产出任何提醒"
    kinds = [x["kind"] for x in out]
    assert kinds == ["perm"], kinds
    assert "rm -rf" in out[0]["text"]


def test_opencode_hermes_stop_yields_done(monkeypatch):
    """opencode/hermes 的 Stop 在规则层归一为 done(智能改报 perm 是付费层,另测)。"""
    monkeypatch.setattr(server, "_np_get", lambda: {"max_text": 160})
    for d in (
        agent_opencode.notify_event({"type": agent_opencode.SESSION_EVENT_TYPE,
                                     "sid": "oc-9", "last": "ok", "prompt": "p", "cwd": "/t"}),
        agent_hermes.notify_event({"type": agent_hermes.SESSION_EVENT_TYPE,
                                   "sid": "he-9", "last": "ok", "prompt": "p", "cwd": "/t"}),
    ):
        out = server._ev_local(d)
        assert out and out[0]["kind"] == "done", (d["agent"], out)
