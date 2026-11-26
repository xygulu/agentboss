#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""任务台账聚合 _task_rows:任务=sid 生命周期,五态机。

批次二 #5:跨 agent 跨机器台账。五态(五态之外不许出现第五态):
  running(在跑) / deciding(等你拍板) / done(办结待验收)
  accepted(已通过) / rejected(已打回)

跑法:.venv/bin/python -m pytest tests/test_task_ledger.py -q
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

STATES = {"running", "deciding", "done", "accepted", "rejected"}
NOW = 1_800_000_000


def _h(sid, kind, dt=0, state="", summary="", about="", n=1, title="任务标题"):
    return {"n": n, "ts": NOW - dt, "sid": sid, "title": title, "kind": kind,
            "state": state, "summary": summary, "about": about, "act": "", "text": ""}


def test_busy_with_pending_perm_is_deciding():
    online = [{"sid": "s1", "title": "改样式", "agent": "claude", "state": "busy",
               "last": NOW - 60, "host": False}]
    hist = [
        _h("s1", "done", dt=7200, n=1, summary="改完了"),
        _h("s1", "perm", dt=300, n=2, state="", about="要不要删缓存"),
    ]
    rows = server._task_rows(online, hist, now=NOW)
    r = next(x for x in rows if x["sid"] == "s1")
    assert r["state"] == "deciding"
    assert r["waited"] and r["waited"] > 0, "deciding 必须带等你拍板的时长"
    assert r["rounds"] == 1  # 轮次=办结行计数,perm 行不算一轮


def test_idle_done_row_accepted_state():
    online = [{"sid": "s2", "title": "跑测试", "agent": "codex", "state": "idle",
               "last": NOW - 3600, "host": False}]
    hist = [_h("s2", "done", dt=3600, n=5, state="accepted", summary="全绿")]
    r = next(x for x in server._task_rows(online, hist, now=NOW) if x["sid"] == "s2")
    assert r["state"] == "accepted"


def test_exited_session_aggregates_from_history_only():
    """退出的会话不在 online:纯历史聚合,态仍必须在五态内。"""
    online = []
    hist = [_h("s3", "done", dt=86400, n=9, summary="搞定了", title="昨天的活")]
    rows = server._task_rows(online, hist, now=NOW)
    r = next(x for x in rows if x["sid"] == "s3")
    assert r["state"] in STATES
    assert r["state"] == "done"
    assert r["title"] == "昨天的活"  # 离线从历史行兜底标题


def test_online_without_history_is_running():
    online = [{"sid": "s4", "title": "新会话", "agent": "claude", "state": "busy",
               "last": NOW, "host": False}]
    rows = server._task_rows(online, [])
    r = next(x for x in rows if x["sid"] == "s4")
    assert r["state"] == "running"
    assert r["rounds"] == 0


def test_all_states_within_five_and_running_priority():
    """任何输入组合都不许出现五态之外的值;rejected 的办结行同样识别。
    busy(在跑,含打回后的返工)优先于历史验收态——状态反映「现在」。"""
    online = [
        {"sid": "a", "title": "t", "agent": "claude", "state": "idle", "last": NOW, "host": False},
        {"sid": "b", "title": "t", "agent": "codex", "state": "exited", "last": NOW, "host": False},
        {"sid": "c", "title": "t", "agent": "claude", "state": "busy", "last": NOW, "host": False},
    ]
    hist = [
        _h("a", "done", dt=600, n=1, state="rejected"),
        _h("a", "perm", dt=100, n=2, state="done"),   # 旧待拍板已被清账,不算挂着
        _h("b", "done", dt=300, n=3, summary="完"),
        _h("c", "done", dt=60, n=4, state="rejected"),  # 打回后正在返工
    ]
    rows = server._task_rows(online, hist, now=NOW)
    assert all(x["state"] in STATES for x in rows)
    assert next(x for x in rows if x["sid"] == "a")["state"] == "rejected"
    assert next(x for x in rows if x["sid"] == "b")["state"] == "done"
    assert next(x for x in rows if x["sid"] == "c")["state"] == "running"


def test_last_summary_three_round_summaries_joined():
    """验收摘要=末三轮轮次概括拼接(零新增模型调用);总长 ≤320。"""
    online = [{"sid": "s5", "title": "t", "agent": "claude", "state": "idle",
               "last": NOW, "host": False}]
    hist = [
        _h("s5", "done", dt=9000, n=1, summary="第一轮做了骨架"),
        _h("s5", "done", dt=6000, n=2, summary="第二轮补了测试"),
        _h("s5", "done", dt=3000, n=3, summary="第三轮修了边界"),
        _h("s5", "done", dt=600, n=4, summary="第四轮全绿收尾"),
        _h("s5", "perm", dt=100, n=5, state="done", summary="不该混进来的概括"),
    ]
    r = next(x for x in server._task_rows(online, hist, now=NOW) if x["sid"] == "s5")
    s = r["last_summary"]
    assert "第四轮" in s and "第三轮" in s and "第二轮" in s, s   # 末三条
    assert "第一轮" not in s, "只取末三轮"
    assert "不该混进来" not in s, "办结行之外的概括不混入"
    assert len(s) <= 320


