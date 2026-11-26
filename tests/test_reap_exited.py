# SPDX-License-Identifier: AGPL-3.0-or-later

"""已结束的终端要自动关掉，内存才收得回来（2026-11-19 用户要求）。

两块：
1) 回滚行数够快照用就行——pyte 回滚是满宽字符网格，行数直接是每会话几十 MB；
2) 进程退出后：先砍回滚，过了宽限期再整个摘掉（屏幕/输出缓冲跟着回收）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyte
import server


class _S:
    """够 _reap_exited 用的最小 Session 替身。"""

    def __init__(self, alive=True, exited_at=0.0, ghost=False):
        self.alive = alive
        self.exited_at = exited_at
        self.last_out = exited_at
        self.clients = set()
        self.ghost = ghost


def test_hist_lines_cover_snapshot_but_not_much_more():
    # 快照要拿最后 SNAP_HIST_MAX 行，回滚少了会截断；多了全是白占内存
    assert server.HIST_LINES >= server.SNAP_HIST_MAX
    assert server.HIST_LINES <= server.SNAP_HIST_MAX * 2


def test_trim_history_keeps_only_tail():
    scr = pyte.HistoryScreen(100, 32, history=server.HIST_LINES)
    stream = server._CleanStream(scr)
    for i in range(server.HIST_LINES + 300):
        stream.feed(("行%d\r\n" % i).encode())
    assert len(scr.history.top) <= server.HIST_LINES
    before = len(scr.history.top)
    server._trim_history(scr, 50)
    assert len(scr.history.top) == min(before, 50)
    # 留下的是最近的，不是最早的（喂了 N 行，最后一行就是 行N-1）
    last = "".join(c.data for c in scr.history.top[-1].values()).strip()
    assert last.startswith("行"), last
    assert int(last[1:]) > server.HIST_LINES, "尾巴被砍错了，留下的是老内容"


def test_reap_exited_removes_dead_sessions_only():
    now = time.time()
    server.SESSIONS.clear()
    server.SESSIONS["dead-old"] = _S(alive=False, exited_at=now - server.EXITED_KEEP_SEC - 60)
    server.SESSIONS["dead-new"] = _S(alive=False, exited_at=now - 5)
    server.SESSIONS["live"] = _S(alive=True)
    server.SESSIONS["ghost"] = _S(alive=False, ghost=True)
    watched = _S(alive=False, exited_at=now - server.EXITED_KEEP_SEC - 60)
    watched.clients.add(object())
    server.SESSIONS["watched"] = watched
    saved = []
    orig = server.save_registry
    server.save_registry = lambda: saved.append(1)
    try:
        dead = server._reap_exited(now)
    finally:
        server.save_registry = orig
        server.SESSIONS.clear()
    assert dead == ["dead-old"], dead
    assert saved == [1], "摘掉会话后要落盘，免得重启又冒出来"
