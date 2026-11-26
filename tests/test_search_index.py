# SPDX-License-Identifier: AGPL-3.0-or-later

"""找会话的索引 + 增量更新（CodeBoss 全文检索第二版）。

直扫转录能跑，但每次搜索都要把几百个 jsonl 重新解析一遍——手机上等不了。
索引落在同一个 sqlite 里：
- `msg_file`：每个转录读到哪了（offset/size），只读新追加的字节；
- `msg_idx`：每行消息一条干净文本（正文 + 工具入参，不含 json 噪音），
  查询就是一次 LIKE，命中片段直接从存下来的文本里截。
文件被截断/删掉时索引跟着清，不会搜出已经不存在的内容。

跑法： .venv/bin/python -m pytest tests/test_search_index.py -q
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _line(text, role="user"):
    return json.dumps({"type": role, "message": {"role": role,
                                                 "content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def _cands(p, sid="S1", last=100):
    return [{"sid": sid, "path": str(p), "title": sid, "agent": "claude", "last": last}]


def _db_at(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(str(path)))
    return path


def test_index_finds_and_is_incremental(tmp_path, monkeypatch):
    _db_at(tmp_path, monkeypatch)
    p = tmp_path / "a.jsonl"
    p.write_text(_line("部署 A"), encoding="utf-8")
    c = _cands(p)
    assert [x["sid"] for x in server._search_index(c, "部署")] == ["S1"]
    p.write_text(p.read_text(encoding="utf-8") + _line("登录页改成蓝色"), encoding="utf-8")
    out = server._search_index(c, "登录页")
    assert out and "登录页改成蓝色" in out[0]["snips"][0]
    con = sqlite3.connect(str(tmp_path / "t.db"))
    n = con.execute("SELECT COUNT(*) FROM msg_idx").fetchone()[0]
    con.close()
    assert n == 2, "老内容不重复入库（只读新追加的字节）"


def test_index_stores_clean_text(tmp_path, monkeypatch):
    _db_at(tmp_path, monkeypatch)
    p = tmp_path / "a.jsonl"
    p.write_text(_line("把登录页改蓝"), encoding="utf-8")
    server._search_index(_cands(p), "登录页")
    con = sqlite3.connect(str(tmp_path / "t.db"))
    txt = con.execute("SELECT txt FROM msg_idx").fetchone()[0]
    con.close()
    assert "登录页" in txt and '"type"' not in txt


def test_index_drops_stale_rows_when_file_rewritten(tmp_path, monkeypatch):
    _db_at(tmp_path, monkeypatch)
    p = tmp_path / "a.jsonl"
    p.write_text(_line("旧内容 部署"), encoding="utf-8")
    c = _cands(p)
    assert server._search_index(c, "部署")
    p.write_text(_line("新内容 登录页"), encoding="utf-8")   # 变短了 → 从 0 重扫
    assert server._search_index(c, "部署") == [], "旧内容不该还在索引里"
    assert server._search_index(c, "登录页"), "新内容要进索引"


def test_index_prunes_deleted_file(tmp_path, monkeypatch):
    _db_at(tmp_path, monkeypatch)
    p = tmp_path / "a.jsonl"
    p.write_text(_line("部署 A"), encoding="utf-8")
    c = _cands(p)
    assert server._search_index(c, "部署")
    p.unlink()
    assert server._search_index(c, "部署") == [], "文件没了，索引里也不该留"


def test_search_all_falls_back_to_direct_scan(tmp_path, monkeypatch):
    p = tmp_path / "a.jsonl"
    p.write_text(_line("部署 A"), encoding="utf-8")
    monkeypatch.setattr(server, "_search_candidates", lambda limit=300: _cands(p))
    monkeypatch.setattr(server, "_search_index",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("db 挂了")))
    assert [x["sid"] for x in server._search_all("部署")] == ["S1"]


def test_search_candidates_lists_agent_sessions(monkeypatch):
    class Ad:
        key = "claude"

        def closed_sessions(self, live, limit):
            return [{"sid": "S1", "cwd": "/p", "title": "t", "last": 5}]

        def transcript_path(self, cwd, sid):
            return "/p/%s.jsonl" % sid
    monkeypatch.setattr(server.agents, "adapters", lambda: [Ad()])
    c = server._search_candidates()
    assert c == [{"sid": "S1", "path": "/p/S1.jsonl", "title": "t",
                  "agent": "claude", "last": 5}]
