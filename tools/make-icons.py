#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""从唯一源图 `mac/icon-1024.png` 生成所有平台的图标。

统一口径：App 图标只此一份（mac/icon-1024.png）；Mac/iOS/Windows/Android/网页
都由它派生，任何一端要改图标都先换源图，再跑这个脚本 + 各端打包脚本。

用法：
    python3 tools/make-icons.py            # 重新生成 android/site 下的图标
    python3 tools/make-icons.py --product agentboss   # 只出产品线图标覆盖（products/<线>/android/res）
    python3 tools/make-icons.py --relay    # 额外打印中继内联 favicon 的 Python 常量

生成物：
    android/res/mipmap-*/ic_launcher.png          旧版方形图标（24–25）
    android/res/mipmap-*/ic_launcher_round.png    圆形图标（roundIcon）
    android/res/mipmap-*/ic_launcher_foreground.png 自适应图标前景
    android/res/drawable-*/ic_stat_codeboss.png   通知栏白色剪影（18+ 必须）
    site/apple-touch-icon.png                     iOS 添加到主屏幕
"""
import base64
import io
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "clients", "mac", "icon-1024.png")
AND = os.path.join(ROOT, "clients", "android", "res")
# 官网在仓库根目录（codeboss/ 的上一层），不在 codeboss/site
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
SITE = _site_dir()

# 自适应图标的底板色：取源图四周边缘的均值，和源图暗角无缝衔接
ADAPTIVE_BG = (20, 9, 4, 255)
# 自适应图标前景占 108dp 画布的比例：0.88 时手机本体落在 66dp 安全区内，
# 圆/方/水滴遮罩都只裁到边缘暗角（右上信号波可能被切成弧线，与 iOS 一致）
ADAPTIVE_SCALE = 0.88

DPIS = [("mdpi", 1.0), ("hdpi", 1.5), ("xhdpi", 2.0), ("xxhdpi", 3.0), ("xxxhdpi", 4.0)]
# 各产品线通知栏剪影取源图里哪种颜色（codeboss=绿终端，agentboss=金色信封+抽屉）
GLYPH_COLOR = {"codeboss": "green", "agentboss": "yellow"}
# 产品线源图：网页页头/favicon 也按它出（brand_icons.py）
PRODUCT_SRC = {"codeboss": SRC,
               "agentboss": os.path.join(ROOT, "products", "agentboss", "android", "icon-1024.png")}
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def load():
    im = Image.open(SRC).convert("RGBA")
    if im.size != (1024, 1024):
        raise SystemExit("源图必须是 1024×1024：%s" % SRC)
    return im


def rounded(im, size):
    return im.resize((size, size), Image.LANCZOS)


def circular(im, size):
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out.paste(im.resize((size, size), Image.LANCZOS), (0, 0), mask)
    return out


def foreground(im, px, bg=ADAPTIVE_BG):
    """108dp 画布，源图缩到中间 64dp（安全区内），四周填底板色。"""
    out = Image.new("RGBA", (px, px), bg)
    n = max(1, int(round(px * ADAPTIVE_SCALE)))
    pic = im.resize((n, n), Image.LANCZOS)
    off = (px - n) // 2
    out.alpha_composite(pic, (off, off))
    return out


def glyph_mask(im, color="green"):
    """把源图里的符号抠成白色剪影，供通知栏小图标用。

    green = CodeBoss 的绿色终端符号（> + _），yellow = AgentBoss 的金色信封+抽屉。
    通知栏 5.0 起按 alpha 渲染，彩色图标会变成一坨白块；这里只留形状。
    """
    w, h = im.size
    px = im.load()
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            if color == "yellow":
                ok = r > 150 and g > 120 and b < 120 and (r - b) > 60
            else:
                ok = g > 150 and b < 140 and (g - r) > 15 and (g - b) > 40
            if ok:
                mp[x, y] = 255
    if color == "green":
        # 去掉右上角的信号波，只留屏幕里的 > 和 _（y>280 之后没有波纹）
        ImageDraw.Draw(mask).rectangle((0, 0, w, 280), fill=0)
    bbox = mask.getbbox()
    if not bbox:
        raise SystemExit("源图里找不到绿色符号，检查源图是否被换掉")
    crop = mask.crop(bbox)
    side = max(crop.size)
    sq = Image.new("L", (side, side), 0)
    sq.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    white = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    white.putalpha(sq)
    return white


def save(im, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, "PNG", optimize=True)
    print("  %-58s %dx%d" % (os.path.relpath(path, ROOT), im.width, im.height))


def write_adaptive_xml():
    for name in ("ic_launcher", "ic_launcher_round"):
        p = os.path.join(AND, "mipmap-anydpi-v26", name + ".xml")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/ic_launcher_background"/>\n'
                '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                '</adaptive-icon>\n')
        print("  %-58s xml" % os.path.relpath(p, ROOT))
    p = os.path.join(AND, "values", "colors.xml")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<resources>\n'
            '    <!-- 自适应图标底板色：取自 mac/icon-1024.png 四周暗角的均值（tools/make-icons.py） -->\n'
            '    <color name="ic_launcher_background">#140904</color>\n'
            '</resources>\n')
    print("  %-58s xml" % os.path.relpath(p, ROOT))


def product_main(name):
    """产品线图标：源图 products/<线>/android/icon-1024.png → 该线的 res 覆盖目录。

    启动图标、自适应底板色、通知栏剪影都出（2026-11-19 用户：AgentBoss 里还是
    CodeBoss 的图标）。通知栏资源名保持 ic_stat_codeboss，Java 那边不用改。
    """
    src = os.path.join(ROOT, "products", name, "android", "icon-1024.png")
    out = os.path.join(ROOT, "products", name, "android", "res")
    im = Image.open(src).convert("RGBA")
    if im.size != (1024, 1024):
        raise SystemExit("源图必须是 1024×1024：%s" % src)
    bg = im.getpixel((512, 24))          # 源图自带的圆角底板色
    print("产品 %s：%s" % (name, os.path.relpath(src, ROOT)))
    for dpi, f in DPIS:
        size = LEGACY[dpi]
        save(rounded(im, size), os.path.join(out, "mipmap-" + dpi, "ic_launcher.png"))
        save(circular(im, size), os.path.join(out, "mipmap-" + dpi, "ic_launcher_round.png"))
        save(foreground(im, int(round(108 * f)), bg),
             os.path.join(out, "mipmap-" + dpi, "ic_launcher_foreground.png"))
    glyph = glyph_mask(im, GLYPH_COLOR.get(name, "green"))
    for dpi, f in DPIS:
        n = int(round(24 * f))
        inner = Image.new("RGBA", (n, n), (255, 255, 255, 0))
        g = glyph.resize((int(n * 0.92), int(n * 0.92)), Image.LANCZOS)
        o = (n - g.width) // 2
        inner.alpha_composite(g, (o, o))
        save(inner, os.path.join(out, "drawable-" + dpi, "ic_stat_codeboss.png"))
    p = os.path.join(out, "values", "colors.xml")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<resources>\n'
            '    <!-- 自适应图标底板色：取自 products/%s/android/icon-1024.png 的底色 -->\n'
            '    <color name="ic_launcher_background">#%02X%02X%02X</color>\n'
            '</resources>\n' % (name, bg[0], bg[1], bg[2]))
    print("  %-58s xml" % os.path.relpath(p, ROOT))


def web_main():
    """网页用的产品线图标 → brand_icons.py（页头 96px + favicon 32px，内联 base64）。

    页面本身是两条线共用的一份 HTML，_app_page 下发时按本机产品线换这两个占位符。
    """
    out = ['"""产品线图标（tools/make-icons.py --web 生成，别手改）。',
           "",
           "两条线各一套：页面页头（BRAND）与 favicon（FAVICON），base64 PNG。",
           '"""',
           ""]
    for key in ("BRAND_B64", "FAVICON_B64"):
        out.append(key + " = {")
        for prod, src in PRODUCT_SRC.items():
            im = Image.open(src).convert("RGBA")
            if im.size != (1024, 1024):
                raise SystemExit("源图必须是 1024×1024：%s" % src)
            buf = io.BytesIO()
            rounded(im, 96 if key == "BRAND_B64" else 32).save(buf, "PNG", optimize=True)
            out.append('    %r: %r,' % (prod, base64.b64encode(buf.getvalue()).decode()))
        out.append("}")
        out.append("")
    p = os.path.join(ROOT, "shared", "brand_icons.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("  %-58s %d bytes" % (os.path.relpath(p, ROOT), os.path.getsize(p)))


def main():
    if "--web" in sys.argv:
        web_main()
        return
    if "--product" in sys.argv:
        product_main(sys.argv[sys.argv.index("--product") + 1])
        return
    im = load()
    print("源图：%s" % os.path.relpath(SRC, ROOT))
    print("Android 启动图标：")
    for dpi, _ in DPIS:
        size = LEGACY[dpi]
        save(rounded(im, size), os.path.join(AND, "mipmap-" + dpi, "ic_launcher.png"))
        save(circular(im, size), os.path.join(AND, "mipmap-" + dpi, "ic_launcher_round.png"))
        save(foreground(im, int(round(108 * dict(DPIS)[dpi]))),
             os.path.join(AND, "mipmap-" + dpi, "ic_launcher_foreground.png"))
    write_adaptive_xml()
    print("Android 通知栏剪影：")
    glyph = glyph_mask(im)
    for dpi, f in DPIS:
        n = int(round(24 * f))
        # 24dp 里再留 2dp 内边距，圆角通知图标下不顶边
        inner = Image.new("RGBA", (n, n), (255, 255, 255, 0))
        g = glyph.resize((int(n * 0.92), int(n * 0.92)), Image.LANCZOS)
        o = (n - g.width) // 2
        inner.alpha_composite(g, (o, o))
        save(inner, os.path.join(AND, "drawable-" + dpi, "ic_stat_codeboss.png"))
    print("网页：")
    touch = os.path.join(SITE, "apple-touch-icon.png")
    save(rounded(im, 180), touch)
    # 官网导航栏的 logo（index/guide 的 <img src="/logo.png">）也用同一张
    save(rounded(im, 192), os.path.join(SITE, "logo.png"))
    if "--relay" in sys.argv:
        print("\n中继内联 favicon（贴进 relay.py 的 _FAVICON_PNG_B64 / _TOUCH_PNG_B64）：")
        for name, size in (("_FAVICON_PNG_B64", 32), ("_TOUCH_PNG_B64", 180)):
            buf = io.BytesIO()
            rounded(im, size).save(buf, "PNG", optimize=True)
            b = base64.b64encode(buf.getvalue()).decode()
            print('\n%s = (\n    "%s"\n)' % (name, b))
        print("\nAgentBoss 线条目图标（贴进 relay.py 的 _AGENT_ICON_PNG_B64）：")
        aim = Image.open(PRODUCT_SRC["agentboss"]).convert("RGBA")
        buf = io.BytesIO()
        rounded(aim, 180).save(buf, "PNG", optimize=True)
        print('\n_AGENT_ICON_PNG_B64 = (\n    "%s"\n)'
              % base64.b64encode(buf.getvalue()).decode())
    print("\n完成。Mac/iOS/Windows 的图标由各自打包脚本从同一源图生成。")


if __name__ == "__main__":
    main()
