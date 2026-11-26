# SPDX-License-Identifier: AGPL-3.0-or-later

"""ACP（Agent Client Protocol）stdio 内核（窗口核心 Phase 1 · Task 5）。

线上协议：NDJSON over stdio——每行一个 JSON-RPC 2.0 帧。request 带 id 关联
等待（Future 表），notification 只发不收，服务端 session/update 通知派给
on_update 回调。T6 的 gemini ACP 适配器和未来的多 agent 接入都挂在这层上，
不在各适配器里各写一份进程管理。

终止路径枚举（2026-11-18 全灭教训：每条终止路径都必须有人唤醒等待者，否则
手机端就是 60s 静默断流）：
  1. reader EOF            —— agent 退出/崩溃关了 stdout → _reader 的 finally 调 _gone
  2. 进程退出但 stdout 还开 —— 同一 reader task 结束（readline 返回 EOF）覆盖
  3. 写失败（BrokenPipe）  —— _writer 捕获后调 _gone，发送侧同时拒绝新请求
  4. close() 主动关        —— 先 _gone 再 SIGTERM→等 3s→SIGKILL，取消两个常驻 task
  _gone 是唯一收口：置 _dead、把所有在途 Future set_exception(AcpError)，
  之后所有 request() 立刻拒绝。绝不挂死等待者。

跑法（测试）： .venv/bin/python -m pytest tests/test_acp_core.py -q
"""
import asyncio
import json
import signal


class AcpError(Exception):
    """通道级错误：进程死了 / RPC 回了 error / 通道没起。"""


class AcpConn:
    def __init__(self, argv):
        self.argv = list(argv)
        self.proc = None                     # 测试要读 poll()，公开
        self._id = 0
        self._wait = {}                      # id -> Future
        self._cb = None
        self._rd = None
        self._wr = None
        self._wq = None
        self._dead = None                    # asyncio.Event；None = 未 start
        self._death_cb = None

    def on_update(self, cb):
        """服务端 session/update 通知回调，cb(method, params)。"""
        self._cb = cb

    def on_death(self, cb):
        """连接死亡（EOF/进程退出/写失败/close）时的同步回调，无参。
        窗口层用它把状态翻成 exited——崩溃要可见，不能等下一次操作才发现。"""
        self._death_cb = cb

    async def start(self):
        """spawn 子进程 + initialize 握手（返回服务端能力字典）。"""
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._wq = asyncio.Queue()
        self._dead = asyncio.Event()
        self._rd = asyncio.create_task(self._reader())
        self._wr = asyncio.create_task(self._writer())
        return await self.request(
            "initialize",
            {"protocolVersion": "1", "clientCapabilities": {}})

    async def request(self, method, params):
        """JSON-RPC request：按 id 关联等待，返回 result；死了/错了抛 AcpError。"""
        if self._dead is None:
            raise AcpError("conn not started")
        if self._dead.is_set():
            raise AcpError("agent process gone")
        self._id += 1
        fid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._wait[fid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": fid,
                              "method": method, "params": params})
            return await fut
        finally:
            self._wait.pop(fid, None)

    async def notify(self, method, params):
        """只发不等回复。"""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, frame):
        if self._dead.is_set():
            raise AcpError("agent process gone")
        await self._wq.put(json.dumps(frame, ensure_ascii=False) + "\n")

    async def _writer(self):
        try:
            while True:
                line = await self._wq.get()
                self.proc.stdin.write(line.encode("utf-8"))
                await self.proc.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._gone()               # 路径 3：BrokenPipe 等

    async def _reader(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break                    # 路径 1/2：EOF
                line = line.strip()
                if not line:
                    continue
                try:
                    f = json.loads(line)
                except ValueError:
                    continue                 # 坏行跳过，不连坐
                if f.get("id") is not None and ("result" in f or "error" in f):
                    fut = self._wait.get(f["id"])
                    if fut is not None and not fut.done():
                        if "error" in f:
                            fut.set_exception(
                                AcpError("rpc error: %s" % (f["error"],)))
                        else:
                            fut.set_result(f.get("result") or {})
                    continue
                if f.get("id") is not None and f.get("method"):
                    # agent→client 请求（如 session/request_permission）：Phase 1
                    # 没有权限 UI，但绝不静默吞——真适配器会一直等答复，整回合
                    # 挂死。回 -32601 让它立刻失败（可见，不是失踪）。
                    try:
                        await self._send({"jsonrpc": "2.0", "id": f["id"],
                                          "error": {"code": -32601,
                                                    "message": "unhandled method: %s"
                                                               % f.get("method")}})
                    except AcpError:
                        pass                 # 通道已死：_gone 已唤醒所有等待者
                    continue
                m = f.get("method") or ""
                if m == "session/update" and self._cb:
                    try:
                        self._cb(m, f.get("params") or {})
                    except Exception:
                        pass                 # 回调炸了不该带崩通道
        finally:
            await self._gone()

    async def _gone(self):
        """唯一终止收口（见模块 docstring 的路径枚举）。"""
        if self._dead is None or self._dead.is_set():
            return
        self._dead.set()
        for fut in self._wait.values():
            if not fut.done():
                fut.set_exception(AcpError("agent process gone"))
        self._wait.clear()
        if self._death_cb is not None:
            try:
                self._death_cb()
            except Exception:
                pass

    async def close(self):
        """路径 4：SIGTERM → 等 3s → SIGKILL。幂等。"""
        if self.proc is None:
            return
        await self._gone()
        try:
            self.proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            await self.proc.wait()
        for t in (self._rd, self._wr):
            if t is not None:
                t.cancel()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
