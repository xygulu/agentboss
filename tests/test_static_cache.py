#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""改完 ext.html 手机上看不到：host 的 _tpl 缓存按内容哈希做键、而哈希本身被永久
缓存，开发检出里文件一改 cache 永远命中旧的 —— page_ver 变了、手机每分钟 reload
也还是旧页面。缓存键必须带文件时间。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server  # noqa: E402

d = Path(tempfile.mkdtemp())
(d / "ext.html").write_text("AAA /static/xterm.js", encoding="utf-8")
sr = server.static_res        # 资源读取搬到了 static_res.py：要打桩的是它自己的模块名
old = sr.STATIC
sr.STATIC = d
sr._STATIC_REV.clear()
server._TPL_CACHE.clear()
try:
    a = sr._static_rev("ext.html")
    assert "AAA" in server._tpl("ext.html")
    time.sleep(0.01)
    (d / "ext.html").write_text("BBB /static/xterm.js", encoding="utf-8")
    b = sr._static_rev("ext.html")
    assert b != a, (a, b)
    assert "BBB" in server._tpl("ext.html")
finally:
    sr.STATIC = old
    sr._STATIC_REV.clear()
    server._TPL_CACHE.clear()
print("ok")
