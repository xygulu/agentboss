# SPDX-License-Identifier: AGPL-3.0-or-later

"""批次一 Task 9：拍照派活上传端点（/api/ext_upload）。

评审 I1 的教训：Task 9 只测了 h_ext_files/h_ext_file 的辅助函数，上传端点
零测试。这里走真实 handler（make_mocked_request + 真 multipart 字节流），
钉住：白名单、10MB 上限、未知 sid、无 file 字段。

跑法： .venv/bin/python -m pytest tests/test_ext_upload.py -q
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

import server

B = "cbtestboundary"


def _body(filename, content, field="file"):
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
            % (B, field, filename)).encode()
    return head + content + ("\r\n--%s--\r\n" % B).encode()


class _FakeProto:
    """StreamReader 的 protocol 只在背压/唤醒时被摸到；喂完就 eof，全部 no-op。"""

    def __getattr__(self, k):
        return lambda *a, **kw: None


def _post(body, entry):
    async def _go():
        # aiohttp 的 multipart 解析要它自己的 StreamReader（readline 带 max_line_length），
        # 标准库 asyncio.StreamReader 会 TypeError（评审复现实测）
        sr = StreamReader(_FakeProto(), 2 ** 16)
        sr.feed_data(body)
        sr.feed_eof()
        req = make_mocked_request(
            "POST", "/api/ext_upload?sid=" + entry["sid"],
            headers={"Content-Type": "multipart/form-data; boundary=" + B},
            payload=sr)
        return await server.h_ext_upload(req)

    return asyncio.run(_go())


@pytest.fixture()
def sess(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()

    async def _noop(request):
        pass

    async def _find(sid):
        return {"sid": sid, "cwd": str(cwd)} if sid == "s1" else None

    monkeypatch.setattr(server, "require_token", _noop)
    monkeypatch.setattr(server, "_find_session", _find)
    return cwd


def test_upload_ok(sess):
    r = _post(_body("shot.png", b"\x89PNG fake"), {"sid": "s1"})
    assert r.status == 200
    d = __import__("json").loads(r.text)
    assert d["ok"] is True and d["path"].startswith(".boss-uploads/")
    files = list((sess / ".boss-uploads").iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"\x89PNG fake"
    assert files[0].suffix == ".png"  # 服务端命名，不用客户端文件名


def test_upload_bad_ext_403(sess):
    r = _post(_body("evil.sh", b"#!/bin/sh"), {"sid": "s1"})
    assert r.status == 403
    # 目录本身会在解析前 mkdir（无害），但不能留下任何写盘文件
    up = sess / ".boss-uploads"
    assert not up.exists() or not list(up.iterdir())


def test_upload_over_limit_413_and_cleaned(sess, monkeypatch):
    monkeypatch.setattr(server, "_EXT_UPLOAD_MAX", 8)
    r = _post(_body("big.png", b"x" * 64), {"sid": "s1"})
    assert r.status == 413
    assert not list((sess / ".boss-uploads").iterdir())  # 半成品清掉


def test_upload_unknown_sid_404(sess):
    r = _post(_body("a.png", b"x"), {"sid": "nosuch"})
    assert r.status == 404


def test_upload_no_file_field_400(sess):
    r = _post(_body("a.png", b"x", field="other"), {"sid": "s1"})
    assert r.status == 400
