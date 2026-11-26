# SPDX-License-Identifier: AGPL-3.0-or-later

"""板端切好的一段语音 → ASR → 人格回复 → TTS。可取消，慢结果不补发。

原始音频只在内存里过一遍，ASR 完就丢；历史只留文本。
"""

import re
from dataclasses import dataclass

from .lines import Line
from .llm import dev_intent, talk_style

KEEP_TURNS = 8

# ASR 对这个名字常听成同音字（实测干净音频也出「公投公投」），只用来判断
# 「整句只剩喊名字」和 kws 转写验名
_WAKE_WORD = re.compile(r"(工头|公投|公头|攻头|弓头|光头|宫头|拱头)")
_NOISE = re.compile(r"[\s，。、！？,.!?~～…]+")


def is_bare_wake(text: str) -> bool:
    """整句只有喊名字（没有任何实际内容）→ 只点头，不接话。

    不接话很重要：她一张嘴，板子的麦克风就被自己的声音占住，老板紧跟着问的
    那句会被吞掉，只剩一句打发人的场面话。"""
    s = _NOISE.sub("", text or "")
    return s == "" or _WAKE_WORD.sub("", s) == ""


_FILLER = set("嗯啊哦噢喔呃诶欸哎唉呀哈呵嘿咳")


def is_non_speech(text: str) -> bool:
    """ASR 在噪声上只能吐出「嗯。」「啊？」这种语气词，不是老板在问事。

    接下去就是大模型礼貌地跟着「嗯」、合成一句、老板听一句废话；她一张嘴，
    板子的麦克风又把这句听成噪声——一直循环。"""
    s = _NOISE.sub("", text or "")
    return len(s) <= 3 and all(c in _FILLER for c in s)


_BRIEF_CAP = 24


def shorten(line):
    """老板嫌啰嗦后的硬约束：光在 prompt 里说没用（实测照样一大串），
    句子真截到第一口气。"""
    t = (line.text or "").strip()
    if len(t) <= _BRIEF_CAP:
        return line
    cut = _BRIEF_CAP
    for i, ch in enumerate(t[:_BRIEF_CAP]):
        if ch in "，。！？；…" and i + 1 >= 6:
            cut = i + 1
            break
    s = t[:cut].strip().rstrip("，,；;、")
    return Line(s, line.emotion, line.gesture, line.badge,
                getattr(line, "dev", None))


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    line: str = ""
    audio: str = ""
    emotion: str = "calm"
    gesture: str = "none"
    error: str = ""
    dev: object = None      # 顺手摸一下开发板：{"vol":40} / {"bri":70}
    pref: str = ""          # 老板这次给的新要求：存起来，以后每轮都照做


class ChatSession:
    def __init__(self, asr, reply, tts, persona, facts, history,
                 max_bytes=10 * 1024 * 1024):
        self.asr = asr
        self.reply = reply
        self.tts = tts
        self.persona = persona
        self.facts = facts
        self.history = history
        self.max_bytes = max_bytes
        self.buf = b""
        self._too_big = False
        self._cancelled = False

    def feed(self, chunk):
        self.buf += chunk
        if len(self.buf) > self.max_bytes:
            self._too_big = True

    def cancel(self):
        self._cancelled = True

    async def transcribe(self):
        """→ (text, err)。先只做识别：调用方据 text 决定要不要接话。"""
        if self._cancelled:
            return "", "cancelled"
        if self._too_big:
            return "", "音频太大"
        if not self.buf:
            return "", "空音频"
        raw = self.buf
        self.buf = b""
        text, err = await self.asr(raw)
        if self._cancelled:
            return "", "cancelled"
        return text, err

    async def respond(self, text: str) -> ChatResult:
        """对着这句识别结果想一句、合成语音、记进历史。"""
        # 老板调她的说法方式（「太啰嗦了」）：当轮就生效，也带给后面的轮次
        style = talk_style(text)
        if style:
            self.facts["talk"] = style
        line = await self.reply(self.persona, self.history, text, self.facts)
        # 模型认出的「老板新要求」：当轮就照做，并带回调用方存起来
        talk = getattr(line, "talk", None) or talk_style(text)
        if talk in ("brief", "normal"):
            self.facts["talk"] = talk
        pref = getattr(line, "pref", None) or ""
        if pref:
            prefs = [p for p in (self.facts.get("prefs") or []) if p != pref]
            self.facts["prefs"] = (prefs + [pref])[-8:]
        if self.facts.get("talk") == "brief":
            line = shorten(line)
        if self._cancelled:
            return ChatResult(False, error="cancelled")
        # 模型嘴上答应了却没发 dev（实测常事）→ 老板那句话自己认一遍，
        # 不然她说「调到 30 啦」而板子一动不动
        dev = getattr(line, "dev", None) or dev_intent(text, self.facts)
        audio = self.tts(line.text, self.persona.voice)
        self.history = (list(self.history)
                        + [{"role": "user", "text": text},
                           {"role": "assistant", "text": line.text}])[-KEEP_TURNS:]
        return ChatResult(True, text=text, line=line.text, audio=audio,
                          emotion=line.emotion, gesture=line.gesture,
                          dev=dev, pref=pref)

    async def end(self) -> ChatResult:
        text, err = await self.transcribe()
        if err:
            return ChatResult(False, error=err)
        return await self.respond(text)
