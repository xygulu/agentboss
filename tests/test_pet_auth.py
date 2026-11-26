# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from pet.auth import PairManager, pet_token, require_pet


def test_token_file_is_private_and_stable(tmp_path):
    p = tmp_path / "pet_token.txt"
    t = pet_token(p)
    assert len(t) == 64
    assert pet_token(p) == t
    assert p.stat().st_mode & 0o777 == 0o600


def test_require_pet_accepts_header_and_query(monkeypatch):
    monkeypatch.setattr("pet.auth.pet_token", lambda *a, **k: "abc")
    req = make_mocked_request("GET", "/api/pet/state", headers={"X-Pet-Token": "abc"})
    asyncio.run(require_pet(req))
    req2 = make_mocked_request("GET", "/api/pet/state?pt=abc")
    asyncio.run(require_pet(req2))


def test_require_pet_rejects_wrong(monkeypatch):
    monkeypatch.setattr("pet.auth.pet_token", lambda *a, **k: "abc")
    req = make_mocked_request("GET", "/api/pet/state", headers={"X-Pet-Token": "no"})
    try:
        asyncio.run(require_pet(req))
        assert False, "should raise"
    except web.HTTPUnauthorized:
        pass


def test_pair_code_one_time_and_expiry():
    pm = PairManager(ttl=600)
    code = pm.start(now=100)
    assert len(code) == 6 and code.isdigit()
    tok = pm.claim(code, "desk-1", now=110)
    assert tok
    assert pm.claim(code, "desk-1", now=111) is None
    assert pm.claim(pm.start(now=200), "desk-1", now=900) is None
