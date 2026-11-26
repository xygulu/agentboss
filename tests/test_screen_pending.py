# SPDX-License-Identifier: AGPL-3.0-or-later

"""屏幕级拍板（2026-11-19 计划 B3）：没有 hook/rid 的 agent 也能在工作台看到并点掉选项。

- 会话屏幕上正开着选项弹层 → /api/ext_pending 里出现一条「待你决定」（source=screen，
  rid 为空，选项来自屏幕文字）；
- 已有 perm 提醒的会话不重复列；
- 没有镜像/没有弹层的会话不出现；
- 选项带 is_deny（拒绝/cancel 类），点击仍走 /api/ext_dlg_pick 的通用通道。

跑法：.venv/bin/python -m pytest tests/test_screen_pending.py -q
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


class _Ad:
    def dialog_hints(self):
        return None

    def dialog_prefixes(self):
        return None


@pytest.fixture
def fake_mirror(monkeypatch):
    snap = {"cur": 1, "opts": ["1", "2"], "multi": False, "sig": "x",
            "text": {"1": "Yes, allow this command", "2": "No, tell the agent"},
            "rows": ["Allow Bash command?", "❯ 1. Yes, allow this command",
                     "  2. No, tell the agent"]}
    monkeypatch.setattr(server, "_tick_sessions", lambda force=False: [
        {"sid": "s1", "state": "busy", "pid": 111, "agent": "opencode"},
        {"sid": "s2", "state": "busy", "pid": 222, "agent": "hermes"},
    ])
    monkeypatch.setattr(server, "_meta_for_pid", lambda pid: {"win": (80, 24)} if pid == 111 else None)
    monkeypatch.setattr(server, "_mirror_feed_inner", lambda sid, meta: {"win": (80, 24)})
    monkeypatch.setattr(server, "_dlg_snapshot_rows",
                        lambda st, cols, rows, hints=None, prefixes=None: dict(snap))
    monkeypatch.setattr(server.agents, "adapter_of", lambda sid: _Ad())
    monkeypatch.setattr(server, "_sid_title", lambda sid: "会话 " + sid)
    monkeypatch.setattr(server.agents, "agent_of", lambda sid: "opencode")
    return snap


def test_screen_dialog_becomes_pending(fake_mirror):
    items = server._screen_pending(set())
    assert len(items) == 1, "只该列有镜像且屏幕上有弹层的会话"
    it = items[0]
    assert it["sid"] == "s1" and it["rid"] == "" and it["source"] == "screen"
    assert [o["id"] for o in it["options"]] == ["1", "2"]
    assert it["options"][0]["is_deny"] is False
    assert it["options"][1]["is_deny"] is True, "No/cancel 类选项要标成拒绝"
    assert "allow" in it["options"][0]["label"]
    assert it["ask"].get("action"), "没有 ask 结构，工作台会把它当「等你回话」"


def test_existing_perm_not_duplicated(fake_mirror):
    assert server._screen_pending({"s1"}) == []


def test_no_dialog_no_item(fake_mirror, monkeypatch):
    monkeypatch.setattr(server, "_dlg_snapshot_rows",
                        lambda st, cols, rows, hints=None, prefixes=None: None)
    assert server._screen_pending(set()) == []


def test_pending_list_merges_screen(fake_mirror, monkeypatch):
    monkeypatch.setattr(server, "_NOTIFY_Q", [])
    items = server._pending_list()
    assert any(x.get("source") == "screen" for x in items)
