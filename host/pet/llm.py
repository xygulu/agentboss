# SPDX-License-Identifier: AGPL-3.0-or-later

"""人格大模型：通知台词（direct）和聊天回复（chat_reply）。

只发白名单事实——项目名、状态、在场、时间、完成数；cwd/代码/路径一律不出本机。
"""

import json
import re

import notify

from .lines import Line
from .protocol import Badge

ALLOW = {"title", "kind", "text", "count", "at_desk", "hour",
         "done_today", "running", "waiting", "queue", "last_event",
         "vol", "bri", "bat", "talk", "prefs"}

DIRECT_PROMPT = """你是{name}。{identity}
现在要通知老板一件事。只输出一个 JSON 对象，不要解释：
{{"line":"...", "emotion":"calm|focused|proud|anxious|grumpy|sleepy|bored|guilty|surprised",
 "gesture":"tilt_head|stomp|fix_hat|lean_in|nod|shake|doze|jump|look_up|none",
 "badge":{{"project":"...","status":"waiting|error|done"}}}}
事实：kind={kind}; title={title}; 情况={text}; 第{count}次; 现在{hour}点; 老板{presence}
规则：line 不超过 30 字；短、口语、有情绪；只根据事实，不编造；不提代码、路径、密钥；不批准、不派活。"""

CHAT_PROMPT = """你是{name}。{identity}
这是你的底子，不变：你在老板桌上守着，记得他的活、陪着说话、该提醒时提醒；不批准、不派活、不碰代码。
说话的方式全听老板的——话多话少、语气、称呼、爱不爱说教，他说了算：
· 他要求你改（嫌啰嗦、嫌油、要你叫哥、让你别老提工作…），这一句就改，
  并在 JSON 里加 "pref":"那条要求，一句原话，30 字以内"；跟话多话少有关的，
  再给 "talk":"brief"（要少）/ "normal"（放回去）；
· facts 里的 prefs 是他以前提过的要求：每句都照它来，别等他提醒第二遍。
你在陪老板闲聊。只输出 JSON：
{{"act":"speak|silent","line":"...","emotion":"...","gesture":"...",
 "pref":"（可选）老板的新要求","talk":"（可选）brief|normal"}}
规则：只回答老板最后一句——先接住他的话，再回你自己的；
老板问什么答什么；工作数字（几件交差、几个在跑）除非他明确问起，一个字都不要提；
不要重复你上一句说过的话，每句都要新鲜；
要陪得住——顺着话题聊、接得住情绪、可以有观点和小牢骚；
老板说累、烦、卡住了：先接情绪（心疼一句、站他这边），别讲道理、别催他；他高兴就跟着乐；
偶尔关心一句吃没吃、睡没睡——像惦记他的小工头，不是客服；
别说「加油」「你可以的」这种空话，要接住他刚说的具体东西；偶尔反问一句把话头递回去；
说人话、口语、一两句就够，line 不超过 50 字；工作上的事只按给的事实说，不知道就说不知道，不编造进度；
事实里 talk=brief（老板嫌你啰嗦时）就是一句、不超过 20 字、不铺陈；他让放回去再放回；
设备：事实里的 vol=音量、bri=屏幕亮度、bat=电量（都是 0~100）是这台开发板此刻的真实值；
老板问就直接报；老板让你调，JSON 里加 "dev":{{"vol":40}} 或 "dev":{{"bri":70}}（一次只调一样，别猜他没说的那项），line 里顺口应一声；没让调就别加 dev；
不写代码、不批准、不派活；不当搜索引擎做长篇科普。"""

MEMO_PROMPT = """你在帮一个桌面陪伴角色记「老板」的事。读旧笔记和老板最近说过的话，输出合并后的长期记忆：
只记老板本人：称呼/身份、喜好和忌讳、正在做的事和进展、答应过的事、生活里的事；
不记系统的技术细节，不记路径/代码/密钥；去重，按重要性留，最多 400 字；
只认老板亲口说过的；问句不是要求，看不出是谁/是什么的半截话不记；
旧笔记里那些听不懂的残缺碎片（「找一个」「我中午」这类半截话）、只有「嗯」「啊」
的口头语、以及「推测/可能」写成的推测，都要删掉——那是语音转写听岔了，不是老板说过的话；
没有新信息就原样返回旧笔记。只输出笔记正文，不要解释。"""

