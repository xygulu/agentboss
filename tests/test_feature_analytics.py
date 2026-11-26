# SPDX-License-Identifier: AGPL-3.0-or-later

"""功能使用埋点 + 后台「数据分析」（2026-11-21 用户）。

口径：页面在每个功能入口打一个点（只记事件名，不记内容），中继按
「天 × 产品线 × 事件 × 版本 × 平台 × 账号 × 设备」聚合成计数（feat_day）。
后台一页看：近 N 天活跃账号/设备、功能排行、每日趋势、版本/平台分布。

为什么不复用 use_act_log：那张表按「写请求」记账，7 天就清，且只覆盖发消息/
按键/语音这类走 host 的动作；开关类（静音、免打扰、模型切换）和「打开页面」
根本不进表。埋点是产品口径，按天聚合，留 400 天，行数还是很小。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / "relay" / "relay.py"
SRC = ROOT / "host" / "static" / "src" / "js"


def _src(p):
    return p.read_text(encoding="utf-8")


def test_track_table_endpoint_and_aggregation():
    src = _src(RELAY)
    assert "CREATE TABLE IF NOT EXISTS feat_day(" in src, "要按天聚合的事件表"
    assert re.search(
        r"PRIMARY KEY\(day,\s*prod,\s*ev,\s*pg,\s*k,\s*ver,\s*plat,\s*uid,\s*cid\)", src), \
        "同一人同一天同一事件/页面/明细只有一行，重复上报累加 n"
    m = re.search(r"async def h_track\(request\):[\s\S]*?\n\n\n", src)
    assert m, "找不到 /api/track 的处理函数"
    body = m[0]
    assert 'add_post("/api/track"' in src or "add_post('/api/track'" in src, "要注册路由"
    assert "_track_ctx(" in body, "产品线/版本/平台由中继读 UA 和 cookie，页面不用自己报"
    assert "_track_add(" in body, "写入走统一入口（局域网直连那条路也用它）"
    m2 = re.search(r"def _track_add\(app,[^)]*\):[\s\S]*?\n\n\n", src)
    assert m2, "要有一个共用的入表函数"
    add = m2[0]
    assert "ON CONFLICT" in add and "executemany" in add, "批量 upsert 累加"
    assert "_FEAT_EV" in add, "事件名要过白名单正则（防乱写/注入）"
    assert "_FEAT_MAX" in add, "单次上报要有条数上限"


def test_track_identity_from_cookie_and_ua():
    src = _src(RELAY)
    m = re.search(r"def _track_ctx\(request\):[\s\S]*?\n\n\n", src)
    assert m, "要有 _track_ctx() 统一取身份/产品线/版本/平台"
    body = m[0]
    assert "uid_of(" in body and "_client_key(" in body, "身份从登录 cookie + cb_cid 认"
    assert "cb_product" in body and "X-Product" in body, "产品线认壳种的 cookie"
    assert "_track_ua(" in body
    ua = re.search(r"def _track_ua\(ua\):[\s\S]*?\n\n\n", src)[0]
    assert "Shell/" in ua, "壳版本认 UA 里的 CodebossShell/x.y.z"
    assert '"ios"' in ua and '"android"' in ua and '"web"' in ua, "平台只分三档"


def test_track_event_whitelist():
    src = _src(RELAY)
    m = re.search(r"_FEAT_EV\s*=\s*re\.compile\([^\n]+", src)
    assert m, "事件名白名单正则"
    assert "[a-z]" in m[0], "小写事件名"


def test_lan_direct_events_forwarded_by_host():
    """局域网直连时页面源在 host 上，/api/track 不在中继同源 → host 要接住再转中继。
    没有这条，家里 WiFi 直连的那批使用在报表里整段消失。"""
    host = _src(ROOT / "host" / "server.py")
    m = re.search(r"async def h_track\(request\):[\s\S]*?\n\n\n", host)
    assert m, "host 要接 /api/track"
    body = m[0]
    assert 'add_post("/api/track", h_track)' in host, "host 上注册路由"
    assert '"/r/dev_track"' in body, "按本机设备凭证转给中继"
    assert "run_in_executor" in body, "转发是网络调用，不许卡住页面请求"
    assert "cb_product" in body, "产品线跟着页面 cookie 走"
    rel = _src(RELAY)
    m2 = re.search(r"async def r_dev_track\(request\):[\s\S]*?\n\n\n", rel)
    assert m2, "中继要收设备转来的埋点"
    b2 = m2[0]
    assert "_dev_self_auth(" in b2, "设备自证"
    assert "_track_add(" in b2
    assert '"d:" + dev' in b2 or "'d:'+dev" in b2, "设备维度用 dev 认"
    assert 'add_post("/r/dev_track"' in rel
    assert re.search(r"def _track_ua\(ua\)", rel), "UA 解析抽出来给两条路共用"


def test_dev_track_attributes_owner_and_product():
    import asyncio
    import json
    import sys
    sys.path.insert(0, str(ROOT))
    import relay
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')"
                     ).lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner,product) "
               "VALUES('d1','dev','sec',?,'agentboss')", (uid,))
    db.commit()

    class Req:
        remote = "10.0.0.5"
        app = {"db": db, "key": b"k"}
        async def json(self):
            return {"dev": "d1", "sec": "sec",
                    "e": [["send_msg", 1], ["key", 2]],
                    "ua": "Mozilla/5.0 (Android 9) AgentBossShell/0.1.14",
                    "product": "agentboss"}

    resp = asyncio.run(relay.r_dev_track(Req()))
    assert json.loads(resp.text)["ok"] is True
    row = db.execute("SELECT uid,cid,prod,ver,plat,SUM(n) FROM feat_day "
                     "GROUP BY uid,cid,prod,ver,plat").fetchone()
    assert row == (uid, "d:d1", "agentboss", "0.1.14", "android", 3), row


def test_track_retention():
    src = _src(RELAY)
    m = re.search(r"def use_prune\(db\):[\s\S]*?\n\n\n", src)
    assert m
    assert "DELETE FROM feat_day" in m[0], "旧事件按天清理，别无限涨"
    assert "_FEAT_KEEP_DAYS" in m[0], "保留天数写成常量"


def test_admin_analytics_api():
    src = _src(RELAY)
    m = re.search(r"async def r_admin_analytics\(request\):[\s\S]*?\n\n\n", src)
    assert m, "后台分析接口"
    body = m[0]
    assert "_adm_auth(request)" in body, "管理接口要鉴权"
    assert 'add_get("/r/admin_api/analytics"' in src or 'add_route("*", "/r/admin_api/analytics"' in src
    for k in ("daily", "fev", "byprod", "byver", "byplat", "today", "pages", "evdet"):
        assert '"%s"' % k in body, "返回里要有 %s" % k
    assert "GROUP BY day" in body and "GROUP BY ev" in body
    assert "LIMIT" in body, "排行要有上限"


def test_admin_page_has_analytics_pane():
    src = _src(RELAY)
    assert '<div class="pane" id="t-analytics">' in src, "后台新增「分析」页"
    assert "['analytics','分析']" in src, "菜单里挂上"
    assert "t==='analytics'?loadAn" in src, "切页要加载"
    m = re.search(r"async function loadAn\(\)\{[\s\S]*?\n\}", src)
    assert m, "loadAn() 要拉数据"
    assert "/r/admin_api/analytics" in m[0]
    assert "anSet(" in src and "anBar(" in src, "时间/产品线筛选 + 条形渲染"


def test_page_track_helper():
    src = _src(SRC / "11_track.js")
    assert "function track(" in src and "function trackFlush(" in src
    assert "/api/track" in src
    assert "sendBeacon" in src, "退后台也要能发出去"
    assert "keepalive" in src, "fetch 兜底也要 keepalive"
    # 页面维 + 停留时长（2026-11-21 用户：要「哪个页面、点了什么、停多久」）
    assert "function trackPage(" in src and "function trkPageNow(" in src
    assert "['visit'" in src and "['dwell'" in src, "进页记 visit、离开记停留秒数"
    for ev in ("home_tap", "sess_tap", "set_tap", "term_tap"):
        assert "'%s'" % ev in src, "%s 要能细分到点了哪一项" % ev
    assert "TRK_SEC" in src
    build = _src(ROOT / "tools" / "build_page.py")
    assert '"11_track.js"' in build, "新文件要进页面打包"
    assert "function track(" in _src(ROOT / "host" / "static" / "ext.html"), "生成物要重建"


WIRING = {
    "send_msg": "04_net.js",
    "ptt": "08_ptt.js",
    "asr_result": "08_ptt.js",
    "mute_on": "03_voice.js",
    "dnd_on": "03_voice.js",
    "npref": "03_voice.js",
    "nh_open": "03_voice.js",
    "sess_new": "05_cmd.js",
    "sess_switch": "05_cmd.js",
    "key": "05_cmd.js",
    "cmd_run": "05_cmd.js",
    "cmd_scan": "05_cmd.js",
    "settings_open": "05_cmd.js",
    "pay": "06_settings.js",
    "pair": "07_account.js",
    "bind_email": "07_account.js",
    "bind_phone": "07_account.js",
    "dev_switch": "07_account.js",
    "model_open": "07_account.js",
    "model_switch": "07_account.js",
    "media_open": "02_view.js",
    "view_switch": "02_view.js",
    "push_set": "04_net.js",
    "feedback": "09_feedback.js",
    "feedback_submit": "09_feedback.js",
    "stl_view": "10_stl.js",
}


def test_feature_flows_are_wired():
    got = {}
    for f in SRC.glob("*.js"):
        for ev in re.findall(r"track\('([a-z0-9_]+)'", f.read_text(encoding="utf-8")):
            got.setdefault(ev, set()).add(f.name)
    missing = [ev for ev in WIRING if ev not in got]
    assert not missing, "这些功能入口还没打点：%s" % ", ".join(missing)
    wrong = [(ev, WIRING[ev], sorted(got[ev])) for ev in WIRING
             if ev in got and WIRING[ev] not in got[ev]]
    assert not wrong, "打点打错了文件：%s" % wrong


def test_page_track_node_gate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不在，跳过埋点缓冲检查")
    for f in ("test_feature_analytics.mjs", "test_feature_track_page.mjs"):
        r = subprocess.run([node, str(ROOT / "tests" / f)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f + ":\n" + r.stdout + r.stderr
