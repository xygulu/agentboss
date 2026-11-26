# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "shared"))
import branding


def test_values_unchanged():
    assert branding.RELAY_URL_DEFAULT == "https://relay.example.com"
    assert branding.SITE_URL == "https://example.com"
    assert branding.PRODUCTS == ("codeboss", "agentboss")
    assert branding.PRODUCT_DEFAULT == "codeboss"
    assert branding.valid_product("agentboss") == "agentboss"
    assert branding.valid_product("garbage") == "codeboss"
    assert branding.valid_product("") == "codeboss"


def test_no_domain_literals_outside_branding():
    for f in ("host/server.py", "relay/relay.py"):
        text = (HERE / f).read_text(encoding="utf-8")
        # relay.py 里的兜底块就是 branding.py 的单文件副本（等值性由
        # tests/test_relay_single_file.py 钉住），不算字面量散落回业务代码
        while "# __BRANDING_FALLBACK_BEGIN__" in text:
            a = text.index("# __BRANDING_FALLBACK_BEGIN__")
            b = text.index("# __BRANDING_FALLBACK_END__")
            text = text[:a] + text[b:]
        assert "relay.example.com" not in text, f
        assert "example.com" not in text, f
