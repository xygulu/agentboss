# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口核心 Phase 1 · Task 2：滚动历史持久仓（logs/scrollback/）。

st["hist"] 是 row-dict 列表（{x: pyte.Char}），持久化必须保结构（颜色/加粗），
JSON 行格式一行一 row。与 logs/ansi 严格分离——那边是 wrapper 回放流
（claude 启动依赖，断链会崩），这边只存 _diff_commit 入账的窗口历史。

跑法： .venv/bin/python -m pytest tests/test_scrollback.py -q
"""
import os
import time

import pyte.screens
import pytest

import scrollback
import server


def _row(text):
    return {i: pyte.screens.Char(ch) for i, ch in enumerate(text)}


def _plain(row):
    return "".join(c.data for _, c in sorted(row.items()))


def test_roundtrip_caps_to_800(tmp_path, monkeypatch):
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    scrollback.save("s1", [_row("line-%03d" % i) for i in range(900)])
    out = scrollback.load("s1")
    assert len(out) == 800                       # 与 MIRROR_HIST_MAX 同值
    assert _plain(out[0]) == "line-100"          # 超限从最老头部丢
    assert _plain(out[-1]) == "line-899"


def test_roundtrip_keeps_char_attributes(tmp_path, monkeypatch):
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    row = {0: pyte.screens.Char("红", fg="#ff0000", bg="#111111", bold=True), 1: pyte.screens.Char("字")}
    scrollback.save("s2", [row])
    out = scrollback.load("s2")
    assert len(out) == 1
    c = out[0][0]
    assert (c.data, c.fg, c.bg, c.bold) == ("红", "#ff0000", "#111111", True)


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    assert scrollback.load("nope") == []


def test_load_skips_corrupt_lines(tmp_path, monkeypatch):
    """坏行跳过不连坐——磁盘半截/手改都不该把整个历史吞了。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    (tmp_path / "scrollback_bad.txt").write_text(
        "{not json\n\n[[0, [\"x\"]]]\n\"string not row\"\n", encoding="utf-8")
    out = scrollback.load("bad")
    assert len(out) == 1 and _plain(out[0]) == "x"


def test_save_atomic_no_partial_file(tmp_path, monkeypatch):
    """中途炸（模拟磁盘满）：正式文件保持旧内容，.part 不留尸。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    scrollback.save("s1", [_row("a"), _row("b")])

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        scrollback.save("s1", [_row("c")])
    out = scrollback.load("s1")
    assert [_plain(r) for r in out] == ["a", "b"]
    assert not list(tmp_path.glob("*.part"))


def test_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    scrollback.save("s1", [_row("x")])
    scrollback.delete("s1")
    assert scrollback.load("s1") == []
    scrollback.delete("s1")                       # 幂等，不抛


def test_gc_by_age_and_keep(tmp_path, monkeypatch):
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    for sid in ("old", "kept-old", "new"):
        scrollback.save(sid, [_row(sid)])
    t = time.time() - 20 * 86400
    os.utime(tmp_path / "scrollback_old.txt", (t, t))
    os.utime(tmp_path / "scrollback_kept-old.txt", (t, t))
    scrollback.gc({"kept-old"}, max_age_s=14 * 86400)
    assert not (tmp_path / "scrollback_old.txt").exists()
    assert (tmp_path / "scrollback_kept-old.txt").exists()
    assert (tmp_path / "scrollback_new.txt").exists()


# ---------- 接线：_diff_commit 防抖保存 + _mirror_get 重建播种 ----------

def _feed2(sid, log, append):
    with open(log, "ab") as f:
        f.write(append)
    return server._mirror_feed_inner(sid, {"log": str(log), "pid": 0,
                                           "cols": 80, "rows": 24})


def _lines2(tag, n):
    return ("\r\n".join("%s-%03d" % (tag, i) for i in range(n))).encode() + b"\r\n"


def _fill_with_marker(marker):
    """24 行铺屏批次，第 6 行是手机注入的消息（它会随后续滚行进入 hist）。"""
    rows = ["fill-%03d" % i for i in range(24)]
    rows[5] = marker
    return ("\r\n".join(rows)).encode() + b"\r\n"


def _sb_rows(sid):
    return ["".join(c.data for _, c in sorted(r.items())) for r in (server._MIRROR[sid].get("hist") or [])]


def test_diff_commit_saves_with_debounce(tmp_path, monkeypatch):
    """攒够 16 行触发落盘：_diff_commit 的防抖保存接线。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    sid = "sW-sb1"
    server._MIRROR.pop(sid, None)
    log = tmp_path / "w.log"
    log.write_bytes(_fill_with_marker("phone-injected-msg"))
    _feed2(sid, log, b"")
    _feed2(sid, log, _lines2("new", 8))          # 8 行：不够防抖阈值
    assert not scrollback.load(sid), "8 行不该触发写盘"
    _feed2(sid, log, _lines2("more", 8))          # 累计 16 行：触发
    saved = scrollback.load(sid)
    assert len(saved) == 16
    assert _plain(saved[0]).startswith("fill-")   # 滚出屏的旧行
    server._MIRROR.pop(sid, None)


