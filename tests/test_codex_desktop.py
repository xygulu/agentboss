#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Codex 桌面版（Codex.app）会话发现：没有 tty 的 codex 进程，rollout 由它内置的
app-server 开着——要作为「无窗口会话」列出来（能看转录、能收提醒，发送靠 caps 挡住）。

跑法：cd codeboss && python3 -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import agent_codex  # noqa: E402

SID_TUI = "11111111-1111-4111-8111-111111111111"
SID_APP = "019a1cd0-7f00-7000-8000-000000000001"


class CodexDesktopDiscoverTest(unittest.TestCase):
    def test_desktop_session_listed_as_no_window(self):
        tmp = Path(tempfile.mkdtemp())
        p_tui = tmp / ("rollout-2026-11-12T07-00-00-%s.jsonl" % SID_TUI)
        p_app = tmp / ("rollout-2026-11-12T07-50-00-%s.jsonl" % SID_APP)
        for p in (p_tui, p_app):
            p.write_text("{}\n")

        rows = {
            100: {"ppid": 1, "tty": "ttys001", "cmd": "codex"},
            200: {"ppid": 1, "tty": "??",
                  "cmd": "/Applications/Codex.app/Contents/MacOS/codex app-server"},
        }
        lsof = {100: ("/proj/a", p_tui), 200: (None, [p_app])}

        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows", return_value=rows), \
             mock.patch.object(agent_codex, "_proc_info_via_lsof",
                               side_effect=lambda pid: lsof[pid][:2]), \
             mock.patch.object(agent_codex, "_rollouts_via_lsof",
                               side_effect=lambda pid: lsof[pid][1]), \
             mock.patch.object(agent_codex.plat, "window_map",
                               return_value={"/dev/ttys001": (42, "win")}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               side_effect=lambda p: ("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            got = {x["sid"]: x for x in ad.discover()}

        self.assertEqual(set(got), {SID_TUI, SID_APP}, got)
        self.assertEqual(got[SID_TUI]["window_id"], 42)
        self.assertFalse(got[SID_TUI].get("desktop"))
        # 桌面版：无窗口无 tty，带 desktop 标记（server 据此写只读原因）
        self.assertIsNone(got[SID_APP]["window_id"])
        self.assertEqual(got[SID_APP]["tty"], "")
        self.assertTrue(got[SID_APP]["desktop"])

    def test_notify_seen_thread_listed_even_without_lsof(self):
        """lsof 看不到（桌面版读完关 fd）时，notify 见过的 thread 也要列出来。"""
        tmp = Path(tempfile.mkdtemp())
        day = tmp / "sessions" / "2026" / "11" / "12"
        day.mkdir(parents=True)
        p = day / ("rollout-2026-11-12T07-50-00-%s.jsonl" % SID_APP)
        p.write_text("{}\n")
        agent_codex._NOTIFY_SEEN.clear()
        agent_codex._note_notify(SID_APP, "/proj/a")
        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows",
                               return_value={1: {"ppid": 0, "tty": "??", "cmd": "launchd"}}), \
             mock.patch.object(agent_codex, "codex_home", return_value=tmp), \
             mock.patch.object(agent_codex, "_rollout_via_lsof", return_value=None), \
             mock.patch.object(agent_codex.plat, "window_map", return_value={}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               side_effect=lambda q: ("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            got = ad.discover()
        self.assertEqual([x["sid"] for x in got], [SID_APP], got)
        self.assertTrue(got[0].get("desktop"))
        agent_codex._NOTIFY_SEEN.clear()

    def test_desktop_duplicate_sid_not_listed_twice(self):
        """桌面版 rollout 和 TUI 会话是同一个 thread 时只留 TUI 那条。"""
        tmp = Path(tempfile.mkdtemp())
        p = tmp / ("rollout-2026-11-12T07-00-00-%s.jsonl" % SID_TUI)
        p.write_text("{}\n")
        rows = {
            100: {"ppid": 1, "tty": "ttys001", "cmd": "codex"},
            200: {"ppid": 1, "tty": "??", "cmd": "/x/Codex.app/codex app-server"},
        }
        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows", return_value=rows), \
             mock.patch.object(agent_codex, "_proc_info_via_lsof",
                               side_effect=lambda pid: ("/proj/a", p) if pid == 100 else ("", None)), \
             mock.patch.object(agent_codex, "_rollouts_via_lsof",
                               side_effect=lambda pid: [p]), \
             mock.patch.object(agent_codex.plat, "window_map",
                               return_value={"/dev/ttys001": (42, "win")}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               side_effect=lambda q: ("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            got = ad.discover()
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["window_id"], 42)


class ResumedExecThreadTest(unittest.TestCase):
    """exec 起的线程后来 `codex resume` 在终端里接着跑：source 还写着 exec，
    但它已经是交互会话，不能按 source 丢掉（否则手机点召回开了终端却显示已退出）。"""
    def _discover(self, cmd):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / ("rollout-2026-11-12T07-00-00-%s.jsonl" % SID_TUI)
        p.write_text('{"type":"session_meta","payload":{"source":"exec"}}\n')
        rows = {100: {"ppid": 1, "tty": "ttys001", "cmd": cmd}}
        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows", return_value=rows), \
             mock.patch.object(agent_codex, "_proc_info_via_lsof",
                               return_value=("/proj/a", p)), \
             mock.patch.object(agent_codex.plat, "window_map",
                               return_value={"/dev/ttys001": (42, "win")}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               return_value=("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            return ad.discover()

    def test_resume_in_terminal_is_interactive(self):
        got = self._discover("/Applications/x/codex resume %s" % SID_TUI)
        self.assertEqual([x["sid"] for x in got], [SID_TUI], got)

    def test_codex_exec_still_skipped(self):
        self.assertEqual(self._discover("/Applications/x/codex exec %s" % SID_TUI), [])


class PushMsgDesktopTest(unittest.TestCase):
    """推送模式的 msg 事件也要带 desktop/caps：手机端只吃这个事件，缺了就不切对话视图。"""
    def test_desktop_flagged_in_push_event(self):
        import server
        ad = mock.Mock()
        ad.transcript_path.return_value = "/x"
        ad.parse_transcript.return_value = []
        s = {"sid": SID_APP, "cwd": "/p", "agent": "codex", "state": "idle", "desktop": True}
        with mock.patch.object(server.agents, "get", return_value=ad), \
             mock.patch.object(server, "_meta_for_pid", return_value=None), \
             mock.patch.object(agent_codex, "exec_resume_ok", return_value=False):
            ev = asyncio.run(server._push_msg_ev(s, SID_APP, {"msg_prev": []}, prev_none=True))
        self.assertTrue(ev.get("desktop"))
        self.assertEqual(ev.get("caps"), [])
        self.assertIn("桌面版", ev.get("why") or "")

    def test_desktop_push_event_enables_input_when_exec_resume_ok(self):
        """这台机器没有能 resume 的 codex 时不能用机器实际状态断言，两种都要测。"""
        import server
        ad = mock.Mock()
        ad.transcript_path.return_value = "/x"
        ad.parse_transcript.return_value = []
        s = {"sid": SID_APP, "cwd": "/p", "agent": "codex", "state": "idle", "desktop": True}
        with mock.patch.object(server.agents, "get", return_value=ad), \
             mock.patch.object(server, "_meta_for_pid", return_value=None), \
             mock.patch.object(agent_codex, "exec_resume_ok", return_value=True):
            ev = asyncio.run(server._push_msg_ev(s, SID_APP, {"msg_prev": []}, prev_none=True))
        self.assertTrue(ev.get("desktop"))
        self.assertEqual(ev.get("caps"), ["input"])
        self.assertEqual(ev.get("why"), "")

    def test_plain_session_push_event_has_no_caps_override(self):
        import server
        ad = mock.Mock()
        ad.transcript_path.return_value = "/x"
        ad.parse_transcript.return_value = []
        s = {"sid": SID_TUI, "cwd": "/p", "agent": "codex", "state": "idle"}
        with mock.patch.object(server.agents, "get", return_value=ad), \
             mock.patch.object(server, "_meta_for_pid", return_value=None):
            ev = asyncio.run(server._push_msg_ev(s, SID_TUI, {"msg_prev": []}, prev_none=True))
        self.assertNotIn("caps", ev)
        self.assertFalse(ev.get("desktop"))


class PushMsgNoChangeTest(unittest.TestCase):
    """rollout 长了但解析出的消息没变（工具输出/思考）时不能推空尾巴——
    手机端刚垫上去的那条用户消息会被 inc_from 截断，之后增量全部「对不上号」。"""
    def test_unchanged_messages_emit_nothing(self):
        import server
        ad = mock.Mock()
        ad.transcript_path.return_value = "/x"
        msgs = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "yo"}]
        ad.parse_transcript.return_value = msgs
        s = {"sid": SID_APP, "cwd": "/p", "agent": "codex", "state": "idle", "desktop": True}
        with mock.patch.object(server.agents, "get", return_value=ad), \
             mock.patch.object(server, "_meta_for_pid", return_value=None):
            ev = asyncio.run(server._push_msg_ev(s, SID_APP, {"msg_prev": list(msgs)}))
        self.assertIsNone(ev)

    def test_new_message_still_pushes_tail(self):
        import server
        ad = mock.Mock()
        ad.transcript_path.return_value = "/x"
        ad.parse_transcript.return_value = [{"role": "user", "text": "hi"},
                                           {"role": "user", "text": "again"}]
        s = {"sid": SID_APP, "cwd": "/p", "agent": "codex", "state": "idle", "desktop": True}
        with mock.patch.object(server.agents, "get", return_value=ad), \
             mock.patch.object(server, "_meta_for_pid", return_value=None):
            ev = asyncio.run(server._push_msg_ev(s, SID_APP, {"msg_prev": [{"role": "user", "text": "hi"}]}))
        self.assertEqual((ev["inc"], ev["inc_from"]), (1, 1))
        self.assertEqual(ev["messages"], [{"role": "user", "text": "again"}])


class CodexDesktopAppInstalledTest(unittest.TestCase):
    """只装了 Codex 桌面版、没单独装 codex CLI 的机器：也算装了 codex，
    会话列表顶上的 Claude/Codex 切换条才会出现。"""
    def test_desktop_app_bin_counts_as_installed(self):
        app = "/Applications/Codex.app/Contents/MacOS/codex"
        env = {k: v for k, v in os.environ.items() if k != "CODEBOSS_CODEX_BIN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(agent_codex.shutil, "which", return_value=None), \
             mock.patch.object(agent_codex.os.path, "isfile", side_effect=lambda p: str(p) == app), \
             mock.patch.object(agent_codex.os, "access", return_value=True):
            self.assertEqual(agent_codex.codex_bin(), app)
            self.assertTrue(agent_codex.Adapter().installed())


    def test_codex_home_counts_as_installed(self):
        """没有 CLI、只有 ~/.codex（桌面版/以前用过）：三个平台判断都以此为准。"""
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(agent_codex, "codex_bin", return_value=None), \
             mock.patch.object(agent_codex, "codex_home", return_value=tmp):
            self.assertTrue(agent_codex.Adapter().installed())

    def test_windows_search_path_includes_npm_and_winget(self):
        """Windows 上 npm 全局 / winget 的 shim 不在 host 的 PATH 里也要找得到。"""
        seen = {}
        def fake_which(name, path=None):
            seen["path"] = path or ""
            return None
        env = {k: v for k, v in os.environ.items() if k != "CODEBOSS_CODEX_BIN"}
        env.update(APPDATA="C:/U/x/AppData/Roaming", LOCALAPPDATA="C:/U/x/AppData/Local")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(agent_codex.plat, "IS_WIN", True), \
             mock.patch.object(agent_codex.shutil, "which", side_effect=fake_which), \
             mock.patch.object(agent_codex.os.path, "isfile", return_value=False), \
             mock.patch.object(agent_codex.os, "access", return_value=False):
            self.assertIsNone(agent_codex.codex_bin())
        p = seen["path"].replace("\\", "/")
        self.assertIn("AppData/Roaming/npm", p)
        self.assertIn("WinGet/Links", p)


class CodexDaemonBinTest(unittest.TestCase):
    """只装桌面版、没 npm 装 codex 的机器：CLI 在守护进程的 release 包里，要能找到。
    （真机就是这样：npm shim 缺失/坏掉，~/.codex/packages/app-server-daemon 里有完整 codex。）"""
    def _mk(self):
        tmp = Path(tempfile.mkdtemp())
        bins = {}
        for ver in ("0.160.0-x", "0.167.0-x"):
            d = tmp / "packages" / "app-server-daemon" / "releases" / ver / "bin"
            d.mkdir(parents=True)
            p = d / "codex"
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
            bins[ver] = p
        return tmp, bins

    def test_daemon_release_bin_found_newest_first(self):
        tmp, bins = self._mk()
        env = {k: v for k, v in os.environ.items() if k != "CODEBOSS_CODEX_BIN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(agent_codex, "codex_home", return_value=tmp), \
             mock.patch.object(agent_codex.shutil, "which", return_value=None), \
             mock.patch.object(agent_codex.os.path, "isfile",
                               side_effect=lambda p: str(p) in {str(q) for q in bins.values()}):
            cands = agent_codex._codex_candidates()
            self.assertEqual(cands[0], str(bins["0.167.0-x"]), cands)
            self.assertEqual(len(cands), 2, cands)
            self.assertEqual(agent_codex.codex_bin(), str(bins["0.167.0-x"]))


class SeenBinTest(unittest.TestCase):
    """正在跑的 codex 装在哪都认：从 ps 的 argv[0] 记下来，优先当候选。"""
    def setUp(self):
        agent_codex._SEEN_BINS.clear()

    tearDown = setUp

    def test_note_bin_from_argv0(self):
        tmp = Path(tempfile.mkdtemp())
        b = tmp / "bin" / "codex"
        b.parent.mkdir(parents=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
        got = agent_codex._note_bin("%s app-server --listen unix://" % b)
        self.assertEqual(got, os.path.realpath(str(b)))
        self.assertIn(os.path.realpath(str(b)), agent_codex._SEEN_BINS)
        env = {k: v for k, v in os.environ.items() if k != "CODEBOSS_CODEX_BIN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(agent_codex, "_daemon_bins", return_value=[]), \
             mock.patch.object(agent_codex.shutil, "which", return_value=None):
            self.assertEqual(agent_codex._codex_candidates()[0], os.path.realpath(str(b)))

    def test_relative_or_missing_argv0_ignored(self):
        self.assertEqual(agent_codex._note_bin("codex app-server"), "")
        self.assertEqual(agent_codex._note_bin("/nope/codex app-server"), "")
        self.assertEqual(agent_codex._SEEN_BINS, set())

    def test_app_helper_not_named_codex_ignored(self):
        """桌面 App 的 Sparkle Autoupdate 命令行里也带 codex 字样，别拿去当候选探。"""
        tmp = Path(tempfile.mkdtemp())
        b = tmp / "Autoupdate"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
        self.assertEqual(agent_codex._note_bin("%s -NSApplicationName Codex" % b), "")
        self.assertEqual(agent_codex._SEEN_BINS, set())


class DesktopSendTest(unittest.TestCase):
    """桌面版发消息：靠 `codex exec resume`；支持与否探一次就缓存。"""
    def setUp(self):
        agent_codex._EXEC_RESUME.clear()
        agent_codex._DESK_RUN.clear()

    def test_exec_resume_probe_cached(self):
        calls = []
        def fake_run(argv, timeout=8, env=None):
            calls.append(argv)
            return 0, "Usage: codex exec resume <SESSION_ID> <PROMPT>", ""
        with mock.patch.object(agent_codex, "_codex_candidates", return_value=["/x/codex"]), \
             mock.patch.object(agent_codex, "_run", side_effect=fake_run):
            self.assertTrue(agent_codex.exec_resume_ok())
            self.assertTrue(agent_codex.exec_resume_ok())
        self.assertEqual(len(calls), 1, calls)

    def test_probe_timeout_not_cached_as_unsupported(self):
        """探测超时/起不来（rc<0）只是「没探到」，不能缓存成不支持——否则手机端
        这一整段时间（10 分钟）都把桌面版会话画成只读输入框。"""
        answers = [(-1, "", ""), (0, "  resume  Resume a previous session", "")]
        def fake_run(argv, timeout=8, env=None):
            return answers.pop(0)
        with mock.patch.object(agent_codex, "_codex_candidates", return_value=["/x/codex"]), \
             mock.patch.object(agent_codex, "_run", side_effect=fake_run):
            self.assertFalse(agent_codex.exec_resume_ok())     # 第一次：超时，先当不可用
            self.assertEqual(agent_codex._EXEC_RESUME, {})     # 但不落缓存
            self.assertTrue(agent_codex.exec_resume_ok())      # 下一次探测就通了

    def test_broken_first_binary_falls_back_to_second(self):
        """npm 那个 shim 坏了（缺可选依赖）时，接着试 Codex.app 自带的那个。"""
        def fake_run(argv, timeout=8, env=None):
            if argv[0] == "/broken/codex":
                return 1, "", "Missing optional dependency @openai/codex-darwin-arm64"
            return 0, "  resume  Resume a previous session", ""
        with mock.patch.object(agent_codex, "_codex_candidates",
                               return_value=["/broken/codex", "/Applications/Codex.app/Contents/MacOS/codex"]), \
             mock.patch.object(agent_codex, "_run", side_effect=fake_run):
            self.assertEqual(agent_codex.resume_bin(), "/Applications/Codex.app/Contents/MacOS/codex")

    def test_unsupported_version_reports_reason(self):
        with mock.patch.object(agent_codex, "_codex_candidates", return_value=["/x/codex"]), \
             mock.patch.object(agent_codex, "_run", return_value=(0, "Usage: codex exec <PROMPT>", "")):
            ok, msg, _note = agent_codex.desktop_send("/p", SID_APP, "hi")
        self.assertFalse(ok)
        self.assertIn("不支持", msg)

    def test_busy_session_refuses_second_send(self):
        class P:
            returncode = None
            def poll(self):
                return None
        agent_codex._DESK_RUN[SID_APP] = {"at": 0, "p": P(), "err": []}
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"):
            ok, msg, _note = agent_codex.desktop_send("/p", SID_APP, "hi")
        self.assertFalse(ok)
        self.assertIn("处理中", msg)

    def test_send_runs_exec_resume_and_reports_failure(self):
        class P:
            returncode = 2
            def __init__(self):
                import io
                self.stderr = io.BytesIO(b"error: session not found\n")
            def poll(self):
                return 2
        seen = {}
        def fake_popen(argv, **kw):
            seen["argv"] = argv
            return P()
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=False), \
             mock.patch.object(agent_codex.subprocess, "Popen", side_effect=fake_popen):
            ok, msg, _note = agent_codex.desktop_send("/p", SID_APP, "帮我看看")
        self.assertFalse(ok)
        self.assertIn("session not found", msg)
        self.assertEqual(seen["argv"][:3], ["/x/codex", "exec", "resume"])
        self.assertIn("--skip-git-repo-check", seen["argv"])
        self.assertIn('sandbox_mode="workspace-write"', " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][-2:], [SID_APP, "帮我看看"])

    def test_active_writer_quits_app_then_writes_same_thread(self):
        """App 开着这个对话时 rollout 被它占着写：先退出 App 释放写入权，再写原会话
        （同一会话，重开 App 能看到），不是 fork。"""
        import io
        class Reject:
            returncode = 1
            def __init__(self):
                self.stderr = io.BytesIO(b"ERROR failed to initialize thread persistence: "
                                         b"thread-store conflict: thread x already has an active writer\n")
            def poll(self):
                return 1
        class Running:
            returncode = None
            def __init__(self):
                self.stderr = io.BytesIO(b"")
            def poll(self):
                return None
        seq = [Reject(), Running()]
        calls = []
        def fake_popen(argv, **kw):
            calls.append(argv)
            return seq[len(calls) - 1]
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=False), \
             mock.patch.object(agent_codex, "app_busy_threads", return_value=[]), \
             mock.patch.object(agent_codex, "quit_desktop_app", return_value=True) as q, \
             mock.patch.object(agent_codex.subprocess, "Popen", side_effect=fake_popen):
            ok, msg, note = agent_codex.desktop_send("/p", SID_APP, "在吗")
        self.assertTrue(ok, msg)
        self.assertIn("原对话", note)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(calls[0], calls[1])          # 重试的是同一条 resume（原会话）
        self.assertIn(SID_APP, calls[0])
        q.assert_called_once()

    def test_active_writer_untouched_when_app_cannot_quit(self):
        import io
        class Reject:
            returncode = 1
            def __init__(self):
                self.stderr = io.BytesIO(b"thread-store conflict: x already has an active writer\n")
            def poll(self):
                return 1
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=False), \
             mock.patch.object(agent_codex, "app_busy_threads", return_value=[]), \
             mock.patch.object(agent_codex, "quit_desktop_app", return_value=False), \
             mock.patch.object(agent_codex.subprocess, "Popen", return_value=Reject()):
            ok, msg, _note = agent_codex.desktop_send("/p", SID_APP, "在吗")
        self.assertFalse(ok)
        self.assertIn("打开着", msg)

    def test_queue_is_preferred_and_never_quits_app(self):
        """最温和的路：codex queue 排进会话队列让 App/守护进程自己取，不抢写入权、不退 App。"""
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=True), \
             mock.patch.object(agent_codex, "queue_send", return_value=(True, "")) as qs, \
             mock.patch.object(agent_codex, "quit_desktop_app") as quit_, \
             mock.patch.object(agent_codex.subprocess, "Popen") as popen:
            ok, msg, note = agent_codex.desktop_send("/p", SID_APP, "在吗")
        self.assertTrue(ok, msg)
        self.assertEqual(note, "")
        qs.assert_called_once()
        self.assertEqual(qs.call_args[0][1], SID_APP)
        popen.assert_not_called()
        quit_.assert_not_called()

    def test_queue_unsupported_falls_back_to_exec_resume(self):
        import io
        class Running:
            returncode = None
            def __init__(self):
                self.stderr = io.BytesIO(b"")
            def poll(self):
                return None
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=True), \
             mock.patch.object(agent_codex, "queue_send", return_value=(False, "unknown command: queue")), \
             mock.patch.object(agent_codex.subprocess, "Popen", return_value=Running()) as popen:
            ok, msg, note = agent_codex.desktop_send("/p", SID_APP, "在吗")
        self.assertTrue(ok, msg)
        self.assertEqual(note, "")
        self.assertEqual(popen.call_args[0][0][:3], ["/x/codex", "exec", "resume"])

    def test_queue_send_argv(self):
        class R:
            returncode = 0
            stderr = b""
        with mock.patch.object(agent_codex.subprocess, "run", return_value=R()) as run:
            ok, err = agent_codex.queue_send("/x/codex", SID_APP, "你好")
        self.assertTrue(ok, err)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["/x/codex", "queue"])
        self.assertIn("--thread", argv)
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--thread") + 1], SID_APP)
        self.assertEqual(argv[argv.index("--message") + 1], "你好")

    def _conflict_send(self, busy):
        import io
        class Reject:
            returncode = 1
            def __init__(self):
                self.stderr = io.BytesIO(b"thread-store conflict: x already has an active writer\n")
            def poll(self):
                return 1
        with mock.patch.object(agent_codex, "resume_bin", return_value="/x/codex"), \
             mock.patch.object(agent_codex, "_desk_server_running", return_value=False), \
             mock.patch.object(agent_codex, "app_busy_threads", return_value=busy), \
             mock.patch.object(agent_codex, "quit_desktop_app") as q, \
             mock.patch.object(agent_codex.subprocess, "Popen", return_value=Reject()):
            ok, msg, _note = agent_codex.desktop_send("/p", SID_APP, "在吗")
        q.assert_not_called()      # 有对话在跑：绝不退 App
        return ok, msg

    def test_other_thread_running_blocks_app_quit(self):
        """App 里别的对话正在跑时退出会把它掐断（rollout 记 turn_aborted）——不能退。"""
        ok, msg = self._conflict_send([SID_TUI])
        self.assertFalse(ok)
        self.assertIn("还有 1 个对话正在运行", msg)

    def test_target_thread_running_blocks_app_quit(self):
        ok, msg = self._conflict_send([SID_APP])
        self.assertFalse(ok)
        self.assertIn("等这一轮答完", msg)

    def test_last_task_event_reads_tail(self):
        import json
        p = Path(tempfile.mkdtemp()) / ("rollout-x-%s.jsonl" % SID_APP)
        rows = [{"type": "session_meta", "payload": {}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "response_item", "payload": {"role": "assistant"}}]
        p.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
        self.assertEqual(agent_codex._last_task_event(p), "task_started")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        self.assertEqual(agent_codex._last_task_event(p), "task_complete")


