# SPDX-License-Identifier: AGPL-3.0-or-later

"""按账号手动额度覆盖（2026-11-18 从生产 relay 补平回本地）。

语义：meta planov:{uid} 只写覆盖的键；0 = 不限；清空 = 恢复套餐。
覆盖是最终决定权——可以超过 RELAY_MAX_DEVICES 天花板。

跑法： .venv/bin/python -m pytest tests/test_relay_quota_override.py -q
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

_TIER = {"name": "免费版", "sell": True, "limits": {"hosts": 1, "llm_day": 5}}


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, plan TEXT, plan_expires REAL,"
               " plan_sku TEXT, plan_next TEXT, plan_next_days INTEGER, plan_next_n INTEGER,"
               " product TEXT)")
    db.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO users VALUES(7,'free',0,'','',0,0,'codeboss')")
    return db


def _plans(tmp_path, monkeypatch):
    p = tmp_path / "plans.json"
    p.write_text(json.dumps({"free": _TIER, "pro": dict(_TIER, name="Pro")}),
                 encoding="utf-8")
    monkeypatch.setattr(relay, "_PLAN_PATH", str(p))
    monkeypatch.setattr(relay, "_PLAN_CACHE", [None, 0])
    monkeypatch.setattr(relay, "MAX_DEVICES", 3)


def test_override_wins_zero_is_unlimited_and_clear_restores(tmp_path, monkeypatch):
    _plans(tmp_path, monkeypatch)
    db = _db()

    assert relay.plan_limits(db, 7) == {"hosts": 1, "llm_day": 5}

    relay._plan_override_save(db, 7, {"llm_day": 0, "hosts": 9})
    lim = relay.plan_limits(db, 7)
    assert lim["llm_day"] == 0        # 0 = 不限，不是「没设」
    assert lim["hosts"] == 9          # 覆盖可超 MAX_DEVICES 天花板
    assert relay.plan_override(db, 7) == {"llm_day": 0, "hosts": 9}

    relay._plan_override_save(db, 7, {})
    assert relay.plan_override(db, 7) == {}
    assert relay.plan_limits(db, 7) == {"hosts": 1, "llm_day": 5}
