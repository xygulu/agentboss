# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notify
from pet import gate


def setup_function():
    gate.update(online=True, at_desk=True, voice_on=True, dnd=(23, 8), hour=12)
    notify.set_pet_gate(gate.phone_voice)


def teardown_function():
    notify.set_pet_gate(None)
    gate.update(online=False, at_desk=False, voice_on=True, dnd=(23, 8), hour=12)


def test_pet_at_desk_mutes_phone_for_p0_to_p2():
    assert gate.phone_voice("perm") is False
    assert gate.phone_voice("stuck") is False
    assert gate.phone_voice("done") is False
    assert gate.phone_voice("progress") is True


def test_pet_not_at_desk_lets_phone_speak():
    gate.update(at_desk=False)
    assert gate.phone_voice("perm") is True


def test_pet_offline_or_dnd_lets_phone_speak():
    gate.update(online=False)
    assert gate.phone_voice("perm") is True
    gate.update(online=True, hour=23)
    assert gate.phone_voice("perm") is True


def test_notify_keeps_facts_but_drops_phone_audio(monkeypatch):
    monkeypatch.setattr(notify, "_voice_ok", lambda: True)
    monkeypatch.setattr(notify, "_say_prepare", lambda *a, **k: "/api/say/x")
    monkeypatch.setattr(notify, "_nh_add", lambda *a, **k: 0)
    ev = notify._notify_add("s1", "perm", "需要确认", about="在改配置",
                            reply="要不要改？", voice=True)
    try:
        assert ev["voice"] is False
        assert ev["say"] == "" and ev["saytext"] == ""
        assert ev["about"] == "在改配置"
        assert ev["summary"] == "要不要改？"
    finally:
        notify._NOTIFY_Q.pop()
