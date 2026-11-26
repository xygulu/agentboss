# SPDX-License-Identifier: AGPL-3.0-or-later

"""中继面上的系列文案（批次二 Task 6）。

用户 2026-11-18 拍板：系列名 aiboss 只做系列/官网/条款层主品牌，端名保留
CodeBoss（口袋工头）/ AgentBoss（代理工头）。这里钉住落地页与条款不再用
「CodeBoss」单产品叙事。

跑法： .venv/bin/python -m pytest tests/test_relay_brand_copy.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def test_landing_page_is_series_first():
    p = relay._PAGE_DEV
    assert "aiboss 系列" in p
    assert "口袋工头" in p and "代理工头" in p


def test_terms_and_privacy_name_the_series():
    for doc in (relay._LEGAL_TERMS, relay._LEGAL_PRIVACY):
        assert "aiboss 系列" in doc
        assert "口袋工头" in doc and "代理工头" in doc


def test_no_bare_single_product_narrative():
    """「CodeBoss 提供…」「欢迎使用 CodeBoss（口袋工头）。」这类单产品口吻不能留。"""
    for doc in (relay._PAGE_DEV, relay._LEGAL_TERMS, relay._LEGAL_PRIVACY):
        assert "欢迎使用 CodeBoss（口袋工头）" not in doc
        assert "CodeBoss 提供" not in doc


def test_app_dead_end_pages_have_way_back():
    """2026-11-19 用户：App 壳把系统返回键整个吞掉（MainActivity.onBackPressed），
    扫码开始页 →「已有账号，登录」(/r/) 和「开通 Pro / Max」(/r/plans 未开放页)
    都没有返回链接 = 进得去出不来。两页都要有回 / 的「返回」。"""
    for page in (relay._PAGE_LOGIN, relay._PAGE_PLANS_OFF):
        i = page.find('id="pgback"')
        assert i >= 0, "缺返回链接"
        assert 'href="/"' in page[max(0, i - 60):i + 200]


def test_agentboss_pages_swap_icon():
    """2026-11-19 用户：AgentBoss 里还是 CodeBoss 的图标。?product=agentboss 时
    页头 logo 和 favicon 换成 agentboss 版，/agentboss-icon.png 吐 PNG。"""
    for page in (relay._PAGE_LOGIN, relay._PAGE_ANON):
        assert "/agentboss-icon.png" in page
        assert "link[rel=icon]" in page
    assert relay._AGENT_ICON_PNG.startswith(b"\x89PNG\r\n\x1a\n")
