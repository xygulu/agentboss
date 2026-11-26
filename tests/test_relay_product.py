# SPDX-License-Identifier: AGPL-3.0-or-later

"""relay 产品线列（users.product / devices.product，缺省 codeboss）+ 透出。

跑法： .venv/bin/python -m pytest tests/test_relay_product.py -q
范式同 tests/test_relay_takeover.py：import relay + db_init(":memory:")。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _cols(db, table):
    return {r[1]: (r[3], r[4]) for r in
            db.execute("PRAGMA table_info(%s)" % table).fetchall()}


def test_migration_adds_product_columns():
    db = relay.db_init(":memory:")
    for table in ("users", "devices"):
        info = _cols(db, table)
        assert "product" in info, table
        # notnull=0 且默认 'codeboss'
        assert info["product"][1] == "'codeboss'", table


def test_device_product_roundtrip():
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')"
                     ).lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner,product) "
               "VALUES('d1','dev1','sec',?, 'agentboss')", (uid,))
    db.execute("INSERT INTO devices(id,name,secret,owner) "
               "VALUES('d2','dev2','sec',?)", (uid,))
    app = {"links": {}, "direct": {}}
    out = relay._devices_payload(app, db, uid)
    prods = {d["name"]: d.get("product") for d in out["devices"]}
    assert prods["dev1"] == "agentboss"
    assert prods["dev2"] == "codeboss"      # 缺省回落


def test_register_persists_product(tmp_path, monkeypatch):
    import asyncio, json
    db = relay.db_init(":memory:")

    class _Req:   # r_register 只吃 json()/remote/app/scheme
        def __init__(self, data):
            self._d = data
            self.app = {"db": db, "key": b"test-key"}
            self.remote = "127.0.0.1"
            self.scheme = "http"
        async def json(self):
            return self._d

    async def _pv(*a, **k): return True   # _phone_verify 是协程
    monkeypatch.setattr(relay, "rate_ok", lambda *a, **k: True)
    monkeypatch.setattr(relay, "_phone_verify", _pv)
    monkeypatch.setattr(relay, "promo_grant", lambda *a: (0, ""))
    monkeypatch.setattr(relay, "_alert_cfg", lambda db: {"reg": 0})

    async def _go(data):
        return await relay.r_register(_Req(data))

    out = asyncio.run(_go({"phone": "13800000001", "code": "123456", "product": "agentboss"}))
    assert json.loads(out.text)["ok"] is True
    row = db.execute("SELECT product FROM users WHERE phone='13800000001'").fetchone()
    assert row[0] == "agentboss"

    out = asyncio.run(_go({"phone": "13800000002", "code": "123456", "product": "nope"}))
    assert json.loads(out.text)["ok"] is True
    row = db.execute("SELECT product FROM users WHERE phone='13800000002'").fetchone()
    assert row[0] == "codeboss"     # 非法 product 回落默认（Review Focus 1）


def test_legal_pages_swap_brand_for_agentboss():
    import asyncio

    class _Req:
        def __init__(self, q):
            self.query = q

    # 2026-11-18 改成系列口径：正文两端都列（aiboss 系列产品），标题仍按线换端名。
    text = asyncio.run(relay.r_terms(_Req({"product": "agentboss"}))).text
    assert "aiboss 系列" in text
    assert "口袋工头" in text and "代理工头" in text       # 系列里两端都点名
    assert "AgentBoss · 服务条款" in text                  # 标题按线换
    text2 = asyncio.run(relay.r_terms(_Req({}))).text      # 无参 = codeboss 标题
    assert "CodeBoss · 服务条款" in text2 and "口袋工头" in text2
    text3 = asyncio.run(relay.r_privacy(_Req({"product": "agentboss"}))).text
    assert "aiboss 系列" in text3 and "AgentBoss · 隐私政策" in text3
