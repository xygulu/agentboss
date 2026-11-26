# SPDX-License-Identifier: AGPL-3.0-or-later

"""免费版「每天 N 条」计的是发送类动作：发消息/按键/终端输入/跑命令/写文件/新建会话。
只看不算——GET 拉列表、拉输出、轮询都不计入。

跑法： .venv/bin/python -m pytest tests/test_send_actions_counted.py -q
"""
import sqlite3
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
import server

SEND_PATHS = ["/api/ext_send", "/api/ext_key", "/api/ext_term_in/abc",
              "/api/ext_cmdrun/sid", "/api/ext_write", "/api/ext_sess_new"]


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    return sqlite3.connect(p)


def test_send_paths_recognised():
    for p in SEND_PATHS:
        assert relay.is_send_action(p), p
    for p in ("/api/ext_sessions", "/api/ext_term_out/abc", "/api/ext_cmdout/x",
              "/api/plan", "/d"):
        assert not relay.is_send_action(p), p


def test_act_label_maps_every_send_path():
    for p in SEND_PATHS:
        kind, name = relay.act_label(p, "POST")
        assert (kind, name) in [("act", a) for a in relay.SEND_ACTS], (p, kind, name)


def test_five_actions_count_as_five_sends(tmp_path):
    db = _db(tmp_path)
    m = relay._min_now()
    for p in SEND_PATHS:
        _kind, name = relay.act_label(p, "POST")
        relay.use_act(db, "dab12", m, "relay", dev="ab12", act=name, path=p)
    # 只读不该算
    relay.use_act(db, "dab12", m, "relay", act="看会话列表", path="/api/ext_sessions")
    db.commit()
    assert relay.sends_used(db, "dab12") == len(SEND_PATHS)


class _Req:
    def __init__(self, path, method="POST", remote="10.0.0.9"):
        self.path, self.method, self.remote = path, method, remote
        self.query = {}
        self.headers = {}


def test_host_counts_direct_send_actions(monkeypatch):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    for p in SEND_PATHS:
        server._USE_ST.clear()
        server._USE_ST.update({"date": server._use_today(), "sends_local": 0})
        server._use_send_count(_Req(p))
        assert server._USE_ST["sends_local"] == 1, p
    server._USE_ST.clear()
    server._USE_ST.update({"date": server._use_today(), "sends_local": 0})
    server._use_send_count(_Req("/api/ext_sessions", method="GET"))
    assert server._USE_ST["sends_local"] == 0


def test_host_skips_relayed_sends(monkeypatch):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    server._USE_ST.clear()
    server._USE_ST.update({"date": server._use_today(), "sends_local": 0})
    server._use_send_count(_Req("/api/ext_key", remote="127.0.0.1"))
    assert server._USE_ST["sends_local"] == 0, "中继已经记过，别重复算"
