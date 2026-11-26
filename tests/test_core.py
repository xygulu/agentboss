#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""核心路径回归测试（stdlib unittest，无框架、无夹具）。

跑法：cd codeboss && python3 -m unittest discover -s tests -v
CI 与发版前各跑一遍。只覆盖「错了会静默出事」的纯逻辑：
更新包签名、版本比较、DB 迁移幂等。
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# RFC 8032 §7.1 TEST 1：seed / 空消息 / 公钥 / 签名
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ed = _load("cb_ed25519", os.path.join(ROOT, "tools", "ed25519.py"))


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector(self):
        self.assertEqual(ed.publickey(SEED), PUB)
        self.assertEqual(ed.sign(SEED, b""), SIG)
        self.assertIs(ed.verify(PUB, b"", SIG), True)

    def test_tamper_rejected(self):
        self.assertIs(ed.verify(PUB, b"x", SIG), False)
        bad = bytearray(SIG)
        bad[0] ^= 1
        self.assertIs(ed.verify(PUB, b"", bytes(bad)), False)
        self.assertIs(ed.verify(b"\x00" * 32, b"", SIG), False)
        # 畸形输入不能抛异常（host 里在 try 之外调用）
        self.assertIs(ed.verify(b"", b"", b""), False)
        self.assertIs(ed.verify(PUB, b"", SIG[:32]), False)


