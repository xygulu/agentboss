# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent CLI 的定位不能靠宿主进程的 PATH（2026-11-20）。

实测：launchd 起的 host 进程 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin。
opencode / hermes 的 installed() 走 shutil.which(裸名) → 找不到 → 手机端
那两个 tab 直接消失（Claude/Codex 有自己的绝对路径定位所以还在）。
定位用户装的 CLI 一律走 plat.which：进程 PATH + 常见安装位。

跑法： .venv/bin/python -m pytest tests/test_agent_bin_resolution.py -q
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "host"))
sys.path.insert(0, str(HERE / "shared"))

import plat

HOST = HERE / "host"


def test_ext_path_covers_user_bins():
    parts = plat._EXT_PATH.split(":")
    assert str(Path.home() / ".npm-global/bin") in parts
    assert str(Path.home() / ".local/bin") in parts, "hermes 装在 ~/.local/bin"


def test_which_ignores_daemon_path(monkeypatch, tmp_path):
    """进程 PATH 是 launchd 的最小集合时，也按 plat 的搜索路径找。"""
    fake = tmp_path / "fakecli"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(plat, "_EXT_PATH", str(tmp_path) + ":" + plat._EXT_PATH)
    assert plat.which("fakecli") == str(fake)


def test_adapters_use_plat_which():
    for mod in ("agent_opencode.py", "agent_hermes.py"):
        src = (HOST / mod).read_text(encoding="utf-8")
        i = src.find("def _bin():")
        assert i >= 0, "%s 没有 _bin()" % mod
        body = src[i:i + 200]
        assert "plat.which(" in body, "%s 的 _bin() 还在用裸 shutil.which（launchd 下找不到）" % mod
