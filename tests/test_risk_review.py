# SPDX-License-Identifier: AGPL-3.0-or-later

"""审批风险 agent 二审（窗口核心 Phase 2 Task 5）：不确定区才问 agent、
8s 上限、降级留痕、缓存。跑法： .venv/bin/python -m pytest tests/test_risk_review.py -q
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import risk_review


class _Spy:
    """executor 桩：记录调用次数，按脚本作答。"""

    def __init__(self, results=None, raise_exc=None):
        self.calls = 0
        self.prompts = []
        self.results = results or {}
        self.raise_exc = raise_exc

    def __call__(self, prompt, timeout=risk_review.TIMEOUT_S):
        self.calls += 1
        self.prompts.append(prompt)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.results


# ---- (a) regex 有结论就直通，不问 agent ----

def test_high_regex_passthrough_no_agent():
    spy = _Spy()
    out = risk_review.grade("Bash", "sudo rm -rf /tmp/x", {"command": "sudo rm -rf /tmp/x"},
                            [{"level": "high", "text": "以管理员权限执行"}], executor=spy)
    assert out["level"] == "high" and out["src"] == "regex"
    assert out["cached"] is False
    assert spy.calls == 0, "regex=high 不许发 agent 请求"


def test_medium_regex_passthrough_no_agent():
    spy = _Spy()
    out = risk_review.grade("Bash", "scp f a@b:/tmp", {"command": "scp f a@b:/tmp"},
                            [{"level": "medium", "text": "把数据发往外部"}], executor=spy)
    assert out["level"] == "medium" and out["src"] == "regex" and spy.calls == 0


def test_low_readonly_tool_no_agent():
    spy = _Spy()
    out = risk_review.grade("Read", "/tmp/x.py", {"file_path": "/tmp/x.py"}, [], executor=spy)
    assert out["level"] == "low" and out["src"] == "regex" and spy.calls == 0


# ---- (b) 不确定区问 agent：run 动作 + regex 没拦 → agent 说了算 ----

def test_uncertain_zone_runs_agent():
    spy = _Spy(results={"level": "high", "why": "发布到线上环境，难回滚"})
    out = risk_review.grade("Bash", "npm run deploy", {"command": "npm run deploy"},
                            [], executor=spy)
    assert spy.calls == 1
    assert out["level"] == "high" and out["src"] == "agent"
    assert "deploy" in spy.prompts[0] or "npm" in spy.prompts[0], "prompt 里要有这次的事实"
    assert out["why"]


def test_unknown_tool_is_uncertain():
    spy = _Spy(results={"level": "medium", "why": "少见工具"})
    out = risk_review.grade("SomeMcpTool", "do thing", {"x": 1}, [], executor=spy)
    assert spy.calls == 1 and out["src"] == "agent" and out["level"] == "medium"


def test_card_high_suppresses_quick_allow(monkeypatch):
    """agent 评 high：卡面撤掉快捷放行（verified 不认），并写明评级文案。"""
    import server
    monkeypatch.setattr(server.risk_review, "grade",
                        lambda *a, **k: {"level": "high", "why": "动线上", "src": "agent",
                                         "cached": False, "trace": ""})
    snap = {"opts": [1], "text": {1: "1. Yes, allow"},
            "rows": [], }
    card = server._decision_card({"tool": "Bash", "ti": {"command": "npm run deploy"},
                                  "desc": "npm run deploy"}, snap=snap)
    assert not card.get("verified"), "high 时 verified 必须为假（撤一键放行）"
    note = json.dumps(card, ensure_ascii=False)
    assert "agent 评级：高" in note, "卡上要写明 agent 评级"


# ---- (c) agent 挂了/超时：回退正则结论，降级可见 ----

def test_agent_down_falls_back():
    spy = _Spy(raise_exc=RuntimeError("codex not found"))
    out = risk_review.grade("Bash", "npm run build", {"command": "npm run build"},
                            [], executor=spy)
    assert out["level"] == "low" and out["src"] == "down"
    assert out["trace"] == "agent-down"


def test_agent_timeout_falls_back():
    spy = _Spy(raise_exc=subprocess.TimeoutExpired(cmd="codex", timeout=8))
    out = risk_review.grade("Bash", "npm test", {"command": "npm test"}, [], executor=spy)
    assert out["level"] == "low" and out["src"] == "timeout"
    assert out["trace"] == "agent-timeout"


# ---- (d) 缓存：同 (tool, target 归一化, cwd) 只问一次 ----

def test_cache_hit_second_call():
    spy = _Spy(results={"level": "medium", "why": "x"})
    a = risk_review.grade("Bash", "ls  -la   /tmp", {}, [], cwd="/p", executor=spy)
    b = risk_review.grade("Bash", "ls -la /tmp", {}, [], cwd="/p", executor=spy)
    assert spy.calls == 1, "第二次要命中缓存"
    assert a["cached"] is False and b["cached"] is True
    assert b["level"] == a["level"]
    risk_review.cache_clear()


# ---- (e) 审计落盘：每条判定（含缓存命中）带 risk_review 键 ----

def test_audit_row_written(tmp_path):
    audit = tmp_path / "perm_audit.jsonl"
    spy = _Spy(results={"level": "high", "why": "删库"})
    risk_review.grade("Bash", "drop table users", {}, [], cwd="/p",
                      executor=spy, audit_path=audit)
    risk_review.grade("Bash", "drop table users", {}, [], cwd="/p",
                      executor=spy, audit_path=audit)
    rows = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 2, "两次判定（含缓存命中）都要留痕"
    for r in rows:
        assert "risk_review" in r and r["risk_review"]["level"] == "high"
        assert r["tool"] == "Bash"
    risk_review.cache_clear()


# ---- 默认 executor：不注入时走 codex 一次性（只验形状，不真跑） ----

def test_default_executor_missing_codex_is_down(monkeypatch):
    monkeypatch.setattr(risk_review, "_codex_bin", lambda: "")
    out = risk_review.grade("Bash", "npm run whatever", {}, [],
                            cwd="/q", executor=None)
    assert out["src"] == "down" and out["level"] == "low"
    risk_review.cache_clear()
