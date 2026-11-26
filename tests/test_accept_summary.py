# SPDX-License-Identifier: AGPL-3.0-or-later

"""验收摘要（2026-11-19 计划 A5）：点「通过」前先看清这轮干了什么。

- 材质 = 最近三轮 assistant 原文 + 最近验证红绿；
- 模型不行就回落最近三条原文拼接（不许空手）；
- 不受语音开关约束：notify.accept_summary 不查 _voice_ok。

跑法：.venv/bin/python -m pytest tests/test_accept_summary.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notify
import server


@pytest.fixture
def sess(tmp_path, monkeypatch):
    async def _fs(sid):
        return {"cwd": str(tmp_path), "agent": "claude"} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)

    class _Ad:
        def transcript_path(self, cwd, sid):
            return tmp_path / "t.jsonl"
    monkeypatch.setattr(server.agents, "get", lambda k: _Ad())
    msgs = [{"role": "user", "text": "把导出改成 PDF"},
            {"role": "assistant", "text": "已改完导出格式，跑了一遍样例。"},
            {"role": "assistant", "text": "顺手修了目录名，测试全绿。"}]
    monkeypatch.setattr(server, "_tr_load", lambda p, limit=400: msgs)
    return msgs


def test_material_includes_rounds_and_cards(sess, monkeypatch):
    seen = {}

    async def fake(sid, material, fallback=""):
        seen["sid"], seen["material"], seen["fallback"] = sid, material, fallback
        return {"text": "改完导出。测试全绿。没遗留。", "src": "llm"}
    monkeypatch.setattr(notify, "accept_summary", fake)
    monkeypatch.setattr(server, "_test_cards_payload",
                        lambda sid, limit=5: [{"cmd": "pytest -q", "state": "pass"}])
    r = asyncio.run(server._accept_summary_payload("s1"))
    assert r["ok"] and r["text"].startswith("改完导出") and r["src"] == "llm"
    assert "把导出改成 PDF" not in seen["material"], "老板指令不该混进 assistant 汇总（原文里没有）"
    assert "已改完导出格式" in seen["material"] and "测试全绿" in seen["material"]
    assert "pytest -q → 通过" in seen["material"], "最近验证红绿没进材质"
    assert "已改完导出格式" in seen["fallback"], "回落串要带原文"


def test_fallback_when_llm_empty(sess, monkeypatch):
    async def fake(sid, material, fallback=""):
        return {"text": fallback, "src": "fallback"}
    monkeypatch.setattr(notify, "accept_summary", fake)
    monkeypatch.setattr(server, "_test_cards_payload", lambda sid, limit=5: [])
    r = asyncio.run(server._accept_summary_payload("s1"))
    assert r["ok"] and r["src"] == "fallback" and "已改完导出格式" in r["text"]


def test_missing_session():
    r = asyncio.run(server._accept_summary_payload("nope"))
    assert not r["ok"]


def test_notify_falls_back_without_relay(monkeypatch):
    monkeypatch.setattr(notify, "_relay_cfg", lambda: {})
    r = asyncio.run(notify.accept_summary("s1", "材料", "最近三条原文"))
    assert r == {"text": "最近三条原文", "src": "fallback"}


def test_notify_does_not_gate_on_voice(monkeypatch):
    """语音播报关着也要出摘要（验收是办正事）——只认模型返回。"""
    monkeypatch.setattr(notify, "_voice_ok", lambda: False)

    async def llm(prompt, text, purpose, timeout=20):
        assert purpose == "accept_summary" and "验收" in prompt
        return "  三句话摘要  "
    monkeypatch.setattr(notify, "_llm_text", llm)
    r = asyncio.run(notify.accept_summary("s1", "材料", "兜底"))
    assert r == {"text": "三句话摘要", "src": "llm"}
