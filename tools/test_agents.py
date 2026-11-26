# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent 适配层自检：确认「抽出 AgentAdapter 之后 Claude 的路径一个字都没变」。

跑法：
    .venv/bin/python -m pytest tools/test_agents.py -q
    .venv/bin/python tools/test_agents.py          # 不带 pytest 也能跑

含一个「跟 git HEAD 的老实现逐条对拍」的用例：把 HEAD 的 server.py 拷到临时目录，
分别跑老 discover() 和新 _claude_discover()，比较会话集合/标题/目录/状态。
本机没装 claude 时这条会跳过（不像别的用例那样假通过）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agents       # noqa: E402
import server       # noqa: E402
import screen       # 弹层识别已独立成 screen.py


# ---------------------------------------------------------------- 归属判定

def test_agent_of_defaults_to_claude():
    agents.forget_agents()
    agents.set_lookup(lambda sid: None)
    assert agents.agent_of("sid-nobody-knows") == "claude"
    assert agents.adapter_of("sid-nobody-knows").key == "claude"


def test_agent_of_uses_registry_lookup():
    agents.forget_agents()
    agents.set_lookup(lambda sid: "codex" if sid == "kd-1" else None)
    try:
        assert agents.agent_of("kd-1") == "codex"
        # 查过一次就进内存缓存：外部查法变了也不影响（重启才重查）
        agents.set_lookup(lambda sid: None)
        assert agents.agent_of("kd-1") == "codex"
    finally:
        agents.forget_agents()
        agents.set_lookup(server._reg_agent)


def test_agent_of_probes_transcripts():
    """登记表没有、缓存也没有时，谁的转录在盘上就是谁的。"""
    class _Fake(agents.AgentAdapter):
        key = "fake"
        label = "Fake"

        def transcript_path(self, cwd, sid):
            return Path("/tmp/whatever") if sid == "fake-1" else None

    agents.forget_agents()
    agents.register(_Fake())
    try:
        assert agents.agent_of("fake-1") == "fake"
        assert agents.agent_of("not-fake") == "claude"
    finally:
        agents._ADAPTERS.pop("fake", None)     # 只摘掉这个假适配器
        agents.forget_agents()


# ---------------------------------------------------------------- Claude 的启动命令

def test_claude_launch_argv_matches_legacy():
    ad = agents.get("claude")
    assert ad.launch_argv("/tmp") == ["claude"]
    assert ad.launch_argv("/tmp", sid="abc") == ["claude", "--resume", "abc"]
    assert ad.launch_argv("/tmp", sid="abc", model="MiniMax-M3") == \
        ["claude", "--resume", "abc", "--model", "MiniMax-M3"]
    # 「默认」是原生 provider 的占位，不是真实模型名 —— 老代码有意不传
    assert ad.launch_argv("/tmp", sid="abc", model="默认") == ["claude", "--resume", "abc"]
    assert ad.fork_argv("/tmp", "abc") == \
        ["claude", "--resume", "abc", "--fork-session"]


def test_open_wrapped_terminal_builds_same_argv(monkeypatch):
    """召回会话的命令行：wrapper 前缀 + --meta（sid 常驻，镜像日志按会话 sid 续命）
    + claude --resume。"""
    got = {}

    def _fake_open(cwd, argv, name=None, term=""):
        got["cwd"], got["argv"], got["name"] = cwd, list(argv), name
        return 123, "/dev/ttys999", None

    monkeypatch.setattr(server.plat, "open_wrapped", _fake_open)
    wid, tty, err = server._open_wrapped_terminal("/tmp/proj", "abcd-1234", model="MiniMax-M3")
    assert not err and wid == 123 and tty == "/dev/ttys999"
    assert got["argv"] == [sys.executable, str(server.WRAPPER), "--meta",
                           json.dumps({"sid": "abcd-1234"}, ensure_ascii=False), "--",
                           "claude", "--resume", "abcd-1234", "--model", "MiniMax-M3"]
    assert got["name"] == "codeboss-abcd-123"

    # extra_meta 走 --meta，位置跟以前一样在最前面
    server._open_wrapped_terminal("/tmp/proj", "abcd-1234",
                                 extra_meta={"sid": "abcd-1234"})
    assert got["argv"][:4] == [sys.executable, str(server.WRAPPER), "--meta",
                               json.dumps({"sid": "abcd-1234"}, ensure_ascii=False)]


def test_unknown_agent_reports_missing(monkeypatch):
    """没装的 agent 要明说，不能静默当成成功。"""
    monkeypatch.setattr(server.agents, "_ADAPTERS",
                        dict(server.agents._ADAPTERS, ghost=_NoLaunch()))
    agents.note_agent("ghost-1", "ghost")      # 登记表里记着这个 sid 是 ghost 的
    try:
        wid, tty, err = server._open_wrapped_terminal("/tmp", "ghost-1")
        assert wid is None and tty is None
        assert "没装" in err
    finally:
        server.agents._ADAPTERS.pop("ghost", None)
        agents.forget_agents()


