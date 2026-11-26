# SPDX-License-Identifier: AGPL-3.0-or-later

"""产出物收件箱（批次三 Task 11）：GET /api/ext_files?artifacts=1。

老板打开会话先看到「这次产出的东西」——递归 2 层、只出会话开始之后的新文件、
隐藏项与 node_modules/dist/.git/__pycache__ 不进、mtime 倒序、上限 500。

跑法： .venv/bin/python -m pytest tests/test_ext_artifacts.py -q
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

SINCE = time.time() - 60          # 会话一分钟前开始


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    async def _fs(sid):
        return {"cwd": str(tmp_path), "started": SINCE} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    return tmp_path


def _touch(p, age=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def _arts(sid="s1"):
    return asyncio.run(server._ext_artifacts_payload(sid))


def test_only_new_files(cwd):
    _touch(cwd / "old.png", age=3600)          # 会话之前就有：不算产出
    _touch(cwd / "new.png", age=10)
    names = [e["name"] for e in _arts()["entries"]]
    assert names == ["new.png"]


def test_recurses_into_doc_dirs_and_skips_noise(cwd):
    """设计文档在 docs/superpowers/specs/x-design.md（第三层）：以前只扫两层，
    agent 写了一整份设计文档、产出物却是空的（2026-11-20 用户在 ESP32-C3 会话上踩到）。
    噪音目录（node_modules/.git/.boss-uploads/隐藏项）照旧不进。"""
    _touch(cwd / "out" / "a.png", age=5)
    _touch(cwd / "out" / "deep" / "b.png", age=5)       # 第 3 层：现在要
    _touch(cwd / "docs" / "superpowers" / "specs" / "2026-11-20-desk-foreman-design.md", age=5)
    _touch(cwd / "node_modules" / "x.png", age=5)
    _touch(cwd / "node_modules" / "pkg" / "deep" / "y.png", age=5)
    _touch(cwd / ".git" / "y.png", age=5)
    _touch(cwd / ".boss-uploads" / "z.png", age=5)
    _touch(cwd / ".hidden.png", age=5)
    names = sorted(e["name"] for e in _arts()["entries"])
    assert names == ["2026-11-20-desk-foreman-design.md", "a.png", "b.png"]
    rels = {e["name"]: e["rel"] for e in _arts()["entries"]}
    assert rels["2026-11-20-desk-foreman-design.md"] == \
        "docs/superpowers/specs/2026-11-20-desk-foreman-design.md"


def test_sorted_by_mtime_desc(cwd):
    _touch(cwd / "a.png", age=50)
    _touch(cwd / "b.png", age=10)
    _touch(cwd / "c.png", age=30)
    assert [e["name"] for e in _arts()["entries"]] == ["b.png", "c.png", "a.png"]


def test_caps_at_500(cwd):
    for i in range(520):
        _touch(cwd / ("f%03d.png" % i), age=1)
    d = _arts()
    assert len(d["entries"]) == 500
    assert d["truncated"] is True


def test_media_endpoint_accepts_relative_artifact_path(tmp_path, monkeypatch):
    """产出物列表给的是相对路径（out/render.png）。/api/ext_file 以前拿它
    按 host 进程 cwd 解析，别的项目里就 404——图片预览打不开，只剩「用其他
    应用打开」（原生按会话 cwd 找，所以是好的）。相对路径要按会话 cwd 解析。"""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "render.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 200)
    monkeypatch.setattr(server, "_media_roots", lambda sid: [str(tmp_path)])
    monkeypatch.setattr(server, "_sess_cwd_of", lambda sid: str(tmp_path))

    async def _t(request):
        pass
    monkeypatch.setattr(server, "require_token", _t)
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/api/ext_file?p=out/render.png&sid=s1")
    r = asyncio.run(server.h_ext_file(req))
    assert r.status == 200
    assert "image/png" in r.headers.get("Content-Type", "")


def test_unknown_sid_raises(cwd):
    with pytest.raises(server._ExtPathError):
        _arts("nope")


def test_missing_started_uses_transcript_birth_time(tmp_path, monkeypatch):
    """发现出来的会话没有 started（适配器只给 sid/cwd）。以前回落目录 mtime：
    每放进一个新文件目录 mtime 就刷新，之前的产出物全部消失——用户反馈
    「本次会话产出物有时候有，有时候没有」。改用转录文件的创建时间。"""
    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    tr = tmp_path / ".claude" / "t.jsonl"      # 真转录在 ~/.claude/projects，不在 cwd
    tr.parent.mkdir()
    tr.write_text("{}\n")
    monkeypatch.setattr(server, "_born_at", lambda p: SINCE if str(p) == str(tr) else 0.0)

    class _Ad:
        def transcript_path(self, cwd, sid):
            return tr
    monkeypatch.setattr(server.agents, "adapter_of", lambda sid, cwd="": _Ad())

    _touch(tmp_path / "old.png", age=3600)
    _touch(tmp_path / "new.png", age=10)
    assert [e["name"] for e in _arts()["entries"]] == ["new.png"]


def test_missing_started_falls_back_to_dir_birth_not_mtime(tmp_path, monkeypatch):
    """连转录都找不到时用目录的创建时间——目录 mtime 会随新文件跳动，不能用。"""
    async def _fs(sid):
        return {"cwd": str(tmp_path)} if sid == "s1" else None
    monkeypatch.setattr(server, "_find_session", _fs)
    monkeypatch.setattr(server, "_born_at", lambda p: SINCE if str(p) == str(tmp_path) else 0.0)
    monkeypatch.setattr(server.agents, "adapter_of", lambda sid, cwd="": None)

    _touch(tmp_path / "old.png", age=3600)
    _touch(tmp_path / "new.png", age=10)
    assert [e["name"] for e in _arts()["entries"]] == ["new.png"]


def test_media_mime_and_size_cap():
    assert server._EXT_MIME[".mp4"] == "video/mp4"
    assert server._EXT_MIME[".docx"]
    assert server._EXT_FILE_MAX >= 100 * 1024 * 1024


def test_stl_artifact_kind_and_mime():
    """3D 打印的 .stl 手机上没应用能开：列表里要认成 model（出专门的图标），
    预览层拿它走自绘查看器。"""
    assert server._ext_kind("part.stl") == "model"
    assert server._EXT_MIME[".stl"] == "model/stl"


def test_html_artifact_is_page():
    """产出物里的 .html 是「网页」，不是文本：列表出 🌐，预览按真页面渲染。"""
    assert server._ext_kind("index.html") == "page"
    assert server._ext_kind("x.HTM") == "page"


def test_page_preview_opens_pvfile():
    """网页预览（电脑上的页面在 App 里也能看）。"""
    _node_script("test_fv_page.mjs")


def test_doc_cards_and_review_buttons():
    """工头递文档：会话里提到的文档出卡、点开即看、看完就地确认/提建议回会话。"""
    _node_script("test_doc_cards.mjs")


def _get(qs, sid="s1"):
    """走真实 handler：?artifacts=1 要切到收件箱口径，不是普通文件列表。"""
    from aiohttp.test_utils import make_mocked_request
    import json

    async def _token(request):
        pass
    import server as _s
    _s_require = _s.require_token
    _s.require_token = _token
    try:
        req = make_mocked_request("GET", "/api/ext_files?sid=" + sid + qs)
        r = asyncio.run(server.h_ext_files(req))
        return json.loads(r.text)
    finally:
        _s.require_token = _s_require


def test_handler_routes_artifacts_flag(cwd):
    _touch(cwd / "old.png", age=3600)
    _touch(cwd / "new.png", age=10)
    d = _get("&artifacts=1")
    assert [e["name"] for e in d["entries"]] == ["new.png"]
    assert d["entries"][0]["kind"] == "image"


def test_handler_default_stays_plain_listing(cwd):
    _touch(cwd / "old.png", age=3600)
    d = _get("")
    assert [e["name"] for e in d["entries"]] == ["old.png"]
    assert "is_dir" in d["entries"][0]


def _node_script(name):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 渲染门禁")
    r = subprocess.run([node, str(HERE / "tests" / name)], capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]


def test_crop_and_quote_logic():
    """圈选几何与终端引用（批次三 Task 13）。"""
    _node_script("test_crop_quote.mjs")


def test_source_preview_opens_overlay():
    """产出物里点 .py 要有反应：code 分支也得把 #fview 打开（2026-11-20 反馈）。"""
    _node_script("test_fv_code_open.mjs")


def test_text_preview_uses_authenticated_path():
    """打开 JSON/MD 显示 bad token 的回归门禁（缓存参数 t 与 host token 撞名）。"""
    _node_script("test_fv_text.mjs")


def test_stl_viewer_parse_and_crop():
    """3D 模型预览：STL 解析 + 转完视角仍能圈选（截 WebGL 当前帧）。"""
    _node_script("test_stl.mjs")


def test_media_preview_kind_logic():
    """预览分类（批次三 Task 12）：视频不能被当成图片塞进 <img>。"""
    _node_script("test_fv_media.mjs")


def test_grid_render_logic():
    """网格渲染逻辑（typescript 无关，纯 node 断言；没 node 就跳）。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过网格渲染门禁")
    r = subprocess.run([node, str(HERE / "tests" / "test_art_grid.mjs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]
