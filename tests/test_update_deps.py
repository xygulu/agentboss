# SPDX-License-Identifier: AGPL-3.0-or-later

"""更新必须先装依赖、装不上就不更新（2026-11-21：0.9.54 那台永久离线）。

原来的顺序是「覆盖代码 → pip install（不看返回值）→ 重启」：这台机器上 pip 装不上，
新代码起来就 ModuleNotFoundError，进程死了自动更新循环也跟着死，再没人能给它更新。
现在 _deps_install 返回 (ok, why)，调用方拿 False 就放弃这次更新、旧版本继续跑。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import server


def _py(tmp_path):
    p = tmp_path / "python"
    p.write_text("")
    return str(p)


def _run_factory(ok_substrs, smoke_ok=True):
    def run(cmd, timeout=0, *a, **kw):
        line = " ".join(str(c) for c in cmd)
        if "import aiohttp, pyte, qrcode" in line:
            return (0, "", "") if smoke_ok else (1, "", "ImportError: pyte")
        for s in ok_substrs:
            if s in line:
                return 0, "", ""
        return 1, "", "boom"
    return run


def test_falls_back_to_mirror_when_direct_fails(tmp_path, monkeypatch):
    calls = []

    def run(cmd, timeout=0, *a, **kw):
        calls.append(" ".join(str(c) for c in cmd))
        return _run_factory(["pypi.tuna.tsinghua.edu.cn"])(cmd, timeout)

    monkeypatch.setattr(server, "_run", run)
    monkeypatch.setattr(server, "_uv_bin", lambda: "")
    ok, why = server._deps_install(_py(tmp_path), tmp_path / "requirements.txt")
    assert (ok, why) == (True, "")
    assert "pypi.tuna.tsinghua.edu.cn" in calls[0], "国内直连 PyPI 常被掐，镜像先试"


def test_refuses_when_every_source_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_run", _run_factory([], smoke_ok=False))
    monkeypatch.setattr(server, "_uv_bin", lambda: "")
    ok, why = server._deps_install(_py(tmp_path), tmp_path / "requirements.txt")
    assert not ok and "依赖" in why


def test_passes_when_no_installer_but_deps_already_there(tmp_path, monkeypatch):
    """uv 建的 venv 可能没有 pip：装不了不算失败，只要 import 得通就放行。"""
    monkeypatch.setattr(server, "_run", _run_factory([]))     # 装不上，但 import 成功
    monkeypatch.setattr(server, "_uv_bin", lambda: "")
    assert server._deps_install(_py(tmp_path), tmp_path / "requirements.txt") == (True, "")


def test_refuses_when_import_still_fails(tmp_path, monkeypatch):
    """pip 说成功、import 不过（装错版本/装到别的环境）也不能放行。"""
    monkeypatch.setattr(server, "_run", _run_factory(["pip"], smoke_ok=False))
    monkeypatch.setattr(server, "_uv_bin", lambda: "")
    ok, why = server._deps_install(_py(tmp_path), tmp_path / "requirements.txt")
    assert not ok and "导入失败" in why


def test_missing_interpreter_is_a_clear_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_run", _run_factory(["pip"]))
    ok, why = server._deps_install(str(tmp_path / "nope"), tmp_path / "requirements.txt")
    assert not ok and "解释器" in why
