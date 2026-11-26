#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""连上中继后起的补推 watcher 必须在链路死时收手。

2026-11-18 实测：relay 重启后最迟重连成功，但 APK 一直推不上去——旧连接的
_apk_watch 拿着已关闭的 ws 每 60s 醒一次，send 立刻抛错并记一次「推送失败」，
把退避一路刷新，新连接永远被自己的旧 watcher 挡住。根因：连接结束只记了
inflight 失败，从没取消这条连接起的子任务。

跑法：.venv/bin/python -m pytest tests/test_server_apk_watch_ends.py -q
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


class _WS:
    closed = True


def test_apk_watch_returns_when_ws_closed(monkeypatch):
    calls = []

    async def fake_push(ws, lock):
        calls.append(1)
        return {}

    monkeypatch.setattr(server, "_push_apk", fake_push)
    monkeypatch.setattr(server, "APK_WATCH_S", 0.01)
    # 修复前：进 while True，每 0.01s 醒一次，1s 超时都退不出来
    asyncio.run(asyncio.wait_for(server._apk_watch(_WS(), None), 1))
    assert calls == [1]      # 连上时那一次照推；ws 已关就不再循环
