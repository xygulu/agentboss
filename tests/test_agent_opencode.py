# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent_opencode 适配器：发现/召回/fork/提醒/降级。

跑法： .venv/bin/python -m pytest tests/test_agent_opencode.py -q
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_opencode as ao
import agents
import shutil

_DDL = """
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  directory TEXT,
  title TEXT,
  agent TEXT,
  model TEXT,
  time_created TEXT,
  time_updated TEXT,
  time_archived TEXT
);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT);
"""


def _mkdb(tmp, cols=_DDL, rows=None):
    p = tmp / "opencode.db"
    db = sqlite3.connect(str(p))
    db.executescript(cols)
    if rows is None:
        rows = [("s_arch", "/w", "归档会话", "build", "gpt", "2026-11-17T08:00:00",
                 "2026-11-17T09:00:00", "2026-11-17T09:30:00"),
                ("s_old", "/w", "旧会话", "build", "gpt", "2026-11-17T09:00:00",
                 "2026-11-17T10:00:00", None),
                ("s_new", "/w", "新会话", "build", "gpt", "2026-11-17T10:30:00",
                 "2026-11-17T11:00:00", None)]
    db.executemany("INSERT INTO session VALUES (%s)"
                   % ",".join("?" * len(rows[0])), rows)
    db.commit()
    db.close()
    return p


def _stub_live(monkeypatch, pid=4242, cwd="/w"):
    monkeypatch.setattr(ao, "_live_pids", lambda: [(pid, "opencode")])
    monkeypatch.setattr(ao, "_pid_cwd", lambda p: cwd if p == pid else None)


# ---- 基本信息 ----

def test_adapter_identity():
    a = agents.get("opencode")
    assert a is not None and a.key == "opencode" and a.label == "OpenCode"
    assert a.eager_session is False
    assert a.can_resume is True and a.can_notify is True
    assert a.can_model_switch is False
    assert a.supports_fork is True


# ---- 起进程 ----

def test_launch_argv(monkeypatch):
    a = agents.get("opencode")
    assert a.launch_argv("/w") == ["opencode"]
    assert a.launch_argv("/w", sid="s1") == ["opencode", "--session", "s1"]
    assert a.launch_argv("/w", sid="s1", model="gpt-5") == \
        ["opencode", "--session", "s1", "-m", "gpt-5"]


def test_fork_argv():
    assert agents.get("opencode").fork_argv("/w", "s1") == \
        ["opencode", "--fork", "--session", "s1"]


def test_installed(monkeypatch):
    a = agents.get("opencode")
    monkeypatch.setattr(shutil, "which", lambda n, **kw: "/usr/local/bin/opencode" if n == "opencode" else None)
    assert a.installed() is True
    monkeypatch.setattr(shutil, "which", lambda n, **kw: None)
    assert a.installed() is False
    assert a.launch_argv("/w") is None


# ---- 发现 ----

def test_discover_picks_newest_per_cwd(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    _stub_live(monkeypatch)
    rows = agents.get("opencode").discover()
    assert len(rows) == 1
    r = rows[0]
    assert r["sid"] == "s_new"
    assert r["pid"] == 4242 and r["cwd"] == "/w"
    assert r["state"] == "busy" and r["agent"] == "opencode"
    assert r["title"] == "新会话" and r["short"] == "s_new"[:8]
    assert "tty" in r and "window_id" in r


def test_discover_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "nope.db"))
    assert agents.get("opencode").discover() == []


def test_discover_two_procs_same_cwd(tmp_path, monkeypatch):
    """同目录两个 opencode 进程：argv 带 --session 的精确认领，新的按启动时刻
    认领次新——同一行不能出两次（2026-11-17 验收发现同 sid 双行、旧会话被遮蔽）。
    行用真实形状（epoch 毫秒整数）钉住启动时刻比较那条路。"""
    p = _mkdb(tmp_path)
    db = sqlite3.connect(str(p))
    db.execute("UPDATE session SET time_updated=? WHERE id='s_new'", (1794882320000,))
    db.execute("UPDATE session SET time_updated=? WHERE id='s_old'", (1794878800000,))
    db.commit()
    db.close()
    monkeypatch.setenv("OPENCODE_DB", str(p))
    monkeypatch.setattr(ao, "_live_pids",
                        lambda: [(101, "opencode --session s_new"),
                                 (102, "opencode")])
    monkeypatch.setattr(ao, "_pid_cwd", lambda x: "/w")
    now = 1794882800.0
    # 101 在 s_new 最近一次更新前 10s 起的；102 比 s_old 的更新还早 100s
    monkeypatch.setattr(ao, "_pid_started",
                        lambda x: now - 490 if x == 101 else now - 4100)
    rows = agents.get("opencode").discover()
    assert sorted(r["sid"] for r in rows) == ["s_new", "s_old"]
    by = {r["sid"]: r for r in rows}
    assert by["s_new"]["pid"] == 101
    assert by["s_old"]["pid"] == 102


