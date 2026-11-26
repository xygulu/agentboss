# SPDX-License-Identifier: AGPL-3.0-or-later

"""拍屏跨平台（2026-11-26）：macOS screencapture 原行为保持，Windows 补 PIL ImageGrab。

- Windows 路径：注入假 ImageGrab，断言先设 DPI 感知（高缩放屏不设会抓出低分辨率图）
  再 grab→save，成功回 (0, "")；
- Windows 失败：grab 抛异常回 (500, 文案)；
- macOS：假 subprocess.run 验证命令与失败文案原样保留；
- 其他平台：回 (400, 「暂不支持」)。

跑法：.venv/bin/python -m pytest tests/test_shot_platforms.py -q
"""
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


class _FakeImg:
    def __init__(self, store):
        self._store = store

    def convert(self, mode):
        assert mode == "RGB"
        return self

    def save(self, path, fmt, quality=None):
        self._store.append(("save", path, fmt, quality))


def _install_fake_pil(monkeypatch, store, grab=None):
    """假 PIL.ImageGrab：grab 返回假图或抛指定异常。"""
    pil = types.ModuleType("PIL")
    ig = types.ModuleType("PIL.ImageGrab")
    if grab is None:
        ig.grab = lambda *a, **k: _FakeImg(store)
    else:
        ig.grab = grab
    pil.ImageGrab = ig
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", ig)


def test_win32_sets_dpi_then_grabs_and_saves(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sys, "platform", "win32")
    _store = []
    _order = []
    monkeypatch.setattr(server, "_set_win_dpi", lambda: _order.append("dpi"))
    _install_fake_pil(monkeypatch, _store, grab=lambda *a, **k: _order.append("grab") or _FakeImg(_store))
    path = tmp_path / "s.jpg"

    status, err = server._screenshot_to(str(path))

    assert (status, err) == (0, "")
    assert _order == ["dpi", "grab"], "必须先设 DPI 感知再抓屏"
    assert _store and _store[0] == ("save", str(path), "JPEG", _store[0][3])
    assert _store[0][3] is not None  # quality 给了（省带宽）


def test_win32_grab_failure_is_500(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sys, "platform", "win32")

    def boom(*a, **k):
        raise RuntimeError("no display")
    _install_fake_pil(monkeypatch, [], grab=boom)
    monkeypatch.setattr(server, "_set_win_dpi", lambda: None)

    status, err = server._screenshot_to(str(tmp_path / "s.jpg"))

    assert status == 500
    assert "拍屏失败" in err and "no display" in err


def test_darwin_uses_screencapture(monkeypatch, tmp_path):
    path = tmp_path / "s.jpg"
    seen = {}

    class _R:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        path.write_bytes(b"x" * 2048)  # >1KB 才算真拍到
        return _R()

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    status, err = server._screenshot_to(str(path))

    assert (status, err) == (0, "")
    assert seen["cmd"] == ["screencapture", "-x", "-t", "jpg", str(path)]


def test_darwin_failure_copy_unchanged(monkeypatch, tmp_path):
    class _R:
        returncode = 1

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _R())

    status, err = server._screenshot_to(str(tmp_path / "s.jpg"))

    assert status == 500
    assert "屏幕录制" in err


def test_unknown_platform_is_400(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sys, "platform", "linux")

    status, err = server._screenshot_to(str(tmp_path / "s.jpg"))

    assert status == 400
    assert "暂不支持" in err
