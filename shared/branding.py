#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""boss 系列品牌常量。

换域名 / 加产品线只动这里，别把字面量散回业务文件。
值是「当前值」：改名换域那一刻改这一处即可。
"""

RELAY_HOST_DEFAULT = "relay.example.com"
RELAY_URL_DEFAULT = "https://" + RELAY_HOST_DEFAULT
SITE_URL = "https://example.com"

# 产品线（设计文档 D5/D12：一套底座，产品线以权限与配置区分）
PRODUCT_CODEBOSS = "codeboss"
PRODUCT_AGENTBOSS = "agentboss"
PRODUCTS = (PRODUCT_CODEBOSS, PRODUCT_AGENTBOSS)
PRODUCT_DEFAULT = PRODUCT_CODEBOSS

# 系列名（2026-11-18 用户拍板）：aiboss 只做系列/官网/条款层的主品牌，
# 两个端名不变——「CodeBoss 口袋工头」「AgentBoss 代理工头」。
SERIES = "aiboss"
PRODUCT_NAMES = {
    PRODUCT_CODEBOSS: {"label": "口袋工头", "app": "CodeBoss"},
    PRODUCT_AGENTBOSS: {"label": "代理工头", "app": "AgentBoss"},
}


def product_name(product: str) -> dict:
    """端名（中文昵称 + 英文正式名）：页面标题、关于页、托盘都用这一份。"""
    return PRODUCT_NAMES[valid_product(product)]


def valid_product(s: str) -> str:
    """产品线标识清洗：不在枚举里（含空/None）一律回落默认，绝不猜。"""
    return s if s in PRODUCTS else PRODUCT_DEFAULT


def app_label(s: str) -> str:
    """说给用户听的端名（二维码旁提示等）。

    与 valid_product 不同：认不得的产品线**不许回落成口袋工头**——一套 host 服务三条端线，
    deskboss（或以后的任何线）不能被叫成别人的 App。认不得就说通用「工头」。
    """
    n = PRODUCT_NAMES.get(s or "")
    return n["label"] if n else "工头"
