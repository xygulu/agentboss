# SPDX-License-Identifier: AGPL-3.0-or-later

"""提醒引擎：从「这一轮干完了」到「手机上响一声」的全部逻辑。

搬出 server.py 的原因：这是改得最勤的一块（判定、概括、播报、历史），原来埋在
1.4 万行里，改一句要滚半天；独立成文件后可以连着语料回归（tests/test_corpus.py）一起读。

它依赖的 8 个 server 函数（数据库、中继配置、跑命令、标题、trace）用**延迟导入**拿：
server.py 在顶部 import 本模块，模块级反向 import 会成环；调用时 server 早已加载完。
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from remind_rules import (_act_conflict, _NA_WEAK, _na_rule, _na_tail, _reply_decide,
                          _reply_span, _reply_split)

HERE = Path(__file__).resolve().parent

# server.py 里的这 8 件事不搬过来（它们还服务别的模块），延迟导入避免循环。
def _db(*a, **kw):
    import server; return server._db(*a, **kw)
def _relay_cfg(*a, **kw):
    import server; return server._relay_cfg(*a, **kw)
def _run(*a, **kw):
    import server; return server._run(*a, **kw)
def _sid_title(*a, **kw):
    import server; return server._sid_title(*a, **kw)
def _cause_clean(*a, **kw):
    import server; return server._cause_clean(*a, **kw)
def _tr_set(*a, **kw):
    import server; return server._tr_set(*a, **kw)
def _trace_load(*a, **kw):
    import server; return server._trace_load(*a, **kw)
def _voice_ok(*a, **kw):
    import server; return server._voice_ok(*a, **kw)

_NOTIFY_SEQ = [0]


_NOTIFY_Q = []      # 最多 50 条；连接建立时补发 10 分钟内的，重连后角标不丢


_NOTIFY_MAX = 50

# 桌宠在桌前会开口 → 手机别再念同一条。pet 启动时装上，停掉时卸下（None）。
_PET_GATE = None


def set_pet_gate(fn):
    global _PET_GATE
    _PET_GATE = fn


_NH_DAYS = 30


_NH_MAX = 3000


_NH_CLEAN = [0.0]


_PREF = {"about": {"on": True, "llm": True, "max": 12},
         "reply": {"on": True, "max": 40},
         "say": {"style": "plain"},
         # 智能提醒（Pro / Max）：开关跟着**每台手机**走（devs 是各设备上报的值），
         # 开发机只要还有一台开着就照常生成智能内容；全关才不花模型/合成。
         # on 只给老页面（不带 dev 的请求）兜底。
         "smart": {"on": True, "devs": {}},
         # 每台手机的「语音播报」开关（devs）。手边一台要听都没有 → 不合成语音、
         # 不算「在做什么/回复概括/是否在等你」，只发基础文字提醒。
         "voice": {"devs": {}},
         # 智能判断「这一轮其实在等你」（done→perm），关掉只按规则判断。
         "na": {"on": True}}


_LLM_REPLY_PROMPT = (
    "你是手机语音提醒的播报助手。用户用手机远程指挥一个 AI 编程助手干活，"
    "现在它这一轮干完了。根据【会话标题】【用户这一轮的指令】和【AI 最后的回复】，"
    "输出**严格三行**，不要序号、不要引号、不要 Markdown：\n"
    "第一行：一句口语说清当前发生了什么（做完的什么、结果如何）。开头先交代是针对什么事做的"
    "（拿用户这一轮的指令说），不用「接着」「然后」「刚才」「另外」「继续」这类承接词开头，"
    "不重复会话标题，不超过 40 个汉字，以句号结尾——这句会被直接念出来。\n"
    "第二行：只看【AI 最后的回复】——回复里**明确向用户提了问句**，或**明确要求用户下一步动手**"
    "（发布、确认、选择、补充信息），就把那句话**原样摘抄**下来（一个字都不许改写、不许补全、"
    "不许把两句话拼起来），最多 80 个字；摘不到就只写一个字：无。"
    "**不许根据【用户这一轮的指令】推测还剩什么事没办**——指令只是起因，不是任务清单。"
    "如果第一行已经交代做完/已发布/已推送/已安装，而回复里没再问用户任何事，也写「无」。"
    "寒暄、打招呼、自我介绍，以及「有什么可以帮你的吗」「还需要什么」「随时告诉我」这类客套话"
    "不算在问用户要拍板的事——也写「无」。\n"
    "第三行：判断【用户这一轮的指令】是不是第二行那件事（第二行是「无」就看第一行）的**直接来由**："
    "它得明确交代要做什么、或为什么现在做，能据此回答「为什么现在要问这件事」，才写「相关」；"
    "只是应答词（好的、嗯、行、继续、收到）、寒暄、或者问的根本是另一件事，都写「无关」。"
    "只写「相关」或「无关」。"
)


_REPLY = {}              # (sid, 前因, 回复尾部 600 字) -> (概括, 时间)


_REPLY_BUSY = set()


_REPLY_TTL = 900


SAY_STYLE_DEFAULT = "plain"


_SAY_STYLES = {
    "plain": {
        "name": "简洁", "hint": "干净利落，和以前的提醒一样",
        "voice": "Cherry", "vname": "亲切女声",
        "tails": {
            "done": "，完成了",
            "perm": "，需要你确认一下",
            "ask": "，需要你做个选择",
            "input": "，等你回复",
            "limit": "，模型额度用完了",
            "stuck": "，任务可能卡住了",
        }},
    "biz": {
        "name": "商务", "hint": "正式、专业，像同事在汇报",
        "voice": "Ethan", "vname": "沉稳男声",
        "tails": {
            "done": "，已经完成了，请您查收",
            "perm": "，有一项操作待您确认",
            "ask": "，需要您做出决定",
            "input": "，期待您的进一步指示",
            "limit": "，模型额度已用尽，请您及时处理",
            "stuck": "，任务进度可能受阻，建议尽快检查",
        }},
    "chill": {
        "name": "轻松", "hint": "随口一句，像个熟人提醒你",
        "voice": "Serena", "vname": "温柔女声",
        "tails": {
            "done": "，搞定啦",
            "perm": "，有个事儿等你点一下",
            "ask": "，来帮我选一个呗",
            "input": "，等你回个话哈",
            "limit": "，额度用光啦",
            "stuck": "，好像卡住了，瞅一眼呗",
        }},
    "flatter": {
        "name": "夸夸", "hint": "嘴甜一点，活儿干完先夸你",
        "voice": "Chelsie", "vname": "二次元甜妹",
        "tails": {
            "done": "，您交代的活儿办妥啦，您真厉害",
            "perm": "，就等您过目啦，您看一眼准没错",
            "ask": "，这个选择非您不可，您来拿个主意嘛",
            "input": "，小的候着您吩咐",
            "limit": "，额度用光了，辛苦您啦",
            "stuck": "，小的好像卡住了，等您来救场",
        }},
}


_SAY_DIR = HERE / "saycache"


_SAY_TTL = 1800


_SAY_MAX = 60            # 缓存里最多留多少个语音文件（同 30 分钟里说了很多句时的兜底）


_SAY_TEXTS = {}          # id -> (文本, 音色, 时间)；URL 里只放 id，其余来这张表查


_SAY_LOCK = threading.Lock()


_SAY_BUSY = set()        # 正在合成的 out_path：两个 say 同时写一个文件会写坏


_SAY_RELAY_FAIL = [0.0, 0]   # 连续失败次数 + 上次失败时间：中继不通时别再每句等十几秒


_SAY_RELAY_COOL = 120


_NA_PROMPT = (
    "你在判断一段 AI 编程助手的回复是不是在等用户做事。只回答 YES 或 NO。"
    "YES：它在向用户提问、请求确认或授权（例如「要不要现在发布？」「确认后我就推送」），"
    "或者要求用户手动执行/运行某个命令。"
    "NO：它只是汇报已经做完的事、说明结果，不需要用户做任何事；"
    "结尾客套的「还有什么需要吗？」也算 NO。")



def _nh_add(ev, reply=""):
    """记一条提醒历史，返回这一行的 n（拿不到返回 0）。绝不影响提醒本身：任何异常都吞掉。
    summary 取「AI 回复概括」，没有就取「在做什么」——不用 saytext，那句带了播报
    风格的尾巴（「请您查收」「快来看嘛」），当历史看不合适。
    返回 n 是为了「概括来晚了」时能回填：通知路径只等 2.5 秒，模型慢的这条先落原文。"""
    try:
        summary = (str(reply or "").strip() or str(ev.get("about") or "").strip())
        con = _db()
        try:
            ts = float(ev.get("ts") or time.time())
            rep = int(ev.get("rep") or 0)
            sid = ev.get("sid") or ""
            if rep > 0 and sid:
                # 同一条阻塞提醒又响了一次：只加次数（时间线保留首次发生的时刻）
                cur = con.execute(
                    "UPDATE notify_hist SET rep=? WHERE n=(SELECT n FROM notify_hist"
                    " WHERE sid=? AND kind=? AND ts>? ORDER BY n DESC LIMIT 1)",
                    (rep, sid, ev.get("kind") or "", ts - 6 * 3600))
                if cur.rowcount:
                    con.commit()
                    return 0
            # 这一轮跑完了：此前的「需要确认/等待回复」就算处理掉了——手机点确认
            # 或用户自己回了话都一样。只动最近 6 小时、还没标完成的那些。
            # round_end：kind 被 act_wait 从 done 改成 perm 后，这条仍然是「跑完」的落点，
            # 否则旧的待确认行永远停在 state=''（2026-11-09：68/69/70 三条一直挂着）。
            if ((ev.get("kind") or "") == "done" or ev.get("round_end")) and sid:
                con.execute(
                    "UPDATE notify_hist SET state='done' WHERE sid=? AND COALESCE(state,'')<>'done'"
                    " AND (kind IN ('perm','ask','input') OR COALESCE(act,'')<>'') AND ts>=?",
                    (sid, ts - 6 * 3600))
            cur = con.execute(
                "INSERT INTO notify_hist(ts,sid,title,kind,text,summary,about,cause,act,cwd,src,rep,state,rid,trace)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, sid, ev.get("title") or "", ev.get("kind") or "",
                 ev.get("text") or "", summary, ev.get("about") or "",
                 ev.get("cause") or "", ev.get("act") or "",
                 ev.get("cwd") or "", ev.get("src") or "", rep, "",
                 ev.get("rid") or "",
                 (json.dumps(ev["trace"], ensure_ascii=False) if ev.get("trace") else "")))
            n = int(cur.lastrowid or 0)
            if ts - _NH_CLEAN[0] > 3600:      # 每小时清一次：过期 + 超量
                _NH_CLEAN[0] = ts
                con.execute("DELETE FROM notify_hist WHERE ts<?", (ts - _NH_DAYS * 86400,))
                con.execute("DELETE FROM notify_hist WHERE n NOT IN"
                            " (SELECT n FROM notify_hist ORDER BY n DESC LIMIT ?)", (_NH_MAX,))
            con.commit()
        finally:
            con.close()
    except Exception:
        return 0
    return n


async def _nh_fill(n, task, old=""):
    """「概括来晚了」的回填：通知只等 2.5 秒，模型慢的时候这条历史先落「在做什么」
    （没有它才是截断原文），概括算出来后补写 summary。task 已经被 shield 过，这里
    只是第二个等待者，不会再调一次模型。
    old 是当时落库的那句兜底（about）；只有它还没被改过才覆盖，真概括不会被顶掉。"""
    if not n:
        return
    try:
        d = await asyncio.shield(task)
    except Exception:
        return
    rel = None
    if isinstance(d, dict):
        t = str(d.get("summary") or "").strip()
        act = str(d.get("act") or "").strip()
        rel = d.get("cause_ok")
    else:
        t, act = str(d or "").strip(), ""
    if not t and not act:
        return
    try:
        con = _db()
        try:
            cur = con.execute("UPDATE notify_hist SET summary=?, act=?"
                              " WHERE n=? AND COALESCE(summary,'')=?",
                              (t[:300], act[:80], int(n), str(old or "").strip()))
            # 概括晚到：这一轮 trace 里「待确认是怎么来的」也补上。只在真写进去时补
            # （同一行的兜底串还没被改过），不然会把别的轮的账记到这条上。
            src = str(d.get("act_src") or "") if isinstance(d, dict) else ""
            if src and cur.rowcount:
                r = con.execute("SELECT trace FROM notify_hist WHERE n=?",
                                (int(n),)).fetchone()
                tr = _trace_load(r[0] if r else "")
                tr["act"] = src
                con.execute("UPDATE notify_hist SET trace=? WHERE n=?",
                            (json.dumps(tr, ensure_ascii=False), int(n)))
            if act and rel is not True:
                # 补回来的 act 和起因没被确认过相关：推送时已经带出去了，历史里别留着
                con.execute("UPDATE notify_hist SET cause='' WHERE n=?", (int(n),))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _nh_list(sid="", days=30, limit=500):
    try:
        con = _db()
        try:
            q = ("SELECT n,ts,sid,title,kind,text,summary,about,cause,act,cwd,src,rep,state,rid,trace"
                 " FROM notify_hist WHERE ts>=?")
            args = [time.time() - max(1, min(90, int(days or 30))) * 86400]
            if sid:
                q += " AND sid=?"
                args.append(sid)
            q += " ORDER BY ts DESC LIMIT ?"
            args.append(max(1, min(_NH_MAX, int(limit or 500))))
            rows = con.execute(q, args).fetchall()
        finally:
            con.close()
        out = [{"n": r[0], "ts": r[1], "sid": r[2], "title": r[3], "kind": r[4],
                "text": r[5], "summary": r[6], "about": r[7], "cause": r[8],
                "act": r[9] or "", "cwd": r[10], "src": r[11], "rep": r[12] or 0,
                "state": r[13] or "", "rid": r[14] or "",
                "trace": _trace_load(r[15])} for r in rows]
        for it in out:
            # 已经做完却还挂着「要不要发布」的旧行：读出来就当这轮跑完了（2026-11-14：
            # esp32c3 那轮「已将查看器发布为网页」，act 却还是上一轮的「要不要发布」）。
            # 只清 act 还不够：kind 仍是 perm，收件箱/会话页会把它当成还没回的事。
            if it["act"] and _act_conflict(it["summary"], it["act"]):
                it["act"] = ""
                it["kind"] = "done"
        return out
    except Exception:
        return []


def _notify_add(sid, kind, text, cwd="", title=None, src="local", sound=None,
                voice=False, rep=0, about="", say="", reply="", act="",
                kind_rule="", sound_rule="", cause="", round_end=False, rid="",
                ask=None, risk=None, why="", trace=None):
    # 基础提醒（文字/角标/提醒音）免费；智能提醒（模型概括 + 语音播报）才是 Pro / Max。
    # 非智能档、或手边设备都把「语音播报」关了：语音相关字段全部清空——不调模型、
    # 不调合成，只发基础提醒。页面上的开关只是提示，真正的闸门在这一处。
    smart = _voice_ok()
    if not smart:
        voice, about, reply, say = False, "", "", ""
        act = ""
    # 桌宠那边会在桌前说：手机这端把语音让出去，文字/角标照旧。
    phone_voice = bool(voice)
    if phone_voice and _PET_GATE is not None:
        try:
            phone_voice = bool(_PET_GATE(kind, sid, title or "", about or "", reply or ""))
        except Exception:
            phone_voice = True
    # 要念语音就把「整句 + 念好的音频」一起带上：口吻和音色都按当前风格（见 _SAY_STYLES）。
    # reply 是「AI 这轮回复的概括」（发生了什么/要你确认什么），有它就代替状态尾句。
    saytext = ""
    if phone_voice and not say:
        v_reply = _say_decision(reply, ask, risk, act)
        saytext = _say_text(kind, about or "", reply=v_reply)
        say = _say_prepare(kind, about or "", v_reply) if saytext else ""
    if not phone_voice:
        say = saytext = ""
    _NOTIFY_SEQ[0] += 1
    ev = {"id": _NOTIFY_SEQ[0], "sid": sid, "kind": kind,
          "title": title or _sid_title(sid),
          "text": re.sub(r"\s+", " ", (text or "").strip())[:200],
          "cwd": cwd or "", "src": src,
          "sound": sound or kind, "voice": bool(phone_voice), "rep": int(rep or 0),
          "about": about or "",          # 语音开头那句「这一轮在做什么」（没开/算不出来就是空）
          "cause": cause or "",          # 前因：用户当轮的原话（提醒历史用，不受语音开关影响）
          "say": say or "",              # 整句念好的音频（/api/say/…）：手机没 TTS 引擎时放它
          "saytext": saytext or "",      # 同样的整句文字：音频放不出来时手机自己念
          "summary": str(reply or "").strip(),  # AI 这轮结果的概括：会话页「待确认」浮条/历史用
          "act": str(act or "").strip(),        # 要用户拍板的那件事（问句，具体到对象）
          "ts": time.time()}
    if rid:                            # 权限请求号：手机点完选项回传 /api/perm_answer 时对上
        ev["rid"] = rid
    # 决定的业务语义（确认什么/风险/为什么）：手机通知和「待你决定」卡直接用，
    # 不让每台设备各自猜。只有 perm 类才有。
    if ask:
        ev["ask"] = ask
    if risk:
        ev["risk"] = risk
    if why:
        ev["why"] = str(why)[:200]
    if round_end:
        # 这一条是「一轮跑完」的落点；kind 可能已被 act_wait 改成 perm，清理旧待确认
        # 不能靠 kind=='done' 判断了，单记一个标记给 _nh_add。
        ev["round_end"] = True
    if kind_rule:                      # 智能判断改过 kind 时的原始规则值（本机关智能提醒的手机按它）
        ev["kind_rule"] = kind_rule
    if sound_rule:
        ev["sound_rule"] = sound_rule
    if trace:
        ev["trace"] = dict(trace)      # 这一轮「怎么判的」：只给提醒健康页算账用
    # 先落历史拿行号再入队：手机端「会话页浮条」点确认时要拿这个 n 标「已确认」，
    # 入队后推送线程可能立刻把它序列化出去，那时还没 n（中间有 SQLite 的阻塞调用，
    # 不能指望 GIL 替我们保住顺序）。
    ev["nh"] = _nh_add(ev, reply)   # 留痕：手机「提醒历史」按天/会话时间线看
    _NOTIFY_Q.append(ev)
    del _NOTIFY_Q[:-_NOTIFY_MAX]
    return ev


def _reply_pref():
    rp = _PREF.get("reply") or {}
    try:
        mx = int(rp.get("max") or 40)
    except (TypeError, ValueError):
        mx = 40
    return {"on": rp.get("on", True) is not False,
            "max": max(16, min(80, mx))}


def _na_pref():
    return (_PREF.get("na") or {}).get("on", True) is not False


def _reply_key(sid, msg, cause=""):
    return (sid, str(cause or "")[:120], str(msg or "")[-600:])


async def _reply_llm(sid, msg, title, maxn, cause=""):
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return ""
    key = _reply_key(sid, msg, cause)
    span = _reply_span(msg)
    try:
        import aiohttp
        body = {"prompt": _LLM_REPLY_PROMPT, "purpose": "remind_reply",
                "text": "【会话标题】%s\n【用户这一轮的指令】%s\n【AI 最后的回复】%s"
                        % (title or "", _cause_clean(cause, 120) or "（未记录）", span)}
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm", json=body,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=6)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict) and d.get("ok"):
            t, act, rel, src = _reply_decide(d.get("text"), span, maxn)
            if src in ("not-grounded", "conflict"):
                print("[act] %s %s，丢掉 sid=%s：%s"
                      % (time.strftime("%m-%d %H:%M:%S"),
                         "不是这轮回复里的原话" if src == "not-grounded"
                         else "已经做完却还问要不要",
                         str(sid)[:8], _reply_split(d.get("text"))[1][:40]), flush=True)
            _tr_set(sid, act=src)
            if t or act:
                # act_src 跟着结果一起走：模型慢（超过通知等的那 2.5 秒）或命中缓存时
                # 这轮 trace 早就取走了，只能靠 _nh_fill 回填（否则健康页把这轮算成没判过）。
                out = {"summary": t, "act": act, "cause_ok": rel, "act_src": src}
                _REPLY[key] = (out, time.time())
                if len(_REPLY) > 400:
                    for q in sorted(_REPLY, key=lambda q: _REPLY[q][1])[:-200]:
                        _REPLY.pop(q, None)
                return out
    except Exception:
        pass
    return ""


async def _reply_get(sid, msg, cause=""):
    """拿到这轮回复的概括、「待用户拍板的那件事」，以及起因和它是否真的对得上。
    返回 {"summary": 念出来的那句, "act": 要确认的动作, "cause_ok": True/False/None}；
    开关关着/没内容/算不出来返回空串。"""
    if not _voice_ok():          # 免费档/语音播报关掉：不花模型调用
        return ""
    p = _reply_pref()
    msg = str(msg or "").strip()
    if not (p["on"] and msg):
        return ""
    key = _reply_key(sid, msg, cause)
    hit = _REPLY.get(key)
    if hit and time.time() - hit[1] < _REPLY_TTL:
        return hit[0]
    if key in _REPLY_BUSY:
        return ""
    _REPLY_BUSY.add(key)
    try:
        return await _reply_llm(sid, msg, _sid_title(sid), p["max"], cause)
    finally:
        _REPLY_BUSY.discard(key)


def _say_style():
    """当前风格 id；prefs.json 里坏了/是旧值都退回默认，不抛错。"""
    st = str(((_PREF.get("say") or {}).get("style")) or "")
    return st if st in _SAY_STYLES else SAY_STYLE_DEFAULT


def _say_pref():
    st = _say_style()
    return {"style": st,
            "styles": [{"id": k, "name": v["name"], "hint": v["hint"],
                        "vname": v["vname"]} for k, v in _SAY_STYLES.items()]}


def _say_decision(reply, ask=None, risk=None, act=None):
    """语音要念的那句：和收件箱「待你决定 / 等你回话」卡同一份事实。

    有结构化 ask（来自 hook 的 tool_input）就念「动作：对象」+ 高风险提示，
    不再只念模型写的那段概括——手机上看到「运行命令：rm -rf build/」，
    耳朵里也该是同一件事；没有结构化输入但模型给了待拍板那句，就念那句。"""
    _act = str(act or "").strip()
    if not isinstance(ask, dict):
        return _act or reply or ""
    a = str(ask.get("action") or "").strip()
    tgt = str(ask.get("target") or "").strip()
    what = (a + "：" + tgt) if (a and tgt) else (tgt or a)
    what = what.strip("：").strip()
    if not what:
        return _act or reply or ""
    out = "要确认：" + what
    try:
        r0 = ((risk or [])[0] or {})
        if str(r0.get("level") or "") == "high" and r0.get("text"):
            out += "。" + str(r0["text"])
    except Exception:
        pass
    return out


def _say_text(kind, about="", style=None, reply=""):
    """拼出要念的整句。
    有 AI 回复概括（reply）时，它就是句子的主体：概括里已经说清发生了什么/要你确认
    什么，再叠「完成了」「需要你确认一下」会把同一件事说两遍，所以只留风格音色。
    没有 reply 时才是老句子：开头（在做什么）+ 风格化的结尾。"""
    sty = _SAY_STYLES.get(style or _say_style()) or _SAY_STYLES[SAY_STYLE_DEFAULT]
    tail = (sty.get("tails") or {}).get(kind) or ""
    ab = (about or "").strip()
    rp = (reply or "").strip()
    if rp:
        say = (ab + "，" if ab else "") + rp
    elif not tail:
        return ""
    else:
        say = ab + tail if ab else tail.lstrip("，")
    # 句末补个句号：TTS 靠标点收语气，没有收尾会念得像被打断
    if say and say[-1] not in "。！!？?…":
        say += "。"
    return say


def _say_relay(text, out_wav, voice=""):
    """让中继按指定音色合成这句 → 写进 out_wav。不带 voice/中继不认识时用默认樱桃音。
    失败返回 False（调用方**不念**，见 _say_gen_locked）。中继连续失败就冷却一会儿，
    免得每条提醒都去等一次超时。"""
    import urllib.request
    rc = _relay_cfg()
    base = (rc.get("url") or "").rstrip("/")
    dev, sec = rc.get("device_id") or "", rc.get("device_secret") or ""
    if not (base and dev and sec):
        return False
    ts, n = _SAY_RELAY_FAIL
    if n >= 2 and time.time() - ts < _SAY_RELAY_COOL:
        return False
    try:
        body = {"dev": dev, "sec": sec, "text": text}
        if voice:
            body["voice"] = voice          # 老中继会忽略这个键，照样用 Cherry
        req = urllib.request.Request(
            base + "/r/tts", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read()
        if len(data) < 2000 or data[:4] != b"RIFF":
            raise ValueError("回包不是 WAV（%dB）" % len(data))
        with open(out_wav, "wb") as f:
            f.write(data)
        _SAY_RELAY_FAIL[1] = 0
        return True
    except Exception as e:
        _SAY_RELAY_FAIL[0], _SAY_RELAY_FAIL[1] = time.time(), n + 1
        if n == 0:
            print("[say] 中继合成失败，这条不念：%s" % str(e)[:100], flush=True)
        return False


def _say_gen(text, out_path, voice=""):
    """合成一句话 → m4a；afconvert 不可用就留 wav。返回实际文件路径或空。
    先写 .part 再原子改名：手机可能在这句话刚合成到一半就来取，
    直接落在正式文件名上会把残缺音频发出去（放到一半就断）。"""
    with _SAY_LOCK:
        if out_path in _SAY_BUSY:
            return ""
        _SAY_BUSY.add(out_path)
    try:
        return _say_gen_locked(text, out_path, voice)
    finally:
        with _SAY_LOCK:
            _SAY_BUSY.discard(out_path)


def _say_gen_locked(text, out_path, voice=""):
    wav_final = out_path[:-4] + ".wav"
    wav_tmp = out_path[:-4] + ".part.wav"
    m4a_tmp = out_path + ".part"
    try:
        _SAY_DIR.mkdir(parents=True, exist_ok=True)
        if not _say_relay(text, wav_tmp, voice):
            # 中继合成不出来就**不念**。本机 say 是另一个嗓音（婷婷），播出来就是
            # 「怎么变成别人的声音了」——用户明确不要降级顶替：宁缺毋滥。
            return ""
        if not os.path.exists(wav_tmp) or os.path.getsize(wav_tmp) < 1000:
            return ""
        try:
            r = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", wav_tmp, m4a_tmp],
                               timeout=8)
            if r.returncode == 0 and os.path.exists(m4a_tmp) and os.path.getsize(m4a_tmp) > 500:
                os.replace(m4a_tmp, out_path)
                try: os.replace(wav_tmp, wav_final)   # 板子要 PCM，wav 留着
                except OSError: pass
                return out_path
        except Exception:
            pass
        os.replace(wav_tmp, wav_final)      # 退回 wav：Android 也放得了
        return wav_final
    except Exception:
        return ""


def _say_files(i):
    return _SAY_DIR / (i + ".m4a"), _SAY_DIR / (i + ".wav")


def _say_prune():
    """清合成语音缓存（saycache/）。
    以前只在「要合成新句子」时才扫，而且只按时间筛：不常发提醒的机器上缓存一直躺着，
    短时间说了很多句不同的话又会在 30 分钟内堆出几十兆。现在三道：
      1) 超过 _SAY_TTL 的删；
      2) 合成到一半被杀留下的 .part* 只留 5 分钟；
      3) 总量兜底：按最近使用时间只留最新的 _SAY_MAX 个。
    删正在被手机读取的文件没关系（Unix 上打开着的句柄照样能读完）。"""
    try:
        if not _SAY_DIR.exists():
            return
        now = time.time()
        left = []
        for p in _SAY_DIR.iterdir():
            try:
                st = p.stat()
                if ".part" in p.name:              # 半截文件，正常流程不会留
                    if now - st.st_mtime > 300:
                        p.unlink()
                    continue
                if now - st.st_mtime > _SAY_TTL:
                    p.unlink()
                    continue
                left.append((st.st_mtime, p))
            except OSError:
                pass
        if len(left) > _SAY_MAX:
            left.sort(key=lambda x: x[0], reverse=True)
            for _, p in left[_SAY_MAX:]:
                try:
                    p.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _say_prepare(kind, about, reply=""):
    """通知里带上这句的音频地址；同时后台先合成，手机来取时通常已经就绪。
    没有「在做什么」那半句也照样合成状态那半句：多亲这类机器一个朗读引擎都没装，
    只靠壳的 TTS 就是没声音（「有提醒但没有语音播报」）。
    缓存键带上音色：同一个句子的「商务男声」和「夸夸甜妹」是两段不同的音频。"""
    if not _voice_ok():          # 语音播报属于智能提醒：免费档/没人要听不合成
        return ""
    text = _say_text(kind, about, reply=reply or "")
    if not text:
        return ""
    voice = (_SAY_STYLES.get(_say_style()) or _SAY_STYLES[SAY_STYLE_DEFAULT])["voice"]
    # v2：中继修掉了合成语音开头杂音/结尾被斩断的问题，本机缓存一并换键，
    # 否则同一句话永远命中旧 m4a，用户怎么更新都还是坏的。
    i = hashlib.sha1(("v2|" + voice + "|" + text).encode("utf-8")).hexdigest()[:24]
    _SAY_TEXTS[i] = (text, voice, time.time())
    if len(_SAY_TEXTS) > 200:
        for k in sorted(_SAY_TEXTS, key=lambda q: _SAY_TEXTS[q][2])[:-100]:
            _SAY_TEXTS.pop(k, None)
    m4a, wav = _say_files(i)
    _say_prune()          # 每条提醒顺手清一次（几十个 stat，比攒一盘子便宜）
    if not (m4a.exists() or wav.exists()):
        try:
            threading.Thread(target=_say_gen, args=(text, str(m4a), voice),
                             daemon=True).start()
        except Exception:
            return ""
    return "/api/say/" + i


async def _na_llm(msg):
    """让中继的 qwen 判一次；中继不可用/超时返回 None（调用方走规则兜底）。"""
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return None
    try:
        import aiohttp
        body = {"prompt": _NA_PROMPT, "purpose": "remind_na", "text": _na_tail(msg)}
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm", json=body,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=3.5)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict) and d.get("ok"):
            t = str(d.get("text") or "").strip().upper()
            if t.startswith("YES"):
                return True
            if t.startswith("NO"):
                return False
    except Exception:
        pass
    return None


async def _na_need(msg, sid=""):
    """这一轮结束的回复，是不是还在等用户（确认/回复/手动执行）。
    sid 只用来留痕（提醒健康页要看「这句是规则判的、模型判的，还是模型没答上」）。"""
    # 智能判断是收费能力；免费档/关掉后只按规则判断，不调模型。
    if not (_voice_ok() and _na_pref()):
        _tr_set(sid, na="off")
        return _na_rule(msg)
    if not (msg or "").strip():
        return False
    if _na_rule(msg):
        _tr_set(sid, na="rule")
        return True
    if not _NA_WEAK.search(_na_tail(msg)):   # 一点「可能要做点什么」的迹象都没有：别花模型钱
        _tr_set(sid, na="no-signal")
        return False
    v = await _na_llm(msg)
    # 留痕：这条只在规则说不清、又够可能时才调模型，一天没几次；出问题好回溯
    print("[na] %s 模型判断=%s" % (time.strftime("%m-%d %H:%M:%S"),
          "YES" if v is True else ("NO" if v is False else "超时/不可用")), flush=True)
    _tr_set(sid, na="llm-yes" if v is True else ("llm-no" if v is False else "llm-down"))
    if v is not None:
        return v
    tail = _na_tail(msg).rstrip()
    return tail.endswith("?") or tail.endswith("？")


# ── 验收摘要（2026-11-19 计划 A5）──────────────────────────────────────
# 老板点「通过」前先看清这轮干了什么。不受语音开关约束：验收是办正事，
# 不是播报——免费档/关了语音也照样出摘要（模型调用失败就回落原文拼接）。
_ACCEPT_PROMPT = (
    "你在帮老板验收一个 AI 员工刚办完的任务。根据下面的工作记录，用三句话以内说明：\n"
    "1) 这一轮实际做完了什么；2) 和老板的指令对得上吗（哪里对不上直接说）；\n"
    "3) 还有什么没做或值得留意的。\n"
    "只输出这三句，不要客套、不要标题、不要 markdown。")
_ACCEPT_MAX = 600


async def _llm_text(prompt, text, purpose, timeout=20):
    """通用模型调用：没配中继/失败一律回空串，由调用方兜底。"""
    rc = _relay_cfg()
    base, dev, sec = rc.get("url"), rc.get("device_id"), rc.get("device_secret")
    if not (base and dev and sec):
        return ""
    try:
        import aiohttp
        body = {"prompt": prompt, "purpose": purpose, "text": str(text or "")[:6000]}
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/api/llm", json=body,
                              headers={"X-CB-Dev": dev, "X-CB-Sec": sec},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                d = await r.json(content_type=None)
        if isinstance(d, dict) and d.get("ok"):
            return str(d.get("text") or "").strip()
    except Exception:
        pass
    return ""


async def accept_summary(sid, material, fallback=""):
    """→ {"text": 三句摘要或原文拼接, "src": "llm"|"fallback"}"""
    t = await _llm_text(_ACCEPT_PROMPT,
                        "【会话标题】%s\n%s" % (_sid_title(sid), material), "accept_summary")
    if t and t.strip():
        return {"text": t.strip()[:_ACCEPT_MAX], "src": "llm"}
    return {"text": str(fallback or "")[:_ACCEPT_MAX], "src": "fallback"}


async def _say_cache_loop(app):
    """合成语音缓存定期清：没人发提醒的时候也要把旧文件收掉，别一直占着盘。
    进程重启时先扫一遍（上次退出前可能留了一批）。"""
    async def _run():
        while True:
            await asyncio.get_event_loop().run_in_executor(None, _say_prune)
            await asyncio.sleep(600)
    app["say_cache_task"] = asyncio.create_task(_run())
