# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.chat import ChatSession, is_bare_wake, is_non_speech
from pet.lines import Line
from pet.personas import get_persona


def make(asr_text="谁在跑？", asr_err=""):
    async def asr(raw):
        return asr_text, asr_err
    async def reply(persona, history, text, facts):
        return Line("两个在跑，一个在等你。", "proud", "nod", None)
    return ChatSession(asr, reply, lambda t, v: "/api/pet/audio/x",
                       get_persona("xiaogongtou"), {"running": 2}, [])


def test_end_runs_asr_reply_tts_and_clears_audio():
    s = make()
    s.feed(b"\x00\x01" * 100)
    r = asyncio.run(s.end())
    assert r.ok and r.text == "谁在跑？"
    assert r.audio == "/api/pet/audio/x"
    assert s.buf == b""
    assert len(s.history) == 2


def test_empty_audio_is_rejected():
    r = asyncio.run(make().end())
    assert not r.ok and "空" in r.error


def test_asr_error_is_reported():
    s = make(asr_err="识别失败")
    s.feed(b"x" * 100)
    r = asyncio.run(s.end())
    assert not r.ok and r.error == "识别失败"


def test_cancel_drops_late_reply():
    s = make()
    s.feed(b"x" * 100)
    s.cancel()
    r = asyncio.run(s.end())
    assert not r.ok and r.error == "cancelled"
    assert s.history == []


def test_audio_too_large_is_rejected():
    s = ChatSession(None, None, None, get_persona("xiaogongtou"), {}, [], max_bytes=10)
    s.feed(b"x" * 11)
    r = asyncio.run(s.end())
    assert not r.ok and "太大" in r.error


def test_device_command_survives_into_result():
    """调音量/亮度这类「顺手摸一下开发板」的动作要跟着回复一起回去。"""
    from pet.lines import Line

    async def reply(persona, history, text, facts):
        return Line("好，调到 30", "calm", "nod", None, dev={"vol": 30})

    sess = ChatSession(lambda raw: None, reply, lambda t, v: "a.wav",
                       get_persona("xiaogongtou"), {}, [])
    r = asyncio.run(sess.respond("音量三十"))
    assert r.dev == {"vol": 30}


def test_device_command_falls_back_when_model_forgets_dev():
    """模型嘴上说「调到 30 啦」却没带 dev（线上实测连中三次）：
    老板那句话要自己认一遍，不然板子一动不动、她在说谎。"""
    from pet.lines import Line

    async def reply(persona, history, text, facts):
        return Line("行，调到 30 啦", "calm", "nod", None)

    sess = ChatSession(lambda raw: None, reply, lambda t, v: "a.wav",
                       get_persona("xiaogongtou"), {"vol": 55, "bri": 60}, [])
    assert asyncio.run(sess.respond("音量调到三十")).dev == {"vol": 30}
    # 只是聊天，别乱调
    assert asyncio.run(sess.respond("今天好累")).dev is None


def test_saying_too_wordy_makes_her_short():
    """老板说「你太啰嗦了」，她还在说一大串——prompt 管不住就得真截短。"""
    from pet.lines import Line

    async def reply(persona, history, text, facts):
        return Line("老板你别着急，我是想把这件事从头到尾给你讲清楚，"
                    "毕竟细节都挺重要的，你听我慢慢说完嘛。", "calm", "nod", None)

    facts = {}
    sess = ChatSession(lambda raw: None, reply, lambda t, v: "a.wav",
                       get_persona("xiaogongtou"), facts, [])
    r = asyncio.run(sess.respond("你太啰嗦了"))
    assert len(r.line) <= 24
    assert facts["talk"] == "brief"
    # 之后每轮都短
    r2 = asyncio.run(sess.respond("今天吃什么"))
    assert len(r2.line) <= 24
    # 让她多说点，放回原来的
    r3 = asyncio.run(sess.respond("多说点，详细些"))
    assert facts["talk"] == "normal"
    assert len(r3.line) > 24


def test_model_pref_is_applied_and_reported():
    """模型认出老板提的新要求：当轮生效（talk=brief 真截短），并按 pref 报回去存。"""
    from pet.lines import Line

    async def reply(persona, history, text, facts):
        assert "老板嫌我啰嗦" not in (facts.get("prefs") or [])
        return Line("好，这段时间我讲得太细了，以后我就说重点，别耽误你干活。",
                    "calm", "nod", None, pref="老板嫌我啰嗦", talk="brief")

    facts = {}
    sess = ChatSession(lambda raw: None, reply, lambda t, v: "a.wav",
                       get_persona("xiaogongtou"), facts, [])
    r = asyncio.run(sess.respond("你太啰嗦了"))
    assert r.pref == "老板嫌我啰嗦"
    assert facts["talk"] == "brief"
    assert facts["prefs"] == ["老板嫌我啰嗦"]
    assert len(r.line) <= 24
    # 下一轮：要求已经带在 facts 里
    seen = {}

    async def reply2(persona, history, text, facts2):
        seen.update(facts2)
        return Line("嗯", "calm", "nod", None)

    sess.reply = reply2
    asyncio.run(sess.respond("今天累"))
    assert seen["prefs"] == ["老板嫌我啰嗦"]
    assert seen["talk"] == "brief"


def test_bare_wake_is_not_a_question():
    # 只喊了名字：点个头就行，别接话（接话会盖住老板真正要问的那句）
    assert is_bare_wake("工头工头。")
    assert is_bare_wake(" 工头，工头！")
    assert not is_bare_wake("工头工头，你叫什么名字")
    assert not is_bare_wake("嗯")
    assert is_bare_wake("")          # 什么都没说，更不值得接话


def test_noise_transcript_is_not_answered():
    # 键盘/椅子/风扇转出来的就是「嗯。」这类语气词。接话就是一句
    # 「我没听清，再说一遍」——说了，板子又把这句听成噪声，一直循环。
    assert is_non_speech("嗯。")
    assert is_non_speech("嗯嗯…")
    assert is_non_speech("啊？")
    assert not is_non_speech("好。")          # 真在应答，得接
    assert not is_non_speech("今天有什么要处理的")
