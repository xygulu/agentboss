# SPDX-License-Identifier: AGPL-3.0-or-later

"""滚动历史持久仓（窗口核心 Phase 1）：host 重启不清零的「窗口历史」。

与 logs/ansi 严格分离——那边是 wrapper 的 ANSI 回放流（claude 启动依赖，
断链会崩，7 天/300MB GC），这边只存 _diff_commit 入账的滚动历史
（st["hist"]：row-dict 列表 {x: pyte.Char}），JSON 行格式一行一 row，
保结构（颜色/加粗/反显），重载后直接进 _reflow_rows 渲染。

接口：save(sid, rows) / load(sid) / delete(sid) / gc(keep_sids, max_age_s)
跑法（测试）： .venv/bin/python -m pytest tests/test_scrollback.py -q
"""
import json
import os
import time
from pathlib import Path

from pyte.screens import Char

HERE = Path(__file__).resolve().parent
SCROLLBACK_DIR = HERE / "logs" / "scrollback"
SCROLLBACK_MAX = 800          # 与 server.MIRROR_HIST_MAX 同值：窗口历史上限

_CHAR_FIELDS = Char._fields   # data/fg/bg/bold/italics/underscore/strikethrough/reverse/blink


def _enc_row(row):
    return [[x, [getattr(c, f) for f in _CHAR_FIELDS]] for x, c in sorted(row.items())]


def _dec_row(arr):
    return {int(x): Char(*vals) for x, vals in arr}


def _path(sid):
    return Path(SCROLLBACK_DIR) / ("scrollback_%s.txt" % sid)


def save(sid, rows):
    """原子落盘：.part 写完再 os.replace；只留末尾 SCROLLBACK_MAX 行。"""
    Path(SCROLLBACK_DIR).mkdir(parents=True, exist_ok=True)
    p = _path(sid)
    part = p.with_name(p.name + ".part")
    try:
        with open(part, "w", encoding="utf-8") as f:
            for r in rows[-SCROLLBACK_MAX:]:
                f.write(json.dumps(_enc_row(r), ensure_ascii=False) + "\n")
        os.replace(part, p)
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def load(sid):
    """缺失/损坏返回 []，坏行跳过不连坐——绝不抛。"""
    out = []
    try:
        f = open(_path(sid), "r", encoding="utf-8")
    except OSError:
        return []
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_dec_row(json.loads(line)))
            except (ValueError, TypeError, IndexError):
                continue
    return out


def delete(sid):
    try:
        _path(sid).unlink()
    except OSError:
        pass


def known():
    """仓里还有哪些 sid（按文件名反解）。重启后的鬼影行从这里来。"""
    try:
        files = list(Path(SCROLLBACK_DIR).glob("scrollback_*.txt"))
    except OSError:
        return []
    return [p.name[len("scrollback_"):-len(".txt")] for p in files]


def gc(keep_sids, max_age_s=14 * 86400):
    """按 mtime 清旧文件；keep_sids 里的（还在跑的会话）绝不动。"""
    try:
        files = list(Path(SCROLLBACK_DIR).glob("scrollback_*.txt"))
    except OSError:
        return
    now = time.time()
    for p in files:
        sid = p.name[len("scrollback_"):-len(".txt")]
        if sid in keep_sids:
            continue
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except OSError:
            pass
