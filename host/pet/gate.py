# SPDX-License-Identifier: AGPL-3.0-or-later

"""手机语音闸门：桌宠在桌前会开口时，手机就别再把同一条念一遍。

同步、纯内存——notify._notify_add 在事件线程里直接调，不能 await。
"""

from .events import priority_of

_CACHE = {"online": False, "at_desk": False, "voice_on": True,
          "dnd": (23, 8), "hour": 0}


def update(**kw):
    for k in ("online", "at_desk", "voice_on", "dnd", "hour"):
        if k in kw:
            _CACHE[k] = kw[k]


def in_dnd(hour: int, dnd: tuple[int, int]) -> bool:
    a, b = int(dnd[0]), int(dnd[1])
    if a == b:
        return False
    hour = int(hour)
    if a < b:
        return a <= hour < b
    return hour >= a or hour < b


def phone_voice(kind, sid="", title="", about="", reply="") -> bool:
    pri = priority_of(kind)
    if pri is None:
        return True
    if not _CACHE["online"] or not _CACHE["at_desk"] or not _CACHE["voice_on"]:
        return True
    if in_dnd(_CACHE["hour"], _CACHE["dnd"]):
        return True
    if pri >= 3:
        return True
    return False
