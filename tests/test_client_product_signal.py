# SPDX-License-Identifier: AGPL-3.0-or-later

"""每个「打开产品页」的 App 都必须自报产品线（2026-11-20 用户：「所有 APP 都要带上产品的信号」）。

host 一个进程同时伺候多条线。App 不带信号时 host 只能回落到自己那条线——
于是这条线的 App 长得像另一条线（11-18/11-20 两次都是这个形状）。
所以：新客户端壳必须在 APPS 表里登记（或在 EXEMPT 写清为什么不需要产品线），
没登记就是红的；安卓/ iOS 的信号机制各有断言，构建时还有一道硬闸。

跑法：.venv/bin/python -m pytest tests/test_client_product_signal.py -q
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CLIENTS = HERE / "clients"

# 打开产品页的客户端壳：目录名 → 产品线信号怎么来
APPS = {
    "android": "products/<线>/product.json + android/res overlay → R.string.cb_product"
               "（cookie + 首屏 ?product= + 查更新）",
    "ios": "Info.plist CBProduct → cb_product cookie + 首屏 ?product=",
}
# 不打开产品页的壳：为什么不需要产品线信号
EXEMPT = {
    "android-admin": "管理后台壳：连 /r/admin 管理页，与产品线无关",
    "ios-admin": "管理后台壳：同上",
    "mac": "BossHost 主机菜单栏 App：不打开产品页",
    "win": "Windows 安装器：装的是 host，不打开产品页",
}


def _products():
    return sorted(p.parent.name for p in (HERE / "products").glob("*/product.json"))


def test_every_client_shell_registered():
    """新加一个客户端目录不登记 → 红：先回答它要不要带产品线。"""
    dirs = {d.name for d in CLIENTS.iterdir() if d.is_dir()}
    unknown = sorted(dirs - set(APPS) - set(EXEMPT))
    assert not unknown, ("clients/ 下这些壳没登记产品线归属（APPS 或 EXEMPT）："
                         + ", ".join(unknown))
    stale = sorted((set(APPS) | set(EXEMPT)) - dirs)
    assert not stale, "登记了但目录已经不存在的壳（删掉）：" + ", ".join(stale)


def test_android_each_line_states_itself():
    """每条线的包都必须自带 cb_product——只靠 base 默认值等于把线写死成 agentboss。"""
    prods = _products()
    assert "codeboss" in prods and "agentboss" in prods
    for p in prods:
        d = json.loads((HERE / "products" / p / "product.json").read_text(encoding="utf-8"))
        assert d["cb_product"] == p, "%s/product.json 的 cb_product 不是 %s" % (p, p)
        sx = HERE / "products" / p / "android" / "res" / "values" / "strings.xml"
        assert sx.is_file(), ("%s 没覆盖 strings.xml：cb_product 会继承 base 的值，"
                              "APK 自报的产品线就错了" % p)
        text = sx.read_text(encoding="utf-8")
        assert re.search(r'name="cb_product">%s<' % p, text), \
            "%s 的 strings.xml 里 cb_product 不是 %s" % (p, p)


def test_android_shell_sends_signal():
    src = (CLIENTS / "android" / "src" / "com" / "agentboss" / "terminal"
           / "MainActivity.java").read_text(encoding="utf-8")
    assert "R.string.cb_product" in src, "产品线没从资源读（又硬编码了？）"
    assert '"/?product=" + product()' in src, "首屏 URL 没带产品线"
    assert "web.loadUrl(withProduct(url))" in src, "后续加载没带产品线"
    assert "static void seedProductCookie" in src, "cookie 没种"
    # 构建时的硬闸：产物里的 cb_product 必须等于这条线，否则不出包
    bs = (CLIENTS / "android" / "build.sh").read_text(encoding="utf-8")
    assert 'GOT_LINE" != "$PRODUCT' in bs, "build.sh 少了产品线自证检查"


def test_ios_shell_declares_and_sends_signal():
    plist = (CLIENTS / "ios" / "Codeboss" / "Info.plist").read_text(encoding="utf-8")
    m = re.search(r"<key>CBProduct</key>\s*<string>([a-z]+)</string>", plist)
    assert m, "iOS 壳没声明 CBProduct（打包后不知道自己是哪条线）"
    assert m.group(1) in _products(), "CBProduct=%s 不是已上架的产品线" % m.group(1)

    src = (CLIENTS / "ios" / "Codeboss" / "MainViewController.swift").read_text(encoding="utf-8")
    assert 'infoDictionary?["CBProduct"]' in src, "产品线没从 Info.plist 读"
    assert '.name: "cb_product"' in src, "cookie 没种产品线"
    assert 'URLQueryItem(name: "product"' in src, "首屏 URL 没带 ?product="
    assert "self.withProduct(u)" in src, "loadServer 没走 withProduct"
