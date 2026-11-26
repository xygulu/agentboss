# SPDX-License-Identifier: AGPL-3.0-or-later

"""页面入口登记表门禁（2026-11-20）：每个 data-* 入口都要登记属于哪条线、为什么。

跑法：.venv/bin/python -m pytest tests/test_page_entries.py -q
（断言在 tests/test_page_entries.mjs，纯 node；没装 node 就跳过。）
"""
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_page_entries_registered():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("没有 node，跳过 JS 门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_page_entries.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
