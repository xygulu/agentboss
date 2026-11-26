# SPDX-License-Identifier: AGPL-3.0-or-later

"""提醒语料回归：拿真实/复盘出来的「这一轮回复 + 当时模型的输出」重放一遍判定。

锁的是三件事，每一件都在真机上出过事：
  1. 待确认问句必须是这轮回复的原话（模型编出来的问句不能推到手机上）
  2. 已经做完的事不能再问「要不要做」（发布完又问你发不发）
  3. 「这轮其实在等你」到底是谁判的——规则 / 模型 / 模型没答上，都要能说清

跑法：
    .venv/bin/python -m pytest tests/test_corpus.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402

CASES = json.loads((Path(__file__).resolve().parent / "corpus" / "rounds.json")
                   .read_text(encoding="utf-8"))


def judge(c):
    """把一个用例跑完整判定：概括模型的输出走 _reply_decide，na 走 _na_need。
    返回（判定结果, 记下的判定来路）。"""
    sid = "corpus-" + c["id"]
    span = server._reply_span(c["reply"])
    _, act, _, act_src = server._reply_decide(c["llm"]["text"], span, 40)

    nt = server.notify          # 判定函数搬到 notify.py 了：要打桩的是它自己的模块名
    prev = (nt._na_llm, nt._voice_ok, nt._na_pref)

    async def fake_llm(msg):
        return c["llm"].get("na")

    nt._na_llm = fake_llm
    nt._voice_ok = lambda: bool(c.get("voice", True))
    nt._na_pref = lambda: True
    try:
        na = asyncio.run(server._na_need(c["reply"], sid))
    finally:
        nt._na_llm, nt._voice_ok, nt._na_pref = prev

    tr = server._TRACE.pop(sid, {})
    got = {"wait": bool(na) or bool(act), "act": act,
           "act_src": act_src, "na": tr.get("na", "")}
    return got


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_round(case):
    got = judge(case)
    assert got == case["expect"], "%s：%s\n得到 %s\n期望 %s" % (
        case["id"], case["note"], got, case["expect"])


def test_corpus_covers_every_verdict():
    """语料别悄悄缩水：每种判定来路都得至少有一条用例压着。"""
    seen = {"act_src": set(), "na": set()}
    for c in CASES:
        seen["act_src"].add(c["expect"]["act_src"])
        seen["na"].add(c["expect"]["na"])
    assert seen["act_src"] >= {"", "ok", "not-grounded", "conflict"}
    assert seen["na"] >= {"off", "rule", "no-signal", "llm-yes", "llm-no", "llm-down"}
