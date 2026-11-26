#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Phase 3 产品线合并（spec 拍板 12/8/9）：agentboss 吃下 codeboss 图标成为唯一构建线。
- 图标覆盖：codeboss 的 res/ 图标资源（png + colors.xml）覆盖进 agentboss；
  strings.xml 是产品身份自证（cb_product），绝不能拷（build.sh:106 靠它锁线）。
- 合并=替换：codeboss 侧资源原样保留（relay 分发槽位还要服务存量装机）。"""
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AB = os.path.join(ROOT, "products", "agentboss")
CB = os.path.join(ROOT, "products", "codeboss")

# OSS 导出仓不带 products/codeboss（spec 拍板 8）：这些钉「codeboss 原样保留」的
# 测试在导出仓自跳过，源仓照常钉死
pytestmark = pytest.mark.skipif(
    not os.path.isdir(CB), reason="products/codeboss 不在（OSS 导出仓）")


def test_agentboss_face_stays_unfiltered():
    # 合并=能力并集（spec 拍板 9）：agentboss 的 face_agents 保持 null（ledger 全量
    # 可见 hermes 等）——收窄成编码三件套是能力回退，test_face_consistency 已钉死该语义
    with open(os.path.join(AB, "product.json"), encoding="utf-8") as f:
        assert json.load(f).get("face_agents") is None


def test_codeboss_assets_untouched():
    # 合并=替换不是删线：codeboss 的 product.json 与图标必须原样在
    assert os.path.isfile(os.path.join(CB, "product.json"))
    assert os.path.isfile(os.path.join(CB, "android", "res", "values", "strings.xml"))
    assert os.path.isfile(os.path.join(
        CB, "android", "res", "mipmap-xxxhdpi", "ic_launcher.png"))


def test_agentboss_icons_come_from_codeboss():
    # 各档分辨率 + 通知图标 + 底色，全部与 codeboss 字节一致（覆盖成功的证据）
    pairs = [
        "res/mipmap-mdpi/ic_launcher.png",
        "res/mipmap-xhdpi/ic_launcher.png",
        "res/mipmap-xxhdpi/ic_launcher.png",
        "res/mipmap-xxxhdpi/ic_launcher.png",
        "res/mipmap-xxxhdpi/ic_launcher_round.png",
        "res/mipmap-xxxhdpi/ic_launcher_foreground.png",
        "res/drawable-xxxhdpi/ic_stat_codeboss.png",
        "res/drawable-xxhdpi/ic_stat_codeboss.png",
        "res/values/colors.xml",
    ]
    for rel in pairs:
        a = open(os.path.join(AB, "android", rel), "rb").read()
        c = open(os.path.join(CB, "android", rel), "rb").read()
        assert a == c, "agentboss 图标未与 codeboss 对齐：%s" % rel


def test_agentboss_strings_identity_kept():
    # 身份文件必须是 agentboss 自己的：cb_product=agentboss（拷错 = 包变 codeboss）
    s = open(os.path.join(AB, "android", "res", "values", "strings.xml"),
             encoding="utf-8").read()
    assert "cb_product</string>" in s.replace(
        '<string name="cb_product">', "cb_product</string>")
    assert ">agentboss<" in s
    assert ">codeboss<" not in s
