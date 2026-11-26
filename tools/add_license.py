#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给源码文件幂等加 AGPL-3.0 SPDX 头（Phase 3 Task 2）。

用法: python3 tools/add_license.py [目录|文件 ...]   # 缺省 = 仓内源码集
      python3 tools/add_license.py --check [目录]    # 只检查，缺头退出码 1

头 = 一行 SPDX 注释（按扩展名选注释语法）+ 空行；有 shebang 插在其后，否则在文件头。
已有 SPDX 的文件跳过（幂等）；非源码扩展名/生成物（ext.html）/二进制一律不动。
"""
import os
import sys

SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"
COMMENT = {".py": "#", ".sh": "#", ".js": "//", ".java": "//",
           ".kt": "//", ".swift": "//"}
SKIP_EXT = (".png", ".jpg", ".jpeg", ".ico", ".keystore", ".db", ".pyc",
            ".apk", ".ipa", ".dmg", ".gz", ".woff", ".woff2", ".ttf",
            ".bin", ".zip", ".tar")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv",
             ".superpowers", "build", "dist"}
# 第三方 vendored：上游是 MIT，不许打成 AGPL（头手工钉成 MIT，工具跳过）
VENDORED = {"host/static/xterm.js", "host/static/xterm-addon-fit.js"}
DEFAULT_ROOTS = ("host", "relay/relay.py", "shared", "clients/android",
                 "clients/ios", "tests", "tools")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _add_to_file(path):
    rel = os.path.relpath(path, REPO)
    if rel in VENDORED:
        return "skip(vendored)"
    c = COMMENT.get(os.path.splitext(path)[1])
    if c is None:
        return "skip(gen)"
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except (UnicodeDecodeError, OSError):
        return "skip(binary)"
    if SPDX in src[:600]:
        return "have"
    block = "%s %s\n\n" % (c, SPDX)
    nl = src.find("\n") + 1
    out = src[:nl] + block + src[nl:] if src.startswith("#!") else block + src
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return "added"


def _iter_files(roots):
    for r in roots:
        p = r if os.path.isabs(r) else os.path.join(REPO, r)
        if os.path.isfile(p):
            yield p
        elif os.path.isdir(p):
            for dp, dns, fns in os.walk(p):
                dns[:] = [d for d in dns if d not in SKIP_DIRS]
                for fn in sorted(fns):
                    if os.path.splitext(fn)[1] in COMMENT:
                        yield os.path.join(dp, fn)


def main(argv):
    check = "--check" in argv
    roots = [a for a in argv if a != "--check"] or \
        [os.path.join(REPO, r) for r in DEFAULT_ROOTS]
    missing = []
    changed = 0
    for p in _iter_files(roots):
        if os.path.relpath(p, REPO) in VENDORED:
            continue
        if check:
            try:
                head = open(p, encoding="utf-8").read(600)
            except (UnicodeDecodeError, OSError):
                continue
            if SPDX not in head:
                missing.append(p)
        else:
            if _add_to_file(p) == "added":
                changed += 1
    if check:
        for m in missing:
            print("no SPDX:", m)
        print("missing: %d" % len(missing))
        return 1 if missing else 0
    print("headers added: %d" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
