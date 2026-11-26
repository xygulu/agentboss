#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""用户回话了 → 这个会话还没处理的提醒全部作废（2026-11-18 浮条一直挂着）。

以前只靠手机页面自己 POST /api/notify_act 标掉一条；从电脑上直接敲、或页面在
后台/刚重载没来得及标，那行一直是 state=''，重进会话又被 pendLoad 捞回来。

跑法：.venv/bin/python -m pytest tests/test_nh_answered.py -q
"""
import sqlite3
import sys
from pathlib import Path

import pytest

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
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    rows = [
        (1, 1000, "s1", "t", "perm", "a", "", "", "", "上线吗", "", "", 0, "", "", "{}"),
        (2, 1100, "s1", "t", "perm", "b", "", "", "", "", "", "", 0, "done", "", "{}"),
        (3, 1200, "s2", "t", "perm", "c", "", "", "", "", "", "", 0, "", "", "{}"),
    ]
    con.executemany("INSERT INTO notify_hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    yield path


def test_all_unresolved_of_sid_marked_sent(db):
    assert server._nh_answered("s1") == 1          # 只动还空着的那条，done 那条不回头改
    st = dict(sqlite3.connect(db).execute(
        "SELECT n, state FROM notify_hist").fetchall())
    assert st[1] == "sent" and st[2] == "done"
    assert st[3] == ""                              # 别的会话不受影响


def test_idempotent_and_empty_args(db):
    server._nh_answered("s1")
    assert server._nh_answered("s1") == 0           # 重复调用不再算数
    assert server._nh_answered("") == 0
    assert server._nh_answered("nope") == 0
