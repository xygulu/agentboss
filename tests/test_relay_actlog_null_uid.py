# SPDX-License-Identifier: AGPL-3.0-or-later

"""后台「动作明细」遇到 uid 为 NULL 的老行会 500（2026-11-21 线上）。

use_act_log 现在的 DDL 是 uid INTEGER NOT NULL，但线上那张表建得早、没有这个约束，
历史行里确实有 uid=NULL 的（登录前/匿名动作）。明细路由假设 uid 一定有值，
int(None) 直接抛异常，整页挂掉。

这里按线上真实表形状复现：建一张没有 NOT NULL 的 use_act_log，塞一行 NULL。

跑法： .venv/bin/python -m pytest tests/test_relay_actlog_null_uid.py -q
"""
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


async def _ta(request, hard=False):
    return True


class _Req:
    def __init__(self, db, query=None):
        self.app = {"db": db}
        self.headers = {}
        self.cookies = {}
        self.query = query or {}


def _legacy_db_with_null_uid():
    db = relay.db_init(":memory:")
    # 线上老表：同样的列，但 uid 没有 NOT NULL
    db.execute("DROP TABLE use_act_log")
    db.execute(
        "CREATE TABLE use_act_log("
        "uid INTEGER, m INTEGER NOT NULL, src TEXT NOT NULL, dev TEXT,"
        "cid TEXT, ua TEXT, kind TEXT, act TEXT, path TEXT, n INTEGER DEFAULT 1,"
        "first REAL, last REAL, PRIMARY KEY(uid,m,src,dev,cid,act,path))")
    now = int(time.time() // 60)
    db.execute("INSERT INTO use_act_log(uid,m,src,act,n) VALUES(NULL,?,?,?,?)",
               (now, "relay", "按键", 3))
    db.commit()
    return db


def test_actlog_survives_null_uid_rows(monkeypatch):
    db = _legacy_db_with_null_uid()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    out = asyncio.run(relay.r_admin_actlog(_Req(db)))
    d = json.loads(out.text)
    assert d["ok"] is True, "匿名老行不能把整页明细打挂"
    assert d["count"] == 1
    row = d["rows"][0]
    assert row["uid"] == 0, "匿名行按 uid 0 显示"
    assert row["act"] == "按键" and row["n"] == 3
    assert d["total_n"] == 3, "汇总也得算上匿名行"


# ── 主体编码：use_act_log.uid 里混着三种写法（2026-11-21）──
# 账号是 'u<id>'、匿名设备是 'd<dev>'（subject_of 的编码，隧道每一条都这么写），
# 老数据才是整数 id。路由以前一律 int(uid) → 'u12' 直接 ValueError，这个页面
# 实际上从来没打开过（NULL 只是其中一种）。
def _db_with_subjects():
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('alice','h','s')"
                     ).lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner) VALUES('dve1','macmini4','s',?)",
               (uid,))
    now = int(time.time() // 60)
    db.executemany(
        "INSERT INTO use_act_log(uid,m,src,dev,cid,act,n) VALUES(?,?,?,?,?,?,?)",
        [("u%d" % uid, now, "relay", "dve1", "c1", "发消息", 2),
         ("dabc123", now, "relay", "", "", "按键", 1),
         (uid, now, "relay", "", "", "按键", 1)])   # 老数据：整数 id
    db.commit()
    return db, uid


def test_actlog_understands_subject_ids(monkeypatch):
    db, uid = _db_with_subjects()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    d = json.loads(asyncio.run(relay.r_admin_actlog(_Req(db))).text)
    assert d["ok"] is True
    got = {(r["act"], r["n"]): r for r in d["rows"]}
    acc = got[("发消息", 2)]
    assert acc["uid"] == uid, "'u<id>' 要按账号 id 显示"
    assert acc["user"] == "alice", "名字要能对上（JOIN 认主体编码）"
    assert got[("按键", 1)]["uid"] in (uid, "dabc123"), "老整数行和匿名主体都照常出来"


def test_actlog_uid_filter_matches_subject_ids(monkeypatch):
    db, uid = _db_with_subjects()
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    d = json.loads(asyncio.run(
        relay.r_admin_actlog(_Req(db, query={"uid": str(uid)}))).text)
    assert d["ok"] is True
    assert d["count"] == 2, "按 uid 筛要同时命中 'u<id>' 和老整数行"
    assert all(r["uid"] == uid for r in d["rows"])
