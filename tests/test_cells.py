#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""电脑端会话窗口（/api/ext_cells）配色自检：python3 tests/test_cells.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from server import _css_color, _row_cells  # noqa: E402

# pyte 的 256 色是整数：0~15 要落成标准色，否则整屏都变默认白
assert _css_color(1) == "#f87171"
assert _css_color(2) == "#4ade80"
assert _css_color("brightblue") == "#93c5fd"
assert _css_color("ff8800") == "#ff8800"
assert _css_color("default") is None and _css_color(None) is None


class Ch:
    def __init__(self, data=" ", fg="default", bg="default", bold=False, reverse=False):
        self.data, self.fg, self.bg = data, fg, bg
        self.bold, self.italics, self.underscore, self.reverse = bold, False, False, reverse


# 同一样式并成一段、换样式开新段；行尾空白不留游程
row = {0: Ch("a", fg="red"), 1: Ch("b", fg="red"), 2: Ch("c", fg="green", bold=True),
       3: Ch(" ", fg="default")}
runs = _row_cells(row, 10)
assert [[r[0], r[6]] for r in runs] == [["#f87171", "ab"], ["#4ade80", "c"]], runs
assert runs[1][2] is True
# 反显交给窗口自己交换前景/背景，这里原样带上标志
assert _row_cells({0: Ch("x", fg="red", reverse=True)}, 4)[0][5] is True
# 全空行：没有游程
assert _row_cells({}, 10) == []
print("ok")