class TestPkgSignature(unittest.TestCase):
    """host 侧校验逻辑：中继换了包或改了版本号都必须过不去。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = __import__("server")

    def _sig(self, ver, sha):
        return ed.sign(SEED, self.srv._pkg_msg(ver, sha))

    def test_message_binds_version_and_hash(self):
        self.assertEqual(self.srv._pkg_msg("0.8.21", "ab"), b"0.8.21|ab")
        self.assertEqual(self.srv._pkg_msg(None, "ab"), b"|ab")

    def test_verify_accepts_matching(self):
        sig = self._sig("0.8.21", "a" * 64)
        self.assertIs(self.srv._sig_verify(PUB, self.srv._pkg_msg("0.8.21", "a" * 64), sig), True)

    def test_verify_rejects_substitution(self):
        sig = self._sig("0.8.21", "a" * 64)
        # 中继把内容换成别的包
        self.assertIs(self.srv._sig_verify(PUB, self.srv._pkg_msg("0.8.21", "b" * 64), sig), False)
        # 中继谎报版本号
        self.assertIs(self.srv._sig_verify(PUB, self.srv._pkg_msg("0.9.0", "a" * 64), sig), False)

    def test_builtin_pubkey_is_set(self):
        # 公钥丢了 = 验签形同虚设；这条防的是「发布包忘了内置公钥」
        self.assertEqual(len(self.srv._SIG_PUB), 32)

    def test_apply_pkg_gets_sig_from_state(self):
        """中继返回的 sig 必须原样进 relay_pkg：漏了会把自己签的包拒掉（0.8.23 的坑）。"""
        srv = self.srv
        old_ver, old_fn = srv._relay_host_ver, srv._ver_tuple
        srv._relay_host_ver = lambda: {"ver": "9.9.9", "sha256": "ab" * 32,
                                       "sig": "cd" * 64, "url": "/x.tgz"}
        try:
            srv._update_state.update(at=0.0, relay_pkg=None, relay_ver="")
            srv.update_check(force=True)
            self.assertEqual((srv._update_state.get("relay_pkg") or {}).get("sig"), "cd" * 64)
        finally:
            srv._relay_host_ver, srv._ver_tuple = old_ver, old_fn


class TestVerTuple(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = staticmethod(__import__("server")._ver_tuple)

    def test_order(self):
        self.assertLess(self.v("0.8.9"), self.v("0.8.21"))   # 字符串比较会在这里出错
        self.assertLess(self.v("0.8.21"), self.v("0.9.0"))
        self.assertEqual(self.v("v1.2.3"), self.v("1.2.3"))
        self.assertEqual(self.v(""), (0,))
        self.assertFalse(self.v("garbage") > self.v("0.0.1"))


class TestDbMigrate(unittest.TestCase):
    """_db_migrate 必须幂等：每次进程启动都会跑，跑第二遍不能炸。"""

    def test_idempotent(self):
        srv = __import__("server")
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            con = sqlite3.connect(path)
            srv._db_migrate(con)
            con.close()
            con = sqlite3.connect(path)
            srv._db_migrate(con)      # 第二遍：表/列/索引都已存在
            n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            con.close()
            self.assertGreater(n, 0)
        finally:
            os.unlink(path)


class TestSvcRollup(unittest.TestCase):
    """[svc] 日聚合在后台线程里跑：必须在工作线程里自己开 sqlite 连接。

    线上症状：`[svc] 日聚合落库失败：SQLite objects created in a thread can only be used
    in that same thread`，一天 280+ 条，svc_daily/svc_hour 一直是空的。"""

    def test_rollup_from_worker_thread_persists(self):
        relay = __import__("relay")
        d = tempfile.mkdtemp()
        dbp = os.path.join(d, "roll.db")
        relay.db_init(dbp)
        relay._svc_note("llm", True, 12.0)
        relay._svc_note("llm", False, 0)
        th = threading.Thread(target=relay._svc_rollup, args=(dbp,))
        th.start()
        th.join()
        con = sqlite3.connect(dbp)
        rows = con.execute("SELECT ok,fail FROM svc_daily WHERE key='llm'").fetchall()
        hours = con.execute("SELECT count(*) FROM svc_hour WHERE key='llm'").fetchone()[0]
        con.close()
        self.assertEqual(rows, [(1, 1)], "工作线程里聚合没有落库")
        self.assertGreaterEqual(hours, 1)


class TestSvcSave(unittest.TestCase):
    """service 状态落盘：`_svc_save` 抛异常会被 `_svc_watch` 吞掉，静默不写。

    线上症状：`svc_state.json` 停在 11-10 10:46 再也不更新，探活却一直在跑。"""

    def test_save_writes_state_file(self):
        relay = __import__("relay")
        d = tempfile.mkdtemp()
        dbp = os.path.join(d, "save.db")
        relay._svc_note("savet", True, 12.0, n=100)
        relay._svc_note("savet", False, 0, err="boom")
        self.assertTrue(relay._svc_save({"dbp": dbp}), "落盘返回 False")
        p = os.path.join(d, "svc_state.json")
        self.assertTrue(os.path.exists(p), "没有写出 svc_state.json")
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
        self.assertIn("savet", got)
        self.assertEqual(len(got["savet"]["ev"]), 2, got["savet"]["ev"])
        self.assertEqual(got["savet"]["ev"][1][1], 0)   # 失败事件 ok=0
        # 写完清脏标记；没新事件时不重复写
        self.assertFalse(relay._svc_save({"dbp": dbp}))

    def test_save_tolerates_legacy_3_tuple(self):
        """老状态文件里的短事件（3 元组）不能把整次落盘搞崩。"""
        relay = __import__("relay")
        d = tempfile.mkdtemp()
        dbp = os.path.join(d, "legacy.db")
        import time
        relay._svc("legacy")["ev"].append((time.time(), 1, 5.0))
        relay._SVC_DIRTY[0] = True
        self.assertTrue(relay._svc_save({"dbp": dbp}))
        with open(os.path.join(d, "svc_state.json"), encoding="utf-8") as f:
            ev = json.load(f)["legacy"]["ev"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(len(ev[0]), 6, ev[0])


class TestAdmSession(unittest.TestCase):
    """后台登录会话：手机号登录走 cb_adm cookie，要能跨天保持（滑动续期）。"""

    class _Req:
        def __init__(self, app, exp, scheme="https"):
            self.app, self.scheme = app, scheme
            self.cookies = {"cb_adm": __import__("relay")._adm_sess(app, exp)} if exp else {}

    def test_renew_when_less_than_half_left(self):
        import time
        from aiohttp import web
        relay = __import__("relay")
        app = {"key": b"k" * 32}
        old = int(time.time() + 5 * 86400)
        resp = relay._adm_touch(self._Req(app, old), web.json_response({}))
        self.assertIn("cb_adm", resp.cookies, "快到期的会话没有续期")
        new_exp = int(resp.cookies["cb_adm"].value.split(".")[1])
        self.assertGreater(new_exp, old + 20 * 86400)

    def test_no_renew_when_fresh(self):
        import time
        from aiohttp import web
        relay = __import__("relay")
        app = {"key": b"k" * 32}
        resp = relay._adm_touch(self._Req(app, int(time.time() + 25 * 86400)),
                                web.json_response({}))
        self.assertNotIn("cb_adm", resp.cookies, "刚登录的会话不该重复写 cookie")

    def test_no_renew_for_token_auth_or_expired(self):
        import time
        from aiohttp import web
        relay = __import__("relay")
        app = {"key": b"k" * 32}
        for req in (self._Req(app, 0), self._Req(app, int(time.time() - 10))):
            self.assertNotIn("cb_adm", relay._adm_touch(req, web.json_response({})).cookies)

    def test_ttl_is_long(self):
        self.assertGreaterEqual(__import__("relay").ADM_TTL, 30 * 86400)


class TestApkTicket(unittest.TestCase):
    """管理 App 下载票据：签给浏览器用（那条链路没有 cookie/头），必须挡伪造和过期。"""

    @classmethod
    def setUpClass(cls):
        cls.r = __import__("relay")
        cls.key = b"k" * 32

    def _ticket(self, exp, key=None):
        import hashlib
        import hmac
        return "%d.%s" % (exp, hmac.new(key or self.key, ("apk.%d" % exp).encode(),
                                        hashlib.sha256).hexdigest())

    def test_valid_and_expiry(self):
        import time
        now = int(time.time())
        self.assertTrue(self.r._apk_ticket_ok(self.key, self._ticket(now + 600)))
        self.assertFalse(self.r._apk_ticket_ok(self.key, self._ticket(now - 1)))

    def test_forgery_rejected(self):
        import time
        now = int(time.time())
        self.assertFalse(self.r._apk_ticket_ok(self.key, self._ticket(now + 600, b"x" * 32)))
        self.assertFalse(self.r._apk_ticket_ok(self.key, "abc"))
        self.assertFalse(self.r._apk_ticket_ok(self.key, "%d." % (now + 600)))
        self.assertFalse(self.r._apk_ticket_ok(self.key, ""))


class TestDefaultAgent(unittest.TestCase):
    """请求没指定 agent 时按「这台机器装了谁」定，不能硬默认 Claude。

    线上症状：只装 Codex 的开发机上，手机「当前模型 / 切模型」显示的是 Claude
    （前端只有一种 agent 时不带 ?agent=，服务端一路答 Claude）。"""

    class _Ad:
        def __init__(self, key, on):
            self.key, self.label, self._on = key, key, on

        def installed(self):
            return self._on

    def _with(self, ads):
        srv = __import__("server")
        orig = srv.agents.adapters
        srv.agents.adapters = lambda: ads
        self.addCleanup(setattr, srv.agents, "adapters", orig)
        return srv

    def test_codex_only_machine(self):
        srv = self._with([self._Ad("claude", False), self._Ad("codex", True)])
        self.assertEqual(srv._default_agent(), "codex")

    def test_both_installed_still_claude(self):
        srv = self._with([self._Ad("claude", True), self._Ad("codex", True)])
        self.assertEqual(srv._default_agent(), "claude")

    def test_nothing_detected_falls_back_to_claude(self):
        srv = self._with([self._Ad("claude", False), self._Ad("codex", False)])
        self.assertEqual(srv._default_agent(), "claude")


class TestLanIps(unittest.TestCase):
    """_lan_ips 在 macOS 上走 mDNS 解析，网络不顺能卡十几秒。

    线上症状：用户机器日志里 `[loop] 卡顿 2~19s` 反复出现，每次的栈都是
    _direct_watch → _direct_state → _lan_ips —— 它在事件循环线程里被直接调用。
    修法：结果缓存 60 秒（这里锁住），调用方放 executor。"""

    def test_cached_between_calls(self):
        import socket
        srv = __import__("server")
        calls = []
        orig = socket.gethostbyname_ex
        socket.gethostbyname_ex = lambda h: (calls.append(h) or (h, [], ["192.168.1.9"]))
        try:
            srv._LAN_CACHE.update(t=0.0, ips=[])
            a = srv._lan_ips()
            b = srv._lan_ips()
        finally:
            socket.gethostbyname_ex = orig
            srv._LAN_CACHE.update(t=0.0, ips=[])
        self.assertIn("192.168.1.9", a)
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1, "第二次调用没有走缓存，又解析了一次主机名")


class TestTerminalCaps(unittest.TestCase):
    """终端适配器：id 约定、转义、能力判定。AppleScript 实际执行不在单测里跑。"""

    @classmethod
    def setUpClass(cls):
        cls.terms = _load("cb_terms", os.path.join(ROOT, "shared", "terms.py"))
        cls.plat = __import__("plat")

    def test_iterm_id(self):
        self.assertTrue(self.terms.owns("it:abc"))
        self.assertFalse(self.terms.owns(3))
        self.assertFalse(self.terms.owns(None))

    def test_escape_quotes(self):
        self.assertEqual(self.terms._esc('a"b\\c'), 'a\\"b\\\\c')

    @unittest.skipUnless(sys.platform == "darwin", "iTerm2/Terminal.app 的 window_map 只在 macOS 上")
    def test_caps_iterm(self):
        orig = self.plat.window_map
        self.plat.window_map = lambda: {"/dev/ttys001": ("it:x", "t")}
        try:
            c = self.plat.caps("/dev/ttys001")
        finally:
            self.plat.window_map = orig
        self.assertIn("input", c)
        self.assertIn("screen", c)

    def test_caps_readonly_when_unknown(self):
        orig = self.plat.window_map
        self.plat.window_map = lambda: {}
        try:
            self.assertEqual(self.plat.caps("/dev/ttys009"), frozenset())
        finally:
            self.plat.window_map = orig

    def test_iterm_supports_single_keys(self):
        """iTerm2 以前不能发单键 → Claude 的确认框只能「接管」。现在走 write text。"""
        self.assertIn("keys", self.terms.CAPS)
        expr = self.terms._asc_expr(b"\x1b[B")
        self.assertEqual(expr, '((character id 27) & "[" & "B")')
        self.assertEqual(self.terms._asc_expr(b""), '""')

    def test_iterm_key_dispatch(self):
        """按下方向键 → iTerm2 那条路必须走 terms.send_key，别再回到「只能接管」。"""
        if not self.plat.IS_MAC:
            self.skipTest("iTerm2 只存在于 macOS")
        t = self.plat.terms          # plat 自己 import 的那个 terms，不是本类另加载的副本
        old_wm, old_key = self.plat.window_map, t.send_key
        calls = []
        self.plat.window_map = lambda: {"/dev/ttys001": ("it:x", "t")}
        t.send_key = lambda wid, raw: (calls.append((wid, raw)) or (True, "ok"))
        try:
            ok, _msg = self.plat.send_special("/dev/ttys001", "down", 125, None)
        finally:
            self.plat.window_map, t.send_key = old_wm, old_key
        self.assertTrue(ok)
        self.assertEqual(calls, [("it:x", b"\x1b[B")])

    def test_iterm_key_bytes_cover_server_keys(self):
        """宿主认识的每个按键，iTerm2 这条路都得有对应字节，否则手机上又会「点了没反应」。"""
        srv = __import__("server")
        missing = [k for k in srv.EXT_SPECIAL_KEYS if k not in self.terms.KEY_BYTES]
        self.assertEqual(missing, [])
        self.assertEqual(self.terms.KEY_BYTES["down"], b"\x1b[B")
        self.assertEqual(self.terms.KEY_BYTES["enter"], b"\r")

    def test_screen_tail_truncates(self):
        text = "\n".join("line%d" % i for i in range(500))
        out = self.plat._tail(text, 200)
        self.assertEqual(len(out.splitlines()), 200)
        self.assertTrue(out.startswith("line300"))
        self.assertEqual(self.plat._tail("a\nb", 200), "a\nb")

    def test_no_caps_why_explains(self):
        self.assertTrue(self.plat.no_caps_why("").strip())
        why = self.plat.no_caps_why("/dev/ttys009")
        self.assertTrue(why and "接管" in why)


class _Req:
    """require_token 用得到的最小请求面（不进真实 aiohttp app）。"""

    def __init__(self, path, method="POST", remote="192.168.1.9", query=None):
        self.path, self.method, self.remote = path, method, remote
        self.query = {"t": "tok"} if query is None else query
        self.cookies = {}
        self.headers = {}


class TestTokenNoDurationGate(unittest.TestCase):
    """时长墙已摘（2026-11-26 拍板：收费墙只按条数，去掉时长）。

    以前局域网 token / dt / P2P 写操作会撞 `_use_over` 403（quota JSON，
    2026-11-11 复查的「绕过 A」修复）——现在整面墙退役，写操作不再查时长；
    条数墙（_send_over）和功能墙照旧。边界不变：本机（127.x）不拦；读请求不拦。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = __import__("server")

    def _call(self, path, method="POST", remote="192.168.1.9", query=None):
        srv = self.srv
        with mock.patch.object(srv, "_tokens_current", lambda: ["tok"]), \
             mock.patch.object(srv, "_send_over", lambda: False), \
             mock.patch.object(srv, "_use_send_count", lambda r: None), \
             mock.patch.object(srv, "_use_touch", lambda r=None, **k: None), \
             mock.patch.object(srv, "_listen_touch", lambda r: None):
            return asyncio.run(srv.require_token(_Req(path, method, remote, query)))

    def test_duration_gate_is_gone(self):
        # 摘墙是「不存在」不是「不触发」：老代码超额时会拦人
        self.assertFalse(hasattr(self.srv, "_use_over"))
        self.assertFalse(hasattr(self.srv, "_use_403"))

    def test_lan_write_not_duration_blocked(self):
        self._call("/api/ext_key")
        self._call("/api/ext_key", query={"t": "tok", "a": "0"})

    def test_relay_forwarded_local_peer_passes(self):
        # 中继把请求翻译成 127.0.0.1:8790 再打进来，账已经在中继侧算过
        self._call("/api/ext_key", remote="127.0.0.1")
        self._call("/api/ext_key", remote="::1")

    def test_reads_and_exempt_paths_pass(self):
        self._call("/api/ext_key", method="GET")
        self._call("/api/seen")           # 标已读：属于「看」
        self._call("/api/ext_pref")       # 改本机偏好：不算远程操作
        self._call("/api/ext_push")       # 长连接


