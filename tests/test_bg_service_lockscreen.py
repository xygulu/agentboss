#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""锁屏签字的前提:perm 类(待拍板)通知必须锁屏可见。

Android 锁屏默认 PRIVATE——正文和 action 按钮一起被藏,「锁屏可签字」
(spec 批次二出口标准)不可能成立。BgService 必须对待拍板类通知设
VISIBILITY_PUBLIC;普通 done 类不许 public(代码/路径不亮锁屏)。

跑法:.venv/bin/python -m pytest tests/test_bg_service_lockscreen.py -q
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SRC = (HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal" / "BgService.java")


def test_perm_notify_public_on_lockscreen():
    src = SRC.read_text(encoding="utf-8")
    assert "setVisibility" in src, (
        "BgService 从不设置锁屏可见性:Android 锁屏默认 PRIVATE,"
        "action 按钮一起被藏,锁屏签字不可能成立")
    # 待拍板的判据在 sysNotify 里是 rid 非空(允许/拒绝按钮挂同一分支)——
    # VISIBILITY_PUBLIC 必须与该分支真实关联
    assert re.search(r'rid[\s\S]{0,600}?VISIBILITY_PUBLIC', src) or \
           re.search(r'VISIBILITY_PUBLIC[\s\S]{0,600}?addAction[\s\S]{0,200}?"允许"', src), \
           "待拍板通知(rid 分支)没有设 VISIBILITY_PUBLIC——锁屏上按钮一起被藏"


def test_done_notify_not_forced_public():
    """done(办结)类不许 public:办结正文可能带代码/路径,不亮锁屏。
    表现形式:VISIBILITY_PUBLIC 只出现在待拍板分支,不得无条件套用全部通知。"""
    src = SRC.read_text(encoding="utf-8")
    n_pub = src.count("VISIBILITY_PUBLIC")
    assert n_pub > 0, "没有 public 设置(与上一条测试矛盾)"
    # public 必须在条件分支里(kind 相关),不允许出现「对所有通知统一 setVisibility(PUBLIC)」的写法:
    # 检查 setVisibility 调用所在行之前 5 行内出现 if/kind/perm 字样
    for m in re.finditer(r"\.setVisibility\(Notification\.VISIBILITY_PUBLIC\)", src):
        ctx = src[max(0, m.start() - 500):m.start()]
        assert re.search(r"\bif\b|kind|perm|ask|input", ctx), \
            "VISIBILITY_PUBLIC 疑似无条件套用(会把办结正文亮锁屏)"
