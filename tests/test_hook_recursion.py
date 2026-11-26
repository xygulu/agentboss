#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""notify 链回路：别的工具（Sky Computer Use 的 turn-ended）会把自己的 previous notify
指回 hook.sh，转发过去又被它调回来——一次「完成」变成无限循环，手机每 10 秒重复播报。
转发时打 NEST=1 记号，绕回来的那次直接退。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402


class HookRecursionTest(unittest.TestCase):
    def _gen(self):
        p = Path(tempfile.mkdtemp()) / "hook.sh"
        hk = server.hooks        # 生成脚本的函数搬到了 hooks.py：要打桩的是它自己的模块名
        with mock.patch.object(hk, "HOOK_SH", p), \
             mock.patch.object(hk, "IS_WIN", False):
            server._write_hook_sh()
        return p

    def test_chain_marked_and_nested_run_sends_nothing(self):
        p = self._gen()
        self.assertIn('CODEBOSS_HOOK_NEST=1 "$CHAIN"', p.read_text(encoding="utf-8"))
        env = dict(os.environ, CODEBOSS_HOOK_NEST="1")
        r = subprocess.run(["/bin/sh", "-x", str(p), "codex",
                            '{"type":"agent-turn-complete","thread-id":"t"}'],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("curl", r.stderr, "绕回来的那次不该再上报/转发")

    def test_identical_content_keeps_mtime(self):
        """hermes hooks doctor 查「审批后脚本被改过」（mtime 漂移）。host 每次启动
        都调 _write_hook_sh：内容没变就别重写，不然用户每次重启 host 都要在 hermes
        里重新同意一次。"""
        p = self._gen()
        st1 = p.stat().st_mtime_ns
        hk = server.hooks
        with mock.patch.object(hk, "HOOK_SH", p), \
             mock.patch.object(hk, "IS_WIN", False):
            server._write_hook_sh()
        self.assertEqual(p.stat().st_mtime_ns, st1, "内容没变不该重写（mtime 漂移）")


if __name__ == "__main__":
    unittest.main()
