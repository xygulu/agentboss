# SPDX-License-Identifier: AGPL-3.0-or-later

"""匿名态页面：未登录先见扫码/输码页；设备页显示本机名与今日额度。

跑法： .venv/bin/python -m pytest tests/test_anon_pages.py -q
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon,owner,product) "
               "VALUES('d1','书房 Mac','s','f',1,NULL,'codeboss')")
    db.commit()
    return db


def test_landing_is_pair_page_not_login():
    html = relay._anon_landing_html()
    assert 'id="pc"' in html and "/r/pair" in html, "先给扫码/输码入口"
    assert "已有账号" in html and "开通 Pro" in html, "登录/注册只从这两个入口进"
    assert 'id="v-login"' not in html, "首页不再甩登录表单"


def test_device_page_shows_name_and_today_quota(tmp_path):
    db = _db(tmp_path)
    subj = relay.subject_of(db, None, "d1")
    relay.usage_add(db, relay._llm_key(db, None, "d1"), 3)
    relay._asr_quota(db, "d1", add=30)
    relay.use_act(db, subj, relay._min_now(), "relay", dev="d1", kind="发消息", act="发消息")
    html = relay._anon_dev_page_html(db, "d1")
    lim = relay.plan_limits(db, subj)
    assert "书房 Mac" in html
    assert "1/%d" % lim["sends_day"] in html          # 今日发送
    assert "30/%d" % lim["asr_sec_day"] in html       # 今日语音秒数
    assert "3/%d" % lim["llm_day"] in html            # 今日智能调用
    assert "剩" in html and "开通 Pro" in html


def test_device_page_escapes_name(tmp_path):
    db = _db(tmp_path)
    db.execute("UPDATE devices SET name=? WHERE id='d1'", ("<b>x</b>",))
    db.commit()
    assert "<b>x</b>" not in relay._anon_dev_page_html(db, "d1")


def test_unauth_root_goes_to_pair_page(tmp_path):
    """未登录访问 /：扫码/输码页，不是登录表单（账号入口只在页内链接）。"""
    import asyncio
    import types
    db = _db(tmp_path)
    req = types.SimpleNamespace(
        app={"db": db, "key": b"k", "links": {}},
        cookies={}, path="/", method="GET", query={}, scheme="http",
        headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Test"},
        remote="203.0.113.9",
        rel_url=types.SimpleNamespace(raw_path_qs="/"))
    resp = asyncio.run(relay.tunnel(req))
    assert resp.status == 200 and 'id="pc"' in resp.text
    assert 'id="v-login"' not in resp.text
