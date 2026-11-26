# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent 自己跑过的测试，自动落卡（2026-11-19 用户口径：不要人再点，绿红要留痕）。

- 判定沿用 outcome.classify（转录里的验证类命令 + 退出码 + passed/failed），
  这里只把「命令/退出码/尾巴」一起带回来；
- 轮次结束（Stop）时写进 test_runs，src='agent'；同一条命令同一条尾巴短期内
  重复上报只记一次（Stop 和 agent_completed 挨着来时别记两行）；
- 没有验证证据（done_unverified）就不写：宁缺勿假。

跑法：.venv/bin/python -m pytest tests/test_agent_test_record.py -q
"""
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import outcome
import server

SCHEMA = ("CREATE TABLE test_runs(n INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT,"
          " cmd TEXT, state TEXT, rc INTEGER, ts REAL, dur REAL, tail TEXT, src TEXT)")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    return path


def _ev(cmd, text, exit_code=None, err=None):
    return {"prompt": "", "texts": [],
            "uses": {"u1": {"name": "Bash", "input": {"command": cmd}}},
            "results": [{"id": "u1", "text": text, "exit": exit_code, "err": err}]}


def test_classify_carries_command_and_rc():
    oc = outcome.classify(_ev(".venv/bin/pytest -q", "719 passed in 16.58s", 0))
    assert oc["state"] == "done_verified"
    assert oc["cmd"] == ".venv/bin/pytest -q"
    assert oc["rc"] == 0
    assert "719 passed" in oc["tail"]


def test_classify_carries_failure():
    oc = outcome.classify(_ev("npm test", "1 failed, 3 passed", 1))
    assert oc["state"] == "failed"
    assert oc["rc"] == 1
    assert oc["cmd"] == "npm test"


def test_no_evidence_no_fields():
    oc = outcome.classify(_ev("ls", "a b c"))
    assert oc["state"] == "done_unverified"
    assert "cmd" not in oc


def test_record_agent_run(db):
    oc = {"state": "done_verified", "cmd": ".venv/bin/pytest -q", "rc": 0,
          "tail": "719 passed"}
    assert server._record_agent_run("s1", oc) is True
    row = sqlite3.connect(db).execute(
        "SELECT sid, cmd, state, rc, src FROM test_runs").fetchone()
    assert row == ("s1", ".venv/bin/pytest -q", "pass", 0, "agent")


def test_record_agent_run_dedupes_same_output(db):
    oc = {"state": "failed", "cmd": "npm test", "rc": 1, "tail": "1 failed"}
    assert server._record_agent_run("s1", oc) is True
    assert server._record_agent_run("s1", oc) is False
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
    assert n == 1


def test_record_skips_unverified(db):
    assert server._record_agent_run("s1", {"state": "done_unverified",
                                           "label": "本轮结束"}) is False
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
    assert n == 0
