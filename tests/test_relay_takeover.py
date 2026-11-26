#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""开发机接管确认：机器已在别人名下时，绝不靠配对码静默改归属。

规则（用户口径 2026-11-14）：
- 原账号手机 App 或机器本机确认后才放行；10 分钟不确认自动失效。
- 确认后开发机直接移到新账号（旧账号列表里消失，机器上的会话/历史不动）。
- 旧客户端传 takeover=1 也不再是「一按就换」。
"""
import os
import sqlite3
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import relay  # noqa: E402


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.db = relay.db_init(":memory:")
        for uid, name in ((1, "old"), (2, "new"), (3, "third")):
            self.db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan)"
                            " VALUES(?,?, 'x','y',?, 'free')", (uid, name, time.time()))
        # 机器 dev1 归账号 1；dev9 是账号 1 的另一台（原账号手机经它代理）
        for dev, owner in (("dev1", 1), ("dev9", 1)):
            self.db.execute("INSERT INTO devices(id,name,secret,owner,created)"
                            " VALUES(?,?, 's', ?, ?)", (dev, dev, owner, time.time()))
        self.code = self._code("dev1", 900)
        self.db.commit()

    def _code(self, dev, ttl=900):
        c = "c-" + str(time.time_ns())
        self.db.execute("INSERT INTO pair_codes(code,device_id,created,expires,used)"
                        " VALUES(?,?,?,?,0)", (c, dev, time.time(), time.time() + ttl))
        self.db.commit()
        return c

    def _owner(self, dev):
        return self.db.execute("SELECT owner FROM devices WHERE id=?", (dev,)).fetchone()[0]

    def _status(self, rid):
        return self.db.execute("SELECT status FROM takeover_req WHERE id=?", (rid,)).fetchone()[0]

    def test_scan_does_not_silently_take_over(self):
        r, rid = relay._do_pair(self.db, 2, self.code, True)   # 老客户端传 takeover=1 也不行
        self.assertEqual(r, "need")
        self.assertIsNotNone(rid)
        self.assertEqual(self._owner("dev1"), 1)
        self.assertEqual(self._status(rid), "pending")

    def test_owner_device_approves_moves_machine(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        self.assertEqual(r, "need")
        res, _ = relay._tk_apply(self.db, rid, True, "dev9", 1)
        self.assertEqual(res, "ok")
        self.assertEqual(self._owner("dev1"), 2)
        self.assertEqual(self._status(rid), "approved")
        used = self.db.execute("SELECT used FROM pair_codes WHERE code=?", (self.code,)).fetchone()[0]
        self.assertEqual(used, 1)

    def test_machine_itself_approves(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        res, _ = relay._tk_apply(self.db, rid, True, "dev1", 1)   # 目标机器自证
        self.assertEqual(res, "ok")
        self.assertEqual(self._owner("dev1"), 2)

    def test_deny_keeps_owner(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        res, _ = relay._tk_apply(self.db, rid, False, "dev9", 1)
        self.assertEqual(res, "ok")
        self.assertEqual(self._status(rid), "denied")
        self.assertEqual(self._owner("dev1"), 1)

    def test_third_party_cannot_act(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        res, _ = relay._tk_apply(self.db, rid, True, "dev-x", 3)
        self.assertEqual(res, "forbidden")
        self.assertEqual(self._owner("dev1"), 1)
        self.assertEqual(self._status(rid), "pending")

    def test_expired_code_at_approval(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        self.db.execute("UPDATE pair_codes SET expires=? WHERE code=?",
                        (time.time() - 1, self.code))
        self.db.commit()
        res, _ = relay._tk_apply(self.db, rid, True, "dev9", 1)
        self.assertEqual(res, "expired")
        self.assertEqual(self._owner("dev1"), 1)

    def test_request_times_out(self):
        r, rid = relay._do_pair(self.db, 2, self.code)
        self.db.execute("UPDATE takeover_req SET expires=? WHERE id=?",
                        (time.time() - 1, rid))
        self.db.commit()
        res, _ = relay._tk_apply(self.db, rid, True, "dev9", 1)
        self.assertEqual(res, "bad")
        self.assertEqual(self._status(rid), "expired")

    def test_stale_request_after_owner_changed(self):
        # 排队期间机器已经被接走：旧账号不能再拿老请求搬第二次
        r, rid = relay._do_pair(self.db, 2, self.code)
        self.db.execute("UPDATE devices SET owner=2 WHERE id='dev1'")
        self.db.commit()
        res, _ = relay._tk_apply(self.db, rid, True, "dev9", 1)
        self.assertEqual(res, "expired")
        self.assertEqual(self._owner("dev1"), 2)
        self.assertEqual(self._status(rid), "expired")

    def test_approval_clears_other_pending(self):
        _, rid1 = relay._do_pair(self.db, 2, self.code)
        self.db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan)"
                        " VALUES(4,'new2','x','y',?,'free')", (time.time(),))
        self.db.commit()
        _, rid2 = relay._do_pair(self.db, 4, self.code)
        self.assertNotEqual(rid1, rid2)
        res, _ = relay._tk_apply(self.db, rid1, True, "dev9", 1)
        self.assertEqual(res, "ok")
        self.assertEqual(self._status(rid2), "expired")

    def test_same_owner_rescan_is_idempotent(self):
        r, _ = relay._do_pair(self.db, 1, self._code("dev1"))
        self.assertEqual(r, "ok")
        self.assertEqual(self._owner("dev1"), 1)

    def test_new_account_quota_checked_at_approval(self):
        relay.MAX_DEVICES = 0
        try:
            r, rid = relay._do_pair(self.db, 2, self.code)
            self.assertEqual(r, "need")
            self.db.execute("UPDATE usage SET n=n")   # no-op：确保没有别的额度缓存
            old = relay.plans
            relay.plans = lambda: {"free": {"name": "免费", "limits": {"hosts": 1}}}
            try:
                self.db.execute("INSERT INTO devices(id,name,secret,owner,created)"
                                " VALUES('dev2','dev2','s',2,?)", (time.time(),))
                self.db.commit()
                res, _ = relay._tk_apply(self.db, rid, True, "dev9", 1)
                self.assertEqual(res, "quota")
                self.assertEqual(self._owner("dev1"), 1)
            finally:
                relay.plans = old
        finally:
            relay.MAX_DEVICES = int(os.environ.get("RELAY_MAX_DEVICES") or 0)


if __name__ == "__main__":
    unittest.main()
