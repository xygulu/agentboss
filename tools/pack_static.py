#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""把 static/ 里的网页资源压进一个 Python 模块，给发布包用。

发布包里没有 static/ 目录：页面、脚本都编在 static_pack.pyc 里，装完在安装目录
下翻不到可读的前端源码（开发检出照旧读 static/ 目录）。

用法：pack_static.py <static 目录> <输出的 .py 路径>
"""
import base64
import hashlib
import pathlib
import sys
import zlib


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[0])
        return 2
    src = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2])
    if not src.is_dir():
        print("✗ 没有目录：%s" % src)
        return 1
    files = {}
    total = 0
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name == "_preview.html":      # 开发预览页，带 token，绝不进包
            continue
        raw = p.read_bytes()
        files[p.name] = base64.b64encode(zlib.compress(raw, 9)).decode()
        total += len(raw)
    if not files:
        print("✗ %s 里没有文件" % src)
        return 1
    # 版本号：内容变就变，App 拿它判断内存里的页面是不是旧的
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode("utf-8"))
        h.update(base64.b64decode(files[k]))
    ver = int(h.hexdigest()[:15], 16)
    lines = [
        "# 由 tools/pack_static.py 生成，别手改",
        "VER = %d" % ver,
        "FILES = {",
    ]
    for k in sorted(files):
        lines.append("    %r: %r," % (k, files[k]))
    lines.append("}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("✓ static_pack：%d 个文件，%d KB → %d KB"
          % (len(files), total // 1024, out.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
