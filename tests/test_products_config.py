# SPDX-License-Identifier: AGPL-3.0-or-later

"""monorepo 统一 Task 2：products/<线>/product.json 是产品参数单一来源。

钉键集、face 白名单、版本 lineage 接续现网（codeboss 现网 189→下一发 190、
agentboss 现网 2→下一发 3）、keystore 在位、壳内四处产品串已资源化。

跑法： .venv/bin/python -m pytest tests/test_products_config.py -q
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import branding


def _p(name):
    return json.loads((HERE / "products" / name / "product.json").read_text(encoding="utf-8"))


def test_products_valid_and_labeled():
    for name in ("agentboss", "codeboss"):
        d = _p(name)
        assert d["cb_product"] == name
        assert branding.valid_product(d["cb_product"]) == name
        assert d["app_label"]


def test_face_whitelist():
    # 与代码/官网口径一致：codeboss 编码面 = claude/codex/opencode，agentboss 全量
    assert _p("codeboss")["face_agents"] == ["claude", "codex", "opencode"]
    assert _p("agentboss")["face_agents"] is None


def test_agent_face_follows_products_config():
    """face_agents 不再只记录：server._agent_face 以 products/<线>/product.json 为唯一来源。"""
    import server
    assert server._agent_face("agentboss") is None
    assert server._agent_face("codeboss") == frozenset({"claude", "codex", "opencode"})


def _vt(s):
    return tuple(int(x) for x in str(s).split("."))


def test_version_lineage_continues_production():
    # 只断言谱系单调递增（现网基线 codeboss=190 / agentboss=4），不锁死具体值——
    # 锁死会导致每次发版都要改测试。版本号按段比（字符串比会把 0.1.10 判成 < 0.1.2）。
    cb = _p("codeboss")
    assert cb["version_code"] >= 190
    assert _vt(cb["version_name"]) >= (0, 11, 25)
    ab = _p("agentboss")
    assert ab["version_code"] >= 4
    assert _vt(ab["version_name"]) >= (0, 1, 2)


def test_keystores_in_place():
    for name in ("agentboss", "codeboss"):
        d = _p(name)
        ks = HERE / d["keystore"]
        assert ks.is_file(), "%s keystore 不在：%s" % (name, d["keystore"])


def test_shell_product_strings_resourced():
    src = (HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal"
           / "MainActivity.java").read_text(encoding="utf-8")
    # 四处硬编码都资源化（包名声明/注释/InstallStatus 广播名不算产品串）
    assert '"/?product=agentboss"' not in src
    assert "apk_ver?product=agentboss" not in src
    assert '"/agentboss.apk"' not in src
    assert '"cb_product=agentboss' not in src
    assert "R.string.cb_product" in src
    # 资源在位
    sx = (HERE / "clients" / "android" / "res" / "values" / "strings.xml").read_text(encoding="utf-8")
    assert 'name="cb_product"' in sx


def test_codeboss_overlay_carries_icon_background():
    """评审 I1：自适应图标底色是 values 资源，光覆盖图标 png 不够——
    overlay 缺 colors.xml 时 codeboss 包底色渲染成 agentboss 的暗夜蓝 #101624。"""
    oc = HERE / "products" / "codeboss" / "android" / "res" / "values" / "colors.xml"
    assert oc.is_file()
    assert "#140904" in oc.read_text(encoding="utf-8")     # codeboss 暗棕
    base = (HERE / "clients" / "android" / "res" / "values" / "colors.xml").read_text(encoding="utf-8")
    assert "#101624" in base                                # agentboss 暗夜蓝（对照）


def test_first_page_carries_product_line():
    """装机第一次打开 / 刚配对换地址：cookie 还没种下，WebView 的第一个请求没有
    cb_product，host 按自己的线渲染——两个 App 打开一模一样的页（2026-11-20 用户）。
    首屏 URL 必须自带 ?product=，并在加载前按这个 URL 的 origin 再种一次 cookie。"""
    src = (HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal"
           / "MainActivity.java").read_text(encoding="utf-8")
    assert "web.loadUrl(withProduct(url))" in src, "启动/换地址的加载没带产品线"
    assert "seedProductCookie(this, originOf(url))" in src, "首装 base 还空着：要按这次的 URL 种"
    m = re.search(r"private String withProduct\(String url\)\s*\{[\s\S]*?\n    \}", src)
    assert m, "缺 withProduct()"
    body = m.group(0)
    assert "product=" in body and "product()" in body, "withProduct 要拼产品线资源值"
    assert "startsWith(OFFLINE_PREFIX)" in body, "离线页不做产品线改写"
