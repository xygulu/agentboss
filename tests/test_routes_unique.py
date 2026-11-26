# SPDX-License-Identifier: AGPL-3.0-or-later

"""路由表不许有同方法同路径的重复注册。

aiohttp 对被遮蔽的路由在注册时直接抛 RuntimeError——服务器起不来，而且是
main() 跑到一半才崩。评审 C2 的教训：新产物端点撞了旧媒体端点的名字和路径
（/api/ext_file 定义两次、注册两次），而全套测试没有一个经过路由表。

跑法： .venv/bin/python -m pytest tests/test_routes_unique.py -q
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

SRC = (HERE / "host" / "server.py").read_text(encoding="utf-8")


def test_route_paths_unique():
    seen = {}
    dups = []
    for m in re.finditer(r'add_(get|post)\(\s*"([^"]+)"', SRC):
        key = (m.group(1), m.group(2))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            dups.append(key)
    assert not dups, "同方法同路径重复注册（aiohttp 起服即崩）：%s" % dups


def test_ext_route_handlers_resolve():
    """注册行引用的 ext handler 名字必须在 server 模块里有定义且只有一个 def。

    Python 后定义覆盖先定义：重名时注册到的是后者，前者静默失效。
    """
    import server
    names = set(re.findall(r'add_(?:get|post)\(\s*"[^"]*",\s*(h_ext_\w+)\)',
                           SRC))
    assert names, "没扫到 ext 路由注册？"
    for n in names:
        assert hasattr(server, n), "注册了不存在的 handler：%s" % n
