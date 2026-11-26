#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""浅底处理自检：python3 tests/test_contrast.py

背景：TUI 按宿主终端配色自适应，终端是浅色主题时给输入框/提示框画浅底
（#f9f9f9），手机端固定深底。用户口径：白底去掉、默认全深色——浅底那块
整块拿掉，内容压成灰字（#8b949e），底就是页面深底。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from server import _row_runs, _row_sgr, _row_cells          # noqa: E402
from screen import _light_bg, _TERM_GRAY                     # 渲染层已独立成 screen.py


class Ch:
    def __init__(self, data=" ", fg="default", bg="default", bold=False, reverse=False):
        self.data, self.fg, self.bg = data, fg, bg
        self.bold, self.italics, self.underscore, self.reverse = bold, False, False, reverse


# 底的亮度判定
assert _light_bg("#f9f9f9") and _light_bg("#63a8f8")
assert not _light_bg("#0b0e14") and not _light_bg("#424242") and not _light_bg(None)

# 重排视图（手机）：浅底那一段 → 灰字、不发背景（落回页面深底）
row = {0: Ch("你", bg="f9f9f9"), 1: Ch("好", bg="f9f9f9")}
css = "".join(c for c, _ in _row_runs(row, 2))
assert "color:#8b949e" in css, css
assert "background" not in css, css

# 深底那一段保持原样（不发多余的 color / background）
row2 = {0: Ch("x", bg="424242")}
css2 = "".join(c for c, _ in _row_runs(row2, 1))
assert "color:" not in css2 and "background:#424242" in css2, css2

# 原样视图（xterm）：浅底那段 → 灰字 + 不发背景 SGR（没有 48;）
sgr = _row_sgr(row, 2)
assert "38;2;139;148;158" in sgr, repr(sgr)
assert ";48;" not in sgr, repr(sgr)   # 不发背景（背景 SGR 只会跟在 ; 后面）
# 深底那段照旧发自己的底
sgr2 = _row_sgr(row2, 1)
assert "48;2;66;66;66" in sgr2 and "38;2;139;148;158" not in sgr2, repr(sgr2)

# 256 色浅底（231 近白）同样拿掉
row3 = {0: Ch("x", bg=231)}
assert "38;2;139;148;158" in _row_sgr(row3, 1), repr(_row_sgr(row3, 1))
css3 = "".join(c for c, _ in _row_runs(row3, 1))
assert "background" not in css3, css3

# 电脑端窗口（/api/ext_cells）：浅底 → 灰字 + 无背景
cells = _row_cells(row, 2)
assert cells[0][0] == _TERM_GRAY and cells[0][1] is None, cells

# 反显行（原样视图）：别透传 SGR 7（xterm 拿主题浅前景当底 → 白条），
# 自己发深色高亮条 + 白字（与重排视图 #3a4150 一致）
rev = {0: Ch("选中", reverse=True)}
sgr_r = _row_sgr(rev, 2)
assert "48;2;58;65;80" in sgr_r and "38;2;255;255;255" in sgr_r, repr(sgr_r)
assert ";7;" not in sgr_r and not sgr_r.endswith(";7m"), repr(sgr_r)
# 重排视图的反显也是深色高亮条
css_r = "".join(c for c, _ in _row_runs(rev, 2))
assert "background:#3a4150" in css_r, css_r

# dim（SGR 2）：pyte 会丢属性 → 喂之前重写成灰字；SGR 22 跟着补前景重置
import server
b = b"\x1b[2mdim text\x1b[22mnormal"
r = server._dim_to_gray(b)
assert b"38;2;139;148;158" in r and b"22;39" in r, r
# 48;2;r;g;b 里的 2 是子参数，不能被误改
b2 = b"\x1b[48;2;249;249;249m x \x1b[0m"
r2 = server._dim_to_gray(b2)
assert r2 == b2, r2
print("ok")
