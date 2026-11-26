# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.director import PetDirector
from pet.presence import FakePresenceSampler, Presence
from pet.store import Store


class FakeClient:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, msg):
        self.sent.append(msg)


def make(tmp_path):
    store = Store(tmp_path / "pet.json")
    d = PetDirector(store, FakePresenceSampler(Presence(idle_s=5)),
                    tts=lambda t, v: "", direct=None, chat_llm=None,
                    now=lambda: 1000.0, sessions=lambda: [])
    return d


def test_devices_show_paired_offline_then_online(tmp_path):
    d = make(tmp_path)
    d.store.set("devices", {"desk": 123.0})
    got = d.devices()
    assert got == [{"name": "desk", "last_seen": 123.0, "online": False}]

    c = d.attach(FakeClient(), device="desk")
    assert d.devices() == [{"name": "desk", "last_seen": 123.0, "online": True}]

    d.detach(c)
    assert d.devices()[0]["online"] is False


def test_unknown_client_shows_as_unnamed(tmp_path):
    d = make(tmp_path)
    d.attach(FakeClient())
    got = d.devices()
    assert got == [{"name": "", "last_seen": 0.0, "online": True}]


def test_devices_route_registered():
    from aiohttp import web
    from pet import api
    app = web.Application()
    api.add_routes(app)
    assert "/api/pet/devices" in {r.resource.canonical for r in app.router.routes()}
