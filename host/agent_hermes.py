#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""hermes 适配器（agentboss v1 阶段2）。

探明结论（hermes v0.15.1，2026-11-17 本机实测 + ~/.hermes/hermes-agent 源码/文档）：
- 库：~/.hermes/state.db（SQLite；sessions/messages 表，形状见 tests fixture）。
  started_at/ended_at/timestamp 都是 epoch 秒（浮点）——跟 opencode 的毫秒不一样。
  渠道会话（weixin 等）cwd IS NULL；TUI 会话 cwd 有值。archived=1 是自己归档的。
- 发现：ps 里认 hermes 进程（argv0 是 venv python，`-m hermes_cli.main` 或
  wrapper 脚本——匹配 argv0 basename==hermes / 命令行含 hermes_cli / hermes-agent），
  cwd 用 lsof，会话行按 cwd + started_at >= 进程启动-2s 认领（只认 TUI 行）。
- 召回：hermes chat --resume <SESSION_ID>（-c/--continue [NAME] 是按名字）。
- 转录：hermes sessions export --session-id <sid> - 出 JSONL（首行 = 会话 +
  messages 数组；role: user/assistant/tool/session_meta）；失败回落直读 messages 表。
- hook：~/.hermes/config.yaml 顶层 hooks: 块（shell hooks），schema：
    hooks:
      <event>:                    # on_session_end 等VALID_HOOKS
      - command: "<shell 命令>"   # shlex.split 直接执行（shell=False），可带参数
        timeout: 5                # 可选，默认 60 上限 300
  on_session_end 每轮 run_conversation 结尾都发（≈一轮跑完），payload 是 JSON 从
  stdin 进来：{hook_event_name, session_id, cwd, extra:{completed, interrupted, ...}}——
  没有 last 消息内容，hook.sh 原样转发、notify_event 在服务端翻译。
  ⚠️ 首次使用要同意（consent）：hermes 启动时按 (event, command) 查
  ~/.hermes/shell-hooks-allowlist.json，没记录会弹 TUI 确认（--accept-hooks 跳过）。
  非交互启动没同意 = hook 被跳过，不是报错。hooks doctor 还查脚本 mtime 漂移。

