# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠中继兜底：POST /api/pet/turn 一轮完整对话（ASR→LLM→TTS）+ 按设备的记忆。

板子连不上 host 时用；凭证是 host 在配对时发下来的中继设备凭证。
跑法： .venv/bin/python -m pytest tests/test_relay_pet.py -q
"""
import asyncio
import base64
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, body=None, dev="d" * 16, sec="s" * 48):
        self.app = {"db": db, "key": b"k"}
        self._body = body if body is not None else {}
        self.headers = {"X-CB-Dev": dev, "X-CB-Sec": sec}
        self.remote = "127.0.0.1"
        self.scheme = "http"
        self.match_info = {}

    async def json(self):
        return self._body

    async def read(self):
        return b""


def _db(tmp_path, owner=7, secret="s" * 48):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,owner) VALUES(?,?,?,?)",
               ("d" * 16, "mac", secret, owner))
    db.commit()
    return db


def _turn(db, body, dev="d" * 16, settle=0.0):
    async def go():
        r = await relay.h_pet_turn(_Req(db, body, dev=dev))
        if settle:
            await asyncio.sleep(settle)   # 等长期记忆的后台任务跑完
        return r
    r = asyncio.run(go())
    return r, json.loads(r.text)


def _fake_llm(monkeypatch, reply='{"act":"speak","line":"在呢","emotion":"calm","gesture":"nod"}'):
    calls = []

    def fake(prompt, text, timeout=25, models=None, temperature=0):
        calls.append((prompt, text, models, temperature))
        if "记忆" in prompt or "笔记" in prompt:
            return "老板爱喝冰美式", 10
        return reply, 10
    monkeypatch.setattr(relay, "_llm_text_sync", fake, raising=False)
    return calls


def test_db_init_creates_pet_mem(tmp_path):
    db = _db(tmp_path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(pet_mem)").fetchall()}
    assert {"dev", "memo", "chat"} <= cols


def test_pet_chat_keeps_last_12_rounds(tmp_path):
    db = _db(tmp_path)
    for i in range(20):
        relay.pet_chat_append(db, "d" * 16, "问%d" % i, "答%d" % i)
    memo, chat = relay.pet_mem_get(db, "d" * 16)
    assert len(chat) == 24
    assert chat[-2]["text"] == "问19" and chat[-2]["role"] == "user"
    assert chat[-1]["text"] == "答19" and chat[-1]["role"] == "assistant"


def test_pet_turn_text_roundtrip(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    calls = _fake_llm(monkeypatch)
    monkeypatch.setattr(relay, "_tts_cached", lambda t, v: b"RIFFwav", raising=False)
    r, body = _turn(db, {"text": "今天好累", "persona": {"name": "小工头",
                                                         "identity": "你是工头"}},
                    settle=0.2)
    assert r.status == 200, body
    assert body["ok"] is True
    assert body["heard"] == "今天好累"
    assert body["line"] == "在呢"
    assert body["emotion"] == "calm" and body["gesture"] == "nod"
    assert body["audio"] and body["audio_url"] == "/api/pet/audio/" + body["audio"]
    _, chat = relay.pet_mem_get(db, "d" * 16)
    assert chat[-2] == {"role": "user", "text": "今天好累"}
    assert chat[-1] == {"role": "assistant", "text": "在呢"}
    # 长期记忆异步并进去
    assert relay.pet_mem_get(db, "d" * 16)[0] == "老板爱喝冰美式"
    assert "今天好累" in calls[0][1]


def test_pet_turn_uses_old_memo_and_history(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    relay.pet_memo_save(db, "d" * 16, memo="老板女儿六岁", chat=[
        {"role": "user", "text": "我女儿几岁"}])
    calls = _fake_llm(monkeypatch)
    _turn(db, {"text": "还记得我吗"})
    prompt, text, models, temp = calls[0]
    assert "六岁" in text and "我女儿几岁" in text
    assert models == [relay.PET_LLM_MODEL] and temp == relay.PET_LLM_TEMP, \
        "陪聊要用像人的模型和一点温度，日志摘要那条路别跟着变"


def test_pet_turn_rejects_unbound_device(tmp_path, monkeypatch):
    db = _db(tmp_path, owner=None)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    r, body = _turn(db, {"text": "你好"})
    assert body["ok"] is False
    assert "未绑定账号" in body["error"]


def test_pet_turn_rejects_bad_secret(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    r = asyncio.run(relay.h_pet_turn(_Req(db, {"text": "你好"}, sec="x" * 48)))
    assert r.status == 401


def test_pet_turn_audio_path(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    monkeypatch.setattr(relay, "_asr_run", lambda raw, ext, prompt=None: ("老板你好", "", 2),
                        raising=False)
    _fake_llm(monkeypatch)
    monkeypatch.setattr(relay, "_tts_cached", lambda t, v: b"RIFFwav", raising=False)
    wav = base64.b64encode(b"RIFF" + b"\0" * 100).decode()
    r, body = _turn(db, {"audio_b64": wav})
    assert body["ok"] is True and body["heard"] == "老板你好"


def test_pet_turn_audio_off_returns_no_audio(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    _fake_llm(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("关语音时不该合成")
    monkeypatch.setattr(relay, "_tts_cached", boom, raising=False)
    r, body = _turn(db, {"text": "在吗", "voice_on": False})
    assert body["ok"] is True and body["audio"] == ""


def test_pet_audio_store_ttl(monkeypatch):
    now = time.time()
    monkeypatch.setattr(relay, "PET_AUDIO", {})
    aid = relay.pet_audio_put(b"RIFFwav", now=now)
    assert relay.pet_audio_get(aid, now=now + 1) == b"RIFFwav"
    assert relay.pet_audio_get(aid, now=now + relay.PET_AUDIO_TTL + 1) is None


def test_h_pet_audio_serves_wav(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "PET_AUDIO", {})
    aid = relay.pet_audio_put(b"RIFFwav")
    req = _Req(db, {})
    req.match_info = {"name": aid}
    r = asyncio.run(relay.h_pet_audio(req))
    assert r.status == 200 and r.body == b"RIFFwav"
    assert r.content_type == "audio/wav"


def test_h_pet_audio_unknown_id_404(tmp_path):
    db = _db(tmp_path)
    req = _Req(db, {})
    req.match_info = {"name": "nope"}
    r = asyncio.run(relay.h_pet_audio(req))
    assert r.status == 404


def test_pet_turn_persona_by_id(tmp_path, monkeypatch):
    """板子发的是人格 id（"laogongtou"），中继要认出来，不能一律用默认人格。"""
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    calls = _fake_llm(monkeypatch)
    _turn(db, {"text": "在吗", "persona": "laogongtou"})
    assert "老工头" in calls[0][0]


def test_pet_turn_unknown_persona_id_falls_back_to_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "k")
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    calls = _fake_llm(monkeypatch)
    _turn(db, {"text": "在吗", "persona": "nobody"})
    assert "小工头" in calls[0][0]
