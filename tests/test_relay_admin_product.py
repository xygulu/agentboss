#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""后台设备动作 set_product：存量设备翻产品线（短信码闸沿用 rename/freeze 同一闸）。

跑法： .venv/bin/python -m pytest tests/test_relay_admin_product.py -q
鉴权范式照 tests/test_relay_stepup.py：_ADM_CODE_OK 复用码窗口，免真发短信。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _dev_db():
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')").lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner) VALUES('d1','dev','sec',?)", (uid,))
    return db


async def _ta(request):
    return True


class _AReq:
    def __init__(self, db, data, code=""):
        self._d = data
        self.app = {"db": db}
        self.headers = {"x-adm-code": code} if code else {}
        self.cookies = {}

    async def json(self):
        return self._d


def test_admin_set_product(monkeypatch):
    db = _dev_db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    relay._ADM_OK_UNTIL = 0.0
    relay._ADM_CODE_OK.update(code="", exp=0.0)

    # 1) 无短信码 → 需要验证码（同 rename/freeze 一道闸）
    out = asyncio.run(relay.r_admin_device(
        _AReq(db, {"action": "set_product", "id": "d1", "product": "agentboss"})))
    body = json.loads(out.text)
    assert body["ok"] is False and body.get("need_sms") is True

    # 2) 复用码窗口内带有效码 → 翻线成功
    relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)
    out = asyncio.run(relay.r_admin_device(
        _AReq(db, {"action": "set_product", "id": "d1", "product": "agentboss"}, code="654321")))
    assert json.loads(out.text)["ok"] is True
    assert db.execute("SELECT product FROM devices WHERE id='d1'").fetchone()[0] == "agentboss"

    # 3) 非法 product → 清洗回落 codeboss
    out = asyncio.run(relay.r_admin_device(
        _AReq(db, {"action": "set_product", "id": "d1", "product": "bogus"}, code="654321")))
    assert json.loads(out.text)["ok"] is True
    assert db.execute("SELECT product FROM devices WHERE id='d1'").fetchone()[0] == "codeboss"
