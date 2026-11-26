# SPDX-License-Identifier: AGPL-3.0-or-later

"""品牌出口 + 账号端权限出口（批次二 Task 4）。

- /api/brand          免鉴权，首屏就能用；页面标题/关于页认它。
- /api/acct_products  要 token，经设备自证问中继「这账号能用哪些端」；
                      中继不可达一律 fail-open（双端），绝不能把人关在门外。

跑法： .venv/bin/python -m pytest tests/test_brand_endpoints.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _req(headers=None, cookies=None):
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {},
                           query={}, match_info={})


def _brand(headers=None, cookies=None):
    resp = asyncio.run(server.h_brand(_req(headers, cookies)))
    return json.loads(resp.body)


def test_brand_defaults_to_host_product(monkeypatch):
    """不带任何线索：回落到本机 host 自己的产品线。"""
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    d = _brand()
    assert d["series"] == "aiboss"
    assert d["product"] == "codeboss"
    assert d["product_label"] == "口袋工头"
    assert d["product_app"] == "CodeBoss"


def test_brand_follows_x_product_header():
    d = _brand(headers={"X-Product": "agentboss"})
    assert d["product"] == "agentboss"
    assert d["product_label"] == "代理工头"
    assert d["product_app"] == "AgentBoss"


def test_brand_ignores_bogus_product():
    d = _brand(headers={"X-Product": "<script>"})
    assert d["product"] == "codeboss"


def test_ping_carries_series_and_label(monkeypatch):
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    resp = asyncio.run(server.h_ping(_req()))
    d = json.loads(resp.body)
    assert d.get("series") == "aiboss"
    assert d.get("plabel") == "口袋工头"
    assert d.get("ok") == 1 and d.get("p")


def test_acct_products_fail_open(monkeypatch):
    """中继不可达：双端全给，不让本地把人关在门外。"""
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda *a, **k: {"ok": False, "error": "中继不可达"})
    d = server._acct_products_payload()
    assert d["ok"] is False
    assert d["allowed"] == ["codeboss", "agentboss"]
    assert d["source"] == "unknown"


def test_acct_products_from_relay(monkeypatch):
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda *a, **k: {"ok": True, "allowed": ["codeboss"],
                                         "source": "plan"})
    d = server._acct_products_payload()
    assert d == {"ok": True, "allowed": ["codeboss"], "source": "plan"}


def test_acct_products_sanitizes_relay_answer(monkeypatch):
    """中继答了脏值：只留合法产品线，认不出就 fail-open。"""
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda *a, **k: {"ok": True, "allowed": ["<script>", "agentboss"]})
    d = server._acct_products_payload()
    assert d["allowed"] == ["agentboss"]


def test_acct_products_empty_override_is_respected(monkeypatch):
    """后台把某端关掉（空列表）要照实回——这不是「拿不到」，是明确不允许。"""
    monkeypatch.setattr(server, "_relay_dev_call",
                        lambda *a, **k: {"ok": True, "allowed": [], "source": "override"})
    d = server._acct_products_payload()
    assert d == {"ok": True, "allowed": [], "source": "override"}


def test_app_page_icon_follows_host_product(monkeypatch):
    """2026-11-19 用户：AgentBoss 里还是 CodeBoss 的图标。页头 logo 与 favicon
    按本机产品线替换（同一条 HTML，服务端填占位符）。"""
    monkeypatch.setattr(server, "_tpl", lambda _n: "<i>__FAVICON__</i><b>__BRANDICON__</b>")
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    html = server._app_page("", "t").text
    assert server.brand_icons.BRAND_B64["agentboss"] in html
    assert server.brand_icons.FAVICON_B64["agentboss"] in html
    assert server.brand_icons.BRAND_B64["codeboss"] not in html


_TplT = "<t>__TITLE__</t><i>__FAVICON__</i><b>__BRANDICON__</b>"


def test_app_page_icon_follows_client_product(monkeypatch):
    """2026-11-19 用户：CodeBoss 手机连到 agentboss 线的 host，页头全是老板的图。
    一台 host 同时服务两条线，品牌要跟着「客户端」产品线走，不能跟 host 自己的。"""
    monkeypatch.setattr(server, "_tpl", lambda _n: _TplT)
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    html = server._app_page("", "t", product="codeboss").text
    assert server.brand_icons.BRAND_B64["codeboss"] in html
    assert server.brand_icons.FAVICON_B64["codeboss"] in html
    assert server.brand_icons.BRAND_B64["agentboss"] not in html


def test_list_page_title_and_icons_follow_client_cookie(monkeypatch):
    """codeboss 壳种了 cb_product=codeboss：列表页标题与图标都得是工头的。"""
    async def _ok(_r):
        return None
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_tpl", lambda _n: _TplT)
    monkeypatch.setattr(server, "_host_product", lambda: "agentboss")
    html = asyncio.run(server.handle_list(
        _req(cookies={"cb_product": "codeboss"}))).text
    assert "CodeBoss（口袋工头）" in html
    assert server.brand_icons.BRAND_B64["codeboss"] in html
    html2 = asyncio.run(server.handle_list(
        _req(cookies={"cb_product": "agentboss"}))).text
    assert "Agentboss（代理工头）" in html2
    assert server.brand_icons.BRAND_B64["agentboss"] in html2
