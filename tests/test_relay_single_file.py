#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""relay.py 必须能单文件部署。

/r/update 只携带 relay.py 一个文件（_unwrap_update 只取它），branding.py 不会
跟着上云。生产上 `import branding` 一旦失败，进程直接起不来——整条中继全灭。
所以 relay.py 里要有一段与 branding.py 等值的兜底。

跑法：.venv/bin/python -m pytest tests/test_relay_single_file.py -q
"""
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_inline_branding_fallback_matches_branding_py():
    src = (HERE / "relay" / "relay.py").read_text(encoding="utf-8")
    a = src.rindex("\n", 0, src.index("# __BRANDING_FALLBACK_BEGIN__"))
    b = src.index("# __BRANDING_FALLBACK_END__")
    ns = {}
    exec(compile(textwrap.dedent(src[a:b]), "<branding-fallback>", "exec"), ns)
    fb = ns["branding"]

    import branding as real

    for name in ("RELAY_HOST_DEFAULT", "RELAY_URL_DEFAULT", "SITE_URL",
                 "PRODUCT_CODEBOSS", "PRODUCT_AGENTBOSS", "PRODUCTS",
                 "PRODUCT_DEFAULT", "SERIES", "PRODUCT_NAMES"):
        assert getattr(fb, name) == getattr(real, name), name
    for p in (None, "", "codeboss", "agentboss", "x"):
        assert fb.valid_product(p) == real.valid_product(p), p
        assert fb.product_name(p) == real.product_name(p), p


def test_relay_imports_survive_without_branding_py():
    """兜底要真的生效：模拟 branding 不可导入，relay.py 顶层导入不能抛。"""
    src = (HERE / "relay" / "relay.py").read_text(encoding="utf-8")
    head = src[:src.index("\ndef ", src.index("import sqlite3"))]
    ns = {"__name__": "relay_probe", "__file__": str(HERE / "relay" / "relay.py")}

    class Block:
        def find_module(self, name, path=None):
            return self if name == "branding" else None

        def find_spec(self, name, path=None, target=None):
            if name == "branding":
                raise ImportError("模拟单文件部署：没有 branding.py")
            return None

    import sys
    old = sys.meta_path[:]
    sys.meta_path.insert(0, Block())
    sys.modules.pop("branding", None)
    try:
        exec(compile(head, "<relay-head>", "exec"), ns)
    finally:
        sys.meta_path[:] = old
        sys.modules.pop("branding", None)
    assert ns["branding"].PRODUCT_DEFAULT == "codeboss"
