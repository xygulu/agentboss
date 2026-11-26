# SPDX-License-Identifier: AGPL-3.0-or-later

"""收银台建号 + 设备认领：匿名设备付费后归到账号，当日用量并入账号主体；
已认领指纹的设备不能再回到匿名档。

跑法： .venv/bin/python -m pytest tests/test_claim_device.py -q
"""
import asyncio
import sqlite3
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,created) "
               "VALUES('d1','书房 Mac','sec','abc123',1,NULL,'codeboss',0)")
    db.commit()
    return db


def _seed_usage(db, dev="d1"):
    subj = relay.subject_of(db, None, dev)
    relay.usage_add(db, relay._llm_key(db, None, dev), 3)
    relay._asr_quota(db, dev, add=40)
    relay.use_act(db, subj, relay._min_now(), "relay", dev=dev, act="发消息")
    relay.usage_add(db, "use:%s:d:%s" % (subj, time.strftime("%Y-%m-%d")), 30)


def test_claim_moves_owner_and_merges_usage(tmp_path):
    db = _db(tmp_path)
    _seed_usage(db)
    assert relay._claim_device(db, 7, "d1") is True
    row = db.execute("SELECT owner,anon FROM devices WHERE id='d1'").fetchone()
    assert row == (7, 0)
    day = time.strftime("%Y-%m-%d")
    assert relay.usage_get(db, "llm:u7:d:%s" % day) == 3
    assert relay.usage_get(db, "asr:u7:d:%s" % day) == 40
    assert relay.usage_get(db, "use:u7:d:%s" % day) == 30
    assert relay.sends_used(db, 7) == 1
    # 旧主体清空：不重复计
    assert relay.usage_get(db, relay._llm_key(db, None, "d1")) == 0
    assert relay.sends_used(db, relay.subject_of(db, None, "d1")) == 0


def test_claim_is_idempotent_for_same_account(tmp_path):
    db = _db(tmp_path)
    assert relay._claim_device(db, 7, "d1") is True
    assert relay._claim_device(db, 7, "d1") is True
    assert relay._claim_device(db, 8, "d1") is False     # 别人的设备不抢


def _pay_req(db, body, dev="d1"):
    tok = relay._dev_sess_make(db, dev)
    return types.SimpleNamespace(
        app={"db": db, "key": b"k", "links": {}},
        cookies={"cb_dev_sess": tok}, headers={"Host": "relay.test"},
        host="relay.test", scheme="http", remote="203.0.113.9", query={},
        path="/r/pay/create", method="POST", rel_url=types.SimpleNamespace(raw_path_qs=""),
        json=lambda: _await(body))


async def _await(v):
    return v


def _zpay_ok(monkeypatch):
    monkeypatch.setattr(relay, "ZPAY_PID", "pid")
    monkeypatch.setattr(relay, "ZPAY_KEY", "key")
    monkeypatch.setattr(relay, "_zpay_post", lambda path, params: {"code": 1, "payurl": "p"})
    monkeypatch.setattr(relay, "SMS_CONSOLE", True)


def test_anon_checkout_creates_account_claims_device(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed_usage(db)
    _zpay_ok(monkeypatch)
    db.execute("INSERT INTO phone_codes(phone,out_id,purpose,created,expires,used) "
               "VALUES('13800138000','123456','register',?,?,0)", (time.time(), time.time() + 300))
    db.commit()
    req = _pay_req(db, {"plan": "month", "type": "alipay",
                        "phone": "13800138000", "code": "123456"})
    resp = asyncio.run(relay.r_pay_create(req))
    assert resp.status == 200
    body = __import__("json").loads(resp.body)
    assert body["ok"] is True
    row = db.execute("SELECT owner,anon FROM devices WHERE id='d1'").fetchone()
    assert row[0] is not None and row[1] == 0
    uid = row[0]
    assert db.execute("SELECT COUNT(*) FROM orders WHERE uid=? AND sku='month'",
                      (uid,)).fetchone()[0] == 1
    assert relay.sends_used(db, uid) == 1, "当日用量并入账号主体"
    assert any(c.key == "r_sess" for c in resp.cookies.values()), "下单即登录"


def test_anon_checkout_without_device_session_is_rejected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _zpay_ok(monkeypatch)
    req = _pay_req(db, {"plan": "month", "phone": "13800138000", "code": "123456"})
    req.cookies = {}
    resp = asyncio.run(relay.r_pay_create(req))
    assert resp.status == 401


def test_claimed_fp_enroll_stays_owned(tmp_path):
    db = _db(tmp_path)
    relay._claim_device(db, 7, "d1")

    def enroll(body):
        req = types.SimpleNamespace(
            app={"db": db}, headers={}, remote="203.0.113.10",
            json=lambda: _await(body))
        return asyncio.run(relay.r_enroll(req))

    # 带旧 secret：重装/换 secret 复用原设备，归属不变
    resp = enroll({"fp": "abc123", "old_secret": "sec", "product": "codeboss"})
    body = __import__("json").loads(resp.body)
    assert body.get("device_id") == "d1"
    assert db.execute("SELECT owner,anon FROM devices WHERE id='d1'").fetchone() == (7, 0)

    # 不带旧 secret：config 丢了也不能匿名换手，走后台人工解绑
    resp = enroll({"fp": "abc123", "product": "codeboss"})
    assert resp.status == 403
    assert db.execute("SELECT COUNT(*) FROM devices WHERE fp='abc123'").fetchone()[0] == 1


def test_claim_invalidates_device_session(tmp_path):
    """认领后旧的匿名设备会话作废：否则同一浏览器清掉 r_sess 就变回 d<dev>，
    用量已被合并清零 → 再拿一份完整的免费额度。"""
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    assert relay._dev_sess_read(db, tok) == "d1"
    relay._claim_device(db, 7, "d1")
    assert relay._dev_sess_read(db, tok) is None


def test_enroll_reuses_device_across_product_lines(tmp_path):
    """同一台机器换产品线登记不能变成第二台设备（否则再拿一份免费额度）。"""
    db = _db(tmp_path)

    def enroll(body):
        async def _j():
            return body
        req = types.SimpleNamespace(app={"db": db}, headers={}, remote="203.0.113.11",
                                    json=_j)
        return asyncio.run(relay.r_enroll(req))

    body = __import__("json").loads(enroll({"fp": "abc123", "product": "agentboss"}).text)
    assert body["device_id"] == "d1"
    assert db.execute("SELECT COUNT(*) FROM devices WHERE fp='abc123'").fetchone()[0] == 1
    assert db.execute("SELECT product FROM devices WHERE id='d1'").fetchone()[0] == "agentboss"
