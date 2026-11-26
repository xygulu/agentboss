# SPDX-License-Identifier: AGPL-3.0-or-later

"""产出物收件箱（B1，代理工头默认视图）：跨会话把新产出收在一处。

- 每条带 sid/title/agent（点开要能回到任务、预览要能在对的会话里取文件）；
- 按 mtime 倒序、总量有上限；
- 单个会话读失败不许拖垮整页。

跑法：.venv/bin/python -m pytest tests/test_artifact_inbox.py -q
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server

SINCE = time.time() - 60


def _touch(p, age=0, data="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture
def two(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _touch(a / "old.png", age=3600)        # 会话开始前就有：不算产出
    _touch(a / "r1.png", age=30)
    _touch(b / "r2.mp4", age=10)
    sess = {"s1": {"cwd": str(a), "started": SINCE}, "s2": {"cwd": str(b), "started": SINCE}}

    async def _fs(sid):
        return sess.get(sid)
    monkeypatch.setattr(server, "_find_session", _fs)
    return [
        {"sid": "s1", "title": "做图任务", "agent": "claude", "cwd": str(a)},
        {"sid": "s2", "title": "剪视频", "agent": "hermes", "cwd": str(b)},
    ]


def test_inbox_aggregates_with_owner(two):
    items, partial = asyncio.run(server._artifact_inbox_payload(two))
    names = [x["name"] for x in items]
    assert names == ["r2.mp4", "r1.png"]          # mtime 倒序，会话开始前的不算
    assert partial is False
    by = {x["name"]: x for x in items}
    assert by["r2.mp4"]["sid"] == "s2" and by["r2.mp4"]["title"] == "剪视频"
    assert by["r2.mp4"]["agent"] == "hermes"
    assert by["r1.png"]["sid"] == "s1"


def test_inbox_limit(two):
    items, _partial = asyncio.run(server._artifact_inbox_payload(two, limit=1))
    assert len(items) == 1 and items[0]["name"] == "r2.mp4"


def test_broken_session_does_not_break_inbox(two, monkeypatch):
    real = server._ext_artifacts_payload

    async def flaky(sid, sub="", timeout=2.0):
        if sid == "s2":
            raise server._ExtPathError("会话不存在或没有 cwd")
        return await real(sid, sub, timeout=timeout)
    monkeypatch.setattr(server, "_ext_artifacts_payload", flaky)
    items, _partial = asyncio.run(server._artifact_inbox_payload(two))
    assert [x["name"] for x in items] == ["r1.png"]
