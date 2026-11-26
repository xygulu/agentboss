# SPDX-License-Identifier: AGPL-3.0-or-later

"""工头事实层（M1）：把会话、审批、提醒历史投影成「活」。

原则：**状态只由事实算，模型只负责怎么说**。M1 的句子全是确定性事实句；
M2 的表达层（本文件末尾）在状态跃迁时调一次中继小模型换说法，不改状态，
失败/超预算就退回事实句并标出来。

数据不另存一份真值：活 = 现有会话/审批/提醒的投影；这里只留两样东西——
  · verdicts：老板「收下/退回」的动作（供后续学习和下次不重复交）
  · state：每条活上一次的状态（用来把状态跃迁写进 events.jsonl）
"""
import json
import re
import time
from pathlib import Path

HOME = Path.home() / ".codeboss"
UI_CFG = HOME / "boss.json"
WORK_DIR = HOME / "boss"
STATE_PATH = WORK_DIR / "state.json"
EVENTS_PATH = WORK_DIR / "events.jsonl"
SAYS_PATH = WORK_DIR / "says.json"
BRAIN_PATH = WORK_DIR / "brain.json"

DONE_WINDOW = 24 * 3600      # 已交活只回看一天：一周前的成果不该一直挂在板上
RUN_RECENT = 300             # 5 分钟内还有输出 = 在跑；更久只当「停在上一轮」
RUN_STALE = 1800             # 适配器报 busy 但半小时没输出：当它停了，别写「在跑着」
NOW_MAX = 4                  # 在跑；停在上一轮的另算一块（等你吩咐）
IDLE_MAX = 4
DONE_MAX = 8


def ui_enabled(dev="", cfg_path=None):
    """这台 host 上要不要给这台手机开「工头（新）」。
    默认关；host 上放 boss.json = {"enabled": true, "devs": []} 才开，
    devs 非空时只认列出来的手机（灰度）。"""
    p = Path(cfg_path) if cfg_path else UI_CFG
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return False
    devs = cfg.get("devs") or []
    return (not devs) or (str(dev or "") in {str(x) for x in devs})


def load_state(path=None):
    p = Path(path) if path else STATE_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("verdicts", {})
    d.setdefault("states", {})
    return d


def _save_state(d, path=None):
    p = Path(path) if path else STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def set_verdict(sid, verdict, text="", path=None):
    """老板对一件成果的动作：accepted（收下）/ rejected（退回）。
    只记动作和时刻；真正「发回原会话」由调用方走既有发消息通道。"""
    sid = str(sid or "")
    if not sid or verdict not in ("accepted", "rejected"):
        return False
    d = load_state(path)
    d["verdicts"][sid] = {"verdict": verdict, "ts": time.time(),
                          "text": str(text or "")[:200]}
    _save_state(d, path)
    return True


_SCRATCH_ROOTS = ("/tmp", "/private/tmp", "/private/var/folders", "/var/folders",
                  "/dev/shm")


def is_scratch(cwd):
    """临时目录 / host 自己的 scratch（~/.codeboss 下，如 pet_brain）里的会话不是
    「老板的活」：实测板上混进过 /private/tmp 的一次性「回复：好」和桌宠记忆维护。
    只用来过滤「现在/已交活」——审批永远照报。"""
    c = str(cwd or "").strip().rstrip("/")
    if not c:
        return False
    if any(c == r or c.startswith(r + "/") for r in _SCRATCH_ROOTS):
        return True
    h = str(HOME).rstrip("/")
    return c == h or c.startswith(h + "/")


def _alive(s):
    """会话是不是还活着。发现出来的电脑会话（agents 列表）只带 pid、不带 alive；
    以前只认 alive，于是「现在」永远是空的——正在跑的会话全看不见。"""
    if s.get("alive"):
        return True
    return bool(s.get("pid")) and str(s.get("state") or "") not in (
        "exited", "closed", "dead", "")


def _project_of(s, hist_row):
    if s and s.get("proj"):
        return s["proj"]
    cwd = (s or {}).get("cwd") or (hist_row or {}).get("cwd") or ""
    return Path(cwd).name if cwd else ""


def _minutes(sec):
    try:
        sec = max(0, int(sec))
    except Exception:
        return 0
    return sec // 60


def _ask_what(p):
    ask = p.get("ask") or {}
    what = " ".join(str(ask.get(k) or "") for k in ("action", "target")).strip()
    return what or str(p.get("text") or p.get("summary") or "等你点头")


