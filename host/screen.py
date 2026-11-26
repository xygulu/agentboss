# SPDX-License-Identifier: AGPL-3.0-or-later

"""终端屏幕 → 手机页面：全彩镜像的行渲染、CJK/折行处理、TUI 弹层识别。

这一层只吃 pyte 的行（dict of Char），不碰会话/连接/状态：给一组行，吐出 HTML、
识别出弹层选项。搬出来是为了能单独喂屏幕样本做回归——它同时决定「看回话」和
「会话里点选确认」两件事，格式（颜色、宽字符、弹层文案）一变就是手机上看不见/点错。
"""
import re
import time
import unicodedata

from pyte.screens import Char

# 全彩重排的字号参考列数：固定值保证所有 agent/所有会话同一字号
# （以前跟会话终端列数走，hermes 80 列字很大、opencode 120+ 列字很小）
REF_COLS = 80

_CSS16 = {"black": "#6e7681", "red": "#f87171", "green": "#4ade80", "brown": "#fbbf24",
          "yellow": "#fbbf24", "blue": "#60a5fa", "magenta": "#c084fc", "cyan": "#22d3ee",
          "white": "#e6edf3", "brightblack": "#9ca3af", "brightred": "#fca5a5",
          "brightgreen": "#86efac", "brightbrown": "#fde047", "brightyellow": "#fde047",
          "brightblue": "#93c5fd", "brightmagenta": "#d8b4fe", "brightcyan": "#67e8f9",
          "brightwhite": "#ffffff"}


_MARKS = ("•", "⎿", "❯", "✻", "✽", "✳", "✶", "✦", "·", "∙", ">", "%", "⏵", "⏺", "●")


_FINAL = "。．.！!？?；;：:…）」』】"   # 句读/收尾符：短行以此收尾更像真分段


def _esc_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_XTERM16 = ("#6e7681", "#f87171", "#4ade80", "#fbbf24", "#60a5fa", "#c084fc",
            "#22d3ee", "#e6edf3", "#9ca3af", "#fca5a5", "#86efac", "#fde047",
            "#93c5fd", "#d8b4fe", "#67e8f9", "#ffffff")


def _css_color(v):
    if v in (None, "default"):
        return None
    if isinstance(v, str) and len(v) == 6:
        return "#" + v
    if isinstance(v, int) and 0 <= v < 16:
        return _XTERM16[v]      # pyte 的 256 色是整数，0~15 是标准 16 色
    return _CSS16.get(v)


_TERM_GRAY = "#8b949e"    # 浅底内容压成的灰字（深底上读得清）


def _lum(css):
    """'#rrggbb' → 亮度 0~255"""
    try:
        r = int(css[1:3], 16)
        g = int(css[3:5], 16)
        b = int(css[5:7], 16)
    except (ValueError, IndexError, TypeError):
        return 0
    return (r * 299 + g * 587 + b * 114) // 1000


def _light_bg(bg):
    """背景亮不亮（None/深色 → False）"""
    return bool(bg) and _lum(bg) > 140


def _xterm256(n):
    """xterm 256 色调色板 → '#rrggbb'（原样视图里 xterm 自己按这套上色）"""
    if n < 16:
        return _XTERM16[n]
    if n < 232:
        n -= 16
        lv = (0, 95, 135, 175, 215, 255)
        return "#%02x%02x%02x" % (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def _row_runs(row, cols, trim_lead=False):
    """一行 → (style, text) 游程列表；整行空白返回 None；trim_lead 去掉行首缩进空格"""
    row_get = row.get
    last = -1
    for x in range(cols):
        ch = row_get(x)
        if ch is not None and (ch.data.strip() or ch.bg != "default" or ch.reverse):
            last = x
    if last < 0:
        return None
    x0 = 0
    if trim_lead:
        while x0 <= last and (row_get(x0) is None or row_get(x0).data == " "):
            x0 += 1
        if x0 > last:
            return None
    runs = []
    prev = None
    buf = []
    for x in range(x0, last + 1):
        ch = row_get(x)
        if ch is None:
            st, data = ("default", "default", False, False, False, False), " "
        else:
            st, data = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse), ch.data
        if st != prev and buf:
            runs.append((prev, "".join(buf)))
            buf = []
        buf.append(data)
        prev = st
    if buf:
        runs.append((prev, "".join(buf)))
    out = []
    for st, txt in runs:
        css = ""
        fg = _css_color(st[0])
        bg = _css_color(st[1])
        # 浅底整块去掉（页面固定深底），那一块统一压成灰字——
        # 白底白字/白底黑字都不再区分，直接深底灰字
        if _light_bg(bg):
            bg = None
            fg = _TERM_GRAY
        if fg:
            css += "color:" + fg
        if bg:
            css += ";background:" + bg
        if st[2]:
            css += ";font-weight:600"
        # 斜体/下划线在原样视图里由 SGR 3/4 还原；实时视图以前只认粗体和反显，
        # 同样的内容两边样式不一致（引用块斜体、链接下划线在实时里消失）
        if st[3]:
            css += ";font-style:italic"
        if st[4]:
            css += ";text-decoration:underline"
        if st[5]:
            css = ";background:#3a4150;color:#ffffff"
        out.append((css, txt))
    return out


