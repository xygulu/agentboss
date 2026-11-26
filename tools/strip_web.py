#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""发布打包时压缩网页资源：去掉注释、缩进和多余空白。

    python3 tools/strip_web.py 文件...

只处理「一定安全」的部分，宁可留一点空白也不改坏页面：
- HTML：删 <!-- --> 注释、行尾空白、纯空行；<script>/<style> 里分别按 JS/CSS 处理。
- JS：删 // 与 /* */ 注释（认字符串、模板字符串和转义，URL 里的 // 不受影响），
      删行首缩进和空行；模板字符串内部原样保留。
- CSS：删 /* */ 注释，折叠空白（字符串内部保留）。

为什么不用 node 工具链：安装包要在没有 node 的机器上也能打出来，这里只用标准库。
"""
import sys

JS = ".js"
CSS = ".css"


_KW = set("""return typeof instanceof in of new delete void throw case do else
yield await""".split())
_REGEX_PREV = set("([{,;:=!&|?+-*%<>~^}")


def _js(text):
    out = []
    i, n = 0, len(text)
    blank = True          # 当前输出行是否还只有空白
    prev = ""             # 上一个有意义的字符（判断 / 是除法还是正则）
    word = ""             # 上一个标识符（判断 return / 这类关键字后面的正则）
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                    blank = True
                i += 1
            i += 2
            continue
        if c in "'\"`":
            q = c
            i = _copy_js_string(text, i, out)
            blank = False
            prev, word = q, ""
            continue
        if c == "/" and (prev == "" or prev in _REGEX_PREV or word in _KW):
            i = _copy_js_regex(text, i, out)
            blank = False
            prev, word = "/", ""
            continue
        if c == "\n":
            while out and out[-1] in " \t":
                out.pop()
            out.append("\n")
            blank = True
            prev, word = "", ""
            i += 1
            continue
        if blank and c in " \t":
            i += 1
            continue
        out.append(c)
        if c not in " \t":
            blank = False
            if c.isalnum() or c in "_$":
                word = (word + c) if (prev.isalnum() or prev in "_$") else c
            else:
                word = ""
            prev = c
        i += 1
    # 不再整体扫一遍：模板字符串里的空白/空行是有意义的，原样保留
    return "".join(out).strip("\n") + "\n"


def _copy_js_regex(text, i, out):
    out.append("/")
    i += 1
    n = len(text)
    in_cls = False
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if c == "\n":           # 正则不会跨行：认错了就及时收手
            return i
        out.append(c)
        i += 1
        if c == "[":
            in_cls = True
        elif c == "]":
            in_cls = False
        elif c == "/" and not in_cls:
            while i < n and (text[i].isalpha()):
                out.append(text[i])
                i += 1
            return i
    return i


def _copy_js_string(text, i, out):
    q = text[i]
    out.append(q)
    i += 1
    n = len(text)
    templ = q == "`"
    depth = 0
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if templ and c == "$" and i + 1 < n and text[i + 1] == "{":
            out.append("${")
            i += 2
            depth = 1
            while i < n and depth:
                cc = text[i]
                if cc in "'\"":
                    i = _copy_js_string(text, i, out)
                    continue
                if cc == "`":
                    i = _copy_js_string(text, i, out)
                    continue
                if cc == "{":
                    depth += 1
                elif cc == "}":
                    depth -= 1
                out.append(cc)
                i += 1
            continue
        out.append(c)
        i += 1
        if c == q:
            return i
    return i


def _tidy(text):
    lines = [ln.rstrip() for ln in text.split("\n")]
    res = []
    for ln in lines:
        if not ln and res and not res[-1]:
            continue
        res.append(ln)
    while res and not res[0]:
        res.pop(0)
    while res and not res[-1]:
        res.pop()
    return "\n".join(res) + ("\n" if res else "")


def _css(text):
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in "'\"":
            q = c
            out.append(q)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                if text[i - 1] == q:
                    break
            continue
        if c.isspace():
            while i < n and text[i].isspace():
                i += 1
            out.append(" ")
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    for a in ("{", "}", ";", ",", ":"):
        s = s.replace(" " + a, a).replace(a + " ", a)
    s = s.replace("{ ", "{").replace("} ", "}")
    return s.strip() + "\n"


def _html(text):
    out = []
    i, n = 0, len(text)
    low = text.lower()
    while i < n:
        if text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            i = n if j < 0 else j + 3
            continue
        if low.startswith("<script", i):
            j = text.find(">", i)
            if j < 0:
                out.append(text[i:])
                break
            k = low.find("</script>", j + 1)
            if k < 0:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])
            out.append("\n")
            out.append(_js(text[j + 1:k]))
            out.append(text[k:k + len("</script>")])
            i = k + len("</script>")
            continue
        if low.startswith("<style", i):
            j = text.find(">", i)
            k = low.find("</style>", j + 1)
            if j < 0 or k < 0:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])
            out.append("\n")
            out.append(_css(text[j + 1:k]))
            out.append(text[k:k + len("</style>")])
            i = k + len("</style>")
            continue
        out.append(text[i])
        i += 1
    s = "".join(out)
    lines = [ln.rstrip() for ln in s.split("\n")]
    res = []
    for ln in lines:
        if not ln.strip():
            if res and not res[-1].strip():
                continue
            res.append("")
            continue
        res.append(ln)
    return "\n".join(res).strip() + "\n"


def strip(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    low = path.lower()
    if low.endswith(JS):
        dst = _js(src)
    elif low.endswith(CSS):
        dst = _css(src)
    else:
        dst = _html(src)
    with open(path, "w", encoding="utf-8") as f:
        f.write(dst)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        strip(p)
