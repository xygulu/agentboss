#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""host 发布包清单完整性：server.py 依赖的本地模块必须都在打包清单里。

2026-11-18 差点翻车：统一仓新增 branding/agent_opencode/agent_hermes 后
清单没补——发布出去的包 import 失败，存量 host 升级后起不来（等于打断
自动更新机制）。这个测试就是防它再发生。

跑法： .venv/bin/python -m pytest tests/test_pkg_manifest.py -q
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _local(m):
    return any((HERE / d / (m + ".py")).is_file() for d in ("", "host", "shared", "relay"))


def _server_local_imports():
    src = (HERE / "host" / "server.py").read_text(encoding="utf-8")
    mods = set(re.findall(r"^import (\w+)", src, re.M))
    return sorted(m for m in mods if _local(m))


def test_pkg_scripts_cover_all_local_imports():
    need = [m + ".py" for m in _server_local_imports()]
    assert need, "server.py 的本地 import 解析为空（解析坏了）"
    for script in ("publish-host.sh", "build-pyc.sh"):
        text = (HERE / "host" / script).read_text(encoding="utf-8")
        missing = [f for f in need if f not in text]
        assert not missing, "%s 打包清单缺：%s（发出去的包会 import 失败）" % (script, missing)


def test_packaged_source_files_exist():
    """清单里点名的文件必须真实存在（改名/删除后清单要跟着改）。"""
    text = (HERE / "host" / "publish-host.sh").read_text(encoding="utf-8")
    m = re.search(r"for f in (.+?)\s*\\\n", text)
    assert m, "publish-host.sh 里找不到打包清单 for 循环"
    names = [x.strip() for x in m.group(1).split() if x.strip().endswith(".py")]
    gone = [f for f in names if not (HERE / "host" / f).is_file()]
    assert not gone, "publish-host.sh 清单点了不存在的文件：%s" % gone


def test_build_pyc_ships_every_module():
    """无源码包最终落盘名单（cp …"$TMP/x.pyc" … "$OUT/"）必须覆盖全部模块。

    它和编译清单是两份独立名单：编译了但没拷进发行目录，装完照样 import 崩。
    入口 4 个走 _impl 薄壳（build-pyc.sh 里的 mv），其余按 server.py 的本地
    import 展开。接管自旧 codeboss 仓 test_packaging.py（2026-11-18 收编）。"""
    text = (HERE / "host" / "build-pyc.sh").read_text(encoding="utf-8")
    joined = text.replace("\\\n", " ")
    shipped = set()
    for stmt in joined.splitlines():
        if stmt.strip().startswith("cp ") and '"$OUT/"' in stmt:
            for n in re.findall(r"\$TMP/(\w+)\.pyc", stmt):
                shipped.add(n[:-5] if n.endswith("_impl") else n)
    need = set(_server_local_imports()) | {"server", "wrapper", "tray", "ptywin"}
    missing = sorted(need - shipped)
    assert not missing, "build-pyc.sh 落盘名单缺：%s（无源码包装完 import 崩）" % missing
