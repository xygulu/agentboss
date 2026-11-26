# SPDX-License-Identifier: AGPL-3.0-or-later

"""Linux 命令行客户端的名字是 bosshost（老名字 codeboss 留作兼容别名）。

改名是产品口径：电脑端叫 BossHost，Linux 终端里输入的也是 bosshost。
装的时候要同时建两个软链，卸载时要都清掉——只改文档不改安装脚本，
用户照文档输入 bosshost 会 command not found。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = ROOT / "host"


def test_install_links_bosshost_and_keeps_codeboss_alias():
    s = (HOST / "install-linux.sh").read_text(encoding="utf-8")
    assert 'ln -sf "$HERE/codeboss-cli" "$CLI_DIR/bosshost"' in s
    assert 'ln -sf "$HERE/codeboss-cli" "$CLI_DIR/codeboss"' in s


def test_cli_usage_speaks_bosshost():
    s = (HOST / "codeboss-cli").read_text(encoding="utf-8")
    assert "bosshost update" in s
    assert "\n  codeboss " not in s  # 帮助里不该再出现旧命令名


def test_uninstall_removes_both_names():
    s = (HOST / "uninstall-linux.sh").read_text(encoding="utf-8")
    assert "for _n in bosshost codeboss; do" in s
