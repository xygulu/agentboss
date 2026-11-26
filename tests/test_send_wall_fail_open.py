# SPDX-License-Identifier: AGPL-3.0-or-later

"""收费墙只挂官方中继（2026-11-26 拍板口径：开源版+自建中继都免费）。

三态：
- 自建中继（caps.selfhost=1）或裸机（没配 relay）→ 无墙。自建 relay 本来就
  下发全 0（plan_limits 的 SELFHOST 分支），但 host 端「账不可信」的回落档
  （免费 15 条/天）会把自建用户和对账失败的窗口一起误伤——短路掉。
- 官方中继 → 墙照旧：下发值权威；没拿到/对账过期 15 分钟回落免费档
  （fail-closed，0.9.10x 复查加固：防断链逃逸官方条数墙，保留不动）。

跑法： .venv/bin/python -m pytest tests/test_send_wall_fail_open.py -q
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _st(**kw):
    base = {"sends": 0, "sends_local": 0, "sends_cap": 0,
            "sends_cap_known": False, "synced": time.time()}
    base.update(kw)
    return base


def _mk(monkeypatch, st, selfhost=False, url="https://relay.example"):
    monkeypatch.setattr(server, "_USE_ST", st)
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_roll_day", lambda: None)
    monkeypatch.setattr(server, "_use_reconcile", lambda: None)
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": url})
    monkeypatch.setattr(server, "_CAPS_CACHE", {"ts": time.time(), "ok": True,
                                                "val": {"selfhost": selfhost}})


# ---- 条数墙：自建 / 裸机 → 无墙 ----

def test_selfhost_no_wall_even_over(monkeypatch):
    _mk(monkeypatch, _st(sends=999, sends_local=999, sends_cap=15,
                         sends_cap_known=True), selfhost=True)
    assert server._send_cap_eff() == 0
    assert server._send_over() is False


def test_bare_host_no_wall(monkeypatch):
    """没配 relay 的开源裸机：免费无墙（对官方服务的开支为零）。"""
    _mk(monkeypatch, _st(sends=999, sends_local=999), url="")
    assert server._send_cap_eff() == 0
    assert server._send_over() is False


def test_selfhost_short_circuit_survives_stale_sync(monkeypatch):
    """自建 host 对账失败/过期也不能被回落档挡住（本轮修的正是这个误伤）。"""
    _mk(monkeypatch, _st(sends=15, sends_local=15, sends_cap=15,
                         sends_cap_known=True, synced=time.time() - 1000),
        selfhost=True)
    assert server._send_over() is False


# ---- 条数墙：官方 → 照旧（下发值权威；回落档保留）----

def test_official_unknown_falls_back_to_free_cap(monkeypatch):
    _mk(monkeypatch, _st(sends=15, sends_local=15))
    assert server._send_cap_eff() == 15
    assert server._send_over() is True


def test_official_stale_falls_back_to_free_cap(monkeypatch):
    _mk(monkeypatch, _st(sends=15, sends_local=15, sends_cap=15,
                         sends_cap_known=True, synced=time.time() - 1000))
    assert server._send_cap_eff() == 15
    assert server._send_over() is True


def test_official_known_unlimited_no_wall(monkeypatch):
    _mk(monkeypatch, _st(sends=999, sends_cap=0, sends_cap_known=True))
    assert server._send_cap_eff() == 0
    assert server._send_over() is False


def test_official_free_cap_walls_when_synced(monkeypatch):
    _mk(monkeypatch, _st(sends=15, sends_local=15, sends_cap=15,
                         sends_cap_known=True))
    assert server._send_cap_eff() == 15
    assert server._send_over() is True


def test_official_free_under_cap_passes(monkeypatch):
    _mk(monkeypatch, _st(sends=3, sends_local=0, sends_cap=15,
                         sends_cap_known=True))
    assert server._send_over() is False


# ---- 功能墙：自建 / 裸机 → 全开；官方回落档不动 ----

def test_feature_wall_selfhost_all_on(monkeypatch):
    _mk(monkeypatch, _st(), selfhost=True)
    for name in ("terminal", "remind", "auto"):
        assert server._feat_on(name) is True, name


def test_feature_wall_bare_host_all_on(monkeypatch):
    _mk(monkeypatch, _st(), url="")
    for name in ("terminal", "remind", "auto"):
        assert server._feat_on(name) is True, name


def test_feature_wall_official_missing_snapshot_stays_free_tier(monkeypatch):
    """官方中继、快照缺失：回落免费档（复查加固语义，不动）。"""
    _mk(monkeypatch, _st())
    assert server._feat_on("terminal") is False
