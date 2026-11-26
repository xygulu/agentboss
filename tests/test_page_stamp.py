#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""构建戳(PAGE_BUILD)必须随内容变化——页面自更新靠它触发硬刷新。

2026-11-18 事故:build_page.py 从不更新 PAGE_BUILD,ext.html 内容变了戳还是
'2026-11-14b' → 手机上旧页面轮询 /api/page_ver 时 d.b===自己的 PAGE_BUILD,
永远不触发 reload → 探针验证/切台修复等所有页面改动根本到不了手机。
钉死:构建出的 ext.html 里 PAGE_BUILD 必须等于当前内容的短哈希——
内容变 → 戳变;同内容重建 → 戳不变(幂等,--check 才不会天天报不同步)。

跑法:.venv/bin/python -m pytest tests/test_page_stamp.py -q
"""
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

EXT = HERE / "host" / "static" / "ext.html"


def _stamp_of(text):
    m = re.search(r"PAGE_BUILD='([^']*)'", text)
    return m.group(1) if m else ""


def _want_of(text):
    """正确戳 = 把戳位挖空后全文的 sha256[:10]。

    不能对「含戳文件」求哈希再放进同一文件——自指，永远对不上。
    挖空位保证：内容任何一字变化 → 戳变；同内容重建 → 戳不变（幂等）。
    构建端 tools/build_page.py 与本测试必须同一算法。"""
    blanked = re.sub(r"PAGE_BUILD='[^']*'", "PAGE_BUILD=''", text)
    return hashlib.sha256(blanked.encode("utf-8")).hexdigest()[:10]


def test_stamp_matches_content():
    raw = EXT.read_text(encoding="utf-8")
    want = _want_of(raw)
    got = _stamp_of(raw)
    assert got, "ext.html 里必须有 PAGE_BUILD"
    assert got == want, "PAGE_BUILD(%s) 必须等于挖空戳位后的内容 sha256[:10](%s)——内容变了戳没变,旧页面永远等不到刷新" % (got, want)


def test_stamp_not_the_hand_written_literal():
    src = (HERE / "host" / "static" / "src" / "js" / "07_account.js").read_text(encoding="utf-8")
    hand = _stamp_of(src)   # 源码里手写的那个(例如 '2026-11-14b')
    built = _stamp_of(EXT.read_text(encoding="utf-8"))
    assert hand != built, "构建产物必须覆盖掉源码手写戳,否则内容变了戳不变"
