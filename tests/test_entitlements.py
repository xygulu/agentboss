# SPDX-License-Identifier: AGPL-3.0-or-later

"""账号能用哪些端（批次二 Task 7）：中继是唯一硬墙。

规则（用户 2026-11-18 拍板）：
- users.products 有值（CSV）→ 逐账号覆盖；空串 = 一个端都不给
- pro / max 且未过期 → 双端
- 其它（free / 过期）→ 只给 users.product 那条线，缺省 codeboss
**绝不收回正在用的端**：free 账号 product=agentboss 仍拿 agentboss。

跑法： .venv/bin/python -m pytest tests/test_entitlements.py -q
"""
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

COLS = ("id INTEGER PRIMARY KEY, name TEXT, email TEXT, phone TEXT, "
        "plan TEXT, plan_expires REAL, plan_sku TEXT, plan_next TEXT, "
        "plan_next_days INTEGER DEFAULT 0, plan_next_n INTEGER DEFAULT 0, "
        "product TEXT, products TEXT")


def _db(plan="free", exp=0, product="codeboss", products=None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE users (" + COLS + ")")
    # plan_state 的老库兜底会查 orders（plan_sku 为空时），空表即可
    db.execute("CREATE TABLE orders (uid INTEGER, sku TEXT, status INTEGER, paid REAL)")
    db.execute("INSERT INTO users(id,plan,plan_expires,product,products) VALUES(1,?,?,?,?)",
               (plan, exp, product, products))
    return db


FUTURE = time.time() + 86400 * 30


def test_pro_gets_both():
    assert relay._allowed_products(_db("pro", FUTURE), 1) == (["codeboss", "agentboss"], "plan")


def test_max_gets_both():
    assert relay._allowed_products(_db("max", FUTURE), 1) == (["codeboss", "agentboss"], "plan")


def test_free_gets_its_own_line():
    assert relay._allowed_products(_db("free", 0, "codeboss"), 1) == (["codeboss"], "plan")


def test_free_agentboss_keeps_agentboss():
    """老账号在老板线：不能因为「free 默认工头」把人踢回工头。"""
    assert relay._allowed_products(_db("free", 0, "agentboss"), 1) == (["agentboss"], "plan")


def test_expired_pro_keeps_current_line():
    exp = time.time() - 86400
    assert relay._allowed_products(_db("pro", exp, "agentboss"), 1) == (["agentboss"], "plan")


def test_override_wins():
    assert relay._allowed_products(_db("pro", FUTURE, "codeboss", "agentboss"), 1) == \
        (["agentboss"], "override")


def test_override_empty_means_no_end():
    assert relay._allowed_products(_db("pro", FUTURE, "codeboss", ""), 1) == ([], "override")


def test_override_garbage_falls_back_to_plan():
    """后台填了脏值（不是「一个端都不给」）：按套餐走，别把人锁死。"""
    assert relay._allowed_products(_db("pro", FUTURE, "codeboss", "火星端"), 1) == \
        (["codeboss", "agentboss"], "plan")


def test_unknown_user_defaults_codeboss():
    db = _db()
    assert relay._allowed_products(db, 999) == (["codeboss"], "plan")


def test_migration_adds_products_column():
    """老库升级：products 列必须自己长出来（IF NOT EXISTS 风格，重复跑不炸）。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE users (" + COLS.replace(" product TEXT, products TEXT",
                                                     " product TEXT") + ")")
    relay._migrate_users_products(db)
    relay._migrate_users_products(db)          # 幂等
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
    assert "products" in cols


def test_locked_page_names_the_end_and_offers_upgrade():
    """403 页：说清楚「哪个端没开通」+ 给购买入口，不是一句冷冰冰的 Forbidden。"""
    html = relay._locked_page("agentboss", ["codeboss"])
    assert "代理工头" in html and "未开通" in html
    assert "aiboss" in html
    assert "/r/account" in html          # 购买/升级入口
    assert "口袋工头" in html            # 告诉他现在能用的是哪个端


def test_parse_products_none_means_follow_plan():
    assert relay._parse_products(None) is None


def test_parse_products_list_and_csv():
    assert relay._parse_products(["agentboss"]) == "agentboss"
    assert relay._parse_products("agentboss,codeboss") == "codeboss,agentboss"
    assert relay._parse_products(["<script>"]) == ""      # 全脏 → 空串=一个端都不给
    assert relay._parse_products([]) == ""                # 显式清空也照办
