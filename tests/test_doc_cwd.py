# SPDX-License-Identifier: AGPL-3.0-or-later

"""会话页文档/产物卡片按「实时 cwd」解析路径，agent 在会话里 cd 之后实时目录
漂走，早先写的相对路径（相对出生目录）全变成「不是文件」403——而文件页
（ext_files 自己列目录再解析）自洽，照样能开（2026-11-25 用户实测，这个
会话 cd 进 host/ 后所有卡片都打不开）。

修法：ext_file_meta 解析失败时换「转录出生 cwd」（_transcript_meta 的头部
cwd，不随 cd 漂、自带 mtime 缓存）再试一次；两个时代写的路径都能开。

跑法： .venv/bin/python -m pytest tests/test_doc_cwd.py -q
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pytest

import server


class _StubAD:
    """假适配器：转录文件第一行写着出生 cwd。"""

    def __init__(self, proj, transcript):
        self.proj = str(proj)
        self.transcript = str(transcript)

    def transcript_path(self, cwd, sid):
        return self.transcript

    def transcript_meta(self, p):
        return (self.proj, "标题")


@pytest.fixture()
def drifted(tmp_path, monkeypatch):
    """出生目录 proj，实时 cwd 漂进 proj/sub：proj/docs/a.md 只有出生基座能解。"""
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "docs" / "a.md").write_text("# hi", encoding="utf-8")
    (proj / ".env").write_text("TOKEN=x", encoding="utf-8")
    (proj / "sub").mkdir()
    (proj / "sub" / "new.md").write_text("new", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tr = tmp_path / "tr.jsonl"
    import json as _j
    tr.write_text(_j.dumps({"cwd": str(proj), "type": "user"}) + "\n", encoding="utf-8")
    ad = _StubAD(proj, tr)
    monkeypatch.setattr(server.agents, "get", lambda k: ad)
    monkeypatch.setattr(server.agents, "default", lambda: ad)
    s = {"sid": "doc-cwd-1", "cwd": str(proj / "sub"), "agent": "claude"}

    async def _f(sid):
        return s
    monkeypatch.setattr(server, "_find_session", _f)
    monkeypatch.setattr(server, "_tick_sessions", lambda: [s])
    return s, proj, outside


def _meta(sid, path):
    return asyncio.run(server._ext_file_meta(sid, path))


def test_resolves_at_birth_cwd_when_live_drifted(drifted):
    """漂移后的会话：相对出生目录写的路径要能开（消息早于 cd 的场景）。"""
    _, proj, _ = drifted
    p, mime = _meta("doc-cwd-1", "docs/a.md")
    assert p == proj / "docs" / "a.md"
    assert "markdown" in mime


def test_live_cwd_still_first(drifted):
    """实时 cwd 优先：cd 之后新写的文件按新目录解（消息晚于 cd 的场景）。"""
    _, proj, _ = drifted
    p, _mime = _meta("doc-cwd-1", "new.md")
    assert p == proj / "sub" / "new.md"


def test_birth_base_still_guarded(drifted):
    """出生基座不豁免安全边界：越界和凭据名照样拒。"""
    _, _proj, outside = drifted
    with pytest.raises(server._ExtPathError):
        _meta("doc-cwd-1", "../../outside.txt")
    with pytest.raises(server._ExtPathError):
        _meta("doc-cwd-1", ".env")
    assert not outside.exists() or True


def test_code_payload_resolves_at_birth_cwd(drifted):
    """代码卡（ext_code）同一类漂移：早写的源码路径按出生目录解。"""
    _, proj, _ = drifted
    d = asyncio.run(server._ext_code_payload("doc-cwd-1", "docs/a.md", 1))
    assert d["total"] == 1
    assert d["lines"][0]["text"] == "# hi"


def test_media_roots_include_birth_cwd(drifted):
    """媒体白名单要认出生目录：不然漂移后早先产出的图/视频全 403。"""
    _, proj, _ = drifted
    assert str(proj) in server._media_roots("doc-cwd-1")
