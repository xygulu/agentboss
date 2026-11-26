# SPDX-License-Identifier: AGPL-3.0-or-later

"""用户/开发机/终端各状态下的闭环：删开发机、删账号、冻结、超额、匿名免费设备。

本轮修的四条：
① 匿名免费设备（owner 空 + anon=1）不能被「未绑定就踢下线」的巡检误杀；
② 删账号后他名下的开发机回到匿名免费档（anon=1）并立刻断链，host 按免费档重连；
③ 老的无主设备（anon=0）重新 enroll 时应转成匿名设备，否则永远 409 连不上；
④ host 被拒只在凭证失效（401/403）时清本机注册；冻结/超额是可恢复的，不能让那台
   机器被永久注销（解冻/升级后回不来）。

跑法： .venv/bin/python -m pytest tests/test_device_state_loops.py -q
"""
import asyncio
import json
import sqlite3
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
import server


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    return sqlite3.connect(p)


def _dev(db, did, owner=None, anon=0, fp=""):
    db.execute("INSERT INTO devices(id,name,secret,owner,anon,fp,product,created) "
               "VALUES(?,?,?,?,?,?,'codeboss',0)", (did, did, "s-" + did, owner, anon, fp))
    db.commit()


class _Ws:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


# ① 巡检只踢「无主且不是匿名免费档」的设备；删除（行都没了）也踢
def test_orphan_sweep_keeps_anonymous_devices(tmp_path):
    db = _db(tmp_path)
    _dev(db, "d1", anon=1)             # 匿名免费设备：允许在线
    _dev(db, "d2", anon=0)             # 老的无主设备：踢
    w1, w2, w3 = _Ws(), _Ws(), _Ws()
    app = {"db": db, "links": {"d1": w1, "d2": w2, "d3": w3}}   # d3 已被删

    kicked = asyncio.run(relay._orphan_sweep_step(app))

    assert sorted(kicked) == ["d2", "d3"]
    assert "d1" in app["links"] and not w1.closed
    assert "d2" not in app["links"] and w2.closed
    assert "d3" not in app["links"] and w3.closed


# ② 删账号：名下开发机变匿名免费档、立刻断链（不再等巡检）
async def _ta(request):
    return True


class _AReq:
    def __init__(self, app, data, code=""):
        self._d = data
        self.app = app
        self.headers = {"x-adm-code": code} if code else {}
        self.cookies = {}
        self.remote = "203.0.113.9"

    async def json(self):
        return self._d


def test_user_delete_frees_devices_to_anonymous_and_drops_link(tmp_path, monkeypatch):
    db = _db(tmp_path)
    uid = db.execute("INSERT INTO users(name,pass_hash,salt,created) "
                     "VALUES('u','h','s',0)").lastrowid
    _dev(db, "d1", owner=uid)
    _dev(db, "d2", owner=uid)
    w1 = _Ws()
    app = {"db": db, "key": b"k", "links": {"d1": w1}, "direct": {"d1": {"lan": []}}}
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    relay._ADM_CODE_OK.update(code="654321", exp=time.time() + 60)

    out = asyncio.run(relay.r_admin_user(
        _AReq(app, {"action": "delete", "id": uid, "confirm": "u"}, "654321")))
    assert json.loads(out.text)["ok"] is True

    assert db.execute("SELECT id,owner,anon FROM devices ORDER BY id").fetchall() == \
        [("d1", None, 1), ("d2", None, 1)]
    assert "d1" not in app["links"] and w1.closed, "删账号要立刻断链，不能等巡检"
    assert "d1" not in app["direct"]


# ③ 老无主设备重新 enroll → 转匿名免费档，host 才能连上来
def test_reenroll_legacy_unowned_device_becomes_anonymous(tmp_path):
    db = _db(tmp_path)
    _dev(db, "d1", anon=0, fp="abc123")

    async def _j():
        return {"fp": "abc123", "product": "codeboss"}

    req = types.SimpleNamespace(app={"db": db}, headers={}, remote="203.0.113.10",
                                json=_j)
    resp = asyncio.run(relay.r_enroll(req))
    assert resp.status == 200
    assert db.execute("SELECT anon FROM devices WHERE id='d1'").fetchone()[0] == 1


# ④ 凭证失效才清本机注册；冻结/超额是临时拒绝，host 要继续重连
def test_host_wipe_only_on_credential_denials():
    assert server._link_deny_wipe(401) is True
    assert server._link_deny_wipe(403) is True
    assert server._link_deny_wipe(423) is False    # 冻结：解冻后自动恢复
    assert server._link_deny_wipe(429) is False    # 开发机数超额：升级/移除后恢复


# ⑤ 终端身份没了（匿名设备被删/会话失效）：页面立刻退回中继入口（扫码/接入页）
def test_client_exits_when_identity_lost():
    src = (HERE / "host" / "static" / "src" / "js" / "05_cmd.js").read_text(encoding="utf-8")
    assert "(r.status===401||r.status===403)&&!r.json" in src, \
        "401 也要像 403 一样退回入口，否则匿名设备被删后还挂着旧会话列表"
