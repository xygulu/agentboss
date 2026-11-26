# SPDX-License-Identifier: AGPL-3.0-or-later

"""静态前端自检：把页面里的内联 <script> 拆出来过一遍 node --check，
再跑 tests/ 下那几个自带的 .mjs 用例。

为什么要单独一条：ext.html 是几万行的单文件，语法错只会在手机上白屏，
本机跑测试完全看不出来。CI 里这一条是「改前端不用开真机」的最低保证。

跑法：
    python tools/check_static.py
"""
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def check_inline_scripts(path):
    """每个 <script> 块单独 node --check；报错带块序号和行号偏移。"""
    html = path.read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        for i, b in enumerate(blocks):
            if not b.strip():
                continue
            js = pathlib.Path(td) / ("block%d.js" % i)
            js.write_text(b, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            if r.returncode:
                bad += 1
                print("FAIL %s 第 %d 个 <script>：\n%s" % (path.name, i + 1, r.stderr[:800]))
    print("%s：%d 个 script 块，%d 个失败" % (path.name, len(blocks), bad))
    return bad


def check_mjs():
    bad = 0
    for f in sorted((ROOT / "tests").glob("*.mjs")):
        r = subprocess.run(["node", str(f)], capture_output=True, text=True)
        ok = r.returncode == 0 and "not ok" not in r.stdout.lower()
        print("%s %s：%s" % ("ok  " if ok else "FAIL", f.name, (r.stdout or r.stderr).strip()[:300]))
        bad += 0 if ok else 1
    return bad


def main():
    bad = 0
    for f in sorted((ROOT / "host" / "static").glob("*.html")):
        bad += check_inline_scripts(f)
    bad += check_mjs()
    selftest()
    print("PASS" if not bad else "FAILED %d" % bad)
    return 1 if bad else 0


def selftest():
    """自检：坏语法必须被抓到（不然这条检查永远绿）。"""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "x.html"
        p.write_text("<script>var a = ;</script>", encoding="utf-8")
        assert check_inline_scripts(p) == 1, "坏脚本没被抓到"


if __name__ == "__main__":
    sys.exit(main())
