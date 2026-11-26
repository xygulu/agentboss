# SPDX-License-Identifier: AGPL-3.0-or-later

"""灰度常驻：开发机选进灰度一次之后，以后每次发版都自动拿最新，不用重选。

以前每条规则钉一个版本号（规则建时那版），发新版得再拉一遍名单；老规则一多，
开发机就停在旧版上（2026-11-20 macmini 卡 0.9.61 就是这么来的）。
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Req:
    def __init__(self, db, body=None, dev="", cid=""):
        self.app = {"db": db, "key": b"k"}
        self._body = body or {}
        self.query = {"dev": dev, "cid": cid}
        self.headers = {}
        self.cookies = {}
        self.remote = "127.0.0.1"
        self.scheme = "http"
        self.method = "POST"

    async def json(self):
        return self._body

    async def read(self):
        return b""


def _env(tmp_path, monkeypatch, cfg=None):
    cf = tmp_path / "canary.json"
    cf.write_text(json.dumps(cfg if cfg is not None else {}), encoding="utf-8")
    monkeypatch.setattr(relay, "CANARY_FILE", str(cf))
    monkeypatch.setattr(relay, "_pkg_vers", lambda: ["0.9.64", "0.9.63", "0.9.61"])
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    return db, cf


def _post(db, body):
    async def go():
        return await relay.r_admin_canary(_Req(db, body))
    r = asyncio.run(go())
    return json.loads(r.text)


def _pick(db, dev):
    req = _Req(db, dev=dev)
    return relay._canary_pick(req.app, req)


def test_added_dev_is_gray_and_gets_newest(tmp_path, monkeypatch):
    """加进规则 = 进灰度常驻：规则钉的是旧版，也照样拿最新。"""
    db, cf = _env(tmp_path, monkeypatch, {"rules": [
        {"id": "r1", "ver": "0.9.61", "apk": 0, "devs": [], "clients": [], "users": []}]})
    monkeypatch.setattr(relay, "_adm_auth", lambda *a: _true())
    monkeypatch.setattr(relay, "_adm_sensitive", lambda *a: _true())
    d = _post(db, {"action": "add", "rule": "r1", "kind": "devs", "ids": ["macmini"]})
    assert d["ok"], d
    on_disk = json.loads(cf.read_text(encoding="utf-8"))
    assert "macmini" in on_disk["gray"]["devs"]
    assert _pick(db, "macmini")["ver"] == "0.9.64"
    assert _pick(db, "macmini")["rule"] == "gray"


def test_legacy_rule_members_seed_gray(tmp_path, monkeypatch):
    """升级前就有规则、没有 gray 字段：老成员直接算灰度常驻，不要求重勾。"""
    db, _ = _env(tmp_path, monkeypatch, {"rules": [
        {"id": "r1", "ver": "0.9.61", "apk": 0, "devs": ["macmini"], "clients": [], "users": []}]})
    p = _pick(db, "macmini")
    assert p["ver"] == "0.9.64" and p["rule"] == "gray"


def test_non_gray_dev_still_follows_rule(tmp_path, monkeypatch):
    db, _ = _env(tmp_path, monkeypatch, {"rules": [
        {"id": "r1", "ver": "0.9.61", "apk": 0, "devs": ["other"], "clients": [], "users": []}],
        "gray": {"devs": [], "clients": [], "users": []}})
    assert _pick(db, "other")["ver"] == "0.9.61"


def test_gray_survives_rule_deletion(tmp_path, monkeypatch):
    """删规则不该把开发机踢出灰度——不然下次发版又要重选。"""
    db, cf = _env(tmp_path, monkeypatch, {"rules": [
        {"id": "r1", "ver": "0.9.61", "apk": 0, "devs": ["macmini"], "clients": [], "users": []}],
        "gray": {"devs": ["macmini"], "clients": [], "users": []}})
    monkeypatch.setattr(relay, "_adm_auth", lambda *a: _true())
    monkeypatch.setattr(relay, "_adm_sensitive", lambda *a: _true())
    d = _post(db, {"action": "del_rule", "rule": "r1"})
    assert d["ok"], d
    assert "macmini" in d["gray"]["devs"]
    assert _pick(db, "macmini")["ver"] == "0.9.64"


def test_del_removes_from_gray(tmp_path, monkeypatch):
    db, cf = _env(tmp_path, monkeypatch, {"rules": [
        {"id": "r1", "ver": "0.9.61", "apk": 0, "devs": ["macmini"], "clients": [], "users": []}],
        "gray": {"devs": ["macmini"], "clients": [], "users": []}})
    monkeypatch.setattr(relay, "_adm_auth", lambda *a: _true())
    monkeypatch.setattr(relay, "_adm_sensitive", lambda *a: _true())
    d = _post(db, {"action": "del", "rule": "r1", "kind": "devs", "ids": ["macmini"]})
    assert d["ok"], d
    assert "macmini" not in d["gray"]["devs"]


def test_gray_del_without_rule(tmp_path, monkeypatch):
    """灰度名单里的 × 不依赖规则还在不在。"""
    db, _ = _env(tmp_path, monkeypatch, {
        "gray": {"devs": ["macmini"], "clients": [], "users": []}})
    monkeypatch.setattr(relay, "_adm_auth", lambda *a: _true())
    monkeypatch.setattr(relay, "_adm_sensitive", lambda *a: _true())
    d = _post(db, {"action": "gray_del", "kind": "devs", "ids": ["macmini"]})
    assert d["ok"], d
    assert d["gray"]["devs"] == []
    assert _pick(db, "macmini")["ver"] == ""


def _true():
    async def go(*a, **k):
        return True
    return go()
