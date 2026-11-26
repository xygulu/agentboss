#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""h_direct_info 的局域网候选下发闸门:手机与 host 公网出口不同 → 不下发 lan。

2026-11-18 事故:开发机在 aliyun(内网 IP 172.19.x),手机在外面,私网候选照发
→ 手机探针经代理撞上不可靠路径误建直连 → 整页半死 → 壳断网页。
闸门规则:
  - 手机出口 IP == host link 出口 IP(同一 NAT = 大概率同一局域网)→ 照发
  - 不同 / 任一侧拿不到 → lan 置空(rtc/ice 照旧:广域网打洞本来就是跨网用的)

跑法:.venv/bin/python -m pytest tests/test_direct_lan_gate.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: F401  (relay 与 server 同仓;下面直接测 relay)
import relay

INFO = {"lan": ["172.19.48.157"], "port": 8790, "rtc": True, "ts": 100.0}


def _app(link_ip, dev="d1"):
    return {"links": {"d1": SimpleNamespace(closed=False)},
            "direct": {"d1": dict(INFO)},
            "link_ip": {"d1": link_ip}}


def _req(app, ip):
    return SimpleNamespace(app=app, remote=ip)


def test_same_egress_lan_sent(monkeypatch):
    monkeypatch.setattr(relay, "_sel_dev", lambda r: (None, ("d1", None)))
    monkeypatch.setattr(relay, "_turn_cfg", lambda a: [])
    r = relay.h_direct_info(_req(_app("1.2.3.4"), "1.2.3.4"))
    assert r.status == 200
    import json
    d = json.loads(r.body)
    assert d["lan"] == ["172.19.48.157"], "同一公网出口=大概率同一局域网:照发"


def test_diff_egress_lan_withheld(monkeypatch):
    monkeypatch.setattr(relay, "_sel_dev", lambda r: (None, ("d1", None)))
    monkeypatch.setattr(relay, "_turn_cfg", lambda a: [])
    import json
    d = json.loads(relay.h_direct_info(_req(_app("1.2.3.4"), "5.6.7.8")).body)
    assert d["lan"] == [], "不同出口:私网候选不下发"
    assert d["rtc"] is True, "P2P 打洞是跨网用的,照旧"


def test_missing_host_ip_lan_withheld(monkeypatch):
    """host 没记过出口 IP(新链路/重启前):拿不到实证就不发,宁走中继。"""
    monkeypatch.setattr(relay, "_sel_dev", lambda r: (None, ("d1", None)))
    monkeypatch.setattr(relay, "_turn_cfg", lambda a: [])
    import json
    d = json.loads(relay.h_direct_info(_req(_app(""), "1.2.3.4")).body)
    assert d["lan"] == []


def test_missing_client_ip_lan_withheld(monkeypatch):
    monkeypatch.setattr(relay, "_sel_dev", lambda r: (None, ("d1", None)))
    monkeypatch.setattr(relay, "_turn_cfg", lambda a: [])
    import json
    d = json.loads(relay.h_direct_info(_req(_app("1.2.3.4"), "")).body)
    assert d["lan"] == []

# no_lan 设备级开关已按产品方向移除(2026-11-18 用户拍板「不要开关,产品自己搞定」):
# 假直连由页面探针身份验证(test_direct_probe_proof)+失败自动回退根治,见同日提交。
