# SPDX-License-Identifier: AGPL-3.0-or-later

"""语音/轻量模型只认会话身份：账号会话按账号额度，设备会话按免费额度，什么都没有 401。

跑法： .venv/bin/python -m pytest tests/test_ai_requires_session.py -q
"""
import sqlite3
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product,frozen) "
               "VALUES('d1','mac','s','f',1,NULL,'codeboss',0)")
    db.commit()
    return db


def _req(db, cookies=None, headers=None):
    return types.SimpleNamespace(
        app={"db": db}, cookies=cookies or {}, headers=headers or {},
        query={}, remote="1.2.3.4")


def test_no_session_has_no_device(tmp_path):
    db = _db(tmp_path)
    assert relay._asr_dev(_req(db)) is None


def test_device_session_identifies_device(tmp_path):
    db = _db(tmp_path)
    tok = relay._dev_sess_make(db, "d1")
    assert relay._asr_dev(_req(db, cookies={"cb_dev_sess": tok})) == "d1"


def test_asr_quota_uses_free_tier_for_anon(tmp_path):
    db = _db(tmp_path)
    _d, capd, _m, _capm = relay._asr_quota(db, "d1")
    assert capd == relay.plans()["free"]["limits"]["asr_sec_day"]


def test_asr_quota_account_is_per_account(tmp_path):
    db = _db(tmp_path)
    db.execute("UPDATE devices SET owner=7, anon=0 WHERE id='d1'")
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.commit()
    _d, capd, _m, _capm = relay._asr_quota(db, "d1")
    assert capd == relay.plans()["free"]["limits"]["asr_sec_day"]
    # 账号主体的用量键落 u7，不是设备键
    relay._asr_quota(db, "d1", add=5)
    row = db.execute("SELECT k FROM usage WHERE k LIKE 'asr:u7:%'").fetchone()
    assert row is not None


def test_llm_key_separates_account_and_device(tmp_path):
    db = _db(tmp_path)
    assert relay._llm_key(db, 7, "d1").startswith("llm:u7:")
    assert relay._llm_key(db, None, "d1").startswith("llm:dd1:")
