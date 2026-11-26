#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""直接跟 Codex 常驻守护进程说话（unix socket，JSON-RPC over websocket）。

桌面版 App 把会话跑在自己的管道里，增量只喂给它自己的界面：rollout 要等整轮写完
才落盘，thread 库也只记整条 item。所以手机端看桌面版会话只有「答完一次性出现」。
守护进程（app-server --managed-daemon）是同一套 codex 核心的对外 RPC：
thread/resume + turn/start 由它来跑这一轮，item/agentMessage/delta 逐字推过来。

线程被 App 占着写（already has an active writer）时 resume 会失败——那种情况
调用方退回 `codex queue`（整段送达，不抢写入权），行为跟以前一致。
"""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp

# 桌面版 App 自带的 codex 内核：App 和 host 共用这一套（见 ensure_shared_server）
_APP_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def shared_sock():
    """host 拉起的共享内核 socket。桌面版 App 通过 host 的 /rpc 转发连它。"""
    p = _codex_home() / "app-server-share.sock"
    try:
        return str(p) if p.exists() else ""
    except OSError:
        return ""


def sock_path():
    """守护进程的控制 socket。没在跑时返回空串（调用方直接走队列）。"""
    sp = shared_sock()
    if sp:
        return sp
    p = _codex_home() / "app-server-control" / "app-server-control.sock"
    try:
        return str(p) if p.exists() else ""
    except OSError:
        return ""


def ensure_shared_server(wait=10.0):
    """拉起共享内核（App 里同一个 codex 版本）。

    桌面版 App 的默认做法是自己拉一套内核，会话写入权握在它手里：它只把成品落盘，
    手机端拿不到增量，host 也接管不了那一轮。把 App 指到同一个内核后，两边是同一个
    codex：谁跑的一轮，另一方都能实时看到增量。App 端的开关见 set_app_env。
    """
    sp = shared_sock()
    if sp:
        return sp
    b = _APP_BIN if os.path.exists(_APP_BIN) else (shutil.which("codex") or "")
    if not b:
        return ""
    home = _codex_home()
    sock = home / "app-server-share.sock"
    log = open(home / "app-server-share.log", "ab")
    args = [b, "-c", "features.code_mode_host=true", "app-server",
            "--listen", "unix://%s" % sock, "--analytics-default-enabled",
            "-c", "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true"]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, cwd=str(home))
        print("[share] 共享内核启动：%s" % b, flush=True)
    except Exception as e:
        print("[share] 共享内核启动失败：%s" % str(e)[:160], flush=True)
        return ""
    t0 = time.time()
    while time.time() - t0 < wait:
        if shared_sock():
            return shared_sock()
        time.sleep(0.05)
    return ""


_APP_RST = {"at": 0.0}   # 上次自愈重启 App 的时间（限速）

def app_restart_if_idle(reason=""):
    """流式通道坏了时的自愈：App 在跑却没接上共享内核 / 占着写入权 → 重启它。

    只在**没有任何桌面版会话正在跑**（rollout 末条 task 事件不是 task_started）时动手：
    退出 App 会把在答的那一轮掐断。能不能动手都返回 (ok, 原因)，调用方决定要不要重试。"""
    import agent_codex
    now = time.time()
    if now - _APP_RST["at"] < 600:
        return False, "刚重启过 %.0f 秒前" % (now - _APP_RST["at"])
    if not agent_codex.desktop_app_running():
        return False, "App 没开"
    busy = agent_codex.app_busy_threads()
    if busy:
        return False, "有会话在跑（%s）" % ",".join(x[:8] for x in busy)
    _APP_RST["at"] = now
    print("[heal] 流式异常（%s），桌面版没在跑会话 → 重启 App" % ((reason or "-")[:80]),
          flush=True)
    if not agent_codex.quit_desktop_app():
        return False, "App 退不掉"
    try:
        subprocess.run(["open", "-a", "ChatGPT"], capture_output=True, timeout=15)
    except Exception as e:
        print("[heal] 重新打开 App 失败：%s" % str(e)[:120], flush=True)
    return True, "已重启"


class _RPC:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.events = []   # 等 response 期间收到的通知：不能丢，交给 _pump 补处理

    async def call(self, method, params, timeout=20):
        self.n += 1
        i = self.n
        await self.ws.send_json({"id": i, "method": method, "params": params})
        while True:
            msg = await asyncio.wait_for(self.ws.receive_json(), timeout)
            if "method" not in msg:
                if msg.get("id") == i:
                    return msg
                continue
            if not await _maybe_approval(self.ws, msg):
                self.events.append(msg)


async def _maybe_approval(ws, msg):
    """服务端在等人拍板命令执行：手机发起的这一轮没人能点弹窗，按桌面版常规放行。
    （exec resume 那条老路本来就是 workspace-write、没人问；这里保持一致。）"""
    m = msg.get("method") or ""
    if "requestApproval" not in m or "id" not in msg:
        return False
    try:
        await ws.send_json({"id": msg["id"], "result": {"decision": "accept"}})
        print("[desk] 流式这一轮自动放行一次审批（%s）" % m, flush=True)
    except Exception:
        pass
    return True


async def _close(ws, sess):
    try:
        if ws is not None:
            await ws.close()
    except Exception:
        pass
    try:
        await sess.close()
    except Exception:
        pass


async def _frames(ws, pending=None):
    """先吐 _RPC 等待期间攒下的通知，再继续收：turn/start 很快时增量会抢在响应前面到。"""
    for m in pending or ():
        yield m
    while True:
        yield await asyncio.wait_for(ws.receive_json(), 1800)


# 这一轮现在在干什么（手机端状态行用）。item 的类型名取自 codex 内置协议
# （agentMessage / reasoning / commandExecution / fileChange / mcpToolCall /
#  dynamicToolCall / collabAgentToolCall / webSearch）。
_ITEM_ST = {
    "reasoning": "正在思考",
    "commandExecution": "正在运行命令",
    "fileChange": "正在改文件",
    "webSearch": "正在搜索",
    "mcpToolCall": "正在调用工具",
    "dynamicToolCall": "正在调用工具",
    "collabAgentToolCall": "正在调用工具",
    "agentMessage": "正在输出",
}
# commandExecution 的 commandActions 细分（桌面版「正在读取/正在搜索」就是它）：
# read / search / listFiles（rollout 里叫 parsed_cmd，type 是 read/search/list_files）
_CMD_ST = {
    "read": "正在读取",
    "search": "正在搜索",
    "listFiles": "正在浏览文件",
    "list_files": "正在浏览文件",
}


def _cmd_stat(it):
    for a in (it.get("commandActions") or it.get("parsed_cmd") or ()):
        st = _CMD_ST.get(a.get("type") or "")
        if st:
            return st
    return "正在运行命令"


async def _pump(ws, sess, on_delta, on_end, pending=None, on_stat=None):
    """收这一轮的事件：agent 正文增量 → on_delta(全文)；结束 → on_end(status, 全文)。
    on_stat(状态文案)：这一轮在思考/跑命令/改文件…（可选）。"""

    def _stat(s):
        if on_stat:
            try:
                on_stat(s)
            except Exception:
                pass

    # 一轮里可能有多条 agentMessage（先说了句前言、跑完工具再补一段、最后是正文）：
    # 按 itemId 各存一份再拼起来。以前把增量累加进一个字符串、完成时用本条覆盖，
    # 前面几条说完就整段消失——手机上看到的就是「中间少了内容」。
    parts, order = {}, []
    cmd_st = {}          # 命令条目 id → 细分状态：outputDelta 到达时别把「正在读取」盖成「正在运行命令」

    def _put(iid):
        if iid not in parts:
            parts[iid] = ""
            order.append(iid)

    def _joined():
        return "\n\n".join(parts[x] for x in order if parts.get(x))

    status = "err"
    n = 0
    _stat("正在思考")
    try:
        async for msg in _frames(ws, pending):
            m = msg.get("method") or ""
            p = msg.get("params") or {}
            if not m:
                continue                      # 别的请求的响应：不归这里管
            if m == "item/started":
                it = p.get("item") or {}
                if it.get("type") == "commandExecution":
                    st = _cmd_stat(it)
                    cmd_st[it.get("id") or ""] = st
                    _stat(st)
                else:
                    st = _ITEM_ST.get(it.get("type") or "")
                    if st:
                        _stat(st)
                if it.get("type") == "agentMessage" and it.get("id"):
                    _put(it["id"])            # 先占位置，保住 item 顺序
            elif m == "item/agentMessage/delta":
                n += 1
                d = p.get("delta") or ""
                if d:
                    iid = p.get("itemId") or ""
                    _put(iid)
                    parts[iid] += d
                    on_delta(_joined())
            elif m == "item/completed":
                it = p.get("item") or {}
                if it.get("type") == "agentMessage" and it.get("text"):
                    iid = it.get("id") or ""
                    _put(iid)
                    parts[iid] = it["text"]   # 完成时以整段为准（增量可能有缺帧）
                    on_delta(_joined())
                elif it.get("type") in _ITEM_ST:
                    _stat("正在思考")       # 工具跑完了，又回去想
            elif m == "turn/completed":
                status = "ok"
                break
            elif m == "turn/aborted":
                break
            else:
                # 没有 item/started、只有细粒度增量的版本：按方法名前缀认
                if m.startswith("item/commandExecution/"):
                    _stat(cmd_st.get(p.get("itemId") or "", "正在运行命令"))
                elif m.startswith("item/fileChange/"):
                    _stat("正在改文件")
                elif m.startswith("item/mcpToolCall/") or m.startswith("item/dynamicToolCall/"):
                    _stat("正在调用工具")
                elif m.startswith("item/webSearch/"):
                    _stat("正在搜索")
                elif m.startswith("item/reasoning/"):
                    _stat("正在思考")
                await _maybe_approval(ws, msg)
    except Exception as e:
        print("[desk] 流式中断：%s" % str(e)[:120], flush=True)
    finally:
        text = _joined()
        print("[desk] 流式结束 status=%s deltas=%d items=%d len=%d"
              % (status, n, len(order), len(text)), flush=True)
        await _close(ws, sess)
        try:
            on_end(status, text)
        except Exception:
            pass


async def _turn(sid, text, on_delta, on_end, start=True, on_stat=None):
    """连内核并（可选）发起一轮。成功返回 (True, "")（之后增量走回调）；
    起不来（内核没跑 / 线程被另一套内核占着 / 版本不认这个方法）返回 (False, 原因)。"""
    sp = sock_path()
    if not sp:
        return False, "守护进程没在跑"
    sess = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=sp))
    ws = None
    try:
        ws = await asyncio.wait_for(sess.ws_connect("http://localhost/"), 6)
        rpc = _RPC(ws)
        r = await rpc.call("initialize", {"clientInfo": {"name": "codeboss-host",
                                                         "version": "1"},
                                          "capabilities": {"experimentalApi": True}}, 8)
        if r.get("error"):
            raise RuntimeError((r["error"] or {}).get("message") or "initialize 失败")
        r = await rpc.call("thread/resume", {"threadId": sid}, 20)
        if r.get("error"):
            err = (r["error"] or {}).get("message") or "resume 失败"
            await _close(ws, sess)
            return False, err
        if start:
            r = await rpc.call("turn/start", {"threadId": sid,
                                              "input": [{"type": "text", "text": text}]}, 20)
            if r.get("error"):
                err = (r["error"] or {}).get("message") or "turn/start 失败"
                await _close(ws, sess)
                return False, err
        asyncio.ensure_future(_pump(ws, sess, on_delta, on_end, rpc.events, on_stat))
        return True, ""
    except Exception as e:
        await _close(ws, sess)
        return False, str(e)[:160]


async def start_turn(sid, text, on_delta, on_end, on_stat=None):
    """在守护进程里跑这一轮，成功返回 (True, "")（之后增量走回调）。
    起不来（守护进程没跑 / 线程被 App 占着 / 版本不认这个方法）返回 (False, 原因)，
    调用方退回 codex queue。"""
    return await _turn(sid, text, on_delta, on_end, True, on_stat)


async def watch_turn(sid, on_delta, on_end, on_stat=None):
    """旁观（rejoin）同一条线程上正在跑的一轮：桌面版 App 自己发起的也能逐字看到。
    内核不支持/线程不在这套内核里时返回 (False, 原因)，调用方忽略即可。"""
    return await _turn(sid, "", on_delta, on_end, False, on_stat)
