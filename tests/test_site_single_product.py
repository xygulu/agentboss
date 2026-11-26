#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""官网单产品化（2026-11-26 重构）：agentboss 是唯一构建线，
官网不再讲双产品叙事；开源要素（GitHub 仓 / AGPL / 自建路径）必须在首页。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(os.path.dirname(HERE), "site")


def _pages():
    return [f for f in os.listdir(SITE) if f.endswith(".html")]


def _txt(name):
    with open(os.path.join(SITE, name), encoding="utf-8") as f:
        return f.read()


def test_compare_page_gone_and_unlinked():
    assert not os.path.exists(os.path.join(SITE, "compare.html"))
    for p in _pages():
        assert "compare.html" not in _txt(p), p


def test_nav_has_no_dual_product_links():
    # 双产品导航只剩 codeboss.html 自己（它是迁移说明页，讲历史合法）
    for p in _pages():
        if p == "codeboss.html":
            continue
        t = _txt(p)
        assert '>口袋工头</a>' not in t, p


def test_index_leads_with_oss_identity():
    t = _txt("index.html")
    assert "github.com/example/agentboss" in t
    assert "AGPL-3.0" in t
    assert "tools/selfhost/install-selfhost.sh" in t


def test_selfhost_points_to_agentboss_repo():
    t = _txt("selfhost.html")
    assert "github.com/example/agentboss" in t
    # 只禁旧仓链接；systemd unit 名叫 codeboss-relay 是 installer 写死的事实，保留
    assert "github.com/example/codeboss-relay" not in t
    assert "tools/selfhost/install-selfhost.sh" in t


def test_downloads_point_to_agentboss_line():
    # 各页导航下载钮不再指向旧线（迁移页与 ios 页除外——ios 是 CodeBoss TF 页）
    for p in _pages():
        if p in ("codeboss.html", "ios.html"):
            continue
        t = _txt(p)
        assert 'href="/codeboss.html#dl"' not in t, p
