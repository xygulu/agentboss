# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.lines import line_for
from pet.personas import DEFAULT_PERSONA, PERSONAS, get_persona
from pet.protocol import Badge, PetEvent


def test_default_persona_and_fallback():
    p = get_persona("nope")
    assert p.id == DEFAULT_PERSONA
    assert p.voice == "Ethan"   # 小工头已换男声（2026-11-21）


def test_all_personas_have_core_lines_and_sounds():
    for pid, p in PERSONAS.items():
        for kind in ("perm", "stuck", "done", "input", "limit", "chat_unknown"):
            assert p.lines.get(kind), (pid, kind)
        assert p.sounds.get("think") and p.sounds.get("ok")


def test_line_for_is_stable_and_fills_title():
    p = get_persona("xiaogongtou")
    ev = PetEvent("n1", "perm", 0, "改配置", "", badge=Badge("改配置", "waiting"))
    a = line_for(p, ev)
    b = line_for(p, ev)
    assert a.text == b.text
    assert "改配置" in a.text
    assert a.text[-1] in "。！？…"
    assert a.emotion == "anxious"
    assert a.gesture == "look_up"


def test_no_service_tone_or_honorific():
    for p in PERSONAS.values():
        for templates in p.lines.values():
            for t in templates:
                assert "您" not in t
