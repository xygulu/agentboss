# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notify
from pet.llm import _json_obj, chat_reply, dev_intent, direct, update_memo
from pet.personas import get_persona
from pet.protocol import Badge, PetEvent


def test_json_tolerates_code_fence():
    assert _json_obj('```json\n{"line":"x"}\n```') == {"line": "x"}
    assert _json_obj("not json") is None


def test_direct_parses_line(monkeypatch):
    async def fake(prompt, text, purpose, timeout=20):
        assert "/secret" not in prompt + text
        return '{"line":"老大，卡住了！","emotion":"anxious","gesture":"stomp"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    ev = PetEvent("n1", "perm", 0, "改配置", "在改配置", sid="s1",
                  badge=Badge("改配置", "waiting"))
    line = asyncio.run(direct(get_persona("xiaogongtou"), ev,
                              {"title": "改配置", "at_desk": True, "cwd": "/secret"}))
    assert line.text == "老大，卡住了！"
    assert line.emotion == "anxious"


def test_direct_returns_none_on_garbage(monkeypatch):
    async def fake(*a, **k):
        return "我觉得应该这样"
    monkeypatch.setattr(notify, "_llm_text", fake)
    ev = PetEvent("n1", "perm", 0, "改配置", "")
    assert asyncio.run(direct(get_persona("xiaogongtou"), ev, {})) is None


def test_chat_reply_uses_history_and_limits_line(monkeypatch):
    async def fake(prompt, text, purpose, timeout=20):
        assert "老板: 谁在跑？" in text
        return '{"act":"speak","line":"' + "啊" * 100 + '","emotion":"proud","gesture":"nod"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    line = asyncio.run(chat_reply(get_persona("xiaogongtou"),
                                 [{"role": "user", "text": "谁在跑？"}],
                                 "谁在跑？", {"running": 2}))
    assert len(line.text) <= 80
    assert line.emotion == "proud"


def test_chat_prompt_allows_companion_talk(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        return '{"act":"speak","line":"在呢","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "今天好累", {}))
    assert "陪" in seen["p"] or "闲聊" in seen["p"]
    assert "不百科" not in seen["p"]


def test_chat_reply_sees_only_last_6_turns(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["t"] = text
        return '{"act":"speak","line":"嗯","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    hist = [{"role": "user", "text": "第%d句" % i} for i in range(20)]
    asyncio.run(chat_reply(get_persona("xiaogongtou"), hist, "现在", {}))
    assert "第13句" not in seen["t"]  # 带多了会照着复读
    assert "第14句" in seen["t"]


def test_chat_prompt_tells_it_to_answer_the_last_line(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        return '{"act":"speak","line":"嗯","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "你在干嘛", {}))
    assert "只回答老板最后一句" in seen["p"]
    assert "不要重复你上一句" in seen["p"]


def test_chat_reply_falls_back_when_model_down(monkeypatch):
    async def fake(*a, **k):
        return ""
    monkeypatch.setattr(notify, "_llm_text", fake)
    line = asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "喂", {}))
    # 模型挂了要说模型挂了：「我没听清」是赖老板没说明白，他会一遍遍重说
    assert line.text and "听清" not in line.text


def test_chat_reply_includes_long_term_memo(monkeypatch):
    """长期记忆（老板是谁、喜好、生活里的事）要进聊天 prompt：
    只留最近 6 轮的话，隔天就全忘了。"""
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["t"] = text
        return '{"act":"speak","line":"嗯","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "还记得我吗",
                           {}, memo="老板爱喝冰美式；女儿六岁"))
    assert "冰美式" in seen["t"]


def test_chat_reply_carries_device_command(monkeypatch):
    """老板说「音量调到 40」：模型回里的 dev 要原样带到板子上，
    不然她嘴上答应、开发板一点没动。"""
    async def fake(prompt, text, purpose, timeout=20):
        assert '"vol": 55' in text and '"bri": 70' in text   # 板子报的现状要进事实
        return '{"line":"行，调到 40","dev":{"vol":40}}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    line = asyncio.run(chat_reply(get_persona("xiaogongtou"), [],
                                  "音量调到四十", {"vol": 55, "bri": 70}))
    assert line.dev == {"vol": 40}


def test_chat_reply_clamps_and_drops_bad_dev(monkeypatch):
    async def fake(*a, **k):
        return '{"line":"好","dev":{"vol":150,"bri":"亮一点"}}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    line = asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "亮一点", {}))
    assert line.dev == {"vol": 100}

    async def fake2(*a, **k):
        return '{"line":"好","dev":"vol"}'
    monkeypatch.setattr(notify, "_llm_text", fake2)
    assert asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "嗯", {})).dev is None


