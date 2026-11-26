# SPDX-License-Identifier: AGPL-3.0-or-later

"""Codex 适配器自检：rollout 解析 / 找会话 / notify 提醒。

跑法：
    .venv/bin/python tools/test_agent_codex.py
    .venv/bin/python -m pytest tools/test_agent_codex.py -q

全部在自己的临时 CODEX_HOME 里造数据，不读也不写真实的 ~/.codex。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_codex   # noqa: E402
import agents        # noqa: E402
import server        # noqa: E402
import screen       # 弹层识别已独立成 screen.py

SID_A = "01a1c0c3-14b6-7a02-b057-8864778ef3b8"
SID_B = "01a1c0c4-aaaa-7b03-9034-aa1122334455"


def _home():
    """每个用例一个干净的 CODEX_HOME。"""
    d = tempfile.mkdtemp(prefix="cb_codex_")
    os.environ["CODEX_HOME"] = d
    # roots() 默认还会带上真实的 ~/.codex（老会话可能在那边）——测试不需要，
    # 否则开发机上真有一个 Codex 会话就会把「已退出列表」的断言带偏。
    agent_codex.roots = lambda: [Path(d)]
    agent_codex._META.clear()
    agent_codex._STATE.clear()
    agent_codex._PATH_CACHE.clear()
    agent_codex._MISS.clear()
    agent_codex._THREADS_CACHE.update(t=0.0, rows=[])
    return Path(d)


def _rollout(home, sid, lines, day="2026/11/09"):
    d = home / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("rollout-2026-11-09T22-42-52-%s.jsonl" % sid)
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                 encoding="utf-8")
    return p


def _meta(cwd="/tmp/proj", source="cli"):
    return {"type": "session_meta",
            "payload": {"session_id": SID_A, "cwd": cwd, "source": source,
                        "originator": "codex_cli_rs"}}


def _msg(role, text):
    key = "input_text" if role == "user" else "output_text"
    return {"type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": key, "text": text}]}}


def _call(name, args):
    return {"type": "response_item",
            "payload": {"type": "function_call", "name": name,
                        "arguments": json.dumps(args), "call_id": "c1"}}


def _out():
    return {"type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1",
                        "output": "ok"}}


def _ev(kind):
    return {"type": "event_msg", "payload": {"type": kind}}


# ---------------------------------------------------------------- 转录解析

def test_parse_transcript_basics():
    home = _home()
    p = _rollout(home, SID_A, [
        _meta(),
        _msg("user", "<environment_context>cwd=/tmp</environment_context>"),
        _msg("user", "修一下登录页的报错"),
        _msg("assistant", "先看一下代码"),
        _call("exec_command", {"cmd": "rg 'login' -n web/"}),
        _out(),
    ])
    msgs = agent_codex.parse_rollout(p)
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
    assert msgs[0]["text"] == "修一下登录页的报错"      # 注入的上下文不算人说的话
    assert msgs[1]["text"] == "先看一下代码"
    assert msgs[2]["tools"] and msgs[2]["tools"][0]["name"] == "exec_command"
    assert "rg 'login'" in msgs[2]["tools"][0]["detail"]


def test_parse_transcript_merges_and_truncates_tail():
    home = _home()
    p = _rollout(home, SID_A, [
        _meta(), _msg("user", "一"), _msg("user", "二"), _msg("assistant", "答")])
    msgs = agent_codex.parse_rollout(p)
    assert msgs[0]["text"] == "一\n二"      # 连续同角色、中间没工具调用：合并
    assert len(agent_codex.parse_rollout(p, limit=1)) == 1


def test_parse_transcript_bad_lines_ignored():
    home = _home()
    d = home / "sessions/2026/11/09"
    d.mkdir(parents=True)
    p = d / ("rollout-x-%s.jsonl" % SID_A)
    p.write_text('{"type":"session_meta","payload":{}}\nnot json\n'
                 + json.dumps(_msg("user", "在吗")) + "\n", encoding="utf-8")
    assert agent_codex.parse_rollout(p)[0]["text"] == "在吗"


def test_rollout_meta_and_state():
    home = _home()
    p = _rollout(home, SID_A, [
        _meta(cwd="/Users/x/proj"), _msg("user", "  把\n登录页  修好  "), _ev("task_started")])
    assert agent_codex.rollout_meta(p) == ("/Users/x/proj", "把 登录页 修好")
    assert agent_codex.rollout_state(p) == "busy"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_ev("task_complete")) + "\n")
    assert agent_codex.rollout_state(p) == "idle"


# ---------------------------------------------------------------- 找会话文件

def test_rollout_path_by_glob_and_sqlite():
    home = _home()
    p = _rollout(home, SID_A, [_meta()])
    assert agent_codex.rollout_path(SID_A) == p
    # state 库指到别处（比如归档目录）也要认
    q = home / "moved" / p.name
    q.parent.mkdir()
    p.rename(q)
    agent_codex._PATH_CACHE.clear()
    db = home / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, archived INT)")
    con.execute("INSERT INTO threads VALUES(?,?,0)", (SID_A, str(q)))
    con.commit()
    con.close()
    assert agent_codex.rollout_path(SID_A) == q


def test_rollout_path_miss_is_cached_but_recovers():
    home = _home()
    assert agent_codex.rollout_path(SID_B) is None
    p = _rollout(home, SID_B, [_meta()])
    assert agent_codex.rollout_path(SID_B) is None   # 5s 内不重复扫盘
    agent_codex._MISS.clear()
    assert agent_codex.rollout_path(SID_B) == p


# ---------------------------------------------------------------- 信任目录
# 手机在某个目录开会话时电脑前没人点「Trust this folder?」，codex 会停在信任框、
# 连 thread 都不建，列表里就永远看不到这个会话。开之前替用户把它记成 trusted。

def test_ensure_trusted_adds_and_updates_preserving_rest():
    home = _home()
    cfg = home / "config.toml"
    cfg.write_text('notify = ["/x/hook.sh", "codex"]\n\n'
                   '[projects."/a"]\ntrust_level = "untrusted"\n', encoding="utf-8")
    assert agent_codex.ensure_trusted("/b") is True
    t = cfg.read_text(encoding="utf-8")
    assert '[projects."/b"]' in t and 'trust_level = "trusted"' in t
    assert 'notify = ["/x/hook.sh", "codex"]' in t          # 别的内容一个字节不动
    before = cfg.read_text(encoding="utf-8")
    assert agent_codex.ensure_trusted("/b") is True          # 已经 trusted：不再写
    assert cfg.read_text(encoding="utf-8") == before
    assert agent_codex.ensure_trusted("/a") is True
    t = cfg.read_text(encoding="utf-8")
    assert 'trust_level = "untrusted"' not in t
    assert t.count('[projects."/a"]') == 1
    assert 'notify' in t
    print("信任目录：新增 / 幂等 / 改 untrusted→trusted，别的配置不动")


def test_sid_from_argv():
    assert agent_codex._sid_from_argv("/x/bin/codex resume %s --model m" % SID_A) == SID_A
    assert agent_codex._sid_from_argv("/x/bin/codex fork %s" % SID_B) == SID_B
    assert agent_codex._sid_from_argv("/x/bin/codex") == ""
    assert agent_codex._sid_from_argv("") == ""


def test_threads_for_cwd_matches_startup_thread():
    home = _home()
    rp = _rollout(home, SID_A, [_meta(cwd="/tmp/p")])
    db = home / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT,"
                " created_at REAL, cwd TEXT, archived INT)")
    con.execute("INSERT INTO threads VALUES(?,?,?,?,0)", (SID_A, str(rp), 1000.0, "/tmp/p"))
    con.execute("INSERT INTO threads VALUES(?,?,?,?,0)", (SID_B, str(rp), 9000.0, "/tmp/p"))
    con.commit()
    con.close()
    agent_codex._THREADS_CACHE.update(t=0.0, rows=[])
    got = agent_codex._threads_for_cwd("/tmp/p", 1004.0)
    assert [x[1] for x in got] == [SID_A]        # 按启动时刻认领，不串到另一个会话
    assert agent_codex._threads_for_cwd("/tmp/other", 1004.0) == []
    print("thread 认领：同目录按启动时刻对上，别的目录不串")


# ---------------------------------------------------------------- 已退出会话

def test_closed_sessions_skips_live_and_archived():
    home = _home()
    a = _rollout(home, SID_A, [_meta()])
    b = _rollout(home, SID_B, [_meta(cwd="/tmp/other"), _msg("user", "另一个")])
    os.utime(a, (time.time() - 100, time.time() - 100))
    db = home / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, archived INT)")
    con.execute("INSERT INTO threads VALUES(?,?,1)", (SID_B, str(b)))
    con.commit()
    con.close()
    ad = agent_codex.Adapter()
    got = ad.closed_sessions(live_sids=set())
    assert [x["sid"] for x in got] == [SID_A]        # 归档的不出现，最新的排前面
    assert got[0]["agent"] == "codex" and got[0]["resumable"] is True
    assert got[0]["cwd"] == "/tmp/proj"
    assert ad.closed_sessions(live_sids={SID_A}) == []


def test_delete_transcript_removes_file():
    home = _home()
    p = _rollout(home, SID_A, [_meta()])
    ad = agent_codex.Adapter()
    old = agent_codex.codex_bin
    agent_codex.codex_bin = lambda: None       # 不真跑 codex delete（测试机可能装着）
    try:
        assert ad.delete_transcript(SID_A) == 1
        assert not p.exists()
        assert ad.delete_transcript(SID_A) == 0
    finally:
        agent_codex.codex_bin = old


# ---------------------------------------------------------------- 启动命令

def test_launch_and_fork_argv():
    ad = agent_codex.Adapter()
    old_bin, old_home = agent_codex.codex_bin, agent_codex.codex_home
    no_home = Path(tempfile.mkdtemp(prefix="cbnohome-")) / "codex-absent"   # 不存在的目录
    agent_codex.codex_bin = lambda: "/opt/bin/codex"
    agent_codex.codex_home = lambda: no_home
    try:
        assert ad.launch_argv("/tmp") == ["/opt/bin/codex"]
        assert ad.launch_argv("/tmp", sid=SID_A) == ["/opt/bin/codex", "resume", SID_A]
        assert ad.launch_argv("/tmp", sid=SID_A, model="默认")[-2:] == ["resume", SID_A]
        assert ad.launch_argv("/tmp", sid=SID_A, model="gpt-5")[-2:] == ["--model", "gpt-5"]
        assert ad.fork_argv("/tmp", SID_A) == ["/opt/bin/codex", "fork", SID_A]
        assert ad.installed() is True
        agent_codex.codex_bin = lambda: None
        assert ad.launch_argv("/tmp") is None      # 没装：明说，不假装
        assert ad.fork_argv("/tmp", SID_A) is None
        assert ad.installed() is False             # 手机端「＋」据此不列 Codex
    finally:
        agent_codex.codex_bin, agent_codex.codex_home = old_bin, old_home


# ---------------------------------------------------------------- notify 提醒

def test_notify_event_translates_to_stop():
    ev = agent_codex.notify_event({
        "type": "agent-turn-complete", "thread-id": SID_A, "cwd": "/tmp/p",
        "input-messages": ["<environment_context>x", "把登录页修好"],
        "last-assistant-message": "改完了，\n要不要现在发布？"})
    assert ev["session_id"] == SID_A
    assert ev["hook_event_name"] == "Stop"
    assert ev["prompt"] == "把登录页修好"
    assert ev["last_assistant_message"] == "改完了， 要不要现在发布？"
    assert agent_codex.notify_event({"type": "other"}) == {}
    assert agent_codex.notify_event({"type": "agent-turn-complete"}) == {}   # 没 thread-id
    assert agent_codex.notify_event(None) == {}


def test_notify_install_and_uninstall_restore_user_notify():
    home = _home()
    cfg = home / "config.toml"
    before = ('model = "gpt-5"\n'
              'notify = ["notify-send", "Codex 干完了"]\n'
              '\n[projects."/tmp/p"]\ntrust_level = "trusted"\n')
    cfg.write_text(before, encoding="utf-8")
    chain = agent_codex.notify_chain_path()      # 跟 hook.sh 里写死的是同一个路径
    assert chain.parent == home
    ad = agent_codex.Adapter(notify_argv=lambda: ["/x/hook.sh", "codex"])
    assert ad.hooks_installed() is False
    assert ad.hook_install(True) == 0
    got = cfg.read_text(encoding="utf-8")
    assert '["/x/hook.sh", "codex"]' in got
    assert 'model = "gpt-5"' in got and 'trust_level' in got     # 别的内容一个字节不动
    assert chain.exists() and "notify-send" in chain.read_text(encoding="utf-8")
    assert ad.hooks_installed() is True
    # 重装第二次不该把「我们自己的那行」当成用户原来的 notify 存进转发脚本
    assert ad.hook_install(True) == 0
    assert "notify-send" in chain.read_text(encoding="utf-8")
    assert ad.hook_install(False) == 0
    back = cfg.read_text(encoding="utf-8")
    assert "/x/hook.sh" not in back and not chain.exists()
    assert back == before                    # 还原必须一个字节都不差


def test_notify_install_without_existing_notify():
    home = _home()
    cfg = home / "config.toml"
    cfg.write_text('[projects."/tmp/p"]\ntrust_level = "trusted"\n', encoding="utf-8")
    ad = agent_codex.Adapter(notify_argv=lambda: ["/x/hook.sh", "codex"])
    assert ad.hook_install(True) == 0
    assert "codex" in cfg.read_text(encoding="utf-8")
    assert ad.hook_install(False) == 0
    t = cfg.read_text(encoding="utf-8")
    assert "notify" not in t and "trust_level" in t
    assert agent_codex._NOTIFY_MARK not in t      # 标记注释也别留下孤零零一行


def test_notify_multiline_array_is_replaced_whole():
    home = _home()
    cfg = home / "config.toml"
    cfg.write_text('notify = [\n  "a",\n  "b=c",\n]\nmodel = "m"\n', encoding="utf-8")
    assert agent_codex._toml_notify(cfg.read_text(encoding="utf-8")) == ["a", "b=c"]
    new = agent_codex._set_notify(cfg.read_text(encoding="utf-8"), ["/x", "codex"])
    assert '"b=c"' not in new and new.count("notify") == 1
    assert 'model = "m"' in new


def test_windows_like_platform_has_no_hook():
    ad = agent_codex.Adapter()          # notify_argv=None
    assert ad.hook_spec() == []
    assert ad.hooks_installed() is True
    assert ad.hook_install(True) == 0


# ---------------------------------------------------------------- 弹层识别

def test_codex_dialog_footer_recognized():
    ad = agents.get("codex")
    assert ad is not None
    line = "Press enter to confirm or esc to cancel"
    assert screen._dlg_hint_line(line, ad.dialog_hints(), ad.dialog_prefixes()) is True
    assert screen._dlg_hint_line("enter select · esc back",
                                 ad.dialog_hints(), ad.dialog_prefixes()) is True
    # Claude 的底栏不该被 Codex 这套认成弹层
    claude = agents.get("claude")
    assert screen._dlg_hint_line("Enter to select · ↑/↓ to navigate · Esc to cancel",
                                 ad.dialog_hints(), ad.dialog_prefixes()) is False
    assert claude is not None


if __name__ == "__main__":
    os.environ.setdefault("CODEBOSS_TEST", "1")
    fails = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            fails += 1
            import traceback
            traceback.print_exc()
            print("FAIL", name, "->", repr(e)[:300])
        else:
            print("PASS", name)
    print("%d passed, %d failed" % (len(tests) - fails, fails))
    sys.exit(1 if fails else 0)
