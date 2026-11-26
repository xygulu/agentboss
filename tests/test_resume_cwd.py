# SPDX-License-Identifier: AGPL-3.0-or-later

"""召回会话时原工作目录已经没了（/private/tmp/xxx 被清掉）也要能起来。

2026-11-22 实测：opencode 召回终端里 `cd: no such file or directory: /private/tmp/abacc`，
wrapper 一步都没跑，agent 自然没起来。目录回不来，但会话（opencode 按 id 全局查）
还能恢复——换到 home 起，降级要看得见。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def test_missing_cwd_falls_back_to_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    got = server._resume_cwd("ses_abc", str(tmp_path / "gone"))
    assert got == str(home)
    assert "原目录不存在" in capsys.readouterr().out, "降级要让用户/日志看得见"


def test_existing_cwd_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    assert server._resume_cwd("ses_abc", str(work)) == str(work)