if __name__ == "__main__":
    unittest.main()


def setUpModule():
    # 来源记录会落盘；测试写到临时目录，别碰真 host 的文件
    agent_codex._ORIGIN_FILE = Path(tempfile.mkdtemp()) / "origin.json"
    agent_codex._ORIGIN.clear()


class SessionOriginTest(unittest.TestCase):
    """会话最后一次用在桌面版还是终端：召回想回原处（桌面版回桌面版，消息进队列）。"""

    def setUp(self):
        agent_codex._ORIGIN.clear()

    def _rollout(self, originator):
        p = Path(tempfile.mkdtemp()) / ("rollout-2026-11-12T07-00-00-%s.jsonl" % SID_APP)
        p.write_text(json.dumps({"type": "session_meta",
                                 "payload": {"originator": originator}}) + "\n")
        return p

    def test_desktop_originator_means_desktop(self):
        self.assertEqual(agent_codex.thread_origin(self._rollout("codex_work_desktop")),
                         "desktop")

    def test_tui_originator_means_terminal(self):
        self.assertEqual(agent_codex.thread_origin(self._rollout("codex-tui")), "terminal")

    def test_recorded_origin_wins_over_rollout(self):
        p = self._rollout("codex_work_desktop")
        with mock.patch.object(agent_codex, "rollout_path", return_value=p):
            agent_codex.note_origin(SID_APP, "terminal")
            self.assertEqual(agent_codex.last_origin(SID_APP), "terminal")
            agent_codex._ORIGIN.clear()
            agent_codex._origin_load()
            self.assertEqual(agent_codex.last_origin(SID_APP), "terminal",
                             "记录要落盘，host 重启后还认得来源")

    def test_open_desktop_row_listed_without_lsof(self):
        """App 只在内存里开着 thread、不挂 rollout fd 时，召回后也要列一条桌面版行，
        否则手机端一直显示已退出。"""
        tmp = Path(tempfile.mkdtemp())
        p = tmp / ("rollout-2026-11-12T07-50-00-%s.jsonl" % SID_APP)
        p.write_text("{}\n")
        agent_codex.mark_desktop_open(SID_APP)
        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows",
                               return_value={1: {"ppid": 0, "tty": "??", "cmd": "launchd"}}), \
             mock.patch.object(agent_codex, "rollout_path", return_value=p), \
             mock.patch.object(agent_codex.plat, "window_map", return_value={}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               return_value=("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            got = ad.discover()
        self.assertEqual([x["sid"] for x in got], [SID_APP], got)
        self.assertTrue(got[0].get("desktop"))

    def test_open_desktop_running_app_skips_ui_launch(self):
        with mock.patch.object(agent_codex, "_desk_server_running", return_value=True), \
             mock.patch.object(agent_codex.subprocess, "run") as run:
            ok, err = agent_codex.open_desktop(SID_APP)
        self.assertTrue(ok, err)
        run.assert_not_called()          # 桌面版开着：只记一笔，消息走队列
        self.assertEqual(agent_codex.last_origin(SID_APP), "desktop")


class DraftWaitTest(unittest.TestCase):
    """话交给桌面版 App 之后：先挂「正在处理」占位，转录里冒出新消息就收掉。"""

    def setUp(self):
        import server
        self.srv = server
        server._DRAFTS.clear()

    def tearDown(self):
        self.srv._DRAFTS.clear()

    def test_waiting_draft_clears_when_desktop_reply_lands(self):
        srv = self.srv
        srv._draft_begin("s1", waiting=True)
        self.assertTrue(srv._DRAFTS["s1"]["waiting"])
        self.assertFalse(srv._DRAFTS["s1"]["done"])
        ad = mock.Mock()
        ad.transcript_path.return_value = "/tmp/t.jsonl"
        ad.parse_transcript.return_value = [{"role": "user", "text": "问"},
                                            {"role": "assistant", "text": "答"}]
        c = {"msg_prev": [{"role": "user", "text": "问"}]}
        s = {"agent": "codex", "state": "idle", "cwd": "/tmp", "pid": 0}
        with mock.patch.object(srv.agents, "get", return_value=ad):
            ev = asyncio.run(srv._push_msg_ev(s, "s1", c))
        self.assertEqual(ev.get("inc"), 1)
        self.assertTrue(srv._DRAFTS["s1"]["done"])
        self.assertFalse(srv._DRAFTS["s1"]["waiting"])

    def test_streaming_delta_clears_wait_flag(self):
        srv = self.srv
        srv._draft_begin("s2", waiting=True)
        srv._draft_delta("s2", "半")
        self.assertFalse(srv._DRAFTS["s2"]["waiting"])
        self.assertEqual(srv._DRAFTS["s2"]["text"], "半")


class HelperProcessBudgetTest(unittest.TestCase):
    """ChatGPT.app 的 Chromium 助手进程（Codex Framework / Aperitif）命令行里也带
    "codex"，会先把 8 个 lsof 名额吃光，真 app-server 轮不到——桌面版会话就又没了。"""

    def test_framework_helpers_do_not_eat_lsof_budget(self):
        tmp = Path(tempfile.mkdtemp())
        p_app = tmp / ("rollout-2026-11-12T07-50-00-%s.jsonl" % SID_APP)
        p_app.write_text("{}\n")
        rows = {}
        for i in range(8):
            rows[10 + i] = {"ppid": 508, "tty": "??",
                            "cmd": "/Applications/ChatGPT.app/Contents/Frameworks/Codex "
                                   "Framework.framework/Helpers/Codex (Aperitif) --type=renderer"}
        rows[200] = {"ppid": 508, "tty": "??",
                     "cmd": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
                            "CodexCLI.app/Contents/MacOS/codex app-server"}
        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_ps_rows", return_value=rows), \
             mock.patch.object(agent_codex, "_rollouts_via_lsof",
                               side_effect=lambda pid: [p_app] if pid == 200 else []), \
             mock.patch.object(agent_codex.plat, "window_map", return_value={}), \
             mock.patch.object(agent_codex, "rollout_meta",
                               return_value=("/proj/a", "标题")), \
             mock.patch.object(agent_codex, "rollout_state", return_value="idle"):
            got = ad.discover()
        self.assertEqual([x["sid"] for x in got], [SID_APP], got)
        self.assertTrue(got[0].get("desktop"))
