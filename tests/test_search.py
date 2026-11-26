# SPDX-License-Identifier: AGPL-3.0-or-later

"""全文检索（CodeBoss 待办）：在手机上一句话找回「哪个会话里说过/改过这个」。

转录是 jsonl，直接搜原始行会把 `{"type":"user","message":...` 一大堆噪音怼给用户，
所以命中后从消息正文里取片段；正文里没有（比如只在 tool_use 的 input 里）时
才退回清理过的原始片段。候选=各 agent 适配器能列出的会话转录，按最近活跃排，
带文件数/字节预算（手机上一搜就要有回音）。

跑法： .venv/bin/python -m pytest tests/test_search.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _line(role, text):
    return json.dumps({"type": role, "message": {"role": role,
                                                 "content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def _tool(path):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]}},
        ensure_ascii=False) + "\n"


def _tr(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("".join(lines), encoding="utf-8")
    return p


def _files(*items):
    return [{"sid": s, "path": p, "title": s, "agent": "claude", "last": t}
            for s, p, t in items]


def test_finds_in_user_text_with_clean_snippet(tmp_path):
    p = _tr(tmp_path, "a.jsonl", [_line("user", "把登录页改成蓝色"),
                                  _line("assistant", "好的，先看当前样式")])
    out = server._search_files(_files(("S1", p, 100)), "登录页")
    assert [x["sid"] for x in out] == ["S1"]
    sn = out[0]["snips"][0]
    assert "登录页改成蓝色" in sn
    assert '"type"' not in sn, "片段里不许有 json 噪音"


def test_tool_input_is_searchable(tmp_path):
    p = _tr(tmp_path, "a.jsonl", [_line("user", "改一下"),
                                  _tool("/p/src/login.css")])
    out = server._search_files(_files(("S1", p, 100)), "login.css")
    assert out and "login.css" in out[0]["snips"][0]


def test_no_match_no_row(tmp_path):
    p = _tr(tmp_path, "a.jsonl", [_line("user", "别的东西")])
    assert server._search_files(_files(("S1", p, 100)), "登录页") == []


def test_ascii_case_insensitive(tmp_path):
    p = _tr(tmp_path, "a.jsonl", [_line("user", "deploy the Build")])
    assert server._search_files(_files(("S1", p, 100)), "build")


def test_newest_first_and_capped(tmp_path):
    a = _tr(tmp_path, "a.jsonl", [_line("user", "部署 A")])
    b = _tr(tmp_path, "b.jsonl", [_line("user", "部署 B")])
    c = _tr(tmp_path, "c.jsonl", [_line("user", "部署 C")])
    out = server._search_files(_files(("S1", a, 100), ("S2", b, 300), ("S3", c, 200)),
                               "部署", limit=2)
    assert [x["sid"] for x in out] == ["S2", "S3"], "最近活跃的在前，超过上限截断"


def test_snippet_window_is_short(tmp_path):
    long = "前" * 300 + "关键词" + "后" * 300
    p = _tr(tmp_path, "a.jsonl", [_line("user", long)])
    sn = server._search_files(_files(("S1", p, 1)), "关键词")[0]["snips"][0]
    assert len(sn) <= 140 and "关键词" in sn


def test_handler_returns_items(monkeypatch):
    monkeypatch.setattr(server, "_search_all",
                        lambda q, limit=30: [{"sid": "S1", "title": "t", "snips": [q]}])
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        from aiohttp.test_utils import make_mocked_request
        r = asyncio.run(server.h_search(make_mocked_request("GET", "/api/search?q=%E9%83%A8%E7%BD%B2")))
        d = json.loads(r.text)
    finally:
        server.require_token = old
    assert d["ok"] and d["q"] == "部署" and d["items"][0]["sid"] == "S1"


def test_ui_gate():
    """工作台「找会话」接线（node 门禁；没 node 就跳）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_search_ui.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]


def test_handler_needs_query():
    old = server.require_token

    async def _t(request):
        pass
    server.require_token = _t
    try:
        from aiohttp.test_utils import make_mocked_request
        r = asyncio.run(server.h_search(make_mocked_request("GET", "/api/search?q=")))
    finally:
        server.require_token = old
    assert r.status == 400
