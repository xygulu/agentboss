# SPDX-License-Identifier: AGPL-3.0-or-later

"""ACP 协议源窗口适配器（窗口核心 Phase 1 · Task 6）：gemini 起步。

窗口核心的第一条协议源通道：内容不是终端回显（pty），而是 ACP 的结构化
事件流（session/update）。落地口径：

- hist 是文本行（agent_message_chunk 的文本、tool_call 的「工具 <名>」行），
  但形状与镜像 st["hist"] 完全一致（row-dict {x: Char}）——同一个 scrollback
  仓、同一套 GC/删除、手机端同一个 _reflow_rows 渲染，零新渲染器。
- seq 与 T1 同语义：随 hist 行数递增；start 时从仓播种，seq=len(种子)。
- 换 agent 是配置不是代码：boss.json 的 acpm.command 指到任何 ACP 适配器
  命令都行，缺省 gemini-acp。
- ACP 无发现协议：_WINDOWS 窗口表就是真相（进程内）；host 重启后窗口表蒸发，
  但 hist 在仓里不丢——仓里还认得的 acp-* sid 以「已退出」鬼影行回到列表，
  历史可读（持久注册/召回重生是 Phase 2）。

跑法（测试）： .venv/bin/python -m pytest tests/test_agent_acp.py -q
"""
import json
import shlex
import shutil
import time
import uuid
from pathlib import Path

import acp
import agents
import scrollback
from pyte.screens import Char

BOSS_CFG = Path.home() / ".codeboss" / "boss.json"

HIST_MAX = 800          # 与 scrollback.SCROLLBACK_MAX / MIRROR_HIST_MAX 同值


def acpm_command():
    """boss.json 的 acpm.command（字符串或列表，缺省 gemini-acp）。"""
    cmd = None
    try:
        cfg = json.loads(BOSS_CFG.read_text(encoding="utf-8"))
        cmd = (cfg.get("acpm") or {}).get("command")
    except Exception:
        pass
    if not cmd:
        cmd = "gemini-acp"
    return list(cmd) if isinstance(cmd, list) else shlex.split(str(cmd))


_WINDOWS = {}           # sid -> AcpWindow（进程内窗口表）


def window(sid):
    return _WINDOWS.get(sid)


def windows():
    return list(_WINDOWS.values())


def new_sid():
    return "acp-" + uuid.uuid4().hex[:10]


async def open_window(cwd=""):
    """手机「＋」开协议源窗口：起进程 + 握手 + session/new，返回运行中的窗口。
    命令读 boss.json（换 agent 是配置不是代码）。"""
    argv = acpm_command()
    if not argv:
        raise acp.AcpError("没配 ACP 命令（boss.json 的 acpm.command）")
    w = AcpWindow(new_sid(), cwd=cwd)
    await w.start(argv)
    return w


def ghosts():
    """重启后仓里还认得的 ACP 窗口 → 已退出鬼影行（hist 可读，进程没了）。"""
    out = []
    for sid in scrollback.known():
        if not sid.startswith("acp-") or sid in _WINDOWS:
            continue
        out.append(dict(sid=sid, short=sid[:8], kind="computer", pid=None,
                        cwd="", title="Gemini", window_id=None, tty="",
                        state="exited", last=0, agent="gemini", acp=1))
    return out


def is_ghost(sid):
    """这个 sid 是重启后的 ACP 鬼影吗（窗口表没有、仓里有）。"""
    return (sid or "").startswith("acp-") and sid not in _WINDOWS \
        and bool(scrollback.load(sid))


def forget(sid):
    """从窗口表摘掉（删除时用；进程的关停在调用方做）。"""
    _WINDOWS.pop(sid, None)


def _row(text):
    return {i: Char(ch) for i, ch in enumerate(text)}


