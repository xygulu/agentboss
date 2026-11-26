# SPDX-License-Identifier: AGPL-3.0-or-later

"""static/ext.html 必须是由 static/src/ 拼出来的那一份。

防止两种常见事故：改了生成物没改源码（下次构建就丢）、改了源码没重新构建
（手机上还是旧页面，本地测试却全绿）。

跑法：
    .venv/bin/python -m pytest tests/test_page_build.py -q
    python tools/build_page.py      # 修：重新生成
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tools"))

import pytest  # noqa: E402

import build_page  # noqa: E402


def test_page_is_built_from_sources():
    assert build_page.OUT.read_text(encoding="utf-8") == build_page.build(), (
        "static/ext.html 跟 static/src/ 对不上：改前端请改 static/src/，"
        "再跑 python tools/build_page.py")


def _node_check(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 语法门禁")
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def test_source_js_parses():
    """static/src/js/ 每个文件必须能过 node --check。"""
    for js in sorted((HERE / "host" / "static" / "src" / "js").glob("*.js")):
        ok, err = _node_check(js)
        assert ok, "%s 语法错误：%s" % (js.name, err[:300])


def test_built_page_js_parses():
    """构建产物 ext.html 里的内联脚本必须能过语法检查。

    评审 C1 的教训：生成物规则测试只比对文本，一个少写的 `}` 让整页 JS 全死
    还全绿。这里把产物的内联 <script> 块逐个喂给 node --check。"""
    html = build_page.OUT.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "ext.html 里没找到内联脚本？"
    for i, b in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(b)
            tmp = Path(f.name)
        try:
            ok, err = _node_check(tmp)
            assert ok, "ext.html 内联脚本 #%d 语法错误：%s" % (i, err[:300])
        finally:
            tmp.unlink(missing_ok=True)


def test_shell_page_has_no_machine_specific_dirs():
    """整页 HTML 不能嵌开发机目录（2026-11-18）：嵌了每台机器页面都不一样，中继
    就没法跨设备共用一份缓存，切到慢链路机器要重拉 611KB。目录改由
    /api/ext_sessions 的 dirs 带回（见 server.h_ext_sessions）。"""
    html = build_page.OUT.read_text(encoding="utf-8")
    assert 'list="dirs"' in html
    assert re.search(r'id="n_cwd"[^>]*value=""', html), "n_cwd 不该再预填本机路径"
    assert '<datalist id="dirs"></datalist>' in html, "datalist 不该再嵌本机目录"


def test_ext_sessions_carries_dirs():
    """/api/ext_sessions 要带 dirs，页面靠它填目录下拉。"""
    import asyncio
    import json
    from unittest import mock

    import server

    async def _noop(_request):
        return None

    async def _items(_request):
        return []

    req = type("R", (), {"headers": {}, "cookies": {}, "query": {}})()
    with mock.patch.object(server, "require_token", _noop), \
            mock.patch.object(server, "_ext_sessions_items", _items):
        resp = asyncio.run(server.h_ext_sessions(req))
    assert isinstance(json.loads(resp.body).get("dirs"), list)
