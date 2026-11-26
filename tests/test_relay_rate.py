#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""管理员短信限额：打满后 429 要说「等多久」，别只写「稍后再试」。"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import relay  # noqa: E402


class RateWaitTest(unittest.TestCase):
    def setUp(self):
        relay._RL.clear()

    tearDown = setUp

    def test_zero_when_slot_available(self):
        self.assertEqual(relay.rate_wait("k", "a", 3), 0)
        self.assertTrue(relay.rate_ok("k", "a", 3))
        self.assertEqual(relay.rate_wait("k", "a", 3), 0)

    def test_wait_until_oldest_frees_a_slot(self):
        now = time.time()
        # 3 条占满，最早一条还要 100 秒才过期
        relay._RL[("k", "a")] = [now - 3500, now - 100, now - 50]
        w = relay.rate_wait("k", "a", 3)
        self.assertGreater(w, 90)
        self.assertLessEqual(w, 101)
        # 放掉一条后立刻可用
        relay._RL[("k", "a")] = [now - 100, now - 50]
        self.assertEqual(relay.rate_wait("k", "a", 3), 0)

    def test_expired_entries_ignored(self):
        relay._RL[("k", "a")] = [time.time() - 3700] * 5
        self.assertEqual(relay.rate_wait("k", "a", 1), 0)
        self.assertTrue(relay.rate_ok("k", "a", 1))


if __name__ == "__main__":
    unittest.main()
