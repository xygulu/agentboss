# SPDX-License-Identifier: AGPL-3.0-or-later

"""概括晚到时，这一轮的 trace 也要补上（否则提醒健康页把这轮算成没判过）。

场景：通知只等模型 2.5 秒，慢的这条先落原文、trace 在那时已经取走了；
模型 3~6 秒才回来，act_src 只能由 _nh_fill 回填进 notify_hist.trace。
"""
import asyncio
import json
import sqlite3

import pytest

import notify


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "nh.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notify_hist(n INTEGER PRIMARY KEY, summary TEXT,"
                " act TEXT, cause TEXT, trace TEXT DEFAULT '')")
    con.execute("INSERT INTO notify_hist(n,summary,act,cause,trace) VALUES(1,'在做什么','','','')")
    con.execute("INSERT INTO notify_hist(n,summary,act,cause,trace) VALUES(2,'真概括','','','')")
    con.commit()
    con.close()
    monkeypatch.setattr(notify, "_db", lambda: sqlite3.connect(path))
    return path


def _row(path, n):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT summary,act,trace FROM notify_hist WHERE n=?", (n,)).fetchone()
    finally:
        con.close()


def _fill(n, payload, old=""):
    async def fake():
        return payload
    asyncio.run(notify._nh_fill(n, fake(), old))


def test_late_reply_backfills_trace_act(db):
    _fill(1, {"summary": "已发布", "act": "", "cause_ok": True, "act_src": "not-grounded"},
          old="在做什么")
    summary, _, trace = _row(db, 1)
    assert summary == "已发布"
    assert json.loads(trace)["act"] == "not-grounded"


def test_stale_row_keeps_its_own_trace(db):
    """兜底串已经被改过（真概括先到了）：这一条不是我们这轮的回填对象，别动 trace。"""
    _fill(2, {"summary": "别的概括", "act": "", "cause_ok": True, "act_src": "conflict"},
          old="在做什么")
    assert _row(db, 2) == ("真概括", "", "")


def test_cached_reply_without_act_src_is_fine(db):
    """老缓存/没判过的结果没有 act_src：照常回填概括，trace 保持原样。"""
    _fill(1, {"summary": "已发布", "act": "", "cause_ok": None}, old="在做什么")
    summary, _, trace = _row(db, 1)
    assert summary == "已发布"
    assert trace == ""
