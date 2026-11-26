# SPDX-License-Identifier: AGPL-3.0-or-later

"""「装 App」在 iPhone 上不能把主页面导航走（2026-11-19 反馈：点了跳断网）。

跑法：.venv/bin/python -m pytest tests/test_apk_open.py -q
（断言在 tests/test_apk_open.mjs，纯 node；没装 node 就跳过。）
"""
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_apk_open_ui():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("没有 node，跳过 JS 门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_apk_open.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]
