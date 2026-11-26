# SPDX-License-Identifier: AGPL-3.0-or-later

"""后台「可用端」：用户卡片能选 agentboss / codeboss（2026-11-19 用户实报缺入口）。

接口 set_products 早就有，缺的是后台页面上的控件和 users 列表里的当前值。
三态语义（_allowed_products）：NULL=按套餐、空串=两条线都关、CSV=逐账号指定。

跑法： .venv/bin/python -m pytest tests/test_admin_user_products.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


async def _ta(request, hard=False):
    return True


class _Req:
    def __init__(self, db, data=None, query=None):
        self._d = data or {}
        self.app = {"db": db}
        self.headers = {}
        self.cookies = {}
        self.query = query or {}

    async def json(self):
        return self._d


def _db():
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')").lastrowid
    return db, uid


def test_users_list_exposes_products_override(monkeypatch):
    db, uid = _db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    out = asyncio.run(relay.r_admin_users(_Req(db, query={"q": "u"})))
    u = [x for x in json.loads(out.text)["users"] if x["id"] == uid][0]
    assert "products" in u and u["products"] is None, "NULL=按套餐，页面要能看出没覆盖"


def test_set_products_three_states(monkeypatch):
    db, uid = _db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    monkeypatch.setattr(relay, "_adm_sensitive", _ta)

    def run(products):
        out = asyncio.run(relay.r_admin_user(_Req(db, {"action": "set_products",
                                                       "id": uid, "products": products})))
        return json.loads(out.text)

    assert run("agentboss")["ok"] is True
    assert db.execute("SELECT products FROM users WHERE id=?", (uid,)).fetchone()[0] == "agentboss"
    assert run("codeboss,agentboss")["ok"] is True
    assert db.execute("SELECT products FROM users WHERE id=?", (uid,)).fetchone()[0] == \
        "codeboss,agentboss"
    assert run("")["ok"] is True, "空串=两条线都关，不能当脏值回落到按套餐"
    assert db.execute("SELECT products FROM users WHERE id=?", (uid,)).fetchone()[0] == ""
    assert run(None)["ok"] is True, "None=恢复按套餐"
    assert db.execute("SELECT products FROM users WHERE id=?", (uid,)).fetchone()[0] is None


def test_users_list_shows_real_asr_caps(monkeypatch):
    """后台语音上限要按套餐显示（之前写死 0=不限，免费用户也看着「不限」，
    排查「Max 说额度用完」时后台和运行时对不上）。"""
    import time
    db, uid = _db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)

    def caps():
        out = asyncio.run(relay.r_admin_users(_Req(db, query={"q": "u"})))
        u = [x for x in json.loads(out.text)["users"] if x["id"] == uid][0]
        return u["asr_day_cap"], u["asr_month_cap"]

    assert caps() == (180, 5400), "免费档 180 秒/天、5400 秒/月"
    db.execute("UPDATE users SET plan='max',plan_expires=? WHERE id=?",
               (time.time() + 30 * 86400, uid))
    db.commit()
    assert caps() == (0, 0), "Max 不限"


def test_admin_page_has_products_control():
    """页面控件和保存函数都得在：接口有、界面没有就还是「后台没法选」（用户实报）。"""
    src = (HERE / "relay" / "relay.py").read_text(encoding="utf-8")
    assert "id=\"uprod-" in src and "uSetProducts" in src
    assert "action:'set_products'" in src
