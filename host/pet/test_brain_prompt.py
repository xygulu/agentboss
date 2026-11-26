# SPDX-License-Identifier: AGPL-3.0-or-later

"""慢脑子/笔记只许记老板亲口说的话。

11-21 半夜出过事：老板只问「那咋玩？」，她自己顺口编了「绿豆汤快好了吧」，
这句话进了笔记，变成老板「在厨房煮绿豆汤」。她的回话再也不许进 prompt。

python3 pet/test_brain_prompt.py
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pet import brain, llm  # noqa: E402

HIST = [{"role": "user", "text": "那咋玩？"},
        {"role": "assistant", "text": "绿豆汤快好了吧？我闻着味儿啦～"}]


def test_brain_prompt_only_boss():
    p = brain.prompt("", [], HIST, "在玩什么？")
    assert "那咋玩？" in p and "在玩什么？" in p
    assert "绿豆汤" not in p


def test_memo_body_only_boss():
    got = {}

    async def fake(system, body, kind, timeout):
        got["body"] = body
        return "笔记"

    real = llm.notify._llm_text
    llm.notify._llm_text = fake
    try:
        out = asyncio.run(llm.update_memo("旧笔记", HIST, "睡觉。", "灯调暗～"))
    finally:
        llm.notify._llm_text = real
    assert out == "笔记"
    assert "绿豆汤" not in got["body"] and "灯调暗" not in got["body"]
    assert "那咋玩？" in got["body"] and "睡觉。" in got["body"]


if __name__ == "__main__":
    test_brain_prompt_only_boss()
    test_memo_body_only_boss()
    print("ok")
