# SPDX-License-Identifier: AGPL-3.0-or-later

"""语音输入泛化的页面接线（逻辑在 tests/test_voice_anywhere.mjs，没有 node 就跳过）。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_voice_anywhere_node_gate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不在，跳过页面接线检查")
    r = subprocess.run([node, str(ROOT / "tests" / "test_voice_anywhere.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
