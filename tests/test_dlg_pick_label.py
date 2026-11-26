# SPDX-License-Identifier: AGPL-3.0-or-later

"""点选弹层时页面传回来的选项文字，host 必须认得出（用户 2026-11-19：手点弹层
总报红字「选项内容变了」）。

根因：页面从 #liveflow 的 .dopt 里取文字，那里画的是整行「❯ 1. Yes」；
host 快照（_dlg_snapshot_rows）已经用 _DLG_OPT_RE 把「❯ 1. 」前缀剥掉，
_dlg_pick_core 拿两边做前缀比对 → 永远对不上 → 拒绝按键。

跑法： .venv/bin/python -m pytest tests/test_dlg_pick_label.py -q
"""
import html as _html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pyte.screens import Char

import screen
import server

COLS, ROWS = 60, 8
LINES = [
    "Do you want to proceed?",
    "",
    "❯ 1. Yes",
    "  2. No, tell Claude what to do differently",
    "",
    "Enter to select · Esc to cancel",
    "",
    "",
]


def _screen_st():
    class _Scr:
        pass

    scr = _Scr()
    scr.buffer = [dict(zip(range(COLS), [Char(c) for c in ln.ljust(COLS)]))
                  for ln in LINES]
    return {"win": (COLS, ROWS), "scr": scr}


def _snapshot():
    st = _screen_st()
    cols, rows = st["win"]
    rds = [st["scr"].buffer[y] for y in range(rows)]
    return server._dlg_snapshot_rows(st, cols, rows)


def _page_label(want):
    """照 static/src/js/05_cmd.js dlgOptText+dlgEsc+归一化 的做法，从渲染的 HTML
    里取页面会传给 host 的那串文字。"""
    st = _screen_st()
    cols, rows = st["win"]
    rds = [st["scr"].buffer[y] for y in range(rows)]
    dlg = screen._dialog_screen(rds, cols)
    h = screen._dialog_html(rds, cols, dlg)
    m = re.search(r'class="dopt[^"]*" data-opt="%d">([\s\S]*?)</div>' % want, h)
    assert m, h
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()


class _Sim:
    """按上下键挪光标的假终端：press 改 cur，read 重出快照。"""

    def __init__(self):
        self.cur = 1
        self.enter = False

    def read(self):
        snap = _snapshot()
        snap["cur"] = self.cur
        return snap

    def press(self, k):
        if k == "down":
            self.cur = self.cur % 2 + 1
        elif k == "up":
            self.cur = (self.cur - 2) % 2 + 1
        elif k == "enter":
            self.enter = True
            return True, ""
        return True, ""


def test_page_label_is_row_text_with_prefix():
    # 页面画的是整行（带 ❯ / 序号）；剥掉前缀后必须等于 host 快照的文字。
    # host 侧核对时也得这么剥一遍，见 test_tap_pick_accepts_page_label。
    for n in (1, 2):
        bare = server._DLG_OPT_RE.sub("", _page_label(n)).strip()
        assert bare == _snapshot()["text"][n], (
            "剥前缀后仍对不上：%r vs %r" % (bare, _snapshot()["text"][n]))


def test_tap_pick_accepts_page_label():
    sim = _Sim()
    res = server._dlg_pick_core(sim.read, sim.press, 2, _page_label(2))
    assert res["reason"] != "changed", res
    assert res["ok"] and sim.enter, res
