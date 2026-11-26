# SPDX-License-Identifier: AGPL-3.0-or-later

"""付费档到期立刻按 free 的额度算，不等后台改 users.plan。

跑法： .venv/bin/python -m pytest tests/test_plan_expiry.py -q
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


def _user(db, plan, exp):
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,?,?)", (plan, exp))
    db.commit()


def test_expired_pro_falls_back_to_free(tmp_path):
    db = _db(tmp_path)
    _user(db, "pro", time.time() - 60)
    lim = relay.plan_limits(db, 7)
    free = relay.plans()["free"]["limits"]
    assert lim["sends_day"] == free["sends_day"]
    assert lim["llm_day"] == free["llm_day"]
    assert relay.user_plan(db, 7) == "free"


def test_active_pro_keeps_pro_limits(tmp_path):
    db = _db(tmp_path)
    _user(db, "pro", time.time() + 86400)
    lim = relay.plan_limits(db, 7)
    pro = relay.plans()["pro"]["limits"]
    assert lim["sends_day"] == pro["sends_day"]


def test_override_still_wins_after_expiry(tmp_path):
    db = _db(tmp_path)
    _user(db, "pro", time.time() - 60)
    relay._plan_override_save(db, 7, {"sends_day": 77})
    assert relay.plan_limits(db, 7)["sends_day"] == 77
