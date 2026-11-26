#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""隧道内压缩：aliyun(境外)→中继(上海)上行实测 ~17KB/s，653KB 页面裸传 51s，
手机等不到就显示断网。host 端 gzip 后 ~130KB（8s），relay 侧带
Content-Encoding 时不得再压一次（否则双重 gzip，页面全坏）。

跑法：.venv/bin/python -m pytest tests/test_tunnel_gzip.py -q
"""
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


def test_gzip_when_client_accepts():
    raw = b"<html>" + b"a" * 100000
    body, hdr = server._tunnel_body(raw, {"Accept-Encoding": "gzip, deflate"}, 200)
    assert hdr.get("content-encoding") == "gzip"
    assert zlib.decompress(body, 31) == raw, "必须是真 gzip 容器（wbits=31）：只认 gzip 头、里面是 zlib 流的写法就是今天全站断网的根因"
    assert len(body) < len(raw) // 2, "必须有实际压缩收益"


def test_no_gzip_without_accept():
    raw = b"x" * 100000
    body, hdr = server._tunnel_body(raw, {}, 200)
    assert body == raw and hdr == {}, "客户端没要 gzip 就别压"


def test_small_or_non200_untouched():
    raw = b"{" + b"y" * 50 + b"}"
    assert server._tunnel_body(raw, {"accept-encoding": "gzip"}, 200) == (raw, {}), "小响应不压"
    big = b"z" * 100000
    assert server._tunnel_body(big, {"accept-encoding": "gzip"}, 406) == (big, {}), "非 200 不压"
    assert server._tunnel_body(b"", {"accept-encoding": "gzip"}, 200) == (b"", {})
