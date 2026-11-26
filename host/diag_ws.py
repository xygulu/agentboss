#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""诊断：验证 WS 客户端能否收到广播输出"""
import asyncio
import json
import re
from pathlib import Path

import aiohttp

BASE = "http://127.0.0.1:8790"
TOKEN = (Path(__file__).parent / "token.txt").read_text().strip()


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/?t={TOKEN}") as r:
            html = await r.text()
        sids = re.findall(r"/s/([0-9a-f]{8})\?t=", html)
        print("sessions:", sids)
        if not sids:
            print("NO SESSION")
            return
        sid = "cc71e373"

        ws = await s.ws_connect(f"{BASE}/ws/{sid}?t={TOKEN}")
        print("connected", sid)
        total = {"a": 0, "b": 0}

        async def reader(name, sock):
            async for msg in sock:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    total[name] += len(msg.data)
                    txt = msg.data.decode("utf-8", "replace")
                    if "BROADCAST_TEST" in txt or "alive" in txt:
                        print(f"[{name}] 命中: {txt[-200:]!r}")
                else:
                    print(f"[{name}] {msg.type}")
            print(f"[{name}] closed")

        ta = asyncio.create_task(reader("a", ws))
        await asyncio.sleep(1.0)
        print("--- A 初始 buffer:", total["a"], "字节")

        print("--- A 发送命令")
        await ws.send_str(json.dumps({"t": "i", "d": "echo BROADCAST_TEST\r"}))

        await asyncio.sleep(0.3)
        ws2 = await s.ws_connect(f"{BASE}/ws/{sid}?t={TOKEN}")
        tb = asyncio.create_task(reader("b", ws2))
        await asyncio.sleep(1.5)
        print("--- B 加入后: A=", total["a"], "B=", total["b"])

        print("--- A 再发命令（此时有 2 个客户端）")
        await ws.send_str(json.dumps({"t": "i", "d": "echo SECOND_TEST\r"}))
        await asyncio.sleep(2.5)
        print("=== 最终 A=", total["a"], "B=", total["b"])
        ta.cancel()
        tb.cancel()


asyncio.run(main())
