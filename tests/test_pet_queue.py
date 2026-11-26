# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.director import PetDirector
from pet.presence import FakePresenceSampler, Presence
from pet.store import Store


class FakeClient:
    def __init__(self): self.sent = []
    async def send(self, msg): self.sent.append(msg)


def make(tmp_path, idle=5):
    store = Store(tmp_path / "pet.json")
    sampler = FakePresenceSampler(Presence(idle_s=idle))
    d = PetDirector(store, sampler, tts=lambda t, v: "/api/pet/audio/x",
                    direct=None, chat_llm=None, now=lambda: 1000.0)
    c = FakeClient()
    d.attach(c)
    return d, c, sampler


def perm(id=1, sid="s1"):
    return {"id": id, "sid": sid, "kind": "perm", "title": "改配置", "about": "在改配置", "ts": 1.0}


def done(id=2, sid="s1"):
    return {"id": id, "sid": sid, "kind": "done", "title": "改配置", "about": "跑完了", "ts": 2.0}


def test_away_queues_and_return_delivers_one(tmp_path):
    d, c, sampler = make(tmp_path, idle=60)
    asyncio.run(d.consume([perm()]))
    assert len(d.store.get("queue")) == 1
    sampler.set(Presence(idle_s=3))
    asyncio.run(d.tick(1, [], now=1006))
    ds = asyncio.run(d.flush_queue())
    assert len(ds) == 1 and ds[0].decision.mode == "speak"
    assert d.store.get("queue") == []
    assert asyncio.run(d.flush_queue()) == []


def test_pump_delivers_queue_on_return(tmp_path):
    # 回桌补报不能只靠客户端重连触发；主循环每一拍都要补
    d, c, sampler = make(tmp_path, idle=60)
    asyncio.run(d.consume([perm()]))
    sampler.set(Presence(idle_s=3))
    ds = asyncio.run(d.pump(1, [], now=1006))
    assert len(ds) == 1 and ds[0].decision.mode == "speak"
    assert d.store.get("queue") == []


def test_done_drops_resolved_queue(tmp_path):
    d, c, sampler = make(tmp_path, idle=60)
    asyncio.run(d.consume([perm(1)]))
    asyncio.run(d.consume([done(2)]))
    assert d.store.get("queue") == []
    sampler.set(Presence(idle_s=3))
    assert asyncio.run(d.flush_queue()) == []


def test_return_reports_highest_priority_only(tmp_path):
    d, c, sampler = make(tmp_path, idle=60)
    asyncio.run(d.consume([{"id": 1, "sid": "s1", "kind": "stuck", "title": "A"}, perm(2, "s2")]))
    sampler.set(Presence(idle_s=3))
    asyncio.run(d.tick(2, [], now=1006))
    ds = asyncio.run(d.flush_queue())
    assert len(ds) == 1 and ds[0].event.priority == 0
    assert len(d.store.get("queue")) == 1


def test_due_repeats_and_ack_stops(tmp_path):
    d, c, sampler = make(tmp_path)
    asyncio.run(d.consume([perm()]))
    assert d.due_repeats(1000) == []
    assert d.due_repeats(1120) == ["n1"]
    out = asyncio.run(d.repeat_pending(1121))
    assert len(out) == 1 and out[0].event.ref == "n1"
    d.ack("n1")
    assert d.due_repeats(2000) == []


def test_touch_pauses_speech(tmp_path):
    d, c, sampler = make(tmp_path)
    d.mark_touch()
    ds = asyncio.run(d.consume([perm()]))
    assert ds[0].decision.mode == "queue"


def test_played_is_not_resolution(tmp_path):
    d, c, sampler = make(tmp_path)
    asyncio.run(d.consume([perm()]))
    d.played("n1")
    assert d.arb.speaking is False
    assert d.due_repeats(1120) == ["n1"]
    d.ack("n1")
    assert d.due_repeats(2000) == []


def _says(c):
    return [m for m in c.sent if m.get("type") == "say"]


def test_voice_off_does_not_flush(tmp_path):
    # 静音只挡了新事件进场，回桌补报也不能把队列念出来
    d, c, sampler = make(tmp_path)
    d.arb.voice_on = False
    asyncio.run(d.consume([perm()]))
    assert len(d.queue) == 1
    asyncio.run(d.pump(1, [], now=1006))
    assert len(d.queue) == 1 and _says(c) == []


def test_touching_does_not_flush(tmp_path):
    d, c, sampler = make(tmp_path)
    d.mark_touch()
    asyncio.run(d.consume([perm()]))
    assert len(d.queue) == 1
    asyncio.run(d.pump(1, [], now=1006))
    assert len(d.queue) == 1 and _says(c) == []


def test_dnd_does_not_flush(tmp_path):
    import time
    d, c, sampler = make(tmp_path)
    h = time.localtime(1000.0).tm_hour
    d.arb.dnd = (h, (h + 2) % 24)
    asyncio.run(d.consume([perm()]))
    assert len(d.queue) == 1
    asyncio.run(d.pump(1, [], now=1006))
    assert len(d.queue) == 1 and _says(c) == []


def test_queue_overflow_keeps_most_urgent(tmp_path):
    d, c, sampler = make(tmp_path, idle=60)
    low = [{"id": i, "sid": "s%d" % i, "kind": "progress",
            "title": "T%d" % i, "ts": float(i)} for i in range(1, 21)]
    asyncio.run(d.consume(low))
    assert len(d.queue) == 20
    asyncio.run(d.consume([perm(id=99, sid="urgent")]))
    assert len(d.queue) == 20
    assert any(e["wire"]["sid"] == "urgent" for e in d.queue)
