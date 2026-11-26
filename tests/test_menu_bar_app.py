# SPDX-License-Identifier: AGPL-3.0-or-later

"""装完/更新完把菜单栏 App 拉起来。

以前只有 install.sh 里有这一步：host 走「下载包 → 覆盖文件 → 原地重启」的
自动更新路径时根本不跑 install.sh，所以更新完 App 还是不见（用户 2026-11-20 反馈）。
现在 host 每次启动都补一次「有 App 但没在跑就打开」。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import server

_NOT_GIT = (128, "", "not a git repository")
_IS_GIT = (0, ".git\n", "")


def _app(tmp_path):
    p = tmp_path / "BossHost.app"
    p.mkdir()
    return str(p)


def test_opens_installed_app_on_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_run", lambda *a, **k: _NOT_GIT)
    got = []
    app = _app(tmp_path)
    assert server._open_menu_bar_app(paths=[app], opener=got.append) == app
    assert got == [app]


def test_skips_git_checkout(tmp_path, monkeypatch):
    """开发机跑 server.py 就弹一个 App 太烦：源码部署（git 检出）不碰。"""
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_run", lambda *a, **k: _IS_GIT)
    got = []
    assert server._open_menu_bar_app(paths=[_app(tmp_path)], opener=got.append) == ""
    assert got == []


def test_skips_when_app_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_run", lambda *a, **k: _NOT_GIT)
    got = []
    assert server._open_menu_bar_app(
        paths=[str(tmp_path / "Nope.app")], opener=got.append) == ""
    assert got == []


def test_skips_on_non_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    got = []
    assert server._open_menu_bar_app(paths=[_app(tmp_path)], opener=got.append) == ""
    assert got == []


def test_startup_hook_is_registered():
    """on_startup 里没挂上就等于没修——这条防的是「函数写了没人调」。"""
    import inspect
    src = inspect.getsource(server.main)
    assert "_open_menu_bar_app_startup" in src
