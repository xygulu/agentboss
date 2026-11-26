#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""opencode 适配器（agentboss v1 阶段1）。

探明结论（opencode 1.19.5，2026-11-17 本机实测 + 官方文档 opencode.ai/docs/plugins）：
- 插件：全局目录 ~/.config/opencode/plugins/（复数）下的 .js/.ts 启动时自动加载，
  不需要在 opencode.json 里注册；`opencode plugin` CLI 只是 npm 包安装器。
  （实施计划里写的单数 plugin/ 目录有误，以官方文档为准。）
- 事件挂法：插件导出一个 async 函数，返回 {"event": async ({event}) => ...}；
  event.type == "session.idle" 即一轮跑完。插件在 Bun 运行时里跑，fetch/fs 都可用。
- 库：~/.local/share/opencode/opencode.db（SQLite；session/message/part 表，
  message/part 的 data 列存 JSON）。只读打开：mode=ro 失败落 immutable=1 再试
  （库被写进程锁死时 -wal 也能读个大概）。
- 召回：opencode --session <id>（--fork 复制一份、-m provider/model 指模型）；
  opencode export <sid> 出整段 JSON（版本漂移最不敏感，转录首选走它）。

约定与 codex 适配器一致：认不出来的事件返回空，绝不猜；没装/读不了返回空列表，
调用方把「没装/不支持」显示出来。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import pathname2url

import agents
import plat

HERE = Path(__file__).resolve().parent

SESSION_EVENT_TYPE = "agentboss-opencode-idle"

_LOCK = threading.Lock()      # sqlite 句柄是一次性的，锁只护 _cols 探测缓存


def _db_path():
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode" / "opencode.db"


def _plugins_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode" / "plugins"


def _bin():
    return plat.which("opencode")   # 进程 PATH 在 launchd 下是空的，别用裸 shutil.which


def _port():
    """host 实际端口（logs/port.txt），没写就是默认 8790。"""
    try:
        return int((HERE / "logs" / "port.txt").read_text().strip())
    except (OSError, ValueError):
        return 8790


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
    print("[opencode] 会话库打不开（%s）——发现/转录这轮跳过" % p, flush=True)
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
    """跑着的 opencode 进程：ps 里 argv[0] 以 opencode 结尾的，带完整命令行
    （召回的会话 argv 里写着 --session <sid>，能和库里的行精确认领）。"""
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
        argv0 = cmd.strip().split(" ", 1)[0] if cmd.strip() else ""
        if argv0.endswith("opencode"):
            try:
                out.append((int(pid), cmd.strip()))
            except ValueError:
                pass
    return out


def _pid_started(pid):
    """进程启动时刻（epoch 秒）；问不出来返回 0（=老进程，别用它认领新会话）。"""
    try:
        r = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                           capture_output=True, timeout=5)
        return time.time() - float(r.stdout.decode().strip() or 0)
    except Exception:
        return 0.0


def _sid_from_argv(cmd):
    """命令行里的 --session/-s <sid>；没有就空。"""
    toks = str(cmd or "").split()
    for i, t in enumerate(toks[:-1]):
        if t in ("--session", "-s") and not toks[i + 1].startswith("-"):
            return toks[i + 1]
    return ""


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


class Ref:
    """转录「路径」替身：opencode 没有转录文件，sid 就是钥匙。

    server 各处对转录只做 p.stat().st_mtime（排序/指纹）和 parse_transcript(p)，
    所以替身只需要 stat().st_mtime 和 sid。"""

    def __init__(self, sid):
        self.sid = str(sid)

    def __str__(self):
        return "opencode:" + self.sid

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
    """Ref / "opencode:<sid>" / 裸 sid -> sid。"""
    if isinstance(x, Ref):
        return x.sid
    s = str(x or "")
    return s[len("opencode:"):] if s.startswith("opencode:") else s


