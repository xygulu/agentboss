# SPDX-License-Identifier: AGPL-3.0-or-later

"""已退出会话按 sid 要找得到（ext_msg / SSE 启动共用 _find_session）。

现象：列表端点补了 closed 会话，但 _find_session 只查 discover（活）缓存，
app 点开已退出会话 → state=gone → 打不开（hermes 首先暴露，因为它没有
wrapper meta 的活会话兜底）。

跑法： .venv/bin/python -m pytest tests/test_find_closed_session.py -q
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _isolate_live(monkeypatch):
    """发现层全空：缓存与两轮 discover 都没有这个 sid。"""
    monkeypatch.setattr(server, "_cached_sessions", lambda: [])

    def _tick(force=False):
        return []

    monkeypatch.setattr(server, "_tick_sessions", _tick)


_CLOSED = [{"sid": "20261117_114131_ec1f02", "agent": "hermes",
            "state": "exited", "pid": None, "cwd": "/tmp"}]


def test_find_session_falls_back_to_closed(monkeypatch):
    _isolate_live(monkeypatch)
    monkeypatch.setattr(server, "_closed_sessions", lambda live, limit=20: _CLOSED)
    s = asyncio.run(server._find_session("20261117_114131_ec1f02"))
    assert s is not None and s["sid"] == "20261117_114131_ec1f02"


def test_find_session_live_still_wins(monkeypatch):
    # 活会话照旧从缓存直出，不走 closed 回退（刚召回的会话 closed 里可能还有旧影子）
    _isolate_live(monkeypatch)
    monkeypatch.setattr(server, "_closed_sessions", lambda live, limit=20: _CLOSED)
    monkeypatch.setattr(server, "_cached_sessions",
                        lambda: [{"sid": "live1", "agent": "hermes", "state": "busy"}])
    s = asyncio.run(server._find_session("live1"))
    assert s is not None and s["sid"] == "live1"


def test_find_session_unknown_still_none(monkeypatch):
    _isolate_live(monkeypatch)
    monkeypatch.setattr(server, "_closed_sessions", lambda live, limit=20: _CLOSED)
    assert asyncio.run(server._find_session("nosuch")) is None
