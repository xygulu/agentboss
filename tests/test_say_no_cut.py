#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""播报不许被下一条截断（2026-11-20 用户：播报会突然截断，要讲完整）。

以前每来一条提醒，壳就把正在响的 MediaPlayer release 掉再放新的——
话说到一半就被掐掉。现在整条链路只认一个「正在念 + 最多排一句」的队列：
念完才轮到下一句（多句只念最新那句），所有结束/失败出口都汇到同一个
sayFinished()，由它决定接下一句还是收工。

跑法：.venv/bin/python -m pytest tests/test_say_no_cut.py -q
"""
import re
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
AND = HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal"
BG = AND / "BgService.java"
MA = AND / "MainActivity.java"


def _body(src, decl_re, name):
    """按大括号配对取出方法体（Java 方法体里有嵌套大括号，正则截不准）。"""
    m = re.search(decl_re, src)
    assert m, "找不到 %s" % name
    s = src.index("{", m.start())
    d = 0
    for k in range(s, len(src)):
        if src[k] == "{":
            d += 1
        elif src[k] == "}":
            d -= 1
            if d == 0:
                return src[s:k + 1]
    raise AssertionError("括号不配对：%s" % name)


def test_bg_say_queues_instead_of_interrupting():
    src = BG.read_text(encoding="utf-8")
    body = _body(src, r"private void say\(String kind, String about, String sayPath\)", "BgService.say")
    assert re.search(r"if\s*\(\s*sayBusy\s*\)", body), \
        "say() 开头没有「正在念就排队」的判断——新提醒会直接掐掉在念的那句"
    assert re.search(r"if\s*\(\s*sayBusy\s*\)[\s\S]{0,400}?qHas\s*=\s*true[\s\S]{0,300}?return\s*;", body), \
        "正在念的时候新提醒要存进等待槽（最新的顶掉旧的）再 return，不能往下走直接放"
    fin = _body(src, r"private void sayFinished\(\)", "sayFinished")
    rest = src.replace("private static volatile boolean SAYING = false;", "", 1).replace(fin, "", 1)
    assert "SAYING = false" not in rest, \
        "播报结束/失败的出口都得汇到 sayFinished()，那里才决定接下一句；现在还有别处各自收尾"


def test_bg_every_exit_finishes_speech():
    """播放完成、下载失败、prepare 失败、内置音完成/失败——五条出口都不能丢。"""
    src = BG.read_text(encoding="utf-8")
    n = src.count("sayFinished()")
    # 定义 1 处 + 调用 ≥ 5 处（say 空串、playRemoteSay 取不到、playFile 完成/失败、playCue 完成/失败）
    assert n >= 6, "只有 %d 处调 sayFinished()，有的出口会漏掉排队的那句（或者页面一直等）" % (n - 1)


def test_main_activity_fg_say_queues():
    src = MA.read_text(encoding="utf-8")
    body = _body(src, r"private void playSayFile\(File f\)", "MainActivity.playSayFile")
    assert re.search(r"if\s*\([^)]*sayMp[^)]*isPlaying\(\)[^)]*\)", body), \
        "playSayFile 没有「上一句还在念」的判断——直接释放播放器就是突然截断"
    assert re.search(r"isPlaying\(\)[\s\S]{0,200}?fgPend\s*=\s*f[\s\S]{0,40}?return\s*;", body), \
        "上一句还在念时要把新的一句存进 fgPend 再 return"
    assert body.count("fgPend") >= 3, "fgPend 只在存的地方出现：播放完/失败时要接着念排队那句"
    guard = re.search(r"if\s*\([^)]*sayMp[^)]*isPlaying\(\)[^)]*\)", body)
    rel = re.search(r"sayMp\.release\(\)", body)
    assert guard and (not rel or rel.start() > guard.start()), \
        "还在念的时候不许 release 掉当前播放器——那就是突然截断"


def test_fg_say_skips_when_bg_is_speaking():
    """后台服务正在念，页面又递一份同一个地址（4 秒兜底重喊）：两份叠着放，
    听感就是「卡顿/截断」。后台在念就不归前台管。"""
    src = MA.read_text(encoding="utf-8")
    body = _body(src, r"public void sayUrl\(final String url\)", "MainActivity.sayUrl")
    assert "BgService.isSpeaking()" in body, \
        "sayUrl 没有让位给后台正在念的那句——两条音轨同时放，听感是卡顿"


def test_page_play_say_queues():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("没有 node，跳过 playSay 行为门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_say_queue.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]
