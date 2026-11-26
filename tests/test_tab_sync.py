# SPDX-License-Identifier: AGPL-3.0-or-later

"""电脑终端 tab 的钉名同步：wrapper 重生（换模型/召回/host 重启）后新进程 pin 是空的，
记账必须认「世代」（进程 pid）——只按 sid+名字记，重生后就永远跳过，
claude 的原始 OSC 标题（ai-title + ◐ 转圈）一直漏到窗口上（2026-11-25 实锤）。

跑法： .venv/bin/python -m pytest tests/test_tab_sync.py -q
"""
import server


def _sess(sid, pid, title):
    return {"sid": sid, "pid": pid, "title": title}


def _setup(monkeypatch, metas):
    """摆两个世代的 meta，_pty_ctl_write 记下推送。返回推送列表。"""
    pushes = []
    monkeypatch.setattr(server, "_meta_for_pid", lambda p: metas.get(p))
    monkeypatch.setattr(
        server, "_pty_ctl_write",
        lambda m, d: (pushes.append((m.get("pid"), bytes(d))), True)[1])
    server._TAB_SYNC.clear()
    return pushes


def test_repushes_when_wrapper_generation_changes(monkeypatch):
    """同一会话 wrapper 重生（pid 111→222）、名字没变：必须重推，否则新进程没钉，
    claude 的标题串裸奔到窗口上。"""
    metas = {111: {"pid": 111, "ctl": "/x", "wv": 1},
             222: {"pid": 222, "ctl": "/x", "wv": 1}}
    pushes = _setup(monkeypatch, metas)
    want = b"\x00T:" + "agentboss 副本".encode("utf-8") + b"\n"
    server._sync_titles([_sess("s1", 111, "agentboss 副本")])
    assert pushes == [(111, want)]
    server._sync_titles([_sess("s1", 111, "agentboss 副本")])   # 同世代同名：不重推
    assert len(pushes) == 1
    server._sync_titles([_sess("s1", 222, "agentboss 副本")])   # 重生：重推
    assert pushes == [(111, want), (222, want)]


def test_repushes_when_title_changes(monkeypatch):
    """改名要跟：同名同世代不推，名字一变就推。"""
    metas = {7: {"pid": 7, "ctl": "/x", "wv": 1}}
    pushes = _setup(monkeypatch, metas)
    server._sync_titles([_sess("s2", 7, "旧名")])
    server._sync_titles([_sess("s2", 7, "旧名")])
    assert len(pushes) == 1
    server._sync_titles([_sess("s2", 7, "新名")])
    assert len(pushes) == 2


def test_skips_sessions_without_wrapper_meta(monkeypatch):
    """没经 wrapper 的会话（第三方终端）没有 ctl 通道：不推，也不记账。"""
    pushes = _setup(monkeypatch, {9: {"pid": 9, "ctl": None, "wv": 1}})
    server._sync_titles([_sess("s3", 9, "名字"),
                         _sess("s4", None, "名字")])
    assert pushes == []
    assert server._TAB_SYNC == {}
