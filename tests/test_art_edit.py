# SPDX-License-Identifier: AGPL-3.0-or-later

"""产出物编辑端点（窗口核心 Phase 2 Task 3）：文本/大小双闸、乐观锁、越界拒绝。

跑法： .venv/bin/python -m pytest tests/test_art_edit.py -q
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import art_edit


# ---- (a) 文本判定：.py/.md/.json 放行，.png/无扩展名二进制拒绝 ----

def test_text_gate_by_ext(tmp_path):
    for name in ("a.py", "b.md", "c.json", "d.txt", "e.html"):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        assert art_edit.is_text_file(p), name
    assert not art_edit.is_text_file(tmp_path / "a.png")
    assert not art_edit.is_text_file(tmp_path / "a.mp4")


def test_text_gate_no_ext_by_content(tmp_path):
    # Makefile/Dockerfile 这类无扩展名文本要能用；无扩展名二进制（编译产物）拒绝
    mk = tmp_path / "Makefile"
    mk.write_text("all:\n\techo hi\n", encoding="utf-8")
    assert art_edit.is_text_file(mk)
    binf = tmp_path / "output"
    binf.write_bytes(b"\x00\x01\x02\xff\xfebinary")
    assert not art_edit.is_text_file(binf)


# ---- (b) 256KB 上限 ----

def test_load_text_size_gate(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("x" * (art_edit.TEXT_MAX + 1), encoding="utf-8")
    out = art_edit.load_text(big)
    assert out["ok"] is False and "256KB" in out["error"]
    small = tmp_path / "small.txt"
    small.write_text("hello", encoding="utf-8")
    out = art_edit.load_text(small)
    assert out["ok"] is True and out["text"] == "hello"
    assert out["size"] == 5 and out["hash"]


# ---- (c) save 生成合法 unified diff 与新全文两文件 ----

def test_save_writes_diff_and_new(tmp_path):
    f = tmp_path / "src" / "app.py"
    f.parent.mkdir()
    f.write_text("line1\nline2\n", encoding="utf-8")
    cur = art_edit.load_text(f)
    out = art_edit.save(str(tmp_path / "src"), "app.py", "line1\nline2 changed\n", cur["hash"])
    assert out["ok"] is True
    edits = tmp_path / "src" / ".boss" / "edits"
    diffs = sorted(edits.glob("*.diff"))
    news = sorted(edits.glob("*.new"))
    assert len(diffs) == 1 and len(news) == 1, "diff 和新全文各落一个文件"
    dtext = diffs[0].read_text(encoding="utf-8")
    assert dtext.startswith("---") and "@@" in dtext, "要是 unified diff"
    assert "-line2" in dtext and "+line2 changed" in dtext
    assert news[0].read_text(encoding="utf-8") == "line1\nline2 changed\n"
    assert out["ref_msg"] and len(out["ref_msg"]) <= 120
    assert ".boss/edits" in out["ref_dir"] or str(edits) in out["ref_msg"]


# ---- (d) hash 不符 → conflict ----

def test_save_conflict_on_stale_hash(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("old\n", encoding="utf-8")
    cur = art_edit.load_text(f)
    f.write_text("agent 改过了\n", encoding="utf-8")   # 手机打开后 agent 动了它
    out = art_edit.save(str(tmp_path), "app.py", "手机那份旧内容\n", cur["hash"])
    assert out["ok"] is False and out.get("conflict") is True


# ---- (e) rel 越出 cwd 拒绝（trust boundary）----

def test_save_rejects_escape(tmp_path):
    inside = tmp_path / "proj"
    inside.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("$$$\n", encoding="utf-8")
    out = art_edit.save(str(inside), "../secret.txt", "hacked\n", "whatever")
    assert out["ok"] is False and "越界" in out["error"]
    assert secret.read_text(encoding="utf-8") == "$$$\n", "越界路径不许被写"
    out2 = art_edit.save(str(inside), "/etc/passwd", "hacked\n", "whatever")
    assert out2["ok"] is False


# ---- 路由层：sid+rel → 会话 cwd，双闸 + 冲突走 409 ----

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


def _session(cwd):
    import asyncio

    async def _find(sid):
        return {"sid": sid, "cwd": cwd}
    return _find


def test_routes_roundtrip(tmp_path, monkeypatch):
    import server
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "note.md"
    f.write_text("# hi\n", encoding="utf-8")
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_find_session", _session(str(proj)))
    out = {}

    async def run():
        import asyncio
        r1 = await server.h_ext_art_text(_get("/api/ext_art_text?sid=s1&rel=note.md"))
        d1 = json.loads(r1.body)
        out["get"] = d1
        r2 = await server.h_ext_art_edit(_post("/api/ext_art_edit",
            {"sid": "s1", "rel": "note.md", "content": "# hi\n# 改的\n",
             "base_hash": d1["hash"]}))
        out["post"] = (r2.status, json.loads(r2.body))

    import asyncio
    asyncio.run(run())
    assert out["get"]["ok"] is True and out["get"]["text"] == "# hi\n"
    status, d2 = out["post"]
    assert status == 200 and d2["ok"] is True
    assert "ref_msg" in d2 and len(d2["ref_msg"]) <= 120
    assert (proj / ".boss" / "edits").is_dir()


def test_route_binary_rejected_and_conflict_409(tmp_path, monkeypatch):
    import server
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    f = proj / "app.py"
    f.write_text("old\n", encoding="utf-8")
    h = art_edit.load_text(f)["hash"]
    f.write_text("agent 改过\n", encoding="utf-8")
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_find_session", _session(str(proj)))
    out = {}

    async def run():
        r1 = await server.h_ext_art_text(_get("/api/ext_art_text?sid=s1&rel=pic.png"))
        out["get"] = json.loads(r1.body)
        r2 = await server.h_ext_art_edit(_post("/api/ext_art_edit",
            {"sid": "s1", "rel": "app.py", "content": "旧\n", "base_hash": h}))
        out["post"] = (r2.status, json.loads(r2.body))

    import asyncio
    asyncio.run(run())
    assert out["get"]["ok"] is False and out["get"]["error"]
    status, d = out["post"]
    assert status == 409 and d.get("conflict") is True
