# SPDX-License-Identifier: AGPL-3.0-or-later

"""浏览器算客户端，但闲置一段时间自动踢掉、名额放回去，下次要重新登录（2026-11-21）。

用户口径（第二次澄清）：浏览器**要**占名额；只是不用了要自动踢掉释放名额，
不是永远留着，也不是直接豁免。实现：
  · 浏览器照常登记、占名额；
  · 闲置超过 BROWSER_IDLE_SEC 的浏览器行由定时清扫删掉，记一条 client_kick；
  · 被踢的浏览器带着旧 r_sess 回来：cookie 当场作废、按未登录处理；
  · 重新登录成功（响应里有新 r_sess）就清掉 kick 记录，可以正常再用；
  · 浏览器登录态是会话 cookie（关浏览器即退出）；App/壳照旧 7 天，且永不按浏览器规则踢。
壳的 WebView（"; wv)"）和 Dalvik 原生请求都是 App，不能被误判成浏览器。
"""
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay

MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
ZTE_UA = ("Mozilla/5.0 (Linux; Android 11; ZTE 7530N Build/RP1A.200720.011; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/87 Mobile Safari")
QIN_UA = "Dalvik/2.1.0 (Linux; U; Android 9; Qin 2 Pro) CodebossShell/0.11.27"


def _db(tmp_path):
    relay._CID_MEM.clear()
    relay._CLI_GATE.clear()
    relay._PRUNE_AT = 0.0
    p = str(tmp_path / "relay.db")
    relay.db_init(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO users(id,name,pass_hash,salt,created,plan,plan_expires)"
               " VALUES(7,'u','h','s',0,'free',0)")
    db.commit()
    return db


class _Req:
    def __init__(self, ua, db=None, cookies=None, remote="127.0.0.1"):
        self.headers = {"User-Agent": ua}
        self.cookies = cookies or {}
        self.remote = remote
        self.app = {"db": db}
        self.path = "/"
        self.method = "GET"
        self.scheme = "https"


def _browse_row(db, cid="browcid", ua=MAC_SAFARI, last=None, uid=7):
    now = time.time()
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(?,?,?,?,?)",
               (uid, cid, "mac", now - 100, last if last is not None else now))
    db.execute("INSERT INTO usage_client(uid,m,k,label,ua,n,first,last) VALUES(?,?,?,?,?,?,?,?)",
               (uid, relay._min_now(), relay._use_key(cid), "", ua, 0, now, now))
    db.commit()


def test_browser_request_occupies_a_seat(tmp_path):
    """浏览器是终端：照常登记、占名额（用户口径）。"""
    db = _db(tmp_path)
    assert relay._seat_wall(_Req(MAC_SAFARI, db), db, 7) is None
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 1
    assert relay.clients_used(db, 7) == 1


def test_idle_browser_is_kicked_and_releases_seat(tmp_path):
    db = _db(tmp_path)
    _browse_row(db, last=time.time() - relay.BROWSER_IDLE_SEC - 60)
    assert relay._browser_sweep(db) == 1
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 0
    assert db.execute("SELECT 1 FROM client_kick WHERE cid='browcid'").fetchone()


def test_kick_removes_it_from_the_client_list_entirely(tmp_path):
    """踢掉就要从列表消失：clients 行和查账明细（usage_client）一起删，别留半截。"""
    db = _db(tmp_path)
    _browse_row(db, last=time.time() - relay.BROWSER_IDLE_SEC - 60)
    assert relay._browser_sweep(db) == 1
    left = db.execute("SELECT COUNT(*) FROM usage_client WHERE uid=7"
                      " AND k=?", (relay._use_key("browcid"),)).fetchone()[0]
    assert left == 0


def test_kick_works_even_without_usage_ua(tmp_path):
    """浏览器只刷页面、从不写操作时 usage_client 里没有 UA，cid 里也没有——
    能不能认出来靠登记那一刻记下的 kind（线上就有一批这样的行踢不掉）。"""
    db = _db(tmp_path)
    now = time.time()
    req = _Req(MAC_SAFARI, db, cookies={"cb_cid": "cmwrealcid"})
    assert relay._seat_wall(req, db, 7) is None
    assert not db.execute("SELECT 1 FROM usage_client").fetchone()
    assert relay._browser_sweep(db, now + relay.BROWSER_IDLE_SEC + 60) == 1
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 0


