#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""工作台整理：模型判断「值不值得看」+ 用户隐藏/删除。

规则（2026-11-19 用户）：
- 模型判 keep=0 的行不透出（测试会话、废弃任务），用户可恢复/删除
- 隐藏与判断只在「最近一次动静」之前有效：任务有新进展就翻篇
- 「等你拍板」的行永远不判、不收：那是一句话的事
- 删除只从工作台移除，会话和历史不动

跑法:.venv/bin/python -m pytest tests/test_task_view.py -q
"""
import asyncio
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


async def _ok(request):
    return None


class Req:
    headers = {"X-Product": "agentboss"}
    cookies = {"cb_product": "agentboss"}
    remote = "t"

    def __init__(self, body=None):
        self._b = body or {}

    async def json(self):
        return self._b


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    now = int(__import__("time").time())
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    rows = [
        (1, now - 3000, "s1", "真实任务", "done", "干完了", "改了登录", "", "", "", "", "", 0, "", "", "{}"),
        (2, now - 2000, "s2", "测试会话", "done", "跑通了", "随便试试", "", "", "", "", "", 0, "", "", "{}"),
        (3, now - 100, "s3", "等你拍板", "perm", "", "", "要不要删缓存", "", "", "", "", 0, "", "", "{}"),
    ]
    con.executemany("INSERT INTO notify_hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    monkeypatch.setattr(server, "require_token", _ok)

    async def fake_online(request):
        # s3 挂着 perm 且在线 = 等你拍板（离线会话只按历史判态，复现不了 pending）
        return [{"sid": "s3", "title": "等你拍板", "agent": "claude",
                 "state": "busy", "last": now - 100}]
    monkeypatch.setattr(server, "_ext_sessions_items", fake_online)
    monkeypatch.setattr(server, "TV_FILE", tmp_path / "task_view.json")
    server._TV.clear()
    server._TRIAGE_AT[0] = 0
    yield path
    server._TV.clear()
    server._TRIAGE_AT[0] = 0


def _items():
    r = asyncio.run(server.h_task_ledger(Req()))
    return {it["sid"]: it for it in r.body and __import__("json").loads(r.body)["items"]}


def test_verdict_stale_after_new_activity(env):
    server._TV["s1"] = {"keep": 0, "hide": 1, "at": 500}
    assert server._tv_of("s1", 400) == server._TV["s1"], "判断晚于最近动静：有效"
    assert server._tv_of("s1", 900) == {}, "任务有了新进展：判断翻篇"
    assert server._tv_of("nope", 1) == {}


def test_hide_and_keep_roundtrip(env):
    asyncio.run(server.h_task_view(Req({"sid": "s1", "action": "hide"})))
    it = _items()["s1"]
    assert it["hidden"] == 1 and it["deleted"] == 0
    asyncio.run(server.h_task_view(Req({"sid": "s1", "action": "keep"})))
    it = _items()["s1"]
    assert it["hidden"] == 0 and it["keep"] == 1, "恢复=保留，压过模型结论"


def test_delete_only_leaves_workbench(env):
    asyncio.run(server.h_task_view(Req({"sid": "s2", "action": "delete"})))
    assert "s2" not in _items(), "删除后工作台不再出这一行"
    con = sqlite3.connect(env)
    n = con.execute("SELECT COUNT(*) FROM notify_hist WHERE sid='s2'").fetchone()[0]
    con.close()
    assert n == 1, "删除只影响工作台：历史行不许动"


def test_bad_action_is_400(env):
    r = asyncio.run(server.h_task_view(Req({"sid": "s1", "action": "nuke"})))
    assert r.status == 400


def test_triage_stores_verdicts_and_skips_deciding(env, monkeypatch):
    seen = {}

    async def fake_call(text):
        seen["text"] = text
        return ('```json\n[{"i":0,"keep":0,"why":"测试会话","act":"hide"},'
                '{"i":99,"keep":0}]```')   # 越界序号必须被丢掉

    monkeypatch.setattr(server, "_triage_call", fake_call)
    r = asyncio.run(server.h_task_triage(Req()))
    d = __import__("json").loads(r.body)
    assert d["ok"] and d["judged"] == 1
    assert "要不要删缓存" not in seen["text"], "等你拍板的行不送模型判断"
    it = _items()["s1"]
    assert it["keep"] == 0 and it["vwhy"] == "测试会话"


def test_triage_sees_only_unjudged(env, monkeypatch):
    server._TV["s1"] = {"keep": 1, "at": 10 ** 10}
    calls = []

    async def fake_call(text):
        calls.append(text)
        return "[]"

    monkeypatch.setattr(server, "_triage_call", fake_call)
    asyncio.run(server.h_task_triage(Req()))
    assert "真实任务" not in (calls[0] if calls else ""), "判过的任务不重复送模型"


def test_triage_parse_tolerates_fences():
    assert server._triage_parse('```json\n[{"i":0,"keep":1}]\n```') == [{"i": 0, "keep": 1}]
    assert server._triage_parse("没有 JSON") == []
    assert server._triage_parse("[坏掉的") == []
