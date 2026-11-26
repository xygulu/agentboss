# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notify
from pet.audio import pet_audio_file, pet_tts, persona_assets
from pet.personas import get_persona


def test_same_text_and_voice_same_url(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "_SAY_DIR", tmp_path)
    monkeypatch.setattr(notify, "_SAY_TEXTS", {})
    monkeypatch.setattr(notify, "_say_gen", lambda *a, **k: "")
    a = pet_tts("老大，卡住了。", "Chelsie")
    b = pet_tts("老大，卡住了。", "Chelsie")
    assert a == b
    assert a.startswith("/api/pet/audio/")
    assert len(a.rsplit("/", 1)[1]) == 24


def test_voice_changes_key(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "_SAY_DIR", tmp_path)
    monkeypatch.setattr(notify, "_SAY_TEXTS", {})
    monkeypatch.setattr(notify, "_say_gen", lambda *a, **k: "")
    assert pet_tts("嗯？", "Chelsie") != pet_tts("嗯？", "Ethan")


def test_empty_text_returns_empty():
    assert pet_tts("", "Chelsie") == ""
    assert pet_tts("  ", "Chelsie") == ""


def test_missing_file_and_bad_name(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "_SAY_DIR", tmp_path)
    assert pet_audio_file("0" * 24) is None
    assert pet_audio_file("../etc/passwd") is None


def test_persona_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "_SAY_DIR", tmp_path)
    monkeypatch.setattr(notify, "_SAY_TEXTS", {})
    monkeypatch.setattr(notify, "_say_gen", lambda *a, **k: "")
    a = persona_assets(get_persona("xiaogongtou"))
    assert a["voice"] == "Ethan"   # 小工头已换男声（2026-11-21）
    assert a["sounds"]["think"].startswith("/api/pet/audio/")