class Adapter(agents.AgentAdapter):
    key = "opencode"
    label = "OpenCode"
    supports_fork = True
    eager_session = False       # 开进程时还没有会话 id，第一句话之后才有
    can_resume = True
    can_notify = True
    can_model_switch = False    # 手机端「切换模型」对 opencode 先不放开

    # ---- 会话发现 ----

    def discover(self):
        db = _opendb()
        if db is None:
            return []
        try:
            cols = _cols(db, "session")
            if "id" not in cols:
                return []
            sel = ",".join(c for c in ("id", "directory", "title", "model",
                                       "time_updated")
                           if c in cols)   # time_updated：年龄守卫要读它，缺了守卫就是死代码
            arch = (" AND time_archived IS NULL"
                    if "time_archived" in cols else "")
            order = ("time_updated DESC" if "time_updated" in cols
                     else "rowid DESC")
            out = []
            claimed = set()
            for pid, cmd in _live_pids():
                cwd = _pid_cwd(pid)
                if not cwd:
                    continue
                sid = _sid_from_argv(cmd)
                r = None
                if sid:
                    # argv 指定的一定是活的会话；归档条件只约束「按目录猜」的路
                    rows = _q(db, "SELECT %s FROM session WHERE id=?" % sel,
                              (sid,))
                    r = rows[0] if rows else None
                if r is None:
                    started = _pid_started(pid)
                    rows = _q(db, "SELECT %s FROM session WHERE directory=?%s "
                                  "ORDER BY %s LIMIT 8" % (sel, arch, order),
                              (cwd,))
                    for x in rows:
                        if x.get("id") in claimed:
                            continue
                        tu = x.get("time_updated")
                        try:
                            # time_updated 是 epoch 毫秒：会话得比进程新才是它的
                            if started and tu and float(tu) / 1000.0 < started - 2:
                                continue
                        except (TypeError, ValueError):
                            pass
                        r = x
                        break
                if r is None or not r.get("id") or r.get("id") in claimed:
                    continue
                claimed.add(r.get("id"))
                out.append(dict(
                    sid=r.get("id"), short=str(r.get("id") or "")[:8],
                    pid=pid, cwd=r.get("directory") or cwd,
                    title=r.get("title") or "",
                    window_id=None, tty="", state="busy", agent=self.key))
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
            return Ref(s) if _q(db, "SELECT id FROM session WHERE id=?", (s,)) else None
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
            rows = _q(db, "SELECT directory,title FROM session WHERE id=?", (s,))
            if not rows:
                return ("", "")
            return (rows[0].get("directory") or "", rows[0].get("title") or "")
        finally:
            db.close()

    def closed_sessions(self, live_sids, limit=50):
        """已退出的会话（可 --session 召回）：archived 行 + 没归档但不在活集合的行。

        实测 1.19.5（2026-11-17 验收）：杀掉 TUI 进程并不写 time_archived——
        只认归档会让被杀会话从列表上消失（发现靠 pid，pid 没了就没了）。"""
        db = _opendb()
        if db is None:
            return []
        try:
            cols = _cols(db, "session")
            if "id" not in cols:
                return []
            sel = ",".join(c for c in ("id", "directory", "title") if c in cols)
            order = ("time_updated DESC" if "time_updated" in cols
                     else "rowid DESC")
            try:
                mt = _db_path().stat().st_mtime
            except OSError:
                mt = 0
            live = {s for s in (live_sids or []) if s}
            params = ()
            if "time_archived" in cols:
                where = "time_archived IS NOT NULL"
            else:
                where = "1=1"
            if live:
                where = ("(%s OR id NOT IN (%s))"
                         % (where, ",".join("?" * len(live))))
                params = tuple(live)
            out = []
            for r in _q(db, "SELECT %s FROM session WHERE %s "
                            "ORDER BY %s LIMIT 200" % (sel, where, order), params):
                sid = r.get("id")
                if not sid:
                    continue
                cwd = r.get("directory") or ""
                out.append(dict(sid=sid, short=str(sid)[:8], kind="computer",
                                pid=None, cwd=cwd,
                                title=r.get("title") or Path(cwd).name or ("会话 %s" % str(sid)[:8]),
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
        # argv 用字面量 "opencode"（走 PATH 解析），不落绝对路径——nvm/volta 换环境就漂
        if not _bin():
            return None
        argv = ["opencode"]
        if sid:
            argv += ["--session", sid]
        model = (model or "").strip()
        if model and model not in ("默认", "default"):
            argv += ["-m", model]
        return argv

    def fork_argv(self, cwd, sid):
        if not _bin():
            return None
        return ["opencode", "--fork", "--session", sid]

    def parse_transcript(self, path, limit=120):
        return parse_transcript(path, limit=limit)

    # ---- 提醒 hook：全局插件目录（见模块头注释的探明结论）----

    def hook_spec(self):
        return [("session.idle", None)]

    def _applicable(self):
        """这台机器该不该接 opencode 的提醒：装过/用过才装，别给人凭空造目录。"""
        return bool(_bin()) or _db_path().exists() or _plugins_dir().parent.exists()

    def _hook_path(self):
        return _plugins_dir() / "agentboss-notify.js"

    def _hook_body(self):
        # token 只传路径不传值：插件运行期自己读 token.txt（与 hook.sh 同一不变量
        # ——密钥不落第二处、不进 0644 文件）；出错写一行进 host 的 logs/ 可排查。
        return _HOOK_JS.replace("__PORT__", str(_port())).replace(
            "__TOKEN_FILE__", str(HERE / "token.txt")).replace(
            "__LOG_FILE__", str(HERE / "logs" / "opencode-notify.log")).replace(
            "__ETYPE__", SESSION_EVENT_TYPE)

    def hook_install(self, on):
        if on:
            if not self._applicable():
                return 0          # 这台机器没用 opencode：没什么可装
            f = self._hook_path()
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(self._hook_body(), encoding="utf-8")
            except OSError:
                return 1
            return 0
        try:
            self._hook_path().unlink()
        except OSError:
            pass
        return 0

    def hooks_installed(self):
        if not self._applicable():
            return True           # 不支持/用不到的：不参与自愈
        f = self._hook_path()
        try:
            return f.exists() and f.read_text(encoding="utf-8") == self._hook_body()
        except OSError:
            return True           # 读不了就别乱动

    def hook_status(self):
        f = self._hook_path()
        return (["session.idle"] if f.exists() else [], str(f))


_HOOK_JS = '''// agentboss-notify：host（--hooks install）自动生成，勿手改——改了会被自愈接回。
// 监听 session.idle（一轮跑完）→ POST /api/session_event，复用 Claude 那条提醒链路。
// token 运行期从 token.txt 读——不烤进本文件（本文件 0644，密钥只住 token.txt 0600）。
// 出错写一行进 host 的 logs/opencode-notify.log：提醒静默丢失时来这排查。
import { readFileSync, appendFileSync } from "node:fs"
const TOKEN = (() => {
  try {
    for (const l of readFileSync("__TOKEN_FILE__", "utf8").split(/\\r?\\n/)) {
      const t = l.trim()
      if (t && !t.startsWith("#")) return t
    }
  } catch {}
  return ""
})()
const LOG = (m) => { try {
  appendFileSync("__LOG_FILE__", new Date().toISOString() + " " + m + "\\n")
} catch {} }
export const AgentbossNotify = async ({ client, directory }) => {
  return {
    event: async ({ event }) => {
      try {
        if (!event || event.type !== "session.idle") return
        const pr = event.properties || {}
        const sid = pr.sessionID || (pr.info && pr.info.sessionID) || ""
        if (!sid) return
        if (!TOKEN) { LOG("no token: token.txt 缺失或为空，跳过上报"); return }
        let last = ""
        try {
          const r = await client.session.messages({ sessionID: sid })
          const list = (r && r.data) || r || []
          const m = Array.isArray(list) ? list[list.length - 1] : null
          const info = (m && (m.info || m)) || {}
          if (info.role === "assistant") {
            const parts = (m && m.parts) || info.parts || []
            last = parts.filter(function (p) { return p && p.type === "text" })
              .map(function (p) { return p.text || "" }).join(" ").trim()
          }
        } catch (e) { LOG("messages: " + ((e && e.message) || e)) }
        await fetch("http://127.0.0.1:__PORT__/api/session_event", {
          method: "POST",
          headers: { "content-type": "application/json",
                     cookie: "codeboss_t=" + TOKEN },
          body: JSON.stringify({ type: "__ETYPE__", sid: sid,
                                 cwd: directory || pr.directory || "", last: last })
        })
      } catch (e) { LOG("hook: " + ((e && e.message) || e)) }
    },
  }
}
'''


# ---- 跑完提醒：插件 payload -> 与 Claude hook 同形的 Stop 事件 ----

def notify_event(d):
    if not isinstance(d, dict):
        return {}
    if str(d.get("type") or "") != SESSION_EVENT_TYPE:
        return {}
    sid = str(d.get("sid") or d.get("session_id") or "").strip()
    if not sid:
        return {}
    last = re.sub(r"\s+", " ", str(d.get("last") or "")).strip()
    prompt = re.sub(r"\s+", " ", str(d.get("prompt") or "")).strip()
    return {"session_id": sid, "hook_event_name": "Stop", "agent": "opencode",
            "cwd": str(d.get("cwd") or ""),
            "last_assistant_message": last, "prompt": prompt[:4000]}


# ---- 转录：export 子进程首选，失败回落直读 part 表 ----

def _export(sid):
    """opencode export <sid> -> JSON 文本；失败返回 ""。"""
    b = _bin()
    if not b:
        return ""
    try:
        r = subprocess.run([b, "export", sid], capture_output=True, timeout=15)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def _norm(role, parts):
    """(role, [part JSON]) -> {role, text, tools:[{name, detail}]}。"""
    text = []
    tools = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("type") or "")
        if t == "text" or (not t and p.get("text") is not None):
            text.append(str(p.get("text") or ""))
        elif t == "tool" or p.get("tool") or p.get("toolName"):
            name = p.get("tool") or p.get("toolName") or p.get("name") or "?"
            st = p.get("state") if isinstance(p.get("state"), dict) else {}
            detail = str(st.get("output") or st.get("summary")
                         or st.get("title") or st.get("input") or "")
            tools.append({"name": str(name), "detail": detail[:600]})
    return {"role": role or "?", "text": "\n".join(x for x in text if x).strip(),
            "tools": tools}


def _from_export(data):
    if isinstance(data, dict):
        data = data.get("messages") or data.get("parts") or []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        info = item.get("info") if isinstance(item.get("info"), dict) else item
        role = info.get("role") or item.get("role")
        parts = item.get("parts")
        if parts is None and isinstance(item.get("data"), dict):
            parts = item["data"].get("parts")
        out.append(_norm(role, parts))
    return out


def _from_db(sid):
    db = _opendb()
    if db is None:
        return []
    try:
        try:
            rows = db.execute(
                "SELECT m.id, m.data, p.data FROM part p "
                "JOIN message m ON m.id = p.message_id "
                "WHERE m.session_id=? ORDER BY m.rowid, p.rowid", (sid,)).fetchall()
        except sqlite3.Error:
            return []
        out = []
        cur_mid, cur_role, cur_parts = None, None, []
        for mid, mdata, pdata in rows:
            if mid != cur_mid:
                if cur_mid is not None:
                    out.append(_norm(cur_role, cur_parts))
                cur_mid, cur_parts = mid, []
                try:
                    j = json.loads(mdata or "{}")
                    cur_role = j.get("role") or (j.get("info") or {}).get("role")
                except Exception:
                    cur_role = None
            try:
                cur_parts.append(json.loads(pdata or "{}"))
            except Exception:
                pass
        if cur_mid is not None:
            out.append(_norm(cur_role, cur_parts))
        return out
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
            msgs = _from_export(json.loads(raw))
        except Exception:
            msgs = []
    if not msgs:
        msgs = _from_db(s)
    return msgs[-limit:] if limit else msgs


agents.register(Adapter())
