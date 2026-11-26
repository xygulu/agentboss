# SPDX-License-Identifier: AGPL-3.0-or-later

"""发布必须带上 Windows 安装包——2026-11-21 官网写 0.9.78、下载下来是 0.9.38。

Windows 安装包以前是手工打的，跟 host 版本各走各的：官网/下载页跟着 VERSION 走，
exe 停在最后一次手动打包的版本。现在 publish-host.sh 发布时用刚打好的 host 包
现打现传（同一个 VERSION），打不出来直接非零退出，不再静默发个对不上的成品。
"""
from pathlib import Path

PUB = Path(__file__).resolve().parent.parent / "host" / "publish-host.sh"


def test_publish_builds_exe_from_the_package_it_just_built():
    s = PUB.read_text(encoding="utf-8")
    # 用同一个 $TMP/pkg.tgz 打 exe：版本号来自 VERSION，天然对齐
    assert 'build-installer.sh" "$TMP/pkg.tgz"' in s
    assert 'BossHost-Setup.exe' in s


def test_publish_uploads_exe_to_distpkg():
    s = PUB.read_text(encoding="utf-8")
    assert "/r/admin/distpkg?name=BossHost-Setup.exe" in s


def test_publish_fails_loudly_without_makensis():
    s = PUB.read_text(encoding="utf-8")
    i = s.index("command -v makensis")
    tail = s[i:i + 400]
    assert "exit 1" in tail
