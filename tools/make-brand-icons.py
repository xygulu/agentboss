#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""aiboss 系列的品牌图标：代理工头 App 图标 + 系列核心标志。

口径：CodeBoss 的图标（mac/icon-1024.png）是母版——暖黑底、橙色圆角手机框、
深蓝屏、屏内一个亮色字形、右上两道信号弧。代理工头和工头主机**都直接取母版的
框**（不再各画各的圆角框），只擦掉屏内的绿字形、换上新字形和强调色：
代理工头 = 金色收件匣，工头主机 = 米白显示器；信号弧的颜色同步跟着换。
系列标志直接用母版本身（用户 2026-11-19 拍板），一字不改。

用法：
    python3 tools/make-brand-icons.py            # 只打印（预览到 build/brand/）
    python3 tools/make-brand-icons.py --install  # 落到各端源图位置（各端打包脚本再派生）

产出：
    build/brand/agentboss-icon-1024.png   代理工头 App 图标（源图）
    build/brand/aiboss-mark-1024.png      系列核心标志
    build/brand/aiboss-mark-256.png       网页/页头用的小尺寸
"""
import os
import sys

from PIL import Image, ImageDraw
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build", "brand")
def _site_dir():
    """官网仓库位置：本仓库拆出来后不在上一级，优先 CB_SITE，其次旧仓
    vibecoding-terminal/site（现官网真正在跑的那份）。"""
    p = os.environ.get("CB_SITE")
    if p:
        return p
    for c in (os.path.join(os.path.dirname(ROOT), "vibecoding-terminal", "site"),
              os.path.join(os.path.dirname(ROOT), "site")):
        if os.path.isdir(c):
            return c
    return os.path.join(os.path.dirname(ROOT), "site")


# 母版取色（mac/icon-1024.png 实测）
SCREEN = (15, 21, 30)
GOLD = (230, 185, 63)
CREAM = (245, 227, 213)



def _rr(d, box, r, fill=None, outline=None, width=0):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


MASTER = os.path.join(ROOT, "mac", "icon-1024.png")
GLYPH_BOX = (305, 345, 700, 675)   # 母版绿 prompt 在屏上的外接框（含余量）


def _base():
    """三套图标共用母版手机框，只把屏内字形擦掉。
    屏幕是纵向微渐变：按行取屏两侧的底色回填，擦完不留色块。"""
    im = Image.open(MASTER).convert("RGBA")
    px = im.load()
    x0, y0, x1, y1 = GLYPH_BOX
    for y in range(y0, y1):
        samples = [px[x, y] for x in range(272, 302)] + [px[x, y] for x in range(706, 746)]
        samples.sort(key=lambda c: c[0] + c[1] + c[2])
        fill = samples[len(samples) // 2]
        for x in range(x0, x1):
            px[x, y] = fill
    return im


def _recolor_arcs(im, base):
    """右上信号弧跟着子品牌走：母版是绿弧，按亮度换成金/橙，两道弧的明暗保留。"""
    a = np.array(im).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    ys, xs = np.where((g > 90) & (g > r + 8) & (g > b + 25))
    for y, x in zip(ys, xs):
        # 亮度映射别压太狠：外圈那道本来就更暗，直接按比例缩会发褐
        k = 0.6 + 0.4 * (a[y, x, 1] / 245.0)
        im.putpixel((int(x), int(y)), tuple(min(255, int(c * k)) for c in base) + (255,))
    return im


def agentboss_icon():
    """代理工头：同一个手机框，屏里「收件匣」——信封落进托盘：多个 agent 的产出收在一处。"""
    im = _recolor_arcs(_base(), GOLD)
    d = ImageDraw.Draw(im)
    # 信封：圆角矩形 + 两道斜折线，做成「信封正面」
    ex0, ey0, ex1, ey1 = 366, 350, 658, 538
    _rr(d, (ex0, ey0, ex1, ey1), 32, fill=GOLD)
    d.polygon([(ex0 + 6, ey0 + 6), (ex1 - 6, ey0 + 6), ((ex0 + ex1) // 2, ey0 + 165)], fill=SCREEN)
    # 托盘：一圈金色圆角框（中间挖空）
    _rr(d, (344, 570, 680, 658), 44, fill=GOLD)
    _rr(d, (344 + 32, 570 + 28, 680 - 32, 658 - 28), 24, fill=SCREEN)
    return im


def bosshost_icon():
    """工头主机：同一个手机框里换成「显示器」——装在电脑上的那一台。
    米白描线（和听筒/底条同色），不和橙框抢颜色。"""
    im = _recolor_arcs(_base(), CREAM)
    d = ImageDraw.Draw(im)
    _rr(d, (344, 368, 680, 584), 34, fill=CREAM)      # 屏幕（内挖空）
    _rr(d, (376, 400, 648, 552), 18, fill=SCREEN)
    _rr(d, (486, 584, 538, 622), 16, fill=CREAM)      # 支架
    _rr(d, (424, 622, 600, 656), 16, fill=CREAM)      # 底座
    return im


def aiboss_mark():
    """系列标志（用户拍板 2026-11-19）：直接用 CodeBoss 母版图标，一字不改。"""
    return Image.open(MASTER).convert("RGBA")


def main():
    os.makedirs(OUT, exist_ok=True)
    ab = agentboss_icon()
    bh = bosshost_icon()
    mk = aiboss_mark()
    ab.save(os.path.join(OUT, "agentboss-icon-1024.png"))
    bh.save(os.path.join(OUT, "bosshost-icon-1024.png"))
    mk.save(os.path.join(OUT, "aiboss-mark-1024.png"))
    mk.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "aiboss-mark-256.png"))
    if "--install" in sys.argv:
        dest = {
            os.path.join(ROOT, "products", "android", "agentboss", "icon-1024.png"): ab,
            os.path.join(_site_dir(), "agentboss-icon.png"): ab.resize((512, 512), Image.LANCZOS),
            os.path.join(_site_dir(), "bosshost-icon.png"): bh.resize((512, 512), Image.LANCZOS),
            os.path.join(_site_dir(), "aiboss-mark.png"): mk.resize((512, 512), Image.LANCZOS),
        }
        for p, img in dest.items():
            os.makedirs(os.path.dirname(p), exist_ok=True)
            img.save(p)
            print("→", p)
    print("✓ 预览：", OUT)


if __name__ == "__main__":
    main()
