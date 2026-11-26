#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""发布打包时去掉脚本里的注释——发布物只留能跑的东西，不留解释性文字。

    python3 tools/strip_comments.py 文件...

规则（保守，只动确定是注释的地方）：
- 保留第一行的 shebang（#!/...）；
- 整行注释（缩进后以 # 开头）整行删掉；.cmd 的 :: 整行注释同理；
- .ps1 的 <# ... #> 块注释按区间删；
- 行尾注释只在引号外、且 # 前面是空白时才切（"a # b" 这种字符串不动）；
- 空行保留原样。

为什么不用现成的 strip 工具：shell/PowerShell 的引号规则各不相同，这里只做
「确定安全」的那部分，宁可漏删也不改坏脚本。
"""
import sys


def _strip_trailing(line):
    q = None
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if q:
            if c == "\\" and q != "'":
                i += 2
                continue
            if c == q:
                q = None
        else:
            if c in "'\"`":
                q = c
            elif c == "#" and i > 0 and line[i - 1] in " \t":
                return line[:i].rstrip()
        i += 1
    return line.rstrip()


def strip(path):
    # utf-8-sig：源码本身可能就是带 BOM 的（.ps1 必须是）。按 utf-8 读会把这个
    # U+FEFF 留在正文里，写回时再加一个 → 文件开头两个 BOM，PowerShell 5.1 解析
    # 首行就失败（退出码 1，日志都来不及建）。
    with open(path, encoding="utf-8-sig") as f:
        src = f.read()
    ps = path.lower().endswith(".ps1")
    cmd = path.lower().endswith((".cmd", ".bat"))
    out = []
    in_block = False
    for idx, ln in enumerate(src.splitlines()):
        if idx == 0 and ln.startswith("#!"):
            out.append(ln)
            continue
        if ps:
            if in_block:
                if "#>" in ln:
                    in_block = False
                    ln = ln.split("#>", 1)[1]
                else:
                    continue
            if "<#" in ln:
                head, tail = ln.split("<#", 1)
                if "#>" in tail:
                    ln = head + tail.split("#>", 1)[1]
                else:
                    in_block = True
                    ln = head
        s = ln.lstrip()
        if not s:
            out.append("")
            continue
        if s.startswith("#") or (cmd and s.startswith("::")):
            continue
        if not ps:
            ln = _strip_trailing(ln)
        out.append(ln)
    txt = "\n".join(out)
    if src.endswith("\n"):
        txt += "\n"
    # PowerShell 5.1 读无 BOM 的 UTF-8 会按本地代码页解，中文串乱码甚至串义：
    # .ps1 一律写成带 BOM 的 UTF-8（.cmd 不能加，BOM 会被当成命令一部分）
    enc = "utf-8-sig" if path.lower().endswith(".ps1") else "utf-8"
    with open(path, "w", encoding=enc) as f:
        f.write(txt)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        strip(p)
