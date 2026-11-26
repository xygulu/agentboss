# SPDX-License-Identifier: AGPL-3.0-or-later

"""product line must survive the relay hop.

The shell plants a `cb_product` cookie; the host reads X-Product (or the cookie)
to decide which agent tabs exist (codeboss = claude/codex/opencode, agentboss = all).
The relay strips cookies before forwarding, so before this fix a codeboss client
looked like "no product" and the host fell back to its own product line.

2026-11-18 macmini: codeboss app showed Hermes too.

跑法： .venv/bin/python -m pytest tests/test_relay_fwd_headers.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_product_cookie_becomes_x_product():
    h = relay._fwd_headers(_Req(cookies={"cb_product": "codeboss"}))
    assert h["X-Product"] == "codeboss"
    assert "cookie" not in {k.lower() for k in h}


def test_explicit_header_wins_over_cookie():
    h = relay._fwd_headers(_Req(headers={"X-Product": "agentboss"},
                                cookies={"cb_product": "codeboss"}))
    assert h["X-Product"] == "agentboss"


def test_bogus_product_is_ignored():
    h = relay._fwd_headers(_Req(cookies={"cb_product": "<script>"}))
    assert "X-Product" not in h
