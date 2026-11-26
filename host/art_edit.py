# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""产出物文本编辑（窗口核心 Phase 2）：拉全文 + 乐观锁保存落 diff。

这里只有确定性闸门（文本/大小/越界/并发冲突）——CLAUDE.md 口径：判断不写死，
「改什么」交给会话里的 agent（diff/.new 落 .boss/edits/，短引用消息回流注入）。
"""
import difflib
import hashlib
import mimetypes
import os
import time
from pathlib import Path

TEXT_MAX = 256 * 1024          # 拉全文上限：手机编辑器不是 IDE

# 常见文本扩展白名单；白名单外的扩展按 mimetypes 判，不认识再看内容
TEXT_EXTS = frozenset({
    ".py", ".md", ".json", ".txt", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".csv", ".log", ".sh", ".bash", ".zsh", ".xml", ".svg", ".sql",
    ".env", ".gitignore", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".dart", ".lua", ".r", ".m", ".gradle",
})
_TEXT_OK_MIME = ("text/", "application/json", "application/javascript",
                 "application/xml", "application/yaml", "application/toml",
                 "application/x-sh", "image/svg+xml")


def _sniff_text(path, head=8192):
    """无扩展名/陌生扩展名：头 8KB 无 NUL 且严格 utf-8 可解 → 文本（Makefile/Dockerfile 这类）。"""
    try:
        with open(path, "rb") as f:
            b = f.read(head)
    except OSError:
        return False
    if not b:
        return True          # 空文件按文本算（编辑器里能补内容）
    if b"\x00" in b:
        return False
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def is_text_file(path):
    """编辑入口的文本判定：扩展白名单 → mime 已知非文本拒 → 内容嗅探兜底。"""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in TEXT_EXTS:
        return True
    if ext:
        mime = mimetypes.guess_type(p.name)[0]
        if mime and not mime.startswith(_TEXT_OK_MIME):
            return False
    return _sniff_text(p)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()[:16]


def load_text(path):
    """{ok, text, hash, size}；闸门不过 → {ok:False, error}（文案给手机用户看）。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"ok": False, "error": "文件不存在或已删除"}
    if not st.st_size:
        return {"ok": True, "text": "", "hash": _hash(b""), "size": 0}
    if st.st_size > TEXT_MAX:
        return {"ok": False, "error": "文件超过 256KB：手机编辑只适合小文件，大的去电脑上改"}
    if not is_text_file(p):
        return {"ok": False, "error": "不是文本文件：图片/压缩包这类去电脑上看"}
    try:
        raw = p.read_bytes()
    except OSError:
        return {"ok": False, "error": "读不到文件"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "不是 UTF-8 文本，可能带别的编码或二进制内容"}
    return {"ok": True, "text": text, "hash": _hash(raw), "size": st.st_size}


def save(cwd, rel, content, base_hash):
    """乐观锁保存：base_hash 与当前不符 → conflict（agent 改过，手机那份是旧的）。

    落 <cwd>/.boss/edits/<ts>-<name>.diff（unified diff）+ 同名 .new（新全文）；
    返回 {ok, ref_dir, ref_msg}——ref_msg ≤120 字，供会话注入。rel 越界一律拒绝。
    """
    try:
        root = Path(cwd).resolve()
        p = (root / rel).resolve()
    except (OSError, ValueError):
        return {"ok": False, "error": "路径不对"}
    if not str(p).startswith(str(root) + os.sep):
        return {"ok": False, "error": "越界：只许改会话目录里的文件"}
    try:
        cur = p.read_bytes()
    except OSError:
        return {"ok": False, "error": "文件不存在或已删除"}
    if _hash(cur) != str(base_hash or ""):
        return {"ok": False, "conflict": True,
                "error": "文件在你打开后被改过（可能 agent 刚动了它）。重新打开看最新内容再改。"}
    if not is_text_file(p):
        return {"ok": False, "error": "不是文本文件，不收编辑"}
    old_text = cur.decode("utf-8", "replace")
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), str(content).splitlines(keepends=True),
        fromfile="a/" + rel, tofile="b/" + rel))
    edits = root / ".boss" / "edits"
    try:
        edits.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        stem = "%s-%s" % (ts, p.name)
        (edits / (stem + ".diff")).write_text(diff, encoding="utf-8")
        (edits / (stem + ".new")).write_text(str(content), encoding="utf-8")
    except OSError:
        return {"ok": False, "error": "落盘失败（.boss/edits 写不进去）"}
    msg = "手机改了 %s：diff 在 .boss/edits/%s.diff，新全文在同名 .new，请按 diff 应用" % (rel, stem)
    return {"ok": True, "ref_dir": str(edits), "ref_msg": msg[:120]}
