# SPDX-License-Identifier: AGPL-3.0-or-later

"""提醒的判定规则：待确认问句的摘抄校验、na 的规则判、概括的规整。

这些是**纯函数**（只依赖 re），从 server.py 拆出来有两个原因：
  1. 它们是改得最勤的一层（每次误报都在这），单独一个文件改起来不用滚 1.4 万行；
  2. 语料回归（tests/test_corpus.py）可以不启动 host 直接重放判定。
server.py 仍然 import 回这些名字，调用点一个字没动。
"""
import re


def _say_clip(t, maxn, slack=10):
    """把概括削到能一口气念完的长度，但**不许从半句话中间切断**。
    以前是 t[:maxn] 硬切，TTS 念到「…立即说『发」就没了，听着像噎住。
    规矩：模型只稍微超一点（maxn+slack 内）就整句留下；超得多就在标点处收。"""
    t = str(t or "").strip()
    if len(t) <= maxn + slack:
        return t
    head = t[:maxn + slack]
    cut = -1
    for ch in "，、；,;":
        i = head.rfind(ch)
        if i > cut:
            cut = i
    if cut >= max(6, maxn // 2):
        return head[:cut]
    return t[:maxn]          # 实在没有停顿点：只能硬截（模型基本不会这样）


def _reply_split(s):
    """把模型的三行输出拆成（概括, 待确认, 起因是否相关）。
    模型不听话少写行时：只有一行 → 整行当概括；没有第三行 → rel=None（不确定，保留起因）。"""
    lines = [x.strip() for x in str(s or "").splitlines() if x.strip()]
    if not lines:
        return "", "", None
    act = ""
    if len(lines) >= 2:
        act = lines[1]
        act = re.sub(r"^(?:待确认|要确认|下一步|确认项|需要你)[:：]?\s*", "", act).strip()
        if act.strip("。.!！?？ 　") in ("无", "没有", "无待确认", "暂无", "none", "None", "-"):
            act = ""
    rel = None
    if len(lines) >= 3:
        t = re.sub(r"^[^相关无]*[:：]?\s*", "", lines[2]).strip()
        if t.startswith("相关"):
            rel = True
        elif t.startswith("无关"):
            rel = False
    return lines[0], act[:80], rel


_ACT_DONE = re.compile(
    r"已(?:经)?(?:成功)?(?:(?:将|把)[^，。；！？\n]{0,16})?"
    r"(?:发布|推送|发版|上线|完成|装好|落地|部署)")


def _act_conflict(summ, act):
    """概括说事情已经做完（已发布/已推送/已完成），第二行却还在问要不要做同一件事
    → 模型拿【用户这一轮的指令】编出了下一步（2026-11-09：发布完 0.7.41 又推
    「要不要发布 0.7.41 版本？」）。这种 act 一律丢掉，别让用户拍一个已经做完的板。
    这是兜底；第一道是 _act_grounded（必须是回复里的原话）。"""
    if not summ or not act or not _ACT_DONE.search(summ):
        return False
    for v in ("发布", "推送", "发版", "上线", "完成", "装好", "落地", "部署"):
        if v in summ and v in act:
            return True
    return False


def _act_norm(s):
    return re.sub(r"[\s*`#>_~|]+", "", str(s or ""))


def _act_grounded(act, reply):
    """act 必须是从【AI 最后的回复】里原样摘下来的，不能是模型自己造的句子。

    比关键词黑名单可靠：模型把「要不要发布这次改动？」当待决，而那句话根本不在这一轮
    回复里（2026-11-14 esp32c3：回复写的是「已将查看器发布为网页…需要自由看模型时再添加」，
    act 却是上一轮的问句）——摘抄对不上原话，就是编的。
    只比前半段：模型可能只摘了半句，后半句对不上不该判死。"""
    a = _act_norm(act)
    if len(a) < 4:
        return False
    r = _act_norm(reply)
    return (a[:40] if len(a) >= 40 else a) in r


def _reply_span(msg, head=700, tail=1200):
    """喂给概括模型的回复片段：头 + 尾，中间省略。
    以前只喂尾部 1500 字——长回复的尾巴常常从半段开始，模型看不到这轮是怎么起的，
    概括出来就「开头少东西」。头段有结论和起因，尾段有最后一步和待办。"""
    msg = str(msg or "")
    if len(msg) <= head + tail + 40:
        return msg
    return msg[:head] + "\n……（中间省略）……\n" + msg[-tail:]


def _reply_clean(s, maxn=40):
    """模型偶尔带引号/markdown/多句：削成一句适合念的话。"""
    t = re.sub(r"\s+", "", str(s or ""))
    t = re.sub(r"[*#>`_~|]", "", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", t)  # emoji 念不出来
    t = t.strip("「」『』\"'“”‘’【】[]()（）:：,，。.！!？?;；、…-— ")
    t = re.sub(r"^(?:好的|当然|嗯|总结|概括|这一轮|本轮)[,，:：]?", "", t)
    t = re.sub(r"[。！!？?；;]+", "，", t).strip("，,、")
    return _say_clip(t, maxn)


def _reply_decide(raw, reply, maxn=40):
    """概括模型的原始输出 → （概括, 待确认, 起因是否相关, 待确认是怎么来的）。

    act_src："" 没给 / "ok" 是这轮回复的原话 / "not-grounded" 编的 / "conflict" 已经做完还问。
    抽成纯函数是为了语料回归（tests/test_corpus.py）：同一份录制输出 + 同一段回复，
    判定必须每次一样，改规则时跑一遍就知道有没有把以前判对的判坏。"""
    summ, act, rel = _reply_split(raw)
    t = _reply_clean(summ, maxn)
    src = ""
    if act:
        if not _act_grounded(act, reply):
            src, act = "not-grounded", ""
        elif _act_conflict(t, act):
            src, act = "conflict", ""
        else:
            src = "ok"
    return t, act, rel, src


_NA_STRONG = re.compile(
    r"(要不要|需不需要|要我|需要我(先|再|顺手)?(做|改|发|推|出|生成|执行|运行|继续|处理|来)?|"
    r"是否(要|需要)|确认(一下|后|了再)|请确认|等你(确认|回复|决定)|"
    r"回复我|告诉我|需要你(确认|回复|操作|手动|执行|决定|输入)|"
    r"请你?(手动)?(执行|运行|跑|操作|确认|回复)|(手动|自己)(执行|运行)|"
    r"由你(来)?(执行|运行|决定)|"
    r"shall i|do you want|would you like|should i|let me know|please confirm|want me to)",
    re.I)


def _na_tail(msg, n=800):
    return (msg or "").strip()[-n:]


def _na_rule(msg):
    """没模型时的兜底：只看最后一段——真正的请求几乎都在结尾。"""
    tail = _na_tail(msg)
    if not tail:
        return False
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    last = lines[-1] if lines else tail
    if _NA_STRONG.search(last):
        return True
    return False


_NA_WEAK = re.compile(
    r"(\?|？|需要|确认|回复|执行|运行|手动|要不要|是否|决定|选择|发布|部署|推送|下一步|继续)", re.I)
