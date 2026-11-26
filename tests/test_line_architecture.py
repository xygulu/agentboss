# SPDX-License-Identifier: AGPL-3.0-or-later

"""分线架构门禁（2026-11-20）：让「忘了分线」变成红测试，而不是线上事故。

用户问：以后还会出现这种问题吗？光靠这次修的几个点不会——要结构上封死：
  · 客户端可见的一切（页面、供包、能力面）只能从 _client_product_for / cbLine 出来；
    host 自己的产品线只用于「本机是谁」（发布侧、端口探针、配对页兜底）。
  · 页面 JS 的产品字面量只能待在 cbLine / lineCaps / lineName 三张表里，
    别处写 'codeboss' 就是红的（11-20 的 /codeboss.apk 兜底下载就是这种漏）。
  · 同一个 host、两条线的客户端各拿各的（跨层端到端，早先的测试都只测层内）。

跑法： .venv/bin/python -m pytest tests/test_line_architecture.py -q
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

SRV = (HERE / "host" / "server.py").read_text(encoding="utf-8")
JS_DIR = HERE / "host" / "static" / "src" / "js"

# 客户端 handler 里读到「本机线」的例外：本机身份探针（回答「端口上是谁」），
# 不是给客户端渲染用的。
ALLOW_HOST_LINE = {"h_ping"}

# 页面 JS 里允许出现产品字面量的函数：产品线 → 能力的唯一映射 + 名字表。
ALLOW_LINE_FUNCS = {("05_cmd.js", "cbLine"), ("05_cmd.js", "lineCaps"),
                    ("05_cmd.js", "lineName")}
# 页面入口登记表（2026-11-20）：每条入口归属哪条线，天然要写线名。
# 表本身由 tests/test_page_entries.mjs 管，这里只放行它的字面量。
ALLOW_LITERAL_BLOCKS = {"PAGE_ENTRIES"}


def _functions(src):
    """顶层函数名 -> (start, end) 字符区间（这些文件都是顶层 function，够用）。"""
    out = {}
    for m in re.finditer(r"\n(?:async )?function (\w+)\([^)]*\)\{", src):
        end = src.find("\n}", m.end())
        out[m.group(1)] = (m.start(), len(src) if end < 0 else end + 2)
    return out


# ---- 1. 客户端 handler 不许按本机线作答 ----

def test_client_handlers_never_read_host_line():
    """h_apk/h_apk_ver 就是死在这里：一台 codeboss 机器按自己的线给 agentboss 壳发工头的包。"""
    bad = []
    for chunk in re.split(r"\n(?=(?:async )?def )", SRV):
        m = re.match(r"(?:async )?def (h_\w+)", chunk)
        if not m or m.group(1) in ALLOW_HOST_LINE:
            continue
        if "_host_product()" in chunk:
            bad.append(m.group(1))
    assert not bad, ("客户端 handler 按本机产品线作答（应走 _client_product_for / "
                     "_apk_product_for）：" + ", ".join(bad))


# ---- 2. 页面 JS 的产品字面量集中到三张表 ----

def test_page_js_line_literals_confined():
    bad = []
    for f in sorted(JS_DIR.glob("*.js")):
        src = f.read_text(encoding="utf-8")
        # 注释里的名字不算分线逻辑：整段 /* */ 抹成等长空格，行号/偏移不变
        src = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), src)
        spans = {name: span for name, span in _functions(src).items()}
        for name in ALLOW_LITERAL_BLOCKS:
            for bm in re.finditer(r"\bconst %s\s*=\s*\{" % name, src):
                end = src.find("\n};", bm.end())
                spans[name] = (bm.start(), len(src) if end < 0 else end + 3)
        for m in re.finditer(r"['\"]codeboss['\"]|['\"]agentboss['\"]", src):
            line = src.count("\n", 0, m.start()) + 1
            text = src.splitlines()[line - 1].lstrip()
            if text.startswith(("//", "*", "/*", "<!--")):
                continue          # 注释里提名字不算分线逻辑
            owner = next((n for n, (a, b) in spans.items() if a <= m.start() <= b), None)
            if (f.name, owner) not in ALLOW_LINE_FUNCS and owner not in ALLOW_LITERAL_BLOCKS:
                bad.append("%s:%d(%s)" % (f.name, line, owner or "?"))
    assert not bad, ("产品字面量散落在分线表之外（新增差异请进 lineCaps/lineName）："
                     + ", ".join(bad))


def test_every_product_is_handled_by_page_tables():
    """服务端认的产品线，页面必须给得出能力和名字——加线不加表 = 客户端长得跟默认线一样。"""
    import branding
    src = (JS_DIR / "05_cmd.js").read_text(encoding="utf-8")
    src = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), src)
    spans = _functions(src)
    for name in ("lineCaps", "lineName"):
        a, b = spans[name]
        body = src[a:b]
        missing = [p for p in branding.PRODUCTS if "'%s'" % p not in body
                   and '"%s"' % p not in body]
        assert not missing, ("branding.PRODUCTS 里有 %s，但页面 %s() 没认，"
                             "会回落成默认线：%s" % (", ".join(branding.PRODUCTS),
                                                  name, ", ".join(missing)))


# ---- 3. 同一个 host：两条线各拿各的（跨层） ----

class _Req:
    def __init__(self, cookies=None, query=None, headers=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query = query or {}


def _page(product):
    import asyncio

    async def _ok(req):
        return None
    saved = server.require_token
    server.require_token = _ok
    try:
        r = asyncio.run(server.handle_list(_Req(cookies={"cb_product": product})))
        return r.text
    finally:
        server.require_token = saved


def test_same_host_serves_two_brands(monkeypatch):
    """host 自己登记哪条线都不能影响客户端看到的品牌位/标题（早先就是这里串的）。"""
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    a, c = _page("agentboss"), _page("codeboss")
    ta = re.search(r"<title>(.*?)</title>", a).group(1)
    tc = re.search(r"<title>(.*?)</title>", c).group(1)
    assert "代理工头" in ta and "口袋工头" not in ta, "agentboss 客户端拿到了工头的标题"
    assert "口袋工头" in tc and "代理工头" not in tc
    assert a != c
    # 页头品牌图按客户端线注入
    import brand_icons
    assert "data:image/png;base64," + brand_icons.BRAND_B64["agentboss"] in a
    assert "data:image/png;base64," + brand_icons.BRAND_B64["codeboss"] in c


def test_query_param_stamps_first_page(monkeypatch):
    """壳首屏 URL 带的 ?product= 优先于 cookie（cookie 还没种下的窗口）。"""
    monkeypatch.setattr(server, "_host_product", lambda: "codeboss")
    import asyncio

    async def _ok(req):
        return None
    saved = server.require_token
    server.require_token = _ok
    try:
        r = asyncio.run(server.handle_list(_Req(query={"product": "agentboss"})))
        assert "代理工头" in r.text
    finally:
        server.require_token = saved
