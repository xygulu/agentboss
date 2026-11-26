# SPDX-License-Identifier: AGPL-3.0-or-later

"""plans.json 产品线维度（D12：套餐带 product 键，按用户产品线解析档位）。

跑法： .venv/bin/python -m pytest tests/test_relay_plans_product.py -q
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


_TIER_CB = {"name": "免费版", "sell": True,
            "limits": {"hosts": 1, "asr_sec_day": 180},
            "features": {"remind": True}}
_TIER_AB = {"name": "免费版·AB", "sell": True, "product": "agentboss",
            "limits": {"hosts": 2, "asr_sec_day": 999},
            "features": {"remind": True}}


def _set_plans(tmp_path, monkeypatch, plans_dict):
    p = tmp_path / "plans.json"
    p.write_text(json.dumps(plans_dict, ensure_ascii=False), encoding="utf-8")
    # _PLAN_PATH 是 import 时求值的模块常量，必须直接打桩，setenv 无效
    monkeypatch.setattr(relay, "_PLAN_PATH", str(p))
    monkeypatch.setattr(relay, "_PLAN_CACHE", [None, 0])
    return p


def test_plans_for_filters_by_product(tmp_path, monkeypatch):
    # plans() 加载器要求 free+pro 键齐全，否则整体回落默认档——fixture 必须带 pro
    _set_plans(tmp_path, monkeypatch,
               {"free": dict(_TIER_CB), "pro": dict(_TIER_CB, name="Pro"),
                "free-ab": dict(_TIER_AB),
                "skus": {"x": {"name": "x", "money": "1", "days": 30}}})
    assert set(relay._plans_for("agentboss")) == {"free-ab"}
    # 加载器会把 _PLANS_DEFAULT 里 fixture 没写的档回填进来（如 max）
    assert {"free", "pro"} <= set(relay._plans_for("codeboss"))
    assert "free-ab" not in relay._plans_for("codeboss")
    assert "skus" not in relay._plans_for("agentboss")
    assert "skus" not in relay._plans_for("codeboss")


def test_plans_for_legacy_structure_unchanged(tmp_path, monkeypatch):
    _set_plans(tmp_path, monkeypatch,
               {"free": dict(_TIER_CB), "pro": {"name": "Pro", "limits": {}}})
    legacy = {k: v for k, v in relay.plans().items()
              if isinstance(v, dict) and k not in ("skus", "packs")}
    assert set(relay._plans_for("codeboss")) == set(legacy)
    assert set(relay._plans_for("agentboss")) == set()   # 旧结构无 AB 档


_seq = [0]


def _user(db, product=None, plan="free", exp=None):
    _seq[0] += 1
    name = "u%d" % _seq[0]
    cols = "name,pass_hash,salt" + (",product" if product else "")
    vals = [name, "h", "s"] + ([product] if product else [])
    uid = db.execute("INSERT INTO users(%s) VALUES(%s)" %
                     (cols, ",".join("?" * len(vals))), vals).lastrowid
    if plan != "free":
        db.execute("UPDATE users SET plan=? WHERE id=?", (plan, uid))
    # plan_state 对非 free 档要求未来到期才维持档名，否则回落 "free"
    if exp:
        db.execute("UPDATE users SET plan_expires=? WHERE id=?", (exp, uid))
    return uid


def test_plan_limits_follows_user_product(tmp_path, monkeypatch):
    _set_plans(tmp_path, monkeypatch,
               {"free": dict(_TIER_CB), "pro": dict(_TIER_CB, name="Pro"),
                "free-ab": dict(_TIER_AB)})
    db = relay.db_init(":memory:")
    uid_ab = _user(db, product="agentboss", plan="free-ab",
                   exp=time.time() + 86400 * 365)
    lim = relay.plan_limits(db, uid_ab)
    assert lim.get("hosts") == 2 and lim.get("asr_sec_day") == 999


def test_plan_limits_fallback_to_global_tier(tmp_path, monkeypatch):
    """agentboss 用户挂着全局档名（free/pro/max，plan_state 硬编码回落）时不空手。"""
    _set_plans(tmp_path, monkeypatch,
               {"free": dict(_TIER_CB), "pro": dict(_TIER_CB, name="Pro"),
                "free-ab": dict(_TIER_AB)})
    db = relay.db_init(":memory:")
    uid = _user(db, product="agentboss", plan="free")
    assert relay.plan_limits(db, uid).get("asr_sec_day") == 180
    uid_cb = _user(db, plan="free")
    assert relay.plan_limits(db, uid_cb).get("asr_sec_day") == 180
