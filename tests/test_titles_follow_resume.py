# SPDX-License-Identifier: AGPL-3.0-or-later

"""改名要跟着续篇走（2026-11-21 用户：两个叫 3dgenerator 的会话关掉后不见了）。

实测：原件 13a81b32 被用户改名「3dgenerator」（sess_title），--resume 出的续篇
592080f6 是另一个 sid。列表把续篇和原件合成一条、只留最近活跃的续篇——但名字
只按 sid 查，续篇没名字就退回窗口标题（「wrapper.py -- claude --resume … ▸ git」）。
用户按「3dgenerator」找，两条都不见了。

跑法： .venv/bin/python -m pytest tests/test_titles_follow_resume.py -q
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server
import transcript


def _chain(tmp_path, monkeypatch):
    """parent.jsonl（原文）← child.jsonl（续篇，首条是续写说明）。"""
    d = tmp_path / ".claude" / "projects" / "-w"
    d.mkdir(parents=True)
    parent = d / "parent.jsonl"
    parent.write_text(json.dumps({"type": "user", "cwd": "/w",
                                  "message": {"role": "user", "content": "最初的问题"}}) + "\n",
                      encoding="utf-8")
    child = d / "child.jsonl"
    child.write_text(json.dumps({"type": "user", "cwd": "/w", "message": {
        "role": "user", "content": "This session is being continued from a previous "
                                   "conversation… 完整转录：" + str(parent)}}) + "\n",
                     encoding="utf-8")
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda cls: tmp_path))
    return parent, child


def test_resumed_session_inherits_renamed_title(tmp_path, monkeypatch):
    _chain(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_user_titles", lambda: {"parent": "3dgenerator"})
    out = server._apply_titles([{"sid": "child", "cwd": "/w", "agent": "claude",
                                 "title": "wrapper.py -- claude --resume child ▸ git"}])
    assert out[0]["title"] == "3dgenerator", "续篇没继承原件上用户改的名字"


def test_own_title_wins_over_chain_head(tmp_path, monkeypatch):
    _chain(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_user_titles",
                        lambda: {"parent": "3dgenerator", "child": "子会话自己改的名"})
    out = server._apply_titles([{"sid": "child", "cwd": "/w", "agent": "claude",
                                 "title": "x"}])
    assert out[0]["title"] == "子会话自己改的名"


def test_chain_walk_handles_missing_parent(tmp_path, monkeypatch):
    """原件转录被删也能追到能走到的那条（名字还是这条对话的），不能炸。"""
    _chain(tmp_path, monkeypatch)
    (tmp_path / ".claude" / "projects" / "-w" / "parent.jsonl").unlink()
    monkeypatch.setattr(server, "_user_titles", lambda: {"parent": "3dgenerator"})
    assert transcript.chain_head("child", "/w") == "parent"
    out = server._apply_titles([{"sid": "child", "cwd": "/w", "agent": "claude",
                                 "title": "x"}])
    assert out[0]["title"] == "3dgenerator"
