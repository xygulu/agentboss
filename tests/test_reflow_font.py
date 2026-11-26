# SPDX-License-Identifier: AGPL-3.0-or-later

"""全彩重排字号跨会话统一（用户主诉：hermes 字很大、opencode 字很小）。

根因：blk/dblk 字号除数 = 该会话终端列数（cols*0.62），列多字小、列少字大，
同一 agent 不同会话、跨 agent 都不统一。修法：固定参考列数（80 列经典终端），
所有会话同一字号公式；用户实时字号仍走 --fs-scale。

跑法： .venv/bin/python -m pytest tests/test_reflow_font.py -q
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pyte.screens import Char

import screen


def _st(cols, rows=6):
    """最小 pyte-like 屏：一行带双空格 → blk 块（走到字号公式）。"""

    class _Scr:
        pass

    scr = _Scr()
    row = dict(zip(range(cols), [Char(c) for c in ("a  b".ljust(cols))]))
    scr.buffer = [row] + [{} for _ in range(rows - 1)]
    return {"win": (cols, rows), "scr": scr}


def _fs_of(cols):
    html = screen._reflow_html(_st(cols))
    m = re.search(r'font-size:([^"]+)', html)
    assert m, html[:200]
    return m.group(1)


def test_blk_font_same_across_cols():
    assert _fs_of(80) == _fs_of(120)
    assert _fs_of(80) == _fs_of(200)


def test_blk_font_still_scales_with_user_slider():
    # 除数固定，但用户实时字号（--fs-scale）仍要起作用
    html = screen._reflow_html(_st(120))
    assert "var(--fs-scale)" in html


def test_dialog_font_same_across_cols():
    # 弹层视图同一套统一（原来同样按 cols 缩）
    import re as _re
    src = Path(HERE, "host", "screen.py").read_text()
    # 两处字号公式都不得再引用会话列数
    body = _re.sub(r"#.*", "", src)
    assert "cols * 0.62" not in body
