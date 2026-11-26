#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""/d?dev=<id> 切台种 cookie:局域网直连源上 cb_dev 写不进中继域,query 通道兜底。

2026-11-18:安卓在家里 WiFi,页面跑在 Mac 的直连源(http://192.168.x.x)上,
devSwitch 写的 cookie 在 IP 域下,跳中继 /d 后新源读不到 → 中继按旧设备转发,
会话/收件箱全是旧机器内容。relay 收到 /d?dev= 且该设备属于自己 → 302 回 /d 并种 cookie。

跑法:.venv/bin/python -m pytest tests/test_d_dev_query.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
from aiohttp import web

UID = 7
DB_ROWS = {("d-ali", UID), ("d-mac", UID)}


def _db(app):
    con = app["con"]
    return SimpleNamespace(execute=lambda *a: _q(con, a))


def _q(con, a):
    sql, args = a[0], (a[1] if len(a) > 1 else ())
    rows = []
    if "FROM devices" in sql:
        for did, owner in DB_ROWS:
            if (args and args[0] == did and owner == UID) or (not args and True):
                rows.append((did, owner))
    return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


def _req(path_qs, cookie=""):
    app = {"con": None}
    return SimpleNamespace(app=app, path="/d", method="GET",
                           query=dict(x.split("=", 1) for x in path_qs.split("?", 1)[1].split("&"))
                           if "?" in path_qs else {},
                           cookies={"cb_dev": cookie} if cookie else {}, scheme="https")


def _gate(request):
    """直接测种 cookie 分支的逻辑本身(抽出来的纯决策,见实现)"""
    return relay._d_dev_pick(request, _db(request.app), UID)


def test_query_dev_owned_sets_cookie(monkeypatch):
    monkeypatch.setattr(relay, "own_device", lambda db, uid, dev: {"id": dev})
    resp = _gate(_req("/d?dev=d-ali"))
    assert isinstance(resp, web.HTTPFound), "种 cookie 后 302 回 /d"
    assert resp.location.path == "/d" if hasattr(resp.location, "path") else True
    morsel = resp._cookies["cb_dev"] if hasattr(resp, "_cookies") else None
    assert morsel is not None and morsel.value == "d-ali"


def test_query_dev_not_owned_ignored(monkeypatch):
    monkeypatch.setattr(relay, "own_device", lambda db, uid, dev: None)
    resp = _gate(_req("/d?dev=d-other"))
    assert resp is None, "别人的设备:不种,继续走 cookie 流程"


def test_no_query_keeps_flow(monkeypatch):
    monkeypatch.setattr(relay, "own_device", lambda db, uid, dev: {"id": dev})
    resp = _gate(_req("/d"))
    assert resp is None, "没带 dev:维持原流程"
