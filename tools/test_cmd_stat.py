# SPDX-License-Identifier: AGPL-3.0-or-later

"""_pump 状态机自检：命令条目按 commandActions 显示「正在读取/正在搜索」，
outputDelta 不许把它盖回「正在运行命令」。跑法：python3 tools/test_cmd_stat.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_daemon  # noqa: E402


class FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def receive_json(self):
        if not self.msgs:
            await asyncio.sleep(3600)
        return self.msgs.pop(0)

    async def close(self):
        pass


class FakeSess:
    async def close(self):
        pass


async def run():
    seq = [
        {"method": "item/started", "params": {"item": {
            "type": "commandExecution", "id": "c1",
            "commandActions": [{"type": "read", "path": "/etc/hosts"}]}}},
        {"method": "item/commandExecution/outputDelta", "params": {"itemId": "c1", "delta": "x"}},
        {"method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "c1"}}},
        {"method": "item/started", "params": {"item": {
            "type": "commandExecution", "id": "c2",
            "commandActions": [{"type": "search", "query": "*.txt"}]}}},
        {"method": "item/started", "params": {"item": {
            "type": "commandExecution", "id": "c3",
            "commandActions": [{"type": "unknown", "command": "ls"}]}}},
        {"method": "turn/completed", "params": {}},
    ]
    st = []
    await codex_daemon._pump(FakeWS(seq), FakeSess(), lambda t: None,
                             lambda s, t: None, on_stat=st.append)
    print(st)
    assert "正在读取" in st, st
    assert "正在搜索" in st, st
    assert st.count("正在运行命令") == 1, st          # 只有 unknown 那条
    assert st.index("正在运行命令") > st.index("正在搜索")   # outputDelta 没把读取/搜索盖成运行命令


asyncio.run(run())
print("ok")
