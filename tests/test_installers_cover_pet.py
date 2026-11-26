# SPDX-License-Identifier: AGPL-3.0-or-later

"""安装脚本必须显式拷 pet/（目录型模块）——2026-11-21 那台 Linux 永久起不来。

无源码包的程序文件是 *.pyc，安装脚本用通配符拷 .pyc 时 `pet/` 这种目录会被漏掉：
server.py 一 `import pet` 就 ModuleNotFoundError，服务起不来；进程死了自动更新循环
也跟着死，机器永久离线。版本号还是新的，从版本根本看不出来。
macOS 的 install.sh 11-20 修过，Linux / Windows 的安装脚本当时没跟上。
"""
import sys
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent / "host"
sys.path.insert(0, str(HOST))

import server


def test_purge_stale_src_removes_only_shadowed(tmp_path):
    """无源码包更新后，安装目录里同名的旧 .py 会压过新 .pyc（Python 优先源码）：
    机器「更新成功」却一直跑老代码——2026-11-23 新加坡那台 /api/brand 一直 500
    （老 branding.py 赖着不走，新版 branding.pyc 永远不被 import）。"""
    src = tmp_path / "pkg"
    (src / "pet").mkdir(parents=True)
    here = tmp_path / "here"
    (here / "pet").mkdir(parents=True)
    (src / "branding.pyc").write_bytes(b"x")
    (src / "pet" / "audio.pyc").write_bytes(b"x")
    (src / "server.py").write_text("shell")
    (here / "branding.py").write_text("old")
    (here / "pet" / "audio.py").write_text("old")
    (here / "server.py").write_text("old shell")   # 入口薄壳是包里的真源码，不能删
    (here / "custom.py").write_text("user file")   # 包里没有对应 pyc，不归我们管
    server._purge_stale_src(src, here)
    assert not (here / "branding.py").exists()
    assert not (here / "pet" / "audio.py").exists()
    assert (here / "server.py").exists()
    assert (here / "custom.py").exists()


def test_apply_wires_purge(tmp_path):
    """_apply_relay_pkg 覆盖完文件要真调 _purge_stale_src（自动更新从不跑 install.sh，
    清理逻辑必须长在更新路径上）。"""
    s = (HOST.parent / "host" / "server.py").read_text(encoding="utf-8")
    assert "_purge_stale_src(src" in s


def test_linux_installer_copies_pet_dir():
    s = (HOST / "install-linux.sh").read_text(encoding="utf-8")
    assert 'cp -R "$SRC/pet"' in s


def test_mac_installer_copies_pet_dir():
    s = (HOST / "install.sh").read_text(encoding="utf-8")
    assert 'cp -R "$SRC/pet"' in s


def test_windows_installer_copies_pet_dir():
    s = (HOST / "install.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $SRC "pet"' in s and "Copy-Item $petSrc" in s


def test_installers_purge_stale_py():
    """三个安装脚本在 pyc 分支都要清同名旧 .py（手动重装在老目录上时同样会中招）。"""
    for name, marker in (("install-linux.sh", "压过新 .pyc"),
                         ("install.sh", "压过新 .pyc"),
                         ("install.ps1", "Remove-Item $stalePy -Force")):
        s = (HOST / name).read_text(encoding="utf-8")
        assert marker in s, "%s 缺同名旧 .py 清理（%s）" % (name, marker)
