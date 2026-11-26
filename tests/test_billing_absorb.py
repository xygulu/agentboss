# SPDX-License-Identifier: AGPL-3.0-or-later

"""monorepo 统一 Task 1：吸收 codeboss 计费改版（9c8f146）。

git 里的计费真相是 relay._PLANS_DEFAULT（plans.json 是 gitignore 的运行时状态，
由 admin_api+短信码管理生产）。钉两线默认档新数字 + 06_settings.js 全档
「次数+秒数」显示口径。

跑法： .venv/bin/python -m pytest tests/test_billing_absorb.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

# 9c8f146 最终值：全档统一按免费版口径计次数/秒数，时长计费退役（置 0 不是删字段）

def test_plans_default_pro():
    p = relay._PLANS_DEFAULT["pro"]["limits"]
    assert p["hosts"] == 2 and p["clients"] == 2
    assert p["sends_day"] == 200
    assert p["asr_sec_day"] == 1200 and p["asr_sec_month"] == 0
    assert p["llm_day"] == 300 and p["use_min_day"] == 0


def test_plans_default_max():
    p = relay._PLANS_DEFAULT["max"]["limits"]
    assert p["hosts"] == 5 and p["clients"] == 5
    assert p["asr_sec_day"] == 0 and p["asr_sec_month"] == 0
    assert p["llm_day"] == 600 and p["use_min_day"] == 0 and p["sends_day"] == 0


def test_plans_default_free_untouched():
    # 2026-11-19 匿名免费档收紧：15 条/日、20 次智能调用/日（与 plans.json、host 的
    # _FREE_SENDS_CAP 一致）
    f = relay._PLANS_DEFAULT["free"]["limits"]
    assert f["sends_day"] == 15 and f["llm_day"] == 20 and f["use_min_day"] == 0


def test_use_cap_always_zero(monkeypatch):
    """2026-11-26 拍板：收费墙只按条数，时长墙整体摘除。

    plans.json 里就算残留 use_min_day 也只是展示字段，不再参与任何拦截
    （隧道 403 / 语音 429 / host 回落档全走 use_cap 这一个源头）。"""
    monkeypatch.setattr(relay, "plan_limits",
                        lambda db, uid: {"use_min_day": 999, "sends_day": 15})
    assert relay.use_cap(None, None) == 0
    assert relay.use_cap_eff(None, None) == 0


def test_settings_js_all_tiers_count_style():
    src = (HERE / "host" / "static" / "src" / "js" / "06_settings.js").read_text(encoding="utf-8")
    assert "plan==='free'" not in src            # 免费口径判断扩成全档
    assert "今日免费额度" not in src              # 旧标题
    assert "f.textContent='不限时长'" not in src  # 旧 Max 文案
    assert "今日额度：消息" in src                # 新标题（全档同口径）
    assert "textContent='不限'" in src            # 新 Max 文案
    assert "· 模型 '" in src                     # Max 标题带模型次数
