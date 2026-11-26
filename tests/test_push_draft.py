# SPDX-License-Identifier: AGPL-3.0-or-later

"""推送的草稿帧：这一轮结束了，手机端必须能把「正在思考」撤掉。

真机出过的问题（2026-11-14）：codex 桌面版那一轮 19:28 就跑完了，手机端到 20:20
还挂着「正在思考 · 65 分钟」。根因是 host 只在**这一轮还有草稿**时才发 draft 帧，
断线重连后（手机端每 50 秒重连一次）恰好赶在草稿撤掉之后，就再也收不到「这轮完了」
那一帧——手机端自己走的表就一直加下去。

这里钉两件事：
  ① 没有草稿时 _draft_frame 也给一帧 done（host 侧）
  ② 连接建立时**一定**把这帧发出去，不许被「有没有草稿」的条件挡住（host 侧接线）
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


def test_no_draft_still_yields_done_frame():
    server._DRAFTS.pop("sid-none", None)
    sig, ev = server._draft_frame("sid-none")
    assert sig is None, "没有草稿就没有变更签名（下一帧也不用重复发）"
    assert ev["done"] is True, "没有草稿＝这轮早就结束了，要给一帧 done"
    assert ev["text"] == "" and ev["st"] == "" and ev["el"] == 0


def test_live_draft_is_not_done():
    server._DRAFTS["sid-live"] = {"text": "正在写…", "done": False, "err": "",
                                   "at": 0, "waiting": False, "st": "正在思考",
                                   "t0": 1000.0}
    try:
        sig, ev = server._draft_frame("sid-live", now=1000.0)
        assert ev["done"] is False
        assert ev["st"] == "正在思考"
        assert ev["text"] == "正在写…"
        assert ev["el"] == 0
        assert sig == (4, False, "正在思考")
    finally:
        server._DRAFTS.pop("sid-live", None)


def test_finished_draft_is_done_and_empty():
    server._DRAFTS["sid-fin"] = {"text": "答完了", "done": True, "err": "",
                                 "at": 0, "waiting": False, "st": "", "t0": 1000.0}
    try:
        sig, ev = server._draft_frame("sid-fin", now=1042.0)
        assert ev["done"] is True
        assert ev["st"] == ""
        assert ev["el"] == 42
    finally:
        server._DRAFTS.pop("sid-fin", None)


def test_push_connect_always_sends_draft_frame():
    """连接建立时必须无条件发一帧 draft（哪怕 _DRAFTS 是空的）。"""
    src = (HERE / "host" / "server.py").read_text(encoding="utf-8")
    at = src.index("async def h_ext_push(")
    body = src[at:at + 8000]
    assert 'await send("draft", _dev)' in body, "连接建立时没发 draft 帧"
    # 不能再有「有草稿才发」的挡板：那句一回来，手机端又会永远挂着「正在思考」
    assert not re.search(r"if\s+_?dr\w*\s+is\s+not\s+None\s*:\s*\n\s*await send\(\"draft\"", body), \
        "draft 帧又被『有没有草稿』挡住了"
