# SPDX-License-Identifier: AGPL-3.0-or-later

"""产品线能力面要一以贯之：CodeBoss 客户端看不见 hermes，哪一页都一样。

2026-11-19 用户：会话列表里只有 Claude/Codex/OpenCode，工作台里却冒出 hermes 的
任务——同一台机器、同一个客户端，两个页面两套口径。可见集合只该有一个来源
（_agent_face），会话、台账、待决定、提醒历史都按它过滤。
"""
import asyncio
import json
import sqlite3
import sys
import time
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
    remote = "t"

    def __init__(self, product="codeboss", body=None, query=None):
        self.headers = {"X-Product": product}
        self.cookies = {"cb_product": product}
        self.query = query or {}
        self.match_info = {}
        self._b = body or {}

    async def json(self):
        return self._b


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    now = int(time.time())
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.executemany("INSERT INTO notify_hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, now - 300, "h1", "hermes 的活", "done", "干完了", "整理资料", "", "", "",
         "/work/hermes", "", 0, "", "", "{}"),
        (2, now - 200, "c1", "claude 的活", "done", "干完了", "改登录", "", "", "", "", "", 0, "", "", "{}"),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "TV_FILE", tmp_path / "task_view.json")
    server._TV.clear()
    monkeypatch.setattr(server.agents, "agent_of",
                        lambda sid, cwd="": "hermes" if str(sid).startswith("h") else "claude")

    async def fake_online(request):
        return [{"sid": "h1", "title": "hermes 的活", "agent": "hermes",
                 "state": "busy", "last": now - 300}]
    monkeypatch.setattr(server, "_ext_sessions_items", fake_online)
    monkeypatch.setattr(server, "_pending_list", lambda: [
        {"sid": "h1", "agent": "hermes", "title": "hermes 的活", "rid": "", "ts": now - 60},
        {"sid": "c1", "agent": "claude", "title": "claude 的活", "rid": "", "ts": now - 60}])
    yield path
    server._TV.clear()


def _body(r):
    return json.loads(r.body)


def test_ledger_hides_hermes_for_codeboss(env):
    items = _body(asyncio.run(server.h_task_ledger(Req("codeboss"))))["items"]
    sids = {it["sid"] for it in items}
    assert "c1" in sids, "claude/编码面的会话照常"
    assert "h1" not in sids, "codeboss 客户端的工作台不许出现 hermes（和会话列表一致）"


def test_ledger_keeps_hermes_for_agentboss(env):
    items = _body(asyncio.run(server.h_task_ledger(Req("agentboss"))))["items"]
    assert {"c1", "h1"} <= {it["sid"] for it in items}, "agentboss 全量，hermes 要在"


def test_pending_hides_hermes_for_codeboss(env):
    items = _body(asyncio.run(server.h_ext_pending(Req("codeboss"))))["items"]
    assert [it["sid"] for it in items] == ["c1"]


def test_notify_hist_hides_hermes_for_codeboss(env):
    items = _body(asyncio.run(server.h_notify_hist(Req("codeboss"))))["items"]
    assert {it["sid"] for it in items} == {"c1"}


def test_offline_ledger_row_resolves_agent_by_cwd(env, monkeypatch):
    """会话已退出、台账只剩历史行（agent 空着）：按 cwd 推出来也要认得出 hermes。"""
    monkeypatch.setattr(server, "_ext_sessions_items", _no_online)
    monkeypatch.setattr(server.agents, "agent_of",
                        lambda sid, cwd="": "hermes" if "hermes" in (cwd or "") else "claude")
    codeboss = _body(asyncio.run(server.h_task_ledger(Req("codeboss"))))["items"]
    assert "h1" not in {it["sid"] for it in codeboss}
    agentboss = _body(asyncio.run(server.h_task_ledger(Req("agentboss"))))["items"]
    assert "h1" in {it["sid"] for it in agentboss}


async def _no_online(request):
    return []


def test_face_is_one_rule():
    face = server._agent_face(server.branding.PRODUCT_CODEBOSS)
    assert "hermes" not in face and "claude" in face
    assert server._agent_face(server.branding.PRODUCT_AGENTBOSS) is None