class TestMeterTables(unittest.TestCase):
    """三处豁免表必须同口径：中继 METER_EXEMPT_PATHS == 开发机 USE_STREAM_PATHS。

    脱节的表现是「倒计时停了、账在走」或反过来——两边分开改过一次。"""

    def test_host_relay_tables_identical(self):
        relay = __import__("relay")
        srv = __import__("server")
        self.assertEqual(tuple(relay.METER_EXEMPT_PATHS), tuple(srv.USE_STREAM_PATHS))

    def test_seen_exempt_writes_not(self):
        relay = __import__("relay")
        self.assertTrue(relay._meter_exempt("/api/seen"))
        self.assertFalse(relay._meter_exempt("/api/ext_send"))
        self.assertFalse(relay._meter_exempt("/api/ext_key"))


class TestMetaForPid(unittest.TestCase):
    """父链找全彩 meta 不许再跑 ps：事件循环里跑一次能卡 7 秒（客户机 2026-11-11 抓栈）。"""

    def test_parent_chain_from_cache(self):
        srv = __import__("server")
        old_m = srv._PPID_CACHE["m"]
        old_meta = srv._ANSI_META_CACHE["data"]
        try:
            srv._ANSI_META_CACHE["data"] = [{"pid": 100, "log": "/tmp/x"}]
            srv._PPID_CACHE["m"] = {300: 200, 200: 100, 100: 1}
            self.assertEqual(srv._meta_for_pid(300)["pid"], 100)
            self.assertIsNone(srv._meta_for_pid(999))
        finally:
            srv._PPID_CACHE["m"] = old_m
            srv._ANSI_META_CACHE["data"] = old_meta