def test_last_summary_falls_back_to_about():
    online = [{"sid": "s6", "title": "t", "agent": "claude", "state": "idle",
               "last": NOW, "host": False}]
    hist = [_h("s6", "done", dt=600, n=1, summary="", about="在改登录页")]
    r = next(x for x in server._task_rows(online, hist, now=NOW) if x["sid"] == "s6")
    assert r["last_summary"] == "在改登录页"


def test_last_summary_all_empty_no_crash():
    online = [{"sid": "s7", "title": "t", "agent": "claude", "state": "idle",
               "last": NOW, "host": False}]
    hist = [_h("s7", "done", dt=600, n=1, summary="", about="")]
    r = next(x for x in server._task_rows(online, hist, now=NOW) if x["sid"] == "s7")
    assert r["last_summary"] == ""


# ── 老板旗舰挂线(用户 2026-11-18 拍板):台账/验收只在 agentboss 线 ──
import asyncio
from types import SimpleNamespace


def _req(product):
    return SimpleNamespace(headers={}, cookies={"cb_product": product}, remote="t")


async def _no_items(request):
    return []


def test_ledger_ok_for_codeboss_line(monkeypatch):
    """2026-11-19 用户改口径:两条线都叫「工作台」,台账/验收不再老板线专属。"""
    monkeypatch.setattr(server, "require_token", _async_ok)
    monkeypatch.setattr(server, "_ext_sessions_items", _fake_items)
    monkeypatch.setattr(server, "_db", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    r = asyncio.run(server.h_task_ledger(_req("codeboss")))
    assert r.status == 200


def test_ledger_ok_for_xproduct_header(monkeypatch):
    """X-Product 头优先于 cookie:工头壳的头也放行。"""
    monkeypatch.setattr(server, "require_token", _async_ok)
    monkeypatch.setattr(server, "_ext_sessions_items", _fake_items)
    monkeypatch.setattr(server, "_db", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    req = SimpleNamespace(headers={"X-Product": "codeboss"},
                          cookies={"cb_product": "agentboss"}, remote="t")
    r = asyncio.run(server.h_task_ledger(req))
    assert r.status == 200


async def _fake_items(request):
    return []


def test_ledger_ok_for_boss_line(monkeypatch):
    monkeypatch.setattr(server, "require_token", _async_ok)
    monkeypatch.setattr(server, "_ext_sessions_items", _fake_items)
    monkeypatch.setattr(server, "_db", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    r = asyncio.run(server.h_task_ledger(_req("agentboss")))
    assert r.status == 200


def test_accept_works_for_codeboss_line(monkeypatch):
    """工头线也能验收(工作台合并后两线同一页)。"""
    monkeypatch.setattr(server, "require_token", _async_ok)
    seen = {}

    def _core(sid, verdict, note):
        seen["x"] = (sid, verdict, note)
        return {"ok": True}

    monkeypatch.setattr(server, "_task_accept_core", _core)

    class Req:
        headers = {}
        cookies = {"cb_product": "codeboss"}
        remote = "t"

        async def json(self):
            return {"sid": "s1", "verdict": "accept"}

    r = asyncio.run(server.h_task_accept(Req()))
    assert r.status == 200
    assert seen["x"] == ("s1", "accept", "")


async def _async_ok(request):
    return None


def test_perm_superseded_by_later_done_not_pending(monkeypatch):
    """旧 perm(state='')在办结行之后:已被新一轮取代,不许永久挂「等你拍板」。
    (清账窗口只有 6h,perm 挂过夜后 state 仍是空——排序判定兜住)"""
    monkeypatch.setattr(server, "_np_get", lambda: {"max_text": 160})
    online = [{"sid": "s8", "title": "过夜的活", "agent": "claude", "state": "idle",
               "last": NOW, "host": False}]
    hist = [
        _h("s8", "perm", dt=25200, n=1, state="", about="昨晚问你的"),   # 7h 前
        _h("s8", "done", dt=600, n=2, state="", summary="今早跑完了"),   # 10 分钟前办结
    ]
    r = next(x for x in server._task_rows(online, hist, now=NOW) if x["sid"] == "s8")
    assert r["state"] == "done", "旧 perm 被后续办结取代,不许再挂 deciding"
