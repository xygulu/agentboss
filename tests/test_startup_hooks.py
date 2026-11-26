# SPDX-License-Identifier: AGPL-3.0-or-later

"""启动钩子不许把站点卡死（2026-11-21 事故：整个机队离线）。

aiohttp 逐个 await app.on_startup 里的钩子，钩子不返回就永远走不到 site.start()：
进程活着、8790 不监听、_host_link 也轮不到——中继看所有开发机全离线。
0.9.72 的「找会话索引预热」正是把 while True 直接写进 on_startup。
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import server


def test_search_warm_hook_returns_immediately():
    app = {}

    async def run():
        await asyncio.wait_for(server._search_warm_loop_startup(app), 1)
        t = app.get("search_warm_task")
        assert t is not None, "预热循环要在后台任务里跑"
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_startup_guard_cancels_hanging_hook():
    """死循环钩子被取消，站点照常往下走（不抛给 AppRunner）。"""
    async def hang(app):
        while True:
            await asyncio.sleep(0.01)

    async def run():
        await asyncio.wait_for(server._startup_guard(hang, timeout=0.2)({}), 2)

    asyncio.run(run())
