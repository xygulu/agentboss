#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Codex CLI 适配器。

Codex 跟 Claude Code 的差别全在「怎么找、怎么读、怎么开」上，接口本身见 agents.py：

- 会话记录：`~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl`（一行一个 JSON）。
  第一行 `session_meta` 带 cwd / 来源；对话是 `response_item`（message / function_call /
  function_call_output）；`event_msg` 里是 task_started / task_complete 这类运行状态。
- 活着的会话：Codex 没有 `claude agents --json` 这种查询命令，但运行中的进程一直开着
  自己的 rollout 文件——`ps` 找 codex 进程，`lsof -p` 看它打开的 .jsonl，就是会话 id。
- 开进程：`codex`（新会话）、`codex resume <uuid>`（召回）、`codex fork <uuid>`（复制）。
- 提醒：Codex 有 `notify`（config.toml 顶层，命令 + 一个 JSON 参数）。它只在
  「这一轮跑完」时触发，正好对应 Claude 的 Stop。server.py 把这个 payload 翻译成
  Stop 事件，与 Claude 走同一条提醒链路。

这里只描述 Codex 自己的行为，不碰 server 的状态机；取不到能力时明确返回空 / None，
由调用方显示「不支持」，绝不静默当成功。
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import agents
import plat

DEFAULT_HOME = Path.home() / ".codex"

# rollout 文件名：rollout-2026-11-09T22-42-52-01a1c0c3-14b6-7a02-b057-8864778ef3b8.jsonl
_SID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")

# Codex 塞进对话上下文里的「假用户消息」，不是人打的
_INJECTED = ("<environment_context>", "<user_instructions>", "<user_shell_command>",
             "# AGENTS.md", "<skills_instructions>")

_TOOL_CALLS = ("function_call", "custom_tool_call", "local_shell_call",
               "web_search_call", "image_generation_call", "tool_search_call")


def codex_home():
    p = os.environ.get("CODEX_HOME") or ""
    return Path(p).expanduser() if p.strip() else DEFAULT_HOME


def roots():
    """会话目录候选：环境变量指定的 + 默认 ~/.codex（老会话可能在默认目录里）。"""
    out = []
    for p in (codex_home(), DEFAULT_HOME):
        if p not in out:
            out.append(p)
    return out


