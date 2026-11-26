# SPDX-License-Identifier: AGPL-3.0-or-later

"""注册赠送策略的单元测试：不 import relay（会连环境），只抽 promo 段落 + 内存库跑。

跑法：python3 tools/test_promo.py
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "relay.py"


def _load_promo():
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"^_META_PROMO.*?(?=^def _norm_prefix)", src, re.M | re.S)
    assert m, "找不到 promo 段落"
    ns = {"json": json, "time": time}
    exec(m.group(0), ns)   # noqa: S102 - 测试自己仓库的代码
    return ns


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, plan TEXT, plan_expires REAL,"
               " plan_sku TEXT, plan_next TEXT, plan_next_days INTEGER, plan_next_n INTEGER)")
    db.execute("INSERT INTO users(id) VALUES(1)")
    return db


def main():
    ns = _load_promo()
    promo_cfg, promo_grant, _meta_json_save, _META_PROMO = (
        ns["promo_cfg"], ns["promo_grant"], ns["_meta_json_save"], ns["_META_PROMO"])

    # 1. 默认配置：开、pro、30 天、长期
    db = _db()
    cfg = promo_cfg(db)
    assert cfg["enabled"] and cfg["plan"] == "pro" and cfg["days"] == 30 and cfg["until"] == 0, cfg
    assert cfg["banner"] is True and "Pro" in cfg["note"], cfg

    # 2. 默认配置下赠送：写 plan/expires/sku，30 天
    ok, why = promo_grant(db, 1)
    assert ok, why
    row = db.execute("SELECT plan,plan_sku,plan_next FROM users WHERE id=1").fetchone()
    assert row[0] == "pro" and row[1] == "month" and row[2] is None, row
    exp = db.execute("SELECT plan_expires FROM users WHERE id=1").fetchone()[0]
    dt = exp - time.time()
    assert 29.9 * 86400 < dt < 30.1 * 86400, dt

    # 3. max 档 + 自定义 sku
    db = _db()
    _meta_json_save(db, _META_PROMO, {"enabled": 1, "plan": "max", "days": 7, "sku": "promo_max7"})
    cfg = promo_cfg(db)
    assert cfg["plan"] == "max" and cfg["days"] == 7 and cfg["sku"] == "promo_max7", cfg
    ok, _ = promo_grant(db, 1)
    assert ok
    row = db.execute("SELECT plan,plan_sku FROM users WHERE id=1").fetchone()
    assert row == ("max", "promo_max7"), row

    # 4. 活动已到期：不送
    db = _db()
    _meta_json_save(db, _META_PROMO, {"enabled": True, "days": 30, "until": time.time() - 10})
    ok, why = promo_grant(db, 1)
    assert not ok and "活动已结束" in why, (ok, why)
    assert db.execute("SELECT plan FROM users WHERE id=1").fetchone()[0] is None

    # 5. 活动进行中（until 在未来）：送
    _meta_json_save(db, _META_PROMO, {"enabled": True, "days": 30, "until": time.time() + 3600})
    ok, why = promo_grant(db, 1)
    assert ok, why

    # 6. 关闭开关 / 天数 0：不送
    db = _db()
    _meta_json_save(db, _META_PROMO, {"enabled": False, "days": 30})
    ok, why = promo_grant(db, 1)
    assert not ok and "未开启" in why, (ok, why)
    _meta_json_save(db, _META_PROMO, {"enabled": True, "days": 0})
    ok, why = promo_grant(db, 1)
    assert not ok and "天数为 0" in why, (ok, why)

    # 7. 坏配置兜底：plan 非法回 pro、days 越界夹回、垃圾 JSON 忽略
    db = _db()
    _meta_json_save(db, _META_PROMO, {"plan": "ultimate", "days": 99999})
    cfg = promo_cfg(db)
    assert cfg["plan"] == "pro" and cfg["days"] == 3650, cfg
    db.execute("UPDATE meta SET v='{{不是json' WHERE k=?", (_META_PROMO,))
    db.commit()
    cfg = promo_cfg(db)
    assert cfg == dict(ns["PROMO_DEF"]), cfg

    print("test_promo: 7 组全过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