def _cells_fg_bg(st):
    """游程的前景/背景色：浅底整块去掉、压成灰字（电脑端窗口也是深底）"""
    fg = _css_color(st[0])
    bg = _css_color(st[1])
    if not st[5] and _light_bg(bg):
        bg = None
        fg = _TERM_GRAY
    return fg, bg


def _runs_html(runs):
    if not runs:
        return ""
    out = []
    for css, txt in runs:
        t = _esc_html(txt.replace("⏺", "●").replace("⏵", "▸"))   # U+23FA/U+23F5 手机端落 emoji 字体（方块底）；●/▸ 全平台纯文本
        out.append('<span style="%s">%s</span>' % (css, t) if css else t)
    return "".join(out)


_BOXES = set("─│┌┐└┘├┤┬┴┼╭╮╯╰═║╔╗╚╝╠╣╦╩╬")


_RULES = set("─━═")     # 纯横线的分隔行：重排视图里直接丢（手机上白占一行）


_SPC = Char(" ")   # 硬折行回并时的接缝空格


def _is_cjk(ch):
    return bool(ch) and ord(ch) >= 0x2E80


def _disp_w(s):
    """显示宽度：CJK/全角按 2 列计"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _row_plain(row, cols):
    get = row.get
    return "".join((get(x).data if get(x) is not None else " ") for x in range(cols)).rstrip()


def _edge_style(row, width, last=False):
    """行首/行尾第一个可见字符的 (fg,bg,reverse)；全空白返回 None。
    判断两行是不是同一条软折行时用：TUI 自己折出来的续行颜色是连续的；换了一条
    输出（比如 grep 结果接在命令后面、状态行接在工具结果后面）通常换了颜色。"""
    xs = range(width - 1, -1, -1) if last else range(width)
    for x in xs:
        ch = row.get(x)
        if ch is not None and ch.data.strip():
            return (ch.fg, ch.bg, ch.reverse)
    return None


def _same_style(a_row, a_w, b_row, b_w):
    """两行是不是同一条软折行（取不到颜色时不拦，保持老行为）。

    主判据：上一行行尾与下一行行首颜色连续。补充判据：两行**行首**风格一致。
    上一行行尾可能正好落在一个着色片段里（比如正文末尾的 `⎿`、行内代码），
    行尾颜色和下一行行首不同，但两行其实都从正文的默认色起笔——那是 TUI 折行，
    不是新的一行。接在命令后面的工具输出行行首是输出自己的颜色，两条判据都不
    连续，照旧分开。"""
    sa, sb = _edge_style(a_row, a_w, True), _edge_style(b_row, b_w)
    if sa is None or sb is None or sa == sb:
        return True
    ha, hb = _edge_style(a_row, a_w), _edge_style(b_row, b_w)
    return ha is not None and hb is not None and ha == hb


def _fg_colors(row, cols):
    """一行里出现过的前景色集合（识别语法高亮/diff 上色行）"""
    runs = _row_runs(row, cols) or []
    return {css.split(";")[0] for css, _ in runs if css.startswith("color:")}


_CODE_PUNCT = set("()[]{}=")   # 代码标点：正文行里很少连着出现 3 个以上


_STATUS_PREFIXES = ("Thought for", "Thinking", "Synthesizing", "Synthesized",
                    "Worked for", "Crunched for", "Running…", "Running...",
                    "esc to interrupt", "auto mode")


def _reflow_rows(row_dicts, cols):
    """任意一组终端行 → 手机排版 HTML（历史区与实时区共用同一渲染）"""
    lines = []
    for row in row_dicts:
        txt = _row_plain(row, cols)
        # 纯横线（输入框上下那两条 ────、分隔线）在手机上白占一整行：丢掉
        if txt.strip() and set(txt.strip()) <= _RULES:
            continue
        lines.append((row, txt))
    while lines and not lines[-1][1].strip():
        lines.pop()
    blocks = []   # [kind, row, txt]：blank / blk(表格·代码等需对齐) / txt(正文)
    for row, txt in lines:
        if not txt.strip():
            blocks.append(["blank", None, ""])
            continue
        box = sum(1 for ch in txt if ch in _BOXES)
        stripped = txt.strip()
        if box >= 3 or txt.startswith("    ") or "  " in stripped:
            blocks.append(["blk", row, txt])
        else:
            blocks.append(["txt", row, txt])
    # 硬折行回并：TUI 自己折长行（词边界或列边界，行尾空格被吞）。代码"块"行
    # （缩进/对齐空格被判 blk）折出的尾巴行被判成正文（txt），渲染成代码块中间
    # 插一段正文——表象是长路径串行错位。前块无框线且未以句读/…收尾时，满足
    # 之一即并回前块：A. 前块写满行宽（列边界折行，尾巴紧拼）；
    # B. 尾巴行是无连续空格的长词/路径（词边界折行，按词界补空格）。
    def _wrap_src(b):
        if b[0] != "blk" or any(ch in _BOXES for ch in b[2]):
            return False
        pr = b[2].rstrip()
        return (bool(pr) and _disp_w(pr) >= 6 and not pr.endswith(("…", "..."))
                and pr[-1:] not in _FINAL)
    j = 1
    while j < len(blocks):
        a, b = blocks[j - 1], blocks[j]
        if b[0] == "blank" or not _wrap_src(a):
            j += 1
            continue
        pr = a[2].rstrip()
        tail = b[2].strip()
        # 颜色不连续、或下一行是状态行 → 是新的一行，不是折行尾巴
        cont_style = _same_style(a[1], a[3] if len(a) > 3 else cols, b[1], cols)
        # 恰好写满一行才算列边界折行（合成行必然超宽，不能再次触发）；
        # 尾巴必须是正文类（TUI 折行尾巴顶格书写，独立代码行多有缩进/对齐空格=blk）；
        # 以标记/项目符号开头的（⏺ 工具调用、⎿ 结果…）是新一行，不是折行尾巴
        # 末格也是字 = 列边界硬折行（词边界折行末格是空格，补空格才对）；中日文
        # 拼接不需要空格，末格是不是空格无所谓
        lc = a[1].get(cols - 1)
        cjk_edge = _is_cjk(pr[-1:]) or _is_cjk(tail[:1])
        col_exact = (b[0] == "txt" and cols - 1 <= _disp_w(pr) <= cols
                     and (cjk_edge or (lc is not None and bool(lc.data.strip())))
                     and tail[:1] not in _MARKS)
        wordish = (b[0] == "txt" and "  " not in tail and _disp_w(tail) >= 8
                   and not _is_cjk(tail[:1]) and tail[-1:] not in _FINAL
                   and len(_fg_colors(b[1], cols)) < 2
                   and not tail.startswith("```") and tail[:1] not in _MARKS
                   and tail[:1] != "#")
        if (col_exact or wordish) and cont_style and not tail.startswith(_STATUS_PREFIXES):
            # 列边界折行尾巴是断在词中间，紧拼；词边界折行吞了行尾空格，这里补回。
            # 不能只看前一行最后一个字符是不是字母：命令参数常以引号/斜杠收尾
            # （`grep -n "…"` + `server.py`），末尾是 `"` 但空格同样被吞了。
            sep = "" if col_exact or (_is_cjk(pr[-1:]) and _is_cjk(tail[:1])) else " "
            joined = dict(a[1])
            pos = _disp_w(pr)
            if sep:
                joined[pos] = _SPC
                pos += 1
            for x in range(cols):
                ch = b[1].get(x)
                if ch is not None:
                    joined[pos + x] = ch
            a[1] = joined
            a[2] = pr + sep + b[2].lstrip()
            if len(a) > 3:
                a[3] = pos + cols
            else:
                a.append(pos + cols)   # 合成行超出行宽，记有效宽度供 runs/maxw 用
            del blocks[j]              # 不动 j：blocks[j] 已是下一行，下轮接着判
        else:
            j += 1
    # 缩进产生的"blk"若紧跟正文行、无框线 → 多半是 autowrap/悬挂缩进续行，并回正文。
    # claude 的 TUI 按块排版：段落文本近满行宽折行（通道 A），列表项缩进深、折行宽度
    # 窄得多（如 "  2. …💬聊天" 59 列就断 → "     tab（…）"）——通道 B 专收这种：
    # 上一行末尾无句读、续行是"纯缩进块"（无框线、无内部连续空格；代码/表格行必有
    # 连续空格或框线，不会被误并）。
    for i in range(1, len(blocks)):
        b, p = blocks[i], blocks[i - 1]
        if b[0] != "blk" or p[0] != "txt" or b[2][:1] != " ":
            continue
        if any(ch in _BOXES for ch in b[2]):
            continue
        ptxt = p[2].rstrip()
        if ptxt.endswith("…") or ptxt.endswith("..."):
            continue   # 截断行（输入框溢出/队列摘要）：下一行绝不可能是它的折行
        pw = _disp_w(ptxt)
        plain_cont = ("  " not in b[2].strip()                    # 内部连续空格=代码/表格
                      and sum(1 for ch in b[2] if ch in _CODE_PUNCT) < 3   # 代码标点密集
                      and len(_fg_colors(b[1], b[3] if len(b) > 3 else cols)) < 2)  # 语法高亮/diff 上色
        # 工具调用的参数还没收尾（`● Bash(` 的右括号在后面几行）：缩进块一定是它的
        # 续行。参数里引号、等号密集，过不了 plain_cont 的代码判断，所以单独放行。
        # 命令行自己也可能已折成好几行，往前找到整段开头再数括号。
        acc = ptxt
        k = i - 2
        while k >= 0 and blocks[k][0] == "txt" and len(acc) < 4000:
            acc = blocks[k][2] + acc
            k -= 1
        open_call = acc.count("(") > acc.count(")")
        if ((plain_cont and (pw >= cols - 8
                             or (pw >= 12 and ptxt[-1:] not in _FINAL)))
                or open_call):
            b[0] = "txt"
    html = []
    i, n = 0, len(blocks)

    def runs_of(idx, trim_lead=False):
        b = blocks[idx]
        return _runs_html(_row_runs(b[1], b[3] if len(b) > 3 else cols, trim_lead=trim_lead))

    while i < n:
        kind = blocks[i][0]
        if kind == "blank":
            # 连续空行只留一个间隔：TUI 常在块之间留 2～3 行，手机上就是一大片空白
            if html and html[-1] != '<div class="sp"></div>':
                html.append('<div class="sp"></div>')
            i += 1
        elif kind == "blk":
            parts = []
            while i < n and blocks[i][0] == "blk":
                parts.append(runs_of(i))
                i += 1
            # 代码/表格字号：除数用固定参考列数（REF_COLS），不再跟会话终端列数走
            # ——列多字小、列少字大，跨 agent/跨会话字号全不一致（hermes 大、
            # opencode 小，用户主诉）。80 列铺满屏宽；更宽的会话行超屏宽走横向
            # 滚动，窄的留白。实时字号仍由 --fs-scale 统一缩放。
            fs = ("max(6px, calc((100vw - 52px) / %.1f * var(--fs-scale)))"
                  % (REF_COLS * 0.62))
            html.append('<div class="blk"><pre style="font-size:%s">%s</pre></div>' % (fs, "\n".join(parts)))
        else:
            para = []
            while i < n and blocks[i][0] == "txt":
                if para:
                    prev_txt = blocks[i - 1][2]
                    nxt = blocks[i][2].lstrip()
                    # 下一行是新结构（列表/提示符/序号/标题/代码围栏）→ 保留换行；
                    # 整行加粗 = TUI 渲染的标题行（markdown heading 无 # 号，只有 SGR 粗体）
                    nxt_runs = _row_runs(blocks[i][1], cols)
                    bold_line = bool(nxt_runs) and all(
                        ";font-weight:600" in css for css, t in nxt_runs if t.strip())
                    # 上一行整行加粗 = 标题行，标题自己独占一段：正文并进标题会让
                    # 同一个区块里粗体/常规两种字形混排（手机重排后更像一句话被切碎）
                    prev_runs = _row_runs(blocks[i - 1][1], cols)
                    prev_bold = bool(prev_runs) and all(
                        ";font-weight:600" in css for css, t in prev_runs if t.strip())
                    starts_block = bool(nxt) and (
                        nxt[0] in _MARKS
                        or nxt[:2] in ("- ", "* ")
                        or (nxt[0].isdigit() and len(nxt) > 1 and nxt[1] in ".）)")
                        or nxt.startswith("```")
                        or nxt.startswith(_STATUS_PREFIXES)   # Thought for… / Running…
                        or bold_line
                        or (nxt[0] == "#" and (len(nxt) == 1 or nxt[1] in " #")))
                    # claude 的 TUI 自己折行正文（写完一行绝对定位到下一行），段落中间
                    # 不存在"真换行"——相邻正文行默认合并交给手机 CSS 重排；只有
                    # 截断行（…/... 收尾）或"短行 + 句读收尾"才真分段（长行恰好收在
                    # 句读上仍合并）。颜色不连续同理：工具有输出行接在命令折行后面时
                    # 颜色变了，那不是同一句的折行，别并（否则两行粘成一行、不换行）
                    prev_r = prev_txt.rstrip()
                    prev_w = _disp_w(prev_r)
                    # 短行以 … 收尾 = 截断（输入框溢出等），下一行不是它的折行；
                    # 长行里的 … 只是正文标点（"Thought for…、Running…"），照样并
                    like_break = ((prev_r.endswith("…") or prev_r.endswith("..."))
                                  and prev_w < cols - 10)
                    like_break = like_break or (prev_w < cols - 10
                                                and prev_r[-1:] in _FINAL)
                    cont_style = _same_style(blocks[i - 1][1], cols, blocks[i][1], cols)
                    joinable = (not starts_block
                                and not prev_bold
                                and not prev_txt.lstrip().startswith("```")
                                and cont_style
                                and not like_break)
                    if not joinable:
                        html.append('<div class="p">%s</div>' % "".join(para))
                        del para[:]
                if para:  # 续行：去悬挂缩进；断行处补空格——拉丁-拉丁补词间空格，
                    #       中日文与拉丁文交界也补一个（"聊天|tab"→"聊天 tab"），中日文之间直接相连
                    prev_tail = blocks[i - 1][2].rstrip()[-1:]
                    cur_head = blocks[i][2].lstrip()[:1]
                    lat = lambda ch: bool(ch) and ch.isascii() and ch.isalnum()
                    # 上一行最后一格也被字占满 = 终端在列边界硬折行，断点可能在单词
                    # 中间（"…all.T|he quick"）：这里再补空格就把词切开了（"all.T he"）。
                    # 词边界折行会在行尾留下空格（被 _row_plain 去掉），末格是空的。
                    last_cell = blocks[i - 1][1].get(cols - 1)
                    col_wrap = last_cell is not None and bool(last_cell.data.strip())
                    if (not col_wrap
                            and (not (_is_cjk(prev_tail) or _is_cjk(cur_head))
                                 or (_is_cjk(prev_tail) != _is_cjk(cur_head)
                                     and (lat(prev_tail) or lat(cur_head))))):
                        para.append(" ")
                para.append(runs_of(i, trim_lead=bool(para)))  # 续行去悬挂缩进
                i += 1
            if para:
                html.append('<div class="p">%s</div>' % "".join(para))
    if not html:
        html = ['<div class="sp"></div>']
    return "".join(html)


_DLG_HINT = ("Enter to select", "Esc to cancel", "Do you want to proceed",
             "Tab to amend", "↑/↓ to navigate")


_DLG_HINTS = (
    "Enter to select", "Esc to cancel", "Do you want to proceed", "Tab to amend",
    "to navigate", "↑/↓", "to add notes", "to switch questions",
    "to set as default", "to use this session only", "Enter to continue",
    "Enter to confirm",
)


_DLG_OPT_RE = re.compile(r"^\s*([❯↑↓>]\s*)?(\d+)[.)]\s")


_DLG_BORDER_RE = re.compile(r"^\s*[│┃]\s*|\s*[│┃]\s*$")


def _dlg_clean(txt):
    """去掉弹层左右框线（手机上折行后框线会错位），缩进原样保留"""
    s = txt.rstrip()
    s = re.sub(r"^(\s*)[│┃] ?", r"\1", s)
    return re.sub(r"\s*[│┃]$", "", s)


def _dlg_hint_line(t, hints=None, prefixes=None):
    """这一行是不是弹层底栏（按键说明）。

    底栏有固定形状：短句，用 " · " 连成 2~6 段，不带句读标点。正文里引用这些词
    （"按 Esc to cancel 可以取消，Enter to select 是确认。"）有逗号句号、也不是
    这个形状，认不出来——宁可少认，认错了整屏正文会变成一堆可点的"选项"。

    hints/prefixes：不同 agent 的 TUI 底栏文案不一样（Claude 是 "Enter to select"，
    Codex 是 "press enter to confirm"），由调用方按会话所属的 agent 传进来。"""
    s = t.strip()
    if not s:
        return False
    hints = _DLG_HINTS if hints is None else hints
    prefixes = _DLG_HINT if prefixes is None else prefixes
    if len(s) <= 72 and s.startswith(prefixes):
        return True
    n = sum(1 for w in hints if w in s)
    if n < 2 or any(ch in s for ch in "。，；：！？,;"):
        return False
    segs = [x.strip() for x in s.split("·") if x.strip()]
    return 2 <= len(segs) <= 6 and all(len(x) <= 40 for x in segs)


def _dialog_screen(row_dicts, cols, hints=None, prefixes=None):
    """这一屏是不是 TUI 弹层？底栏 + 编号选项行同时出现才算（正文里偶尔出现
    「Enter to select」这种词不会带编号选项）。返回
    {"opts": [(行号, 序号, 是否当前项)], "multi": 多选}，不是则 None。"""
    texts = [_row_plain(r, cols) for r in row_dicts]
    hint = -1
    for y in range(len(texts) - 1, -1, -1):
        if _dlg_hint_line(_dlg_clean(texts[y]), hints, prefixes):
            hint = y
            break
    if hint < 0:
        return None
    # 窄终端上底栏会折成两行（"… · Tab to switch" / "questions · Esc to cancel"），
    # 认到的是最下面那行；往上并到这一组的顶行，选项才是「紧贴底栏」的。
    while hint > 0 and _dlg_hint_line(_dlg_clean(texts[hint - 1]), hints, prefixes):
        hint -= 1
    # 选项从底栏往上找，但只收「紧贴底栏、连成一组」的：最下面一项离底栏 ≤6 行
    # （选项和底栏之间通常就一两个空行），相邻两项也最多隔 6 行（选项之间常夹说明）。
    # 再往上就是正文里的有序列表（"5. launchctl …"），点一下会真的往会话里发方向键
    # 和回车——宁可不认，也不能把正文段落变成可点选项。
    found = []   # (行号, 序号, 标记字符)，从下往上收
    for y in range(hint - 1, max(-1, hint - 34) - 1, -1):
        m = _DLG_OPT_RE.match(_dlg_clean(texts[y]))
        if m:
            # 和「上一个收到的（也就是更靠下的）选项」比间隔；跟最底下那项比会把
            # 一组的头几项（中间隔了说明行）截掉——实测 5 项里只剩后 4 项。
            if found and found[-1][0] - y > 6:
                break
            found.append((y, int(m.group(2)), (m.group(1) or "").strip()))
    if not found or hint - found[0][0] > 6:   # found[0] = 最底下那项，它得贴着底栏
        return None
    found.reverse()
    # 当前项：/model 那种列表会同时标两种——"↑" = 已设为默认、"❯" = 光标所在。
    # 点选要按光标算（cur），标错了会多走几格选到别的模型。
    cur = next((n for _y, n, mk in found if mk.startswith("❯")), 0)
    if not cur:
        cur = next((n for _y, n, mk in found if mk), 0)
    opts = [(y, n, n == cur) for y, n, _mk in found]
    multi = any(ch in _dlg_clean(texts[y]) for y, _n, _s in opts for ch in "☐☑")
    return {"opts": opts, "multi": multi}


def _dialog_html(row_dicts, cols, dlg):
    """弹层按行渲染。选项行带 data-opt=序号，页面点一下就能选（发 ↑/↓ + ⏎）。"""
    opt_of = {y: (n, s) for y, n, s in dlg["opts"]}
    cur = next((n for _y, n, s in dlg["opts"] if s), 0)
    # 弹层平时只有十几行，字号别缩到 6px：正文里代码块可以小，选项必须读得清
    fs = "max(11px, calc((100vw - 52px) / %.1f * var(--fs-scale)))" % (REF_COLS * 0.62)
    out = ['<div class="dblk" data-cur="%d" data-multi="%d" style="font-size:%s">'
           % (cur, 1 if dlg["multi"] else 0, fs)]
    texts = [_row_plain(row, cols) for row in row_dicts]
    # 弹层前面的成片空行不画：40 行的屏上弹层常贴在底部，上面二十几行空白在手机上
    # 就是一大片没内容的内边距，选项被顶到一屏之外。
    start = next((y for y, t in enumerate(texts) if t.strip()), 0)
    for y in range(start, len(row_dicts)):
        row, txt = row_dicts[y], texts[y]
        s = txt.strip()
        if not s:
            out.append('<div class="dsp"></div>')
            continue
        if all(ch in _BOXES for ch in s):      # 整行框线：收成一条细线，别让它折成两行
            out.append('<div class="dln"></div>')
            continue
        body = _dlg_clean(txt)
        if y in opt_of:
            n, sel = opt_of[y]
            out.append('<div class="dopt%s" data-opt="%d">%s</div>'
                       % (" sel" if sel else "", n, _esc_html(body.lstrip())))
        else:
            out.append('<div class="drow">%s</div>' % _esc_html(body))
    out.append('<div class="dtip">%s</div></div>'
               % ("点一项 = 勾选/取消，选完按 ⏎ 确认" if dlg["multi"]
                  else "点一下选项就直接选中"))
    return "".join(out)


_DLG_MISS_AT = [0.0]


def _dialog_miss_note(row_dicts, cols, why=""):
    """底栏在、选项没认出来：限流记一笔。用户报「屏幕上要选，手机上看不到/点不到」
    时，开发机日志里能直接看到当时的屏（前 12 行非空）和口径，不用再猜。"""
    now = time.time()
    if now - _DLG_MISS_AT[0] < 30:
        return
    _DLG_MISS_AT[0] = now
    try:
        texts = [_row_plain(r, cols) for r in row_dicts]
        rows = [t.rstrip()[:110] for t in texts if t.strip()][:12]
        print("[live] 疑似弹层未识别 %s %dx%d | %s"
              % (why, cols, len(texts), " ⏎ ".join(rows)[:600]), flush=True)
    except Exception:
        pass


def _reflow_html(st, hints=None, prefixes=None):
    cols, rows = st["win"]
    rds = [st["scr"].buffer[y] for y in range(rows)]
    dlg = _dialog_screen(rds, cols, hints, prefixes)
    if dlg:
        return _dialog_html(rds, cols, dlg)
    if time.time() - _DLG_MISS_AT[0] >= 30:
        # 只在冷却结束时扫一遍（平时这个函数每秒都跑，别白扫）
        hit = next((y for y in range(rows - 1, -1, -1)
                    if _dlg_hint_line(_dlg_clean(_row_plain(rds[y], cols)), hints, prefixes)), -1)
        if hit >= 0:
            _dialog_miss_note(rds, cols, "hint@%d" % hit)
    return _reflow_rows(rds, cols)
