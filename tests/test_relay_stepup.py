#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""管理员二次验证「一小时通行证」：验过一次后所有敏感操作都不再要码，删除/移除类除外。"""
import asyncio
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import relay  # noqa: E402


class Req:
    def __init__(self, token="", code=""):
        self.cookies = {"cb_adm": token} if token else {}
        self.headers = {"x-adm-code": code} if code else {}
        self.app = {"db": None}


class StepUpTest(unittest.TestCase):
    def setUp(self):
        relay._ADM_OK_UNTIL = 0.0
        relay._ADM_CODE_OK.update(code="", exp=0.0)

    tearDown = setUp

    def test_verified_pass_covers_every_caller(self):
        # 只有一个管理员：浏览器 / 手机壳 / Bearer 令牌都是同一个人，验一次全放行
        relay._adm_stepup_on()
        self.assertGreater(relay._ADM_OK_UNTIL, time.time() + 3500)
        self.assertTrue(asyncio.run(relay._adm_sensitive(Req("tok-abc"))))
        self.assertTrue(asyncio.run(relay._adm_sensitive(Req())))

    def test_expired_pass_needs_code(self):
        relay._ADM_OK_UNTIL = time.time() - 1
        self.assertFalse(asyncio.run(relay._adm_sensitive(Req())))
        self.assertFalse(asyncio.run(relay._adm_sensitive(Req("tok-abc"))))

    def test_hard_ops_ignore_pass(self):
        # 删除/移除类不吃一小时通行证
        relay._adm_stepup_on()
        self.assertFalse(asyncio.run(relay._adm_sensitive(Req("tok-abc"), hard=True)))

    def test_reused_code_also_grants_stepup(self):
        relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)
        self.assertTrue(asyncio.run(relay._adm_sensitive(Req("tok-abc", code="654321"))))
        self.assertGreater(relay._ADM_OK_UNTIL, time.time() + 3500)

    def test_hard_ops_still_take_reused_code(self):
        # 窗口内的同一个码（刚验过）删除类照样认，否则连着删会被短信频控卡死
        relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)
        self.assertTrue(asyncio.run(relay._adm_sensitive(Req(code="654321"), hard=True)))


if __name__ == "__main__":
    unittest.main()
