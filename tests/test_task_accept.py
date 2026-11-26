#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""验收动作 _task_accept_core:通过/打回的记账语义(批次二 #6)。

- accept:末条办结行 state=accepted;**不携带正文**(误触通过绝不放行 note)
- reject:state=rejected;返回 send_text(「老板打回：」+note 原文),由 handler 经
  既有 _send_verified 发回会话
- 无办结行的 sid:ok=False(没有可验收的东西)

跑法:.venv/bin/python -m pytest tests/test_task_accept.py -q
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

DB_PATH = [None]

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

SCHEMA = ("CREATE TABLE notify_hist (n INTEGER PRIMARY KEY, ts INTEGER, sid TEXT,"
          " title TEXT, kind TEXT, text TEXT, summary TEXT, about TEXT, cause TEXT,"
          " act TEXT, cwd TEXT, src TEXT, rep INTEGER, state TEXT, rid TEXT,"
          " trace TEXT)")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    DB_PATH[0] = path
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    rows = [
        (1, 1000, "s1", "t", "done", "x", "做完了", "", "", "", "", "", 0, "", "", "{}"),
        (2, 2000, "s1", "t", "perm", "y", "", "在问你", "", "", "", "", 0, "done", "", "{}"),
        (3, 3000, "s2", "t", "perm", "z", "", "挂着", "", "", "", "", 0, "", "", "{}"),
    ]
    con.executemany("INSERT INTO notify_hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    # core 的生产行为是「用完关连接」:mock 成每次新连接(与 test_notify_trace 同法)
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    yield path


def test_accept_marks_done_row(db):
    r = server._task_accept_core("s1", "accept", "")
    assert r["ok"] and "send_text" not in r
    st = sqlite3.connect(db).execute("SELECT state FROM notify_hist WHERE n=1").fetchone()[0]
    assert st == "accepted"


def test_accept_ignores_note(db):
    """误触通过绝不放行正文:accept 的 note 被忽略,不发任何东西回会话。"""
    r = server._task_accept_core("s1", "accept", "顺手这句也发过去")
    assert r["ok"] and not r.get("send_text")


def test_reject_marks_row_and_returns_send_text(db):
    r = server._task_accept_core("s1", "reject", "边界没测到")
    assert r["ok"]
    assert r["send_text"].startswith("老板打回：")
    assert "边界没测到" in r["send_text"]
    st = sqlite3.connect(db).execute("SELECT state FROM notify_hist WHERE n=1").fetchone()[0]
    assert st == "rejected"


def test_no_done_row_is_400(db):
    """s2 只有挂着 perm,没有办结行:没有可验收的东西。"""
    r = server._task_accept_core("s2", "accept", "")
    assert not r["ok"]


def test_second_verdict_overwrites(db):
    """验收结论以最后一次为准(打回后老板改主意=允许覆盖)。"""
    server._task_accept_core("s1", "accept", "")
    server._task_accept_core("s1", "reject", "再想想")
    st = sqlite3.connect(db).execute("SELECT state FROM notify_hist WHERE n=1").fetchone()[0]
    assert st == "rejected"


# ── 终审修复钉子:发送链路端到端(终审 C1:发送分支调了不存在的 _ext_list,
#    NameError 被吞、前端还报成功——钉住「send_text 必须真走到发送」)──
def test_reject_actually_sends(monkeypatch):
    sent = {}

    async def fake_find(sid):
        return {"sid": sid, "tty": "/dev/x"}

    async def fake_send(x, text):
        sent["text"] = text
        return True, ""

    monkeypatch.setattr(server, "require_token", _async_ok2)
    monkeypatch.setattr(server, "_find_session", fake_find)
    monkeypatch.setattr(server, "_send_verified", fake_send)
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(DB_PATH[0]))

    class Req:
        headers = {"X-Product": "agentboss"}
        cookies = {"cb_product": "agentboss"}
        remote = "t"

        async def json(self):
            return {"sid": "s1", "verdict": "reject", "note": "重测边界"}

    r = asyncio.run(server.h_task_accept(Req()))
    assert r.status == 200
    assert "老板打回：" in sent.get("text", ""), "打回语必须真的发进会话"


async def _async_ok2(request):
    return None
