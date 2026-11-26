#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""重排历史提交（_diff_commit）：Claude 的 TUI 从不滚动，历史靠「整屏上移 K 行」
检测恢复。真实日志实测（logs/ansi/9970f1b6.log，oracle 81 次滚动）：生产代码丢
19 次/58 行——diff 行、「⎿ Added 2 lines」这类内容在重排历史里永远看不到。

根因与口径（2026-11-14 实测生产到达节奏：字节 ~60B/100ms 细水长流，读边界必然
切在重绘帧中间，挑边界没有出路）：
- 把比对本身做鲁棒：位移搜索每次都跑（老代码「变化行数 ≤rows-5 就不找」整类丢
  掉 K=2~4 的小滚动）；滚动帧渐进到达，认出几行先提交几行（渐进提交）。
- 假位移的闸：非空对齐 ≥2 行 + 顶行真的换了内容（真滚动顶行一定变；原地重绘、
  转圈、重复内容行凑出的假游程都顶不住）。
- 「变化行少」的样本只有在**没有**部分位移证据时才按原地更新回灌基线；有证据的
  半帧按垃圾态搁置（回灌会毒化基线，补完帧顶行 = 垃圾基线顶行，top_changed 失灵）。
- 垃圾态不回灌基线，dirty 累积到下一轮；连续 8 次垃圾态强制重置（⌃L 全量重绘、
  弹层收起走这条路恢复）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server as S  # noqa: E402


def row_seq(y, text):
    """Ink 风格行写：CUP 到行首 + 文本 + 擦到行尾。"""
    return ("\x1b[%d;1H%s\x1b[K" % (y + 1, text)).encode()