class TestWinMirrorMeta(unittest.TestCase):
    """Windows host 会话写 wrapper 同款 meta+log：App 靠它认出全彩（和 mac 一样的界面）。"""

    def test_open_tee_close(self):
        srv = __import__("server")
        from pathlib import Path
        s = srv.Session.__new__(srv.Session)
        s.sid, s.agent, s.cmd, s.cwd, s.pid = "abcd1234", "claude", ["claude"], "/tmp", 4321
        s.rows, s.cols, s.started = 24, 80, 1.0
        s._mlog = s._mmeta = s._mmeta_path = None
        s.out_buf, s.out_start, s.out_ev = bytearray(), 0, asyncio.Event()
        s.clients, s.last_out = set(), 0.0

        old_dir, old_cache = srv.ANSI_DIR, srv._ANSI_META_CACHE["data"]
        with tempfile.TemporaryDirectory() as td:
            srv.ANSI_DIR = Path(td)
            try:
                s._mirror_open()
                s.broadcast_bytes(b"hello\r\n")
                meta = json.loads((Path(td) / "abcd1234.meta.json").read_text("utf-8"))
                self.assertEqual((meta["pid"], meta["sid"], meta["host"]), (4321, "abcd1234", True))
                self.assertEqual((Path(td) / "abcd1234.log").read_bytes(), b"hello\r\n")
                srv._ansi_metas_refresh()
                self.assertEqual(srv._meta_for_pid(4321)["sid"], "abcd1234")   # App 的全彩开关
                s._mirror_close(0)
                meta = json.loads((Path(td) / "abcd1234.meta.json").read_text("utf-8"))
                self.assertTrue(meta["exited"])
                self.assertIsNone(s._mlog)
            finally:
                srv.ANSI_DIR, srv._ANSI_META_CACHE["data"] = old_dir, old_cache


