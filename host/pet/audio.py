# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠语音：按人格音色合成，复用 notify 的 saycache 目录和清理逻辑。"""

import hashlib
import re
import threading
import time

import notify

NAME_RE = re.compile(r"[0-9a-f]{24}\Z")


def pet_tts(text: str, voice: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    voice = voice or ""
    i = hashlib.sha1(("pet1|" + voice + "|" + text).encode("utf-8")).hexdigest()[:24]
    notify._SAY_TEXTS[i] = (text, voice, time.time())
    if len(notify._SAY_TEXTS) > 200:
        for k in sorted(notify._SAY_TEXTS, key=lambda q: notify._SAY_TEXTS[q][2])[:-100]:
            notify._SAY_TEXTS.pop(k, None)
    m4a, wav = notify._say_files(i)
    notify._say_prune()
    if not (m4a.exists() or wav.exists()):
        try:
            threading.Thread(target=notify._say_gen,
                             args=(text, str(m4a), voice), daemon=True).start()
        except Exception:
            return ""
    return "/api/pet/audio/" + i


def pet_audio_file(name: str, fmt: str = ""):
    if not NAME_RE.match(name or ""):
        return None
    m4a, wav = notify._say_files(name)
    # 板子只认 WAV：没合成好就 404，让它重试；别拿 m4a 冒充（头解析失败=白哑一句）
    order = (wav,) if fmt == "wav" else (m4a, wav)
    for p in order:
        if p.exists():
            return p
    return None


def persona_assets(persona) -> dict:
    return {"voice": persona.voice,
            "sounds": {k: pet_tts(v, persona.voice)
                       for k, v in persona.sounds.items()}}
