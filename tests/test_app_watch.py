# SPDX-License-Identifier: AGPL-3.0-or-later

"""App 自己更新完没起来时，host 兜底把它拉起来。

App 的更新器会「换包 → 重启自己」，0.9.63 那版因为 open 先于退出，重启失败后直接消失
（用户 2026-11-20 两次反馈）。0.9.64 修了更新器，但**装 0.9.64 这一跳本身还是旧代码跑的**，
所以还要 host 看着：盘上版本变了、App 又没在跑 → open。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import server

_NOT_GIT = (128, "", "not a git repository")


def _run(pgrep_rc):
    def f(args, **k):
        if "rev-parse" in args:
            return _NOT_GIT
        if "/usr/bin/pgrep" in args:
            return (pgrep_rc, "", "")
        return (0, "", "")
    return f


def _app(tmp_path):
    p = tmp_path / "BossHost.app"
    p.mkdir()
    return str(p)


def _watch(monkeypatch, tmp_path, ver, running, seen):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_run", _run(0 if running else 1))
    monkeypatch.setattr(server, "_app_bundle_version", lambda p: ver)
    got = []
    server._app_update_watch(paths=[_app(tmp_path)], opener=got.append, seen=seen)
    return got


def test_opens_when_version_changed_and_app_gone(tmp_path, monkeypatch):
    seen = {"ver": "0.9.63"}
    assert _watch(monkeypatch, tmp_path, "0.9.64", False, seen) == [str(tmp_path / "BossHost.app")]
    assert seen["ver"] == "0.9.64"


def test_no_open_when_app_still_running(tmp_path, monkeypatch):
    assert _watch(monkeypatch, tmp_path, "0.9.64", True, {"ver": "0.9.63"}) == []


def test_no_open_when_version_unchanged(tmp_path, monkeypatch):
    assert _watch(monkeypatch, tmp_path, "0.9.64", False, {"ver": "0.9.64"}) == []


def test_first_tick_only_remembers(tmp_path, monkeypatch):
    """服务刚起来：只记基线，不替用户开 App（启动时那一步已经开过了）。"""
    seen = {"ver": ""}
    assert _watch(monkeypatch, tmp_path, "0.9.64", False, seen) == []
    assert seen["ver"] == "0.9.64"


def test_non_mac_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    got = []
    server._app_update_watch(paths=[_app(tmp_path)], opener=got.append, seen={"ver": "0.9.63"})
    assert got == []


def test_watch_loop_is_registered():
    import inspect
    assert "_app_update_watch_startup" in inspect.getsource(server.main)
