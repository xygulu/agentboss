#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""App 图标草稿：工头 3 版 / 老板 3 版，同一套规格（墨底 #0B0E14、工头黄 #F5C542、
扁平矢量、圆角留白一致）。用户挑定后再换 mac/icon-1024.png 跑 tools/make-icons.py。

用法：
    python3 tools/make-icon-drafts.py            # → /tmp/icon-drafts/*.png + sheet.png
"""
import os

from PIL import Image, ImageDraw

S = 4                       # 4× 超采样后缩回 1024
N = 1024 * S
BG = (11, 14, 20, 255)      # #0B0E14
FG = (245, 197, 66, 255)    # #F5C542
DARK = (11, 14, 20, 255)
OUT = "/tmp/icon-drafts"


def px(v):
    return int(v * S)


def canvas():
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, N - 1, N - 1), radius=px(230), fill=BG)
    return im, d


def rr(d, box, r, color, width=0):
    """width>0 = 只描边（不填），否则实心"""
    d.rounded_rectangle(tuple(px(v) for v in box), radius=px(r),
                        fill=None if width else color, outline=color, width=px(width))


def poly(d, pts, width, color=FG):
    """粗折线，端点/拐点补圆 → 圆头圆角，接近矢量描边"""
    w = px(width)
    d.line([(px(x), px(y)) for x, y in pts], fill=color, width=w, joint="curve")
    for x, y in pts:
        d.ellipse((px(x) - w // 2, px(y) - w // 2, px(x) + w // 2, px(y) + w // 2), fill=color)


def circle(d, cx, cy, r, color):
    d.ellipse((px(cx - r), px(cy - r), px(cx + r), px(cy + r)), fill=color)


def arc(d, box, a0, a1, width, color=FG):
    d.arc(tuple(px(v) for v in box), a0, a1, fill=color, width=px(width))


# ── 工头（CodeBoss）：开发机的终端 ──────────────────────────────

def code_1():
    """终端窗 + >_（现有图标的扁平化）"""
    im, d = canvas()
    rr(d, (186, 260, 838, 764), 96, FG, width=54)
    poly(d, [(368, 420), (528, 512), (368, 604)], 62)
    rr(d, (588, 578, 714, 626), 24, FG)
    arc(d, (616, 118, 742, 244), 275, 360, 30)      # 信号弧，和旧图标一脉
    arc(d, (676, 178, 802, 304), 275, 360, 30)
    return im


def code_2():
    """大 >_ 单字样：工头 = 敲命令的人（和 C1 的「窗」区分开）"""
    im, d = canvas()
    poly(d, [(330, 340), (556, 512), (330, 684)], 84)
    rr(d, (596, 642, 762, 706), 32, FG)
    return im


def code_3():
    """扳手 + 终端：干活 + 写代码"""
    im, d = canvas()
    # 扳手先画，终端窗压在上面：读起来是「工具在，屏幕在前」
    circle(d, 742, 306, 86, FG)
    circle(d, 790, 258, 46, BG)
    poly(d, [(742, 380), (672, 600)], 66)
    rr(d, (196, 316, 664, 728), 84, FG, width=46)
    poly(d, [(322, 432), (438, 522), (322, 612)], 52)
    rr(d, (490, 590, 590, 630), 20, FG)
    return im


# ── 老板（AgentBoss）：收件箱 ──────────────────────────────────

def boss_1():
    """托盘 + 信封：收件箱"""
    im, d = canvas()
    rr(d, (330, 270, 694, 536), 44, FG)                 # 信封
    poly(d, [(330, 330), (512, 452), (694, 330)], 44, DARK)
    rr(d, (220, 560, 804, 770), 72, FG)                 # 托盘
    rr(d, (300, 620, 724, 706), 26, DARK)               # 托盘开口
    return im


def boss_2():
    """公文包 + 对勾：老板收到的是「做完了」"""
    im, d = canvas()
    rr(d, (220, 420, 804, 772), 76, FG)                 # 箱体
    rr(d, (410, 300, 614, 432), 46, FG)                 # 提手外圈
    rr(d, (456, 350, 568, 430), 24, DARK)               # 提手镂空
    poly(d, [(380, 600), (480, 690), (660, 520)], 62, DARK)   # 白色对勾改成墨色镂空
    return im


def boss_3():
    """铃 + 信号：有结果了会响"""
    im, d = canvas()
    d.ellipse((px(404), px(236), px(620), px(372)), fill=FG)     # 铃顶
    d.polygon([(px(404), px(300)), (px(620), px(300)), (px(668), px(620)), (px(356), px(620))],
              fill=FG)                                            # 铃身
    rr(d, (300, 596, 724, 660), 30, FG)                          # 铃口
    circle(d, 512, 730, 44, FG)                                  # 铃舌
    arc(d, (632, 150, 772, 290), 275, 360, 30)                   # 信号弧
    arc(d, (700, 218, 840, 358), 275, 360, 30)
    return im


DRAFTS = [("codeboss", "工头·终端", code_1), ("codeboss", "工头·安全帽", code_2),
          ("codeboss", "工头·扳手", code_3), ("agentboss", "老板·托盘", boss_1),
          ("agentboss", "老板·公文包", boss_2), ("agentboss", "老板·铃", boss_3)]


def main():
    os.makedirs(OUT, exist_ok=True)
    made = []
    for i, (line, name, fn) in enumerate(DRAFTS, 1):
        im = fn().resize((1024, 1024), Image.LANCZOS)
        p = os.path.join(OUT, "%d-%s-%s.png" % (i, line, name.split("·")[1]))
        im.save(p, "PNG", optimize=True)
        made.append((p, line, name))
        print("%s  %6.1fKB" % (p, os.path.getsize(p) / 1024.0))
    sheet = Image.new("RGB", (3 * 360 + 40, 2 * 360 + 40), (24, 26, 31))
    for i, (p, _l, _n) in enumerate(made):
        th = Image.open(p).convert("RGBA").resize((340, 340), Image.LANCZOS)
        x = 20 + (i % 3) * 360
        y = 20 + (i // 3) * 360
        sheet.paste(th, (x, y), th)
    sp = os.path.join(OUT, "sheet.png")
    sheet.save(sp)
    print(sp)


if __name__ == "__main__":
    main()
