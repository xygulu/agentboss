# SPDX-License-Identifier: AGPL-3.0-or-later

"""菜单栏 App 更新完要真的重新起来。

2026-11-20：App 更新后打「客户端已更新，重启」就没了。原因是在自己还活着的时候
`open <dest>`——LaunchServices 按 bundle id 认为这是「激活已有实例」，不新起，
紧接着旧进程 terminate，结果一个都不剩。修法：等自己真退出后再 `open -n`。
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "clients/mac/main.swift"


def _fn(name: str) -> str:
    s = SRC.read_text(encoding="utf-8")
    i = s.index("func " + name)
    j = s.find("\n    func ", i + 1)
    if j < 0:
        j = len(s)
    return s[i:j]


def test_relaunch_waits_for_exit_then_opens_new_instance():
    b = _fn("relaunchAfterExit")
    assert "kill -0" in b, "必须等旧进程退出——退出前 open 只会激活旧实例"
    assert "/usr/bin/open -n" in b, "要用 open -n 强制新实例"
    assert "NSApp.terminate" in b


def test_update_uses_relaunch_not_bare_open():
    b = _fn("updateApp")
    assert "relaunchAfterExit(" in b
    # 旧写法 `open dest` 紧跟 terminate 会吞掉重启
    assert not re.search(r'runCmd\(\["/usr/bin/open", dest\]\)', b)
