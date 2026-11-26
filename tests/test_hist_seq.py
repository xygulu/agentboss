# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口核心 Phase 1 · Task 1：镜像滚动历史的连续性游标 hist_seq。

重连续接要有「事实依据」可对：手机端记住上次看到的 seq，回连后拉到的 seq 比它小
= 服务器重启过（历史被重建），必须整窗重画；比它大 = 正常增量可接续。
游标语义 = _diff_commit 实际入账 hist 的累计行数：从空建起恒等于 len(st["hist"])。

跑法： .venv/bin/python -m pytest tests/test_hist_seq.py -q
"""
import asyncio
import json

from aiohttp.test_utils import make_mocked_request

import server


def _feed(sid, log, append):
    """往 wrapper 日志追加一批行并喂进镜像；返回喂完的 st。"""
    with open(log, "ab") as f:
        f.write(append)
    return server._mirror_feed_inner(sid, {"log": str(log), "pid": 0,
                                           "cols": 80, "rows": 24})


def _lines(tag, n):
    return ("\r\n".join("%s-%03d" % (tag, i) for i in range(n))).encode() + b"\r\n"


def test_seq_tracks_hist_appends(tmp_path):
    """两批滚行各提交一次 hist → seq 逐步累加，且恒等于 len(hist)。
    （简报原文「恰好 +2」与 Interfaces 的 `+= len(appended)` 冲突，按 Interfaces
    行数语义收口——T3/T6 消费的是缺口大小，不是提交次数。）"""
    sid = "sX-seq"
    server._MIRROR.pop(sid, None)
    log = tmp_path / "w.log"
    log.write_bytes(_lines("fill", 24))          # 铺满屏：建基线，不滚
    st = _feed(sid, log, b"")                    # 喂空批：仅建态+基线
    assert st.get("seq", 0) == 0
    assert not st.get("hist")
    st = _feed(sid, log, _lines("new", 6))       # 滚出 6 行 → 提交 6
    assert st["seq"] == 6 == len(st["hist"])
    st = _feed(sid, log, _lines("more", 6))      # 再滚 6 行 → 提交 6
    assert st["seq"] == 12 == len(st["hist"])
    server._MIRROR.pop(sid, None)


def test_ext_live_payload_carries_seq(tmp_path, monkeypatch):
    """h_ext_live 的 payload 必须带 "seq" 且等于当前游标——手机侧只认这个字段。"""
    sid = "sL-seq"
    server._MIRROR.pop(sid, None)
    log = tmp_path / "w.log"
    log.write_bytes(b"")
    # 分两步喂（一步到位会把基线建在已滚完的屏上，本来就不产生提交）：
    # 先铺满建基线，再滚 6 行 → seq=6
    _feed(sid, log, _lines("fill", 24))
    _feed(sid, log, _lines("new", 6))

    async def _noop(request):
        pass

    async def _find(s):
        return {"sid": s, "cwd": str(tmp_path), "pid": 4242} if s == sid else None

    monkeypatch.setattr(server, "require_token", _noop)
    monkeypatch.setattr(server, "_find_session", _find)
    monkeypatch.setattr(server, "_meta_for_pid",
                        lambda pid: ({"log": str(log), "pid": pid,
                                      "cols": 80, "rows": 24}
                                     if pid == 4242 else None))

    async def _go():
        req = make_mocked_request("GET", "/api/ext_live/" + sid)
        req.match_info["sid"] = sid
        return await server.h_ext_live(req)

    r = asyncio.run(_go())
    assert r.status == 200
    d = json.loads(r.text)
    assert d["ok"] == 1
    assert d["seq"] == 6 == len(server._MIRROR[sid]["hist"])
    server._MIRROR.pop(sid, None)
