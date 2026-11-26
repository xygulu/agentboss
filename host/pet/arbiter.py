# SPDX-License-Identifier: AGPL-3.0-or-later

"""仲裁：这条通知该谁出声——桌宠说 / 手机说 / 攒着 / 只亮角标。"""

from dataclasses import dataclass, field

from .gate import in_dnd
from .presence import PresenceState
from .protocol import PetEvent


@dataclass
class ArbiterState:
    speaking: bool = False
    speaking_pri: int = 99
    speaking_until: float = 0.0
    voice_on: bool = True
    dnd: tuple[int, int] = (23, 8)
    touching_until: float = 0.0
    last_acked: dict[str, float] = field(default_factory=dict)
    same_ref_spoken: dict[str, float] = field(default_factory=dict)


@dataclass
class Decision:
    mode: str
    repeat_s: int
    reason: str
    announce: bool


def repeat_delay(priority: int, count: int) -> int:
    if priority == 0:
        seq = [120, 480, 1200]
        return seq[count] if 0 <= count < len(seq) else 0
    if priority == 1:
        return 480 if count == 0 else 0
    return 0


def decide(ev: PetEvent, st: PresenceState, a: ArbiterState,
           now: float, hour: int) -> Decision:
    if in_dnd(hour, a.dnd):
        if ev.priority == 0:
            return Decision("queue", 0, "dnd", True)
        return Decision("silence", 0, "dnd", False)
    if not a.voice_on:
        if ev.priority == 0:
            return Decision("queue", 0, "voice_off", True)
        return Decision("silence", 0, "voice_off", True)
    if st.away:
        if ev.priority <= 1:
            return Decision("phone", repeat_delay(ev.priority, 0), "away", False)
        return Decision("queue", 0, "away", False)
    if st.uncertain:
        return Decision("queue", 0, "uncertain", True)
    if a.touching_until > now:
        return Decision("queue", 0, "touching", False)
    if a.speaking and ev.priority >= a.speaking_pri:
        return Decision("queue", 0, "speaking", False)
    key = f"{ev.sid}:{ev.kind}"
    if key in a.last_acked and now - a.last_acked[key] < 600:
        return Decision("silence", 0, "acked", False)
    if ev.priority >= 3:
        return Decision("silence", 0, "low", True)
    return Decision("speak", repeat_delay(ev.priority, 0), "speak", False)
