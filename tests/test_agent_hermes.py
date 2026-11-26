# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent_hermes 适配器：发现/召回/转录/提醒/config.yaml hook 合并。

跑法： .venv/bin/python -m pytest tests/test_agent_hermes.py -q
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_hermes as ah
import agents
import shutil

# 实测 DDL（~/.hermes/state.db，v0.15.1）裁出来的相关列
_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source TEXT,
  cwd TEXT,
  title TEXT,
  model TEXT,
  started_at REAL,
  ended_at REAL,
  archived INTEGER DEFAULT 0,
  message_count INTEGER DEFAULT 0
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  role TEXT,
  content TEXT,
  tool_calls TEXT,
  tool_name TEXT,
  timestamp REAL
);
"""

# 实测 v0.15.1：TUI 会话 source='cli'、cwd 列不写（398 行全空）；渠道会话
# source='weixin'/…、cwd NULL。t_tui=活 TUI；t_channel=渠道；t_arch=归档；
# t_old=已退出 TUI；t_api=api_server 的会话（不是 TUI）
_ROWS = [
    ("t_tui", "cli", "", "终端会话", "m1", 1794880000.0, None, 0, 3),
    ("t_channel", "weixin", None, "渠道会话", "m1", 1794870000.0, 1794880000.0, 0, 5),
    ("t_arch", "cli", "", "归档会话", "m1", 1794860000.0, 1794870000.0, 1, 2),
    ("t_old", "cli", "", "旧会话", "m1", 1794850000.0, 1794860000.0, 0, 2),
    ("t_api", "api_server", "", "接口会话", "m1", 1794880050.0, None, 0, 1),
]


def _mkdb(tmp, rows=None):
    p = tmp / "state.db"
    db = sqlite3.connect(str(p))
    db.executescript(_DDL)
    db.executemany("INSERT INTO sessions VALUES (%s)"
                   % ",".join("?" * len(_ROWS[0])), rows or _ROWS)
    db.commit()
    db.close()
    return tmp


def _stub_live(monkeypatch, pid=4242, cwd="/w", started=None, procs=None):
    if procs is None:
        procs = [(pid, "hermes chat")]
    monkeypatch.setattr(ah, "_live_pids", lambda: procs)
    monkeypatch.setattr(ah, "_pid_cwd", lambda p: cwd if p in [q for q, _ in procs] else None)
    monkeypatch.setattr(ah, "_pid_tty", lambda p: "/dev/ttys004" if p in [q for q, _ in procs] else "")
    if started is not None:
        monkeypatch.setattr(ah, "_pid_started", lambda p: started)


# ---- 基本信息 ----

def test_adapter_identity():
    a = agents.get("hermes")
    assert a is not None and a.key == "hermes" and a.label == "Hermes"
    assert a.eager_session is False
    assert a.can_resume is True and a.can_notify is True
    assert a.can_model_switch is False
    assert a.supports_fork is False


# ---- 起进程 ----

def test_launch_argv(monkeypatch):
    a = agents.get("hermes")
    monkeypatch.setattr(shutil, "which", lambda n, **kw: "/usr/local/bin/hermes" if n == "hermes" else None)
    assert a.launch_argv("/w") == ["hermes", "chat"]
    assert a.launch_argv("/w", sid="20261116_225003_96e30144") == \
        ["hermes", "chat", "--resume", "20261116_225003_96e30144"]
    assert a.fork_argv("/w", "s1") is None


def test_installed(monkeypatch):
    a = agents.get("hermes")
    monkeypatch.setattr(shutil, "which", lambda n, **kw: "/usr/local/bin/hermes" if n == "hermes" else None)
    assert a.installed() is True
    monkeypatch.setattr(shutil, "which", lambda n, **kw: None)
    assert a.launch_argv("/w") is None


# ---- 发现 ----

def test_discover_tui_only(tmp_path, monkeypatch):
    """只认领 TUI 会话：渠道（weixin）、api_server、归档行不出（Review Focus 3）。
    cwd 列 hermes 不写——输出里的 cwd 用 ps 的。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    _stub_live(monkeypatch, started=1794880000.0 - 100)
    rows = agents.get("hermes").discover()
    assert len(rows) == 1
    r = rows[0]
    assert r["sid"] == "t_tui" and r["pid"] == 4242 and r["cwd"] == "/w"
    assert r["state"] == "busy" and r["agent"] == "hermes"
    assert r["title"] == "终端会话" and r["short"] == "t_tui"[:8]
    assert r["tty"] == "/dev/ttys004"   # 插话按 tty 定位终端 tab，行上必须带
    assert "window_id" in r


