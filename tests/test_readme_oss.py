#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Phase 3 Task 4：开源 README——陌生人 30 分钟路径（自建中继→host→手机）。
README 自己也过出仓清单（不留官方域名/ICP/个人路径）。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(os.path.dirname(HERE), "README.md")


def _txt():
    with open(README, encoding="utf-8") as f:
        return f.read()


def test_readme_has_thirty_minute_path():
    t = _txt()
    assert ("三十分钟" in t) or ("30 分钟" in t)
    assert "docker compose" in t or "docker-compose" in t
    assert "install-selfhost.sh" in t
    assert "relay.py" in t
    assert "--enroll" in t
    # 自行构建 APK 的前提：自备签名 keystore（build.sh 缺它硬退出）（终审 #4）
    assert "keystore" in t.lower()


def test_readme_license_and_identity():
    t = _txt()
    assert "AGPL-3.0" in t


def test_readme_passes_outbound_gate():
    t = _txt()
    for banned in ("relay.example.com", "浙ICP", "/home/dev",
                   "example", "example.com", "testflight"):
        assert banned not in t, banned
