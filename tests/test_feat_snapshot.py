# SPDX-License-Identifier: AGPL-3.0-or-later

"""功能开关快照签名：host 只认中继签的，验不过/过期按 free（不能自己写个 True）。

跑法： .venv/bin/python -m pytest tests/test_feat_snapshot.py -q
"""
import hashlib
import hmac
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
import server


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac','s1','f',0,7,'codeboss',0)")
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'pro',?)", (time.time() + 86400,))
    db.commit()
    return db


def _reset(monkeypatch):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    monkeypatch.setattr(server, "_use_save", lambda *a, **k: None)
    # url 非空 = 挂官方中继（2026-11-26 三态口径：url 空 = 裸机免费无墙，测不了快照）
    monkeypatch.setattr(server, "_relay_cfg",
                        lambda: {"device_secret": "s1", "url": "https://relay.example"})
    server._USE_ST.clear()
    server._USE_ST.update({"date": server._use_today(), "loaded": True,
                           "feat": {}, "feat_ts": 0.0, "synced": 0.0})


def test_relay_signs_pro_snapshot(tmp_path):
    db = _db(tmp_path)
    snap = relay._feat_snap(db, 7, "d1")
    assert snap["feat"]["terminal"] is True and snap["feat_plan"] == "pro"
    assert snap["feat_sig"] and snap["feat_exp"] > time.time()


def test_relay_signs_free_for_anon(tmp_path):
    db = _db(tmp_path)
    snap = relay._feat_snap(db, "dab12", "d1")
    assert snap["feat"]["terminal"] is False


def test_ack_accepts_signed_snapshot(tmp_path, monkeypatch):
    db = _db(tmp_path)
    snap = relay._feat_snap(db, 7, "d1")
    _reset(monkeypatch)
    server._use_ack({"used_s": 0, "cap_s": 0, "unit": 3, "date": server._use_day(),
                     "sends": 0, "sends_cap": 200, "plan": "pro", **snap})
    assert server._feat_on("terminal") is True


def test_ack_rejects_tampered_snapshot(tmp_path, monkeypatch):
    db = _db(tmp_path)
    snap = relay._feat_snap(db, 7, "d1")
    _reset(monkeypatch)
    snap["feat"] = {"terminal": True}      # 签名后偷改
    server._use_ack({"used_s": 0, "cap_s": 0, "unit": 3, "date": server._use_day(),
                     **snap})
    assert server._feat_on("terminal") is False


def test_ack_rejects_expired_snapshot(tmp_path, monkeypatch):
    db = _db(tmp_path)
    snap = relay._feat_snap(db, 7, "d1")
    body = {"feat": snap["feat"], "exp": int(time.time()) - 60, "plan": "pro"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    key = hmac.new(b"s1", b"cb-feat-v1", hashlib.sha256).digest()
    snap["feat_exp"] = body["exp"]
    snap["feat_sig"] = hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()
    _reset(monkeypatch)
    server._use_ack({"used_s": 0, "cap_s": 0, "unit": 3, "date": server._use_day(),
                     **snap})
    assert server._feat_on("terminal") is False


def test_terminal_wall_on_direct_paths(monkeypatch):
    """host 直连（LAN/P2P）也挡收费终端：没快照=free，必须 403。"""
    _reset(monkeypatch)
    assert server._feat_blocked("/api/ext_cmdrun/x") is True
    assert server._feat_blocked("/api/ext_term_in/x") is True
    assert server._feat_blocked("/api/ext_sessions") is False