def test_discover_resume_argv_precise(tmp_path, monkeypatch):
    """--resume <sid> 的进程精确认领那行（该行可以比进程老——resume 本来就是
    开新进程续老会话）。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    _stub_live(monkeypatch, pid=777, started=1794880000.0 + 500,
               procs=[(777, "hermes chat --resume t_old")])
    rows = agents.get("hermes").discover()
    assert len(rows) == 1
    assert rows[0]["sid"] == "t_old" and rows[0]["pid"] == 777


def test_discover_two_procs_pairing(tmp_path, monkeypatch):
    """两个 TUI 进程：新进程领最新行、老进程领次新行；gateway 进程不认领。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    _stub_live(monkeypatch, procs=[
        (201, "python -m hermes_cli.main gateway run --replace"),
        (202, "hermes chat"),
        (203, "hermes chat")])
    monkeypatch.setattr(ah, "_pid_started",
                        lambda p: {201: 1794880500.0, 202: 1794880000.0 - 50,
                                   203: 1794850000.0 - 50}[p])
    rows = agents.get("hermes").discover()
    by = {r["sid"]: r for r in rows}
    assert set(by) == {"t_tui", "t_old"}
    assert by["t_tui"]["pid"] == 202
    assert by["t_old"]["pid"] == 203


def test_discover_row_older_than_proc_skipped(tmp_path, monkeypatch):
    """库里最新的行比进程还老（老进程挂着新库）→ 不认领，别把别人的会话抢过来。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    _stub_live(monkeypatch, started=1794880000.0 + 100)
    assert agents.get("hermes").discover() == []


def test_discover_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
    assert agents.get("hermes").discover() == []


def test_discover_ro_fail_falls_to_immutable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    _stub_live(monkeypatch, started=1794880000.0 - 100)
    real = sqlite3.connect
    calls = []

    def fake_connect(arg, *a, **kw):
        calls.append(str(arg))
        if len(calls) == 1:
            assert "mode=ro" in str(arg)
            raise sqlite3.OperationalError("disk I/O error")
        assert "immutable=1" in str(arg)
        return real(arg, *a, **kw)

    monkeypatch.setattr(ah.sqlite3, "connect", fake_connect)
    rows = agents.get("hermes").discover()
    assert len(calls) == 2
    assert [r["sid"] for r in rows] == ["t_tui"]


# ---- 已退出可召回 ----

def test_closed_sessions(tmp_path, monkeypatch):
    """只出有 cwd 且未 archived 的行（brief Step 1）；活集合里的不出。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    rows = agents.get("hermes").closed_sessions({"t_tui"}, 20)
    assert {r["sid"] for r in rows} == {"t_old"}
    for r in rows:
        assert r["resumable"] is True and r["state"] == "exited"
        assert r["agent"] == "hermes" and r["last"]


def test_closed_sessions_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
    assert agents.get("hermes").closed_sessions(set(), 20) == []


