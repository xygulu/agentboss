#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""host→中继链路必须有「静默黑洞」看门狗——连接死透了也要能自愈重连。

2026-11-18 事故：aliyun 开发机到中继的 ws 长连接被中间设备静默丢弃
（无 RST/FIN，包被黑洞）。host 的 `async for msg in ws` 在 recv 上永远阻塞，
心跳帧堆在 186KB 发不出去的缓冲区里，重连逻辑永远不触发——中继还以为它在线，
把所有手机「切到 aliyun」的请求推进死链 → 全部白屏/黑屏/断网，持续 2 小时。
收到 RST 的死法有 heartbeat+重连兜底；本测试钉死第二种：

1. 超时没收到任何消息 → 判死：底层 transport 被 abort（内核立刻丢弃僵尸连接，
   所有阻塞在它上面的发送/接收全部惊醒报错），并向外抛 TimeoutError 走重连；
2. 有消息就正常返回，不误杀慢链路；
3. abort 本身失败（属性缺失/版本差异）不能吞掉 TimeoutError——宁可错杀重连，
   不可卡死不重连。

跑法：.venv/bin/python -m pytest tests/test_host_link_watchdog.py -q
"""
import asyncio
import sys
from types import SimpleNamespace

HERE = Path = None
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


class FakeWS:
    """模拟 aiohttp ClientWebSocketResponse 的最小面：receive() + _writer.transport。"""

    def __init__(self, hang=True, msg=None, with_writer=True, pending=0):
        self._hang = hang
        self._msg = msg
        self.aborted = False
        self.pending = pending
        if with_writer:
            self._writer = SimpleNamespace(
                transport=SimpleNamespace(abort=self._mark_abort,
                                          get_write_buffer_size=lambda: self.pending))

    def _mark_abort(self):
        self.aborted = True

    async def receive(self):
        if self._hang:
            await asyncio.Event().wait()   # 永不完成 = 黑洞链路
        return self._msg


def _run(coro):
    return asyncio.run(coro)


def test_blackhole_times_out_and_aborts():
    # 黑洞链路：收不到消息 **且** 发送缓冲有积压（数据发不出去）
    ws = FakeWS(hang=True, pending=150000)
    try:
        _run(server._ws_recv_or_dead(ws, 0.05))
        raise AssertionError("黑洞链路必须抛 TimeoutError")
    except asyncio.TimeoutError:
        pass
    assert ws.aborted, "判死必须 abort 底层 transport（内核丢僵尸连接，惊醒全部阻塞者）"


def test_idle_healthy_link_not_killed():
    """2026-11-18 16:20 事故：中继平时不给 host 发任何消息，健康的空闲链路
    每 120s 被看门狗误杀一次（实测所有设备每 2 分钟重连一轮，手机间歇断网）。
    钉死：无消息 **且发送缓冲为空** = 只是闲着，不许杀。"""
    ws = FakeWS(hang=True, pending=0)
    try:
        _run(asyncio.wait_for(server._ws_recv_or_dead(ws, 0.05, max_idle_s=10), 0.4))
        raise AssertionError("空闲≠死亡，不该返回")
    except asyncio.TimeoutError:
        pass   # 外层超时 = 内部还在等，正确
    assert not ws.aborted, "空闲且无积压的链路不得被误杀"


def test_idle_link_still_killed_after_max_idle():
    """空闲链路也不能无限等：超过 max_idle_s 仍无来讯 → 判死重连。"""
    ws = FakeWS(hang=True, pending=0)
    try:
        _run(server._ws_recv_or_dead(ws, 0.05, max_idle_s=0.1))
        raise AssertionError("超过 max_idle 必须抛 TimeoutError")
    except asyncio.TimeoutError:
        pass
    assert ws.aborted, "超上限必须 abort"


def test_default_max_idle_does_not_kill_idle_links_every_10_minutes():
    """2026-11-23 新加坡那台「总是连不上、消息发不出去」的根因：
    默认 max_idle_s=600 会把**健康但空闲**的链路每 10 分钟自己掐一次
    （中继只在 host 报用量时才发 use_ack；空着的机器一句话都不来）。
    日志里 426 次断开原因是空字符串 = 这里抛的 TimeoutError，间隔恰好 10:03。
    空闲≠死亡：默认上限必须放进小时级，别把健康链路当黑洞。"""
    import inspect
    d = inspect.signature(server._ws_recv_or_dead).parameters["max_idle_s"].default
    assert d >= 1800, "空闲判死上限 %ss 太短：空闲机器会周期性自断链路" % d


def test_alive_link_returns_message():
    ws = FakeWS(hang=False, msg=SimpleNamespace(type=1, data="{}"))
    msg = _run(server._ws_recv_or_dead(ws, 5))
    assert msg.data == "{}"
    assert not ws.aborted, "活链路不该被误杀"


def test_abort_failure_still_raises_timeout():
    ws = FakeWS(hang=True, with_writer=False)   # 没有 _writer（版本差异/残缺对象）
    try:
        _run(server._ws_recv_or_dead(ws, 0.05))
        raise AssertionError("abort 失败也必须抛 TimeoutError 走重连")
    except asyncio.TimeoutError:
        pass


def test_close_message_terminates_link():
    """2026-11-18 14:24 事故：中继重启给旧 ws 发 CLOSE 帧，0.9.13 的 recv 循环
    `if msg.type != TEXT: continue` 把关闭消息当普通消息吞了——aiohttp 对已关
    ws 的 receive() 立即返回 CLOSED → while True 忙循环烧死事件循环（aliyun
    实测 81% CPU），重连/自动更新全部饿死 → 四台 0.9.13 设备同时永久离线。
    0.9.12 的 `async for` 遇 CLOSE 自动退出迭代去重连，所以没死。
    钉死：CLOSE 族消息必须判链路终结（abort + ConnectionError → 走重连）。"""
    from aiohttp import WSMsgType
    for typ in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                WSMsgType.ERROR):
        ws = FakeWS(hang=False, msg=SimpleNamespace(type=typ, data="", extra=""))
        try:
            _run(server._ws_recv_or_dead(ws, 5))
            raise AssertionError("收到 %s 必须抛 ConnectionError 走重连" % typ)
        except ConnectionError:
            pass
        except asyncio.TimeoutError:
            raise AssertionError("%s 被当成超时处理了——判死路径不对" % typ)
        assert ws.aborted, "收到 %s 必须 abort 底层 transport" % typ


class SeqWS:
    """发送缓冲按序列变化的假 ws：测上行黑洞看门狗用。"""

    def __init__(self, seq):
        self._seq = list(seq)
        self.aborted = False
        self._writer = SimpleNamespace(
            transport=SimpleNamespace(abort=self._mark_abort,
                                      get_write_buffer_size=self._next))

    def _mark_abort(self):
        self.aborted = True

    def _next(self):
        return self._seq.pop(0) if len(self._seq) > 1 else (self._seq[0] if self._seq else 0)


def test_frozen_send_buffer_aborts_link():
    """2026-11-23 新加坡机 15:53–15:59 手机全部 504 的根因：
    跨境上行被静默黑洞——手机请求帧照常进来（recv 不空闲，旧的收包看门狗
    看不见积压），被卡住的 send 拿着锁发不出去，后续响应全排队，中继 120s
    后逐个 504，整整 6 分钟「根本连不上」，直到别的原因重启才恢复。
    钉死：发送缓冲连续 15s 一个字节都没排出去 = 上行黑洞，必须 abort 走重连。"""
    ws = SeqWS([120000] * 10)   # 冻在 120KB 不动
    try:
        _run(asyncio.wait_for(server._send_watch(ws, interval=0.02, stall_n=3), 1.0))
    except asyncio.TimeoutError:
        raise AssertionError("发送缓冲冻结必须判死（abort 后返回），不能一直等")
    assert ws.aborted, "上行黑洞必须 abort 底层 transport，让阻塞的发送惊醒走重连"


def test_draining_send_buffer_not_killed():
    """慢链路大帧只是排得慢（跨境实测 ~17KB/s）：缓冲持续下降就不是黑洞。"""
    ws = SeqWS([200000, 180000, 150000, 120000, 90000, 60000, 30000, 0] * 3)
    try:
        _run(asyncio.wait_for(server._send_watch(ws, interval=0.02, stall_n=3), 0.5))
        raise AssertionError("正常排空不该返回")
    except asyncio.TimeoutError:
        pass
    assert not ws.aborted, "持续下降的发送缓冲不得误杀"


def test_empty_buffer_not_killed():
    ws = SeqWS([0] * 10)
    try:
        _run(asyncio.wait_for(server._send_watch(ws, interval=0.02, stall_n=3), 0.3))
        raise AssertionError("空闲缓冲不该返回")
    except asyncio.TimeoutError:
        pass
    assert not ws.aborted


def test_binary_message_still_returns():
    """BINARY 是正常数据帧，不得误杀。"""
    from aiohttp import WSMsgType
    ws = FakeWS(hang=False, msg=SimpleNamespace(type=WSMsgType.BINARY, data=b"{}"))
    msg = _run(server._ws_recv_or_dead(ws, 5))
    assert msg.data == b"{}"
    assert not ws.aborted