class TestCtlWriteHostSession(unittest.TestCase):
    """Windows host 会话没有 wrapper 控制 socket：meta 带 host 时直接写进 Session。"""

    def test_writes_into_session(self):
        srv = __import__("server")

        class FakeS:
            alive = True

            def __init__(self):
                self.w = []

            def write(self, b):
                self.w.append(b)

        s = FakeS()
        srv.SESSIONS["abc"] = s
        try:
            self.assertTrue(srv._pty_ctl_write({"host": True, "sid": "abc"}, b"\x0c"))
            self.assertEqual(s.w, [b"\x0c"])
            self.assertFalse(srv._pty_ctl_write({"host": True, "sid": "nope"}, b"\x0c"))
            s.alive = False
            self.assertFalse(srv._pty_ctl_write({"host": True, "sid": "abc"}, b"\x0c"))
        finally:
            srv.SESSIONS.pop("abc", None)


class TestLogRotate(unittest.TestCase):
    """运行中轮转 server.log：copytruncate（改名会让 launchd 的 fd 一直写旧 inode），
    轮出来的历史超过 7 天要删。"""

    def test_copytruncate_and_expire(self):
        srv = __import__("server")
        d = tempfile.mkdtemp()
        logs = os.path.join(d, "logs")
        os.makedirs(logs)
        p = os.path.join(logs, "server.log")
        with open(p, "wb") as f:
            f.write(b"x" * 100)
        old = os.path.join(logs, "server.err.log.20200101-000000")
        with open(old, "wb") as f:
            f.write(b"y")
        os.utime(old, (0, 0))
        try:
            with mock.patch.object(srv, "HERE", __import__("pathlib").Path(d)), \
                    mock.patch.object(srv, "LOG_ROT_BYTES", 10):
                srv._log_rotate_live()
        finally:
            pass
        self.assertEqual(os.path.getsize(p), 0)          # 截断，进程继续往同一个 fd 写
        hist = [x for x in os.listdir(logs) if x.startswith("server.log.")]
        self.assertEqual(len(hist), 1)
        self.assertEqual(os.path.getsize(os.path.join(logs, hist[0])), 10)   # 只留尾部
        self.assertFalse(os.path.exists(old))            # 7 天前的删掉