def test_source_column_missing_fails_closed(tmp_path, monkeypatch):
    """source 列没了（hermes 升版改 schema）= 读不懂 → 宁可看不见：
    兜底 1=1 会把 weixin/api_server 渠道会话泄进手机列表（I3）。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = sqlite3.connect(str(tmp_path / "state.db"))
    db.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT,"
        " model TEXT, started_at REAL, ended_at REAL,"
        " archived INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0);")
    db.execute("INSERT INTO sessions VALUES"
               " ('x1', '/w', '渠道会话', 'm', 1794880000.0, NULL, 0, 1)")
    db.commit()
    db.close()
    _stub_live(monkeypatch, started=1794880000.0 - 100)
    a = agents.get("hermes")
    assert a.discover() == []
    assert a.closed_sessions(set(), 20) == []


# ---- 转录 ----

def test_transcript_path_and_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    a = agents.get("hermes")
    ref = a.transcript_path("/w", "t_tui")
    assert ref is not None
    assert ref.stat().st_mtime > 0
    cwd, title = a.transcript_meta(ref)
    assert cwd == "" and title == "终端会话"   # cwd 列 hermes 不写，title 是主键近亲


def test_ref_stat_shape(tmp_path, monkeypatch):
    """Ref.stat() 的形状是 server 的契约：推送循环吃 (st_mtime, st_size)
    （server.py 11130），_tr_load 吃 (st_mtime_ns, st_size)（server.py 8827）。
    库在位给真 os.stat_result；库缺席也不能 AttributeError——SSE 连接会当场崩。"""
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    ref = agents.get("hermes").transcript_path("/w", "t_tui")
    st = ref.stat()
    assert st.st_mtime > 0 and st.st_mtime_ns > 0 and st.st_size > 0
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
    st = ah.Ref("t_tui").stat()   # transcript_path 库缺席返回 None，直接构造 Ref 打 fallback
    assert st.st_mtime == 0.0 and st.st_mtime_ns == 0 and st.st_size == 0


_EXPORT_MSGS = [
    {"role": "user", "content": "看看 main.py"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"function": {"name": "terminal", "arguments": "cat main.py"}}]},
    {"role": "tool", "tool_name": "terminal", "content": '{"output": "10 行"}'},
    {"role": "session_meta", "content": None},
    {"role": "assistant", "content": "搞定了"},
]


def _jsonl(msgs):
    return json.dumps({"id": "t1", "messages": msgs}, ensure_ascii=False) + "\n"


def test_parse_transcript_via_export(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_mkdb(tmp_path)))
    monkeypatch.setattr(ah, "_export", lambda sid: _jsonl(_EXPORT_MSGS))
    msgs = agents.get("hermes").parse_transcript("hermes:t1")
    # tool 结果折进前一条 assistant 的工具项（detail 用结果，不用入参）
    assert msgs == [
        {"role": "user", "text": "看看 main.py", "tools": []},
        {"role": "assistant", "text": "",
         "tools": [{"name": "terminal", "detail": '{"output": "10 行"}'}]},
        {"role": "assistant", "text": "搞定了", "tools": []}]


def test_parse_transcript_db_fallback(tmp_path, monkeypatch):
    home = _mkdb(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = sqlite3.connect(str(home / "state.db"))
    db.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
        " VALUES (?,?,?,?,?,?)",
        [("t1", "user", "hi", None, None, 1.0),
         ("t1", "assistant", "", json.dumps([{"function": {"name": "read",
                                                           "arguments": "main.py"}}]), None, 2.0),
         ("t1", "tool", "yo", None, "read", 3.0)])
    db.commit()
    db.close()
    monkeypatch.setattr(ah, "_export", lambda sid: (_ for _ in ()).throw(OSError("nope")))
    msgs = agents.get("hermes").parse_transcript("hermes:t1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "hi"
    assert msgs[1]["tools"][0]["detail"] == "yo"
    # limit 生效
    msgs1 = agents.get("hermes").parse_transcript("hermes:t1", limit=1)
    assert [m["role"] for m in msgs1] == ["assistant"]


# ---- 跑完提醒 ----

def test_notify_event():
    # 插件式（带 type）与 hook.sh 原样转发（hermes 的 stdin payload）两种都认
    d = ah.notify_event({"type": ah.SESSION_EVENT_TYPE, "sid": "t9",
                         "cwd": "/w", "last": "跑完了"})
    assert d["session_id"] == "t9" and d["hook_event_name"] == "Stop"
    assert d["agent"] == "hermes" and d["cwd"] == "/w"
    assert d["last_assistant_message"] == "跑完了"
    d = ah.notify_event({"hook_event_name": "on_session_end", "session_id": "t1",
                         "cwd": "/w", "extra": {"completed": True}})
    assert d["session_id"] == "t1" and d["hook_event_name"] == "Stop"
    assert d["agent"] == "hermes" and d["cwd"] == "/w"
    assert d["last_assistant_message"] == ""
    assert ah.notify_event({"type": "other", "sid": "t9"}) == {}
    assert ah.notify_event({"hook_event_name": "pre_tool_call"}) == {}
    assert ah.notify_event("junk") == {}


# ---- config.yaml hook 合并（纯函数）----

_CFG_EMPTY = "model: x\nhooks: {}\nproviders:\n  a: 1\n"
_CFG_USER = ("model: x\n"
             "hooks:\n"
             "  pre_tool_call:\n"
             "  - matcher: \"write_file|patch\"\n"
             "    command: \"~/.hermes/agent-hooks/auto-format.sh\"\n"
             "providers:\n"
             "  a: 1\n")


def test_merge_hooks_empty_roundtrip():
    mon = ah._hook_command()
    t = ah.hermes_merge_hooks(_CFG_EMPTY, True)
    assert "on_session_end" in t
    assert mon in t
    assert "timeout" in t
    # 用户其它内容逐字节不动
    assert t.startswith("model: x\n") and t.endswith("providers:\n  a: 1\n")
    # 幂等
    assert ah.hermes_merge_hooks(t, True) == t
    # 卸载还原
    assert ah.hermes_merge_hooks(t, False) == _CFG_EMPTY


def test_merge_hooks_user_keys_preserved():
    t = ah.hermes_merge_hooks(_CFG_USER, True)
    # 用户自有条目原样保留，我们的加在 on_session_end 下
    assert 'command: "~/.hermes/agent-hooks/auto-format.sh"' in t
    assert "on_session_end" in t and ah._hook_command() in t
    assert ah.hermes_merge_hooks(t, True) == t      # 幂等
    assert ah.hermes_merge_hooks(t, False) == _CFG_USER   # 逐字节还原


def test_merge_hooks_stale_entry_replaced():
    old = ah._hook_command()
    t = ah.hermes_merge_hooks(_CFG_EMPTY, True)
    drifted = t.replace(old, "/old/place/hook.sh hermes")
    t2 = ah.hermes_merge_hooks(drifted, True)
    assert t2.count("hook.sh hermes") == 1
    assert ah._hook_command() in t2 and "/old/place/" not in t2


def test_merge_hooks_nonempty_flow_untouched():
    """非空 flow 风格 hooks（用户手写）：块状条目插进 flow 映射后面必是非法
    YAML，hermes 读配置直接崩——docstring 承诺不动，就真不动（I2）。"""
    cfg = ('model: x\n'
           'hooks: {pre_tool_call: [{command: "~/x.sh"}]}\n'
           'providers:\n  a: 1\n')
    assert ah.hermes_merge_hooks(cfg, True) == cfg
    assert ah.hermes_merge_hooks(cfg, False) == cfg


# ---- hook 装卸（写 config.yaml）----

def test_hook_install_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(_CFG_EMPTY, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = agents.get("hermes")
    assert a.hook_install(True) == 0
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    assert "on_session_end" in cfg
    # 备份的是原始内容
    assert (home / "config.yaml.agentboss-bak").read_text(encoding="utf-8") == _CFG_EMPTY
    assert a.hooks_installed() is True
    evs, path = a.hook_status()
    assert evs == ["on_session_end"] and path == str(home / "config.yaml")
    assert a.hook_install(False) == 0
    assert (home / "config.yaml").read_text(encoding="utf-8") == _CFG_EMPTY


# ---- 同意名单（shell-hooks-allowlist.json）----
# 手机召回没有 TTY：hermes 第一次看到我们的 hook 会弹 y/N，没人能按 → hook 被跳过，
# 提醒就静默没了（2026-11-22 用户）。装 hook 时把我们自己这一条记进同意名单；
# 不用 --accept-hooks（那是全局放行，用户自己的其它 hook 也会一起批）。

def test_hook_install_approves_our_hook(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(_CFG_EMPTY, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = agents.get("hermes")
    assert a.hook_install(True) == 0
    data = json.loads((home / "shell-hooks-allowlist.json").read_text(encoding="utf-8"))
    assert {"event": "on_session_end", "command": ah._hook_command()} in [
        {"event": e.get("event"), "command": e.get("command")} for e in data["approvals"]]


def test_hook_approval_keeps_other_entries_and_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(_CFG_EMPTY, encoding="utf-8")
    (home / "shell-hooks-allowlist.json").write_text(json.dumps(
        {"approvals": [{"event": "pre_tool_call", "command": "~/x.sh"}]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = agents.get("hermes")
    assert a.hook_install(True) == 0
    assert a.hook_install(True) == 0
    data = json.loads((home / "shell-hooks-allowlist.json").read_text(encoding="utf-8"))
    cmds = [e.get("command") for e in data["approvals"]]
    assert "~/x.sh" in cmds, "用户自己的同意记录不许动"
    assert cmds.count(ah._hook_command()) == 1, "我们的只记一条"


def test_hooks_installed_false_until_approved(tmp_path, monkeypatch):
    """配置里挂着我们的 hook、但没同意 → 要算「没装好」，
    _hooks_guard 每 5 分钟一次的自愈才会去补同意。"""
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(ah.hermes_merge_hooks(_CFG_EMPTY, True),
                                      encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = agents.get("hermes")
    assert a.hooks_installed() is False
    assert a.hook_install(True) == 0
    assert a.hooks_installed() is True


def test_launch_does_not_globally_accept_hooks():
    a = agents.get("hermes")
    for sid in (None, "sess_x"):
        argv = a.launch_argv("/w", sid=sid) or []
        assert "--accept-hooks" not in argv, \
            "全局放行会把用户自己的 hook 也一起批；我们只批自己那一条"
