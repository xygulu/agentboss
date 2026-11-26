# SPDX-License-Identifier: AGPL-3.0-or-later

"""2026-11-26 整站冻结事故：recent_dirs 在事件循环上 stat /Volumes 外置盘路径，
USB 盘休眠窗口里一次 is_dir() 卡进内核 → 事件循环冻 20 分钟 → relay 判死
（app 全离线）+ 全彩回放出不来。修复两层：

1. h_ext_sessions 的目录下拉必须离开事件循环（executor/to_thread）——盘再卡也只废
   一个线程池线程，不冻全站；
2. 循环看门狗抓栈不许再被「日志上报开关」锁住——本地排查也要有现场栈（节流防刷屏）。

跑法：.venv/bin/python -m pytest tests/test_recent_dirs_offloop.py -q
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


class _Req:
    def __init__(self):
        self.query = {}

    async def json(self):
        return {}

    async def text(self):
        return "{}"


def _no_token(monkeypatch):
    async def _ok(_r):
        return None
    monkeypatch.setattr(server, "require_token", _ok)


def test_ext_sessions_dirs_do_not_block_loop(monkeypatch):
    """目录探测卡住时，事件循环上的其他协程必须还能跑（= 探测不在循环上）。"""
    _no_token(monkeypatch)
    monkeypatch.setattr(server, "_tick_sessions", lambda: [])
    monkeypatch.setattr(server, "_face_only", lambda out, f: out)
    monkeypatch.setattr(server, "_face_for", lambda r: "")
    monkeypatch.setattr(server, "_agents_installed", lambda p: [])
    monkeypatch.setattr(server, "_jsonz", lambda d: d)

    calls = []

    def slow_dirs():
        time.sleep(0.4)          # 模拟外置盘休眠卡 syscall
        calls.append(1)
        return ["/tmp"]
    monkeypatch.setattr(server, "recent_dirs", slow_dirs)

    async def run():
        ticks = []
        t0 = asyncio.get_running_loop().time()

        async def ticker():
            for _ in range(60):
                ticks.append(asyncio.get_running_loop().time() - t0)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await server.h_ext_sessions(_Req())
        await t
        return ticks

    ticks = asyncio.run(run())
    assert calls == [1]
    # tick 序列上不允许出现 >0.3s 的空洞：同步跑在循环上时，0.4s 的阻塞会让
    # 相邻 tick 的时间戳跳开一大截；走 executor 则空洞只有毫秒级
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert ticks and max(gaps) < 0.3, gaps


def test_recent_dirs_skips_unprobed_volumes(monkeypatch):
    """未授权的外置盘候选必须被跳过、且不许重试轰炸；内盘候选照常返回。"""
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="rdirs_test"))   # 路径不能带连字符（解码按 - 拆）
    try:
        monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: base))
        proj = base / ".claude" / "projects"
        proj.mkdir(parents=True)
        internal = base / "myproj"
        internal.mkdir()
        (proj / ("-" + str(internal).replace("/", "-"))).touch()   # 解码回真实路径
        (proj / "-Volumes----1T-aiboss").touch()

        probes = []
        monkeypatch.setattr(server, "_vol_ok",
                            lambda root, wait_s=0.5: (probes.append(root) or root == ""))
        monkeypatch.setattr(server, "_vol_root",
                            lambda cand: "bad-vol" if cand.startswith("/Volumes") else "")

        got = server.recent_dirs()
        assert len(got) == 1 and Path(got[0]).resolve() == internal.resolve()
        assert "bad-vol" not in [p for p in got]   # 外置盘候选被剔除
        assert "bad-vol" in probes                 # 外置盘确实探测过（下次走缓存不再戳）
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_vol_probe_throttles_retries(monkeypatch):
    """探测失败的卷 10 分钟内不许再戳（防 TCC 弹窗轰炸）。"""
    server._VOL_PROBE.clear()
    monkeypatch.setattr(server.time, "time", lambda: 1000.0)
    tries = []

    def slow_probe(root):
        tries.append(1)
        time.sleep(5)          # 模拟 TCC 弹窗挂起
        return True
    monkeypatch.setattr(server, "_vol_probe_once", slow_probe)

    assert server._vol_ok("/Volumes/X", wait_s=0.05) is False   # 超时 → 不可用
    assert server._vol_ok("/Volumes/X", wait_s=0.05) is False   # 节流窗口内直接跳过
    assert len(tries) == 1


def test_git_root_skips_blocked_volume(monkeypatch):
    """cwd 在被卡住的卷上时 _git_root 必须快速返回空，不许 stat 卷路径。
    （2026-11-26 二次冻结：ext_sessions 每个会话都走 _proj_key→_git_root→isdir(卷)）"""
    monkeypatch.setattr(server, "_vol_ok", lambda root, wait_s=0.5: root == "")
    real_isdir, real_exists = server.os.path.isdir, server.os.path.exists

    class Sentinel(BaseException):
        pass

    def boom(p, _r=real_isdir, _e=real_exists):
        if str(p).startswith("/Volumes/USB1"):   # 只盯假卷路径，别误伤 pytest 自己
            raise Sentinel("卡住的卷不许碰: %s" % p)   # BaseException：逃出 except Exception
        return _r(p)
    monkeypatch.setattr(server.os.path, "isdir", boom)
    monkeypatch.setattr(server.os.path, "exists", boom)

    assert server._git_root("/Volumes/USB1/aiboss") == ""       # 卡住的卷：0 次触碰
    assert server._git_root("/tmp") != ""                       # 内盘照常向上找


def test_watchdog_not_swallowed_by_stall_dump():
    """2026-11-26 三审：插 _loop_stall_dump 时把 _loop_watch 留在 4 空格缩进，
    被吞进函数体 return 之后的死代码——看门狗线程从此不存在（进程里连
    time.sleep 线程都没有），冻结时一个栈都抓不到。用 AST 钉死层级。"""
    import ast
    tree = ast.parse((HERE / "host" / "server.py").read_text(encoding="utf-8"))
    assigns = [a for a in ast.walk(tree) if isinstance(a, ast.Assign)
               and any(isinstance(t, ast.Subscript)
                       and isinstance(t.slice, ast.Constant)
                       and t.slice.value == "loop_watch_task" for t in a.targets)]
    assert assigns, "loop_watch_task 的 create_task 赋值丢了"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_loop_stall_dump":
            ids = {id(x) for x in assigns}
            for sub in ast.walk(node):
                assert id(sub) not in ids, "create_task 被吞进 _loop_stall_dump 死代码"
            assert not any(isinstance(s, ast.AsyncFunctionDef) and s.name == "_loop_watch"
                           for s in ast.walk(node)), "_loop_watch 被吞进 _loop_stall_dump"


def test_vol_probe_uses_scandir(monkeypatch):
    """探测必须试 opendir/readdir 类操作：TCC 挂起时 stat/isdir 能过、scandir 才挂。"""
    class _It:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(())
    seen = []

    def fake_scandir(p):
        seen.append(p)
        return _It()
    monkeypatch.setattr(server.os, "scandir", fake_scandir)
    assert server._vol_probe_once("/Volumes/X") is True
    assert seen == ["/Volumes/X"]

    def boom(p):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(server.os, "scandir", boom)
    assert server._vol_probe_once("/Volumes/X") is False   # 探测报错=不可用，不炸调用方


def test_guard_path_gates_blocked_volume(monkeypatch):
    """会话 cwd 的卷被闸门挡住时，产物页/文件端点必须立刻报错，不许 resolve/scandir 上卷。"""
    from server import _ExtPathError
    monkeypatch.setattr(server, "_vol_ok",
                        lambda root, wait_s=0.5: server._vol_root(root) == "")
    real_resolve = server.Path.resolve

    class Sentinel(BaseException):
        pass

    def boom(self, *a, **k):
        if str(self).startswith("/Volumes/USB1"):   # 只盯假卷路径，别误伤 pytest 自己
            raise Sentinel("闸门关着不许碰: %s" % self)
        return real_resolve(self, *a, **k)
    monkeypatch.setattr(server.Path, "resolve", boom)

    with pytest.raises(_ExtPathError):
        server._ext_guard_path("/Volumes/USB1/proj", "")
    with pytest.raises(_ExtPathError):
        server._ext_guard_path("/Volumes/USB1/proj", "src")
    assert server._ext_guard_path("/tmp", "") is not None   # 内盘照常


def test_proj_facts_never_bypasses_gate(monkeypatch):
    """_git_root 被闸门挡成 "" 时，不许用裸 cwd 旁路回去扫目录（第 3 次冻结的引信）。"""
    calls = []

    def boom(p):
        calls.append(p)
        return True
    monkeypatch.setattr(server, "_git_root", lambda cwd: "")
    monkeypatch.setattr(server.os.path, "isdir", boom)
    assert server._proj_facts("/Volumes/USB1/proj", "proj") is None
    assert calls == []


def test_artifacts_scan_off_loop(monkeypatch):
    """第 4 次冻结定罪处：artifact_inbox→_ext_artifacts_payload→walk→scandir 曾直接
    跑在循环线程上。扫描卡住时循环必须照常跳（=扫描在专用池里）。"""
    _no_token(monkeypatch)

    async def fake_find(sid):
        return {"sid": sid, "cwd": "/tmp", "started": 0}
    monkeypatch.setattr(server, "_find_session", fake_find)

    def slow_scandir(p):
        time.sleep(0.4)          # 模拟 TCC 弹窗挂起
        return iter(())
    monkeypatch.setattr(server.os, "scandir", slow_scandir)

    async def run():
        ticks = []
        t0 = asyncio.get_running_loop().time()

        async def ticker():
            for _ in range(60):
                ticks.append(asyncio.get_running_loop().time() - t0)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        d = await server._ext_artifacts_payload("x")
        await t
        return ticks, d

    ticks, d = asyncio.run(run())
    assert d["cwd"] and d["entries"] == []
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert ticks and max(gaps) < 0.3, gaps   # 循环上不允许出现扫描卡出来的空洞


def test_ext_fs_run_timeout_fails_fast(monkeypatch):
    """探测通过≠担保：挂死的扫描必须 5s 内变干净错误 + 拆该卷的放行缓存，
    不许让页面永久转圈（历史消息白屏就是这么来的）。"""
    server._VOL_PROBE.clear()
    server._VOL_PROBE["/Volumes/USB1"] = -1.0   # 之前探测「通过」

    async def run():
        return await server._ext_fs_run(lambda: time.sleep(0.3),
                                        vol="/Volumes/USB1/proj", timeout=0.05)

    from server import _ExtPathError
    with pytest.raises(_ExtPathError):
        asyncio.run(run())
    assert server._VOL_PROBE.get("/Volumes/USB1") not in (-1.0, None)   # 整卷记坏，下次快败


def test_guard_probes_real_dir_not_volume_root(monkeypatch):
    """卷根能列、项目子目录照样挂死（TCC 实测粒度）——探测必须打在实际目录上。"""
    seen = []
    monkeypatch.setattr(server, "_vol_ok", lambda root, wait_s=0.5: seen.append(root) or False)
    from server import _ExtPathError
    with pytest.raises(_ExtPathError):
        server._ext_guard_path("/Volumes/USB1/proj", "")
    assert seen == ["/Volumes/USB1/proj"]        # 探的是 cwd 本尊，不是卷根


def test_inbox_scans_at_most_30_sessions(monkeypatch):
    """收件箱不许全量扫：107 个会话逐个扫盘 = 30 秒白屏（2026-11-26 定罪）。
    只看最近 30 个，页面先渲染，其余交给下次刷新。"""
    calls = []

    async def fake_payload(sid, timeout=2.0):
        calls.append(sid)
        return {"cwd": "/tmp", "since": 0, "entries": [{"name": "a", "mtime": 1}],
                "truncated": False}
    monkeypatch.setattr(server, "_ext_artifacts_payload", fake_payload)
    sessions = [{"sid": "s%d" % i, "title": "", "agent": ""} for i in range(60)]

    out, partial = asyncio.run(server._artifact_inbox_payload(sessions))
    assert len(calls) == 30 and partial is False
    assert len(out) == 30


def test_inbox_budget_stops_and_marks_partial(monkeypatch):
    """3 秒预算到点必须交卷并标 partial——挂死的会话不许拖住整页（历史消息
    白屏就是这么来的：收件箱一个请求挂 8-15s，页面等它等出白屏）。"""
    tick = [1000.0]

    def fake_time():
        tick[0] += 1.0
        return tick[0]
    monkeypatch.setattr(server.time, "time", fake_time)
    calls = []

    async def fake_payload(sid, timeout=2.0):
        calls.append(1)
        return {"cwd": "/tmp", "since": 0, "entries": [], "truncated": False}
    monkeypatch.setattr(server, "_ext_artifacts_payload", fake_payload)
    sessions = [{"sid": "s%d" % i, "title": "", "agent": ""} for i in range(100)]

    out, partial = asyncio.run(server._artifact_inbox_payload(sessions))
    assert partial is True
    assert len(calls) <= 4       # 假时钟每次 +1s，预算 3s 早就到了


def test_inbox_bounds_each_scan_to_remaining_budget(monkeypatch):
    """预算检查在扫描前≠总时长有界：2.9s 时开始的慢扫描照样跑满 2s 池超时
    （实测收件箱 5.1s）。剩余预算必须传成单次扫描的超时。"""
    captured = []

    async def fake_payload(sid, timeout=2.0):
        captured.append(timeout)
        return {"cwd": "/tmp", "since": 0, "entries": [], "truncated": False}
    monkeypatch.setattr(server, "_ext_artifacts_payload", fake_payload)
    tick = [1000.0]

    def fake_time():
        tick[0] += 1.4          # 每次循环耗 1.4s
        return tick[0]
    monkeypatch.setattr(server.time, "time", fake_time)
    sessions = [{"sid": "s%d" % i, "title": "", "agent": ""} for i in range(10)]

    out, partial = asyncio.run(server._artifact_inbox_payload(sessions))
    assert partial is True
    assert len(captured) == 2                    # 第 3 轮预算耗尽
    assert captured[0] < 2.0                     # 剩余预算 1.6s，不是固定 2.0
    assert captured[1] == pytest.approx(0.3)     # 极小剩余取 0.3s 下限


def test_loop_watch_dumps_stack_without_logs_upload(monkeypatch, capsys):
    """整站冻结时看门狗必须抓主线程栈进 server.log——不再等「后台开日志上报」才抓。"""
    monkeypatch.setitem(server._LOGS_ON, "on", False)   # 上报关闭也要抓栈

    beat = {"t": 0.0}            # 心跳停留在 0 = 冻了很久
    dumped = []

    def fake_dump():
        dumped.append(1)
        print("[loop] 卡顿抓栈", flush=True)

    server._loop_stall_dump(beat, now=99.0, threshold=1.5,
                            min_gap=0.0, dump=fake_dump)
    assert dumped == [1]         # 上报关着也必须抓

    server._loop_stall_dump(beat, now=100.0, threshold=1.5,
                            min_gap=0.0, dump=fake_dump)
    assert len(dumped) == 2

    server._loop_stall_dump(beat, now=101.0, threshold=1.5,
                            min_gap=30.0, dump=fake_dump)
    assert len(dumped) == 2      # 节流窗口内不重复刷

    server._loop_stall_dump(beat, now=200.0, threshold=1.5,
                            min_gap=30.0, dump=fake_dump)
    assert len(dumped) == 3      # 节流过期后再冻结照抓
