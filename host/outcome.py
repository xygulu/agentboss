# SPDX-License-Identifier: AGPL-3.0-or-later

"""本轮结果核验：从转录里找证据，判断这一轮到底是完成、失败还是没证据。

原则：
  1. 只认转录里真实出现的东西（工具调用、is_error、退出码、测试通过/失败数）；
  2. 没证据就叫「未核实」，绝不把模型自述的「完成了」当成功；
  3. 失败也要有证据（最后一个动作是验证类的失败，或权限被拒）。
纯解析、不联网、不调模型，方便离线回放和做准确率基线。
"""
import json
import os
import re

# 「验证类」命令：真正被执行的那一步能证明任务成没成（跑测试、构建、类型检查）。
# 只看每段命令的**第一个词**，避免 `grep "build.sh"`、`sed -n ... build.sh` 这种
# 只是提到验证脚本的命令被当场跑过了。
#   ponytail: 认不出的验证脚本就当没验证（宁可说「未核实」，不能说「完成」）。
_VERIFY_HEAD_RX = re.compile(
    r"^(?:[\w./-]*/)?(?:"
    r"pytest|py\.test|jest|vitest|mocha|tsc|eslint|ruff|flake8|mypy|"
    r"xcodebuild|rspec|make|"
    r"go\s+test|cargo\s+(?:test|build|check)|swift\s+(?:build|test)|"
    r"npm\s+(?:run\s+)?(?:test|build|lint)|"
    r"(?:yarn|pnpm|bun)\s+(?:run\s+)?(?:test|build|lint)|"
    r"(?:npx|bunx)\s+(?:jest|vitest|tsc|eslint)|"
    r"\.?/?[\w./-]*(?:build|test|check)[\w.-]*\.sh"
    r")\b", re.I)
_EXEC_PREFIX_RX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+)?(?:env\s+)?")
_PY_M_RX = re.compile(r"^(?:[\w./-]*/)?python[\w.]*\s+-m\s+"
                      r"(?:pytest|py\.test|mypy|ruff|flake8|unittest)\b", re.I)
_SH_RX = re.compile(r"^(?:bash|sh|zsh)\s+\S*(?:build|test|check)[\w.-]*\.sh\b", re.I)
# 只有测试跑器输出的 passed/failed 才能读成「测试通过数」
_TEST_HEAD_RX = re.compile(
    r"^(?:[\w./-]*/)?(?:pytest|py\.test|jest|vitest|mocha|rspec)\b|"
    r"^go\s+test\b|^npm\s+(?:run\s+)?test\b|^(?:yarn|pnpm|bun)\s+(?:run\s+)?test\b|"
    r"^(?:[\w./-]*/)?python[\w.]*\s+-m\s+(?:pytest|py\.test|unittest)\b|"
    r"^\S*(?:test|check)[\w.-]*\.sh\b", re.I)
_PASS_RX = re.compile(r"\b(\d+)\s+(?:passed|passing)\b|\bPASS(?:ED)?\b|\bOK\b", re.I)
_FAIL_RX = re.compile(r"\b(\d+)\s+(?:failed|failing|errors?)\b|\bFAIL(?:ED)?\b|"
                      r"Traceback \(most recent call last\)", re.I)
# 构建/打包输出里的硬失败（管道只取 tail 时工具层的 is_error 不可靠）
_FAIL_TEXT_RX = re.compile(
    r"\*\* (?:BUILD|ARCHIVE|EXPORT|TEST) FAILED \*\*|compilation failed|"
    r"npm ERR!|^error: ", re.I | re.M)
# 命令只是被扔进后台：没有结果，不能算证据
_BG_RX = re.compile(r"running in background with ID|still running in background", re.I)
# 工具根本没跑起来（通道/模型问题）：不是任务失败，也不算证据
_INFRA_RX = re.compile(r"auto mode cannot determine|temporarily unavailable|"
                       r"tool call interrupted|API Error", re.I)