class TestTrustConfirm(unittest.TestCase):
    """信任框选 No 时：下移和回车必须分两次写（一包 \\x1b[B\\r 会被 TUI 吞掉回车）。"""

    def test_split_down_enter(self):
        srv = __import__("server")
        screen = "  ❯ No, exit\n    Yes, I trust this folder\n  Enter to confirm"
        writes = []
        with mock.patch.object(srv.plat, "screen", return_value=(True, screen)), \
             mock.patch.object(srv, "_pty_ctl_write", side_effect=lambda m, b: (writes.append(b), True)[1]), \
             mock.patch.object(srv.time, "sleep"):
            self.assertTrue(srv._trust_confirm("/dev/ttys9", 1, {}))
        self.assertEqual(writes, [b"\x1b[B", b"\r"])

    def test_default_yes_single_enter(self):
        srv = __import__("server")
        screen = "  ❯ Yes, I trust this folder\n    No, exit"
        writes = []
        with mock.patch.object(srv.plat, "screen", return_value=(True, screen)), \
             mock.patch.object(srv, "_pty_ctl_write", side_effect=lambda m, b: (writes.append(b), True)[1]):
            self.assertTrue(srv._trust_confirm("/dev/ttys9", 1, {}))
        self.assertEqual(writes, [b"\r"])


class TestClaudePretrust(unittest.TestCase):
    """首次启动的「选择界面风格」和目录信任框都要提前写掉：Windows 没有全彩镜像，
    手机端看不见也点不着，新会话会永远卡在这些选择界面上。"""

    def test_first_run_seeded(self):
        srv = __import__("server")
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"HOME": home}):
            cwd = os.path.join(home, "proj")
            os.makedirs(cwd)
            self.assertTrue(srv._claude_pretrust(cwd))
            with open(os.path.join(home, ".claude.json"), encoding="utf-8") as f:
                d = json.load(f)
            self.assertIs(d.get("hasCompletedOnboarding"), True)
            self.assertIs((d.get("projects") or {}).get(cwd, {}).get(
                "hasTrustDialogAccepted"), True)