约定与 codex/opencode 适配器一致：认不出来的事件返回空，绝不猜；没装/读不了
返回空列表，调用方把「没装/不支持」显示出来。
"""
import json
import os
import re
import shutil
import sqlite3
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import pathname2url

try:
    import fcntl
except ImportError:          # Windows：没有 flock，退回不加锁
    fcntl = None

import agents
import plat

SESSION_EVENT_TYPE = "agentboss-hermes-stop"

SESSION_HOOK_EVENT = "on_session_end"


def _home():
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def _db_path():
    return _home() / "state.db"


def _config_path():
    return _home() / "config.yaml"


def _bin():
    return plat.which("hermes")   # 进程 PATH 在 launchd 下是空的，别用裸 shutil.which


def _opendb():
    """只读打开。先 mode=ro，被锁死落 immutable=1，再不行认命返回 None。"""
    p = _db_path()
    if not p.exists():
        return None
    base = "file:" + pathname2url(str(p))
    for uri in (base + "?mode=ro", base + "?immutable=1"):
        try:
            return sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.Error:
            continue
    print("[hermes] 会话库打不开（%s）——发现/转录这轮跳过" % p, flush=True)
    return None


def _cols(db, table):
    try:
        return {r[1] for r in db.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return set()


def _q(db, sql, params=()):
    try:
        cur = db.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _live_pids():
    """跑着的 hermes 进程。三种形态都见过：wrapper argv0=hermes、venv console
    script（argv1=…/bin/hermes）、`python -m hermes_cli.main …`（网关实测）。"""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, timeout=5)
        lines = r.stdout.decode("utf-8", "replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        pid, _, cmd = ln.partition(" ")
        cmd = cmd.strip()
        argv0 = cmd.split(" ", 1)[0] if cmd else ""
        if (argv0.endswith("hermes") or "hermes_cli" in cmd
                or "/hermes-agent/" in argv0):
            try:
                out.append((int(pid), cmd))
            except ValueError:
                pass
    return out


def _pid_started(pid):
    try:
        r = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                           capture_output=True, timeout=5)
        return time.time() - float(r.stdout.decode().strip() or 0)
    except Exception:
        return 0.0


def _pid_cwd(pid):
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                           capture_output=True, timeout=5)
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            if ln.startswith("n/"):
                return ln[1:]
    except Exception:
        return None
    return None


def _resume_sid(cmd):
    """命令行里的 --resume <sid>；没有就空（-c 按名字认，不猜）。"""
    toks = str(cmd or "").split()
    for i, t in enumerate(toks[:-1]):
        if t == "--resume" and not toks[i + 1].startswith("-"):
            return toks[i + 1]
    return ""


def _pid_tty(pid):
    try:
        r = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                           capture_output=True, timeout=5)
        t = r.stdout.decode().strip()
        return ("/dev/" + t) if t and t != "??" else ""
    except Exception:
        return ""


class Ref:
    """转录「路径」替身（opencode 同款）：sid + state.db 的 mtime。"""

    def __init__(self, sid):
        self.sid = str(sid)

    def __str__(self):
        return "hermes:" + self.sid

    class _St:
        st_mtime = 0.0
        st_mtime_ns = 0
        st_size = 0

    def stat(self):
        # server 吃的不止 st_mtime（推送循环 msig=(st_mtime, st_size)、
        # _tr_load=(st_mtime_ns, st_size)）：库在位直接给真 os.stat_result，
        # 缺席才用替身（补齐全部被吃的属性，AttributeError 会崩掉 SSE 连接）。
        try:
            return _db_path().stat()
        except OSError:
            return Ref._St()


def _sid_of(x):
    if isinstance(x, Ref):
        return x.sid
    s = str(x or "")
    return s[len("hermes:"):] if s.startswith("hermes:") else s


# ---- config.yaml hooks: 合并（纯函数，文本手术不重排用户内容）----

def _hook_command():
    import hooks       # HOOK_SH 是 hooks 模块的常量；运行期生成、带本机绝对路径
    return "%s hermes" % hooks.HOOK_SH


def _hooks_region(text):
    """顶层 hooks: 块的 (起行号, 终行号)（不含下一个顶层键）；没有返回 None。"""
    lines = text.splitlines(True)
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"hooks:", ln):
            start = i
            break
        if ln.strip() and not ln[0].isspace() and start is None and re.match(r"\S", ln):
            continue
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and re.match(r"\S", lines[j]):
            end = j
            break
    return (start, end)


_OURS = "hook.sh hermes"      # 我们的条目识别串（command 里必有）


def hermes_merge_hooks(config_text, install):
    """往顶层 hooks: 映射加/删 agentboss 条目；其余内容逐字节保留。幂等。

    只认两种 hooks 形状：`hooks: {}`（空 flow，本机现状）和块状列表。
    其它 flow 形状（`hooks: {a: b}`）不动、原样返回——纯文本手术不赌 YAML。"""
    text = config_text if config_text.endswith("\n") or not config_text \
        else config_text + "\n"
    lines = text.splitlines(True)
    reg = _hooks_region(text)
    ours = []

    def _is_ours(ln):
        return _OURS in ln and re.match(r"\s*-\s*command:", ln)

    if reg:
        s, e = reg
        seg = lines[s:e]
        if len(seg) == 1 and re.match(r"\s*hooks:\s*\{.+\}\s*$", seg[0]):
            return config_text   # 非空 flow 风格：块状条目插进去必是非法 YAML，不动
        # 区块里挑出我们的行（含紧随的更深缩进续行，如 timeout）——下标相对 seg
        for i, ln in enumerate(seg):
            if _is_ours(ln):
                j = i + 1
                while j < len(seg) and seg[j].strip() and not re.match(r"\s*-\s", seg[j]) \
                        and re.match(r"\s{4,}", seg[j]):
                    j += 1
                ours.append((i, j))
        body = [ln for idx, ln in enumerate(seg)
                if not any(lo <= idx < hi for lo, hi in ours)]
        if install:
            entry = ['- command: "%s"\n' % _hook_command(),
                     '  timeout: 5\n']
            ind = " " * 2
            # 找 on_session_end: 键；没有就在块开头插一个
            key = None
            for k, ln in enumerate(body):
                if re.match(r"  %s:" % SESSION_HOOK_EVENT, ln):
                    key = k
                    break
            if key is not None:
                # 插到该事件列表最后一条之后
                pos = key + 1
                while pos < len(body) and (body[pos].startswith("  - ")
                                           or (body[pos].strip()
                                               and body[pos].startswith("    "))):
                    pos += 1
                body[pos:pos] = [ind + x for x in entry]
            else:
                if len(body) == 1 and re.match(r"hooks:\s*\{\s*\}\s*$", body[0]):
                    body = ["hooks:\n",
                            "  %s:\n" % SESSION_HOOK_EVENT]
                else:
                    body.insert(1, "  %s:\n" % SESSION_HOOK_EVENT)
                body[2:2] = [ind + x for x in entry]
        elif ours:
            # 没了列表项的 on_session_end: 键一并删；hooks: 下什么都不剩就归一成
            # `hooks: {}`（本机 config.yaml 的原状）
            body = [ln for idx, ln in enumerate(body)
                    if not (re.match(r"  %s:\s*$" % SESSION_HOOK_EVENT, ln)
                            and not (idx + 1 < len(body)
                                     and re.match(r"\s*- ", body[idx + 1])))]
            if not [ln for ln in body[1:] if ln.strip()]:
                body = ["hooks: {}\n"]
        lines[s:e] = body
        return "".join(lines)
    # 没有顶层 hooks: 块
    if not install:
        return config_text
    block = ("hooks:\n"
             "  %s:\n" % SESSION_HOOK_EVENT +
             "  - command: \"%s\"\n" % _hook_command() +
             "    timeout: 5\n")
    return text + ("\n" if text and not text.endswith("\n") else "") + block \
        if text else block


def _allowlist_path():
    return _home() / "shell-hooks-allowlist.json"


def our_hook_approved():
    """hermes 的同意名单里有没有我们自己这一条 (on_session_end, hook.sh hermes)。"""
    try:
        data = json.loads(_allowlist_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cmd = _hook_command()
    return any(isinstance(e, dict) and e.get("event") == SESSION_HOOK_EVENT
               and e.get("command") == cmd
               for e in (data.get("approvals") or []))


def _hook_mtime_iso():
    """hook.sh 的 UTC mtime（ISO），跟 hermes 自己记的格式一致。"""
    try:
        script = shlex.split(_hook_command())[0]
        return datetime.fromtimestamp(
            os.path.getmtime(os.path.expanduser(script)), tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    except (OSError, IndexError, ValueError):
        return None


def approve_our_hook():
    """把我们那条 hook 记进同意名单，等于替老板在这条上按了 y。

    手机召回没有 TTY，hermes 首次见到 hook 弹的 y/N 没人能按 → hook 被跳过、
    提醒静默消失。不用 --accept-hooks：那是全局放行，用户自己配的还没见过的
    hook 也会一起批。这里只批我们这一条，别的记录原样保留。幂等。

    跟 hermes 自己的 _record_approval 一样拿 .lock 上的 flock 做读改写，
    免得和正在跑的 hermes 同时写名单互删记录。"""
    p = _allowlist_path()
    cmd = _hook_command()
    entry = {"event": SESSION_HOOK_EVENT, "command": cmd,
             "approved_at": datetime.now(tz=timezone.utc).isoformat()
             .replace("+00:00", "Z"),
             "script_mtime_at_approval": _hook_mtime_iso()}
    lock = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = open(p.with_name(p.name + ".lock"), "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("approvals"), list):
            data = dict(data) if isinstance(data, dict) else {}
            data["approvals"] = []
        old = [e for e in data["approvals"]
               if not (isinstance(e, dict) and e.get("event") == SESSION_HOOK_EVENT
                       and e.get("command") == cmd)]
        if len(old) < len(data["approvals"]):
            return True                     # 已在名单里，不重写文件
        data["approvals"] = old + [entry]
        tmp = p.with_name(p.name + ".abtmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except (OSError, ValueError):
        return False
    finally:
        if lock is not None:
            lock.close()
    return True


class Adapter(agents.AgentAdapter):
    key = "hermes"
    label = "Hermes"
    supports_fork = False       # hermes 没有 fork 语义
    eager_session = False       # 起进程时还没有会话 id
    can_resume = True
    can_notify = True
    can_model_switch = False

    # ---- 会话发现 ----

    def discover(self):
        """hermes 的库不写 cwd（v0.15.1 实测 398 行全空）、TUI 会话的记号是
        source='cli'。认领三路：argv --resume <sid> 精认领（行可以比进程老，
        resume 本来就是新进程续老会话）；其余 TUI 进程按启动新→老配剩余行新→老
        （新进程领最新行）；gateway（渠道会话的宿主）进程不认领 cli 行。"""
        db = _opendb()
        if db is None:
            return []
        try:
            cols = _cols(db, "sessions")
            if "id" not in cols:
                return []
            sel = ",".join(c for c in ("id", "cwd", "title", "model", "started_at")
                           if c in cols)
            if "source" not in cols:
                # source 列没了（升版改 schema）= 读不懂 TUI/渠道的判别键：
                # 显式降级返回空，兜底 1=1 会把微信等渠道隐私会话泄进手机列表。
                return []
            srcf = "COALESCE(source,'') IN ('cli','')"
            archf = " AND COALESCE(archived,0)=0" if "archived" in cols else ""
            rows = _q(db, "SELECT %s FROM sessions WHERE %s%s "
                          "ORDER BY started_at DESC LIMIT 64" % (sel, srcf, archf))
            by_id = {r.get("id"): r for r in rows if r.get("id")}
            started = {}
            claimed = {}
            procs = []
            for pid, cmd in _live_pids():
                started[pid] = _pid_started(pid)
                if "gateway" in cmd:      # 渠道宿主，别来抢 TUI 会话
                    continue
                sid = _resume_sid(cmd)
                if sid and sid in by_id and sid not in claimed:
                    claimed[sid] = pid
                    continue
                cwd = _pid_cwd(pid)
                if not cwd:
                    continue
                procs.append((pid, cwd))
            procs.sort(key=lambda x: started.get(x[0]) or 0, reverse=True)
            for pid, cwd in procs:
                st = started.get(pid) or 0
                for x in rows:
                    if x.get("id") in claimed:
                        continue
                    v = x.get("started_at")
                    try:
                        # 会话得比进程新才是它的（新进程开新会话）
                        if st and v and float(v) < st - 2:
                            continue
                    except (TypeError, ValueError):
                        pass
                    claimed[x["id"]] = pid
                    break
            out = []
            for sid, pid in claimed.items():
                r = by_id[sid]
                out.append(dict(
                    sid=sid, short=str(sid)[:8], pid=pid,
                    cwd=r.get("cwd") or (_pid_cwd(pid) or ""),
                    title=r.get("title") or "",
                    window_id=None, tty=_pid_tty(pid),
                    state="busy", agent=self.key))
            return out
        finally:
            db.close()

    # ---- 转录 ----

    def transcript_path(self, cwd, sid):
        s = _sid_of(sid)
        if not s:
            return None
        db = _opendb()
        if db is None:
            return None
        try:
            return Ref(s) if _q(db, "SELECT id FROM sessions WHERE id=?", (s,)) else None
        finally:
            db.close()

    def transcript_meta(self, path):
        s = _sid_of(path)
        if not s:
            return ("", "")
        db = _opendb()
        if db is None:
            return ("", "")
        try:
            rows = _q(db, "SELECT cwd,title FROM sessions WHERE id=?", (s,))
            if not rows:
                return ("", "")
            return (rows[0].get("cwd") or "", rows[0].get("title") or "")
        finally:
            db.close()

    def closed_sessions(self, live_sids, limit=50):
        """已退出可召回：TUI 会话（source='cli'）、未归档、不在活集合。
        渠道会话（weixin/api_server/cron）永不出；cwd 列 hermes 不写，标题照旧。"""
        db = _opendb()
        if db is None:
            return []
        try:
            cols = _cols(db, "sessions")
            if "id" not in cols:
                return []
            sel = ",".join(c for c in ("id", "cwd", "title") if c in cols)
            try:
                mt = _db_path().stat().st_mtime
            except OSError:
                mt = 0
            live = {s for s in (live_sids or []) if s}
            if "source" not in cols:
                return []   # 同 discover：读不懂就显式降级，别泄渠道会话
            where = "COALESCE(source,'') IN ('cli','')"
            if "archived" in cols:
                where += " AND COALESCE(archived,0)=0"
            params = ()
            if live:
                where += " AND id NOT IN (%s)" % ",".join("?" * len(live))
                params = tuple(live)
            out = []
            for r in _q(db, "SELECT %s FROM sessions WHERE %s "
                            "ORDER BY started_at DESC LIMIT 200" % (sel, where), params):
                sid = r.get("id")
                if not sid:
                    continue
                out.append(dict(sid=sid, short=str(sid)[:8], kind="computer",
                                pid=None, cwd=r.get("cwd") or "",
                                title=r.get("title") or ("会话 %s" % str(sid)[:8]),
                                window_id=None, tty="", state="exited", last=mt,
                                full_color=False, resumable=True, agent=self.key))
                if len(out) >= limit:
                    break
            return out
        finally:
            db.close()

    # ---- 起进程 ----

    def installed(self):
        return bool(_bin())

    def launch_argv(self, cwd, sid=None, model=None):
        if not _bin():
            return None
        argv = ["hermes", "chat"]
        if sid:
            argv += ["--resume", sid]
        model = (model or "").strip()
        if model and model not in ("默认", "default"):
            argv += ["-m", model]
        return argv

    def fork_argv(self, cwd, sid):
        return None               # hermes 没有 fork

    def parse_transcript(self, path, limit=120):
        return parse_transcript(path, limit=limit)

    # ---- 提醒 hook：config.yaml 顶层 hooks:（schema 见模块头注释）----

    def hook_spec(self):
        return [(SESSION_HOOK_EVENT, None)]

    def _applicable(self):
        """这台机器该不该接 hermes 的提醒：装过/用过才装，别给人凭空造目录。"""
        return bool(_bin()) or _home().exists()

    def hook_install(self, on):
        if on and not self._applicable():
            return 0          # 这台机器没用 hermes：没什么可装
        p = _config_path()
        try:
            text = p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            return 1
        new = hermes_merge_hooks(text, on)
        # 配置没变也要补同意：老版本只写了 config.yaml，名单一直空着，
        # 手机召回弹出的 y/N 没人能按（2026-11-22 用户）。
        if on and not approve_our_hook():
            return 1
        if new == text:
            return 0
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if on:
                bak = p.with_name(p.name + ".agentboss-bak")
                if not bak.exists():        # 留装之前那份，别拿我们自己的改写覆盖
                    bak.write_text(text, encoding="utf-8")
            tmp = p.with_name(p.name + ".abtmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(p)
        except OSError:
            return 1
        return 0

    def hooks_installed(self):
        if not self._applicable():
            return True           # 不支持/用不到的：不参与自愈
        try:
            text = _config_path().read_text(encoding="utf-8")
        except OSError:
            return True           # 读不了就别乱动
        return hermes_merge_hooks(text, True) == text and our_hook_approved()

    def hook_status(self):
        p = _config_path()
        try:
            installed = hermes_merge_hooks(p.read_text(encoding="utf-8"), True) == p.read_text(
                encoding="utf-8")
        except OSError:
            installed = False
        return ([SESSION_HOOK_EVENT] if installed else [], str(p))


# ---- 跑完提醒：hook stdin payload -> 与 Claude hook 同形的 Stop 事件 ----

def notify_event(d):
    if not isinstance(d, dict):
        return {}
    t = str(d.get("type") or "")
    hen = str(d.get("hook_event_name") or "")
    if t == SESSION_EVENT_TYPE:
        sid = str(d.get("sid") or d.get("session_id") or "").strip()
        last = re.sub(r"\s+", " ", str(d.get("last") or "")).strip()
        prompt = re.sub(r"\s+", " ", str(d.get("prompt") or "")).strip()
    elif hen == SESSION_HOOK_EVENT:
        # hook.sh 原样转发的 hermes payload：没有 last 消息内容（extra 只有
        # completed/interrupted/model/platform），别编造。
        sid = str(d.get("session_id") or "").strip()
        last = ""
        prompt = ""
    else:
        return {}
    if not sid:
        return {}
    return {"session_id": sid, "hook_event_name": "Stop", "agent": "hermes",
            "cwd": str(d.get("cwd") or ""),
            "last_assistant_message": last, "prompt": prompt[:4000]}


# ---- 转录：sessions export 子进程首选，失败回落直读 messages 表 ----

def _export(sid):
    """hermes sessions export --session-id <sid> -> JSONL 文本；失败返回 ""。"""
    b = _bin()
    if not b:
        return ""
    try:
        r = subprocess.run([b, "sessions", "export", "--session-id", sid, "-"],
                           capture_output=True, timeout=15)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def _tool_calls_of(m):
    """assistant 的 tool_calls -> [{name, detail}]（detail=入参，占位；结果来了会覆盖）。"""
    out = []
    tc = m.get("tool_calls")
    if isinstance(tc, str):
        try:
            tc = json.loads(tc)
        except Exception:
            tc = []
    for c in tc if isinstance(tc, list) else []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        out.append({"name": str(fn.get("name") or c.get("tool_name") or "?"),
                    "detail": str(fn.get("arguments") or "")[:600]})
    return out


def _assemble(raw_msgs):
    """消息数组 -> [{role, text, tools}]；tool 结果折进前一条 assistant 的工具项
    （detail 用结果内容——和 opencode 的 state.output 同思路），session_meta 跳过。"""
    out = []
    for m in raw_msgs if isinstance(raw_msgs, list) else []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role == "user":
            out.append({"role": "user", "text": str(m.get("content") or "").strip(),
                        "tools": []})
        elif role == "assistant":
            out.append({"role": "assistant", "text": str(m.get("content") or "").strip(),
                        "tools": _tool_calls_of(m)})
        elif role == "tool" and out and out[-1]["role"] == "assistant":
            name = str(m.get("tool_name") or "?")
            detail = str(m.get("content") or "")[:600]
            for t in reversed(out[-1]["tools"]):
                if t["name"] == name:
                    t["detail"] = detail
                    break
            else:
                out[-1]["tools"].append({"name": name, "detail": detail})
    return out


def _from_export(raw):
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("messages"), list):
            return _assemble(d["messages"])
    return []


def _from_db(sid):
    db = _opendb()
    if db is None:
        return []
    try:
        try:
            rows = db.execute(
                "SELECT role, content, tool_calls, tool_name FROM messages "
                "WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        except sqlite3.Error:
            return []
        return _assemble([dict(zip(("role", "content", "tool_calls", "tool_name"), r))
                          for r in rows])
    finally:
        db.close()


def parse_transcript(path, limit=120):
    """sid -> [{role, text, tools}]。export 失败/为空回落直读库。"""
    s = _sid_of(path)
    if not s:
        return []
    msgs = []
    try:
        raw = _export(s)
    except Exception:
        raw = ""
    if raw:
        try:
            msgs = _from_export(raw)
        except Exception:
            msgs = []
    if not msgs:
        msgs = _from_db(s)
    return msgs[-limit:] if limit else msgs


agents.register(Adapter())