class _NoLaunch(agents.AgentAdapter):
    key = "ghost"
    label = "Ghost"

    def launch_argv(self, cwd, sid=None, model=None):
        return None


# ---------------------------------------------------------------- 弹层识别

def test_dialog_hints_unchanged_for_claude():
    """默认参数走的就是原来的 _DLG_HINTS/_DLG_HINT，Claude 的底栏照样认得。"""
    ad = agents.get("claude")
    line = "Enter to select · ↑/↓ to navigate · Esc to cancel"
    assert screen._dlg_hint_line(line) is True
    assert screen._dlg_hint_line(line, ad.dialog_hints(), ad.dialog_prefixes()) is True
    # 正文里引用这些词（带句读、不是底栏形状）仍旧不认
    assert screen._dlg_hint_line("按 Esc to cancel 可以取消，Enter to select 是确认。") is False


def test_dialog_hints_are_per_agent():
    """Codex 的底栏文案不该被 Claude 那套认成弹层（反过来也一样）。"""
    claude = agents.get("claude")
    codex_line = "press enter to confirm or esc to cancel"
    assert screen._dlg_hint_line(codex_line, claude.dialog_hints(), claude.dialog_prefixes()) is False


# ---------------------------------------------------------------- 已退出会话

def test_closed_sessions_merges_like_before():
    """只有 Claude 一个 adapter 时，合并结果必须跟老函数逐条一致。"""
    live = {x["sid"] for x in server._tick_sessions()}
    legacy = server._claude_closed_sessions(live)
    # Codex 的已退出会话现在也并进来；这条只保证 Claude 那部分逐条没变
    merged = [x for x in server._closed_sessions(live) if x.get("agent") == "claude"]
    assert [x["sid"] for x in merged] == [x["sid"] for x in legacy]
    for a, b in zip(merged, legacy):
        assert ({k: v for k, v in a.items() if k != "agent"}
                == {k: v for k, v in b.items() if k != "agent"})


# ---------------------------------------------------------------- 跟 HEAD 对拍

def _git_show(path, ref="HEAD"):
    r = subprocess.run(["git", "show", "%s:%s" % (ref, path)], cwd=str(HERE.parent),
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def test_discover_matches_head():
    """老 discover() 与新 _claude_discover() 在同一时刻必须认出同一批会话。

    只比稳定字段（sid/标题/目录/状态）：pid、窗口 id 这类会随会话重开变化，
    拿它们做断言只会造出一个随机红的测试。"""
    if not shutil.which(server.CLAUDE_BIN) and not Path(server.CLAUDE_BIN).exists():
        print("skip: 本机没装 claude")
        return
    old_src = _git_show("codeboss/server.py")
    if not old_src or "def _claude_discover" in old_src:
        print("skip: HEAD 里已经是新版 server.py")
        return
    tmp = tempfile.mkdtemp(prefix="cb_head_")
    try:
        (Path(tmp) / "server.py").write_text(old_src, encoding="utf-8")
        for f in ("plat.py", "pty_backend.py"):
            shutil.copy2(HERE / f, Path(tmp) / f)
        code = ("import json, sys; sys.path.insert(0, %r); import server; "
                "print(json.dumps(server.discover(force=True)))" % tmp)
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp,
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            print("skip: 老实现跑不起来:", r.stderr[-400:])
            return
        old = json.loads(r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    new = server._claude_discover()
    key = lambda x: x["sid"]
    oldm = {key(x): x for x in old}
    newm = {key(x): x for x in new}
    assert set(oldm) == set(newm), ("hello 会话集合不一致",
                                    set(oldm) - set(newm), set(newm) - set(oldm))
    for sid, o in oldm.items():
        n = newm[sid]
        for f in ("cwd", "title", "state"):
            assert o.get(f) == n.get(f), (sid[:8], f, o.get(f), n.get(f))
        assert n.get("agent") == "claude"


if __name__ == "__main__":
    import types
    m = types.SimpleNamespace()
    m.MonkeyPatch = None
    fails = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                def _setattr(obj, k, val):
                    old = getattr(obj, k)
                    setattr(obj, k, val)
                    fn._restore = getattr(fn, "_restore", []) + [(obj, k, old)]
                mp = types.SimpleNamespace(setattr=_setattr)
                try:
                    fn(mp)
                finally:
                    for obj, k, old in reversed(getattr(fn, "_restore", [])):
                        setattr(obj, k, old)
            else:
                fn()
            print("PASS", name)
        except Exception as e:
            fails += 1
            print("FAIL", name, "->", repr(e)[:300])
    print("%d passed, %d failed" % (len(tests) - fails, fails))
    sys.exit(1 if fails else 0)
