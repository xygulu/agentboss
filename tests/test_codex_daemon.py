#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""桌面版会话的流式通道：守护进程没跑/线程被占着都要老实报失败（调用方退回队列），
跑起来之后正文增量要按顺序拼成全文、结束事件只发一次。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import codex_daemon  # noqa: E402


class _FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.closed = False

    async def receive_json(self):
        if not self.msgs:
            await asyncio.sleep(3600)
        return self.msgs.pop(0)

    async def send_json(self, obj):
        pass

    async def close(self):
        self.closed = True


class _FakeSess:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class SockPathTest(unittest.TestCase):
    def test_missing_socket_means_no_stream(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": tempfile.mkdtemp()}):
            self.assertEqual(codex_daemon.sock_path(), "")

    def test_socket_found_under_codex_home(self):
        home = Path(tempfile.mkdtemp())
        d = home / "app-server-control"
        d.mkdir(parents=True)
        (d / "app-server-control.sock").write_text("")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(codex_daemon.sock_path(),
                             str(d / "app-server-control.sock"))


class PumpTest(unittest.TestCase):
    def _run(self, msgs):
        ws, sess = _FakeWS(msgs), _FakeSess()
        got, end = [], []

        async def go():
            await asyncio.wait_for(codex_daemon._pump(
                ws, sess, got.append, lambda st, t: end.append((st, t))), 5)

        asyncio.run(go())
        return got, end, ws, sess

    def test_deltas_accumulate_and_turn_completed_ends(self):
        got, end, ws, sess = self._run([
            {"method": "item/agentMessage/delta", "params": {"delta": "你"}},
            {"method": "item/agentMessage/delta", "params": {"delta": "好"}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual(got, ["你", "你好"])
        self.assertEqual(end, [("ok", "你好")])
        self.assertTrue(ws.closed and sess.closed)

    def test_completed_item_wins_over_missing_deltas(self):
        got, end, _, _ = self._run([
            {"method": "item/agentMessage/delta", "params": {"delta": "半截"}},
            {"method": "item/completed",
             "params": {"item": {"type": "agentMessage", "text": "完整回答"}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual(got[-1], "完整回答")
        self.assertEqual(end, [("ok", "完整回答")])

    def test_two_messages_in_one_turn_both_survive(self):
        """一轮两条 agentMessage（前言 + 工具后的正文）：前面那条不能被后面覆盖掉。"""
        got, end, _, _ = self._run([
            {"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "a"}}},
            {"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": "我先查一下。"}},
            {"method": "item/completed",
             "params": {"item": {"type": "agentMessage", "id": "a", "text": "我先查一下。"}}},
            {"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "b"}}},
            {"method": "item/agentMessage/delta", "params": {"itemId": "b", "delta": "结论是"}},
            {"method": "item/agentMessage/delta", "params": {"itemId": "b", "delta": "这样。"}},
            {"method": "item/completed",
             "params": {"item": {"type": "agentMessage", "id": "b", "text": "结论是这样。"}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual(got[-1], "我先查一下。\n\n结论是这样。")
        self.assertEqual(end, [("ok", "我先查一下。\n\n结论是这样。")])

    def test_status_follows_items(self):
        """手机端状态行：起手思考 → 跑命令 → 改文件 → 回正文。"""
        ws, sess = _FakeWS([
            {"method": "item/started", "params": {"item": {"type": "reasoning", "id": "r"}}},
            {"method": "item/started", "params": {"item": {"type": "commandExecution", "id": "c"}}},
            {"method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "c"}}},
            {"method": "item/started", "params": {"item": {"type": "fileChange", "id": "f"}}},
            {"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "a"}}},
            {"method": "turn/completed", "params": {}},
        ]), _FakeSess()
        st = []

        async def go():
            await asyncio.wait_for(codex_daemon._pump(
                ws, sess, lambda t: None, lambda s, t: None,
                on_stat=st.append), 5)

        asyncio.run(go())
        self.assertEqual(st, ["正在思考", "正在思考", "正在运行命令", "正在思考",
                              "正在改文件", "正在输出"])

    def test_aborted_turn_reports_failure(self):
        _, end, _, _ = self._run([{"method": "turn/aborted", "params": {}}])
        self.assertEqual(end, [("err", "")])


class PendingEventsTest(unittest.TestCase):
    """turn/start 响应前就到的事件不能丢（快速回答时整段增量可能都抢在响应前面）。"""

    def test_pending_frames_are_processed_first(self):
        ws, sess = _FakeWS([{"method": "turn/completed", "params": {}}]), _FakeSess()
        got, end = [], []

        async def go():
            await asyncio.wait_for(codex_daemon._pump(
                ws, sess, got.append, lambda st, t: end.append((st, t)),
                pending=[{"method": "item/agentMessage/delta", "params": {"delta": "好"}}]), 5)

        asyncio.run(go())
        self.assertEqual(got, ["好"])
        self.assertEqual(end, [("ok", "好")])


class StartTurnTest(unittest.TestCase):
    def test_no_daemon_reports_reason(self):
        with mock.patch.object(codex_daemon, "sock_path", return_value=""):
            ok, err = asyncio.run(codex_daemon.start_turn(
                "sid", "hi", lambda t: None, lambda st, t: None))
        self.assertFalse(ok)
        self.assertIn("守护进程", err)


class RestartGateTest(unittest.TestCase):
    """自愈重启只在「App 在跑 + 没有会话在跑 + 没刚重启过」三个条件都满足时动手。"""

    def setUp(self):
        self.app = mock.MagicMock()
        self.app.desktop_app_running.return_value = True
        self.app.app_busy_threads.return_value = []
        self.app.quit_desktop_app.return_value = True
        p = mock.patch.dict(sys.modules, {"agent_codex": self.app})
        p.start()
        self.addCleanup(p.stop)
        old = codex_daemon._APP_RST["at"]
        codex_daemon._APP_RST["at"] = 0.0
        self.addCleanup(codex_daemon._APP_RST.__setitem__, "at", old)

    def test_restarts_when_idle(self):
        with mock.patch.object(codex_daemon.subprocess, "run") as run:
            ok, why = codex_daemon.app_restart_if_idle("测试")
        self.assertTrue(ok, why)
        run.assert_called_once()
        self.assertIn("open", run.call_args[0][0])

    def test_never_interrupts_running_turn(self):
        self.app.app_busy_threads.return_value = ["01a1cdc4-x"]
        ok, why = codex_daemon.app_restart_if_idle("测试")
        self.assertFalse(ok)
        self.assertIn("在跑", why)
        self.app.quit_desktop_app.assert_not_called()

    def test_rate_limited(self):
        with mock.patch.object(codex_daemon.subprocess, "run"):
            self.assertTrue(codex_daemon.app_restart_if_idle("第一次")[0])
        ok, why = codex_daemon.app_restart_if_idle("马上又来")
        self.assertFalse(ok)
        self.assertIn("刚重启过", why)

    def test_closed_app_is_not_opened(self):
        self.app.desktop_app_running.return_value = False
        ok, why = codex_daemon.app_restart_if_idle("测试")
        self.assertFalse(ok)
        self.assertIn("没开", why)


class _EchoWS:
    """把每个带 id 的请求都当成成功回应，记录发出去的 method。"""

    def __init__(self):
        self.sent = []
        self.pending = []

    async def send_json(self, obj):
        self.sent.append(obj)
        if "id" in obj:
            self.pending.append({"id": obj["id"], "result": {}})

    async def receive_json(self):
        if self.pending:
            return self.pending.pop(0)
        await asyncio.sleep(3600)

    async def close(self):
        pass


class _FakeSession:
    ws = None

    def __init__(self, **kw):
        pass

    async def ws_connect(self, *a, **k):
        _FakeSession.ws = _EchoWS()
        return _FakeSession.ws

    async def close(self):
        pass


class TurnKindTest(unittest.TestCase):
    """旁观只能 resume，绝不能顺手 turn/start——否则桌面版正在跑的一轮会被重复发起。"""

    def _run(self, coro_maker):
        with mock.patch.object(codex_daemon, "sock_path", return_value="/tmp/x.sock"), \
             mock.patch.object(codex_daemon.aiohttp, "ClientSession", _FakeSession):
            ok, err = asyncio.run(coro_maker())
        return ok, err, [m.get("method") for m in (_FakeSession.ws.sent or [])]

    def test_watch_only_resumes(self):
        ok, err, meths = self._run(lambda: codex_daemon.watch_turn(
            "sid", lambda t: None, lambda st, t: None))
        self.assertTrue(ok, err)
        self.assertIn("thread/resume", meths)
        self.assertNotIn("turn/start", meths)

    def test_start_turn_also_starts(self):
        ok, err, meths = self._run(lambda: codex_daemon.start_turn(
            "sid", "hi", lambda t: None, lambda st, t: None))
        self.assertTrue(ok, err)
        self.assertIn("turn/start", meths)
