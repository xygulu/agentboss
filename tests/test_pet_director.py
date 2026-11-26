# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.director import PetDirector
from pet.lines import Line
from pet.personas import get_persona
from pet.presence import FakePresenceSampler, Presence
from pet.store import Store


class FakeClient:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, msg):
        self.sent.append(msg)


def make(tmp_path, idle=5, persona="xiaogongtou", sessions=lambda: []):
    store = Store(tmp_path / "pet.json")
    store.set("persona", persona)
    sampler = FakePresenceSampler(Presence(idle_s=idle))
    d = PetDirector(store, sampler, tts=lambda text, voice: "/api/pet/audio/x",
                    direct=None, chat_llm=None, now=lambda: 1000.0, sessions=sessions)
    c = FakeClient()
    d.attach(c)
    return d, c


def perm(id=1, sid="s1", title="改配置"):
    return {"id": id, "sid": sid, "kind": "perm", "title": title, "about": "在改配置", "ts": 1.0}


def done(id=2, sid="s1", title="改配置"):
    return {"id": id, "sid": sid, "kind": "done", "title": title, "about": "跑完了", "ts": 2.0}


def test_at_desk_perm_speaks_with_audio(tmp_path):
    d, c = make(tmp_path)
    ds = asyncio.run(d.consume([perm()]))
    assert len(ds) == 1 and ds[0].decision.mode == "speak"
    assert c.sent[-1]["type"] == "say"
    assert c.sent[-1]["audio"].startswith("/api/pet/audio/")
    assert "改配置" in c.sent[-1]["line"]


def test_two_events_same_sid_coalesce(tmp_path):
    d, c = make(tmp_path)
    ds = asyncio.run(d.consume([perm(1), perm(2)]))
    assert len(ds) == 1
    assert ds[0].event.count == 2
    assert len([m for m in c.sent if m["type"] == "say"]) == 1


def test_two_done_events_do_not_talk_over(tmp_path):
    d, c = make(tmp_path)
    ds = asyncio.run(d.consume([done(1, "s1"), done(2, "s2")]))
    assert len(ds) == 2
    assert ds[0].decision.mode == "speak"
    assert ds[1].decision.mode == "queue"


def test_ack_clears_speaking_and_dedupes(tmp_path):
    d, c = make(tmp_path)
    asyncio.run(d.consume([perm(1)]))
    d.ack("n1")
    ds = asyncio.run(d.consume([perm(3)]))
    assert ds[0].decision.mode == "silence"


def test_seen_seq_resets_when_host_restarts(tmp_path):
    d, c = make(tmp_path)
    d.seen_seq = 10
    ds = asyncio.run(d.tick(5, [{"id": 1, "sid": "s1", "kind": "perm", "title": "A"}]))
    assert len(ds) == 1


def test_tick_ignores_already_seen(tmp_path):
    d, c = make(tmp_path)
    d.seen_seq = 5
    assert asyncio.run(d.tick(5, [{"id": 5, "sid": "s1", "kind": "perm", "title": "A"}])) == []


def test_done_increments_completed_today(tmp_path):
    d, c = make(tmp_path)
    asyncio.run(d.consume([done()]))
    assert d.snapshot()["done_today"] == 1


def test_chat_facts_counts_running_and_waiting(tmp_path):
    sessions = lambda: [{"sid": "s1", "state": "busy"}, {"sid": "s2", "state": "idle"}]
    d, c = make(tmp_path, sessions=sessions)
    ds = asyncio.run(d.consume([perm(1, "s1"), perm(2, "s2")]))
    f = d.chat_facts()
    assert f["running"] == 1 and f["waiting"] == 2
    d.ack(ds[0].event.ref)
    assert d.chat_facts()["waiting"] == 1


def test_memo_defaults_empty_and_persists(tmp_path):
    """长期记忆（老板的事）存在 pet store 的 mem.memo：host 重启、板子重连都还在。"""
    d, _ = make(tmp_path)
    assert d.memo() == ""
    d.save_memo("老板爱喝冰美式；女儿六岁")
    d2, _ = make(tmp_path)
    assert d2.memo() == "老板爱喝冰美式；女儿六岁"
