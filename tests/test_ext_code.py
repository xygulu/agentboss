# SPDX-License-Identifier: AGPL-3.0-or-later

"""代码查看（批次三 Task 10）：GET /api/ext_code。

根 = 会话 cwd 子树，越界/符号链接/凭据名一律拒（复用 _ext_guard_path）；
单次 ≤400 行、文件 ≤2MB；message 里的 path:line 点开就直达那一行。

跑法： .venv/bin/python -m pytest tests/test_ext_code.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    return tmp_path


def _code(path="a.py", line=1, n=None, sid="s1"):
    return asyncio.run(server._ext_code_payload(sid, path, line, n))


def test_pages_to_requested_line(cwd):
    (cwd / "a.py").write_text("\n".join("L%d" % i for i in range(1, 501)))
    d = _code("a.py", 120)
    assert d["total"] == 500
    assert d["start"] == 120
    assert d["lines"][0] == {"no": 120, "text": "L120"}
    assert d["lang"] == "python"


def test_caps_at_400_lines(cwd):
    (cwd / "a.py").write_text("\n".join("L%d" % i for i in range(1, 1001)))
    d = _code("a.py", 1, 9999)
    assert len(d["lines"]) == 400


def test_line_past_end_clamps_to_last_page(cwd):
    (cwd / "a.py").write_text("\n".join("L%d" % i for i in range(1, 101)))
    d = _code("a.py", 5000)
    assert d["lines"][-1]["no"] == 100


def test_outside_cwd_rejected(cwd):
    with pytest.raises(server._ExtPathError):
        _code("../outside.py")


def test_dotenv_rejected(cwd):
    (cwd / ".env").write_text("SECRET=1")
    with pytest.raises(server._ExtPathError):
        _code(".env")


def test_too_big_rejected(cwd):
    (cwd / "big.py").write_text("x" * (server._EXT_CODE_MAX + 1))
    with pytest.raises(server._ExtPathError):
        _code("big.py")


def test_unknown_sid_is_404_ish(cwd):
    with pytest.raises(server._ExtPathError):
        _code("a.py", sid="nope")