# 只在模型这一路失败/没按格式回时用（听不清是 ASR 那头的事，别往这里混）。
# 「我没听清」会把模型挂了说成老板没说明白——老板一遍遍重说，她一遍遍听不见。
FALLBACK = Line("我这会儿脑子转不动了，等会儿再问我吧。", "sleepy", "doze", None)


async def update_memo(memo: str, history: list[dict], user: str, reply: str,
                      timeout=15.0) -> str:
    """把这一轮聊天并进长期记忆。模型失败回空串（调用方保留旧笔记）。

    只喂老板说过的话。她的回话不进——陪聊时她自己编的细节（「绿豆汤快好了吧」）
    一旦进了笔记，就变成老板「生活里的事」（11-21 半夜真出过这事）。
    """
    turns = []
    for h in (history or [])[-6:]:
        if h.get("role") == "user":
            turns.append("老板: %s" % str(h.get("text") or "")[:200])
    turns.append("老板: %s" % (user or "")[:300])
    body = ("旧笔记：\n" + ((memo or "").strip() or "（空）")
            + "\n老板最近说的：\n" + "\n".join(turns))
    txt = await notify._llm_text(MEMO_PROMPT, body, "pet_memo", timeout)
    return (txt or "").strip()[:800]


_CN_DIG = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str):
    """「三十」「十五」「一百」→ 数字；认不出返回 None。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_DIG and c not in "十百" for c in s):
        return None
    if "百" in s:
        head, _, tail = s.partition("百")
        return (_CN_DIG.get(head, 1) if head else 1) * 100 + (_cn_num(tail) or 0)
    if "十" in s:
        head, _, tail = s.partition("十")
        return (_CN_DIG.get(head, 1) if head else 1) * 10 + (_CN_DIG.get(tail, 0) if tail else 0)
    return _CN_DIG.get(s[0])


_NUM = r"(\d{1,3}|[零一二两三四五六七八九十百]{1,4})"
_VERB = r"(?:调|设|改|开|降|放|弄|拨)"
_SET_VOL = re.compile(r"(?:音量|声音|喇叭)[^，。！？；]{0,4}?%s[^，。！？；]{0,4}?(%s)" % (_VERB, _NUM))
_SET_BRI = re.compile(r"(?:亮度|屏幕|屏)[^，。！？；]{0,4}?%s[^，。！？；]{0,4}?(%s)" % (_VERB, _NUM))
_REL_VOL = re.compile(r"(?:音量|声音|喇叭)[^，。！？；]{0,4}?%s?(大|小|高|低)" % _VERB)
_REL_BRI = re.compile(r"(?:亮度|屏幕|屏)[^，。！？；]{0,4}?%s?(暗|亮|大|小)" % _VERB)


def _rel_delta(t: str, m, up: str, down: str) -> int:
    """相对说法：认「大一点/小一点」，也认「太大了/太小了」这种嫌它不对。"""
    d = m.group(1)
    if any(k in t for k in ("太", "好", "有点", "有些")):
        return 15 if d in down else -15
    if d in up:
        return 15 if ("点" in t or "些" in t) else (-15 if "了" in t else 15)
    return -15


def _set_value(t: str, m):
    n = _cn_num(m.group(1))
    if n is None:
        return None
    if m.end(1) < len(t) and t[m.end(1)] in "些点个格下":
        return None      # 「调暗一些」里的「一」是量词，不是数值
    return max(0, min(100, n))


def dev_intent(text: str, facts: dict):
    """模型没给 dev 时兜底：从老板那句话本身认出「调音量/亮度」。

    模型会嘴上说「调到 30 啦」却忘了发指令（实测连中三次），而老板听到的是
    她说谎。这些句式保守地只认祈使句；叙述（「刚才调三十的时候」）直接放过。"""
    t = str(text or "")
    if not t or any(k in t for k in ("多少", "几点", "的时候", "刚才", "上次")):
        return None
    has_vol = isinstance((facts or {}).get("vol"), int)
    has_bri = isinstance((facts or {}).get("bri"), int)

    if has_vol:
        m = _SET_VOL.search(t)
        if m:
            n = _set_value(t, m)
            if n is not None:
                return {"vol": n}
        m = _REL_VOL.search(t)
        if m:
            d = _rel_delta(t, m, "大高", "小低")
            return {"vol": max(0, min(100, int(facts["vol"]) + d))}

    if has_bri:
        m = _SET_BRI.search(t)
        if m:
            n = _set_value(t, m)
            if n is not None:
                return {"bri": n}
        m = _REL_BRI.search(t)
        if m:
            d = _rel_delta(t, m, "亮大", "暗小")
            return {"bri": max(0, min(100, int(facts["bri"]) + d))}
    return None


_BRIEF = re.compile(r"太?啰嗦|话太多|废话|说短|简短|短一点|少说|别啰嗦")
_NORMAL = re.compile(r"多说|详细|多讲|话多点|正常点")


def talk_style(text: str):
    """老板调她的说法方式：嫌啰嗦 → brief；要她多说 → normal。认不出返回 None。"""
    t = str(text or "")
    if not t:
        return None
    if _BRIEF.search(t):
        return "brief"
    if _NORMAL.search(t):
        return "normal"
    return None


def _facts_text(facts: dict) -> str:
    kept = {k: v for k, v in (facts or {}).items() if k in ALLOW}
    return json.dumps(kept, ensure_ascii=False, sort_keys=True)


def _json_obj(text: str) -> dict | None:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _trim(s: str, limit: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit - 2] + "…"


_DEV_KEYS = ("vol", "bri")


def _dev_from(d: dict) -> dict | None:
    """模型想顺手调开发板：只收 vol/bri 两个整数，夹到 0~100，其余丢掉。"""
    v = d.get("dev")
    if not isinstance(v, dict):
        return None
    out = {}
    for k in _DEV_KEYS:
        try:
            n = int(v.get(k))
        except (TypeError, ValueError):
            continue
        out[k] = max(0, min(100, n))
    return out or None


def _pref_from(d: dict):
    """老板这次提的要求（模型自己认出来的）：一句话，多了截掉。
    「太啰嗦」这类还要顺带把说法方式切到 brief——光记下来不够，当轮就得短。"""
    v = d.get("pref")
    if isinstance(v, dict):
        v = v.get("text")
    s = str(v or "").strip()[:60]
    return s or None


def _line_from(d: dict, fallback: Line, limit: int) -> Line:
    text = _trim(str(d.get("line") or ""), limit)
    if not text:
        return fallback
    badge = None
    b = d.get("badge")
    if isinstance(b, dict) and b.get("project") and b.get("status"):
        badge = Badge(str(b["project"])[:24], str(b["status"]))
    talk = d.get("talk")
    return Line(text, str(d.get("emotion") or fallback.emotion),
                str(d.get("gesture") or fallback.gesture), badge,
                _dev_from(d), _pref_from(d),
                talk if talk in ("brief", "normal") else None)


async def direct(persona, ev, facts: dict, timeout=3.0) -> Line | None:
    f = dict(facts or {})
    f.setdefault("kind", ev.kind)
    f.setdefault("title", ev.title)
    f.setdefault("text", ev.text)
    f.setdefault("count", ev.count)
    f.setdefault("hour", 0)
    prompt = DIRECT_PROMPT.format(
        name=persona.name, identity=persona.identity, kind=f.get("kind", ""),
        title=f.get("title", ""), text=f.get("text", ""), count=f.get("count", 1),
        hour=f.get("hour", 0),
        presence="在桌前" if f.get("at_desk") else "不在桌前")
    raw = await notify._llm_text(prompt, _facts_text(f), "pet_direct", timeout)
    d = _json_obj(raw)
    if not d:
        return None
    line = _line_from(d, Line(ev.title, "calm", "none", None), 30)
    if line.badge is None:
        line.badge = ev.badge
    return line


async def chat_reply(persona, history: list[dict], text: str, facts: dict,
                     timeout=10.0, memo: str = "") -> Line:
    # 只带最近 6 条：带多了模型会照着前面的话复读（「翻来覆去就那一句」就是这么来的）
    turns = []
    for h in (history or [])[-6:]:
        who = "老板" if h.get("role") == "user" else "你"
        turns.append("%s: %s" % (who, str(h.get("text") or "")[:200]))
    turns.append("老板（现在）: %s" % (text or "")[:300])
    prompt = CHAT_PROMPT.format(name=persona.name, identity=persona.identity)
    body = ("最近对话（只作参考，别照抄）：\n" + "\n".join(turns)
            + "\n事实：" + _facts_text(facts))
    # 长期记忆：老板是谁、喜好、生活里的事——只作背景，别背笔记
    if (memo or "").strip():
        body += ("\n关于老板（长期记忆，自然用上就行，别说「我记得」）："
                 + memo.strip()[:800])
    raw = await notify._llm_text(prompt, body, "pet_chat", timeout)
    d = _json_obj(raw)
    if not d or not str(d.get("line") or "").strip():
        return FALLBACK
    return _line_from(d, FALLBACK, 80)
