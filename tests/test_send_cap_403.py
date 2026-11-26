# SPDX-License-Identifier: AGPL-3.0-or-later

"""免费条数墙的 403 必须真的能发出去（回归：0.9.109 灰度机上首次踩中）。

sends 分支的 HTTPForbidden 带 content_type="application/json; charset=utf-8"，
aiohttp 禁止 content_type 里带 charset → ValueError → 500。用户看到的不是
「额度已用完，明天恢复」而是 Internal Server Error。改成不带 charset 的
"application/json"（aiohttp 对 str text 默认按 utf-8 编码）。

跑法： .venv/bin/python -m pytest tests/test_send_cap_403.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from aiohttp.test_utils import make_mocked_request
from aiohttp import web

import server


def _req(path="/api/ext_send", method="POST", token="tok-test"):
    return make_mocked_request(method, path + "?t=" + token, payload=b"")


def test_send_cap_403_is_not_a_500(monkeypatch):
    """额度墙触发时：必须抛 HTTPForbidden（带 JSON 说明），不能 ValueError/500。"""
    monkeypatch.setattr(server, "_tokens_current", lambda: {"tok-test"})
    monkeypatch.setattr(server, "_send_over", lambda: True)
    monkeypatch.setattr(server, "_send_cap_eff", lambda: 15)
    monkeypatch.setattr(server, "_USE_ST", {"sends": 15, "sends_local": 0})
    try:
        asyncio.run(server.require_token(_req()))
    except web.HTTPForbidden as e:
        body = json.loads(e.text)
        assert body["limit"] == "sends"
        assert "额度已用完" in body["error"]
        assert e.content_type == "application/json"
        return
    except ValueError as e:
        raise AssertionError("quota 403 崩成 %s: %s" % (type(e).__name__, e))
    raise AssertionError("额度墙没拦：/api/ext_send 在 _send_over()=True 时放行了")
