#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""已退出列表不能被一次性批量任务挤占。

3d-generator 这类流水线每跑一次就落一个 `codex exec` thread，几十个批量
rollout 的 mtime 比真人会话新得多——按「最近 20 个」截断时，桌面版/终端里
真正聊过的会话全被挤出去，手机上看就是「会话没了」。

跑法：cd codeboss && python3 -m unittest discover -s tests -v
"""
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

SID_CONV = "019a1cd0-7f00-7000-8000-000000000001"   # 桌面版里聊出来的
SID_B1 = "019a1cd0-7f00-7000-8000-000000000002"     # 批量：最新
SID_B2 = "019a1cd0-7f00-7000-8000-000000000003"
SID_B3 = "019a1cd0-7f00-7000-8000-000000000004"


def _write(p, originator):
    p.write_text(json.dumps({"type": "session_meta",
                             "payload": {"cwd": "/w", "originator": originator}},
                            ensure_ascii=False) + "\n", encoding="utf-8")


class ClosedSessionsOneshotTest(unittest.TestCase):
    def test_oneshot_exec_does_not_crowd_out_conversations(self):
        tmp = Path(tempfile.mkdtemp())
        p_conv = tmp / ("rollout-2026-11-14T22-00-00-%s.jsonl" % SID_CONV)
        p_b1 = tmp / ("rollout-2026-11-18T21-50-00-%s.jsonl" % SID_B1)
        p_b2 = tmp / ("rollout-2026-11-18T21-00-00-%s.jsonl" % SID_B2)
        p_b3 = tmp / ("rollout-2026-11-18T20-00-00-%s.jsonl" % SID_B3)
        _write(p_conv, "codex_work_desktop")
        for p in (p_b1, p_b2, p_b3):
            _write(p, "codex_exec")
        # 批量任务的 mtime 都更新（closed_sessions 按 mtime 倒序）
        base = 1795000000
        for i, p in enumerate((p_b1, p_b2, p_b3, p_conv)):
            os.utime(p, (base - i * 60, base - i * 60))

        ad = agent_codex.Adapter()
        with mock.patch.object(agent_codex, "_rollout_files",
                               return_value=[p_b1, p_b2, p_b3, p_conv]), \
             mock.patch.object(agent_codex, "_archived_ids", return_value=set()):
            rows = ad.closed_sessions(set(), limit=2)

        sids = [x["sid"] for x in rows]
        # 真人会话（哪怕最旧）必须留下；剩下一个名额给最新的批量任务
        self.assertIn(SID_CONV, sids, sids)
        self.assertEqual(sids[1], SID_B1, sids)
        self.assertEqual(len(sids), 2, sids)


if __name__ == "__main__":
    unittest.main()