class TestDiscoverDedupe(unittest.TestCase):
    """同一 sid 被列两次（2026-11-22：claude agents --json 把「项目价值与前景」列成
    两个 interactive 进程）：板上两个一模一样的卡片、会话页两个圆点同时亮、
    滑到下一件就卡住，因为 OPEN 里两个相同 sid 永远「换」到同一页。"""

    def test_same_sid_kept_once_visible_first(self):
        srv = __import__("server")
        sid = "e1a9be21-f1c3-4142-b039-fa14a3e4a8bd"
        out = srv._dedupe_sessions([
            {"sid": sid, "pid": 19566, "tty": "", "window_id": None},
            {"sid": sid, "pid": 19576, "tty": "/dev/ttys004", "window_id": 42},
            {"sid": "other", "pid": 1, "tty": "", "window_id": None},
        ])
        self.assertEqual([x["sid"] for x in out], [sid, "other"])
        self.assertEqual(out[0]["pid"], 19576)      # 留看得见的那个


class TestDiscoverBackground(unittest.TestCase):
    """新版 Claude 把 --resume 的会话放进后台宿主，agents --json 里 kind=background、
    pid 没有 tty。要按命令行找到 `claude attach <sid8>` 客户端当会话 pid，
    否则唤起成功了 host 还说「没起来」（2026-11-11 macmini 连开三个窗口）。"""

    def test_attach_client_picked(self):
        srv = __import__("server")
        sid = "abcd1234-0000-4000-8000-000000000000"
        agents = json.dumps([{"kind": "background", "sessionId": sid, "pid": 999,
                              "cwd": "/nope", "status": "busy"}])
        with mock.patch.object(srv, "_run", return_value=("", agents, "")), \
                mock.patch.object(srv, "_window_map", return_value={}), \
                mock.patch.object(srv, "_ps_tree", return_value={111: (1, "s001")}), \
                mock.patch.object(srv, "_ps_cmd", return_value={
                    111: (1, "s001", "/usr/local/bin/claude attach abcd1234")}):
            out = srv._claude_discover()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sid"], sid)
        self.assertEqual(out[0]["pid"], 111)          # 不是后台宿主的 999
        self.assertEqual(out[0]["tty"], "/dev/s001")

    def test_background_without_attach_skipped(self):
        srv = __import__("server")
        sid = "abcd1234-0000-4000-8000-000000000000"
        agents = json.dumps([{"kind": "background", "sessionId": sid, "pid": 999,
                              "cwd": "/nope", "status": "busy"}])
        with mock.patch.object(srv, "_run", return_value=("", agents, "")), \
                mock.patch.object(srv, "_window_map", return_value={}), \
                mock.patch.object(srv, "_ps_tree", return_value={}), \
                mock.patch.object(srv, "_ps_cmd", return_value={}):
            self.assertEqual(srv._claude_discover(), [])


