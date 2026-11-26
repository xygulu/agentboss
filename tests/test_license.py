#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""AGPL-3.0：LICENSE 存在 + tools/add_license.py 幂等加 SPDX 头（生成物跳过）。
Phase 3 checklist 条目 4：License=AGPL-3.0（LICENSE + 文件头，tool 化批量加）。"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "add_license.py")


def test_license_file_exists():
    txt = open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read()
    up = txt.upper()
    assert "GNU AFFERO GENERAL PUBLIC" in up
    assert "VERSION 3" in up


def _run_tool(args, cwd):
    return subprocess.run([sys.executable, TOOL] + args, cwd=cwd,
                          capture_output=True, text=True)


def test_headers_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        open(os.path.join(tmp, "sample.py"), "w").write(
            "#!/usr/bin/env python3\nprint('hi')\n")
        open(os.path.join(tmp, "sample.js"), "w").write("function f(){return 1}\n")
        _run_tool([tmp], ROOT)
        py1 = open(os.path.join(tmp, "sample.py")).read()
        js1 = open(os.path.join(tmp, "sample.js")).read()
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in py1
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in js1
        # shebang 必须仍在最前（SPDX 头插在 shebang 之后）
        assert py1.startswith("#!/usr/bin/env python3")
        # 再跑一遍：不叠加（幂等）
        _run_tool([tmp], ROOT)
        assert open(os.path.join(tmp, "sample.py")).read() == py1
        assert open(os.path.join(tmp, "sample.js")).read() == js1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_generated_skipped():
    tmp = tempfile.mkdtemp()
    try:
        open(os.path.join(tmp, "ext.html"), "w").write("<html></html>\n")
        _run_tool([tmp], ROOT)
        assert "SPDX" not in open(os.path.join(tmp, "ext.html")).read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_repo_sources_have_headers():
    # 抽查仓内真实源码已被加过头（工具实跑过一遍的证据）
    for rel in ("host/server.py", "relay/relay.py", "shared/branding.py"):
        head = open(os.path.join(ROOT, rel), encoding="utf-8").read(400)
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in head, rel


def test_vendored_mit_not_agpl():
    # 第三方 vendored 库（xterm.js 系 MIT）不许被打成 AGPL（终审 Important #1）
    for rel in ("host/static/xterm.js", "host/static/xterm-addon-fit.js"):
        head = open(os.path.join(ROOT, rel), encoding="utf-8").read(400)
        assert "SPDX-License-Identifier: MIT" in head, rel
        assert "AGPL-3.0-or-later" not in head, rel
