# SPDX-License-Identifier: AGPL-3.0-or-later

"""host 连中继的准入判定：指纹不符/冻结/未绑定 一律拒；匿名设备放行。

跑法： .venv/bin/python -m pytest tests/test_relay_w_link.py -q
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _db(tmp_path):
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    return sqlite3.connect(p)


def _dev(db, **kw):
    r = dict(id="d1", name="m", secret="s1", owner=None, frozen=0, fp="", anon=0)
    r.update(kw)
    db.execute("INSERT INTO devices(id,name,secret,owner,frozen,fp,anon,product) "
               "VALUES(:id,:name,:secret,:owner,:frozen,:fp,:anon,'codeboss')", r)
    db.commit()
    return r


def _check(db, sec="s1", fp=""):
    return relay._link_check(db, "d1", sec, fp)


def test_wrong_secret_403(tmp_path):
    db = _db(tmp_path)
    _dev(db, owner=1)
    _, err = _check(db, sec="nope")
    assert err[0] == 403


def test_frozen_423_so_host_keeps_registration(tmp_path):
    # 403 会让 host 清掉本机注册（凭证失效语义）；冻结是可恢复的，解冻后要能自己连回来
    db = _db(tmp_path)
    _dev(db, owner=1, frozen=1)
    _, err = _check(db)
    assert err[0] == 423 and "冻结" in err[1]


def test_fingerprint_mismatch_403(tmp_path):
    db = _db(tmp_path)
    _dev(db, owner=1, fp="a" * 64)
    _, err = _check(db, fp="b" * 64)
    assert err[0] == 403
    ok, err2 = _check(db, fp="a" * 64)
    assert err2 is None


def test_anon_device_allowed_without_owner(tmp_path):
    db = _db(tmp_path)
    _dev(db, anon=1, fp="a" * 64)
    row, err = _check(db, fp="a" * 64)
    assert err is None and row is not None


def test_old_unbound_still_409(tmp_path):
    db = _db(tmp_path)
    _dev(db, anon=0)
    _, err = _check(db)
    assert err[0] == 409


def test_legacy_device_without_fp_ignores_fp_arg(tmp_path):
    db = _db(tmp_path)
    _dev(db, owner=1, fp="")
    _, err = _check(db, fp="b" * 64)
    assert err is None, "老设备没记指纹时不能拿新指纹去卡它"
