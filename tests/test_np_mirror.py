# SPDX-License-Identifier: AGPL-3.0-or-later

"""中继那份提醒状态机：一次权限请求只报一条。

PermissionRequest 之后约 6 秒，Claude Code 还会补一条自己的
Notification(permission_prompt)——"Claude needs your permission"。
4 秒的 perm_throttle 拦不住，手机上就是两条待确认（2026-11-19 用户实报）。
中继这份是权威（host 本地兜底同语义），两边都得吞掉镜像那条。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay

POL = {"max_text": 160, "perm_throttle": 4, "done_text": "{msg}",
       "perm_text": "{tool} 需要确认：{brief}", "ask_text": "{msg}",
       "limit_text": "{msg}", "idle_after": 0,
       "kinds": {"perm": True, "done": True, "ask": True, "limit": True}}


def test_mirror_after_permission_request_is_swallowed():
    st, t = {}, 1_000_000.0
    out = relay._np_eval(st, {"hook_event_name": "PermissionRequest",
                              "tool_name": "Bash",
                              "tool_input": {"command": "rm -rf build"}}, POL, t)
    assert len(out) == 1 and out[0]["kind"] == "perm"
    out = relay._np_eval(st, {"hook_event_name": "Notification",
                              "notification_type": "permission_prompt",
                              "message": "Claude needs your permission"}, POL, t + 6)
    assert out == [], "同一件事不许再报一遍"


def test_notification_only_still_notifies():
    out = relay._np_eval({}, {"hook_event_name": "Notification",
                              "notification_type": "permission_prompt",
                              "message": "Claude needs your permission"}, POL, 1_000_000.0)
    assert len(out) == 1, "只发 Notification 的客户端照常提醒"
