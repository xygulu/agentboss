#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Windows 会话窗口：把 host 用 ConPTY 托管的会话显示在电脑上，也能在电脑上直接敲。

ConPTY 本身没有窗口——0.8.44 起 Windows 的新会话全归 host 托管，手机上能看能发，
但电脑桌面上一片安静，用户以为「会话没开」。这个窗口是同一块 pyte 屏的另一个显示面：
真正跑着的还是 host 里那一个 claude/codex，手机和这里看到的是同一条会话。

- 关掉窗口不影响会话（手机上照常用）；重新打开就再起一个窗口。
- 走本机 HTTP（token.txt + logs/port.txt），不碰任何外网通道。
- 屏幕来自 /api/ext_cells（带颜色的游程）；老 host 没有这个接口时退回 /api/ext_screen
  的纯文本，照样能看能敲。
"""

import json
import queue
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "token.txt"
PORT_FILE = HERE / "logs" / "port.txt"
DEFAULT_PORT = 8790

DEF_FG = "#e6edf3"
DEF_BG = "#0b0e14"

# keysym / 字符 → 发给 PTY 的字节
_KEYS = {
    "Return": "\r", "KP_Enter": "\r", "BackSpace": "\x7f", "Tab": "\t",
    "Escape": "\x1b", "Up": "\x1b[A", "Down": "\x1b[B", "Right": "\x1b[C",
    "Left": "\x1b[D", "Home": "\x1b[H", "End": "\x1b[F", "Delete": "\x1b[3~",
    "Prior": "\x1b[5~", "Next": "\x1b[6~", "Insert": "\x1b[2~",
}


def key_bytes(keysym, char="", ctrl=False, alt=False):
    """Tk 的按键事件 → 写进终端的字符串（不可打印/组合键都在这张表里）。"""
    ks = keysym or ""
    if ctrl and len(ks) == 1 and ks.isalpha():
        out = chr(ord(ks.lower()) - 96)
    elif ks in _KEYS:
        out = _KEYS[ks]
    elif char and ord(char) >= 32:
        out = char
    else:
        out = ""
    return ("\x1b" + out) if (alt and out) else out


def _token():
    try:
        for ln in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                return ln
    except OSError:
        pass
    return ""


def _base():
    """host 换过端口会写 logs/port.txt（被别的程序占用时 8790 起不来）。"""
    port = DEFAULT_PORT
    try:
        n = int((PORT_FILE.read_text(encoding="utf-8").strip() or "0"))
        if 0 < n < 65536:
            port = n
    except Exception:
        pass
    return "http://127.0.0.1:%d" % port


def _call(path, data=None, timeout=4):
    url = _base() + path
    url += ("&" if "?" in url else "?") + "t=" + urllib.parse.quote(_token())
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def _worker(sid, outq, inq, stop):
    """后台线程：拉屏幕 + 把按键写回去。放线程里做，host 卡住时窗口不跟着卡。"""
    cells_ok = [True]   # 老 host 没有 /api/ext_cells：一次 404 后就一直走纯文本
    while not stop.is_set():
        try:
            while True:
                data = inq.get_nowait()
                try:
                    r = _call("/api/ext_write", {"sid": sid, "data": data}, timeout=2)
                    if not (r or {}).get("ok"):
                        outq.put(("err", "敲键没送进去：%s" % ((r or {}).get("error") or "host 拒绝")))
                except Exception as e:
                    outq.put(("err", "敲键没送进去：%s" % str(e)[:60]))
        except queue.Empty:
            pass
        got = False
        if cells_ok[0]:
            try:
                outq.put(("cells", _call("/api/ext_cells/" + urllib.parse.quote(sid), timeout=3)))
                got = True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    cells_ok[0] = False
                else:
                    outq.put(("err", str(e)[:80]))
            except Exception as e:
                outq.put(("err", str(e)[:80]))
        if not got and not cells_ok[0]:
            try:
                outq.put(("scr", _call("/api/ext_screen/" + urllib.parse.quote(sid), timeout=3)))
            except Exception as e:
                outq.put(("err", str(e)[:80]))
        stop.wait(0.5)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sid, title = "", ""
    i = 0
    while i < len(argv):
        if argv[i] == "--sid" and i + 1 < len(argv):
            sid = argv[i + 1]; i += 2
        elif argv[i] == "--title" and i + 1 < len(argv):
            title = argv[i + 1]; i += 2
        else:
            i += 1
    if not sid:
        sys.stderr.write("用法: ptywin.py --sid <sid> [--title <名字>]\n")
        return 2

    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title("Codeboss · %s（%s）" % (title or "会话", sid[:8]))
    root.geometry("920x620")
    root.configure(bg=DEF_BG)
    fam = "Consolas" if "Consolas" in tkfont.families() else "Courier New"
    txt = tk.Text(root, bg=DEF_BG, fg=DEF_FG, relief="flat", wrap="none",
                  font=(fam, 11), padx=10, pady=8, insertbackground=DEF_FG,
                  insertwidth=7, selectbackground="#264f78")
    txt.pack(fill="both", expand=True)
    # 不能设 state="disabled"：禁用的 Text 收不到键盘事件，窗口就「敲什么都没反应」。
    # 只读靠 <KeyPress> 返回 "break"（类绑定被截住，文字不会被本地改掉）。
    tags = {}

    def tag_of(fg, bg, bold, ital, under, rev):
        if rev:
            fg, bg = bg or DEF_BG, fg or DEF_FG
        key = (fg, bg, bold, ital, under)
        name = tags.get(key)
        if name:
            return name
        name = "t%d" % len(tags)
        kw = {"foreground": fg or DEF_FG, "font": (fam, 11, "bold") if bold else (fam, 11)}
        if bg:
            kw["background"] = bg
        if under:
            kw["underline"] = 1
        txt.tag_configure(name, **kw)
        tags[key] = name
        return name
    bar = tk.Label(root, text="连接中…", anchor="w", bg="#11161f", fg="#7d8590",
                   font=("Microsoft YaHei UI", 9), padx=10, pady=4)
    bar.pack(fill="x")

    outq, inq, stop = queue.Queue(), queue.Queue(), threading.Event()
    threading.Thread(target=_worker, args=(sid, outq, inq, stop), daemon=True).start()

    last = [None]

    def send(s):
        if s:
            inq.put(s)

    def on_key(e):
        ctrl = bool(e.state & 0x4)
        alt = bool(e.state & 0x20000) or bool(e.state & 0x8)
        if ctrl and e.keysym.lower() == "v":
            try:
                send(root.clipboard_get())
            except Exception:
                pass
            return "break"
        if ctrl and e.keysym.lower() == "c" and txt.tag_ranges("sel"):
            return ""          # 有选中就按系统的复制走（Ctrl+C 复制，Ctrl+C 无选中=发给终端）
        send(key_bytes(e.keysym, e.char, ctrl, alt))
        return "break"

    txt.bind("<KeyPress>", on_key)
    txt.bind("<Button-1>", lambda e: (txt.focus_force(), ""))
    txt.focus_force()

    def render_cells(d):
        cells = d.get("cells") or []
        if not cells:
            render_plain("（空）")
            return
        y0 = txt.yview()[0]
        at_bottom = txt.yview()[1] >= 0.999
        txt.delete("1.0", "end")
        for row in cells:
            for fg, bg, bold, ital, under, rev, t in row:
                txt.insert("end", t, tag_of(fg, bg, bold, ital, under, rev))
            txt.insert("end", "\n")
        # 光标：Tk 聚焦时会在 insert 标记处画竖条（块状，insertwidth=7）
        cy, cx = d.get("cursor") or [0, 0]
        nhist = int(d.get("nhist") or 0)
        txt.mark_set("insert", "%d.%d" % (nhist + int(cy) + 1, int(cx)))
        if at_bottom:
            txt.see("end")
        else:
            txt.yview_moveto(y0)

    def render_plain(text):
        at_bottom = txt.yview()[1] >= 0.999
        txt.delete("1.0", "end")
        txt.insert("1.0", text)
        if at_bottom:
            txt.see("end")

    def tick():
        try:
            while True:
                kind, val = outq.get_nowait()
                if kind == "cells":
                    sig = (val.get("cells"), val.get("cursor"), val.get("nhist"))
                    if sig == last[0]:
                        continue
                    last[0] = sig
                    render_cells(val)
                    st = "运行中" if val.get("ok") else "读不到会话"
                    bar.config(text=("● %s · 和手机看到的是同一个会话，关掉窗口不影响它" % st))
                elif kind == "scr":
                    text = val.get("screen") or "（空）"
                    if text == last[0]:
                        continue
                    last[0] = text
                    render_plain(text)
                    bar.config(text=("● %s · 和手机看到的是同一个会话，关掉窗口不影响它"
                                     % (val.get("state") or "运行中")) if val.get("ok")
                               else "× 读不到会话（可能已结束）")
                else:
                    bar.config(text="× %s" % val)
        except queue.Empty:
            pass
        root.after(80, tick)

    def bye():
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", bye)
    root.after(50, tick)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