def _work(sid, state, now, machine, sess=None, pend=None, hist_row=None,
          artifacts=None):
    s = sess or {}
    p = pend or {}
    h = hist_row or {}
    title = (s.get("title") or p.get("title") or h.get("title") or sid[:8])
    about = str(h.get("about") or p.get("about") or "").strip()
    summary = str(h.get("summary") or p.get("summary") or "").strip()
    last = float(s.get("last") or h.get("ts") or p.get("ts") or 0)
    proj = _project_of(s, h)
    w = {
        "id": sid, "sid": sid, "title": title,
        "agent": s.get("agent") or p.get("agent") or h.get("agent") or "",
        "project": proj, "machine": machine or "",
        "cwd": s.get("cwd") or h.get("cwd") or "",
        "state": state, "ts": last,
        "about": about, "summary": summary,
        "alive": _alive(s),
        "basis": [{"kind": "session", "sid": sid, "ts": last}],
    }
    if state == "waiting":
        w.update({"rid": p.get("rid") or "", "ask": p.get("ask") or {},
                  "risk": p.get("risk") or [], "why": p.get("why") or "",
                  "options": p.get("options") or [],
                  "say": _ask_what(p),
                  "basis": [{"kind": "approval", "sid": sid,
                             "ts": float(p.get("ts") or last), "rid": p.get("rid") or ""}]})
    elif state == "done":
        w["say"] = summary or about or title
        if artifacts:
            w["artifacts"] = artifacts
            w["basis"].append({"kind": "artifact", "sid": sid,
                               "n": len(artifacts)})
    elif state == "idle":
        w["say"] = ("停在上一轮，等你吩咐" if not about else "上一句做完了：" + about)
    else:
        if about:
            w["say"] = about
        else:
            w["say"] = ("在 %s 上跑着" % proj if proj else "在跑着") + \
                       ("，最近一次输出 %d 分钟前" % _minutes(now - last)
                        if last and now - last >= 60 else "")
    return w


def project(sessions, pending, hist, artifacts, verdicts, now, machine=""):
    """把事实投影成三块板。纯函数：不改入参，不碰磁盘（verdicts 已由调用方读好）。"""
    now = float(now or time.time())
    smap = {}
    for s in sessions or []:
        sid = str(s.get("sid") or "")
        if sid:
            smap[sid] = s
    pmap = {}
    for p in pending or []:
        sid = str(p.get("sid") or "")
        if sid:
            pmap[sid] = p          # 调用方已按会话取最新一条
    hmap = {}
    for h in sorted(hist or [], key=lambda x: -(float(x.get("ts") or 0))):
        sid = str(h.get("sid") or "")
        if sid and sid not in hmap:
            hmap[sid] = h          # 每个会话最近一条事实

    # 已交活：最近 24h 内「这一轮跑完」且老板还没处理
    done_rows, seen_done = [], set()
    for h in sorted(hist or [], key=lambda x: -(float(x.get("ts") or 0))):
        sid = str(h.get("sid") or "")
        if not sid or sid in seen_done or str(h.get("kind") or "") != "done":
            continue
        ts = float(h.get("ts") or 0)
        if not ts or now - ts > DONE_WINDOW:
            continue
        if is_scratch(h.get("cwd") or (smap.get(sid) or {}).get("cwd")):
            continue
        # 交过活之后又出了新输出 = 老板开了新一轮，这件活回到「现在」
        sm = smap.get(sid) or {}
        if _alive(sm) and float(sm.get("last") or 0) > ts:
            continue
        seen_done.add(sid)
        if sid in pmap:
            continue                       # 还在等你定：以「等」为准
        v = (verdicts or {}).get(sid) or {}
        if v.get("verdict") == "accepted" and float(v.get("ts") or 0) >= ts:
            continue
        done_rows.append(h)

    done = [_work(str(h.get("sid") or ""), "done", now, machine,
                  sess=smap.get(str(h.get("sid") or "")), hist_row=h,
                  artifacts=(artifacts or {}).get(str(h.get("sid") or "")))
            for h in done_rows[:DONE_MAX]]
    done_sids = {w["sid"] for w in done}

    # 等你定：审批就在那里，不管会话还活着没有
    decide = []
    for sid, p in pmap.items():
        decide.append(_work(sid, "waiting", now, machine, sess=smap.get(sid),
                            pend=p, hist_row=hmap.get(sid)))
    decide.sort(key=lambda w: -(w.get("ts") or 0))

    # 现在：活着的、没在等你、也没交过活的
    nows = []
    for sid, s in smap.items():
        if sid in pmap or sid in done_sids:
            continue
        if not _alive(s):
            continue
        if is_scratch(s.get("cwd")):
            continue
        last = float(s.get("last") or s.get("started") or 0)
        done_h = hmap.get(sid) or {}
        if str(done_h.get("kind") or "") == "done" and \
                float(done_h.get("ts") or 0) >= last:
            continue                       # 跑完了还没验收：归「已交活」
        silent = bool(last) and now - last > RUN_STALE
        st = "running" if ((str(s.get("state") or "") == "busy" and not silent)
                           or (last and now - last < RUN_RECENT)) else "idle"
        nows.append(_work(sid, st, now, machine, sess=s,
                          hist_row=done_h if str(done_h.get("kind") or "") != "done" else None))
    nows.sort(key=lambda w: -(w.get("ts") or 0))
    # 停在上一轮的不叫「现在」：它没在干活，是在等老板吩咐。拆成独立一块。
    idle = [w for w in nows if w.get("state") != "running"][:IDLE_MAX]
    nows = [w for w in nows if w.get("state") == "running"][:NOW_MAX]

    counts = {"now": len(nows), "idle": len(idle), "decide": len(decide),
              "done": len(done)}
    parts = []
    if counts["now"]:
        parts.append("%d 件在跑" % counts["now"])
    if counts["idle"]:
        parts.append("%d 件等你吩咐" % counts["idle"])
    if counts["decide"]:
        parts.append("%d 件等你点头" % counts["decide"])
    if counts["done"]:
        parts.append("%d 件交了活" % counts["done"])
    say = ("我盯着呢。" + "，".join(parts) + "。") if parts else "这会儿没活，等你派。"
    return {"ok": 1, "say": say, "counts": counts, "now": nows,
            "idle": idle, "decide": decide, "done": done,
            "meta": {"machine": machine or "", "ts": now, "brain": "facts"}}


