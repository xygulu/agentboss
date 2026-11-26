# SPDX-License-Identifier: AGPL-3.0-or-later

"""客户端名额按「设备」算，不按随机 cid 算（2026-11-21 用户实名反馈）。

实测同一个账号：
  · 壳的 WebView cookie 会丢 → 00_boot.js 每次重新生成 cb_cid，一台手机占好几个名额；
  · 后台提醒通道（BgService，UA 带 " bg"）没有 cookie → i:127.0.0.1|CodebossShell/x bg；
  · 壳的原生/WebView 请求也走过 i:IP|UA 兜底 → 同一台再记一份。
结果中兴手机点「预览」时这条请求换了身份，被当成第 11 台挡在墙外，而发消息正常。

口径：能读出机型的按「平台|机型」去重（版本号、cid 不进指纹）；读不出的保持原样。
后台提醒流不占名额（它是同一台手机的后台通道，不是客户端）。
"""
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

ZTE_UA = ("Mozilla/5.0 (Linux; Android 11; ZTE 7530N Build/RP1A.200720.011; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/87 Mobile Safari")
QIN_UA = "Dalvik/2.1.0 (Linux; U; Android 9; Qin 2 Pro) CodebossShell/0.11.27"
IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148 CodebossShell/0.2.31")
BG_UA = "CodebossShell/0.11.31 bg"


def _db(tmp_path):
    relay._CID_MEM.clear()      # 模块级节流/判定缓存：每个用例一份新库，别串
    relay._CLI_GATE.clear()
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.commit()
    return db


def test_ua_dev_fingerprint_ignores_version():
    assert relay._ua_dev(ZTE_UA) == "android|ZTE 7530N"
    assert relay._ua_dev(QIN_UA) == "android|Qin 2 Pro"
    assert relay._ua_dev(QIN_UA.replace("0.11.27", "0.11.30")) == relay._ua_dev(QIN_UA)
    assert relay._ua_dev(IOS_UA) == "ios|iphone"
    assert relay._ua_dev(BG_UA) == "", "后台通道没有机型，别乱认"


def test_lost_cookie_new_cid_is_the_same_seat(tmp_path):
    """壳丢了 cookie，页面重新生成 cb_cid：还是同一台手机、同一个名额。"""
    db = _db(tmp_path)
    relay.clients_touch(db, 7, "cid1", dev="android|ZTE 7530N")
    assert relay.clients_used(db, 7) == 1
    assert relay.clients_gate(db, 7, "cid2", dev="android|ZTE 7530N") is None
    relay.clients_touch(db, 7, "cid2", dev="android|ZTE 7530N")
    assert relay.clients_used(db, 7) == 1, "一台手机不要占两个名额"


def test_other_device_still_blocked(tmp_path):
    db = _db(tmp_path)
    relay.clients_touch(db, 7, "cid1", dev="android|ZTE 7530N")
    assert relay.clients_gate(db, 7, "cid9", dev="android|Qin 2 Pro") == (1, 1)


def test_unknown_device_keeps_old_behavior(tmp_path):
    """认不出机型（老 UA/没 UA）时不能白送：还是按 cid 各占一个。"""
    db = _db(tmp_path)
    relay.clients_touch(db, 7, "cid1")
    assert relay.clients_gate(db, 7, "cid2") == (1, 1)


def test_bg_channel_rows_do_not_count(tmp_path):
    db = _db(tmp_path)
    now = time.time()
    db.execute("INSERT INTO clients(uid,cid,first,last) VALUES(7,?,?,?)",
               ("i:127.0.0.1|CodebossShell/0.11.31 bg", now, now))
    db.execute("INSERT INTO clients(uid,cid,first,last) VALUES(7,'cid1',?,?)", (now, now))
    db.commit()
    assert relay.clients_used(db, 7) == 1, "后台提醒通道不是客户端"


def test_owner_clients_merges_duplicates(tmp_path):
    """App 的「已登录客户端」列表：同一台手机只出现一次。"""
    db = _db(tmp_path)
    now = time.time()
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(7,'cid1','android|ZTE 7530N',?,?)",
               (now - 100, now))
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(7,'cid2','android|ZTE 7530N',?,?)",
               (now - 50, now - 5))
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(7,'cid3','android|Qin 2 Pro',?,?)",
               (now - 50, now - 10))
    db.commit()
    d = relay._owner_clients(db, 7, cur_cid="cid2")
    assert d["used"] == 2
    assert len(d["clients"]) == 2
    zte = [c for c in d["clients"] if c["cid"] in ("cid1", "cid2")]
    assert len(zte) == 1 and zte[0]["current"] is True


def test_seat_wall_exempt_does_not_touch(tmp_path):
    """后台提醒/播报流不占名额。"""
    import types

    class _R:
        cookies = {}
        headers = {"User-Agent": BG_UA}
        remote = "127.0.0.1"

    db = _db(tmp_path)
    assert relay._seat_wall(_R(), db, 7, exempt=True) is None
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 0


def test_backfill_fills_dev_from_usage_ua(tmp_path):
    """老行没有 dev：启动时按 usage_client 里记的 UA 补上，立刻按设备去重。"""
    db = _db(tmp_path)
    now = time.time()
    db.execute("INSERT INTO clients(uid,cid,first,last) VALUES(7,'cid1',?,?)", (now, now))
    db.execute("INSERT INTO usage_client(uid,m,k,label,ua,last) VALUES(7,0,'c:cid1','',?,?)",
               (ZTE_UA, now))
    db.commit()
    relay._clients_dev_backfill(db)
    assert db.execute("SELECT dev FROM clients WHERE uid=7 AND cid='cid1'").fetchone()[0] \
        == "android|ZTE 7530N"