def test_rebuild_after_restart_merges_persisted(tmp_path, monkeypatch):
    """杀 host 重启（清 _MIRROR）：持久化旧行还在、不与日志重放重复、
    注入的消息仍可见（T3 之鉴「注入消息终端隐形」负验收）。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    sid = "sW-sb2"
    server._MIRROR.pop(sid, None)
    log = tmp_path / "w.log"
    log.write_bytes(_fill_with_marker("phone-injected-msg"))
    _feed2(sid, log, b"")
    _feed2(sid, log, _lines2("new", 8))
    _feed2(sid, log, _lines2("more", 8))          # 攒满 16 触发保存，marker 已滚入 hist
    before = _sb_rows(sid)
    assert any("phone-injected-msg" in t for t in before)

    server._MIRROR.pop(sid, None)                 # 模拟 host 重启
    _feed2(sid, log, b"")                         # 重建：播种 + 整日志重放
    after = _sb_rows(sid)
    assert any("phone-injected-msg" in t for t in after)     # 注入行仍可见
    assert len(after) == 16                                   # 无重复（重放被去重）
    assert sum(1 for t in after if t.startswith("fill-006")) == 1
    server._MIRROR.pop(sid, None)


def test_rebuild_survives_log_gc(tmp_path, monkeypatch):
    """更狠的场景：logs/ansi 被 7 天 GC 清了（日志没了），滚动历史仍从持久仓回来。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    sid = "sW-sb3"
    server._MIRROR.pop(sid, None)
    log = tmp_path / "w.log"
    log.write_bytes(_fill_with_marker("old-visible-row"))
    _feed2(sid, log, b"")
    _feed2(sid, log, _lines2("new", 8))
    _feed2(sid, log, _lines2("more", 8))          # 保存触发
    assert scrollback.load(sid)

    log.write_bytes(b"")                          # 日志被 GC：只剩持久仓
    server._MIRROR.pop(sid, None)
    _feed2(sid, log, b"")
    after = _sb_rows(sid)
    assert len(after) == 16
    assert any("old-visible-row" in t for t in after)
    server._MIRROR.pop(sid, None)


# ---------- 登记表往返（现状零覆盖，规格验收「重启后窗口列表还在」） ----------

class _FakeSess:
    def __init__(self, sid, started):
        self.sid, self.name, self.cwd = sid, "n", "/tmp"
        self.cmd = ["claude", "--session-id", sid]
        self.started, self.alive, self.exit_code = started, True, None
        self.cols, self.rows = 80, 24


def test_registry_roundtrip_ghost_and_respawn(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REG_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(server, "SESSIONS", {})
    now = time.time()
    server.SESSIONS["fresh"] = _FakeSess("fresh", now - 60)
    server.SESSIONS["stale"] = _FakeSess("stale", now - 25 * 3600)   # 超 24h 不恢复
    server.save_registry()
    assert (tmp_path / "sessions.json").exists()

    monkeypatch.setattr(server, "SESSIONS", {})                    # 模拟重启
    n = server.load_registry()
    assert n == 1 and "fresh" in server.SESSIONS
    g = server.SESSIONS["fresh"]
    assert g.alive is False and getattr(g, "ghost", False) is True
    g.kill()                                                        # 无操作，不抛
    cmd = server._respawn_cmd(g.cmd)
    assert cmd[:2] == ["claude", "--resume"]                        # --session-id → --resume


def test_seed_prefix_participates_in_dedup(tmp_path, monkeypatch):
    """（评审 Important #2）重建去重窗口盖不住种子前段：40 行滑窗外的种子行
    会作为重放重复入账、还被防抖落盘——每次重建混进几行，复利腐蚀这个仓。
    直接钉 _diff_commit 的契约：提交行只要作为连续段出现在种子前缀里就丢弃。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    sid = "sW-sb4"
    server._MIRROR.pop(sid, None)
    # 构造一个播种了 100 行的镜像状态（种子行内容唯一，提交副本要撞的是第 5~7 行）
    hist_rows = ["seed old-%03d %s" % (i, "o" * 30) for i in range(100)]
    st = _mk_mirror_st(sid, hist_rows)
    server._MIRROR[sid] = st

    # 屏 A（建基线）：顶部三行恰好是种子第 5~7 行的副本（重放早期日志的形态）
    screen_a = ["seed old-%03d %s" % (i, "o" * 30) for i in (5, 6, 7)] \
        + ["scrA-%02d" % i for i in range(21)]
    st["strm"].feed(("\r\n".join(screen_a)).encode())          # 末行不带换行：屏幕不滚
    server._diff_commit(st)                 # 基线入账
    # 屏 B（滚 3 行）：a00..a20 上移，b0..b2 进底部 → 提交行 = 屏 A 顶部三行种子副本
    st["strm"].feed(b"\r\nb0\r\nb1\r\nb2")
    server._diff_commit(st)

    rows = _sb_rows(sid)
    assert len(rows) == 100, "种子前段的重复提交要被去重：实际 %d 行" % len(rows)
    assert sum(1 for t in rows if t.startswith("seed old-005")) == 1, "种子行不许重复入账"
    server._MIRROR.pop(sid, None)


def _mk_mirror_st(sid, hist_rows):
    """手工构造 _mirror_get 形状的镜像状态（不走路径，直接控制种子）。"""
    import pyte as _pyte
    scr = _pyte.HistoryScreen(80, 24, history=1000)
    return {"scr": scr, "strm": server._CleanStream(scr), "off": 0, "log": None,
            "win": (80, 24), "t": time.time(), "sid": sid,
            "hist": [_row(r) for r in hist_rows],
            "_sb_seed_n": len(hist_rows), "_sb_new": 0,
            "_sb_at": time.time(), "seq": 0}


def _feed_screen(st, lines):
    """清屏重画一屏（模拟一次全量重绘帧后到达的屏幕状态）。"""
    st["strm"].feed(b"\\x1b[2J\\x1b[H")
    st["strm"].feed(("\\r\\n".join(lines)).encode() + b"\\r\\n")
