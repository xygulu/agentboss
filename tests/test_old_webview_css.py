#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""手机上（WebView 70 / Android 8.1）跑得动的 CSS —— 只钉 `inset` 这个坑。

2026-11-19 S24 mini 实测：管理后台点「更多」只出一条看不清的竖线，短信二次验证框一样。
原因：`position:fixed;inset:0` 是 Chrome 87 才有的简写，WebView 70 直接不认识 →
固定定位框没有 left/right，宽度收缩成 0，里面绝对定位的面板就只剩一条竖条。
写全 top/left/right/bottom 老内核照样认，新浏览器结果一模一样。
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SHIPPED = ["relay/relay.py", "host/server.py", "host/static/ext.html", "host/static/src/page.html"]

# 只找 CSS 里的简写（`inset:0` / `inset: 0 0 auto`），JS 变量、Python 里的同名字符串不算
CSS_INSET = re.compile(r"(?:^|[;{\s])inset\s*:", re.M)


def test_no_inset_shorthand():
    bad = []
    for rel in SHIPPED:
        p = HERE / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if CSS_INSET.search(line):
                bad.append("%s:%d  %s" % (rel, i, line.strip()[:90]))
    assert not bad, "这些地方用了 inset 简写，WebView 70 上是零宽度：\n" + "\n".join(bad)


def test_subpage_content_scrolls():
    """全屏子页（文件/测试/交付）的内容区要能滚：.subpage 是 fixed flex 列，
    .pgin 不设 overflow 时超出的部分被直接裁掉——产出物一多，底下的文件就看不到了
    （2026-11-19 用户反馈）。"""
    p = (HERE / "host" / "static" / "src" / "page.html").read_text(encoding="utf-8")
    m = re.search(r"\.subpage>\.pgin\{([^}]*)\}", p)
    assert m, "缺少 .subpage>.pgin 规则：子页内容区不会滚动"
    assert "overflow-y:auto" in m.group(1), "子页内容区要 overflow-y:auto 才能滑到底"


if __name__ == "__main__":
    test_no_inset_shorthand()
    print("test_old_webview_css: ok")
