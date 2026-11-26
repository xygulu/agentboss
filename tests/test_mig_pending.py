#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""转全彩「先杀后开」的待重开标记：杀之前落一条、开起来清掉、开机补一次。
跑法：cd codeboss && .venv/bin/python -m unittest tests.test_mig_pending -v"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402


class MigPendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = server._MIG_PENDING
        server._MIG_PENDING = Path(self.tmp.name) / "migrate_pending.json"

    def tearDown(self):
        server._MIG_PENDING = self.orig
        self.tmp.cleanup()

    def test_set_and_clear(self):
        server._mig_pending("aaaa1111", "/work/x", True)
        server._mig_pending("bbbb2222", "/work/y", True)
        self.assertEqual(set(json.loads(server._MIG_PENDING.read_text())),
                         {"aaaa1111", "bbbb2222"})
        server._mig_pending("aaaa1111", "", False)
        self.assertEqual(list(json.loads(server._MIG_PENDING.read_text())), ["bbbb2222"])

    def test_retry_skips_live_and_clears(self):
        server._mig_pending("dead0001", "/work/z", True)
        calls = []
        orig = (server.discover, server._reopen_in_terminal)
        server.discover = lambda force=False: []
        server._reopen_in_terminal = lambda cwd, sid, wait=0: (calls.append(sid), (True, ""))[1]
        try:
            self.assertEqual(server._mig_pending_retry(), ["dead0001"])
            # 已经活着的、超过 24 小时的不再重开
            server._mig_pending("live0001", "/w", True)
            server.discover = lambda force=False: [{"sid": "live0001"}]
            calls.clear()
            server._mig_pending_retry()
            self.assertEqual(calls, [])
            self.assertEqual(json.loads(server._MIG_PENDING.read_text()), {})
        finally:
            server.discover, server._reopen_in_terminal = orig


if __name__ == "__main__":
    unittest.main()
