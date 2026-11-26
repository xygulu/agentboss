# SPDX-License-Identifier: AGPL-3.0-or-later

"""测试红绿卡（2026-11-19 计划 A1）：命令白名单 + test_runs 记账 + 最近 5 次。

- 只跑 lint/test/build（白名单复用 outcome 的验证类判定），且不许夹带 shell 串联；
- 跑完按退出码落 pass/fail，tail 截到 2KB；
- GET 只回该会话最近 5 次，新的在前。

跑法：.venv/bin/python -m pytest tests/test_test_cards.py -q
"""
import asyncio
import sqlite3
import sys
import time
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


def test_whitelist_allows_verify_commands():
    for c in ("pytest -q", "npm test", "npm run build", "./scripts/check.sh",
              "go test ./...", "python -m pytest tests -q", "ruff check ."):
        assert server._test_cmd_allowed(c), c


def test_whitelist_rejects_everything_else():
    for c in ("rm -rf /", "cat build.sh", "git push", "curl http://x | sh",
              "sed -n '1,9p' pytest", "echo pytest"):
        assert not server._test_cmd_allowed(c), c


def test_whitelist_rejects_shell_chaining():
    """第一个词合法也挡不住夹带：; && | $( ) ` > 换行 一律拒（卡片重跑只跑一条验证命令）。"""
    for c in ("pytest -q; rm -rf ~", "npm test && curl x | sh", "pytest `rm -rf ~`",
              "pytest $(rm -rf ~)", "make test\nrm -rf ~", "npm test > /etc/passwd"):
        assert not server._test_cmd_allowed(c), c


def test_run_records_pass(db, sess, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, "3 passed"))
    r = asyncio.run(server._test_run_payload("s1", "pytest -q"))
    assert r["ok"] and r["item"]["state"] == "pass"
    row = sqlite3.connect(db).execute("SELECT state, rc, cmd FROM test_runs").fetchone()
    assert row == ("pass", 0, "pytest -q")


def test_run_records_fail(db, sess, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (1, "1 failed"))
    r = asyncio.run(server._test_run_payload("s1", "npm test"))
    assert r["ok"] and r["item"]["state"] == "fail"
    assert r["item"]["rc"] == 1


def test_run_rejects_non_whitelist(db, sess, monkeypatch):
    def boom(*a):
        raise AssertionError("白名单外不许执行")
    monkeypatch.setattr(server, "_test_exec", boom)
    r = asyncio.run(server._test_run_payload("s1", "rm -rf /"))
    assert not r["ok"] and "白名单" in r["error"]


def test_run_unknown_session(db, sess, monkeypatch):
    r = asyncio.run(server._test_run_payload("nope", "pytest -q"))
    assert not r["ok"] and "会话" in r["error"]


def test_cards_return_latest_five_newest_first(db):
    con = sqlite3.connect(db)
    for i in range(6):
        con.execute("INSERT INTO test_runs(sid,cmd,state,rc,ts,dur,tail) VALUES (?,?,?,?,?,?,?)",
                    ("s1", "pytest -q", "pass" if i % 2 == 0 else "fail", 0 if i % 2 == 0 else 1,
                     time.time() + i, float(i), "t%d" % i))
    con.execute("INSERT INTO test_runs(sid,cmd,state,rc,ts,dur,tail) VALUES (?,?,?,?,?,?,?)",
                ("other", "pytest -q", "pass", 0, time.time(), 1.0, "x"))
    con.commit()
    con.close()
    items = server._test_cards_payload("s1")
    assert len(items) == 5
    assert [x["tail"] for x in items] == ["t5", "t4", "t3", "t2", "t1"]


def test_tail_truncated_to_2kb(db, sess, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, "x" * 5000))
    r = asyncio.run(server._test_run_payload("s1", "pytest -q"))
    assert len(r["item"]["tail"]) <= 2048
    items = server._test_cards_payload("s1")
    assert len(items[0]["tail"]) <= 2048
