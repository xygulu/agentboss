# SPDX-License-Identifier: AGPL-3.0-or-later

"""Claude Code 转录（~/.claude/projects/<项目>/<sid>.jsonl）的定位和解析。

搬出 server.py 的原因：会话发现（agents.py 的 Claude 适配器）、看回话、
已退出会话召回、命令识别都读同一份 jsonl，格式细节散在三处；收在一处后
改解析逻辑只改这里，tests/test_transcript.py 直接喂 jsonl 断言。

`_claude_closed_sessions` 要用登记表（ext_registry），延迟导入 server 避免循环：
server.py 在顶部 import 本模块。
"""
import calendar
import json
import os
import re
import time
from pathlib import Path

IS_WIN = os.name == "nt"


def _db(*a, **kw):
    import server; return server._db(*a, **kw)


_TRANSCRIPT_META = {}      # 转录路径 → (mtime, cwd, 标题)：关掉的会话读一次就缓存住


def _transcript_meta(p):
    """从转录里取 cwd 和标题：优先 Claude 生成的 ai-title，其次首条用户消息（只读前 200 行）。"""
    try:
        mt = p.stat().st_mtime
    except OSError:
        return "", ""
    hit = _TRANSCRIPT_META.get(str(p))
    if hit and hit[0] == mt:
        return hit[1], (hit[2] or hit[3])
    cwd, ai, msg = "", "", ""
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 200:
                    break
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not ai and d.get("type") == "ai-title":
                    ai = (d.get("aiTitle") or "").strip()
                if not msg:
                    m = d.get("message")
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            txt = c
                        elif isinstance(c, list):
                            txt = " ".join(x.get("text", "") for x in c
                                           if isinstance(x, dict) and x.get("type") == "text")
                        else:
                            txt = ""
                        txt = txt.strip()
                        # 跳过 Claude Code 自己塞进来的 <local-command-*> / <command-*>、
                        # 续篇说明和「用户中断」通知：它们不是用户说的话，拿来当标题全是噪声
                        if (txt and not txt.startswith("<local-command-")
                                and not txt.startswith("<command-")
                                and not txt.startswith("This session is being continued")
                                and not txt.startswith("[Request interrupted by user]")):
                            msg = txt
                if cwd and ai:
                    break
    except OSError:
        pass
    ai = re.sub(r"\s+", " ", ai or "").strip()[:24]
    msg = re.sub(r"\s+", " ", msg or "").strip()[:24]
    _TRANSCRIPT_META[str(p)] = (mt, cwd, ai, msg)
    return cwd, (ai or msg)


_CONT_PARENT = {}          # 转录路径 → (mtime, 上一份的 sid)：续篇关系读一次就缓存住


def _first_ts(p):
    """转录首条带 timestamp 的条目 → 会话出生时间（epoch 秒）。读不到返回 0。"""
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 60:
                    break
                try:
                    ts = (json.loads(ln) or {}).get("timestamp")
                except Exception:
                    continue
                if ts:
                    return calendar.timegm(
                        time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass
    return 0


_TAIL_CWD = {}     # (路径, mtime) → 转录尾部 cwd：列表 5s 一轮，别每次都读盘

def _tail_cwd(p):
    """转录尾部的 cwd。会话搬家后（~/agentboss → aiboss 单仓），已退出列表该显示
    它现在住哪，不是出生地——出生地在文件头 200 行（_transcript_meta），够不着尾巴。"""
    try:
        mt = p.stat().st_mtime
    except OSError:
        return ""
    hit = _TAIL_CWD.get((str(p), mt))
    if hit is not None:
        return hit
    out = ""
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(0, end - 65536))
            buf = f.read().decode("utf-8", "replace")
        for ln in reversed(buf.splitlines()[1:]):    # 首行多半是半截
            try:
                cwd = (json.loads(ln) or {}).get("cwd")
            except Exception:
                continue
            if cwd:
                out = cwd
                break
    except OSError:
        pass
    _TAIL_CWD[(str(p), mt)] = out
    return out


