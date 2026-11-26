# SPDX-License-Identifier: AGPL-3.0-or-later

"""开发机侧的墙：中继没给权威值/太久没对账 → 按免费档拦，不是放行。

跑法： .venv/bin/python -m pytest tests/test_host_fail_closed.py -q
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _st(monkeypatch, **kw):
    monkeypatch.setattr(server, "_use_load", lambda: None)
    monkeypatch.setattr(server, "_use_dirty", lambda: None)
    monkeypatch.setattr(server, "_use_reconcile", lambda *a, **k: None)
    # url 非空 = 挂官方中继（2026-11-26 三态口径：url 空 = 裸机免费无墙，
    # 这份文件测的是官方档的 fail-closed，必须显式指官方）
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": "https://relay.example"})
    server._USE_ST.clear()
    server._USE_ST.update({
        "date": server._use_today(), "sends": 0, "sends_local": 0,
        "sends_cap": 0, "sends_cap_known": False, "synced": 0.0,
        "feat": {}, "feat_ts": 0.0, "used": 0, "cap": 0, "cap_known": False,
        "loaded": True,
    })
    server._USE_ST.update(kw)


def test_no_authority_blocks_at_free_cap(monkeypatch):
    _st(monkeypatch, sends_local=server._FREE_SENDS_CAP)
    assert server._send_over() is True, "没有权威值也要按免费档拦"
    _st(monkeypatch, sends_local=server._FREE_SENDS_CAP - 1)
    assert server._send_over() is False


def test_stale_authority_blocks_at_free_cap(monkeypatch):
    _st(monkeypatch, sends_cap=200, sends_cap_known=True, sends=200,
        synced=time.time() - 1000)
    assert server._send_over() is True, "15 分钟没对账就不能信旧上限"


def test_fresh_authority_uses_relay_cap(monkeypatch):
    _st(monkeypatch, sends_cap=200, sends_cap_known=True, sends=15, synced=time.time())
    assert server._send_over() is False
    server._USE_ST["sends"] = 200
    assert server._send_over() is True


def test_feat_on_without_snapshot_is_free(monkeypatch):
    _st(monkeypatch)
    assert server._feat_on("terminal") is False
    assert server._feat_on("terminal", default=True) is False


def test_feat_on_with_fresh_snapshot(monkeypatch):
    _st(monkeypatch, feat={"terminal": True}, feat_ts=time.time())
    assert server._feat_on("terminal") is True
