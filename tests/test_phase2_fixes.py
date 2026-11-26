# SPDX-License-Identifier: AGPL-3.0-or-later

"""Phase 2 评审修复轮：每个 Important 发现一个失败测试。

1. _about_kick 在线程里也能排上概括任务（to_thread 后 get_running_loop 死了）
2. _ext_art_file 按出生 cwd 找文件 + GET/POST 同根（cd 漂移的产出物可编辑）
3. risk_review._codex_bin 复用 agent_codex.codex_bin（npm-global / Codex.app 机器）

跑法： .venv/bin/python -m pytest tests/test_phase2_fixes.py -q
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


class _FakeProto:
    def __getattr__(self, k):
        return lambda *a, **kw: None


def _post(path, obj):
    from aiohttp.streams import StreamReader
    from aiohttp.test_utils import make_mocked_request
    sr = StreamReader(_FakeProto(), 2 ** 16)
    sr.feed_data(json.dumps(obj).encode())
    sr.feed_eof()
    return make_mocked_request("POST", path, payload=sr)


def _get(path):
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request("GET", path)


async def _ok(request):
    return None


# ---- 修复 1：_about_kick 从工作线程调用也要把概括任务排上 ----

def test_about_kick_works_off_loop(monkeypatch):
    import server
    calls = []

    async def fake_llm(k, pt, title, maxn):
        calls.append(k)

    monkeypatch.setattr(server, "_about_llm", fake_llm)
    monkeypatch.setattr(server, "_voice_ok", lambda: True)
    monkeypatch.setattr(server, "_about_pref",
                        lambda: {"on": True, "llm": True, "max": 12})
    server._ABOUT.clear()
    server._ABOUT_BUSY.clear()
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    try:
        monkeypatch.setattr(server, "_MAIN_LOOP", loop, raising=False)
        # 本线程（无 running loop）直调 = 模拟 to_thread 工作线程的处境
        server._about_kick("s1", "跑测试", "标题")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(2)
        assert calls, "工作线程里 _about_kick 也得把概括任务排上（现在静默丢了）"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)
        loop.close()
        server._ABOUT_BUSY.clear()


# ---- 修复 3：产出物按出生 cwd 找到也能编辑，且 GET/POST 同根 ----

def test_art_edit_follows_birth_cwd(tmp_path, monkeypatch):
    import server
    live = tmp_path / "live"
    birth = tmp_path / "birth"
    live.mkdir()
    birth.mkdir()
    (birth / "note.md").write_text("# in birth\n", encoding="utf-8")

    async def _find(sid):
        return {"sid": sid, "cwd": str(live)}

    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_find_session", _find)
    monkeypatch.setattr(server, "_birth_cwd_of", lambda sid, s=None: str(birth))
    out = {}

    async def run():
        r1 = await server.h_ext_art_text(_get("/api/ext_art_text?sid=s1&rel=note.md"))
        d1 = json.loads(r1.body)
        out["get"] = d1
        if d1.get("ok"):
            r2 = await server.h_ext_art_edit(_post("/api/ext_art_edit",
                {"sid": "s1", "rel": "note.md", "content": "# in birth\n# 改\n",
                 "base_hash": d1["hash"]}))
            out["post"] = json.loads(r2.body)

    asyncio.run(run())
    assert out["get"].get("ok") is True, "出生目录里的文件要认领（cd 漂移场景）"
    assert out["get"]["text"] == "# in birth\n"
    assert out["post"].get("ok") is True, "保存也要落在同一个根"
    assert (birth / ".boss" / "edits").is_dir(), "diff 要落在出生目录的 .boss/edits"
    assert not (live / ".boss").exists(), "保存不许落到另一个根"


# ---- 修复 4：codex 发现复用 agent_codex（npm-global / Codex.app 机器上二审才活着）----

def test_codex_bin_delegates_to_agent_codex(monkeypatch):
    import agent_codex
    import risk_review
    monkeypatch.setattr(agent_codex, "codex_bin", lambda: "/fake/codex")
    assert risk_review._codex_bin() == "/fake/codex"
    monkeypatch.setattr(agent_codex, "codex_bin", lambda: None)
    assert risk_review._codex_bin() == ""
