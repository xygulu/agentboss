# SPDX-License-Identifier: AGPL-3.0-or-later

"""用量主体：账号记账落 u<id>，匿名设备落 d<dev>，两边互不影响。

跑法： .venv/bin/python -m pytest tests/test_subject_usage.py -q
"""
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    return sqlite3.connect(p)


def test_subject_of():
    assert relay.subject_of(None, 5) == "u5"
    assert relay.subject_of(None, None, "ab12") == "dab12"
    assert relay.subject_of(None, "dab12") == "dab12"     # 已经是主体就原样
    assert relay.subject_of(None, 5, "ab12") == "u5"      # 账号优先


def test_anon_send_count_lands_on_device(tmp_path):
    db = _db(tmp_path)
    m = relay._min_now()
    relay.use_act(db, "dab12", m, "relay", dev="ab12", act="发消息")
    db.commit()
    assert relay.sends_used(db, "dab12") == 1
    assert relay.sends_used(db, 5) == 0, "账号的账不能被匿名设备串走"


def test_account_send_count_unchanged(tmp_path):
    db = _db(tmp_path)
    m = relay._min_now()
    relay.use_act(db, 5, m, "relay", act="发消息")
    db.commit()
    assert relay.sends_used(db, 5) == 1
    assert relay.sends_used(db, "dab12") == 0


def test_meter_windows_do_not_share(tmp_path):
    db = _db(tmp_path)
    relay.use_take(db, "dab12")
    relay.use_take(db, 5)
    unit = relay.meter_unit(db)
    assert relay.use_used(db, "dab12") == unit
    assert relay.use_used(db, 5) == unit


def test_anon_falls_back_to_free_plan(tmp_path):
    db = _db(tmp_path)
    lim = relay.plan_limits(db, "dab12")
    assert lim["hosts"] == relay.plans()["free"]["limits"]["hosts"]
    assert relay.user_plan(db, "dab12") == "free"
    assert relay.plan_features(db, "dab12") == relay.plans()["free"]["features"]
    assert relay.use_credit_of(db, "dab12") == 0


def test_paid_account_subject_keeps_paid_limits(tmp_path):
    """账号的用量主体是 'u<id>'——拿来算额度不能当匿名设备回落 free。

    2026-11-19 实报：Max 账号语音被拦「额度已用完，开通 Pro 可继续用」。
    原因是 h_asr / 发送判额都传 subject_of() 的字符串，user_plan("u1") 走
    isinstance(str) 分支直接 free → asr 180 秒/天。"""
    db = _db(tmp_path)
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')").lastrowid
    db.execute("UPDATE users SET plan='max',plan_expires=? WHERE id=?",
               (time.time() + 30 * 86400, uid))
    db.commit()
    subj = relay.subject_of(db, uid)
    assert relay.user_plan(db, uid) == "max"
    assert relay.user_plan(db, subj) == "max", "用量主体是账号，不是匿名设备"
    assert relay.plan_limits(db, subj) == relay.plan_limits(db, uid)
    assert relay.plan_limits(db, subj).get("asr_sec_day") == 0
    assert relay.plan_features(db, subj) == relay.plan_features(db, uid)


def test_paid_account_subject_uses_credit(tmp_path):
    """时长包也是账号级：'u<id>' 主体不能当匿名设备（余额读成 0、充值被丢掉），
    否则套餐用完又买过包的人照样被拦。"""
    db = _db(tmp_path)
    relay.use_credit_add(db, "u5", 600)
    assert relay.use_credit_of(db, "u5") == 600
    assert relay.use_credit_of(db, 5) == 600
    assert relay.use_credit_of(db, "dab12") == 0


def test_quota_cta_only_for_free():
    """额度用完后那句「开通 Pro」只对免费版说：Max 用户被劝升 Pro 是坏文案。"""
    assert "Pro" in relay._quota_cta("free", "发")
    assert relay._quota_cta("pro", "发") == ""
    assert relay._quota_cta("max", "用") == ""
