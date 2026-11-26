#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""远程日志上报只推新增字节——2026-11-18 16:25 事故。

后台给 aliyun 开了「日志上报」后，host 每 15s 把 server.log 尾部 64KB 重推
一遍；跨境上行实测只有 ~16KB/s，队列常年积压 10 万+ 字节，手机请求排在
后面直接「流空闲超时」→ 一选 aliyun 就断网。修法：记住已推偏移，只发新增。

跑法：.venv/bin/python -m pytest tests/test_log_delta.py -q
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


def _with_tmp(td, fn):
    old_here = server.HERE
    old_pos = server._LOG_POS["pos"]
    server.HERE = Path(td)
    server._LOG_POS["pos"] = 0
    (Path(td) / "logs").mkdir(parents=True, exist_ok=True)
    try:
        return fn(Path(td) / "logs" / "server.log")
    finally:
        server.HERE = old_here
        server._LOG_POS["pos"] = old_pos


def test_only_new_bytes_sent():
    with tempfile.TemporaryDirectory() as td:
        def run(log):
            log.write_text("aaa\n")
            assert server._log_delta() == "aaa\n", "首次推全部内容"
            assert server._log_delta() == "", "无新增不得重发"
            log.write_text("aaa\nbbb\n")
            assert server._log_delta() == "bbb\n", "只推新增字节"
        _with_tmp(td, run)


def test_truncated_log_restarts():
    with tempfile.TemporaryDirectory() as td:
        def run(log):
            log.write_text("x" * 100)
            server._log_delta()
            log.write_text("y\n")          # 轮转/清空：变小了
            assert server._log_delta() == "y\n", "日志变小必须从头推"
        _with_tmp(td, run)


def test_burst_capped():
    with tempfile.TemporaryDirectory() as td:
        def run(log):
            log.write_text("z" * 50000)
            got = server._log_delta(limit=8192)
            assert len(got) == 8192, "单次推送必须有上限，慢链路不许 64KB 突发"
            assert len(server._log_delta(limit=8192)) == 8192, "剩余部分下次继续"
        _with_tmp(td, run)
