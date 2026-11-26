# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from pet import api


def test_add_routes_registers_pet_endpoints():
    app = web.Application()
    api.add_routes(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    for p in ("/api/pet/state", "/api/pet/ws", "/api/pet/audio/{name}",
              "/api/pet/event", "/api/pet/presence", "/api/pet/persona",
              "/api/pet/personas", "/api/pet/persona/{id}/assets",
              "/api/pet/pair", "/api/pet/pair/start", "/pet/sim"):
        assert p in paths, p


def test_sim_page_contains_websocket():
    req = make_mocked_request("GET", "/pet/sim")
    resp = asyncio.run(api.h_sim(req))
    assert resp.status == 200
    body = resp.text
    assert "WebSocket" in body and "/api/pet/ws" in body


def test_presence_payload_is_clamped():
    p = api.merge_phone_presence({"phone_present": True, "moving": True,
                                  "screen": "on", "ts": 100}, now=105)
    assert p.phone_present is True
    assert p.phone_moved_s == 5


def test_llm_cfg_reads_key_from_env_or_file(tmp_path):
    # 板子直连百炼的钥匙：只认本机环境/文件，不下发别处
    from pet.api import llm_cfg
    out = llm_cfg({"DASHSCOPE_API_KEY": " sk-abc "}, keyfile=tmp_path / "nope")
    assert out == {"key": "sk-abc", "base": "https://dashscope.aliyuncs.com"}
    f = tmp_path / "dashscope.key"
    f.write_text("sk-file\n")
    assert llm_cfg({}, keyfile=f)["key"] == "sk-file"
    # 没配：整块不给，板子继续只走 host
    assert llm_cfg({}, keyfile=tmp_path / "nope") == {}


def test_pair_extra_carries_llm_and_persona():
    from pet.api import pair_extra
    from pet.personas import get_persona
    p = get_persona("xiaogongtou")
    out = pair_extra(p, {"key": "sk-abc", "base": "https://dashscope.aliyuncs.com"})
    assert out["llm"]["key"] == "sk-abc"
    assert out["persona"]["name"] == "小工头"
    assert out["persona"]["voice"] == "Ethan"
    assert "relay" not in out
    assert "llm" not in pair_extra(p, {})


def test_cfg_msg_goes_out_with_llm_and_persona():
    # 直连配置随 ws cfg 下发：已配对的板子重连就拿到，不用重新配网
    from pet.api import cfg_msg
    from pet.personas import get_persona
    p = get_persona("xiaogongtou")
    out = cfg_msg(p, {"key": "sk-abc", "base": "https://dashscope.aliyuncs.com"})
    assert out["type"] == "cfg"
    assert out["llm"]["key"] == "sk-abc"
    assert out["persona"]["name"] == p.name
    assert "llm" not in cfg_msg(p, {})


def test_audio_fmt_wav_prefers_wav(tmp_path, monkeypatch):
    from pet import audio
    name = "a" * 24
    monkeypatch.setattr(audio.notify, "_say_files",
                        lambda i: (tmp_path / (i + ".m4a"), tmp_path / (i + ".wav")))
    (tmp_path / (name + ".m4a")).write_bytes(b"m4a")
    (tmp_path / (name + ".wav")).write_bytes(b"wav")
    assert audio.pet_audio_file(name, fmt="wav").suffix == ".wav"
    assert audio.pet_audio_file(name).suffix == ".m4a"


def test_audio_fmt_wav_never_falls_back_to_m4a(tmp_path, monkeypatch):
    # 板子只认 WAV：合成还没出 wav 时宁可 404（板子会重试），
    # 也别把 m4a 当 wav 发过去——那样头解析失败，一句白哑掉。
    from pet import audio
    name = "c" * 24
    monkeypatch.setattr(audio.notify, "_say_files",
                        lambda i: (tmp_path / (i + ".m4a"), tmp_path / (i + ".wav")))
    (tmp_path / (name + ".m4a")).write_bytes(b"m4a")
    assert audio.pet_audio_file(name, fmt="wav") is None
    assert audio.pet_audio_file(name).suffix == ".m4a"


def test_say_gen_keeps_wav_for_device(tmp_path, monkeypatch):
    import wave
    import notify
    name = "b" * 24

    def fake_relay(text, path, voice=""):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 8000)
        return True

    monkeypatch.setattr(notify, "_say_relay", fake_relay)
    out = tmp_path / (name + ".m4a")
    assert notify._say_gen("你好", str(out))
    assert out.exists()
    assert (tmp_path / (name + ".wav")).exists()   # 板子放 PCM，wav 得留着


def test_audio_waits_for_late_file(monkeypatch, tmp_path):
    # 回话的 URL 先到、合成文件后到：不能第一句就 404（「问她话没反馈」）
    async def ok(_req):
        return None
    monkeypatch.setattr(api, "require_pet", ok)
    f = tmp_path / "x.wav"
    f.write_bytes(b"RIFF")
    calls = {"n": 0}

    def late(name, fmt=""):
        calls["n"] += 1
        return f if calls["n"] >= 3 else None

    monkeypatch.setattr(api, "pet_audio_file", late)
    req = make_mocked_request("GET", "/api/pet/audio/abc?fmt=wav",
                              match_info={"name": "abc"})
    resp = asyncio.run(api.h_pet_audio(req))
    assert resp.status == 200
    assert calls["n"] >= 3