# ---- M2 · 表达层：只换说法，不改状态 ---------------------------------------
# 跃迁时调一次中继小模型，把事实说成第一人称一句；模型不在/额度用完 → 事实句。
# 句子按「这一轮」缓存（同一轮里输出一直在涨，不重算），evidence 用投影自带的 basis。
BRAIN_MAX = 200               # 每天最多几次模型调用（沿用套餐智能提醒额度口径）
SAY_MAX = 60                  # 一句话上限；超了截断
SAYS_CAP = 400                # 缓存条数；旧句按时间淘汰

SAY_PROMPT = (
    "你是老板的工头。只根据下面给出的【事实】，用第一人称、口语，给老板说一句话。\n"
    "要求：一句话，不超过 24 个字；不加引号、不用 markdown、不要称呼「老板」、不客套；"
    "只说事实里有的，不编造。\n"
    "状态「正在干」：说在办哪件事（优先用项目名），绝不能说「已完成/做完了」。\n"
    "状态「等老板点头」：说清楚要他确认什么。\n"
    "状态「这一轮干完了」：用「AI 最后说的」讲清交了什么，别只说「干完了」。\n"
    "状态「停下了」：说上一轮停在哪，等老板下一句。\n"
    "只输出这句话本身。")


def brain_cfg(cfg_path=None):
    """boss.json 里的 brain 段：{"on": true, "max": 200}。缺省开、200 次。"""
    p = Path(cfg_path) if cfg_path else UI_CFG
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    br = cfg.get("brain") if isinstance(cfg, dict) else None
    br = br if isinstance(br, dict) else {}
    try:
        mx = int(br.get("max") or BRAIN_MAX)
    except (TypeError, ValueError):
        mx = BRAIN_MAX
    return {"on": br.get("on") is not False, "max": max(0, mx)}


def say_key(w):
    """一句话的有效期 = 一轮。在跑时 ts 一直在涨，用「老板这句原话」区分轮次；
    等审批看 rid，交活看那一轮的 done 时刻，停着看最后一次输出。"""
    st = str((w or {}).get("state") or "")
    if st == "waiting":
        fp = str(w.get("rid") or "")
    elif st == "running":
        fp = str(w.get("about") or "")
    else:
        fp = str(int(float(w.get("ts") or 0)))
    return "%s|%s|%s" % (w.get("sid") or "", st, fp)


