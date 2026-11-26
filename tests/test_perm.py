#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""权限请求号与决策回传（P2）：归一化字段、节流、幂等、审计落盘。
跑法：cd codeboss && .venv/bin/python -m unittest tests.test_perm -v"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402

SID = "sess-perm"


class PermTest(unittest.TestCase):
    def setUp(self):
        server._PERM_DONE.clear()
        server._PERM_META.clear()
        server._EV.pop(SID, None)

    def _req(self):
        return server._ev_local({"hook_event_name": "PermissionRequest",
                                 "session_id": SID,
                                 "tool_name": "Bash",
                                 "tool_input": {"command": "rm -rf build"}})

    def test_normalized(self):
        items = self._req()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["request_id"].startswith("pm_"))
        self.assertEqual(it["tool"], "Bash")
        self.assertIn("rm -rf build", it["description"])
        self.assertEqual([o["id"] for o in it["options"]], ["allow", "deny"])
        self.assertIsNone(it["expires_at"])
        self.assertEqual(it["ingress"], "hook")
        self.assertIn((SID, it["request_id"]), server._PERM_META)

    def test_throttled_batch(self):
        self.assertEqual(len(self._req()), 1)
        self.assertEqual(self._req(), [])          # 同一批工具调用只报一次

    def test_answer_idempotent(self):
        it = self._req()[0]
        p = os.path.join(tempfile.mkdtemp(), "perm_audit.jsonl")
        a = server._perm_answer_core(SID, it["request_id"], "2", "Yes", path=p)
        b = server._perm_answer_core(SID, it["request_id"], "2", "Yes", path=p)
        self.assertEqual(a, {"ok": True, "via": "keys"})
        self.assertTrue(b.get("dup"))
        with open(p, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(lines), 1)            # 重复上报只落一条
        self.assertEqual(lines[0]["request_id"], it["request_id"])
        self.assertEqual(lines[0]["tool"], "Bash")
        # 同一个请求、另一个选项是另一个决策，照记
        c = server._perm_answer_core(SID, it["request_id"], "1", "No", path=p)
        self.assertFalse(c.get("dup"))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(len([x for x in f if x.strip()]), 2)

    def _notif_perm(self, msg="Claude needs your permission"):
        return server._ev_local({"hook_event_name": "Notification",
                                 "session_id": SID,
                                 "notification_type": "permission_prompt",
                                 "message": msg})

    def test_notification_mirror_not_duplicated(self):
        """一次权限请求来两条 hook：PermissionRequest（带 rid/选项）+ 6 秒后
        Claude Code 自己的 Notification(permission_prompt)。手机被问两遍
        （2026-11-19 用户实报），镜像那条要吞掉。"""
        self.assertEqual(len(self._req()), 1)
        server._EV[SID]["perm"] -= 6                 # 6 秒后才到那条镜像
        self.assertEqual(self._notif_perm(), [], "同一件事不许再报一遍")

    def test_notification_only_client_still_notifies(self):
        """没收到过 PermissionRequest 的（旧版客户端只发 Notification）：照常提醒。"""
        self.assertEqual(len(self._notif_perm()), 1)

    def test_notification_after_new_turn_notifies(self):
        """用户已经回话（新的一轮）：镜像事件重新算数，不能一直哑着。"""
        self.assertEqual(len(self._req()), 1)
        server._ev_local({"hook_event_name": "UserPromptSubmit",
                          "session_id": SID, "prompt": "继续"})
        server._EV[SID]["perm"] -= 10          # 节流窗口过去了才轮到它说话
        out = self._notif_perm()
        self.assertEqual(len(out), 1)

    def test_missing_args(self):
        self.assertFalse(server._perm_answer_core(SID, "", "1")["ok"])
        self.assertFalse(server._perm_answer_core(SID, "pm_x", "")["ok"])


if __name__ == "__main__":
    unittest.main()
