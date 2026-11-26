# SPDX-License-Identifier: AGPL-3.0-or-later

"""整页缓存必须按产品线分开（2026-11-20）。

中继把整页 HTML 缓存 6 小时（省上行）。但页面里的标题 / favicon / 页头图标是
开发机按「客户端产品线」服务端注入的——缓存键没有产品线时，同一账号先用
CodeBoss 打开一次，再用 AgentBoss 打开，第二台拿到的是 CodeBoss 那一份。
用户证据：AgentBoss 0.1.12 装好后页面还是 CodeBoss 的图标，开发机日志里连
页面请求都没有（全被缓存挡在前面）。

跑法： .venv/bin/python -m pytest tests/test_relay_page_cache_product.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, query=None, headers=None, cookies=None):
        self.query = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_product_from_query_header_or_cookie():
    assert relay._pcache_product(_Req(query={"product": "agentboss"})) == "agentboss"
    assert relay._pcache_product(_Req(headers={"X-Product": "codeboss"})) == "codeboss"
    assert relay._pcache_product(_Req(cookies={"cb_product": "agentboss"})) == "agentboss"


def test_unknown_signal_is_empty():
    assert relay._pcache_product(_Req()) == ""
    assert relay._pcache_product(_Req(cookies={"cb_product": "bogus"})) == ""


def test_cache_key_differs_by_product():
    a = relay._pcache_key("u1", "0.9.70", "", "agentboss")
    b = relay._pcache_key("u1", "0.9.70", "", "codeboss")
    c = relay._pcache_key("u1", "0.9.70", "", "")
    assert len({a, b, c}) == 3


def test_tunnel_plain_key_uses_product():
    """键不是只在测试里对——进缓存的那条路径真的按产品线取键。"""
    src = (HERE / "relay" / "relay.py").read_text(encoding="utf-8")
    i = src.find("pcache_key = _pcache_key(")
    assert i >= 0, "整页缓存键没走 _pcache_key（产品线又漏了）"
    assert "_pcache_product(request)" in src[i:i + 300], \
        "整页缓存键里没有产品线"