class AcpWindow:
    """一个 ACP 窗口：连接 + 文本 hist（row-dict，与镜像同仓同渲染）。"""

    def __init__(self, sid, cwd="", title="Gemini"):
        self.sid, self.cwd, self.title = sid, cwd, title
        self.state = "init"
        self.started = time.time()
        self.last_at = self.started
        self.hist = []
        self.seq = 0
        self.conn = None
        self.session_id = None
        self._sb_new = 0            # 未落盘行数（防抖账本，与镜像同款）
        self._sb_at = time.time()   # 上次落盘时刻

    async def start(self, argv):
        """起进程 + initialize + session/new。重复调用 = respawn：旧连接先关干净。"""
        if self.conn is not None:
            old = self.conn
            self.conn = None
            try:
                await old.close()
            except Exception:
                pass
        conn = acp.AcpConn(argv)
        conn.on_update(self._on_update)
        conn.on_death(self._on_death)
        await conn.start()
        r = await conn.request("session/new",
                               {"cwd": self.cwd, "mcpServers": {}})
        self.conn = conn
        self.session_id = r.get("sessionId")
        if not self.hist:                     # respawn 保留内存里的；首起从仓播种
            seed = scrollback.load(self.sid)
            if seed:
                self.hist = seed
                self.seq = len(seed)
        self.state = "running"
        _WINDOWS[self.sid] = self
        return self

    def _on_update(self, method, params):
        u = params.get("update") or {}
        kind = u.get("sessionUpdate")
        line = None
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text" and c.get("text"):
                line = str(c["text"])
        elif kind == "tool_call":
            line = "工具 %s" % (u.get("title") or u.get("toolCallId") or "?")
        if not line:
            return
        for ln in line.splitlines():
            self.hist.append(_row(ln))
            self.seq += 1
        if len(self.hist) > HIST_MAX:         # 与镜像同上限：别让内存无限涨
            del self.hist[:len(self.hist) - HIST_MAX]
        self.last_at = time.time()
        # 落盘防抖（与镜像同款）：逐事件全量重写会拖住事件循环——真适配器的
        # chunk 是逐 token 来的。攒够 16 行或离上次 3s 才写；死/关时兜底冲一次。
        self._sb_new += 1
        if self._sb_new >= 16 or time.time() - self._sb_at >= 3.0:
            self._sb_flush()

    def _sb_flush(self):
        """把当前 hist 冲进仓。失败不丢内存，下次再试。"""
        try:
            scrollback.save(self.sid, self.hist)
            self._sb_new = 0
            self._sb_at = time.time()
        except Exception:
            pass

    def _on_death(self):
        self._sb_flush()                      # 崩溃前的尾巴别丢
        if self.state == "running":
            self.state = "exited"             # 崩溃要可见：不等下一次操作才发现

    async def prompt(self, text):
        if self.state != "running" or self.conn is None:
            raise acp.AcpError("窗口不在运行")
        try:
            return await self.conn.request("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}]})
        except acp.AcpError:
            self.state = "exited"
            raise

    async def close(self):
        if self.conn is not None:
            await self.conn.close()
        if self.state != "init":
            self.state = "exited"
        self._sb_flush()                      # 关窗兜底：尾部没攒够 16 行的也落盘


class Adapter(agents.AgentAdapter):
    key = "gemini"
    label = "Gemini (ACP)"
    can_resume = False        # ACP 假设 loadSession=False（见假服务器契约）
    can_notify = False        # 无 hook 通道：提醒走手机端自己的轮询
    protocol = True           # 协议源：h_ext_open 走 stdio 直连，不包 PTY

    def discover(self):
        return []             # ACP 无发现协议：窗口表（_WINDOWS）才是真相

    def installed(self):
        cmd = acpm_command()
        return bool(cmd and shutil.which(cmd[0]))

    def launch_argv(self, cwd, sid=None, model=None):
        cmd = acpm_command()
        if not cmd or not shutil.which(cmd[0]):
            return None
        return cmd

    def transcript_path(self, cwd, sid):
        return None           # 转录即 scrollback 仓（h_ext_sess_del 已一并删）


agents.register(Adapter())