def test_active_browser_is_not_kicked(tmp_path):
    db = _db(tmp_path)
    _browse_row(db, last=time.time() - 60)
    assert relay._browser_sweep(db) == 0
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 1


def test_app_client_is_never_kicked_by_browser_idle(tmp_path):
    """App 掉线不按浏览器规则踢：24 小时才释放（CLIENT_SEAT_SEC）。"""
    db = _db(tmp_path)
    now = time.time()
    db.execute("INSERT INTO clients(uid,cid,dev,first,last) VALUES(7,?,?,?,?)",
               ("i:127.0.0.1|" + QIN_UA[:80], "android|Qin 2 Pro", now - 100,
                now - relay.BROWSER_IDLE_SEC - 60))
    db.execute("INSERT INTO usage_client(uid,m,k,label,ua,n,first,last) VALUES(7,?,?,?,?,?,?,?)",
               (relay._min_now(), "i:127.0.0.1|" + QIN_UA[:80], "", QIN_UA, 0, now, now))
    db.commit()
    assert relay._browser_sweep(db) == 0
    assert db.execute("SELECT COUNT(*) FROM clients WHERE uid=7").fetchone()[0] == 1


def test_active_browser_refreshes_last_within_idle_window(tmp_path):
    """30 分钟闲置就踢：还在用的浏览器必须把 last 刷上去，不能等一小时的写库节流。"""
    db = _db(tmp_path)
    now = time.time()
    _browse_row(db, last=now - 20 * 60)
    relay._CID_MEM[(7, "browcid")] = now - 20 * 60   # 写库节流窗口里：老逻辑会跳过
    req = _Req(MAC_SAFARI, db, cookies={"cb_cid": "browcid"})
    assert relay._seat_wall(req, db, 7) is None
    last = db.execute("SELECT last FROM clients WHERE uid=7 AND cid='browcid'").fetchone()[0]
    assert last > now - 60, "活跃浏览器的 last 没刷新，会被误踢"


async def _call_mw(ua, db, cookies=None):
    from aiohttp import web

    async def handler(request):
        return web.Response(text="ok")

    fn = getattr(relay._svc_dim_mw, "__wrapped__", relay._svc_dim_mw)
    return await fn(_Req(ua, db, cookies=cookies), handler)


def test_kicked_browser_session_cookie_is_dropped(tmp_path):
    """被踢的浏览器带着旧 r_sess 回来：cookie 作废，按未登录处理。"""
    db = _db(tmp_path)
    db.execute("INSERT INTO client_kick(cid,ts) VALUES('browcid',?)", (time.time(),))
    db.commit()
    resp = asyncio.run(_call_mw(MAC_SAFARI, db,
                                cookies={"cb_cid": "browcid", "r_sess": "u.0.sig"}))
    m = resp.cookies.get("r_sess")
    assert m is not None and not m.value and m["max-age"] == "0", dict(m)


def test_browser_login_clears_kick_record(tmp_path):
    """重新登录成功（响应里带新 r_sess）就解除限制，可以正常再用。"""
    from aiohttp import web

    db = _db(tmp_path)
    db.execute("INSERT INTO client_kick(cid,ts) VALUES('browcid',?)", (time.time(),))
    db.commit()

    async def handler(request):
        out = web.Response(text="ok")
        out.set_cookie("r_sess", "u.0.newsig", max_age=86400 * 7, httponly=True)
        return out

    fn = getattr(relay._svc_dim_mw, "__wrapped__", relay._svc_dim_mw)
    resp = asyncio.run(fn(_Req(MAC_SAFARI, db, cookies={"cb_cid": "browcid"}), handler))
    assert not db.execute("SELECT 1 FROM client_kick WHERE cid='browcid'").fetchone()
    m = resp.cookies.get("r_sess")
    assert m is not None and m.value == "u.0.newsig"
    assert not m.get("max-age"), "浏览器登录态仍然只在本次会话有效"