class DiffCommitTest(unittest.TestCase):
    COLS, ROWS = 80, 24

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="cb-dc-")
        self.log = os.path.join(self.dir, "s.log")
        self.meta = {"cols": self.COLS, "rows": self.ROWS, "log": self.log,
                     "pid": None, "sid": "t", "cwd": "/tmp"}
        self.sid = "t-" + str(id(self))
        open(self.log, "wb").close()

    def _feed(self, data):
        with open(self.log, "ab") as f:
            f.write(data)
        return S._mirror_feed_inner(self.sid, self.meta)

    def _hist_texts(self):
        st = S._MIRROR.get(self.sid)
        cols, _ = st["win"]
        return [S._row_plain(r, cols) for r in (st.get("hist") or [])]

    def _frame1(self):
        # 22 行正文 + 2 行底部状态（输入框区）。行内容带行号保证互不相同。
        rows = ["L%02d " % y + "x" * (self.COLS - 8) for y in range(22)]
        rows += ["✻ Infusing… (1s · ↓ 1.0k tokens)", "❯ 输入框"]
        return b"".join(row_seq(y, t) for y, t in enumerate(rows)), rows

    def test_small_scroll_not_eaten(self):
        """K=2 的小滚动：帧分两截到达（读边界切在帧中间），两截各被轮询一次。
        旧行为：第一截后「变化行数少」直接当原地更新回灌基线 → 顶上 2 行永久丢。
        """
        _, rows = self._frame1()
        self._feed(b"".join(row_seq(y, t) for y, t in enumerate(rows)))
        # 真滚动 K=2：旧 0、1 行离屏，屏上内容整体上移，底部补 2 行新内容。
        # 帧分两截：先写新 0、1 行（= 旧 2、3 的内容），轮询一次；
        # 再写 2..23 行（旧 4..21 上移 + 底部两条新行），再轮询一次。
        new_a, new_b = "NEW-A tail", "NEW-B tail"
        piece_a = row_seq(0, rows[2]) + row_seq(1, rows[3])
        rest = [rows[y + 2] for y in range(2, 22)] + [new_a, new_b]
        piece_b = b"".join(row_seq(y, t) for y, t in enumerate(rest[2:], start=2))
        piece_b = b"".join(row_seq(y, t) for y, t in
                           zip(range(2, 24), rest[2:]))
        self._feed(piece_a)
        self._feed(piece_b)
        ht = self._hist_texts()
        self.assertIn(rows[0], ht, "滚出的第 1 行没进历史")
        self.assertIn(rows[1], ht, "滚出的第 2 行没进历史")
        self.assertNotIn(rows[0], ht[:0])  # noop 占位，防手滑改断言

    def test_half_frame_sample_does_not_poison(self):
        """半帧状态（重复块+半行）即使对齐上「看起来像位移」也不得提交：
        帧没画完（光标停在正文区），此时提交必然把中间态写进历史。"""
        _, rows = self._frame1()
        self._feed(b"".join(row_seq(y, t) for y, t in enumerate(rows)))
        # 半帧：只重写了 0、1 行和几行中间行（真实日志里看到的形态：顶部换了、
        # 中间留着上一帧的拷贝），光标停在正文区。
        half = (row_seq(0, rows[2]) + row_seq(1, rows[3])
                + row_seq(5, rows[7]))
        self._feed(half)
        self.assertEqual(self._hist_texts(), [], "半帧状态就提交历史")
        # 帧补完（底部新内容 + 光标回到输入框区）：历史里应该是真滚出的行
        rest = [rows[y + 2] for y in range(2, 22)] + ["N1", "N2"]
        self._feed(b"".join(row_seq(y, t) for y, t in zip(range(24), rest)))
        ht = self._hist_texts()
        self.assertIn(rows[0], ht)
        self.assertIn(rows[1], ht)
        self.assertNotIn("L05 " + "x" * (self.COLS - 8), ht)  # 半帧的中间拷贝没混进来

    def test_full_screen_shift_committed(self):
        """K=22 的大位移（一屏几乎全换）：对齐游程只有 2 行也要认，
        不能被「游程 ≥5」挡掉。"""
        _, rows = self._frame1()
        self._feed(b"".join(row_seq(y, t) for y, t in enumerate(rows)))
        big = ["B%02d " % y + "y" * (self.COLS - 8) for y in range(2)]  # 顶部新块
        rest = [rows[y + 2] for y in range(20)] + big  # 旧 2..21 上移 20 行到 2..21? 不：
        # 真 K=22 滚动：新屏 = 旧[2..23] 全部离屏? 不可能（K<rows）。构造 K=20：
        # 顶部 20 行换新内容，旧 0..19 离屏，旧 20..21 上移到 0..1，底部 2 行新。
        rest = [rows[20], rows[21]] + ["B%02d " % y + "y" * (self.COLS - 8)
                                       for y in range(20)] + ["N1", "N2"]
        self._feed(b"".join(row_seq(y, t) for y, t in zip(range(24), rest)))
        ht = self._hist_texts()
        for y in range(20):
            self.assertIn(rows[y], ht, "K=20 大位移：旧第 %d 行没进历史" % y)

    def test_pure_inplace_tick_no_commit(self):
        """转圈/计数原地更新（只有状态行变）：不得伪造位移写历史。"""
        _, rows = self._frame1()
        self._feed(b"".join(row_seq(y, t) for y, t in enumerate(rows)))
        self.assertEqual(self._hist_texts(), [])
        self._feed(row_seq(22, "✻ Infusing… (2s · ↓ 1.4k tokens)"))
        self.assertEqual(self._hist_texts(), [], "原地更新被当成了位移")

    def test_garbage_repaint_rebaselines(self):
        """弹层收起/⌃L 整屏重画：连续垃圾态后基线必须重置，
        否则之后的正常滚动永远对不上、历史从此断流。"""
        _, rows = self._frame1()
        self._feed(b"".join(row_seq(y, t) for y, t in enumerate(rows)))
        # 整屏换成一堆不相干内容（模拟重绘/弹层），光标在底部 = 合法帧边界
        junk = ["J%02d " % y + "z" * (self.COLS - 8) for y in range(22)]
        junk += ["❯ 输入框", "❯ 输入框"]
        for _ in range(10):
            self._feed(b"".join(row_seq(y, t) for y, t in zip(range(24), junk)))
        # 基线恢复后再来一次正常滚动（旧内容上移 4 行 + 底部新块），历史要能继续提交
        r2 = junk[4:] + ["RX-A tail", "RX-B tail"]
        self._feed(b"".join(row_seq(y, t) for y, t in zip(range(24), r2)))
        ht = self._hist_texts()
        self.assertTrue(ht, "基线卡死：重绘后的滚动不再提交历史")
        self.assertIn(junk[0], ht)


if __name__ == "__main__":
    unittest.main()
