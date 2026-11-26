# SPDX-License-Identifier: AGPL-3.0-or-later

"""交付链（2026-11-19 计划 A4）：发布 → 健康卡 → 回滚。

- 跑什么由 products/<线>.json 的 deploy 配置定，App 只触发（传不进命令）；
- 配置命令不许夹带 shell 串联；
- 发布/回滚都落 deploys 台账；回滚要求有过成功发布；
- 健康卡 = 真实探活（HTTP）+ 最近发布记录。

跑法：.venv/bin/python -m pytest tests/test_deploy_chain.py -q
"""
import asyncio
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

SCHEMA = ("CREATE TABLE deploys(n INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT,"
          " kind TEXT, cmd TEXT, state TEXT, rc INTEGER, ts REAL, dur REAL, tail TEXT)")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.commit()
    con.close()
    monkeypatch.setattr(server, "_db", lambda: sqlite3.connect(path))
    return path


@pytest.fixture
def sess(tmp_path, monkeypatch):
    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    return tmp_path


@pytest.fixture
def cfg(monkeypatch):
    c = {"cmd": "npm run deploy", "rollback": "npm run deploy:rollback",
         "health_url": ""}
    monkeypatch.setattr(server, "_deploy_cfg", lambda p: dict(c) if p == "codeboss" else {})
    return c


def test_no_config_no_project_means_no_deploy(db, sess, monkeypatch):
    """两条来源都没有就不放行：产品线配置只是兜底，默认空着（命令跟项目走）。"""
    monkeypatch.setattr(server, "_deploy_cfg", lambda p: {})
    r = asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    assert not r["ok"] and "没配" in r["error"]


def test_agentboss_not_configured(db, sess):
    r = asyncio.run(server._deploy_run_payload("s1", "agentboss"))
    assert not r["ok"] and "没配" in r["error"]


def test_project_deploy_script_wins(db, sess, cfg, monkeypatch):
    """会话项目里配了 deploy 脚本就跑项目的，产品线配置只是兜底。"""
    (sess / "package.json").write_text(json.dumps(
        {"scripts": {"deploy": "wrangler deploy", "deploy:rollback": "wrangler rollback"}}))
    cfg["cmd"], cfg["rollback"] = "npm run ship", "npm run unship"   # 配置里的不生效
    calls = []
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: calls.append(cmd) or (0, "ok"))
    r = asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    assert r["ok"] and calls == ["npm run deploy"]
    r = asyncio.run(server._deploy_rollback_payload("s1", "codeboss"))
    assert r["ok"] and calls[-1] == "npm run deploy:rollback"


def test_run_records_pass(db, sess, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: calls.append(cmd) or (0, "deployed"))
    r = asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    assert r["ok"] and r["item"]["state"] == "pass" and calls == ["npm run deploy"]
    row = sqlite3.connect(db).execute("SELECT kind, cmd, state FROM deploys").fetchone()
    assert row == ("deploy", "npm run deploy", "pass")


def test_run_records_fail(db, sess, cfg, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (1, "boom"))
    r = asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    assert r["ok"] and r["item"]["state"] == "fail"


def test_shell_chaining_in_config_rejected(db, sess, cfg, monkeypatch):
    cfg["cmd"] = "npm run deploy && rm -rf ~"
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, ""))
    r = asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    assert not r["ok"] and "串联" in r["error"]


def test_rollback_needs_successful_deploy(db, sess, cfg, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (1, "boom"))
    asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    r = asyncio.run(server._deploy_rollback_payload("s1", "codeboss"))
    assert not r["ok"] and "成功发布" in r["error"]


def test_rollback_runs_after_success(db, sess, cfg, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, "ok"))
    asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    r = asyncio.run(server._deploy_rollback_payload("s1", "codeboss"))
    assert r["ok"] and r["item"]["cmd"] == "npm run deploy:rollback"
    kinds = [x[0] for x in sqlite3.connect(db).execute("SELECT kind FROM deploys ORDER BY n")]
    assert kinds == ["deploy", "rollback"]


def test_health_card_merges_probe_and_deploys(db, sess, cfg, monkeypatch):
    monkeypatch.setattr(server, "_test_exec", lambda cmd, cwd, to: (0, "ok"))
    asyncio.run(server._deploy_run_payload("s1", "codeboss"))
    monkeypatch.setattr(server, "_health_probe", lambda url, to=6: {"ok": True, "code": 200, "ms": 12})
    cfg["health_url"] = "https://example.com/health"
    r = asyncio.run(server._health_card_payload("codeboss"))
    assert r["ok"] and r["health"]["code"] == 200
    assert r["deploys"][0]["state"] == "pass" and r["deploys"][0]["kind"] == "deploy"


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_probe_real_http():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = server._health_probe("http://127.0.0.1:%d/" % srv.server_port)
        assert r["ok"] and r["code"] == 200
    finally:
        srv.shutdown()
    bad = server._health_probe("http://127.0.0.1:1/", 1)
    assert not bad["ok"] and bad["error"]
