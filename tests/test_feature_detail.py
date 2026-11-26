# SPDX-License-Identifier: AGPL-3.0-or-later

"""埋点要能回答「在哪个页面、点了什么、停了多久」（2026-11-21 用户）。

原来只有事件名（page_open / view_switch / settings_open），看不出「打开的是哪个
页面」「切到哪个视图」「设置里点了哪一项」，也没有停留时长。现在每条事件带两个
维度：pg=当时在哪个页面，k=点了哪一项/切到哪个值；页面停留用 ev=visit（次数）
和 ev=dwell（n=秒）记，平均停留 = dwell / visit。

跑法： .venv/bin/python -m pytest tests/test_feature_detail.py -q
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


def _db():
    return relay.db_init(":memory:")


def _today():
    return time.strftime("%Y-%m-%d")


def _add(db, evs, uid=1, cid="c1", prod="codeboss", ver="0.9.85", plat="ios"):
    app = {"db": db}
    return relay._track_add(app, uid, cid, prod, ver, plat, evs)


def _rows(db):
    return db.execute(
        "SELECT day,ev,pg,k,n FROM feat_day ORDER BY ev,pg,k").fetchall()


def test_rows_carry_page_and_item():
    db = _db()
    assert _add(db, [["view_switch", "raw", "sess", 1],
                     ["dwell", "", "sess", 12],
                     ["visit", "", "home", 1]]) == 3
    got = {(r[1], r[2], r[3]): r[4] for r in _rows(db)}
    assert got[("view_switch", "sess", "raw")] == 1
    assert got[("dwell", "sess", "")] == 12, "dwell 的 n 就是秒数"
    assert got[("visit", "home", "")] == 1


def test_same_event_different_page_or_item_are_separate_rows():
    db = _db()
    _add(db, [["visit", "", "home", 1], ["visit", "", "sess", 1],
              ["set_tap", "推送设置", "set", 1], ["set_tap", "音量", "set", 2]])
    rows = {(r[1], r[2], r[3]): r[4] for r in _rows(db)}
    assert rows[("visit", "home", "")] == 1 and rows[("visit", "sess", "")] == 1
    assert rows[("set_tap", "set", "推送设置")] == 1
    assert rows[("set_tap", "set", "音量")] == 2, "同一细项累加"


def test_legacy_two_element_events_still_work():
    db = _db()
    assert _add(db, [["page_open", 3], ["key", 1]]) == 2
    rows = {(r[1], r[2], r[3]): r[4] for r in _rows(db)}
    assert rows[("page_open", "", "")] == 3
    assert rows[("key", "", "")] == 1


def test_bad_detail_is_dropped_not_fatal():
    db = _db()
    # 细项只收「界面上的名字」：中文/字母/数字/下划线/空格，长度有限。
    # 明细不合法只丢这一维，计数照记（别因为一个标签拼错把真实使用量也丢掉）。
    assert _add(db, [["set_tap", "<script>", "set", 1],
                     ["set_tap", "x" * 80, "set", 1],
                     ["dwell", "", "sess", 5]]) == 2
    rows = {(r[1], r[2], r[3]): r[4] for r in _rows(db)}
    assert rows[("set_tap", "set", "")] == 2
    assert rows[("dwell", "sess", "")] == 5


def test_migration_keeps_old_rows(monkeypatch, tmp_path):
    """线上表是老结构（没有 pg/k）：db_init 要能原地升级，旧数据留在 '' 维度上。"""
    import sqlite3
    old = sqlite3.connect(str(tmp_path / "relay.db"))
    old.executescript(
        "CREATE TABLE feat_day(day TEXT NOT NULL, prod TEXT NOT NULL DEFAULT '', "
        "ev TEXT NOT NULL, ver TEXT NOT NULL DEFAULT '', "
        "plat TEXT NOT NULL DEFAULT '', uid INTEGER NOT NULL DEFAULT 0, "
        "cid TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0, "
        "ts REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY(day, prod, ev, ver, plat, uid, cid));"
        "INSERT INTO feat_day(day,prod,ev,ver,plat,uid,cid,n,ts) "
        "VALUES('%s','codeboss','page_open','0.9.84','ios',7,'c9',4,0);"
        % _today())
    old.commit()
    old.close()
    db = relay.db_init(str(tmp_path / "relay.db"))
    r = db.execute("SELECT ev,pg,k,n,uid FROM feat_day").fetchone()
    assert r == ("page_open", "", "", 4, 7), r
    # 新结构要能写进去（主键含水）
    _add(db, [["visit", "", "home", 1]])
    assert db.execute("SELECT pg FROM feat_day WHERE ev='visit'").fetchone()[0] == "home"


def test_analytics_reports_pages_and_item_breakdown(monkeypatch):
    db = _db()
    _add(db, [["visit", "", "home", 1], ["dwell", "", "home", 30],
              ["visit", "", "sess", 2], ["dwell", "", "sess", 100],
              ["set_tap", "推送设置", "set", 3],
              ["view_switch", "raw", "sess", 2]])
    monkeypatch.setattr(relay, "_adm_auth", _ta)
    d = json.loads(asyncio.run(relay.r_admin_analytics(_Req(db))).text)
    assert d["ok"] is True
    pg = {p["pg"]: p for p in d["pages"]}
    assert pg["home"]["visits"] == 1 and pg["home"]["secs"] == 30
    assert pg["sess"]["visits"] == 2 and pg["sess"]["secs"] == 100
    assert pg["sess"]["avg"] == 50, "平均停留 = 秒数 / 访问次数"
    det = {(x["ev"], x["pg"], x["k"]): x["n"] for x in d["evdet"]}
    assert det[("set_tap", "set", "推送设置")] == 3
    assert det[("view_switch", "sess", "raw")] == 2
    for k in ("today", "daily", "fev", "byprod", "byver", "byplat"):
        assert k in d, "老的返回字段不能丢"