def test_discover_ro_fail_falls_to_immutable(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    _stub_live(monkeypatch)
    real = sqlite3.connect
    calls = []

    def fake_connect(arg, *a, **kw):
        calls.append(str(arg))
        if len(calls) == 1:
            assert "mode=ro" in str(arg)
            raise sqlite3.OperationalError("disk I/O error")
        assert "immutable=1" in str(arg)
        return real(arg, *a, **kw)

    monkeypatch.setattr(ao.sqlite3, "connect", fake_connect)
    rows = agents.get("opencode").discover()
    assert len(calls) == 2
    assert [r["sid"] for r in rows] == ["s_new"]


def test_discover_missing_agent_column(tmp_path, monkeypatch):
    """旧版本库里没有 agent 列：照常出会话，别抛异常（Review Focus 5）。"""
    ddl = _DDL.replace("  agent TEXT,\n", "")
    p = _mkdb(tmp_path, cols=ddl, rows=[
        ("s_arch", "/w", "归档会话", "gpt", "2026-11-17T08:00:00",
         "2026-11-17T09:00:00", "2026-11-17T09:30:00"),
        ("s_old", "/w", "旧会话", "gpt", "2026-11-17T09:00:00",
         "2026-11-17T10:00:00", None),
        ("s_new", "/w", "", "gpt", "2026-11-17T10:30:00",
         "2026-11-17T11:00:00", None)])
    monkeypatch.setenv("OPENCODE_DB", str(p))
    _stub_live(monkeypatch)
    rows = agents.get("opencode").discover()
    assert [r["sid"] for r in rows] == ["s_new"]
    assert rows[0]["title"] == ""
    assert rows[0]["agent"] == "opencode"


# ---- 已退出可召回 ----

def test_closed_sessions_archived_only(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    rows = agents.get("opencode").closed_sessions({"s_new"}, 20)
    # s_arch：opencode 自己归档的；s_old：进程死了但没归档（实测 1.19.5 杀进程
    # 不写 time_archived，2026-11-17 验收发现）——两类都可 --session 召回
    assert {r["sid"] for r in rows} == {"s_arch", "s_old"}
    for r in rows:
        assert r["resumable"] is True and r["state"] == "exited"
        assert r["agent"] == "opencode" and r["last"]


def test_closed_sessions_live_not_listed(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    rows = agents.get("opencode").closed_sessions({"s_new", "s_old"}, 20)
    assert {r["sid"] for r in rows} == {"s_arch"}


def test_closed_sessions_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "nope.db"))
    assert agents.get("opencode").closed_sessions(set(), 20) == []


# ---- 转录 ----

_EXPORT = [
    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "帮我看看 main.py"}]},
    {"info": {"role": "assistant"}, "parts": [
        {"type": "text", "text": "看过了，有个空指针。"},
        {"type": "tool", "tool": "read", "state": {"output": "main.py 10 行"}}]},
]


def test_transcript_path_and_meta(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    a = agents.get("opencode")
    ref = a.transcript_path("/w", "s_new")
    assert ref is not None
    assert ref.stat().st_mtime > 0
    cwd, title = a.transcript_meta(ref)
    assert cwd == "/w" and title == "新会话"


def test_ref_stat_shape(tmp_path, monkeypatch):
    """Ref.stat() 的形状是 server 的契约：推送循环吃 (st_mtime, st_size)
    （server.py 11130），_tr_load 吃 (st_mtime_ns, st_size)（server.py 8827）。
    库在位给真 os.stat_result；库文件中途被删也不能 AttributeError——SSE 连接会当场崩。"""
    monkeypatch.setenv("OPENCODE_DB", str(_mkdb(tmp_path)))
    ref = agents.get("opencode").transcript_path("/w", "s_new")
    st = ref.stat()
    assert st.st_mtime > 0 and st.st_mtime_ns > 0 and st.st_size > 0
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "gone.db"))
    st = ao.Ref("s_new").stat()
    assert st.st_mtime == 0.0 and st.st_mtime_ns == 0 and st.st_size == 0