class TestWinHostSessions(unittest.TestCase):
    """Windows 的会话全由 host 自己 ConPTY 托管：要能当成电脑会话列出来、发消息、按键。
    新会话用 `claude --session-id <uuid>` 起，host sid 就等于转录 id（聊天能对上）。"""

    def _sess(self, srv, sid, cmd=("claude",), agent="claude"):
        s = srv.Session.__new__(srv.Session)     # 不起真 PTY
        s.sid, s.name, s.cwd, s.cmd = sid, "proj", "/tmp", list(cmd)
        s.agent, s.pid, s.alive, s.ghost = agent, 123, True, False
        s.last_out = 0
        s.writes = []
        s.write = lambda b: s.writes.append(b)
        return s

    def test_agent_of_cmd(self):
        srv = __import__("server")
        self.assertEqual(srv._agent_of_cmd([r"C:\bin\claude.cmd"]), "claude")
        self.assertEqual(srv._agent_of_cmd([r"C:\bin\codex.exe", "--x"]), "codex")
        self.assertIsNone(srv._agent_of_cmd(["/bin/zsh"]))
        self.assertIsNone(srv._agent_of_cmd([]))

    def test_host_discover_and_send(self):
        srv = __import__("server")
        sid = "abcd1234-0000-4000-8000-000000000000"
        s = self._sess(srv, sid)
        with mock.patch.object(srv, "IS_WIN", True), \
                mock.patch.object(srv, "SESSIONS", {sid: s}):
            out = srv._win_host_discover()
            self.assertEqual([x["sid"] for x in out], [sid])
            self.assertTrue(out[0]["host"])
            self.assertEqual(out[0]["agent"], "claude")
            ok, _ = srv.ext_send_text({"sid": sid, "host": True}, "你好")
            self.assertTrue(ok)
            ok, _ = srv.ext_send_special({"sid": sid, "host": True}, "up")
            self.assertTrue(ok)
        self.assertEqual(s.writes, ["你好\r".encode("utf-8"), b"\x1b[A"])

    def test_respawn_switches_session_id_to_resume(self):
        srv = __import__("server")
        sid = "abcd1234-0000-4000-8000-000000000000"
        self.assertEqual(srv._respawn_cmd(["claude", "--session-id", sid]),
                         ["claude", "--resume", sid])
        # 普通命令/已带 --resume 的保持原样
        self.assertEqual(srv._respawn_cmd(["claude", "--resume", sid]),
                         ["claude", "--resume", sid])
        self.assertEqual(srv._respawn_cmd(["zsh"]), ["zsh"])

    def test_plain_terminal_not_listed(self):
        srv = __import__("server")
        s = self._sess(srv, "abc12345", cmd=("zsh",), agent=None)
        with mock.patch.object(srv, "IS_WIN", True), \
                mock.patch.object(srv, "SESSIONS", {"abc12345": s}):
            self.assertEqual(srv._win_host_discover(), [])

    def test_ghost_not_listed(self):
        srv = __import__("server")
        s = self._sess(srv, "abcd1234-0000-4000-8000-000000000000")
        s.ghost = True
        with mock.patch.object(srv, "IS_WIN", True), \
                mock.patch.object(srv, "SESSIONS", {s.sid: s}):
            self.assertEqual(srv._win_host_discover(), [])

    def test_ext_write_raw(self):
        """电脑上的镜像窗口：原样写字节，不补回车。"""
        srv = __import__("server")
        sid = "abcd1234-0000-4000-8000-000000000000"
        s = self._sess(srv, sid)

        class Req:
            async def json(self):
                return {"sid": sid, "data": "abc\r"}

        async def _go():
            with mock.patch.object(srv, "require_token", mock.AsyncMock()), \
                    mock.patch.object(srv, "SESSIONS", {sid: s}):
                return await srv.h_ext_write(Req())

        self.assertEqual(asyncio.run(_go()).status, 200)
        self.assertEqual(s.writes, [b"abc\r"])


class TestPtyWinKeys(unittest.TestCase):
    """ptywin.py 把 Tk 按键翻译成终端字节。"""

    def test_key_bytes(self):
        import ptywin
        self.assertEqual(ptywin.key_bytes("Return"), "\r")
        self.assertEqual(ptywin.key_bytes("BackSpace"), "\x7f")
        self.assertEqual(ptywin.key_bytes("Up"), "\x1b[A")
        self.assertEqual(ptywin.key_bytes("c", "c", ctrl=True), "\x03")
        self.assertEqual(ptywin.key_bytes("a", "a"), "a")
        self.assertEqual(ptywin.key_bytes("Shift_L", ""), "")
        self.assertEqual(ptywin.key_bytes("Left", "", alt=True), "\x1b\x1b[D")


class TestRelayFallback(unittest.TestCase):
    """新域名连不上时临时切备用域名：只动内存，config.json 里永远是新的。"""

    def test_alt_switch(self):
        srv = __import__("server")
        old = srv._CFG.get("relay")
        old_on = srv._RELAY_ALT["on"]
        try:
            srv._CFG["relay"] = {"url": "https://relay.example.com",
                                 "device_id": "d", "device_secret": "s"}
            srv._RELAY_ALT["on"] = False
            self.assertEqual(srv._relay_cfg()["url"], "https://relay.example.com")
            srv._RELAY_ALT["on"] = True
            self.assertEqual(srv._relay_cfg()["url"], srv.RELAY_ALT_URL)
            self.assertEqual(srv._CFG["relay"]["url"], "https://relay.example.com")
        finally:
            srv._RELAY_ALT["on"] = old_on
            if old is None:
                srv._CFG.pop("relay", None)
            else:
                srv._CFG["relay"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
