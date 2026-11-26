# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.arbiter import ArbiterState, decide, in_dnd, repeat_delay
from pet.presence import PresenceState
from pet.protocol import PetEvent

DESK = PresenceState(True, False, False, 0, "desk")
AWAY = PresenceState(False, True, False, 120, "idle")
UNSURE = PresenceState(False, False, True, 0, "uncertain")


def ev(pri, ref="n1", sid="s1", kind="perm"):
    return PetEvent(ref, kind, pri, "项目", "", sid=sid)


def test_dnd_wraps_midnight():
    assert in_dnd(23, (23, 8)) and in_dnd(3, (23, 8))
    assert not in_dnd(12, (23, 8))


def test_repeat_schedule_matches_spec():
    assert [repeat_delay(0, n) for n in range(4)] == [120, 480, 1200, 0]
    assert [repeat_delay(1, n) for n in range(2)] == [480, 0]
    assert repeat_delay(2, 0) == 0


def test_p0_p1_p2_speak_at_desk_p3_badges():
    a = ArbiterState()
    assert decide(ev(0), DESK, a, 100, 12).mode == "speak"
    assert decide(ev(1, kind="stuck"), DESK, a, 100, 12).mode == "speak"
    assert decide(ev(2, kind="done"), DESK, a, 100, 12).mode == "speak"
    d = decide(ev(3, kind="progress"), DESK, a, 100, 12)
    assert d.mode == "silence" and d.announce is True


def test_away_p0_p1_phone_p2_queue():
    a = ArbiterState()
    assert decide(ev(0), AWAY, a, 100, 12).mode == "phone"
    assert decide(ev(1), AWAY, a, 100, 12).mode == "phone"
    assert decide(ev(2, kind="done"), AWAY, a, 100, 12).mode == "queue"


def test_uncertain_always_queues():
    assert decide(ev(0), UNSURE, ArbiterState(), 100, 12).mode == "queue"


def test_night_p0_queues_but_badges():
    d = decide(ev(0), DESK, ArbiterState(), 100, 23)
    assert d.mode == "queue" and d.announce is True


def test_voice_off_queues():
    assert decide(ev(0), DESK, ArbiterState(voice_on=False), 100, 12).mode == "queue"


def test_p0_preempts_p2_but_same_priority_queues():
    a = ArbiterState(speaking=True, speaking_pri=2)
    assert decide(ev(0), DESK, a, 100, 12).mode == "speak"
    a = ArbiterState(speaking=True, speaking_pri=0)
    assert decide(ev(0), DESK, a, 100, 12).mode == "queue"


def test_acked_same_event_is_silenced():
    a = ArbiterState(last_acked={"s1:perm": 100})
    d = decide(ev(0), DESK, a, 200, 12)
    assert d.mode == "silence" and d.announce is False


def test_touching_pauses_speech():
    a = ArbiterState(touching_until=130)
    assert decide(ev(0), DESK, a, 120, 12).mode == "queue"
