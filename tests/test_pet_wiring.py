# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import pet
from pet import api
from pet.director import PetDirector
from pet.presence import FakePresenceSampler, Presence
from pet.store import Store


def _director(tmp_path, idle=5):
    return PetDirector(Store(tmp_path / "pet.json"),
                       FakePresenceSampler(Presence(idle_s=idle)),
                       tts=lambda t, v: "", direct=None, chat_llm=None,
                       now=lambda: 1000.0, sessions=lambda: [])


def test_pet_add_routes_is_idempotent_on_fresh_app():
    app = web.Application()
    pet.add_routes(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/pet/state" in paths
    assert "/api/pet/ws" in paths
    assert "/api/pet/chat" in paths


def test_sim_html_exists_and_has_controls():
    html = (Path(HERE) / "host" / "pet" / "sim.html").read_text(encoding="utf-8")
    assert "WebSocket" in html
    assert "桌宠" in html
    assert "/api/pet/sim/event" in html


def test_pet_package_never_approves_or_dispatches():
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (Path(HERE) / "host" / "pet").glob("*.py"))
    assert "perm_answer" not in src


def test_sim_presence_idle_drives_director_away(tmp_path):
    d = _director(tmp_path)
    app = web.Application()
    app["pet"] = d
    req = make_mocked_request("POST", "/api/pet/sim/presence", app=app)

    async def body():
        return {"idle_s": 120}
    req.json = body
    assert asyncio.run(api.h_sim_presence(req)).status == 200
    asyncio.run(d._refresh(1000.0, force=True))
    assert d.presence.away is True
    assert d._override.idle_s == 120


class CountingSampler(FakePresenceSampler):
    def __init__(self, p=None):
        super().__init__(p)
        self.calls = 0

    def sample(self):
        self.calls += 1
        return super().sample()


def _live(tmp_path, sampler, devices=None, now=lambda: 1000.0):
    store = Store(tmp_path / "pet.json")
    if devices:
        store.set("devices", devices)
    return PetDirector(store, sampler, tts=lambda t, v: "", direct=None,
                       chat_llm=None, now=now, sessions=lambda: [])


def _run_loop(d, secs=0.05):
    async def go():
        await d.start()
        await asyncio.sleep(secs)
        await d.stop()
    asyncio.run(go())


def test_no_pet_use_no_sampling(tmp_path):
    # 没配对过、也没人连：主循环不许采样、不许吃事件
    sampler = CountingSampler(Presence(idle_s=5))
    d = _live(tmp_path, sampler)
    _run_loop(d)
    assert sampler.calls == 0


def test_paired_device_arms_sampling(tmp_path):
    sampler = CountingSampler(Presence(idle_s=5))
    d = _live(tmp_path, sampler, devices={"desk": 1.0})
    _run_loop(d)
    assert sampler.calls >= 1


def test_client_attach_arms_sampling(tmp_path):
    sampler = CountingSampler(Presence(idle_s=5))
    d = _live(tmp_path, sampler)
    d.attach(lambda m: None)
    _run_loop(d)
    assert sampler.calls >= 1


def test_first_use_skips_backlog(tmp_path):
    # 放着不管时 notify 攒下的旧事，第一次连上不能一口气补念
    import notify
    notify._NOTIFY_Q.clear()
    notify._NOTIFY_SEQ[0] = 0
    notify._notify_add("sim", "done", "旧事", title="旧事", voice=False)
    try:
        d = _live(tmp_path, CountingSampler(Presence(idle_s=60)))
        d.attach(lambda m: None)
        _run_loop(d)
        assert d.seen_seq == 1
        assert d.queue == [] and d.store.get("queue") == []
    finally:
        notify._NOTIFY_Q.clear()
        notify._NOTIFY_SEQ[0] = 0
