# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口核心 Phase 1 · Task 4：WindowSource 抽象（pty/protocol/screen）。

source_of 只认行上既有字段做判别（kind 字段恒为 "computer"，不能拿它区分
protocol/pty——desktop 行也是 computer）。屏幕源是显式占位：live 不支持，
Phase 1 永不实装，让「降级了」看得见而不是静默变笨。

跑法： .venv/bin/python -m pytest tests/test_window_source.py -q
"""
from window_source import ScreenSource, WindowSource, source_of


def test_pty_for_plain_computer_row():
    """PTY/ConPTY 行（host 自管或 full_color 镜像都算）：kind=pty，语义可用。"""
    assert source_of("s1", {"kind": "computer", "host": True, "pid": 123}) \
        == {"kind": "pty", "sem": 1, "stub": 0}
    assert source_of("s1", {"kind": "computer", "full_color": True})["kind"] == "pty"


def test_protocol_for_desktop_row():
    """Codex 桌面版 /rpc 行（desktop 字段）：走结构化协议通道。"""
    assert source_of("s2", {"kind": "computer", "desktop": 1}) \
        == {"kind": "protocol", "sem": 1, "stub": 0}


def test_protocol_for_acp_row():
    """T6 起的 ACP 窗口行（acp 标记）：同为协议通道。"""
    assert source_of("s4", {"kind": "computer", "acp": 1})["kind"] == "protocol"


def test_screen_stub_for_unknown_row():
    """未知来源：显式屏幕占位（stub=1、无语义、live 不支持），不猜。"""
    assert source_of("s3", {"kind": "web"}) == {"kind": "screen", "sem": 0, "stub": 1}
    assert source_of("s3", {})["kind"] == "screen"


def test_screen_source_is_stub():
    sc = ScreenSource()
    assert sc.kind == "screen"
    assert sc.live_supported() is False


def test_window_source_base_shape():
    ws = WindowSource()
    assert ws.kind in ("pty", "protocol", "screen")
    assert isinstance(ws.semantics, bool)
    assert isinstance(ws.live_supported(), bool)
    assert isinstance(ScreenSource(), WindowSource)