def clean_say(text, w=None):
    """模型输出 → (一句, src)。不合格就退回事实句（src=facts，由调用方补句子）。"""
    s = str(text or "").strip()
    if not s:
        return "", "facts"
    s = " ".join(s.splitlines()[0].split())
    s = s.strip("\"'“”‘’`*# -")
    if len(s) > SAY_MAX:
        s = s[:SAY_MAX].rstrip("，。、,. ")
    st = str((w or {}).get("state") or "")
    if st == "running" and re.search(r"完成|做完|搞定|干完|收工", s):
        return "", "facts"
    return (s, "llm") if s else ("", "facts")


def say_facts(w):
    """喂给模型的全部事实（只喂事实，不喂期望；basis 由调用方原样挂上）。"""
    st = str((w or {}).get("state") or "")
    lines = ["状态：" + {"running": "正在干", "waiting": "等老板点头",
                        "idle": "停下了（等老板下一句）", "done": "这一轮干完了"}
             .get(st, st)]
    for k, label in (("title", "会话"), ("project", "项目"), ("machine", "机器")):
        v = str((w or {}).get(k) or "").strip()
        if v:
            lines.append("%s：%s" % (label, v))
    about = str((w or {}).get("about") or "").strip()
    if about:
        lines.append("老板这一轮的原话：%s" % about)
    if st == "waiting":
        lines.append("要老板确认的事：%s" % str((w or {}).get("say") or ""))
    else:
        summary = str((w or {}).get("summary") or "").strip()
        if summary:
            lines.append("AI 最后说的：%s" % summary)
    n = len((w or {}).get("artifacts") or [])
    if n:
        lines.append("产出物：%d 个" % n)
    return "\n".join(lines)


def load_says(path=None):
    p = Path(path) if path else SAYS_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def put_say(key, say, src="llm", path=None):
    p = Path(path) if path else SAYS_PATH
    d = load_says(p)
    d[str(key)] = {"say": str(say or ""), "src": src, "ts": time.time()}
    if len(d) > SAYS_CAP:
        for k in sorted(d, key=lambda q: float((d[q] or {}).get("ts") or 0))[:-SAYS_CAP]:
            d.pop(k, None)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def apply_says(b, says):
    """把缓存里的模型句盖到事实句上；没有的保持事实句并标 say_src=facts。"""
    for k in ("now", "idle", "decide", "done"):
        for w in b.get(k) or []:
            hit = (says or {}).get(say_key(w)) or {}
            if hit.get("say"):
                w["say"] = hit["say"]
                w["say_src"] = "llm"
            else:
                w["say_src"] = "facts"
    works = [w for k in ("now", "decide", "idle", "done") for w in b.get(k) or []]
    b.setdefault("meta", {})["brain"] = ("llm" if any(w.get("say_src") == "llm"
                                                      for w in works) else "facts")
    return b


def budget_take(path=None, maxn=BRAIN_MAX, day=None):
    """今天还能不能花一次模型调用；能就记上（花没花出去都算，免得失败时无限重试）。"""
    p = Path(path) if path else BRAIN_PATH
    day = day or time.strftime("%Y-%m-%d")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict) or d.get("day") != day:
        d = {"day": day, "n": 0}
    n = int(d.get("n") or 0)
    if n >= maxn:
        return False
    d["n"] = n + 1
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return True


def record_transitions(works, events_path=None, state_path=None, now=None):
    """状态变了才写一行（events.jsonl，可审计/可回放）。返回写了几条。"""
    now = float(now or time.time())
    ep = Path(events_path) if events_path else EVENTS_PATH
    sp = Path(state_path) if state_path else STATE_PATH
    d = load_state(sp)
    prev = d.get("states") or {}
    wrote = 0
    try:
        ep.parent.mkdir(parents=True, exist_ok=True)
        if ep.exists() and ep.stat().st_size > 5 * 1024 * 1024:
            ep.replace(ep.with_name(ep.name + ".1"))
        with ep.open("a", encoding="utf-8") as f:
            for w in works or []:
                sid = str(w.get("sid") or "")
                if not sid:
                    continue
                st = str(w.get("state") or "")
                old = (prev.get(sid) or {}).get("state") or ""
                if st == old:
                    continue
                row = {"ts": round(now, 3), "sid": sid,
                       "title": w.get("title") or "", "state": st, "prev": old,
                       "basis": w.get("basis") or [{"kind": "session", "sid": sid}]}
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                prev[sid] = {"state": st, "ts": now}
                wrote += 1
    except Exception:
        return 0
    d["states"] = prev
    _save_state(d, sp)
    return wrote
