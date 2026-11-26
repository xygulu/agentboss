# SPDX-License-Identifier: AGPL-3.0-or-later

"""开发机硬件指纹：稳定、只上报摘要、取不到时为空串（旧中继不受影响）。

跑法： .venv/bin/python -m pytest tests/test_machine_fp.py -q
"""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import plat


def _fresh(monkeypatch, raw):
    monkeypatch.setattr(plat, "_FP", [])
    monkeypatch.setattr(plat, "_fp_raw", lambda: raw)
    return plat.machine_fp()


def test_fp_is_sha256_of_source(monkeypatch):
    got = _fresh(monkeypatch, "UUID-123")
    assert got == hashlib.sha256(b"codeboss-fp:UUID-123").hexdigest()


def test_fp_empty_when_source_missing(monkeypatch):
    assert _fresh(monkeypatch, "") == ""


def test_fp_cached_and_stable(monkeypatch):
    monkeypatch.setattr(plat, "_FP", [])
    monkeypatch.setattr(plat, "_fp_raw", lambda: "SAME")
    a = plat.machine_fp()
    monkeypatch.setattr(plat, "_fp_raw", lambda: "CHANGED")
    assert plat.machine_fp() == a, "同一进程内应该只算一次"
