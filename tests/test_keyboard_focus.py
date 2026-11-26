# SPDX-License-Identifier: AGPL-3.0-or-later

"""键盘弹起时焦点输入框摆回可见区（逻辑在 tests/test_keyboard_focus.mjs，没有 node 就跳过）。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_keyboard_focus_node_gate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不在，跳过页面接线检查")
    r = subprocess.run([node, str(ROOT / "tests" / "test_keyboard_focus.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
