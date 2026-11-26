# SPDX-License-Identifier: AGPL-3.0-or-later

"""命令跟着会话项目走（2026-11-19 用户口径：不在产品线配置里写死 npm）。

- package.json scripts 里有 lint/test/build/deploy/deploy:rollback 才有对应入口；
- 没有 package.json 的 Python 项目看 pyproject（pytest/ruff）和 Makefile 目标；
- 什么都没有 = 空（前端连按钮都不显示）。

跑法：.venv/bin/python -m pytest tests/test_proj_cmds.py -q
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


def _pkg(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    return str(tmp_path)


def test_npm_scripts(tmp_path):
    c = server._proj_cmds(_pkg(tmp_path, {
        "lint": "eslint .", "test": "jest", "build": "vite build",
        "deploy": "gh-pages -d dist", "deploy:rollback": "vercel rollback"}))
    assert c == {"lint": "npm run lint", "test": "npm run test", "build": "npm run build",
                 "deploy": "npm run deploy", "rollback": "npm run deploy:rollback"}


def test_partial_scripts(tmp_path):
    assert server._proj_cmds(_pkg(tmp_path, {"test": "jest"})) == {"test": "npm run test"}


def _venv_tool(tmp_path, name):
    b = tmp_path / ".venv" / "bin"
    b.mkdir(parents=True, exist_ok=True)
    (b / name).write_text("#!/bin/sh\n")
    (b / name).chmod(0o755)


def test_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n[tool.ruff]\nline-length = 100\n")
    c = server._proj_cmds(str(tmp_path))
    assert c == {"test": "python3 -m pytest -q", "lint": "ruff check ."}


def test_pyproject_prefers_project_venv(tmp_path):
    """host 进程的 PATH 里没有 pytest：项目带 .venv 就跑它自己的。"""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n")
    _venv_tool(tmp_path, "pytest")
    _venv_tool(tmp_path, "ruff")
    c = server._proj_cmds(str(tmp_path))
    assert c == {"test": ".venv/bin/pytest -q", "lint": ".venv/bin/ruff check ."}


def test_venv_pytest_without_pyproject(tmp_path):
    """本仓就是这种：没有 pyproject，只有 .venv/bin/pytest + tests/。"""
    (tmp_path / "tests").mkdir()
    _venv_tool(tmp_path, "pytest")
    assert server._proj_cmds(str(tmp_path))["test"] == ".venv/bin/pytest -q"


def test_venv_tool_for_command_whitelist(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    _venv_tool(tmp_path, "pytest")
    cmd = server._proj_cmds(str(tmp_path))["test"]
    assert server._test_cmd_allowed(cmd), cmd


def test_makefile(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest\nlint:\n\truff check .\n")
    c = server._proj_cmds(str(tmp_path))
    assert c == {"test": "make test", "lint": "make lint"}


def test_makefile_target_must_start_line(tmp_path):
    (tmp_path / "Makefile").write_text("all: build\n\t@echo build\n")
    assert "build" not in server._proj_cmds(str(tmp_path))


def test_empty_dir(tmp_path):
    assert server._proj_cmds(str(tmp_path)) == {}


def test_broken_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert server._proj_cmds(str(tmp_path)) == {}


def test_every_command_passes_whitelist(tmp_path):
    c = server._proj_cmds(_pkg(tmp_path, {"lint": "x", "test": "x", "build": "x"}))
    assert set(c) == {"lint", "test", "build"}
    for v in c.values():
        assert server._test_cmd_allowed(v), v
