# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠线 HTTP / WebSocket 接口。

板子和模拟器用 pet token；手机（人格切换、偏好、手机在场）用 Host token。
"""

import asyncio
import json
import os
import time
from pathlib import Path

from aiohttp import web

from .auth import PairManager, pet_token, require_pet
from .audio import pet_audio_file, persona_assets
from .personas import get_persona, persona_list
from .presence import Presence

HERE = Path(__file__).resolve().parent
_PAIR = PairManager()


async def _require_either(request):
    import server
    try:
        await server.require_token(request)
        return
    except web.HTTPUnauthorized:
        first = None
    except web.HTTPForbidden as e:   # server 的坏 token 走 403
        first = e
    try:
        await require_pet(request)
    except web.HTTPUnauthorized:
        if first is not None:        # 带着 Host token 被额度拦了 → 原样返回
            raise first
        raise


def merge_phone_presence(d: dict, now: float) -> Presence:
    ts = float(d.get("ts") or now)
    moved = max(0.0, now - ts) if d.get("moving") else None
    return Presence(phone_present=bool(d.get("phone_present")),
                    phone_moved_s=moved, ts=now)


async def h_pet_state(request):
    await require_pet(request)
    return web.json_response(request.app["pet"].snapshot())


async def h_pet_devices(request):
    import server
    await server.require_token(request)
    pet = request.app["pet"]
    return web.json_response({"devices": pet.devices()})


async def h_pet_personas(request):
    await _require_either(request)
    return web.json_response({"personas": persona_list()})


async def h_pet_persona(request):
    await _require_either(request)
    d = await request.json()
    pid = str(d.get("id") or "")
    if pid not in {p["id"] for p in persona_list()}:
        return web.json_response({"ok": False, "error": "unknown persona"}, status=400)
    snap = request.app["pet"].set_pref(persona=pid)
    await request.app["pet"].broadcast({"type": "persona", "id": pid})
    await request.app["pet"].broadcast(cfg_msg(get_persona(pid)))
    return web.json_response({"ok": True, "state": snap})


async def h_pet_pref(request):
    await _require_either(request)
    d = await request.json()
    snap = request.app["pet"].set_pref(voice_on=d.get("voice_on"), dnd=d.get("dnd"))
    return web.json_response({"ok": True, "state": snap})


async def h_pet_assets(request):
    await require_pet(request)
    return web.json_response(persona_assets(get_persona(request.match_info["id"])))


async def h_pet_presence(request):
    import server
    await server.require_token(request)
    d = await request.json()
    p = merge_phone_presence(d, time.time())
    await request.app["pet"].note_phone(p.phone_present, p.phone_moved_s)
    return web.json_response({"ok": True})


async def h_pet_event(request):
    await require_pet(request)
    d = await request.json()
    t = str(d.get("t") or "")
    pet = request.app["pet"]
    ref = str(d.get("ref") or "")
    if t == "played":
        pet.played(ref)
    elif t == "ack":
        pet.ack(ref)
    elif t == "touch":
        pet.mark_touch()
    elif t == "heard":
        pet.mark_heard()
    elif t == "held":
        pet.mark_held()
    elif t == "offline":
        pet.mark_offline()
    else:
        return web.json_response({"ok": False, "error": "unknown t"}, status=400)
    return web.json_response({"ok": True})


async def h_pet_ws(request):
    await require_pet(request)
    pet = request.app["pet"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    client = pet.attach(ws.send_json, device=(request.query.get("dt") or "")[:24])
    try:
        await ws.send_json({"type": "hello", **pet.snapshot(),
                            "personas": persona_list()})
        await ws.send_json(cfg_msg(get_persona(pet.persona_id)))
        await pet.flush_queue()
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = str(d.get("t") or "")
            ref = str(d.get("ref") or "")
            if t == "played":
                pet.played(ref)
            elif t == "ack":
                pet.ack(ref)
            elif t == "touch":
                pet.mark_touch()
            elif t == "heard":
                pet.mark_heard()
            elif t == "ping":
                await ws.send_json({"type": "pong"})
    finally:
        pet.detach(client)
    return ws


async def h_pet_audio(request):
    await require_pet(request)
    name = request.match_info["name"]
    fmt = request.query.get("fmt") or ""
    p = pet_audio_file(name, fmt)
    # 刚回的那句是后台线程在合成：URL 先到、文件后到。板子收到回话立刻来取，
    # 以前这里直接 404 —— 她张了嘴却没声（「问她话没反馈」）。等它落盘再给。
    for _ in range(100):                      # 最多 10 s，正常 1~3 s 就有
        if p is not None:
            break
        await asyncio.sleep(0.1)
        p = pet_audio_file(name, fmt)
    if p is None:
        raise web.HTTPNotFound()
    ctype = "audio/mp4" if p.suffix == ".m4a" else "audio/wav"
    return web.FileResponse(p, headers={"Content-Type": ctype})


async def h_pair_start(request):
    import server
    await server.require_token(request)
    return web.json_response({"code": _PAIR.start(), "expires": _PAIR.ttl})


_DS_BASE = "https://dashscope.aliyuncs.com"


def llm_cfg(environ=None, keyfile=None) -> dict:
    """板子直连百炼（ASR/模型/TTS 全走 DashScope）用的钥匙。
    只看本机：DASHSCOPE_API_KEY，其次 ~/.codeboss/dashscope.key。
    没配就整块不给，板子继续只走 host。"""
    e = os.environ if environ is None else environ
    key = (e.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        p = keyfile or (Path.home() / ".codeboss" / "dashscope.key")
        try:
            key = Path(p).read_text().strip()
        except OSError:
            key = ""
    if not key:
        return {}
    return {"key": key, "base": (e.get("DASHSCOPE_BASE") or _DS_BASE).rstrip("/")}


def relay_fallback(rc: dict | None = None) -> dict:
    """板子连不上 host 时的中继兜底配置：中继地址 + 本机设备凭证。

    不另造一套凭证：host 自己在 relay devices 表里那台（已登记、多半已绑账号），
    配额和权限才对得上。没登记中继就整块不给，板子只走 host。"""
    if rc is None:
        try:
            import server
            rc = server._relay_cfg()
        except Exception:
            rc = {}
    base = str((rc or {}).get("url") or "").strip()
    dev = str((rc or {}).get("device_id") or "").strip()
    sec = str((rc or {}).get("device_secret") or "").strip()
    if not (base and dev and sec):
        return {}
    return {"base": base.rstrip("/"), "dev": dev, "sec": sec}


def pair_extra(persona, keycfg: dict | None = None,
               relay: dict | None = None) -> dict:
    """配对/连上时额外给板子的东西：人格（自己拼 prompt）+ 直连大模型的钥匙 +
    连不上 host 时的中继兜底凭证。

    只在校验过 6 位配对码之后才下发；没配钥匙就只给人格——半套配置会让
    板子以为能自己聊，结果每次都失败。"""
    out = {"persona": {"id": persona.id, "name": persona.name,
                       "identity": persona.identity, "voice": persona.voice}}
    kc = llm_cfg() if keycfg is None else keycfg
    if kc:
        out["llm"] = kc
    fb = relay_fallback(relay)
    if fb:
        out["fallback"] = fb
    return out


def cfg_msg(persona, keycfg: dict | None = None,
            relay: dict | None = None) -> dict:
    """随 hello 下发的直连配置（同 pair_extra）。已配对的板子重连就拿到，
    不用重新配网；换了人格也重发一遍。"""
    return {"type": "cfg", **pair_extra(persona, keycfg, relay)}


async def h_pair(request):
    d = await request.json()
    tok = _PAIR.claim(str(d.get("code") or ""), str(d.get("device") or ""))
    if not tok:
        raise web.HTTPForbidden(text="bad or expired code")
    pet = request.app["pet"]
    devs = dict(pet.store.get("devices") or {})
    devs[str(d.get("device") or "desk")] = time.time()
    pet.store.set("devices", devs)
    pet.save()
    body = {"ok": True, "token": tok}
    body.update(pair_extra(get_persona(pet.persona_id)))
    return web.json_response(body)


async def _memo_update(pet, hist, user, reply):
    """一轮聊完把新内容并进长期记忆；模型挂了就保留旧笔记。

    先让 host 自己那个 agent 会话（慢脑子）并：它攒着上下文，越聊越懂老板；
    它跑不了（没装/密钥不通/说太密）就退回中继那条快路径。"""
    try:
        from . import brain
        if await brain.learn(pet, hist, user, reply):
            return
    except Exception as e:
        print("[pet-brain] 跳过：%s" % str(e)[:120], flush=True)
    try:
        memo = await pet.memo_llm(pet.memo(), hist, user, reply)
        if (memo or "").strip():
            pet.save_memo(memo)
    except Exception:
        pass


_SPLIT_PUNCT = "，,。！!？?；;、…"


def split_line(line: str, min_head: int = 6, max_head: int = 26):
    """长句在第一个标点处拆两段：第一段先合成先开口，别等整句合成完才出声。

    合成一句要 0.7 s + 每字 ~0.04 s，四十个字就是两秒多的沉默——老板听到的
    是「她想了半天才张嘴」。拆开后第一声只等一句短话。"""
    s = (line or "").strip()
    if len(s) <= max_head:
        return s, ""
    for i, ch in enumerate(s):
        if ch in _SPLIT_PUNCT and i + 1 >= min_head:
            head, tail = s[:i + 1].strip(), s[i + 1:].strip()
            if tail:
                return head, tail
            break
    return s, ""


async def h_pet_chat(request):
    await require_pet(request)
    import server
    from .chat import ChatSession, _WAKE_WORD, is_bare_wake, is_non_speech

    pet = request.app["pet"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    src = ""      # 板子报的这句从哪来：win=已验证追问 / kws=本地唤醒词

    async def asr(raw):
        text, err, _ = await server._asr_via_relay(raw, "audio/wav")
        if text:
            # 转写一出来就告诉板子：她听见了、有没有喊她名字，板子据此给反馈
            try:
                await ws.send_json({"type": "heard", "text": text[:300]})
            except Exception:
                pass
        return text, err

    sess = None
    async for msg in ws:
        if msg.type == web.WSMsgType.BINARY:
            if sess is not None:
                sess.feed(msg.data)
            continue
        if msg.type != web.WSMsgType.TEXT:
            continue
        try:
            d = json.loads(msg.data)
        except ValueError:
            continue
        t = str(d.get("t") or "")
        if t == "start":
            src = str(d.get("src") or "")[:8]
            persona = get_persona(pet.persona_id)
            hist0 = pet.history()
            # 板子这句话开头报的现场（音量/亮度/电量）：她要知道「现在是多少」，
            # 也得据此判断把音量调到几。
            facts = pet.chat_facts()
            for k in ("vol", "bri", "bat"):
                if isinstance(d.get(k), (int, float)):
                    facts[k] = int(d[k])
            if pet.talk():                 # 老板调过的说法方式，重启也记得
                facts["talk"] = pet.talk()
            if pet.prefs():                # 老板提过的要求：每轮都带上
                facts["prefs"] = pet.prefs()

            def _reply(persona, history, text, facts):
                return pet.chat_llm(persona, history, text, facts, memo=pet.memo())

            sess = ChatSession(asr, _reply, pet.tts, persona, facts, hist0)
        elif t == "barge":
            if sess is not None:
                sess.cancel()
            await ws.send_json({"type": "stopped"})
        elif t == "end":
            if sess is None:
                continue
            # 诊断（2026-11-23）：长句被 ASR 转成一个语气词——已排除中继/削波
            # （24k 合成音频 27% 削顶仍完美转写），落带时间戳的原始 WAV 比对。
            # 只留最近 8 份，用完即删，不做留存。
            try:
                raw = sess.buf or b""
                import glob as _glob, uuid as _uuid
                p = "/tmp/pet_asr_%s.wav" % _uuid.uuid4().hex[:8]
                with open(p, "wb") as f:
                    f.write(raw)
                for old in sorted(_glob.glob("/tmp/pet_asr_*.wav"),
                                   key=os.path.getmtime)[:-8]:
                    os.unlink(old)
                print("[pet-asr] src=%s %d B -> %s" % (src or "-", len(raw), p),
                      flush=True)
            except Exception as e:
                print("[pet-asr-dump] 写不了：%s" % e, flush=True)
            text, err = await sess.transcribe()
            try:
                print("[pet-asr] src=%s -> %r" % (src or "-",
                      (err or text or "")[:80]), flush=True)
            except Exception:
                pass
            if err:
                sess = None
                await ws.send_json({"type": "error", "error": err})
                continue
            if is_bare_wake(text):
                # 只是喊了名字：heard 已让她点头，这里不接话、不合成——一开口，
                # 板子麦克风就被自己的声音占住，老板紧跟着问的那句会被吞掉。
                sess = None
                continue
            if is_non_speech(text):
                # 键盘/椅子/风扇转出来的「嗯。」：回一句「我没听清」她就会一直念，
                # 板子再把这句话听成噪声。什么都不说，让她回待机。
                sess = None
                await ws.send_json({"type": "stopped"})
                continue
            if src == "kws" and not _WAKE_WORD.search(text or ""):
                # 本地唤醒词放进来的这句，转写里却没有名字——误唤醒
                # （2026-11-23 实测本地模型只要有人说话就 0.94+）。
                # 哪个端问的哪个端答：不是在对她说，就静默回去，不接话
                # 不合成不记历史。
                sess = None
                await ws.send_json({"type": "stopped"})
                continue
            assets = persona_assets(get_persona(pet.persona_id))
            await ws.send_json({"type": "thinking",
                                "audio": (assets.get("sounds") or {}).get("think", "")})
            r = await sess.respond(text)
            if sess.facts.get("talk") and sess.facts["talk"] != pet.talk():
                pet.set_talk(sess.facts["talk"])
            if r.pref:
                pet.add_pref(r.pref)
            sess = None
            if r.ok:
                pet.save_chat(r.text, r.line)
                # 长期记忆异步并进去：慢一点没关系，别让老板等
                asyncio.get_event_loop().create_task(
                    _memo_update(pet, hist0, r.text, r.line))
                head, tail = split_line(r.line)
                audio = r.audio
                voice = get_persona(pet.persona_id).voice
                if tail:
                    # 先合成前半句（板子立刻就能开口），后半句随后排队播
                    audio = pet.tts(head, voice)
                msg = {"type": "reply", "text": r.text,
                       "line": r.line, "audio": audio,
                       "emotion": r.emotion, "gesture": r.gesture}
                if r.dev:
                    msg.update(r.dev)      # vol/bri 跟着回复一起下去，板子直接调
                await ws.send_json(msg)
                if tail:
                    await ws.send_json(
                        {"type": "part", "text": tail,
                         "audio": pet.tts(tail, voice)})
            else:
                await ws.send_json({"type": "error", "error": r.error})
    return ws


async def h_sim(request):
    text = (HERE / "sim.html").read_text(encoding="utf-8")
    return web.Response(text=text, content_type="text/html")


def _local(request) -> bool:
    return request.remote in ("127.0.0.1", "::1", None)


async def h_sim_presence(request):
    if not _local(request):
        raise web.HTTPForbidden()
    d = await request.json()
    pet = request.app["pet"]
    if "idle_s" in d:
        # 模拟器直接定在场：不用等 Mac 真空 90 秒
        pet.set_presence_override(Presence(idle_s=float(d.get("idle_s") or 0),
                                           locked=bool(d.get("locked"))))
    else:
        pet.set_presence_override(None)
        p = merge_phone_presence(d, time.time())
        await pet.note_phone(p.phone_present, p.phone_moved_s)
    return web.json_response({"ok": True})


async def h_sim_event(request):
    if not _local(request):
        raise web.HTTPForbidden()
    import notify
    d = await request.json()
    kind = str(d.get("kind") or "perm")
    title = str(d.get("title") or "模拟项目")[:24]
    notify._notify_add("sim", kind, "模拟：" + title, title=title,
                       about=title, voice=True)
    return web.json_response({"ok": True, "seq": notify._NOTIFY_SEQ[0]})


def add_routes(app):
    r = app.router
    r.add_get("/api/pet/state", h_pet_state)
    r.add_get("/api/pet/devices", h_pet_devices)
    r.add_get("/api/pet/personas", h_pet_personas)
    r.add_post("/api/pet/persona", h_pet_persona)
    r.add_post("/api/pet/pref", h_pet_pref)
    r.add_get("/api/pet/persona/{id}/assets", h_pet_assets)
    r.add_post("/api/pet/presence", h_pet_presence)
    r.add_post("/api/pet/event", h_pet_event)
    r.add_get("/api/pet/ws", h_pet_ws)
    r.add_get("/api/pet/chat", h_pet_chat)
    r.add_get("/api/pet/audio/{name}", h_pet_audio)
    r.add_post("/api/pet/pair/start", h_pair_start)
    r.add_post("/api/pet/pair", h_pair)
    r.add_get("/pet/sim", h_sim)
    r.add_post("/api/pet/sim/presence", h_sim_presence)
    r.add_post("/api/pet/sim/event", h_sim_event)
