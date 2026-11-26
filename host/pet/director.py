# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠导演：消费 notify 事件，决定谁说、说什么，再下发给板子。

Host 是唯一真相；这里只管“这一秒要不要开口”，不碰 agent 状态。
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import date

from .arbiter import ArbiterState, Decision, decide, repeat_delay
from .audio import pet_tts
from .events import coalesce_key, from_notify, merge
from .gate import in_dnd
from .gate import update as gate_update
from .lines import Line, line_for, pick
from .llm import chat_reply as llm_chat, update_memo as llm_memo
from .llm import direct as llm_direct
from .personas import DEFAULT_PERSONA, PERSONAS, get_persona
from .presence import PresenceState, PresenceTracker
from .protocol import PetEvent


def _default_sessions():
    import server
    return server._cached_sessions()


@dataclass
class Delivery:
    event: PetEvent
    decision: Decision
    line: Line


class Client:
    def __init__(self, send, device=""):
        self.send = send
        self.alive = True
        self.device = device


class PetDirector:
    def __init__(self, store, sampler, tts=pet_tts, direct=llm_direct,
                 chat_llm=llm_chat, memo_llm=llm_memo, asr=None, now=time.time,
                 sessions=None):
        self.store = store
        self.sampler = sampler
        self.tts = tts
        self.direct = direct
        self.chat_llm = chat_llm
        self.memo_llm = memo_llm
        self.asr = asr
        self.now = now
        self._sessions = sessions or _default_sessions
        self.seen_seq = int(store.get("seen_seq") or 0)
        self.persona_id = store.get("persona") or DEFAULT_PERSONA
        self.queue = [x for x in (store.get("queue") or [])
                      if isinstance(x, dict) and "wire" in x]
        self.arb = ArbiterState(voice_on=bool(store.get("voice_on", True)),
                                dnd=tuple(store.get("dnd") or (23, 8)))
        self.tracker = PresenceTracker()
        self.presence = PresenceState(False, False, True, 0, "init")
        self.clients = []
        self.waiting = {}
        self.chat_until = 0.0
        self._last_sample = 0.0
        self._override = None   # 模拟器/测试固定在场状态，压过真实采样
        self._speaking_ref = ""
        self._by_ref = {}
        self.pending_repeats = {}
        self._task = None
        mem = store.get("mem") or {}
        self.done_today = int(mem.get("done_today") or 0)
        self._done_date = mem.get("done_date") or ""

    # ---- 客户端 ----
    def attach(self, send, device="") -> Client:
        # 收裸回调或带 .send 的客户端对象都行（模拟器/测试直接传对象）
        c = Client(send if callable(send) else send.send, device=device)
        self.clients.append(c)
        gate_update(online=True)
        return c

    def detach(self, client):
        if client in self.clients:
            self.clients.remove(client)
        gate_update(online=bool(self.clients))

    async def broadcast(self, msg: dict):
        for c in list(self.clients):
            try:
                await c.send(msg)
            except Exception:
                c.alive = False
                if c in self.clients:
                    self.clients.remove(c)
        gate_update(online=bool(self.clients))

    # ---- 感知 ----
    async def _refresh(self, now, force=False):
        if not force and now - self._last_sample < 5:
            return
        self._last_sample = now
        if self._override is not None:
            p = self._override
        else:
            loop = asyncio.get_event_loop()
            try:
                p = await loop.run_in_executor(None, self.sampler.sample)
            except Exception:
                return
        self.presence = self.tracker.update(p, now)
        gate_update(online=bool(self.clients), at_desk=self.presence.at_desk,
                    voice_on=self.arb.voice_on, dnd=self.arb.dnd,
                    hour=time.localtime(now).tm_hour)

    def _expire_speech(self, now):
        if self.arb.speaking and now > self.arb.speaking_until:
            self.arb.speaking = False
            self.arb.speaking_pri = 99

    # ---- 事件 ----
    async def consume(self, n_events, now=None):
        now = now or self.now()
        await self._refresh(now)
        self._expire_speech(now)
        mapped = [m for m in (from_notify(e) for e in n_events or []) if m]
        skip = set()
        for m in mapped:
            if m.kind in ("perm", "ask", "input"):
                self.waiting[m.sid] = now
            if m.kind == "done":
                self._count_done()
                if self._drop_resolved(m.sid) > 0:
                    skip.add(m.ref)
        groups = {}
        for m in mapped:
            if m.ref in skip:
                continue
            k = coalesce_key(m)
            groups[k] = merge(groups[k], m) if k in groups else m
        out = []
        for m in sorted(groups.values(), key=lambda x: (x.priority, x.ts)):
            out.append(await self._deliver(m, now))
        return out

    def _count_done(self):
        today = date.today().isoformat()
        if self._done_date != today:
            self._done_date = today
            self.done_today = 0
        self.done_today += 1
        mem = self.store.get("mem") or {}
        mem.update(done_date=self._done_date, done_today=self.done_today)
        self.store.set("mem", mem)

    async def _deliver(self, m, now):
        hour = time.localtime(now).tm_hour
        d = decide(m, self.presence, self.arb, now, hour)
        line = line_for(get_persona(self.persona_id), m)
        if d.mode == "speak" and not self.clients:
            d = Decision("queue", d.repeat_s, "no_client", d.announce)
        if d.mode == "speak" and self.direct is not None and m.priority <= 1:
            try:
                got = await asyncio.wait_for(
                    self.direct(get_persona(self.persona_id), m, self.chat_facts(),
                                timeout=3.0), 3.2)
                if got:
                    line = got
            except Exception:
                pass
        m.line, m.emotion, m.gesture = line.text, line.emotion, line.gesture
        m.badge = line.badge or m.badge
        self._by_ref[m.ref] = (m.sid, m.kind)
        if d.mode == "speak":
            m.audio = self.tts(line.text, get_persona(self.persona_id).voice)
            self._speaking_ref = m.ref
            self.arb.speaking = True
            self.arb.speaking_pri = m.priority
            self.arb.speaking_until = now + max(2, len(line.text) / 5)
            self.arb.same_ref_spoken[m.ref] = now
            if d.repeat_s > 0:
                self.pending_repeats[m.ref] = {"wire": m.wire(), "n": 0,
                                               "at": now + d.repeat_s}
            await self.broadcast({"type": "say", **m.wire()})
        elif d.mode == "queue":
            self._enqueue(m, line)
        elif d.announce and not self.arb.speaking:
            await self.broadcast({"type": "badge", "badge": m.wire().get("badge"),
                                  "ref": m.ref})
        return Delivery(m, d, line)

    def _enqueue(self, m, line):
        self.queue.append({"wire": m.wire(),
                           "line": {"text": line.text, "emotion": line.emotion,
                                    "gesture": line.gesture},
                           "ts": self.now()})
        if len(self.queue) > 20:
            # 丢「优先级最低 + 最旧」的一条：pri 大 = 不重要，所以按 -pri 升序排
            self.queue.sort(key=lambda e: (-int(e["wire"].get("pri") or 0), e.get("ts") or 0))
            self.queue.pop(0)
        self.save()

    def _drop_resolved(self, sid) -> int:
        before = len(self.queue)
        self.queue = [e for e in self.queue
                      if not (e["wire"].get("sid") == sid
                              and int(e["wire"].get("pri") or 0) <= 1)]
        for ref, p in list(self.pending_repeats.items()):
            if p["wire"].get("sid") == sid:
                self.pending_repeats.pop(ref, None)
        self.waiting.pop(sid, None)
        if len(self.queue) != before:
            self.save()
        return before - len(self.queue)

    async def flush_queue(self):
        if not self.clients or not self.presence.at_desk or self.arb.speaking:
            return []
        if not self.queue:
            return []
        # 补报也得守静音：免打扰 / 关了语音 / 刚被摸过头，都不能把队列倒出来
        now = self.now()
        if (not self.arb.voice_on
                or in_dnd(time.localtime(now).tm_hour, self.arb.dnd)
                or self.arb.touching_until > now):
            return []
        self.queue.sort(key=lambda e: (int(e["wire"].get("pri") or 0), e.get("ts") or 0))
        entry = self.queue.pop(0)
        ev = PetEvent.from_wire(entry["wire"])
        l = entry.get("line") or {}
        line = Line(l.get("text") or ev.title, l.get("emotion") or "calm",
                    l.get("gesture") or "none", ev.badge)
        ev.line, ev.emotion, ev.gesture = line.text, line.emotion, line.gesture
        ev.audio = self.tts(line.text, get_persona(self.persona_id).voice)
        delay = repeat_delay(ev.priority, 0)
        if delay > 0:
            self.pending_repeats[ev.ref] = {"wire": ev.wire(), "n": 0, "at": now + delay}
        self._speaking_ref = ev.ref
        self.arb.speaking = True
        self.arb.speaking_pri = ev.priority
        self.arb.speaking_until = now + max(2, len(line.text) / 5)
        await self.broadcast({"type": "say", **ev.wire()})
        self.save()
        return [Delivery(ev, Decision("speak", delay, "return", False), line)]

    def due_repeats(self, now) -> list:
        self._expire_speech(now)
        due = [(p["at"], ref) for ref, p in self.pending_repeats.items() if p["at"] <= now]
        return [ref for _, ref in sorted(due)]

    async def repeat_pending(self, now):
        self._expire_speech(now)
        if self.arb.speaking:
            return []
        out = []
        for ref in self.due_repeats(now):
            p = self.pending_repeats.get(ref)
            if not p:
                continue
            ev = PetEvent.from_wire(p["wire"])
            p["n"] += 1
            ev.count = p["n"] + 1
            line = pick(get_persona(self.persona_id), ev.kind,
                        {"title": ev.title or "那个项目", "n": ev.count},
                        "%s:r%d" % (ref, p["n"]))
            ev.line, ev.emotion, ev.gesture = line.text, line.emotion, line.gesture
            ev.badge = line.badge or ev.badge
            ev.audio = self.tts(line.text, get_persona(self.persona_id).voice)
            self._speaking_ref = ref
            self.arb.speaking = True
            self.arb.speaking_pri = ev.priority
            self.arb.speaking_until = now + max(2, len(line.text) / 5)
            await self.broadcast({"type": "say", **ev.wire()})
            nxt = repeat_delay(ev.priority, p["n"])
            if nxt > 0:
                p["at"] = now + nxt
            else:
                self.pending_repeats.pop(ref, None)
            out.append(Delivery(ev, Decision("speak", nxt, "repeat", False), line))
        return out

    def played(self, ref):
        if ref and ref == self._speaking_ref:
            self._speaking_ref = ""
            self.arb.speaking = False
            self.arb.speaking_pri = 99
            self.arb.speaking_until = 0.0

    def ack(self, ref):
        now = self.now()
        self.arb.speaking = False
        self.arb.speaking_pri = 99
        self.arb.speaking_until = 0.0
        self._speaking_ref = ""
        got = self._by_ref.get(ref)
        if got:
            sid, kind = got
            self.arb.last_acked["%s:%s" % (sid, kind)] = now
            self.waiting.pop(sid, None)
        self.queue = [e for e in self.queue if e["wire"].get("ref") != ref]
        self.pending_repeats.pop(ref, None)
        self.save()

    # ---- 交互信号 ----
    def mark_heard(self):
        self.chat_until = self.now() + 8

    def mark_touch(self):
        self.arb.touching_until = self.now() + 20

    def mark_held(self):
        self.arb.touching_until = self.now() + 20
        if not self.presence.at_desk:
            self.presence = PresenceState(True, False, False, 0, "held")

    def mark_offline(self):
        self.presence = PresenceState(False, True, False, 0, "offline")

    def set_presence_override(self, p):
        """模拟器专用：固定在场状态，传 None 恢复真实采样。"""
        self._override = p

    async def note_phone(self, present, moved_s):
        self.sampler.note_phone(present, moved_s, self.now())
        await self._refresh(self.now(), force=True)

    # ---- 给聊天/板子的事实 ----
    def agent_counts(self) -> dict:
        try:
            running = sum(1 for x in self._sessions() if x.get("state") == "busy")
        except Exception:
            running = 0
        return {"running": running, "waiting": len(self.waiting)}

    def chat_facts(self) -> dict:
        now = self.now()
        return {"done_today": self.done_today, "queue": len(self.queue),
                "at_desk": self.presence.at_desk,
                "hour": time.localtime(now).tm_hour, **self.agent_counts()}

    def snapshot(self) -> dict:
        fact = self.chat_facts()
        return {"persona": self.persona_id, "voice_on": self.arb.voice_on,
                "dnd": list(self.arb.dnd), "queue": fact["queue"],
                "done_today": fact["done_today"], "at_desk": fact["at_desk"],
                "hour": fact["hour"], "running": fact["running"],
                "waiting": fact["waiting"]}

    def devices(self) -> list:
        """host 软件里展示「接上了哪些桌宠」：配过对的 + 此刻连着的。"""
        now_names = {c.device for c in self.clients if c.device}
        out = [{"name": n, "last_seen": float(ts or 0),
                "online": n in now_names}
               for n, ts in (self.store.get("devices") or {}).items()]
        for n in sorted(now_names - {d["name"] for d in out}):
            out.append({"name": n, "last_seen": 0.0, "online": True})
        for c in self.clients:
            if not c.device:
                out.append({"name": "", "last_seen": 0.0, "online": True})
        return out

    def history(self) -> list:
        mem = self.store.get("mem") or {}
        return list(mem.get("chat") or [])

    def memo(self) -> str:
        """长期记忆：老板是谁、喜好、生活里的事（聊天时自然带上，不念笔记）。"""
        return str((self.store.get("mem") or {}).get("memo") or "")

    def prefs(self) -> list:
        """老板提过的要求（「别啰嗦」「叫我哥」）：每轮聊天都带给她，别让他说第二遍。"""
        return [str(x) for x in (self.store.get("prefs") or []) if str(x).strip()][-8:]

    def add_pref(self, text):
        t = str(text or "").strip()[:60]
        if not t:
            return
        prefs = [p for p in self.prefs() if p != t] + [t]
        self.store.set("prefs", prefs[-8:])
        self.save()

    def talk(self) -> str:
        """老板调过她的说法方式没有（「太啰嗦了」）：记住，别每句都重新被嫌。"""
        t = self.store.get("talk")
        return t if t in ("brief", "normal") else ""

    def set_talk(self, talk):
        if talk in ("brief", "normal"):
            self.store.set("talk", talk)
            self.save()

    def save_memo(self, text):
        mem = self.store.get("mem") or {}
        mem["memo"] = str(text or "").strip()[:800]
        self.store.set("mem", mem)
        self.save()

    def save_chat(self, user, assistant):
        mem = self.store.get("mem") or {}
        chat = list(mem.get("chat") or [])
        chat += [{"role": "user", "text": user}, {"role": "assistant", "text": assistant}]
        mem["chat"] = chat[-24:]   # 存 12 轮，和 llm.chat_reply 带过去的窗口一致
        self.store.set("mem", mem)
        self.save()

    def set_pref(self, persona=None, voice_on=None, dnd=None) -> dict:
        if persona in PERSONAS:
            self.persona_id = persona
            self.store.set("persona", persona)
        if voice_on is not None:
            self.arb.voice_on = bool(voice_on)
            self.store.set("voice_on", self.arb.voice_on)
        if dnd is not None:
            try:
                a, b = int(dnd[0]), int(dnd[1])
                if 0 <= a <= 23 and 0 <= b <= 23:
                    self.arb.dnd = (a, b)
                    self.store.set("dnd", [a, b])
            except (TypeError, ValueError, IndexError):
                pass
        self.save()
        return self.snapshot()

    def save(self):
        self.store.set("queue", list(self.queue))
        self.store.set("seen_seq", self.seen_seq)
        self.store.save()

    # ---- 主循环 ----
    async def tick(self, seq, events, now=None):
        now = now or self.now()
        await self._refresh(now)
        if seq < self.seen_seq:
            self.seen_seq = 0
        new = [e for e in events or [] if int(e.get("id") or 0) > self.seen_seq]
        if new:
            self.seen_seq = max(self.seen_seq, max(int(e.get("id") or 0) for e in new))
            self.save()
        return await self.consume(new, now=now)

    async def pump(self, seq, events, now=None):
        """主循环一拍：吃新事件、到点重提、回桌补报队列。"""
        now = now or self.now()
        out = await self.tick(seq, events, now=now)
        await self.repeat_pending(now)
        out += await self.flush_queue()
        return out

    def _in_use(self) -> bool:
        """用过桌宠才算「在用」：配对过设备，或有客户端连着。"""
        return bool(self.clients) or bool(self.store.get("devices"))

    async def _loop(self):
        import notify
        armed = False
        while True:
            try:
                if self._in_use():
                    if not armed:
                        armed = True
                        # 第一次用：之前积压的通知手机已经处理过，不补念
                        self.seen_seq = notify._NOTIFY_SEQ[0]
                    await self.pump(notify._NOTIFY_SEQ[0], list(notify._NOTIFY_Q))
            except Exception as e:
                print("[pet] loop: %s" % str(e)[:120], flush=True)
            await asyncio.sleep(1.0)

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
