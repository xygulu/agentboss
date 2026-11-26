# SPDX-License-Identifier: AGPL-3.0-or-later

"""匿名设备身份：devices 表要有指纹列 fp 和匿名标记 anon。

迁移必须幂等——中继每次启动都会把迁移列表跑一遍，老库补列不能抛错。

跑法： .venv/bin/python -m pytest tests/test_anon_device_schema.py -q
"""
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _cols(db, table):
    return {r[1] for r in db.execute("PRAGMA table_info(%s)" % table)}


def test_migrate_adds_fp_and_anon_idempotent(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    relay.db_init(p)                      # 第二次不能抛错
    db = sqlite3.connect(p)
    cols = _cols(db, "devices")
    assert "fp" in cols
    assert "anon" in cols
    idx = {r[1] for r in db.execute("PRAGMA index_list(devices)")}
    assert "idx_devices_fp" in idx


def test_anon_defaults_and_fp_unique_lookup(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO devices(id,name,secret,fp,anon) VALUES('d1','a','s1','ff',1)")
    db.commit()
    # 一台机器只有一台设备（指纹跨产品线唯一）：否则换线登记就多拿一份免费额度
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO devices(id,name,secret,fp,anon) VALUES('d2','b','s2','ff',1)")
    # 空指纹不占唯一（老设备/老 host 没有指纹）
    db.execute("INSERT INTO devices(id,name,secret,fp,anon) VALUES('d3','c','s3','',0)")
    db.commit()
    row = db.execute("SELECT anon FROM devices WHERE id='d1'").fetchone()
    assert row[0] == 1
