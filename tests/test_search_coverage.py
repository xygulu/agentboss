# SPDX-License-Identifier: AGPL-3.0-or-later

"""找会话的覆盖面与排序（2026-11-21 用户：「找会话没什么用」）。

两个实测问题：
1. 索引预算「最近活跃优先」，全被长会话吃掉——这台机器 31 个转录只索引了 5 个，
   老会话里的内容根本搜不到（预算 20MB/次，热会话每次都有新追加）。
   一个文件本轮最多读 budget/文件数，保证每个都往前走；追平存量靠后台回填。
2. 排序按「最新一条命中行」——常开的大会话永远排第一。改成命中条数优先，
   同条数再按最近活跃。

跑法： .venv/bin/python -m pytest tests/test_search_coverage.py -q
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _line(text):
    return json.dumps({"type": "user", "message": {"role": "user",
                                                   "content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def _cands(rows):
    return [{"sid": s, "path": str(p), "title": s, "agent": "claude", "last": last}
            for (s, p, last) in rows]


def test_budget_is_shared_between_files(tmp_path):
    """热文件再大，也不能把本轮预算吃光——老文件至少往前走一段。"""
    hot = tmp_path / "hot.jsonl"
    hot.write_text("".join(_line("热点 %d" % i) for i in range(4000)), encoding="utf-8")
    old = []
    for i in range(3):
        p = tmp_path / ("old%d.jsonl" % i)
        p.write_text("".join(_line("老会话%d %d" % (i, j)) for j in range(200)),
                     encoding="utf-8")
        old.append(p)
    cands = _cands([("HOT", hot, 999)] + [("O%d" % i, p, 100)
                                          for i, p in enumerate(old)])
    con = sqlite3.connect(":memory:")
    server._idx_migrate(con)
    server._idx_update(con, cands, max_bytes=60 * 1024)
    seen = {r[0] for r in con.execute("SELECT DISTINCT path FROM msg_file")}
    assert seen == {str(hot)} | {str(p) for p in old}, "有文件这一轮完全没被读"
    for p in old:
        n = con.execute("SELECT COUNT(*) FROM msg_idx WHERE path=?", (str(p),)).fetchone()[0]
        assert n > 0, "%s 没进索引" % p.name


def test_repeated_rounds_reach_the_whole_backlog(tmp_path):
    """每轮都有进展，反复跑最终追平（后台回填就是靠这个收敛）。"""
    ps = []
    for i in range(3):
        p = tmp_path / ("s%d.jsonl" % i)
        p.write_text("".join(_line("内容%d %d" % (i, j)) for j in range(400)),
                     encoding="utf-8")
        ps.append(p)
    cands = _cands([("S%d" % i, p, 100 - i) for i, p in enumerate(ps)])
    con = sqlite3.connect(":memory:")
    server._idx_migrate(con)
    for _ in range(200):
        if server._idx_update(con, cands, max_bytes=20 * 1024) == 0:
            break
    for p in ps:
        size = con.execute("SELECT size FROM msg_file WHERE path=?", (str(p),)).fetchone()[0]
        assert size == os.path.getsize(p), "%s 没追平" % p.name


def test_ranking_prefers_sessions_with_more_matches():
    """同一关键词：命中 5 条的会话排在只命中 1 条（虽然更新）的前面。"""
    con = sqlite3.connect(":memory:")
    server._idx_migrate(con)
    rows = [("pA", i, 1000 + i, "A", "claude", "会话A", "部署 第%d次" % i, "部署 第%d次" % i)
            for i in range(5)]
    rows.append(("pB", 0, 5000, "B", "claude", "会话B", "部署 一次", "部署 一次"))
    con.executemany("INSERT INTO msg_idx(path,off,ts,sid,agent,title,txt,tl)"
                    " VALUES(?,?,?,?,?,?,?,?)", rows)
    out = server._idx_query(con, "部署")
    assert out and out[0]["sid"] == "A"
    assert [x["sid"] for x in out] == ["A", "B"]


def test_warm_round_reports_and_finishes(tmp_path, monkeypatch):
    """后台回填的一轮：有存量 → 返回读了多少；追平 → 0（好让循环去睡觉）。"""
    p = tmp_path / "a.jsonl"
    p.write_text(_line("部署 A"), encoding="utf-8")
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(str(tmp_path / "w.db")))
    monkeypatch.setattr(server, "_search_candidates", lambda limit=300: _cands([("A", p, 1)]))
    assert server._search_warm_round() > 0
    assert server._search_warm_round() == 0
