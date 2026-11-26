# SPDX-License-Identifier: AGPL-3.0-or-later

"""批次一 Task 4/5：产物目录列表与安全下载。

安全边界：只允许会话 cwd 子树内路径（resolve 后前缀校验 + 符号链接拒收），
凭据类文件拒发，下载 20MB 上限。跑法： .venv/bin/python -m pytest tests/test_ext_files.py -q
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pytest

import server


@pytest.fixture()
def sess(tmp_path):
    """一个假已退出会话：cwd 指向 tmp 目录，里面放产物/凭据/外部链接。"""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "report.md").write_text("# hello", encoding="utf-8")
    (cwd / "chart.png").write_bytes(b"\x89PNG fake")
    (cwd / ".env").write_text("TOKEN=x", encoding="utf-8")
    (cwd / "sub").mkdir()
    (cwd / "sub" / "a.txt").write_text("a", encoding="utf-8")
    link = tmp_path / "outside.txt"
    link.write_text("secret-outside", encoding="utf-8")
    os.symlink(link, cwd / "evil.lnk")
    return {"sid": "20261117_114131_ec1f02", "cwd": str(cwd)}, cwd


def _find_stub(entry):
    async def _f(sid):
        return entry
    return _f


# ---- Task 4: 目录列表 ----

def test_ext_files_lists_cwd(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    d = asyncio.run(server._ext_files_payload(entry["sid"]))
    names = {e["name"] for e in d["entries"]}
    assert {"report.md", "chart.png", "sub"} <= names
    assert d["entries"][0].keys() >= {"name", "size", "mtime", "is_dir"}


def test_ext_files_traversal_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_files_payload(entry["sid"], sub="../../etc"))


def test_ext_files_symlink_escape_not_listed(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    d = asyncio.run(server._ext_files_payload(entry["sid"]))
    assert "evil.lnk" not in {e["name"] for e in d["entries"]}


def test_ext_files_unknown_sid(sess, monkeypatch):
    async def _none(sid):
        return None
    monkeypatch.setattr(server, "_find_session", _none)
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_files_payload("nosuch"))


# ---- Task 5: 下载 ----

def test_ext_file_reads_within_cwd(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    p, mime = asyncio.run(server._ext_file_meta(entry["sid"], "report.md"))
    assert p.read_bytes() == b"# hello"
    assert mime in ("text/markdown", "text/plain; charset=utf-8")


def test_ext_file_png_mime(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    _, mime = asyncio.run(server._ext_file_meta(entry["sid"], "chart.png"))
    assert mime == "image/png"


def test_ext_file_traversal_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_file_meta(entry["sid"], "../../outside.txt"))


def test_ext_file_absolute_path_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_file_meta(entry["sid"], "/etc/passwd"))


def test_ext_file_symlink_escape_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_file_meta(entry["sid"], "evil.lnk"))


def test_ext_file_credential_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    for name in (".env", "token.txt", "my-secret.json", "id_rsa", "srv.pem", "a.key"):
        (cwd / name).write_text("x", encoding="utf-8")
        with pytest.raises(server._ExtPathError):
            asyncio.run(server._ext_file_meta(entry["sid"], name))


def test_ext_file_oversize_denied(sess, monkeypatch):
    entry, cwd = sess
    monkeypatch.setattr(server, "_find_session", _find_stub(entry))
    (cwd / "big.bin").write_bytes(b"\0")
    import stat as _stat
    # 上限随批次三抬到 100MB（产出物视频）：按常量取超出一字节，别写死数字
    fake = type("S", (), {"st_size": server._EXT_FILE_MAX + 1,
                          "st_mode": _stat.S_IFREG | 0o644})()
    monkeypatch.setattr(Path, "stat", lambda self, **k: fake)
    with pytest.raises(server._ExtPathError):
        asyncio.run(server._ext_file_meta(entry["sid"], "big.bin"))