# `...; echo "exit=$?"` 自己打的退出码
_EXIT_OUT_RX = re.compile(r"(?:^|\n)\s*exit=(\d+)", re.I)
_DENY_RX = re.compile(
    r"permission for this action was denied|user (?:denied|rejected)|"
    r"doesn'?t want to proceed|tool use was rejected|"
    r"permission[^\n]{0,40}\bdenied by the user", re.I)
_EXIT_RX = re.compile(r"^\s*Exit code\s+(\d+)", re.I)

_EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_result":
                    out.append(_content_text(b.get("content")))
        return "\n".join(out)
    return ""


def _is_prompt(row):
    """真正的用户发话（不是工具结果回填）。"""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return False
    c = (row.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                   for b in c)
    return False


def split_turns(rows):
    """按用户发话切轮：返回 [{prompt, rows}]，最后一段就是当前轮。"""
    turns, cur = [], None
    for r in rows:
        if _is_prompt(r):
            if cur:
                turns.append(cur)
            c = (r.get("message") or {}).get("content")
            cur = {"prompt": (c if isinstance(c, str) else _content_text(c))[:400], "rows": []}
        elif cur is not None:
            cur["rows"].append(r)
    if cur:
        turns.append(cur)
    return turns


def turn_evidence(turn):
    """一轮里的证据：工具调用、结果（成功/失败/退出码）、助手最后说的话。"""
    uses, results, texts = {}, [], []
    for r in turn.get("rows") or []:
        if r.get("isSidechain"):
            continue
        c = (r.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        if r.get("type") == "assistant":
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    uses[b.get("id")] = {"name": b.get("name") or "",
                                         "input": b.get("input") or {}}
                elif b.get("type") == "text" and (b.get("text") or "").strip():
                    texts.append(b["text"].strip())
        elif r.get("type") == "user":
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                txt = _content_text(b.get("content"))
                m = _EXIT_RX.match(txt)
                results.append({
                    "id": b.get("tool_use_id"),
                    "err": b.get("is_error"),         # None = 老转录没这个字段
                    "exit": int(m.group(1)) if m else None,
                    "text": txt[:4000]})
    return {"prompt": turn.get("prompt") or "", "uses": uses,
            "results": results, "texts": texts}


def is_verifier(cmd):
    """这条命令真的跑了一个「能证明成败」的步骤吗？"""
    for seg in re.split(r"\|\||&&|[;|]", cmd):
        s = _EXEC_PREFIX_RX.sub("", seg.strip())
        if not s:
            continue
        if _PY_M_RX.match(s) or _SH_RX.match(s) or _VERIFY_HEAD_RX.match(s):
            return True
    return False


def _cmd_of(use):
    ti = use.get("input") or {}
    return str(ti.get("command") or ti.get("file_path") or ti.get("path") or "")


def _test_counts(text):
    """从输出里抽测试通过/失败数；抽不到返回 None（光一个 OK/PASS 不算证据）。"""
    p = _PASS_RX.search(text)
    f = _FAIL_RX.search(text)
    passed = int(p.group(1)) if p and p.group(1) else None
    failed = int(f.group(1)) if f and f.group(1) else None
    if failed is None and f:
        failed = 1
    if passed is None and failed is None:
        return None
    return {"passed": passed, "failed": failed}


def classify(ev):
    """返回 {state, label, evidence:[...]}。

    state: done_verified / failed / blocked / done_unverified
    （running / waiting 由会话状态负责，这里只判「这一轮结束了，结果如何」）
    """
    results = ev.get("results") or []
    uses = ev.get("uses") or {}
    if not results and not uses:
        return {"state": "done_unverified",
                "label": "本轮结束 · 没有工具操作",
                "evidence": ["这一轮没有工具调用"]}
    evidence = []
    last = results[-1] if results else None
    last_text = (last or {}).get("text") or ""

    # 1) 权限被拒：不是失败，是「卡住了」（句子够具体，不拿普通 Permission denied 误判）
    if last and _DENY_RX.search(last_text):
        return {"state": "blocked", "label": "未完成 · 权限被拒绝",
                "evidence": ["最后一步被拒绝：" + (last_text or "")[:120]]}

    # 2) 从后往前找最后一个「验证类」结果：它说通过就是通过，说失败就是失败。
    #    中途报错但后来跑过了，不算失败。
    for r in reversed(results):
        u = uses.get(r.get("id"))
        cmd = _cmd_of(u or {})
        txt = r.get("text") or ""
        # 只有「跑测试/构建/检查」的命令，输出里的 passed/failed 才算证据；
        # 别的命令输出里顺嘴提一句 failed（比如 nohup: failed to run）不能当测试结果。
        if not (cmd and is_verifier(cmd)):
            continue
        if _BG_RX.search(txt) or _INFRA_RX.search(txt):
            continue                    # 后台启动 / 工具没跑起来：没有结果可核
        is_test = bool(_TEST_HEAD_RX.search(cmd))
        tc = _test_counts(txt) if is_test else None
        exit_code = r.get("exit")
        if exit_code is None:
            m = _EXIT_OUT_RX.search(txt)      # `; echo "exit=$?"` 自己打的退出码
            exit_code = int(m.group(1)) if m else None
        failed_here = (r.get("err") is True or exit_code not in (None, 0)
                       or bool(tc and tc.get("failed"))
                       or bool(_FAIL_TEXT_RX.search(txt)))
        # 带回去的明细：手机上的红绿卡直接用（src=agent，用户不用再点一次）
        det = {"cmd": cmd, "rc": exit_code, "tail": txt[:600]}
        if failed_here:
            if tc and tc.get("failed"):
                return dict(det, state="failed",
                            label="失败 · 测试有 %d 项未通过" % tc["failed"],
                            evidence=["测试输出：" + txt[:160]])
            how = ("退出码 %s" % exit_code) if exit_code is not None else "报错"
            return dict(det, state="failed",
                        label="失败 · %s %s" % (_short(cmd or "验证"), how),
                        evidence=[_short(cmd or "验证") + " → " + txt[:160]])
        if tc and tc.get("passed"):
            total = tc["passed"] + (tc.get("failed") or 0)
            return dict(det, state="done_verified",
                        label="完成 · 测试 %d/%d 通过" % (tc["passed"], total),
                        evidence=["测试输出：" + txt[:160]])
        return dict(det, state="done_verified",
                    label="完成 · %s 通过" % _short(cmd),
                    evidence=[_short(cmd) + " → 成功"])

    # 3) 非验证类命令报错：只说「最后一步报错了」，不冒充「任务失败」
    if last and (last.get("err") is True or last.get("exit") not in (None, 0)):
        evidence.append("最后一步报错：" + (last_text or "")[:120])

    # 4) 正常结束但没验证证据：只陈述看到的事实，不叫「完成」
    edits = sorted({str((uses[u].get("input") or {}).get("file_path") or "")
                    for u in uses
                    if (uses[u].get("name") in _EDIT_TOOLS)} - {""})
    if edits:
        label = "本轮结束 · 改了 %d 个文件" % len(edits)
        evidence.insert(0, "改动：" + "、".join(os.path.basename(p) for p in edits[:5]))
    elif results:
        label = "本轮结束 · 执行了 %d 步" % len(results)
    else:
        label = "本轮结束 · 没有工具操作"
    if ev.get("texts"):
        evidence.append("最后一段回复：" + ev["texts"][-1][:120])
    return {"state": "done_unverified", "label": label, "evidence": evidence}


def _short(cmd, n=60):
    s = re.sub(r"\s+", " ", cmd).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def analyze_rows(rows, turns=1):
    """从转录行里取最近 N 轮，返回 [{prompt, result}]。"""
    ts = split_turns(rows)
    out = []
    for t in ts[-turns:]:
        ev = turn_evidence(t)
        out.append({"prompt": ev["prompt"], **classify(ev)})
    return out


def analyze_file(path, tail_bytes=4 * 1024 * 1024, turns=1):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()                  # 丢掉半行
        data = f.read()
    rows = []
    for ln in data.splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return analyze_rows(rows, turns)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print("==", p)
        for r in analyze_file(p):
            print(" ", r["state"], "|", r["label"])
            print("   问：", (r.get("prompt") or "")[:100])
            for e in r.get("evidence") or []:
                print("   证：", e[:160])