def _continuation_parent(p):
    """这个会话是不是「上下文用完续开」的续篇：首条用户消息是续写说明，
    总结里带着上一份转录的路径。返回上一份的 sid（不是续篇就 None）。

    列表靠它把同一条对话的续篇和原件合成一条——否则手机上同名两条，
    点哪个都像点错（2026-11-20 PVE / BambuHelper 各一对）。"""
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _CONT_PARENT.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    parent = None
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 60:                     # 首条用户消息就在开头几行
                    break
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                m = d.get("message")
                if not (isinstance(m, dict) and m.get("role") == "user"):
                    continue
                c = m.get("content")
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    txt = " ".join(x.get("text", "") for x in c
                                   if isinstance(x, dict) and x.get("type") == "text")
                else:
                    txt = ""
                txt = txt.strip()
                if not txt or txt.startswith("<"):
                    continue
                if "continued from a previous conversation" in txt[:200]:
                    mm = re.search(r"/([0-9A-Za-z._-]+)\.jsonl", txt)
                    if mm and mm.group(1) != p.stem:
                        parent = mm.group(1)
                break                          # 只看首条真·用户消息
    except OSError:
        pass
    _CONT_PARENT[str(p)] = (mt, parent)
    return parent


def _claude_closed_sessions(live_sids, limit=50):
    """Claude 已关掉、但转录还在盘上的会话（可 --resume 召回），按最近活跃排。
    以 ~/.claude/projects 下的转录为准：登记表只记得开发机见过的会话，
    开发机没开着时关掉的旧会话压根不在表里（30 天后还被清掉）——关掉就再也召不回。"""
    reg = {}
    try:
        con = _db()
        rows = con.execute("SELECT sid,cwd,title FROM ext_registry").fetchall()
        con.close()
        reg = {r[0]: (r[1] or "", r[2] or "") for r in rows}
    except Exception:
        pass
    root = Path.home() / ".claude" / "projects"
    try:
        files = list(root.glob("*/*.jsonl"))     # 项目目录下一层的都是会话转录（subagents 在更深一层）
        files.sort(key=lambda q: q.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    info, parent, fpath = {}, {}, {}
    for p in files:                     # files 按 mtime 倒序：同一个 sid 多份时留最新的
        if p.stem in info:
            continue
        try:
            info[p.stem] = p.stat().st_mtime
        except OSError:
            info[p.stem] = 0
        fpath[p.stem] = p
        parent[p.stem] = _continuation_parent(p)
    # fork 和续篇在转录里长得一样（都带原件引用），活法不同：续篇是原件死透了才
    # resume；fork 是两边各聊各的，原件在 fork 出生后还活着。出生时间用转录首条
    # timestamp；原件在出生 10 分钟后仍有活动 ＝ 分道扬镳，拆开别合（2026-11-24
    # agentboss 被 fork 出的副本吞掉）。出生读不出来（=0）保守不拆。
    # ponytail: fork 后原件再没动过的极端情况与续篇不可分，仍会被合——要分辨得
    # 在 fork 时登记，不值得。
    for sid, q in list(parent.items()):
        b = _first_ts(fpath[sid]) if q else 0
        if q and b and info.get(q, 0) > b + 600:
            parent[sid] = None
    all_sids = set(info)

    def _head(sid):
        cur = sid
        for _ in range(20):             # 防御环
            q = parent.get(cur)
            if not q or q not in all_sids or q == cur:
                return cur
            cur = q
        return cur

    # 续篇和原件算同一条对话，只留链上最近活跃的一条（正在跑的算最新）
    best = {}
    for sid in all_sids:
        h = _head(sid)
        rank = (sid in live_sids, info[sid])
        if h not in best or rank > best[h][0]:
            best[h] = (rank, sid)
    keep = {v[1] for v in best.values()}

    out = []
    seen = set()
    for p in files:
        sid = p.stem
        if sid in live_sids or sid in seen or sid not in keep:
            continue          # 同一 sid 多一份转录（cwd 迁移过）/ 续篇已被代表取代
        seen.add(sid)
        cwd, title = _transcript_meta(p)
        rcwd, rtitle = reg.get(sid, ("", ""))
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        out.append(dict(sid=sid, short=sid[:8], kind="computer", pid=None,
                        cwd=_tail_cwd(p) or cwd or rcwd,
                        title=title or rtitle or Path(cwd or rcwd).name or ("会话 %s" % sid[:8]),
                        window_id=None, tty="", state="exited", last=mt,
                        full_color=False, resumable=True, agent="claude"))
        if len(out) >= limit:
            break
    return out


def project_jsonl(cwd, sid):
    """会话转录文件 `~/.claude/projects/<项目名>/<sid>.jsonl`。

    项目名是把 cwd 里的路径分隔符换成 '-'：Unix `/Users/a/p` → `-Users-a-p`；
    Windows `C:\\Users\\a\\p` → `C--Users-a-p`。各版本/平台的转义不完全一致，
    所以先按候选拼，拼不出来再按 sid 兜底扫描（sid 全局唯一）。"""
    base = Path.home() / ".claude" / "projects"
    cands = []
    if cwd:
        c = str(cwd)
        cands.append("-" + c.strip("/").replace("/", "-"))
        if IS_WIN or "\\" in c:
            cands.append(c.replace(":", "-").replace("\\", "-").strip("-"))
            cands.append("-" + c.replace(":", "").replace("\\", "-").strip("-"))
    for name in cands:
        p = base / name / (sid + ".jsonl")
        if p.exists():
            return p
    try:
        for d in base.iterdir():
            p = d / (sid + ".jsonl")
            if p.exists():
                return p
    except OSError:
        pass
    return None


def chain_head(sid, cwd=""):
    """续篇链最上面那条（--resume 出的新 sid 沿续写说明回溯到原件）。
    用户改的名字挂在原件 sid 上，列表合成一条后要接着用——不然改成
    「3dgenerator」的会话一到续篇就只剩窗口标题。链断了就停在能走到的那。"""
    cur, seen = sid, set()
    p = project_jsonl(cwd, cur)
    while p is not None and cur not in seen:
        seen.add(cur)
        q = _continuation_parent(p)
        if not q or q == cur:
            break
        cur = q
        p = project_jsonl(cwd, cur)
    return cur


def parse_transcript(path, limit=120, maxbytes=16 * 1024 * 1024):
    """jsonl -> [{role, text, tools:[{name, detail}]}]，跳过 sidechain/meta，合并连续同角色。
    只读文件尾部 maxbytes（够 limit 行用）：转录能长到 150MB+，全量 readlines 纯属自残。
    6MB/4000 行曾把聊天墙压到只剩几小时——一天的重活就能让转录越过 6MB（行均
    3KB 的工具大输出），16MB/8000 行实测全解析 ~60ms，不构成负担（2026-11-24）。"""
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
    for line in lines[-8000:]:
        line = line.strip()
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("isSidechain") or o.get("isMeta"):
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = o.get("message") or {}
        content = msg.get("content")
        texts, tools = [], []
        if isinstance(content, str):
            if content.strip():
                texts.append(content.strip())
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    tx = (b.get("text") or "").strip()
                    if tx:
                        texts.append(tx)
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    inp = b.get("input") or {}
                    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    detail = str(detail)
                    # 截断处补 …：截断命令跑不了，识别层靠它丢弃
                    item = {"name": name, "detail": detail[:160] + ("…" if len(detail) > 160 else "")}
                    if "bash" in name.lower():
                        # 界面显示用截断版，**命令识别要完整原文**：Claude 常跑多行
                        # heredoc，160 字一截就带 …，识别层只能整条丢掉。
                        item["cmd"] = detail
                    tools.append(item)
        if not texts and not tools:
            continue
        role = "user" if t == "user" else "assistant"
        if msgs and msgs[-1]["role"] == role and not tools and not msgs[-1]["tools"]:
            msgs[-1]["text"] += "\n" + "\n".join(texts)
        else:
            msgs.append({"role": role, "text": "\n".join(texts), "tools": tools})
    return msgs[-limit:]


_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def recent_files(path, maxbytes=320 * 1024, cap=40):
    """转录尾巴里 agent 真动手写过的文件，新→旧、去重、完整路径（不截断）。
    工头的「产出」用它：按 cwd 扫 mtime 会把日志/数据库当产出，还会漏掉
    写在会话 cwd 之外的（worktree、记忆目录）。读不到就返回空。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - maxbytes))
            blob = f.read().decode("utf-8", "ignore")
    except Exception:
        return []
    lines = blob.split("\n")
    if size > maxbytes and lines:
        lines = lines[1:]          # 掐掉开头可能截半的行
    seen, out = set(), []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("isSidechain") or o.get("isMeta") or o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in reversed(content):
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if b.get("name") not in _WRITE_TOOLS:
                continue
            fp = str((b.get("input") or {}).get("file_path") or "")
            if fp and fp not in seen:
                seen.add(fp)
                out.append(fp)
                if len(out) >= cap:
                    return out
    return out


def _transcript_cmds(msgs, limit=10):
    """会话转录（jsonl）里的 Bash tool_use 命令：结构化、完整、新→旧。
    cc 自己跑过的命令，作识别的补充来源。"""
    out = []
    for m in reversed(msgs):                       # 新消息在前
        for t in reversed(m.get("tools") or []):
            if "bash" not in (t.get("name") or "").lower():
                continue
            d = (t.get("cmd") or t.get("detail") or "").strip()   # 优先用完整命令
            if not d or d.endswith("…"):           # 截断命令跑不了
                continue
            if d not in out:
                out.append(d)
            if len(out) >= limit:
                return out
    return out
