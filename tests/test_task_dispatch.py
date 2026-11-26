# SPDX-License-Identifier: AGPL-3.0-or-later

"""派单（B2）：手机指定 agent + 目录 + 内容 → 电脑上开新会话干活 → 台账里跟着走。

以前台账只有「电脑上已经开着的会话」——手机只能看和回，不能派活。现在：
- POST /api/task_new 先落一条 tasks 行（dispatching），立刻返回 tid，后台开干；
- 后台 = 复用 sess_new 的建会话路径 → 等会话出现 → _send_verified 发正文 → running；
  哪步失败都记 state=failed + err，台账里看得见（不能静默消失）。
- 台账合并：任务有 sid 就盖在同一条会话行上（标题用任务标题）；
  还没 sid 的（派单中/失败）自己占一行，sid="task:<tid>"。

跑法： .venv/bin/python -m pytest tests/test_task_dispatch.py -q
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

DB = [None]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    DB[0] = path
    con = sqlite3.connect(path)
    server._db_migrate(con)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    return path


def _opt(obj, **kw):
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


def test_task_new_records_dispatching_row(db, monkeypatch):
    fired = []

    async def _d(tid):
        fired.append(tid)
    monkeypatch.setattr(server, "_task_dispatch", _d)
    r = asyncio.run(server._task_new_core(
        {"content": "跑一遍测试\n顺便看看覆盖率", "agent": "claude", "cwd": str(Path.home())}))
    assert r["ok"] and r["tid"]
    assert fired == [r["tid"]], "落库之后必须真的把活发出去（后台任务）"
    t = server._task_get(r["tid"])
    assert t["state"] == "dispatching"
    assert t["title"] == "跑一遍测试", "标题取内容第一行"


def test_task_new_fills_default_agent(db, monkeypatch):
    """没指定 agent：按默认 agent 落库（台账行要带 agent 才过产品线过滤）。"""
    class Ad:
        key = "claude"
    monkeypatch.setattr(server, "_pick_agent", lambda d: (Ad(), ""))
    fired = []

    async def _d(tid):
        fired.append(tid)
    monkeypatch.setattr(server, "_task_dispatch", _d)
    r = asyncio.run(server._task_new_core({"content": "x", "cwd": str(Path.home())}))
    assert r["ok"] and server._task_get(r["tid"])["agent"] == "claude"


def test_task_new_needs_content(db, monkeypatch):
    async def _d(tid):
        raise AssertionError("没内容不该建任务")
    monkeypatch.setattr(server, "_task_dispatch", _d)
    r = asyncio.run(server._task_new_core({"content": "  "}))
    assert not r["ok"]


def test_task_new_rejects_missing_dir(db, monkeypatch):
    async def _d(tid):
        raise AssertionError("目录不存在不该建任务")
    monkeypatch.setattr(server, "_task_dispatch", _d)
    r = asyncio.run(server._task_new_core(
        {"content": "x", "cwd": str(Path.home() / "nope-不存在-xyz")}))
    assert not r["ok"] and "目" in r["error"]


def _make_task(body="干活", cwd="/tmp"):
    tid = server._task_add("干活", "claude", cwd, body)
    return tid


def test_dispatch_creates_session_and_sends(db, monkeypatch, tmp_path):
    tid = server._task_add("干活", "claude", str(tmp_path), "把登录页改蓝")
    sent = {}

    async def _sess_new(data):
        sent["data"] = data
        return {"ok": True, "sid": "S9"}

    async def _find(sid):
        return {"sid": sid, "tty": "/dev/x"}

    async def _send(x, text):
        sent["text"] = text
        return True, ""
    monkeypatch.setattr(server, "_sess_new_core", _sess_new)
    monkeypatch.setattr(server, "_find_session", _find)
    monkeypatch.setattr(server, "_send_verified", _send)

    asyncio.run(server._task_dispatch(tid))
    t = server._task_get(tid)
    assert t["state"] == "running" and t["sid"] == "S9"
    assert t["cwd"] == str(tmp_path)
    assert sent["text"] == "把登录页改蓝", "派单的正文就是要发进会话的那句话"
    assert sent["data"]["agent"] == "claude" and sent["data"]["cwd"] == str(tmp_path)


def test_dispatch_failure_is_recorded(db, monkeypatch, tmp_path):
    tid = server._task_add("干活", "codex", str(tmp_path), "x")

    async def _sess_new(data):
        return {"ok": False, "error": "这台电脑上没装 codex"}
    monkeypatch.setattr(server, "_sess_new_core", _sess_new)
    asyncio.run(server._task_dispatch(tid))
    t = server._task_get(tid)
    assert t["state"] == "failed"
    assert "没装 codex" in t["err"]


def test_dispatch_waits_for_session_when_no_sid(db, monkeypatch, tmp_path):
    """Mac 的 open_wrapped 不返回 sid：按「新出现的、同目录的会话」认。"""
    tid = server._task_add("干活", "claude", str(tmp_path), "你好")
    calls = {"n": 0}

    async def _sess_new(data):
        return {"ok": True}

    def _tick(force=False):
        calls["n"] += 1
        if calls["n"] < 3:
            return [{"sid": "OLD", "cwd": str(tmp_path)}]
        return [{"sid": "OLD", "cwd": str(tmp_path)},
                {"sid": "NEW1", "cwd": str(tmp_path)}]

    async def _find(sid):
        return {"sid": sid} if sid == "NEW1" else None

    async def _send(x, text):
        return True, ""
    monkeypatch.setattr(server, "_sess_new_core", _sess_new)
    monkeypatch.setattr(server, "_cached_sessions", lambda: [{"sid": "OLD", "cwd": str(tmp_path)}])
    monkeypatch.setattr(server, "_tick_sessions", _tick)
    monkeypatch.setattr(server, "_find_session", _find)
    monkeypatch.setattr(server, "_send_verified", _send)
    monkeypatch.setattr(server, "_TASK_POLL", 0.01)

    asyncio.run(server._task_dispatch(tid))
    assert server._task_get(tid)["sid"] == "NEW1"
    assert server._task_get(tid)["state"] == "running"


def test_overlay_takes_title_over_session_row():
    rows = [{"sid": "S1", "title": "自动起的名字", "agent": "claude", "state": "running",
             "last_ts": 100}]
    tasks = [{"tid": "t1", "title": "把登录页改蓝", "agent": "claude", "cwd": "/p",
              "sid": "S1", "state": "running", "created": 50, "updated": 90}]
    out = server._task_overlay(rows, tasks)
    r = next(x for x in out if x["sid"] == "S1")
    assert r["title"] == "把登录页改蓝" and r["tid"] == "t1"


def test_overlay_shows_task_without_session():
    rows = []
    tasks = [{"tid": "t2", "title": "装个依赖", "agent": "codex", "cwd": "/p",
              "sid": "", "state": "failed", "err": "没装 codex", "created": 50, "updated": 60}]
    out = server._task_overlay(rows, tasks)
    r = next(x for x in out if x["sid"] == "task:t2")
    assert r["state"] == "failed" and "没装" in r["last_summary"]
    assert r["title"] == "装个依赖"


def test_dispatch_ui_wiring():
    """工作台派单入口 + 对话框（node 门禁；没 node 就跳）。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_task_dispatch_ui.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]


def test_ledger_handler_includes_tasks(db, monkeypatch):
    tid = server._task_add("装个依赖", "codex", "/p", "x")

    async def _sess_items(request):
        return []
    monkeypatch.setattr(server, "_ext_sessions_items", _sess_items)

    async def _token(request):
        return None
    monkeypatch.setattr(server, "require_token", _token)

    class Req:
        headers = {"X-Product": "agentboss"}
        cookies = {"cb_product": "agentboss"}
        remote = "t"
    r = asyncio.run(server.h_task_ledger(Req()))
    items = __import__("json").loads(r.text)["items"]
    row = next(x for x in items if x["sid"] == "task:" + tid)
    assert row["state"] == "dispatching", "派单中看不到的话，失败就彻底静默了"
