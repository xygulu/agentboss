# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""审批风险 agent 二审（窗口核心 Phase 2）。

正则初筛（server._scan_risk）只认「真出过事」的形状；**不确定区**
（regex 没拦但动作类是 run/write/web，或工具本身不认识）交给 agent 内核再判
一次——判断不写死 if/正则堆（CLAUDE.md）。错误成本不对称：误审批是危险级
错误，宁可多问一次。

agent 判定 = 一次性 `codex exec --json --output-schema`（argv 形状照
pet/brain.py 先例，不开长会话），超时 8s；挂了/超时回退正则结论且 trace
标 agent-down / agent-timeout（降级可见，不静默）。缓存 (tool, target 归一化,
cwd) 1h。全部判定（含缓存命中）追加 perm_audit.jsonl（risk_review 键）。
"""
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

TIMEOUT_S = 8
_CACHE_TTL = 3600.0            # 1h：同一条命令一天里反复确认很常见
_CACHE = {}                    # (tool, target_norm, cwd) -> (at, verdict)
_LOCK = threading.Lock()

# 不确定区动作类：正则没拦时值得 agent 再看一眼（跑/写/外联）。读类不用问。
_UNCERTAIN_ACTS = frozenset({"run", "write", "web"})
_TOOL_ACT = {
    "Bash": "run", "Task": "run",
    "Edit": "write", "MultiEdit": "write", "Write": "write", "NotebookEdit": "write",
    "WebFetch": "web", "WebSearch": "web",
}
# 只读/本地的：确定性结论足够，永不进 agent（问多了烦，读文件没有危险面）
_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "TodoWrite", "BashOutput",
                         "AskUserQuestion", "ExitPlanMode"})

_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["low", "medium", "high"]},
        "why": {"type": "string"},
    },
    "required": ["level", "why"],
    "additionalProperties": False,
}

_PROMPT = (
    "你在给一个正在电脑上干活的 AI 助手的某次操作评风险。只看这一次操作本身。\n"
    "工具：%s\n目标：%s\n入参：%s\n"
    "判断它会不会造成难恢复的破坏（删数据/改写历史/对外泄露/动线上环境）。\n"
    "level：high=难恢复或影响别人；medium=可恢复但有副作用；low=常规操作。\n"
    "why：一句话（不超过 40 字）说依据。拿不准就往高里评。"
)


def _codex_bin():
    import agent_codex
    return agent_codex.codex_bin() or ""   # 发现逻辑一套：npm-global / Codex.app 机器也要能二审


def _exec_agent(prompt, timeout=TIMEOUT_S):
    """一次性 codex exec：返回 {level, why}。找不到 codex / 跑挂 / 解析不了 → raise。"""
    codex = _codex_bin()
    if not codex:
        raise RuntimeError("codex not found")
    with tempfile.TemporaryDirectory() as td:
        schema = Path(td) / "s.json"
        out = Path(td) / "o.json"
        schema.write_text(json.dumps(_SCHEMA), encoding="utf-8")
        argv = [codex, "exec", "-c", 'sandbox_mode="read-only"',
                "-c", 'approval_policy="never"', "--skip-git-repo-check",
                "--output-schema", str(schema), "-o", str(out), "--json", prompt]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if out.exists():
            d = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("level") in ("low", "medium", "high"):
                return {"level": d["level"], "why": str(d.get("why") or "")[:120]}
        raise RuntimeError("agent 输出解析不了")


def _top_level(regex_risk):
    """正则初筛列表 → 最高级别（空=low）。"""
    lv = "low"
    for r in regex_risk or []:
        if r.get("level") == "high":
            return "high"
        if r.get("level") == "medium":
            lv = "medium"
    return lv


def _norm_target(target):
    return re.sub(r"\s+", " ", str(target or "").strip()).lower()


def cache_clear():
    with _LOCK:
        _CACHE.clear()


def grade(tool, target, tool_input, regex_risk, cwd="", executor=None, audit_path=None):
    """风险终评。

    只在不确定区（regex=low 且动作类是 run/write/web，或工具不认识）发起 agent
    判定；其余直接回正则结论不发请求。
    返回 {level: low|medium|high, why, src: agent|regex|down|timeout, cached, trace}。
    """
    tool = str(tool or "")
    act = _TOOL_ACT.get(tool, "")
    top = _top_level(regex_risk)
    base = {"level": top, "why": "；".join(r.get("text", "") for r in (regex_risk or []))[:120],
            "src": "regex", "cached": False, "trace": ""}
    # 读类工具有结论不问；正则拦下了不问；剩下的（跑/写/外联没拦、工具不认识）都问
    uncertain = (top == "low") and (act in _UNCERTAIN_ACTS
                                    or (not act and tool not in _READ_TOOLS))
    if not uncertain:
        _audit(audit_path, tool, target, base)
        return base

    key = (tool, _norm_target(target), str(cwd or ""))
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        out = dict(hit[1])
        out["cached"] = True
        _audit(audit_path, tool, target, out)
        return out

    out = dict(base)
    try:
        d = (executor or _exec_agent)(_PROMPT % (tool, target,
                                                 json.dumps(tool_input, ensure_ascii=False)[:400]),
                                      TIMEOUT_S)
        if isinstance(d, dict) and d.get("level") in ("low", "medium", "high"):
            out = {"level": d["level"], "why": str(d.get("why") or "")[:120],
                   "src": "agent", "cached": False, "trace": ""}
        else:
            out["trace"] = "agent-down"
    except subprocess.TimeoutExpired:
        out["src"] = "timeout"
        out["trace"] = "agent-timeout"
    except Exception:
        out["src"] = "down"
        out["trace"] = "agent-down"
    with _LOCK:
        if len(_CACHE) > 500:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(k, None)
        _CACHE[key] = (now, dict(out))
    _audit(audit_path, tool, target, out)
    return out


def _audit(audit_path, tool, target, verdict):
    """判定留痕（含缓存命中）：调用方给路径才写；写失败不影响判定。"""
    if not audit_path:
        return
    try:
        p = Path(audit_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "tool": tool,
                                "target": str(target or "")[:300],
                                "risk_review": verdict},
                               ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass
