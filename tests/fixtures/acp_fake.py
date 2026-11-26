#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ACP 假服务器（测试夹具）：钉住 stdio NDJSON 的线上契约。

initialize        → {"protocolVersion":"1","agentCapabilities":{"loadSession":False}}
session/new       → {"sessionId":"s1"}
session/prompt    → 先发两条 session/update（agent_message_chunk、tool_call）
                    再回 {"stopReason":"end_turn"}
ACPM_FAKE_DIE=1   → 收到 session/prompt 即 os._exit(1)（崩溃测试：不回结果、
                    不 flush，模拟 agent 进程半路死掉）
ACPM_FAKE_ASK=1   → prompt 先发一个 agent→client 请求（session/request_permission），
                    读我方回帧：回了 error → chunk「ASK_HANDLED_OK」；静默丢弃 →
                    「ASK_DROPPED」（真适配器等权限答复，丢了就挂死整回合）
未知 method       → JSON-RPC error -32601（错误路径测试用）
"""
import json
import os
import sys


def send(frame):
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    while True:
        line = sys.stdin.readline()          # 行缓冲：一帧一行，别用 for 迭代（有预读）
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
        except ValueError:
            continue                          # 坏行跳过，不连坐
        m = f.get("method") or ""
        rid = f.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "1",
                "agentCapabilities": {"loadSession": False}}})
        elif m == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s1"}})
        elif m == "session/prompt":
            if os.environ.get("ACPM_FAKE_DIE") == "1":
                sys.stdout.flush()
                os._exit(1)
            if os.environ.get("ACPM_FAKE_ASK") == "1":
                send({"jsonrpc": "2.0", "id": 501,
                      "method": "session/request_permission",
                      "params": {"sessionId": "s1", "options": []}})
                reply = sys.stdin.readline()
                handled = False
                try:
                    handled = "error" in json.loads(reply)
                except Exception:
                    pass
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text",
                                           "text": "ASK_HANDLED_OK" if handled
                                                   else "ASK_DROPPED"}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s1",
                "update": {"sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "你好"}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s1",
                "update": {"sessionUpdate": "tool_call",
                            "toolCallId": "t1", "title": "ls", "kind": "execute"}}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "Method not found: %s" % m}})


if __name__ == "__main__":
    main()
