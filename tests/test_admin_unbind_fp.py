# SPDX-License-Identifier: AGPL-3.0-or-later

"""后台人工解绑（指纹变更通道）：解绑后设备回到匿名档，同 fp 可重新 enroll；
每次解绑落 admin_audit。

跑法： .venv/bin/python -m pytest tests/test_admin_unbind_fp.py -q
"""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db():
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')").lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner,anon,fp,product) "
               "VALUES('d1','书房 Mac','sec',?,0,'abc123','codeboss')", (uid,))
    db.commit()
    return db, uid


async def _ta(request):
    return True


class _AReq:
    def __init__(self, db, data, code=""):
        self._d = data
        self.app = {"db": db, "links": {}}
        self.headers = {"x-adm-code": code} if code else {}
        self.cookies = {}
        self.remote = "203.0.113.9"

    async def json(self):
        return self._d


def _enroll(db, body):
    async def _j():
        return body
    req = types.SimpleNamespace(app={"db": db}, headers={}, remote="203.0.113.10",
                                json=_j)
    return asyncio.run(relay.r_enroll(req))


def test_unbind_needs_sms_and_leaves_device_anonymous(monkeypatch):
    db, uid = _db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    relay._ADM_OK_UNTIL = 0.0
    relay._ADM_CODE_OK.update(code="", exp=0.0)

    # 1) 改用户资产要当次短信码
    out = asyncio.run(relay.r_admin_device(_AReq(db, {"action": "unbind", "id": "d1"})))
    body = json.loads(out.text)
    assert body["ok"] is False and body.get("need_sms") is True

    # 2) 带有效码 → owner 清空、回到匿名档
    relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)
    out = asyncio.run(relay.r_admin_device(
        _AReq(db, {"action": "unbind", "id": "d1"}, code="654321")))
    assert json.loads(out.text)["ok"] is True
    assert db.execute("SELECT owner,anon FROM devices WHERE id='d1'").fetchone() == (None, 1)

    # 3) 审计落库：动作、原账号
    row = db.execute("SELECT action,target_uid FROM admin_audit ORDER BY rowid DESC"
                     ).fetchone()
    assert row == ("device_unbind", uid)


def test_same_fp_reenrolls_as_anonymous_after_unbind(monkeypatch):
    db, uid = _db()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    relay._ADM_OK_UNTIL = 0.0
    relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)
    out = asyncio.run(relay.r_admin_device(
        _AReq(db, {"action": "unbind", "id": "d1"}, code="654321")))
    assert json.loads(out.text)["ok"] is True

    # 不带旧 secret：原本会被 403 挡掉，解绑后同一 fp 可以重新登记成匿名设备
    resp = _enroll(db, {"fp": "abc123", "product": "codeboss"})
    assert resp.status == 200
    assert json.loads(resp.text)["device_id"] == "d1"
    assert db.execute("SELECT owner,anon FROM devices WHERE id='d1'").fetchone() == (None, 1)
    assert db.execute("SELECT COUNT(*) FROM devices WHERE fp='abc123'"
                      ).fetchone()[0] == 1
