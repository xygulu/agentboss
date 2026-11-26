# SPDX-License-Identifier: AGPL-3.0-or-later

"""跨机器搜索（中继侧）：owner 名下每台在线机器各搜一遍，合并成 machines[]。

不缓存（查询带着用户输入，一人一查），但只问在线机器、最多 4 台；
`/api/search?remote=0` 断掉 host↔中继的递归。

跑法： .venv/bin/python -m pytest tests/test_relay_search.py -q
"""
import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
from test_relay_ledger import _app, _db  # 同一套假设备和假 link


def test_search_merges_online_machines(tmp_path, monkeypatch):
    db = _db(tmp_path)
    app = _app({})
    paths = []

    async def fake_link(app_, dev, path, m="GET", body=None, secs=6):
        paths.append((dev, path))
        return {"ok": True, "q": "部署", "items": [
            {"sid": "sid-" + dev, "title": dev, "last": 5, "snips": ["部署了 " + dev]}]}
    monkeypatch.setattr(relay, "_link_api", fake_link)
    app["links"]["d1"] = type("L", (), {"closed": False})()
    app["links"]["d2"] = type("L", (), {"closed": False})()
    d = asyncio.run(relay._dev_search_payload(app, db, 7, "部署"))
    assert [m["dev"] for m in d["machines"]] == ["d1", "d2"], "离线/别人的机器不问"
    it = d["machines"][1]["items"][0]
    assert it["machine"] == "macbook" and it["dev"] == "d2"
    # remote=0：中继只要「这台机器的本机结果」，否则 host 反过来又去问中继（递归）
    for dev, path in paths:
        u = urlparse(path)
        assert u.path == "/api/search"
        qs = parse_qs(u.query)
        assert qs.get("q") == ["部署"] and qs.get("remote") == ["0"]


def test_search_at_most_four_machines(tmp_path, monkeypatch):
    db = _db(tmp_path)
    for i in range(5, 10):
        db.execute("INSERT INTO devices(id,name,secret,fp,anon,product,frozen,owner,last_seen)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   ("x%d" % i, "m%d" % i, "s", "f%d" % i, 0, "agentboss", 0, 7, i))
    db.commit()
    app = _app({})
    for i in range(5, 10):
        app["links"]["x%d" % i] = type("L", (), {"closed": False})()

    async def fake_link(app_, dev, path, m="GET", body=None, secs=6):
        return {"ok": True, "items": []}
    monkeypatch.setattr(relay, "_link_api", fake_link)
    d = asyncio.run(relay._dev_search_payload(app, db, 7, "x"))
    assert len(d["machines"]) == 4


def test_search_needs_query(tmp_path):
    db = _db(tmp_path)
    d = asyncio.run(relay._dev_search_payload(_app({}), db, 7, "   "))
    assert d == {"ok": False, "error": "给个关键词"}