def test_chat_prompt_lets_it_touch_the_board(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        return '{"line":"嗯","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "你多大", {}))
    assert "音量" in seen["p"] and "亮度" in seen["p"]
    assert "dev" in seen["p"]


def test_dev_intent_sets_volume_by_number():
    # 模型漏了 dev 也不能「嘴上说调了」：这句一定要真的调
    assert dev_intent("音量调到三十", {"vol": 55}) == {"vol": 30}
    assert dev_intent("把声音开到 20 吧", {"vol": 55}) == {"vol": 20}
    assert dev_intent("喇叭调到100", {"vol": 55}) == {"vol": 100}


def test_dev_intent_relative_and_brightness():
    assert dev_intent("音量小一点", {"vol": 55}) == {"vol": 40}
    assert dev_intent("声音开大点", {"vol": 55}) == {"vol": 70}
    assert dev_intent("屏幕调暗一些", {"bri": 60}) == {"bri": 45}
    assert dev_intent("亮度调到二十", {"bri": 60}) == {"bri": 20}
    # 嫌它不对也是要调：说「太大了」不是夸
    assert dev_intent("声音太大了", {"vol": 55}) == {"vol": 40}
    assert dev_intent("屏幕太暗了", {"bri": 60}) == {"bri": 75}


def test_dev_intent_ignores_questions_and_talk():
    for t in ("现在音量是多少", "刚才音量调到三十的时候好吵", "屏幕真好看", "你好"):
        assert dev_intent(t, {"vol": 55, "bri": 60}) is None
    # 板子没报现场就不猜
    assert dev_intent("音量调到三十", {}) is None


def test_talk_style_reads_the_boss():
    from pet.llm import talk_style
    assert talk_style("你太啰嗦了") == "brief"
    assert talk_style("说短一点") == "brief"
    assert talk_style("话太多了吧") == "brief"
    assert talk_style("多说点，说详细些") == "normal"
    assert talk_style("今天好累") is None


def test_chat_prompt_honors_talk_brief(monkeypatch):
    """老板嫌她啰嗦：prompt 里要有这条，事实里 talk=brief 也要带到。"""
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        seen["t"] = text
        return '{"line":"好","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "嗯",
                           {"talk": "brief"}))
    assert "啰嗦" in seen["p"]
    assert '"talk": "brief"' in seen["t"]


def test_chat_reply_carries_boss_pref(monkeypatch):
    """老板提要求（不改身份，只改说法方式）：模型把它写进 pref，带回来存。"""
    async def fake(prompt, text, purpose, timeout=20):
        return ('{"line":"好，我少说点","pref":"老板嫌我啰嗦","talk":"brief"}')
    monkeypatch.setattr(notify, "_llm_text", fake)
    line = asyncio.run(chat_reply(get_persona("xiaogongtou"), [],
                                  "你太啰嗦了", {}))
    assert line.pref == "老板嫌我啰嗦"
    assert line.talk == "brief"


def test_chat_prompt_keeps_identity_but_style_follows_boss(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        return '{"line":"嗯","emotion":"calm","gesture":"none"}'
    monkeypatch.setattr(notify, "_llm_text", fake)
    asyncio.run(chat_reply(get_persona("xiaogongtou"), [], "你好", {}))
    assert "不变" in seen["p"]          # 身份/职责定死
    assert "prefs" in seen["p"]         # 老板的要求每轮带上
    assert "pref" in seen["p"]          # 模型要能记新要求


def test_update_memo_merges_old_notes(monkeypatch):
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["p"] = prompt
        seen["t"] = text
        return "老板爱喝冰美式；在折腾桌宠"
    monkeypatch.setattr(notify, "_llm_text", fake)
    out = asyncio.run(update_memo("老板在折腾桌宠", [], "今天想喝点啥", "冰美式吧"))
    assert "冰美式" in out
    assert "老板在折腾桌宠" in seen["t"]          # 旧笔记要带进去合并
    assert "记忆" in seen["p"] or "笔记" in seen["p"]


def test_update_memo_returns_empty_when_model_down(monkeypatch):
    async def fake(*a, **k):
        return ""
    monkeypatch.setattr(notify, "_llm_text", fake)
    assert asyncio.run(update_memo("旧笔记", [], "x", "y")) == ""
