# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio, json, sys
from aiohttp import ClientSession, WSMsgType

SID = "f55ebed3"
TOKEN = "8a97a0c586fb40ab"
URL = f"http://127.0.0.1:8790/ws/{SID}?t={TOKEN}"


async def main():
    async with ClientSession() as s:
        async with s.ws_connect(URL) as ws:
            print("[client2] connected as a SECOND client")
            # 1) 先看它是否拿到同一屏的输出
            got = b""
            try:
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                    if msg.type == WSMsgType.BINARY:
                        got += msg.data
                    else:
                        break
            except asyncio.TimeoutError:
                pass
            print(f"[client2] replay buffer received: {len(got)} bytes")

            # 2) 从第二个客户端输入，验证会写进同一个 PTY
            text = "第二个客户端（假手机）发言"
            await ws.send_str(json.dumps({"t": "i", "d": text}))
            await asyncio.sleep(0.4)
            await ws.send_str(json.dumps({"t": "i", "d": "\r"}))
            print(f"[client2] sent: {text} + Enter")

            # 3) 读回广播
            await asyncio.sleep(3)
            total = b""
            try:
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
                    if msg.type == WSMsgType.BINARY:
                        total += msg.data
                    else:
                        break
            except asyncio.TimeoutError:
                pass
            txt = total.decode("utf-8", "ignore")
            print(f"[client2] received back {len(total)} bytes")
            print("[client2] contains our text:", text in txt)

asyncio.run(main())