def test_discover_row_older_than_proc_skipped(tmp_path, monkeypatch):
    """「会话得比进程新」守卫必须真生效（I4：sel 里得有 time_updated）：
    老进程挂着旧库时，启动前就存在的行不能被认领成 busy。"""
    now = 1794882800.0
    p = _mkdb(tmp_path, rows=[
        ("s_old", "/w", "老会话", "build", "gpt", "", int((now - 400) * 1000), None)])
    monkeypatch.setenv("OPENCODE_DB", str(p))
    monkeypatch.setattr(ao, "_live_pids", lambda: [(4242, "opencode")])
    monkeypatch.setattr(ao, "_pid_cwd", lambda p_: "/w" if p_ == 4242 else None)
    monkeypatch.setattr(ao, "_pid_started", lambda p_: now - 100)
    assert agents.get("opencode").discover() == []


def test_parse_transcript_via_export(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(_mkdb(tmp_path)))
    monkeypatch.setattr(ao, "_export", lambda sid: json.dumps(_EXPORT))
    msgs = agents.get("opencode").parse_transcript("opencode:s_new")
    assert msgs == [
        {"role": "user", "text": "帮我看看 main.py", "tools": []},
        {"role": "assistant", "text": "看过了，有个空指针。",
         "tools": [{"name": "read", "detail": "main.py 10 行"}]}]


def test_parse_transcript_db_fallback(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("OPENCODE_DB", str(p))
    db = sqlite3.connect(str(p))
    db.executemany("INSERT INTO message VALUES (?,?,?)",
                   [("m1", "s_new", json.dumps({"role": "user"})),
                    ("m2", "s_new", json.dumps({"role": "assistant"}))])
    db.executemany("INSERT INTO part VALUES (?,?,?)",
                   [("p1", "m1", json.dumps({"type": "text", "text": "hi"})),
                    ("p2", "m2", json.dumps({"type": "text", "text": "yo"}))])
    db.commit()
    db.close()
    monkeypatch.setattr(ao, "_export", lambda sid: (_ for _ in ()).throw(OSError("nope")))

    def _ref(sid):
        return ao.Ref(sid)

    msgs = agents.get("opencode").parse_transcript(_ref("s_new"))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "hi" and msgs[1]["text"] == "yo"
    # limit 生效：只要最后 1 条
    msgs1 = agents.get("opencode").parse_transcript(_ref("s_new"), limit=1)
    assert [m["role"] for m in msgs1] == ["assistant"]


# ---- 跑完提醒 ----

def test_notify_event():
    d = ao.notify_event({"type": ao.SESSION_EVENT_TYPE, "sid": "s9",
                         "cwd": "/w", "last": "跑完了"})
    assert d["session_id"] == "s9" and d["hook_event_name"] == "Stop"
    assert d["agent"] == "opencode" and d["cwd"] == "/w"
    assert d["last_assistant_message"] == "跑完了"
    assert ao.notify_event({"type": "other", "sid": "s9"}) == {}
    assert ao.notify_event({"type": ao.SESSION_EVENT_TYPE}) == {}
    assert ao.notify_event("junk") == {}


# ---- 提醒 hook（插件文件）----

def test_hook_install_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(ao, "_port", lambda: 8790)
    a = agents.get("opencode")
    assert a.hook_install(True) == 0
    f = tmp_path / "opencode" / "plugins" / "agentboss-notify.js"
    assert f.exists()
    body = f.read_text()
    # I1：活 token 一行都不许烤进插件文件（0644）——运行期从 token.txt 读
    tok_file = Path(__file__).resolve().parent.parent / "token.txt"
    if tok_file.exists():
        for ln in tok_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                assert ln not in body
    assert "token.txt" in body
    assert "codeboss_t=" in body       # cookie 运行期拼 token
    assert "127.0.0.1:8790" in body
    assert ao.SESSION_EVENT_TYPE in body
    assert "appendFileSync" in body    # I5：出错落日志，不再全吞（静默无提醒无从排查）
    assert a.hooks_installed() is True
    evs, path = a.hook_status()
    assert evs == ["session.idle"] and str(path) == str(f)
    assert a.hook_install(False) == 0
    assert not f.exists()
