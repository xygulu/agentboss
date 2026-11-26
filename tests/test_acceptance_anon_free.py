# SPDX-License-Identifier: AGPL-3.0-or-later

"""匿名免费档验收（spec §七 六条）：每条绕过路径一个测试。

跑法： .venv/bin/python -m pytest tests/test_acceptance_anon_free.py -q
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

FREE = relay.plans()["free"]["limits"]


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','书房 Mac','s1','abc123',1,NULL,'codeboss',0)")
    db.commit()
    return db


# ① 删 config 重新 enroll → 同一 device_id、用量不变、额度不重置
def test_reenroll_reuses_device_and_usage(tmp_path):
    db = _db(tmp_path)

    def enroll(body):
        async def _j():
            return body
        req = types.SimpleNamespace(app={"db": db}, headers={}, remote="203.0.113.9",
                                    json=_j)
        return asyncio.run(relay.r_enroll(req))

    subj = relay.subject_of(db, None, "d1")
    relay.use_act(db, subj, relay._min_now(), "relay", dev="d1", act="发消息")
    relay.usage_add(db, relay._llm_key(db, None, "d1"), 2)
    resp = enroll({"fp": "abc123", "product": "codeboss"})    # config 丢了、只剩硬件
    assert json.loads(resp.text)["device_id"] == "d1"
    assert relay.sends_used(db, subj) == 1 and \
        relay.usage_get(db, relay._llm_key(db, None, "d1")) == 2
    assert db.execute("SELECT COUNT(*) FROM devices WHERE fp='abc123'").fetchone()[0] == 1


# ② 复制 config 到另一台（伪造 fp）→ host 连中继被拒
def test_forged_fp_link_rejected(tmp_path):
    db = _db(tmp_path)
    assert relay._link_check(db, "d1", "s1", "abc123")[1] is None
    row, err = relay._link_check(db, "d1", "s1", "deadbeef")
    assert row is None and err[0] == 403 and "fingerprint" in err[1]


# ③ 断中继 20 分钟后继续发送 → 第 16 条就被本地免费档拦住
def test_host_blocks_at_free_cap_when_offline(monkeypatch):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    monkeypatch.setattr(server, "_use_reconcile", lambda *a, **k: None)
    # 挂官方中继（url 空 = 裸机免费无墙，测不了 fail-closed）
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": "https://relay.example"})
    server._USE_ST.clear()
    server._USE_ST.update({
        "date": server._use_today(), "sends": 0, "sends_local": FREE["sends_day"] - 1,
        "sends_cap": 0, "sends_cap_known": False,
        "synced": time.time() - 1200,          # 20 分钟没对账
        "feat": {}, "feat_ts": 0.0, "used": 0, "cap": 0, "cap_known": False,
        "loaded": True,
    })
    assert server._send_over() is False          # 第 15 条还能发
    server._USE_ST["sends_local"] = FREE["sends_day"]
    assert server._send_over() is True, "断链也不能超过免费档的每日条数"


# ④ 到期账号连上 → 额度按 free 生效、终端被拦
def test_expired_account_is_free_everywhere(tmp_path, monkeypatch):
    db = _db(tmp_path)
    uid = db.execute("INSERT INTO users(name,pass_hash,salt,created,plan,plan_expires) "
                     "VALUES('u','h','s',0,'pro',?)",
                     (time.time() - 60,)).lastrowid
    db.execute("UPDATE devices SET owner=?, anon=0 WHERE id='d1'", (uid,))
    db.commit()
    assert relay.plan_limits(db, uid)["sends_day"] == FREE["sends_day"]

    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    monkeypatch.setattr(server, "_use_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "_relay_cfg",
                        lambda: {"device_secret": "s1", "url": "https://relay.example"})
    server._USE_ST.clear()
    server._USE_ST.update({"date": server._use_today(), "loaded": True,
                           "feat": {}, "feat_ts": 0.0, "synced": 0.0})
    snap = relay._feat_snap(db, uid, "d1")
    server._use_ack({"used_s": 0, "cap_s": 0, "unit": 3, "date": server._use_day(),
                     "sends": 0, "sends_cap": FREE["sends_day"], **snap})
    assert server._feat_on("terminal") is False


# ⑤ 免费设备走 P2P 发 ext_cmdrun → 被拒
def test_p2p_cmdrun_blocked_for_free(monkeypatch):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": "https://relay.example"})
    server._USE_ST.clear()
    server._USE_ST.update({"date": server._use_today(), "loaded": True,
                           "feat": {}, "feat_ts": 0.0, "synced": 0.0})
    assert server._feat_blocked("/api/ext_cmdrun/d1") is True
    assert server._feat_blocked("/api/ext_sessions") is False


# ⑥ 匿名设备调 /api/ptt、/api/llm → 有设备会话可用、无会话 401
def test_ai_endpoints_require_device_session(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(relay, "ASR_KEY", "")

    def req(cookies=None):
        return types.SimpleNamespace(app={"db": db}, cookies=cookies or {},
                                     headers={}, query={}, remote="203.0.113.9")

    ses = {"cb_dev_sess": relay._dev_sess_make(db, "d1")}
    for h in (relay.h_asr, relay.h_llm):
        assert asyncio.run(h(req())).status == 401
        assert asyncio.run(h(req(ses))).status != 401, "有设备会话就不该再 401"
