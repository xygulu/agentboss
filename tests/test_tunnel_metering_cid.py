# SPDX-License-Identifier: AGPL-3.0-or-later

"""host 在线时记账不能再崩（2026-11-21 线上手机一直「断网」）。

`tunnel()` 里 `use_act(..., cid=cid)` 和 `request["_cb_cid"] = cid` 引用的 `cid`
只存在于从未注册过的 `_svc_dim_mw` 里。请求一走到记账就 NameError，aiohttp 兜底
500，手机整页停在断网页——匿名页、离线 host、`/` 都碰不到这一段，所以一直看不出来。

这段只有 host 在线才走得到（计时点在 host-link 校验之后），以前的用例
（test_seat_and_hosts）host 都是离线，覆盖不到。
"""
import asyncio
import sqlite3
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

QIN_UA = ("Dalvik/2.1.0 (Linux; U; Android 9; Qin 2 Pro)"
          " CodebossShell/0.11.27")


class _Req(types.SimpleNamespace):
    """aiohttp 的 Request 是可变映射：tunnel 会往里记 _cb_uid/_cb_cid/_cb_dev。"""
    def __init__(self, db):
        super().__init__(
            app={"db": db, "key": b"k", "links": {"d1": _FakeWs()},
                 "presence": {}, "presence_n": {}},
            cookies={"cb_dev": "d1"}, path="/d", method="GET",
            query={}, headers={"User-Agent": QIN_UA}, scheme="http",
            remote="203.0.113.9",
            rel_url=types.SimpleNamespace(raw_path_qs="/d"),
            _stash={},
        )

    def __getitem__(self, k):
        return self._stash[k]

    def __setitem__(self, k, v):
        self._stash[k] = v

    def get(self, k, default=None):
        return self._stash.get(k, default)


class _FakeWs:
    closed = False

    async def send_json(self, obj):
        pass


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.execute("INSERT INTO devices(id,name,secret,last_seen,owner,product)"
               " VALUES('d1','d1','s',0,7,'codeboss')")
    db.commit()
    return db


def test_online_host_metering_records_cid_without_crashing(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "uid_of", lambda r: 7)
    fwd = {}

    async def fake_plain(request, ws, dev):
        from aiohttp import web
        fwd["dev"] = dev
        return web.Response(text="ok")

    monkeypatch.setattr(relay, "_tunnel_plain", fake_plain)
    req = _Req(db)
    resp = asyncio.run(relay.tunnel(req))
    assert resp.status == 200 and fwd["dev"] == "d1"
    row = db.execute("SELECT cid, dev FROM use_act_log WHERE uid='u7'").fetchall()
    assert row, "host 在线时每一条请求都要在动作表里留痕"
    cid = row[0][0]
    assert cid.startswith("i:"), "没有 cb_cid 时退回 IP+UA 的稳定键（和 clients 一致）"
    assert req.get("_cb_cid").startswith(cid) and req.get("_cb_uid") == 7
