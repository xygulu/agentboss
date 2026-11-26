#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""APK 推送不能每次连上中继都硬推 1.2MB——必须「推过就跳、堵过就退避」。

2026-11-18 事故链：aliyun(境外)→中继(上海)的持续上行在 ~150KB 后被掐
(同机上传海外 500KB 完整成功,排除出站/安全组问题)。而 host 每次连上
中继都把 1.2MB APK 从 ws 里推一遍——上传一卡,APK 帧把后续所有请求帧
堵死 → 中继以为它在线,手机切到它全部白屏。修法:
1. 本机曾推成功过这份包(mtime+size 没变)→ 跳过:中继 APK 库是全局的,
   别机早推过同一份,再推纯属白给;
2. 推送中途链路死掉(看门狗杀链)→ 记失败退避:翻倍增长,封顶 24h,
   别让每次重连都重复一次「卡死-杀链-重推」循环;
3. 包变了(重新 build)→ 照推。

跑法：.venv/bin/python -m pytest tests/test_apk_push_skip.py -q
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


class FakeWS:
    """记录 send_json 的假 ws;hang=True 时 send 永不完成(模拟卡死链路)。"""

    def __init__(self, hang=False):
        self.frames = []
        self.hang = hang

    async def send_json(self, d):
        if self.hang:
            await asyncio.Event().wait()
        self.frames.append(d)


def _mk_apk(tmp, product, code=1):
    d = tmp / "android" / "build" / product
    d.mkdir(parents=True, exist_ok=True)
    apk = d / (product + "-terminal.apk")
    apk.write_bytes(b"\x00" * 20480)
    (d / "apk.json").write_text(json.dumps({"ver": "1.0", "code": code}))
    return apk


def _push(tmp, product, hang=False, timeout=5, self_to=None):
    """在 tmp 里当 HERE 推一条包,返回 (ws, marker 文件内容, 异常)。

    timeout=None 不套外层兜底(测内部自超时);self_to 覆盖内部单帧超时。"""
    old_here = server.HERE
    old_sto = getattr(server, "_APK_SEND_TIMEOUT_S", None)
    server.HERE = tmp
    if self_to is not None:
        server._APK_SEND_TIMEOUT_S = self_to
    try:
        ws = FakeWS(hang=hang)
        lock = asyncio.Lock()

        async def run():
            if timeout is None:
                return await server._push_apk_one(ws, lock, product)
            return await asyncio.wait_for(
                server._push_apk_one(ws, lock, product), timeout)

        try:
            asyncio.run(run())
            err = None
        except asyncio.TimeoutError:
            err = "timeout"
        marker = {}
        state = tmp / "apk_pushed.json"
        if state.exists():
            marker = json.loads(state.read_text())
        return ws, marker, err
    finally:
        server.HERE = old_here
        if old_sto is not None:
            server._APK_SEND_TIMEOUT_S = old_sto


def test_first_push_sends_and_marks_ok():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _mk_apk(tmp, "p1")
        ws, marker, err = _push(tmp, "p1")
        assert err is None
        kinds = [f.get("t") for f in ws.frames]
        assert kinds[0] == "apk" and kinds[-1] == "apk_end", "必须真的推帧"
        ok = marker.get("p1", {}).get("ok")
        assert ok and ok.get("size") == 20480, "推完必须落成功标记"


def test_same_file_skipped():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _mk_apk(tmp, "p1")
        _push(tmp, "p1")                       # 第一次:真推 + 落标记
        ws, marker, err = _push(tmp, "p1")     # 第二次:文件没变
        assert err is None
        assert ws.frames == [], "同一份包(中继已有)不得重推"


def test_stalled_push_backs_off():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _mk_apk(tmp, "p1")
        ws1, marker, err = _push(tmp, "p1", hang=True, timeout=0.3)
        assert err == "timeout", "卡死的推送要能被打断"
        st = marker.get("p1", {})
        assert st.get("fail_at"), "推送中途被打断必须记失败时刻"
        assert not st.get("ok")
        ws2, _, err2 = _push(tmp, "p1")        # 退避期内:直接跳过
        assert err2 is None
        assert ws2.frames == [], "退避期内不得重推(否则每次重连都卡死一遍)"


def test_send_stall_self_aborts():
    """send 挂起(不抛错)时推送必须自己超时——2026-11-18 14:16 事故:
    aliyun/Mac 推 1.2MB APK 时 send 永远 pending,协程堵死在 await 上,
    recv 看门狗排在后面永远轮不到 → 永不重连 → 设备离线。生产里没有
    外层 wait_for 来救,推送必须自己掐。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _mk_apk(tmp, "p1")
        ws, marker, err = _push(tmp, "p1", hang=True, timeout=None, self_to=0.2)
        assert err == "timeout", "send 卡死必须由推送自己超时打断,不能永远挂起"
        assert marker.get("p1", {}).get("fail_at"), "自己超时也必须落失败退避标记"
        assert not marker.get("p1", {}).get("ok")


def test_old_failure_retried_and_rebuild_pushes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _mk_apk(tmp, "p1")
        _push(tmp, "p1", hang=True, timeout=0.3)
        # 把失败时刻拨回 25h 前 → 超过封顶 24h,应允许重试
        mf = tmp / "apk_pushed.json"
        d = json.loads(mf.read_text())
        d["p1"]["fail_at"] = time.time() - 25 * 3600
        mf.write_text(json.dumps(d))
        ws, _, err = _push(tmp, "p1")
        assert err is None and ws.frames, "退避过期后要肯再推"

        # 重新 build(文件变了)→ 立即照推
        apk = tmp / "android" / "build" / "p1" / "p1-terminal.apk"
        apk.write_bytes(b"\x01" * 30720)
        ws2, _, _ = _push(tmp, "p1")
        assert ws2.frames, "包变了必须推新版本"
