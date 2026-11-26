#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""验证新快照机制：初始快照 / 尺寸切换 / 实时增量"""
import asyncio
import json
import re

import aiohttp
from pathlib import Path

PATH_HOME = Path(__file__).parent
BASE = "http://127.0.0.1:8790"
def _tk():
    return (PATH_HOME / "token.txt").read_text().strip()


TK = _tk()


def plain(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)


async def main():
    async with aiohttp.ClientSession() as s:
        # 新建一个 bash 测试会话
        async with s.post(f"{BASE}/new?t={TK}",
                          data={"cwd": "/tmp", "cmd": "bash", "name": "诊断"}) as r:
            sid = str(r.url).split("/s/")[1].split("?")[0]
        print("新会话:", sid)

        ws = await s.ws_connect(f"{BASE}/ws/{sid}?t={TK}")
        first = await ws.receive()
        m = json.loads(first.data)
        print("首包类型:", m["t"], "尺寸:", m["c"], "x", m["r"])
        print("快照字节:", len(m["d"]))

        # 制造：彩色输出 + 超过一屏的历史
        script = ("printf '\\033[31mRED\\033[0m \\033[1;32mBOLDGREEN\\033[0m\\n';"
                  "for i in $(seq 1 60); do echo \"history-line-$i\"; done;"
                  "printf '\\033[44m BG BLUE \\033[0m\\n';"
                  "echo READY-MARKER\n")
        await ws.send_str(json.dumps({"t": "i", "d": script}))
        await asyncio.sleep(1.2)
        # 清掉期间的二进制增量
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if msg.type == aiohttp.WSMsgType.CLOSED:
                break

        # 请求快照
        await ws.send_str(json.dumps({"t": "q"}))
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m["t"] == "snap":
                    break
        txt = plain(m["d"])
        lines = [l for l in txt.split("\r\n") if l.strip()]
        print(f"\n=== 快照 {m['c']}x{m['r']}：{len(lines)} 行非空")
        print("--- 前 3 行:")
        for l in lines[:3]:
            print("   ", l)
        print("--- 末 5 行:")
        for l in lines[-5:]:
            print("   ", l)
        print("含 RED:", "RED" in txt, " 含 BOLDGREEN:", "BOLDGREEN" in txt,
              " 含 READY-MARKER:", "READY-MARKER" in txt)
        # 光标行号不得超过行数
        curs = re.findall(r"\x1b\[(\d+);(\d+)H", m["d"])
        print("末光标定位:", curs[-1] if curs else None, " 行数上限:", m["r"])

        # 改成手机尺寸
        print("\n=== 切换到 45x30")
        await ws.send_str(json.dumps({"t": "r", "c": 45, "r": 30}))
        t0 = asyncio.get_event_loop().time()
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m["t"] == "snap" and m["c"] == 45:
                    print(f"收到 {m['c']}x{m['r']} 快照，耗时 {asyncio.get_event_loop().time()-t0:.2f}s")
                    t2 = plain(m["d"])
                    print("  末 3 行:", [l for l in t2.split("\r\n") if l.strip()][-3:])
                    break

        # 实时增量是否还通
        print("\n=== 实时增量测试")
        await ws.send_str(json.dumps({"t": "i", "d": "echo LIVE-OK\r"}))
        got = b""
        for _ in range(20):
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.4)
            except asyncio.TimeoutError:
                break
            if msg.type == aiohttp.WSMsgType.BINARY:
                got += msg.data
                if b"LIVE-OK" in got and got.count(b"LIVE-OK") > 1:
                    break
        print("收到 LIVE-OK:", b"LIVE-OK" in got, f"({len(got)}B)")
        await ws.close()


asyncio.run(main())