def _search_path():
    """找 codex / 它跑起来要用的 node 时用的 PATH。
    Windows：npm 全局和 winget 的 shim 常常装了但不在 host 自己的 PATH 里
    （和 server._WIN_EXTRA 同一批目录）；mac/Linux：npm 全局 bin 也一样。"""
    if plat.IS_WIN:
        dirs = [str(Path(os.environ.get("APPDATA", "")) / "npm"),
                str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links")]
    else:
        dirs = [str(Path.home() / ".npm-global/bin"), str(Path.home() / ".local/bin"),
                "/opt/homebrew/bin", "/usr/local/bin"]
    return os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def _daemon_bins():
    """Codex 桌面版自己拉的 app-server 守护进程，release 包里带完整 codex CLI：
    `~/.codex/packages/app-server-daemon/releases/<版本>/bin/codex`。
    只装桌面版、没单独 npm 装 codex 的机器上这是唯一能用的二进制（npm shim 可能坏）。
    新版本在前。"""
    out = []
    try:
        base = codex_home() / "packages" / "app-server-daemon" / "releases"
        for d in sorted(base.iterdir(), reverse=True):
            for nm in ("codex", "codex.exe"):
                p = d / "bin" / nm
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    out.append(str(p))
    except OSError:
        pass
    return out


_SEEN_BINS = set()   # discover 从 ps 里记下的「真在跑的 codex 可执行文件」（App/插件自带的都在这）
_DESK_NOTE = set()   # 已打过「桌面版来源」日志的 sid，别每 2 秒刷一遍


def _note_bin(cmd):
    """ps 的 argv[0] 是绝对路径就记下来：桌面 App、IDE 插件带的 codex 装在哪都无所谓，
    正在跑的那个就是能 resume 的那个。返回记下的路径（没有就空串）。"""
    first = (cmd or "").strip().split(" ", 1)[0].strip("\"'")
    # 只认名字里带 codex 的可执行文件：桌面 App 的辅助进程（Sparkle 的 Autoupdate 等）
    # 命令行里也可能带 codex 字样，记进来会拿它去探 `exec --help`，白等十几秒超时。
    if (os.path.isabs(first) and os.path.isfile(first)
            and "codex" in os.path.basename(first).lower()):
        rp = os.path.realpath(first)
        _SEEN_BINS.add(rp)
        return rp
    return ""


def _codex_candidates():
    """所有能找到的 codex 可执行文件（正在跑的、桌面版守护进程、PATH、npm 全局…），按优先级。"""
    search = _search_path()
    out = []
    cands = ((os.environ.get("CODEBOSS_CODEX_BIN"),) + tuple(sorted(_SEEN_BINS))
             + tuple(_daemon_bins())
             + ("codex",
                str(Path.home() / ".npm-global/bin/codex"),
                str(Path.home() / ".local/bin/codex"),
                "/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex",
                # Codex 桌面版自带 CLI（没单独装 codex 的机器，装了这个 app 也算装了）
                "/Applications/Codex.app/Contents/MacOS/codex",
                str(Path.home() / "Applications/Codex.app/Contents/MacOS/codex")))
    for c in cands:
        if not c:
            continue
        if os.path.sep in c:
            if os.path.isfile(c) and os.access(c, os.X_OK) and c not in out:
                out.append(c)
        else:
            w = shutil.which(c, path=search)
            if w and w not in out:
                out.append(w)
    return out


def codex_bin():
    """给本进程自己用的 codex 绝对路径（delete / --version 这类不需要终端 PATH 的调用）。"""
    cands = _codex_candidates()
    return cands[0] if cands else None


def _run(argv, timeout=8, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception:
        return -1, "", ""


# ── 桌面版会话发消息 ─────────────────────────────────────────────
# 桌面 App 的线程归它自己的 app-server，外部接不进去；但 codex CLI 有
# `codex exec resume <thread-id> <文本>`：以同一用户身份往同一个 rollout 追一个回合。
# 手机发的消息走它，回复照旧从 rollout 推送回来（App 那边要不要刷新是它的事）。
_EXEC_RESUME = {}          # codex 可执行文件 -> (检查时刻, 支持与否)
_DESK_RUN = {}             # sid -> {"at": 起的时刻, "p": Popen, "err": 最近几行}


def resume_bin():
    """能跑 `exec resume` 的 codex（按候选顺序探，探过的缓存 10 分钟）。
    不能只信第一个：npm 那个 shim 可能坏了（缺可选依赖），而 Codex.app 自带的还能用。"""
    now = time.time()
    cands = _codex_candidates()
    if not cands:
        print("[desk] 没找到 codex 可执行文件（桌面版守护进程 / npm / app 都没有）", flush=True)
        return None
    for b in cands:
        hit = _EXEC_RESUME.get(b)
        if hit and now - hit[0] < (900 if hit[1] else 600):
            ok = hit[1]
        else:
            rc, out, err = _run([b, "exec", "--help"], timeout=5)
            if rc < 0:
                # 超时/起不来（机器忙、App 正在自更新）：这是「探不到」，不是「不支持」。
                # 以前当成不支持缓存 10 分钟，手机端整个窗口期都显示只读输入框。
                continue
            ok = rc == 0 and "resume" in (out + err)
            _EXEC_RESUME[b] = (now, ok)
            print("[desk] codex %s：exec resume %s" % (b, "可用" if ok else "不可用"), flush=True)
        if ok:
            return b
    return None


def exec_resume_ok(b=None):
    return resume_bin() is not None


def _drain(p, buf, maxlen=4000):
    try:
        for ln in iter(p.stderr.readline, b""):
            buf.append(ln.decode("utf-8", "replace")[:400])
            if len(buf) > 6:
                del buf[0]
    except Exception:
        pass


# 桌面版 App 开着这个对话时，它自己的 app-server 是这个 rollout 的「活写入者」，
# 外部 exec resume 被拒：thread-store conflict: … already has an active writer。
# App 退出后写入权就释放，能直接写回原会话（重开 App 能看到手机发的这段）。
_ACTIVE_WRITER = re.compile(r"already has an active writer|thread-store conflict", re.I)
_DESK_APP_PAT = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"


def desktop_app_running():
    try:
        return subprocess.run(["pgrep", "-f", _DESK_APP_PAT],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def _last_task_event(path, tail=65536):
    """rollout 末尾的 task_started / task_complete / turn_aborted（只读尾巴，别扫整份）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            data = f.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    last = ""
    for ln in data.splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("type") != "event_msg":
            continue
        t = (d.get("payload") or {}).get("type")
        if t in ("task_started", "task_complete", "turn_aborted"):
            last = t
    return last


def app_busy_threads():
    """App 里**正在跑**的对话：rollout 还被 App 的 codex 开着，且末条 task 事件是
    task_started（没 task_complete / turn_aborted）。

    退出 App 会把这些对话从中间掐断（App 会往 rollout 里记 turn_aborted，
    已经写出的内容不丢，但这一轮的回答就没了）——所以有它们时绝不退出。"""
    if sys.platform != "darwin" or not desktop_app_running():
        return []
    out = []
    for pid, r in _ps_rows().items():
        if "ChatGPT.app/Contents/Resources/codex-cli" not in (r.get("cmd") or ""):
            continue
        for p in _rollouts_via_lsof(pid):
            if _last_task_event(p) == "task_started":
                sid = _sid_of(p)
                if sid and sid not in out:
                    out.append(sid)
    return out


def quit_desktop_app(wait=8.0):
    """退出桌面版 App（ChatGPT.app，bundle id com.openai.codex），释放会话写入权。
    Codex 的会话都存在 rollout 里，退出不丢内容；重开 App 就能看到手机写进去的回合。"""
    if sys.platform != "darwin" or not desktop_app_running():
        return False
    try:
        subprocess.run(["osascript", "-e", 'tell application id "com.openai.codex" to quit'],
                       capture_output=True, timeout=15)
    except Exception:
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if not desktop_app_running():
            return True
        time.sleep(0.3)
    return not desktop_app_running()


def _desk_server_running():
    """桌面版 App 或它常驻的 app-server 守护进程在跑：这时 `codex queue` 有人消费。
    都没有时排进去没人取（消息会一直躺在队列里），所以不能走排队这条路。"""
    if desktop_app_running():
        return True
    try:
        rows = _ps_rows()
    except Exception:
        return False
    return any("app-server" in (r.get("cmd") or "") and "codex" in (r.get("cmd") or "")
               for r in rows.values())


def queue_send(b, sid, text, timeout=20):
    """把消息排进会话队列：App 自己/守护进程取走并开跑（没开着的会话由 --resume 唤醒）。

    这是发进桌面版会话最温和的路：不抢 rollout 写入权、不用退 App，
    电脑上别的对话（包括正在答的）都不受影响；正在跑的那一轮答完才轮到这条。"""
    try:
        r = subprocess.run([b, "queue", "--thread", sid, "--message", text, "--resume"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout,
                           env=dict(os.environ, PATH=_search_path()))
    except Exception as e:
        return False, str(e)[:160]
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]


def _start_resume(argv, cwd, sid):
    try:
        p = subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=dict(os.environ, PATH=_search_path()))
    except Exception as e:
        return None, "启动 codex 失败：%s" % str(e)[:120]
    buf = []
    threading.Thread(target=_drain, args=(p, buf), daemon=True).start()
    _DESK_RUN[sid] = {"at": time.time(), "p": p, "err": buf}
    return (p, buf), ""


def _wait_early(p, wait):
    t0 = time.time()
    while time.time() - t0 < wait:
        if p.poll() is not None:
            break
        time.sleep(0.15)


def desktop_send(cwd, sid, text, wait=3.0):
    """往桌面版会话里发一句话（后台起 codex exec resume，不挡住手机请求）。
    立刻退出且非 0 才算失败（会话不在了/正在跑/版本不支持）；真跑起来就交给 rollout 推送。
    返回 (ok, 错误, 提示)：提示非空时手机端要告诉用户（比如「App 被暂时退出了」）。"""
    b = resume_bin()
    if not b:
        if not _codex_candidates():
            return False, "这台电脑上没找到 codex（桌面版守护进程 / npm / app 都查了）", ""
        return False, "这个 codex 版本不支持在外部发消息（升级 codex 或桌面版再试）", ""
    cur = _DESK_RUN.get(sid)
    if cur and cur["p"].poll() is None:
        return False, "上一条消息还在处理中，等它回复完再发", ""
    # 先走队列：App/守护进程自己消费，不碰写入权，电脑上跑着的东西一概不动。
    if _desk_server_running():
        okq, errq = queue_send(b, sid, text)
        if okq:
            print("[desk] 排队送达 sid=%s" % sid[:8], flush=True)
            return True, "", ""
        print("[desk] 排队没成（%s），改走 exec resume" % (errq or "")[:120], flush=True)
    # --skip-git-repo-check：会话目录不一定是 git 仓库，codex 默认会直接拒
    #   （"Not inside a trusted directory and --skip-git-repo-check was not specified"）。
    # -c sandbox_mode=…：exec 没人点审批弹窗，给桌面版会话常规写权限；resume 子命令没有 -s。
    argv = [b, "exec", "resume", "--skip-git-repo-check",
            "-c", 'sandbox_mode="workspace-write"', sid, text]
    got, err = _start_resume(argv, cwd, sid)
    if not got:
        return False, err, ""
    p, buf = got
    _wait_early(p, wait)
    note = ""
    if p.returncode not in (None, 0):
        tail = " ".join(x.strip() for x in buf[-3:] if x.strip())
        if not _ACTIVE_WRITER.search(tail):
            return False, "Codex 拒绝了这条消息%s" % (("：" + tail[:200]) if tail else ""), ""
        # 会话被桌面版 App 占着写：App 关掉就释放。重开 App 能看到这段，所以不是 fork。
        # 但 App 里可能还有别的对话正在跑，退出会把它从中间掐断——那就不退，让手机端说清楚。
        busy = app_busy_threads()
        if busy:
            if sid in busy:
                return False, "这个对话正在电脑上的 Codex 里跑着，等这一轮答完再发", ""
            return False, ("电脑上的 Codex 还有 %d 个对话正在运行（现在退出 App 会把它们掐断），"
                           "等它们跑完再发" % len(busy)), ""
        if not quit_desktop_app():
            return False, "这个对话正在电脑上的 Codex 里打开着，Codex 不允许同时写入", ""
        got, err = _start_resume(argv, cwd, sid)
        if not got:
            return False, err, ""
        p, buf = got
        _wait_early(p, wait)
        if p.returncode not in (None, 0):
            tail = " ".join(x.strip() for x in buf[-3:] if x.strip())
            return False, "Codex 拒绝了这条消息%s" % (("：" + tail[:200]) if tail else ""), ""
        note = "已暂时退出电脑上的 Codex，消息写进了原对话（重新打开 App 就能看到）"
        print("[desk] 会话被 App 占着写，退出 App 后写原会话成功 sid=%s" % sid[:8], flush=True)
    return True, "", note


def _sid_of(path):
    m = _SID_RE.search(str(path))
    return m.group(1) if m else ""


# ---- 转录解析 ----------------------------------------------------------------

def _user_text(pl):
    """message(role=user) 的正文；注入的上下文返回空串。"""
    c = pl.get("content")
    parts = []
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("input_text", "text"):
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
    txt = "\n".join(parts).strip()
    if not txt:
        return ""
    if txt.startswith(_INJECTED):
        return ""
    return txt


def _assistant_text(pl):
    c = pl.get("content")
    parts = []
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") in ("output_text", "text"):
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
    return "\n".join(parts).strip()


def _tool_item(pl):
    """response_item(*_call) -> {name, detail[, cmd]}；参数认不出时至少保留 name。"""
    name = str(pl.get("name") or pl.get("type") or "tool")
    raw = pl.get("arguments")
    if raw is None:
        raw = pl.get("input")
    d = {}
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except Exception:
            d = {"_raw": raw}
    elif isinstance(raw, dict):
        d = raw
    if not isinstance(d, dict):
        d = {}
    act = d.get("action")            # local_shell_call：{"action": {"command": [...], ...}}
    if isinstance(act, dict):
        d = dict(act, **{k: v for k, v in d.items() if k != "action"})
    detail = ""
    for k in ("cmd", "command", "path", "file_path", "filePath", "pattern",
              "query", "url", "prompt"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            detail = v.strip()
            break
        if isinstance(v, list) and v and isinstance(v[0], str):
            detail = " ".join(str(x) for x in v)
            break
    if not detail:
        q = d.get("queries")
        if isinstance(q, list) and q:
            detail = str(q[0])
    if not detail and isinstance(d.get("_raw"), str):
        first = d["_raw"].strip().splitlines()[0] if d["_raw"].strip() else ""
        detail = first
    item = {"name": name, "detail": detail[:160] + ("…" if len(detail) > 160 else "")}
    low = name.lower()
    if ("exec" in low or "shell" in low or "command" in low) and detail:
        item["cmd"] = detail      # 界面显示截断版，命令识别要完整原文
    return item


def parse_rollout(path, limit=120, maxbytes=16 * 1024 * 1024):
    """rollout.jsonl -> [{role, text, tools:[{name, detail}]}]，跟 Claude 的输出同形。
    只读文件尾部：一次会话能写到几十 MB，全量 readlines 纯属自残。"""
    msgs = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - maxbytes))
            blob = f.read().decode("utf-8", "ignore")
    except Exception:
        return msgs
    lines = blob.split("\n")
    if size > maxbytes and lines:
        lines = lines[1:]          # 掐掉开头可能截半的行
    turns = []                     # (last_agent_message, duration_ms)：task_complete 带本轮总时长
    for line in lines[-8000:]:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "event_msg":
            pl = o.get("payload") or {}
            if isinstance(pl, dict) and pl.get("type") == "task_complete":
                dm = pl.get("duration_ms")
                lam = str(pl.get("last_agent_message") or "").strip()
                if isinstance(dm, int) and dm >= 0 and lam:
                    turns.append((lam, dm))
            continue
        if o.get("type") != "response_item":
            continue
        pl = o.get("payload") or {}
        if not isinstance(pl, dict):
            continue
        pt = pl.get("type")
        role, texts, tools = "", [], []
        if pt == "message":
            r = pl.get("role")
            if r == "user":
                t = _user_text(pl)
                if t:
                    role, texts = "user", [t]
            elif r == "assistant":
                t = _assistant_text(pl)
                if t:
                    role, texts = "assistant", [t]
        elif pt in _TOOL_CALLS:
            it = _tool_item(pl)
            if it:
                role, tools = "assistant", [it]
        if not role or (not texts and not tools):
            continue
        if msgs and msgs[-1]["role"] == role and not tools and not msgs[-1]["tools"]:
            msgs[-1]["text"] += "\n" + "\n".join(texts)
        else:
            msgs.append({"role": role, "text": "\n".join(texts), "tools": tools})
    # 每一轮的用时挂到它那条回答上（手机「用时 N秒」）：task_complete 用 last_agent_message
    # 认领消息，正文变了认不出就按前 60 字认——比页面内存里那点 DONE_EL 抗重载。
    for lam, dm in turns:
        for m in reversed(msgs):
            if m["role"] != "assistant" or m.get("el"):
                continue
            t = m["text"].strip()
            if t == lam or (len(lam) >= 20 and t.startswith(lam[:60])):
                m["el"] = max(1, int(round(dm / 1000.0)))
                break
    return msgs[-limit:]


_META = {}      # 路径 -> (cwd, 标题)；一次会话的这两样不会变，不用看 mtime


def rollout_meta(path):
    """rollout -> (cwd, 标题)。cwd 在 session_meta，标题取第一条真人用户消息。"""
    key = str(path)
    hit = _META.get(key)
    if hit is not None:
        return hit
    cwd, title = "", ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 200:
                    break
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                t = o.get("type")
                pl = o.get("payload") or {}
                if t == "session_meta":
                    cwd = str(pl.get("cwd") or "")
                elif not title:
                    if t == "response_item" and pl.get("type") == "message" and pl.get("role") == "user":
                        ut = _user_text(pl)
                        if ut:
                            title = ut
                    elif t == "event_msg" and pl.get("type") == "item_completed":
                        it = pl.get("item") or {}
                        if it.get("type") == "UserMessage":
                            for b in it.get("content") or []:
                                if isinstance(b, dict) and b.get("text"):
                                    txt = str(b["text"]).strip()
                                    if txt and not txt.startswith(_INJECTED):
                                        title = txt
                                        break
                if cwd and title:
                    break
    except OSError:
        pass
    title = re.sub(r"\s+", " ", title or "").strip()[:24]
    if cwd or title:           # 刚建出来的文件可能还空着，别把空结果缓存住
        _META[key] = (cwd, title)
    return cwd, title


_SRC = {}       # 路径 -> 创建者（session_meta.originator）


def _thread_src(path):
    """thread 是谁建的。`codex_exec` 是一次性批量任务——列表按最近 N 条截断时，
    它们会把真人聊过的会话全挤出去（见 closed_sessions）。"""
    key = str(path)
    if key in _SRC:
        return _SRC[key]
    src = ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            first = json.loads(f.readline() or "{}")
        if first.get("type") == "session_meta":
            pl = first.get("payload") or {}
            src = str(pl.get("originator") or pl.get("source") or "")
    except Exception:
        pass
    if src:
        _SRC[key] = src
    return src


_STATE = {}     # 路径 -> (size, 状态)


def rollout_state(path):
    """这一轮在跑还是闲着：看最后一条 task_started / task_complete。"""
    key = str(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    hit = _STATE.get(key)
    if hit and hit[0] == size:
        return hit[1]
    state = ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 128 * 1024))
            blob = f.read().decode("utf-8", "ignore")
        for ln in reversed(blob.split("\n")):
            ln = ln.strip()
            if not ln or not ln.startswith("{"):
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") != "event_msg":
                continue
            pt = (o.get("payload") or {}).get("type")
            if pt == "task_started":
                state = "busy"
                break
            if pt in ("task_complete", "turn_aborted"):
                state = "idle"
                break
    except OSError:
        return ""
    _STATE[key] = (size, state)
    return state


# ---- 找文件 ------------------------------------------------------------------

_PATH_CACHE = {}     # sid -> Path
_MISS = {}           # sid -> 下次再找的时间


def _state_db():
    """最新版本的 state_N.sqlite（threads 表里 id -> rollout_path）。"""
    try:
        dbs = sorted(codex_home().glob("state_*.sqlite"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        dbs = []
    return dbs[0] if dbs else None


def notify_chain_path():
    """装 CodeBoss 之前用户自己的 notify 存这里（我们上报完再转给它）。"""
    return codex_home() / "codex-notify-chain.sh"


def rollout_path(sid):
    """sid -> rollout 文件。先问 state 库（O(1)），再按文件名扫日期目录。"""
    p = _PATH_CACHE.get(sid)
    if p and p.exists():
        return p
    if p and not p.exists():
        _PATH_CACHE.pop(sid, None)
    now = time.time()
    if now < _MISS.get(sid, 0):
        return None
    db = _state_db()
    if db is not None:
        try:
            import sqlite3
            con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                r = con.execute("SELECT rollout_path FROM threads WHERE id=?", (sid,)).fetchone()
            finally:
                con.close()
            if r and r[0] and Path(r[0]).exists():
                _PATH_CACHE[sid] = Path(r[0])
                return _PATH_CACHE[sid]
        except Exception:
            pass
    for root in roots():
        for pat in ("sessions/*/*/*/rollout-*-%s.jsonl" % sid,
                    "sessions/*/*/*/*/rollout-*-%s.jsonl" % sid):
            try:
                hit = next(iter(root.glob(pat)), None)
            except (OSError, ValueError):
                hit = None
            if hit:
                _PATH_CACHE[sid] = hit
                return hit
    _MISS[sid] = now + 5
    return None


def _archived_ids():
    """被 codex archive 过的会话：不再出现在「已退出可召回」列表里。"""
    db = _state_db()
    if db is None:
        return set()
    try:
        import sqlite3
        con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT id FROM threads WHERE archived=1").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _rollout_files():
    out, seen = [], set()
    for root in roots():
        for pat in ("sessions/*/*/*/rollout-*.jsonl", "sessions/*/*/*/*/rollout-*.jsonl"):
            try:
                for p in root.glob(pat):
                    sid = _sid_of(p)
                    if not sid or sid in seen:
                        continue
                    seen.add(sid)
                    out.append(p)
            except (OSError, ValueError):
                pass
    return out


# ---- 活着的进程 --------------------------------------------------------------

def _ps_rows():
    rc, out, _ = _run(["ps", "-axo", "pid=,ppid=,tty=,command="], timeout=6)
    rows = {}
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows[pid] = {"ppid": ppid, "tty": parts[2], "cmd": parts[3]}
    return rows


def _is_codex_cmd(cmd):
    first = (cmd.split() or [""])[0]
    if os.path.basename(first) != "codex":
        return False
    low = cmd.lower()
    # app-server / exec-server / mcp 这些是 CodeBoss 自己的或后台服务，
    # 不是用户在终端里交互跑的会话
    return not any(x in low for x in ("exec-server", "app-server", " mcp ", "update"))


def _oneshot_cmd(cmd):
    """命令行是不是一次性任务（`codex exec …`）。看 codex 后面第一个子命令：
    `codex resume`、光秃秃的 `codex`（TUI）都是交互会话。"""
    toks = (cmd or "").split()
    for i, t in enumerate(toks):
        if os.path.basename(t) == "codex":
            return (toks[i + 1] if i + 1 < len(toks) else "") == "exec"
    return False


_NOTTY_SEEN = set()       # 已经打过日志的「无 tty codex 进程」，别每 2 秒刷一遍
DBG = [False]             # 细日志开关：后台开了「日志上报」才由 server 置位


def _note_notty(pid, cmd):
    """把被跳过的无 tty codex 进程写进 host 日志：这是「codex 会话显示无窗口/发不了字」
    的根因，日志里看不到就只剩一个『什么都没发生』。每个 pid 只打一次。"""
    if not DBG[0] or pid in _NOTTY_SEEN:
        return
    if len(_NOTTY_SEEN) > 1000:
        _NOTTY_SEEN.clear()
    _NOTTY_SEEN.add(pid)
    try:
        print("[disc] codex 跳过无 tty 进程 pid=%s cmd=%s"
              % (pid, (cmd or "")[-80:]), flush=True)
    except Exception:
        pass


def _rollout_via_lsof(pid):
    rc, out, _ = _run(["lsof", "-p", str(pid), "-Fn"], timeout=6)
    if rc not in (0, 1):      # lsof 没权限/不存在时 rc=1 也可能是「没打开文件」
        return None
    for ln in out.splitlines():
        if not ln.startswith("n"):
            continue
        p = ln[1:]
        if p.endswith(".jsonl") and os.sep + "sessions" + os.sep in p:
            return Path(p)
    return None


def _rollouts_via_lsof(pid):
    """进程打开的**全部** rollout（桌面版的 app-server 会同时挂多个 thread）。"""
    rc, out, _ = _run(["lsof", "-p", str(pid), "-Fn"], timeout=6)
    if rc not in (0, 1):
        return []
    got = []
    for ln in out.splitlines():
        if not ln.startswith("n"):
            continue
        p = ln[1:]
        if p.endswith(".jsonl") and os.sep + "sessions" + os.sep in p:
            q = Path(p)
            if q not in got:
                got.append(q)
    return got


def _proc_info_via_lsof(pid):
    """进程的 (cwd, 它打开的 rollout)。一次 lsof 拿两样。

    0.167 起 codex 的 TUI 把会话交给常驻 app-server 守护进程，
    rollout 文件是**守护进程**开着的，lsof TUI 进程看不到 —— 这时 cwd 仍然有，
    调用方据此去 threads 表里认领这个会话（见 _threads_for_cwd）。"""
    rc, out, _ = _run(["lsof", "-p", str(pid), "-Fn"], timeout=6)
    if rc not in (0, 1):
        return "", None
    cwd, fd, path = "", "", None
    for ln in out.splitlines():
        if not ln:
            continue
        t, v = ln[0], ln[1:]
        if t == "f":
            fd = v
        elif t == "n" and fd == "cwd":
            cwd = v
        elif t == "n" and v.endswith(".jsonl") and os.sep + "sessions" + os.sep in v:
            path = Path(v)
    return cwd, path


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _sid_from_argv(cmd):
    """`codex resume <sid>` / `codex fork <sid>` 的 sid；没有就空串。"""
    parts = (cmd or "").split()
    for i, x in enumerate(parts[:-1]):
        if x in ("resume", "fork") or x == "r":
            m = _UUID_RE.search(parts[i + 1])
            if m:
                return m.group(0)
    return ""


def _started_at(pid):
    """进程启动时刻（etime 反推）；拿不到就 0。"""
    rc, out, _ = _run(["ps", "-p", str(pid), "-o", "etime="], timeout=4)
    if rc != 0:
        return 0.0
    s = (out or "").strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return 0.0
    parts = s.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return 0.0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return time.time() - (days * 86400 + secs)


_THREADS_CACHE = {"t": 0.0, "rows": []}


def _threads_for_cwd(cwd, started, window=240):
    """TUI 进程认领 threads 表里「这个目录、启动前后创建」的会话。

    新开一个会话时 codex 在启动瞬间建 thread（created_at ≈ 进程启动时刻），
    据此把进程和 sid 对上；同目录同时开两个也能靠时间差分开。
    没有 rollout（还没信任目录/还没开始）的不返回——那种会话本来就还不存在。"""
    if not cwd:
        return []
    now = time.time()
    if now - _THREADS_CACHE["t"] > 2:
        rows = []
        db = _state_db()
        if db is not None:
            try:
                import sqlite3
                con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2)
                try:
                    rows = con.execute(
                        "SELECT id,rollout_path,created_at,cwd FROM threads"
                        " WHERE archived=0").fetchall()
                finally:
                    con.close()
            except Exception:
                rows = []
        _THREADS_CACHE.update(t=now, rows=rows)
    out = []
    for sid, rp, created, cwd0 in _THREADS_CACHE["rows"]:
        if (cwd0 or "").rstrip("/") != cwd.rstrip("/"):
            continue
        if not rp or not Path(rp).exists():
            continue
        try:
            d = abs(float(created) - started)
        except (TypeError, ValueError):
            continue
        if started and d <= window:
            out.append((d, sid, Path(rp)))
    out.sort()
    return out



def _tty_path(t):
    t = (t or "").strip()
    if not t or t in ("?", "??"):
        return ""
    return t if t.startswith("/dev/") else "/dev/" + t


# ---- 跑完提醒 ----------------------------------------------------------------

def notify_event(d):
    """Codex notify 的 payload -> 与 Claude hook 同形的 Stop 事件。

    Codex 一轮跑完时把 JSON 追加在我们的 notify 命令最后：
      {"type":"agent-turn-complete","thread-id":…,"turn-id":…,"cwd":…,
       "input-messages":[…],"last-assistant-message":"…"}
    认不出来的返回 {}，调用方当空事件丢掉——绝不猜。"""
    if not isinstance(d, dict):
        return {}
    if str(d.get("type") or "") != "agent-turn-complete":
        return {}
    sid = str(d.get("thread-id") or d.get("thread_id") or "").strip()
    if not sid:
        return {}
    msg = re.sub(r"\s+", " ", str(d.get("last-assistant-message") or "")).strip()
    prompt = ""
    ims = d.get("input-messages") or []
    if isinstance(ims, list):
        for x in reversed(ims):
            if isinstance(x, str) and x.strip() and not x.strip().startswith(_INJECTED):
                prompt = re.sub(r"\s+", " ", x.strip())
                break
    _note_notify(sid, d.get("cwd") or "")
    return {"session_id": sid, "hook_event_name": "Stop", "agent": "codex",
            "cwd": str(d.get("cwd") or ""),
            "last_assistant_message": msg, "prompt": prompt[:4000]}


_NOTIFY_SEEN = {}    # sid -> (ts, cwd)：notify 见过活的 thread（桌面版兜底，见 discover）

# 会话最后一次是在哪儿用的：桌面版还是终端。召回想回原处——把桌面版的会话塞进
# 终端会撞上 App 的写入权（codex 直接进只读，手机端没法输入），反过来也一样别扭。
_ORIGIN_FILE = Path(__file__).resolve().parent / "session_origin.json"
_ORIGIN = {}
_ORIGIN_LOCK = threading.Lock()


def _origin_load():
    with _ORIGIN_LOCK:
        if not _ORIGIN and _ORIGIN_FILE.exists():
            try:
                _ORIGIN.update(json.loads(_ORIGIN_FILE.read_text(encoding="utf-8")) or {})
            except Exception:
                pass
        return _ORIGIN


def _origin_save():
    try:
        tmp = _ORIGIN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_ORIGIN, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ORIGIN_FILE)
    except Exception:
        pass


def note_origin(sid, origin):
    """记下「最后一次在哪儿用」。只在变了才落盘，发现每 2 秒跑一轮也不心疼。"""
    if not sid or origin not in ("desktop", "terminal"):
        return
    d = _origin_load()
    with _ORIGIN_LOCK:
        cur = d.get(sid) or {}
        if cur.get("o") == origin:
            return
        d[sid] = dict(cur, o=origin, at=int(time.time()))
        _origin_save()


def mark_desktop_open(sid, ttl=12 * 3600):
    """召回桌面版来源的会话：记成「在桌面版里开着」，discover 补一行出去，
    手机端据此按桌面版走（消息进 codex 队列），不会再被误开成终端。"""
    d = _origin_load()
    with _ORIGIN_LOCK:
        d[sid] = dict(d.get(sid) or {}, o="desktop", at=int(time.time()),
                      app_at=int(time.time()))
        _origin_save()


def thread_origin(path):
    """没见过活会话时，从 rollout 第一行猜来源：桌面版建的 thread，
    originator 是 codex_work_desktop；TUI/exec 算终端。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            pl = (json.loads(f.readline() or "{}").get("payload") or {})
        mark = ("%s %s" % (pl.get("originator") or "", pl.get("source") or "")).lower()
        return "desktop" if "desktop" in mark else "terminal"
    except Exception:
        return "terminal"


def last_origin(sid):
    """这个会话最后一次是在哪儿用的（记录优先，没有记录就看 rollout 是谁建的）。"""
    rec = (_origin_load().get(sid) or {}).get("o")
    if rec in ("desktop", "terminal"):
        return rec
    return thread_origin(rollout_path(sid) or "")


def _opened_desktop(ttl=12 * 3600):
    """最近在桌面版里召回过、还没过期的会话记录。"""
    _now = time.time()
    return [(_sid, _rec) for _sid, _rec in list(_origin_load().items())
            if (_rec or {}).get("app_at") and _now - _rec["app_at"] <= ttl]


def open_desktop(sid):
    """在桌面版里唤起。App/守护进程开着就只记一笔——消息走队列就能送达，
    和用户自己在桌面版里聊一样，不弹窗、不抢写入权；没开才拉起 App 并打开这个 thread。"""
    if _desk_server_running():
        mark_desktop_open(sid)
        return True, ""
    try:
        r = subprocess.run(["open", "codex://threads/%s" % sid],
                           capture_output=True, timeout=20)
    except Exception as e:
        return False, str(e)[:160]
    if r.returncode != 0:
        return False, (r.stderr or b"").decode("utf-8", "replace").strip()[:160]
    mark_desktop_open(sid)
    return True, ""


def _note_notify(sid, cwd):
    """notify 一到就记下这个 thread：桌面版的 rollout 归它自己的 app-server 管，
    lsof 不一定能看见，但完成事件一定经过我们。discover 拿它兜底。"""
    now = time.time()
    _NOTIFY_SEEN[str(sid)] = (now, str(cwd or ""))
    if len(_NOTIFY_SEEN) > 200:
        for k, v in list(_NOTIFY_SEEN.items()):
            if now - v[0] > 7200:
                _NOTIFY_SEEN.pop(k, None)


# ---- 适配器 ------------------------------------------------------------------

class Adapter(agents.AgentAdapter):
    key = "codex"
    label = "Codex"
    supports_fork = True
    eager_session = False     # 要等第一句话才建 thread，列表里先靠 pending 占位
    can_resume = True
    can_notify = True
    can_model_switch = True

    def __init__(self, notify_argv=None, ensure_script=None):
        """notify_argv：() -> [命令, 参数…]，Codex 的 notify 要执行的命令；
        None 表示这个平台不接提醒（Windows）。ensure_script：() -> None，
        装钩子前把上报脚本写新（token/端口变了也跟着走）。"""
        self._notify_argv = notify_argv
        self._ensure_script = ensure_script
        # 转发脚本路径必须和 hook.sh 里写死的那个一致（同一个函数算出来的）
        self._chain_path = notify_chain_path()

    def installed(self):
        # 有 CLI 算装了；没有 CLI 但 ~/.codex 在（装了桌面版 / 以前用过）也算——
        # 桌面版会话手机上能看能收提醒，列表顶上的切换条要它才会出现。
        # 不按 app 安装路径判断：macOS/Windows 各不一样，~/.codex 三个平台都一样。
        return codex_bin() is not None or codex_home().is_dir()

    def prepare_cwd(self, cwd):
        return ensure_trusted(cwd)

    # ---- 会话发现 ----
    def discover(self):
        if plat.IS_WIN:
            return []          # Windows 抓不到 tty/窗口，外部会话由 server 层另行处理
        rows = _ps_rows()
        if not rows:
            return []
        # 先把「跑着 codex、且开着 rollout」的进程挑出来：一个都没有就别去枚举窗口
        # （macOS 上那是 ~1s 的 osascript，没 codex 会话时白花）。
        cands = []
        for pid, r in rows.items():
            if not _is_codex_cmd(r["cmd"]):
                continue
            _note_bin(r["cmd"])
            if not _tty_path(r.get("tty")):
                # 交互会话一定有 tty。没有 tty 的是 codex 自己再拉起的子进程
                # （父链 codex<node_repl<node<codex），既定位不了窗口也发不了字，
                # 认进来只会多一条「无窗口」的死会话，还会把真会话的 sid 抢走。
                _note_notty(pid, r["cmd"])
                continue
            cwd0, p = _proc_info_via_lsof(pid)
            if p:
                cands.append((pid, r, p, False))
                continue
            # 0.167 的 rollout 归 app-server 守护进程所有，TUI 进程的 lsof 里没有。
            # resume/fork 的 sid 就写在命令行里；新会话按「同目录 + 启动时刻」认领 thread。
            sid = _sid_from_argv(r["cmd"])
            if sid:
                p = rollout_path(sid)
                if p:
                    cands.append((pid, r, p, False))
                continue
            for _d, _sid, p in _threads_for_cwd(cwd0, _started_at(pid)):
                cands.append((pid, r, p, False))
                break
        # Codex 桌面版（Codex.app）：完全没有 tty，thread 由它内置的 app-server 开着。
        # 提醒链路本来就收得到它（notify），但 discover 里没有 → 手机上看不到。
        # 这里按「命令行带 codex 的无 tty 进程，lsof 出它挂着的 rollout」认成无窗口会话：
        # 能看转录、能收提醒，发消息靠 caps 置空在手机端明确挡住。
        _n = 0
        for pid, r in rows.items():
            cmd = (r["cmd"] or "").lower()
            if "codex" not in cmd or _tty_path(r.get("tty")):
                continue
            _note_bin(r["cmd"])
            if "/contents/frameworks/" in cmd:
                # ChatGPT.app 的 Chromium 助手进程（Codex Framework / Aperitif）
                # 命令行里也带 codex，先到先得会把 8 个 lsof 名额吃光
                continue
            if any(x in cmd for x in ("exec-server", " mcp ", "update")):
                continue
            if _n >= 8:      # 每条 lsof ~20ms，别让奇怪的进程把发现拖慢
                break
            _n += 1
            for p in _rollouts_via_lsof(pid):
                cands.append((pid, r, p, True))
        # 兜底：lsof 看不见桌面版的 rollout 时（App 可能读完就关 fd），
        # 用 notify 里见过的 thread 补上。只认最近 2 小时（关掉的 CLI 会话也会
        # 留下 notify 记录，窗口太宽它们会以「桌面版」身份一直挂在列表里）。
        _now = time.time()
        for sid, (ts, _cwd) in list(_NOTIFY_SEEN.items()):
            if _now - ts > 7200:
                continue
            p = rollout_path(sid)
            if p:
                cands.append((0, {"ppid": 0, "tty": "??", "cmd": "codex (desktop)"}, p, True))
        opened = _opened_desktop()
        if not cands and not opened:
            return []
        wmap = {}
        try:
            wmap = plat.window_map() or {}
        except Exception:
            wmap = {}
        out, seen = [], set()
        for pid, r, p, desk in cands:
            sid = _sid_of(p)
            if not sid or sid in seen:
                continue
            src = ""
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    first = json.loads(f.readline() or "{}")
                pl = first.get("payload") or {}
                if first.get("type") == "session_meta":
                    src = pl.get("source")
                    if isinstance(src, dict):      # 新版可能是 {"type":"cli", …}
                        src = src.get("type") or ""
                    src = str(src or "")
            except Exception:
                pass
            if src == "exec" and _oneshot_cmd(r.get("cmd")):
                # `codex exec` 是一次性任务，跟 Claude 的 -p 一样不算交互会话。
                # 但 source 是写在线程创建时的：exec 起的线程后来被 `codex resume`
                # 在终端里接着跑，它已经是交互会话了——这时别按 source 丢掉，
                # 否则手机上点「召回」开了终端却永远显示已退出。
                continue
            tty = _tty_path(r.get("tty"))
            wid, wtitle = wmap.get(tty, (None, ""))
            if wid is None and not desk:
                # 经 wrapper 启动的会话：codex 在代理 pty 里，用父进程（wrapper）的 tty 找窗口
                par = rows.get(r.get("ppid") or 0)
                if par:
                    ptty = _tty_path(par.get("tty"))
                    wid2, t2 = wmap.get(ptty, (None, ""))
                    if wid2:
                        wid, wtitle = wid2, t2
                        tty = ptty
            cwd, title = rollout_meta(p)
            cand = title or Path(cwd or "").name or sid[:8]
            row = {
                "sid": sid, "short": sid[:8], "pid": pid, "cwd": cwd,
                "title": cand, "window_id": wid, "tty": tty,
                "state": rollout_state(p), "agent": self.key,
            }
            note_origin(sid, "desktop" if desk else "terminal")
            if desk:
                row["desktop"] = True      # 桌面版：只读，手机端据此写清楚原因
                # 桌面版能不能发消息全看这个：有进程就记下它的二进制。
                # 从 notify 兜底来的（没进程）说明 App 没在跑，发消息必然没门。
                if (sid, pid) not in _DESK_NOTE:
                    _DESK_NOTE.add((sid, pid))
                    print("[disc] 桌面版会话 %s 来源=%s" % (
                        sid[:8], ("进程 pid=%s" % pid) if pid else "notify 兜底（没有 codex 进程）"),
                        flush=True)
            out.append(row)
            seen.add(sid)
        # 刚在桌面版里召回的会话：App 不会一直挂着 rollout 的 fd（它可能只在
        # 内存/分页库里开着），discover 就看不见 → 手机端永远显示「已退出」。
        # 按记录补一条桌面版行，消息走队列（codex queue），跟桌面版里聊一样。
        for _sid, _rec in opened:
            if _sid in seen:
                continue
            _p = rollout_path(_sid)
            if not _p:
                continue
            _cwd, _title = rollout_meta(_p)
            out.append({
                "sid": _sid, "short": _sid[:8], "pid": 0, "cwd": _cwd,
                "title": _title or Path(_cwd or "").name or _sid[:8],
                "window_id": None, "tty": "", "state": rollout_state(_p) or "idle",
                "desktop": True, "agent": self.key,
            })
            seen.add(_sid)
        return out

    # ---- 转录 ----
    def transcript_path(self, cwd, sid):
        return rollout_path(sid) if sid else None

    def parse_transcript(self, path, limit=120):
        return parse_rollout(path, limit=limit)

    def transcript_meta(self, path):
        return rollout_meta(path)

    def closed_sessions(self, live_sids, limit=50):
        files = _rollout_files()
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        archived = _archived_ids()
        chat, batch = [], []
        for p in files:
            sid = _sid_of(p)
            if not sid or sid in live_sids or sid in archived:
                continue
            cwd, title = rollout_meta(p)
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            row = dict(sid=sid, short=sid[:8], kind="computer", pid=None,
                       cwd=cwd,
                       title=title or Path(cwd or "").name or ("会话 %s" % sid[:8]),
                       window_id=None, tty="", state="exited", last=mt,
                       full_color=False, resumable=True, agent=self.key)
            # 真人会话优先占名额，批量任务只补空位：流水线一轮几十个 codex exec，
            # mtime 全比聊过的会话新，纯按时间截断就把桌面版会话全挤没了。
            (batch if _thread_src(p) == "codex_exec" else chat).append(row)
            if len(chat) >= limit and len(batch) >= limit:
                break
        out = chat[:limit]
        if len(out) < limit:
            out += batch[:limit - len(out)]
        return out

    def delete_transcript(self, sid):
        """删掉这个会话：先让 codex 自己删（它会清 state 库里的行），再清残留文件。"""
        if not sid:
            return 0
        p = rollout_path(sid)
        existed = bool(p and p.exists())
        b = codex_bin()
        if b:
            # --force + UUID = 非交互也能删（否则 codex 要求 TTY 里二次确认）
            _run([b, "delete", "--force", sid], timeout=20)
        try:
            if p and p.exists():
                p.unlink()
                existed = True
        except OSError:
            pass
        _PATH_CACHE.pop(sid, None)
        _MISS.pop(sid, None)
        if p:
            _META.pop(str(p), None)
            _STATE.pop(str(p), None)
        return 1 if existed else 0

    # ---- 起进程 ----
    def launch_argv(self, cwd, sid=None, model=None):
        b = codex_bin()
        if not b:
            return None
        argv = [b]
        if sid:
            argv += ["resume", sid]
        model = (model or "").strip()
        if model and model not in ("默认", "default"):
            argv += ["--model", model]
        return argv

    def fork_argv(self, cwd, sid):
        b = codex_bin()
        if not b:
            return None
        return [b, "fork", sid]

    # ---- 提醒 hook：Codex 的 notify（一轮跑完时触发）----
    def _applicable(self):
        """这台机器该不该接 Codex 的提醒：平台支持 + （装过 codex 或用过 ~/.codex）。
        谁都不用 Codex 的话，别为了提醒给人凭空造一个 ~/.codex/config.toml。"""
        if self._notify_argv is None:
            return False
        return bool(codex_bin()) or self._cfg().parent.exists()

    def hook_spec(self):
        if not self._applicable():
            return []
        return [("Stop", None)]

    def _cfg(self):
        return codex_home() / "config.toml"

    def _our_notify(self):
        try:
            argv = self._notify_argv() if self._notify_argv else None
        except Exception:
            argv = None
        return list(argv) if argv else None

    def _read_notify(self, text):
        """config.toml 顶层的 notify。不用 tomllib：3.11 以下没有，
        而「读不出来」会被当成「没有 notify」，一装就把用户自己的转发静悄悄覆盖掉。"""
        return _toml_notify(text)

    def hook_install(self, on):
        """把 Codex 的 notify 接到 CodeBox 的上报脚本上（装/卸）。
        Codex 的 notify 只有一个命令：原来就有的先存成转发脚本，我们上报完再转给它。"""
        ours = self._our_notify()
        if not ours or not self._applicable():
            return 0          # 平台不支持 / 这台机器没用 Codex：没什么可装
        cfg = self._cfg()
        try:
            text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
        except OSError:
            return 1
        cur = self._read_notify(text)
        if on:
            if self._ensure_script:
                try:
                    self._ensure_script()      # 上报脚本每次都写新：端口/token 变了也跟上
                except Exception:
                    return 1
            if cur == ours:
                return 0
            if cur and cur != ours:
                try:
                    self._chain_path.parent.mkdir(parents=True, exist_ok=True)
                    self._chain_path.write_text(
                        "#!/bin/sh\n"
                        "# CodeBoss：Codex notify 转发脚本（--hooks install 生成）\n"
                        "# 这里保存的是安装 CodeBoss 之前你自己的 notify 命令。\n"
                        "exec %s \"$@\"\n" % " ".join(shlex.quote(x) for x in cur),
                        encoding="utf-8")
                    self._chain_path.chmod(0o700)
                    (self._chain_path.with_suffix(".json")).write_text(
                        json.dumps(cur, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    return 1
            new = _set_notify(text, ours)
            try:
                cfg.parent.mkdir(parents=True, exist_ok=True)
                if cfg.exists():
                    bak = cfg.with_name("config.toml.codeboss-bak")
                    if not bak.exists():
                        bak.write_text(text, encoding="utf-8")
                tmp = cfg.with_name("config.toml.cbtmp")
                tmp.write_text(new, encoding="utf-8")
                tmp.replace(cfg)
            except OSError:
                return 1
            return 0
        # 卸载：有转发脚本就还原成用户原来的 notify，否则把我们这行删掉
        bakj = self._chain_path.with_suffix(".json")
        old = None
        if bakj.exists():
            try:
                old = json.loads(bakj.read_text(encoding="utf-8"))
            except Exception:
                old = None
        if cur == ours or old is not None:
            new = _set_notify(text, old if isinstance(old, list) else None, mark=False)
            try:
                tmp = cfg.with_name("config.toml.cbtmp")
                tmp.write_text(new, encoding="utf-8")
                tmp.replace(cfg)
            except OSError:
                return 1
        for f in (self._chain_path, bakj):
            try:
                f.unlink()
            except OSError:
                pass
        return 0

    def hooks_installed(self):
        ours = self._our_notify()
        if not ours or not self._applicable():
            return True       # 不支持/用不到的：不参与自愈
        cfg = self._cfg()
        if not cfg.exists():
            return False
        try:
            return self._read_notify(cfg.read_text(encoding="utf-8")) == ours
        except OSError:
            return True       # 读不了就别乱动

    def hook_status(self):
        ours = self._our_notify()
        if not ours or not self._applicable():
            return ([], str(self._cfg()))
        return (["Stop"] if self.hooks_installed() else [], str(self._cfg()))

    # ---- 界面 ----
    def dialog_hints(self):
        # Codex 的 TUI 底栏文案（源码快照里核对过）：
        #   权限确认  press enter to confirm or esc to cancel
        #   选择列表  enter select · esc back
        #   提问      tab to add notes | enter to submit answer | esc to interrupt
        # 只收 Codex 自己的词：像 "↑/↓ to navigate"、"Esc to cancel" 这种跟 Claude
        # 的底栏一字不差，收进来就会把 Claude 的弹层认成 Codex 的（反过来也会）。
        return ("press enter to confirm", "esc to cancel", "esc to go back",
                "esc back", "enter select", "enter to submit",
                "tab to add notes", "to add notes", "esc to interrupt")

    def dialog_prefixes(self):
        return ("Press enter to confirm", "Press enter to go back",
                "enter select", "tab to add notes", "enter to submit")

    def agent_keys(self):
        return ()

    def model_info(self):
        cfg = self._cfg()
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            return ""
        try:
            import tomllib
            m = tomllib.loads(text).get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
        except Exception:
            pass
        m = re.search(r'(?m)^model\s*=\s*"([^"]+)"', text)
        return m.group(1).strip() if m else ""


# ---- config.toml 的最小读写 ---------------------------------------------------
# 不用第三方 TOML 写库：只动顶层的 notify 一行，其余内容一个字节都不改。

def _toml_str(s):
    return json.dumps(str(s), ensure_ascii=False)


_NOTIFY_RE = re.compile(r"(?m)^notify\s*=")
_NOTIFY_MARK = "# CodeBoss 跑完提醒（--hooks install 生成；删掉这行 = 停用）"


def _notify_span(text):
    """顶层 notify = … 这一行的 (行首下标, 值结束下标, 值文本)；没有就 None。
    值可以换行（数组），所以按方括号配平找结尾，跳过字符串里的括号。"""
    m = _NOTIFY_RE.search(text)
    if not m:
        return None
    i = m.end()
    depth, q, esc = 0, "", False
    while i < len(text):
        c = text[i]
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                i += 1
                break
        elif c == "\n" and depth == 0:
            break
        i += 1
    return (m.start(), i, text[m.end():i].strip())


def _arr_items(s):
    """TOML 字符串数组 -> [str]；有不认识的东西（数字/内嵌表）就 None。"""
    items, i, n = [], 0, len(s)
    s = s.strip()
    if s.startswith("["):
        s = s[1:]
    if s.endswith("]"):
        s = s[:-1]
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n:
                ch = s[j]
                if ch == "\\" and j + 1 < n:
                    e = s[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r",
                                '"': '"', "\\": "\\"}.get(e, e))
                    j += 2
                    continue
                if ch == '"':
                    j += 1
                    break
                buf.append(ch)
                j += 1
            items.append("".join(buf))
            i = j
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return None
            items.append(s[i + 1:j])
            i = j + 1
            continue
        return None
    return items


def _toml_valid(text):
    """能解析才准写回。3.11 以下没有 tomllib：没法校验时信任原逻辑（老平台）。"""
    try:
        import tomllib
    except ImportError:
        return True
    try:
        tomllib.loads(text)
        return True
    except Exception:
        return False


def ensure_trusted(cwd):
    """把目录记成 Codex 的 trusted（等价于在电脑前点「Trust and continue」）。

    手机端在某个目录开新会话/召回时，电脑前没人点信任框，codex 就停在
    「Trust this folder?」——连 thread 都还没建，列表里根本看不到这个会话。
    用户是明确选了这个目录才开的，和 Linux 上 _auto_trust_watch 自动回车同一语义。
    返回 True=已可信（含本来就可信）；False=写配置失败。

    同一个 [projects."目录"] 段出现两次 codex 会直接拒绝启动（duplicate key），
    所以这里不只要「加」，还要把重复段收成一个——之前 cc-switch/手工编辑留下的
    重复段就是一次实锤事故。"""
    if not cwd:
        return False
    cfg = codex_home() / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    except OSError:
        return False
    header = "projects.%s" % json.dumps(str(cwd), ensure_ascii=False)
    pat = re.compile(r"(?m)^\[" + re.escape(header) + r"\]\s*$")
    hits = list(pat.finditer(text))
    if hits:
        spans, trusted = [], False
        for m in hits:
            nxt = re.search(r"(?m)^\[", text[m.end():])
            end = m.end() + nxt.start() if nxt else len(text)
            body = text[m.end():end]
            if re.search(r'(?m)^trust_level\s*=\s*"trusted"\s*$', body):
                trusted = True
            spans.append((m.start(), end))
        if trusted and len(spans) == 1:
            return True
        keep, pos = [], 0
        for a, b in spans:
            keep.append(text[pos:a])
            pos = b
        keep.append(text[pos:])
        out = "".join(keep).rstrip("\n")
        new = (out + "\n\n" if out else "") + '[%s]\ntrust_level = "trusted"\n' % header
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + \
              '[%s]\ntrust_level = "trusted"\n' % header
    if not _toml_valid(new):
        print("[codex] 目录信任：新配置解析不过，放弃写入（原文件没动）：%s" % cwd, flush=True)
        return False
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.with_name("config.toml.cbtmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, cfg)
    except OSError:
        return False
    return True


def _toml_notify(text):
    """config.toml 顶层的 notify -> [str] 或 None。"""
    sp = _notify_span(text)
    if not sp:
        return None
    v = sp[2]
    if v.startswith("["):
        return _arr_items(v)
    try:
        got = _arr_items(v)
    except Exception:
        got = None
    return got            # 也接受单个字符串：notify = "cmd"


def _set_notify(text, argv, mark=True):
    """返回新的 config.toml 文本：argv 是列表就设置 notify，None 就删掉它。
    mark=False 用于「还原用户原来的 notify」——那是他自己的配置，别加我们的注释。"""
    block = ""
    if argv:
        if mark:
            block = _NOTIFY_MARK + "\n"
        block += "notify = [" + ", ".join(_toml_str(x) for x in argv) + "]"
    sp = _notify_span(text)
    if not sp:
        if not block:
            return text
        sep = "" if (not text or text.endswith("\n")) else "\n"
        return text + sep + ("\n" if text.strip() else "") + block + "\n"
    start, end, _v = sp
    i = end
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] == "\n":
        i += 1
    # notify 上面那一行如果是我们写的标记注释，这次一起换掉：
    # 不然每装/卸一次就多留一行注释，越积越多
    head = start
    nl = text.rfind("\n", 0, start)
    if nl >= 0:
        c_start = text.rfind("\n", 0, nl) + 1
        if text[c_start:nl].strip() == _NOTIFY_MARK:
            head = c_start
    if not block:
        out = text[:head] + text[i:]
        # 拼接处别凭空多一个空行（装的时候在那之前加过一行）
        if out.endswith("\n\n") and not text.endswith("\n\n"):
            out = out[:-1]
        return out
    return text[:head] + block + "\n" + text[i:]
