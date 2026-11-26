# SPDX-License-Identifier: AGPL-3.0-or-later

# tests/test_relay_oss_put.py —— 跑法：.venv/bin/python -m pytest tests/test_relay_oss_put.py -q
"""发布包直传 OSS 的两个关键点：PUT 签名动词用对、X-Oss-Done 让中继别再镜像一遍。"""
import base64, hashlib, hmac, sys, urllib.parse
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import relay

CFG = {"on": True, "endpoint": "oss-cn-hangzhou.aliyuncs.com", "bucket": "bkt",
       "ak": "AK", "sk": "SK", "prefix": ""}


def test_put_signature_uses_put_verb():
    """签名串第一行是 PUT（GET 的签名拿去 PUT 会被 OSS 403）。"""
    url = relay._oss_sign_url(CFG, "dist/codeboss-mac.dmg", 1800, "PUT")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    exp = int(q["Expires"][0])
    sts = "PUT\n\n\n%d\n/bkt/dist/codeboss-mac.dmg" % exp
    want = base64.b64encode(hmac.new(b"SK", sts.encode(), hashlib.sha1).digest()).decode()
    assert q["Signature"][0] == want
    assert q["OSSAccessKeyId"][0] == "AK"


def test_get_signature_unchanged():
    url = relay._oss_sign_url(CFG, "dist/x", 600)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    sts = "GET\n\n\n%d\n/bkt/dist/x" % int(q["Expires"][0])
    want = base64.b64encode(hmac.new(b"SK", sts.encode(), hashlib.sha1).digest()).decode()
    assert q["Signature"][0] == want


class _Req:
    def __init__(self, done):
        self.headers = {"x-oss-done": "1"} if done else {}


def test_x_oss_done_skips_mirror(monkeypatch):
    """发布脚本已直传过：中继不传 OSS、也不把这口出口流量记到 ops。"""
    counted = []
    monkeypatch.setattr(relay, "traffic_add", lambda *a, **k: counted.append(a))
    assert relay._oss_done(_Req(True)) is True
    assert relay._oss_done(_Req(False)) is False
    app = {"db": relay.db_init(":memory:")}
    relay._oss_mirror(app, "codeboss-mac.dmg", b"x" * 4096,
                      "application/x-apple-diskimage", skip=True)
    assert counted == []
