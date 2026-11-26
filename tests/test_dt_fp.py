# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P 直连票据 dt 绑定开发机指纹：换个机器（同 secret 克隆/恢复）旧票据验不过。

跑法： .venv/bin/python -m pytest tests/test_dt_fp.py -q
"""
import sqlite3
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay
import server


def _app(tmp_path, fp="fp-abc"):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,owner,product) "
               "VALUES('d1','mac','sec',?,NULL,'codeboss')", (fp,))
    db.commit()
    return {"db": db}


def _host(monkeypatch, fp):
    monkeypatch.setattr(server, "_relay_cfg",
                        lambda: {"device_secret": "sec", "device_id": "d1"})
    monkeypatch.setattr(server.plat, "machine_fp", lambda: fp)


def test_dt_bound_to_same_fp(tmp_path, monkeypatch):
    dt, exp = relay._dt_sign(_app(tmp_path), "d1")
    _host(monkeypatch, "fp-abc")
    assert server._dt_claims(dt) == ("d1", exp)


def test_dt_rejected_on_other_fp(tmp_path, monkeypatch):
    dt, _exp = relay._dt_sign(_app(tmp_path), "d1")
    _host(monkeypatch, "fp-other")
    assert server._dt_claims(dt) is None


def test_dt_legacy_device_without_fp_still_works(tmp_path, monkeypatch):
    """老设备没录指纹：签旧格式，免得升级后直连全断。"""
    dt, _exp = relay._dt_sign(_app(tmp_path, fp=None), "d1")
    _host(monkeypatch, "whatever")
    assert server._dt_claims(dt) is not None
