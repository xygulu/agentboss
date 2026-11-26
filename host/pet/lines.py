# SPDX-License-Identifier: AGPL-3.0-or-later

"""按人格和事件挑一句台词：稳定（同一 ref 同一句）、带情绪/动作/角标。"""

import hashlib
from dataclasses import dataclass

MOOD = {"perm": ("anxious", "look_up"), "ask": ("anxious", "look_up"),
        "input": ("anxious", "tilt_head"), "stuck": ("grumpy", "stomp"),
        "limit": ("sleepy", "doze"), "done": ("proud", "nod")}
ENDS = "。！？…~～"
MAX_LEN = 60


@dataclass
class Line:
    text: str
    emotion: str
    gesture: str
    badge: object = None
    dev: object = None      # 要顺手摸一下开发板：{"vol":40} / {"bri":70}
    pref: object = None     # 老板这次给的新要求（一句原话）：记下来，以后照做
    talk: object = None     # 说法方式："brief" / "normal"（模型按老板要求给）


def pick(persona, kind, fields, seed) -> Line:
    templates = persona.lines.get(kind) or persona.lines["chat_unknown"]
    idx = int(hashlib.sha1((str(seed) + "|" + kind).encode("utf-8")).hexdigest(), 16) % len(templates)
    text = templates[idx].format(**fields)
    if text and text[-1] not in ENDS:
        text += "。"
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 2] + "…"
    emo, ges = MOOD.get(kind, ("calm", "none"))
    return Line(text, emo, ges, None)


def line_for(persona, event) -> Line:
    line = pick(persona, event.kind,
                {"title": event.title or "那个项目", "n": event.count},
                event.ref)
    line.badge = event.badge
    return line
