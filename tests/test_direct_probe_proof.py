#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""直连探针身份验证 + 直连失败不背断网锅 + no_lan 开关移除。

2026-11-18 aliyun 事故链：
  ① 探针只看 HTTP 200 → 代理/运营商劫持的 200 也算「通」→ 误建局域网直连；
  ② 直连路径上的请求失败（status 0）被 netFailTick 计进断网计数 → 6 次就把
     整个 App 锁进壳离线页——但中继是好的，产品该做的是自动回退中继。
钉死三条（产品自己搞定，不留人工开关）：
  1. probePing 验 JSON（ok:1）+ nonce 回声，伪造 200 给不出 → 进不了假直连
  2. 直连路径（绝对 URL）的失败：立刻拆直连回中继，且不计入断网计数
  3. relay 侧 no_lan 设备开关整体移除（用户明确不要开关，自动检测已补齐）

跑法：.venv/bin/python -m pytest tests/test_direct_probe_proof.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

VIEW = (HERE / "host" / "static" / "src" / "js" / "02_view.js").read_text(encoding="utf-8")
CORE = (HERE / "host" / "static" / "src" / "js" / "01_core.js").read_text(encoding="utf-8")
NET = (HERE / "host" / "static" / "src" / "js" / "04_net.js").read_text(encoding="utf-8")
RELAY = (HERE / "relay" / "relay.py").read_text(encoding="utf-8")


def test_probe_validates_identity():
    assert "function probePing(url,nonce)" in VIEW, "探针必须带 nonce"
    body = VIEW[VIEW.index("function probePing(url,nonce)"):]
    body = body[:body.index("function probeLans")]
    assert "JSON.parse(x.responseText)" in body, "200 还不够，必须验应答是开发机的 JSON"
    assert "d.ok===1" in body, "ok:1 是 host /api/ping 的身份标记"
    assert "d.pr===undefined||d.pr===nonce" in body, "nonce 回声必须对上（老 host 无 pr 字段兜底）"


def test_probe_lans_sends_nonce():
    body = VIEW[VIEW.index("function probeLans"):]
    body = body[:body.index("function directRetry")]
    assert "?pr=" in body or "'?pr='+nonce" in body or '"?pr="+nonce' in body, \
        "probeLans 发探针时要带上 nonce"


def test_direct_failures_not_counted_offline():
    # xjX 把「是不是直连路径」告诉 netFailTick（绝对 URL = 直连，中继走相对路径）
    assert "netFailTick(x.status," in CORE, "xjX 必须把 direct 标记传给 netFailTick"
    body = NET[NET.index("function netFailTick"):]
    body = body[:body.index("function netOffSoon")]
    assert "direct" in body.split("{", 1)[0] or ",direct" in body, "netFailTick 收 direct 标记"
    assert "directDown('netdead'" in body, "直连发不出去：立刻拆直连自动回中继"
    assert "return" in body, "直连路径的失败绝不计入 NETDEAD_N"


def test_relay_no_lan_switch_removed():
    assert "no_lan" not in RELAY, "relay 不留设备级开关（用户明确不要开关，自动检测已补齐）"
