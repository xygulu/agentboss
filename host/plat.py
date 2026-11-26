#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""平台层 —— 把「去操作别人开的那个终端」这条通道从 server.py 里拆出来。

原来 server.py 直接 osascript 管 Terminal.app。现在收敛到这里：

  macOS ：Terminal.app + AppleScript（逻辑逐字搬过来，行为不变）
  Linux ：tmux（new-session / send-keys / capture-pane）——无头服务器上
          tmux 就是「可见终端」的等价物：能被注入、能被抓屏、能新开。
  其它  ：空实现（Windows 以后接 ConPTY）。

对外面只有这几个函数（server.py 只认它们）：

  window_map()                     -> {tty: (wid, 标题)}   发现终端里的会话
  send_text(tty, text)             -> (ok, msg)            注入一段文本（自带回车）
  send_special(tty, key, kc, data) -> (ok, msg)            注入一个按键
  screen(wid, tty)                 -> (ok, 文本)           读当前屏幕
  no_caps_why(tty, wid)            -> str                  只读的原因（下发到手机端）
  trust_confirm(tty)               -> bool                 信任弹窗：下移一格 + 回车
  open_terminal(cwd, cmd)          -> (ok, msg, 目标)      开一个终端窗口
  open_wrapped(cwd, argv, name)    -> (wid, tty, err)      开一个终端跑 wrapper+claude
  close_window(wid)                -> bool                 关掉终端窗口

注意：注入的第一优先不是这里，而是 wrapper 的控制 socket（pty 原始字节，
毫秒级）；只有老会话（没走 wrapper）才落到本模块的终端通道。
"""

import hashlib
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pathlib as _pathlib
import sys as _sys
# 仓库按组件分目录（host/ 与 shared/ 是兄弟）；发布包是扁平的，品牌模块就在旁边。
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent / "shared"))
import terms

# 细日志开关：中继后台给这台开发机打开「日志上报」时由 server 置位
DBG = [False]

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WIN = os.name == "nt"

_NPM_BIN = str(Path.home() / ".npm-global/bin")
_LOCAL_BIN = str(Path.home() / ".local/bin")      # hermes 等 pipx/uv 装的 CLI 落这里
_EXT_PATH = (_NPM_BIN + ":" + _LOCAL_BIN + ":/usr/local/bin:/opt/homebrew/bin:"
             "/usr/bin:/bin:/usr/sbin:/sbin")


def which(name):
    """找用户装的 CLI。launchd/systemd 起的 host 进程 PATH 只有 /usr/bin:/bin，
    裸 shutil.which(name) 找不到 opencode/hermes——手机上那两个 tab 就消失了
    （2026-11-20 实测）。一律按 _EXT_PATH 找：进程 PATH + 常见安装位。"""
    import shutil
    return shutil.which(name, path=_EXT_PATH)

_FP = []      # 本进程只算一次：硬件指纹在运行期不会变


def _fp_raw():
    """本机硬件标识原文：macOS IOPlatformUUID / Linux /etc/machine-id /
    Windows MachineGuid。重装系统或换主板才会变。"""
    try:
        if IS_MAC:
            out = _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])[1]
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out or "")
            return m.group(1) if m else ""
        if IS_WIN:
            out = _run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography",
                        "/v", "MachineGuid"])[1]
            m = re.search(r"MachineGuid\s+REG_SZ\s+(\S+)", out or "")
            return m.group(1) if m else ""
        p = Path("/etc/machine-id")
        return p.read_text().strip() if p.exists() else ""
    except Exception:
        return ""


def machine_fp():
    """开发机指纹（SHA-256 摘要）。只上报摘要，原文不出本机。
    取不到时返回空串——老中继/无权限场景按「没有指纹」处理，不阻断使用。"""
    if _FP:
        return _FP[0]
    raw = _fp_raw().strip()
    fp = hashlib.sha256(("codeboss-fp:" + raw).encode()).hexdigest() if raw else ""
    _FP.append(fp)
    return fp


def _run(cmd, inp=None, timeout=12):
    env = os.environ.copy()
    env["PATH"] = _EXT_PATH
    env.pop("VIRTUAL_ENV", None)
    try:
        p = subprocess.run(cmd, input=inp, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def shell_cmd(cwd, argv):
    """拼一条给终端跑的 shell 命令（cd + exec）。引号用 shlex.quote，
    比原来手写的 '%s' 稳（路径里有单引号也不会崩）。"""
    parts = []
    if cwd:
        parts.append("cd %s" % shlex.quote(str(cwd)))
    if argv:
        parts.append("exec " + " ".join(shlex.quote(str(a)) for a in argv))
    return " && ".join(parts)


def backend_name():
    if IS_WIN:
        return "windows"
    return "darwin" if IS_MAC else ("linux" if IS_LINUX else sys.platform)


def claude_cmd():
    """在终端里跑 claude 时该用什么命令。

    macOS：终端是登录 shell，PATH 完整，用 "claude" 跟用户手敲完全一致。
    Linux：tmux server 的环境可能是它第一次启动时继承的旧环境，PATH 会缺东西，
           所以优先用绝对路径（找不到就退回 "claude"）。
    Windows：终端里跑的是 Git Bash，`claude`（winget 的 claude.exe / npm 的
           claude.cmd）在 PATH 里能直接敲，和用户手敲一致。"""
    if IS_WIN:
        return "claude"
    if IS_MAC:
        return "claude"
    for cand in ("/usr/bin/claude", "/usr/local/bin/claude",
                 str(Path.home() / ".local/bin/claude"),
                 str(Path.home() / ".npm-global/bin/claude"),
                 "/snap/bin/claude"):
        if os.path.exists(cand):
            return cand
    return "claude"


# ================================ macOS：Terminal.app + AppleScript ================================

LIST_WINDOWS_AS = '''
tell application "Terminal"
\tset out to ""
\trepeat with w in (every window)
\t\tset wid to (id of w) as text
\t\ttry
\t\t\tset wname to (name of w) as text
\t\ton error
\t\t\tset wname to ""
\t\tend try
\t\trepeat with t in (every tab of w)
\t\t\ttry
\t\t\t\tset ttyv to (tty of t) as text
\t\t\ton error
\t\t\t\tset ttyv to ""
\t\t\tend try
\t\t\tif ttyv is not "" then set out to out & wid & "\\t" & ttyv & "\\t" & wname & "\\n"
\t\tend repeat
\tend repeat
\treturn out
end tell
'''

DO_SCRIPT_AS = """
on run
\ttell application "Terminal"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\tif tty of t is "__TTY__" then
\t\t\t\t\tdo script __PAYLOAD__ in contents of t
\t\t\t\t\treturn
\t\t\t\tend if
\t\t\tend repeat
\t\tend repeat
\t\terror "找不到会话的终端窗口（可能已关闭）"
\tend tell
end run
"""

KEYCODE_AS = """
on run
\ttell application "Terminal"
\t\tactivate
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\tif tty of t is "__TTY__" then
\t\t\t\t\tset index of w to 1
\t\t\t\t\tset selected tab of w to contents of t
\t\t\t\t\tset frontmost of w to true
\t\t\t\t\treturn
\t\t\t\tend if
\t\t\tend repeat
\t\tend repeat
\t\terror "找不到会话的终端窗口（可能已关闭）"
\tend tell
\tset okf to false
\trepeat with i from 1 to 20
\t\tdelay 0.1
\t\ttell application "System Events" to set fn to (name of first application process whose frontmost is true)
\t\tif fn is "Terminal" then
\t\t\tset okf to true
\t\t\texit repeat
\t\tend if
\tend repeat
\tif not okf then error "Terminal 无法置前（Mac 锁屏时方向键不可用，解锁后重试）"
\tdelay 0.1
\ttell application "System Events" to key code __KC__
end run
"""


def _as_expr(data: bytes) -> str:
    """字节序列 -> AppleScript 字符串表达式，控制字节用 character id（实测可透传到 tty）"""
    parts = []
    for b in data:
        if b == 34 or b < 32 or b > 126:
            parts.append("(character id %d)" % b)
        else:
            parts.append('"%s"' % chr(b))
    return " & ".join(parts) if parts else '""'


def _as_sh(s: str) -> str:
    """塞进 AppleScript 双引号字符串里的转义"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _darwin_window_map():
    """tty -> (window_id, 标题)"""
    _, out, _ = _run(["osascript", "-e", LIST_WINDOWS_AS])
    m = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        wid, tty, title = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if wid and tty:
            try:
                m[tty] = (int(wid), title)
            except ValueError:
                pass
    return m


def _darwin_send_text(tty, text):
    lit = text.replace("\\", "\\\\").replace('"', '\\"')
    script = (DO_SCRIPT_AS.replace("__PAYLOAD__", '"%s"' % lit)
              .replace("__TTY__", tty))
    code, out, err = _run(["osascript", "-"], inp=script, timeout=20)
    return code == 0, ((err or out)[:200] or "ok")


def _darwin_send_special(tty, key, kc, data):
    if kc is not None:   # 方向键：System Events key code（会抢焦点、需辅助功能权限）
        script = (KEYCODE_AS.replace("__KC__", str(kc))
                  .replace("__TTY__", tty))
        code, out, err = _run(["osascript", "-"], inp=script, timeout=20)
        if code != 0 and "assistive" in (err or "").lower():
            return False, "需要辅助功能权限：系统设置→隐私与安全性→辅助功能"
        if code == 0:
            return True, "ok（旧会话无控制通道，靠系统事件注入，可能无反应）"
        return False, ((err or out)[:200] or "ok")
    script = (DO_SCRIPT_AS.replace("__PAYLOAD__", _as_expr(data))
              .replace("__TTY__", tty))
    code, out, err = _run(["osascript", "-"], inp=script, timeout=20)
    return code == 0, ((err or out)[:200] or "ok")


def _darwin_screen(wid):
    code, out, err = _run(["osascript", "-e",
        'tell application "Terminal" to get contents of tab 1 of window id %d' % wid],
        timeout=8)
    if code != 0:
        return False, ("读取失败: %s" % (err or out))
    return True, out


def _darwin_trust_confirm(tty):
    """do script 自带回车，正好把「Yes, I trust this folder」确认掉"""
    parts = ["(character id %d)" % ord(ch) if ord(ch) < 32
             else '"%s"' % ch.replace('"', '\\"') for ch in "\x1b[B"]
    script = (DO_SCRIPT_AS.replace("__PAYLOAD__", " & ".join(parts))
              .replace("__TTY__", tty))
    code, _o, _e = _run(["osascript", "-"], inp=script, timeout=10)
    return code == 0


def _darwin_send_enter(tty):
    """只发一个回车。do script 自带回车，所以传空串即可。"""
    script = (DO_SCRIPT_AS.replace("__PAYLOAD__", '""')
              .replace("__TTY__", tty))
    code, _o, _e = _run(["osascript", "-"], inp=script, timeout=10)
    return code == 0


def _darwin_send_down(tty):
    """只发一次「下箭头」，**不带回车**（方向键+回车会重发上一条）。
    走 System Events key code：会抢焦点、需辅助功能权限。"""
    script = (KEYCODE_AS.replace("__KC__", "125")
              .replace("__TTY__", tty))
    code, _o, _e = _run(["osascript", "-"], inp=script, timeout=20)
    return code == 0


def _darwin_open_terminal(cwd, cmd):
    sh = "cd %s" % shlex.quote(cwd)
    if cmd:
        sh += " && " + cmd
    as_cmd = 'tell application "Terminal" to do script "%s"' % _as_sh(sh)
    code, _out, err = _run(["osascript", "-e", as_cmd], timeout=10)
    if code != 0:
        return False, ((err or "").strip() or "打开失败")[:200], ""
    return True, "ok", ""


def _darwin_open_wrapped(cwd, argv, name=None):
    sh = shell_cmd(cwd, argv)
    as_cmd = ('tell application "Terminal"\n'
              'set t to do script "%s"\n'
              'return ((id of front window) as text) & "," & (tty of t)\n'
              'end tell') % _as_sh(sh)
    code, out, err = _run(["osascript", "-e", as_cmd], timeout=15)
    if code != 0:
        return None, None, ((err or "").strip() or "打开失败")[:200]
    try:
        w, tty = out.strip().split(",", 1)
        return int(w), tty.strip(), ""
    except Exception:
        return None, None, ""


def _darwin_launch_script(cwd, argv, name=None):
    """把「cd + exec」写成 .sh，返回脚本路径（给不能直接传命令的终端用）。"""
    p = Path("/tmp") / ("codeboss-%s.sh" % (name or os.getpid()))
    p.write_text("#!/bin/zsh\n" + shell_cmd(cwd, argv) + "\n")
    p.chmod(0o755)
    return p


def _darwin_open_app(args):
    """`open` 一个 app。清掉 CLAUDE*/ANTHROPIC* 环境变量——带着它们启动的 claude
    会认为自己是子会话，直接关掉转录保存，host 就再也发现不了这个会话。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["PATH"] = _EXT_PATH
    try:
        p = subprocess.run(["open"] + list(args), capture_output=True,
                           text=True, timeout=15, env=env)
        return p.returncode, (p.stderr or "").strip()
    except Exception as e:
        return -1, str(e)


# 没有 AppleScript 字典、但能用命令行跑脚本的终端（终端自己注册的命令行参数）：
# TERM_PROGRAM -> (app 名, 参数模板；{sh} 是启动脚本，{shdir} 是它所在目录)
# 都能被 `open -na` 拉起；任何一步失败自动落回 Terminal.app，不会把会话开丢。
_SCRIPT_TERMS = {
    "WezTerm": ("WezTerm", ["start", "--", "/bin/zsh", "{sh}"]),
    "ghostty": ("Ghostty", ["-e", "/bin/zsh", "{sh}"]),
    "kitty":   ("kitty",   ["/bin/zsh", "{sh}"]),
}


def _darwin_open_script_term(cwd, argv, name, app, argtpl):
    """`open -na <app> --args ...` 跑启动脚本。拿不到窗口 id/tty，host 按
    「无窗口 + 全彩镜像」处理（和 Warp 一样：读写都走 wrapper 的 socket 和日志）。"""
    try:
        sh = _darwin_launch_script(cwd, argv, name)
    except OSError as e:
        return None, None, "写启动脚本失败: %s" % e
    code, err = _darwin_open_app(["-na", app, "--args"] +
                                 [x.replace("{sh}", str(sh)).replace("{shdir}", str(sh.parent))
                                  for x in argtpl])
    if code != 0:
        return None, None, (err or ("打开 %s 失败" % app))[:200]
    return None, None, ""


def _darwin_open_warp(cwd, argv, name=None):
    """Warp：写成 .sh，靠它注册的 shell 脚本文档类型执行（`open -a Warp x.sh`）。"""
    try:
        sh = _darwin_launch_script(cwd, argv, name)
    except OSError as e:
        return None, None, "写启动脚本失败: %s" % e
    code, err = _darwin_open_app(["-a", "Warp", str(sh)])
    if code != 0:
        return None, None, (err or "打开 Warp 失败")[:200]
    return None, None, ""


def _darwin_open_iterm(cwd, argv, name=None):
    """iTerm2 有 AppleScript：直接开窗口跑命令，能拿到 tty。"""
    as_cmd = ('tell application "iTerm"\n'
              'set w to create window with default profile command "%s"\n'
              'return tty of current session of w\n'
              'end tell') % _as_sh(shell_cmd(cwd, argv))
    code, out, err = _run(["osascript", "-e", as_cmd], timeout=15)
    if code != 0:
        return None, None, ((err or "").strip() or "打开失败")[:200]
    return None, out.strip() or None, ""


def _darwin_close_window(wid):
    try:
        code, _o, _e = _run(["osascript", "-e",
            'tell application "Terminal" to close window id %d saving no' % int(wid)],
            timeout=6)
        return code == 0
    except Exception:
        return False


# ==================================== Linux：tmux ====================================

# tmux 的按键名（tmux 3.x）
_TMUX_KEYS = {
    "esc": "Escape", "shift_tab": "BTab", "ctrl_c": "C-c", "ctrl_l": "C-l",
    "ctrl_d": "C-d", "enter": "Enter", "tab": "Tab", "ctrl_a": "C-a",
    "ctrl_e": "C-e", "ctrl_u": "C-u", "ctrl_k": "C-k", "ctrl_w": "C-w",
    "ctrl_r": "C-r", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right", "space": "Space", "backspace": "BSpace",
}

_TMUX_FMT = "#{pane_tty}\t#{pane_id}\t#{pane_title}\t#{pane_current_command}"


def _tmux(args, timeout=10):
    return _run(["tmux"] + list(args), timeout=timeout)


def _linux_panes():
    """[(tty, pane_id, title)]，没有 tmux server 时返回空"""
    code, out, _ = _tmux(["list-panes", "-a", "-F", _TMUX_FMT], timeout=6)
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        tty = parts[0].strip()
        pid = parts[1].strip()
        title = (parts[2].strip() if len(parts) > 2 else "")
        cmd = (parts[3].strip() if len(parts) > 3 else "")
        if not tty or not pid:
            continue
        rows.append((tty, pid, title or cmd))
    return rows


def _linux_window_map():
    """tty -> (pane_id, 标题)"""
    return {tty: (pid, title) for tty, pid, title in _linux_panes()}


def _linux_target(tty):
    """tty -> pane_id（每次现查：pane 关了 id 就失效，缓存会指到别人）"""
    for t, pid, _title in _linux_panes():
        if t == tty:
            return pid
    return None


def _linux_send_keys(target, *keys):
    args = ["send-keys", "-t", target]
    args.extend(keys)
    code, _o, err = _tmux(args, timeout=8)
    return code == 0, (err or "")[:200]


def _linux_send_text(tty, text):
    target = _linux_target(tty)
    if not target:
        return False, "会话所在终端已关闭（可能已退出 tmux）"
    text = text.replace("\r", " ").replace("\n", " ")
    if not text:
        return False, "内容为空"
    # 先按字面量敲进去，再单独发回车（-l 时回车必须单独一条命令）
    ok, err = _linux_send_keys(target, "-l", "--", text)
    if not ok:
        return False, err or "写入失败"
    ok, err = _linux_send_keys(target, "Enter")
    if not ok:
        return False, err or "回车失败"
    return True, "ok"


def _linux_send_special(tty, key, kc=None, data=None):
    name = _TMUX_KEYS.get(key)
    if not name:
        return False, "未知按键: %s" % key
    target = _linux_target(tty)
    if not target:
        return False, "会话所在终端已关闭（可能已退出 tmux）"
    ok, err = _linux_send_keys(target, name)
    return (True, "ok") if ok else (False, err or "按键失败")


def _linux_screen(wid, tty):
    target = (_linux_target(tty) if tty else None) or wid
    if not target:
        return False, "未找到对应的终端（可能已关闭）"
    code, out, err = _tmux(["capture-pane", "-p", "-t", target], timeout=8)
    if code != 0:
        return False, ("读取失败: %s" % (err or out))
    return True, out


def _linux_trust_confirm(tty):
    target = _linux_target(tty)
    if not target:
        return False
    ok, _e = _linux_send_keys(target, "Down")
    if not ok:
        return False
    ok, _e = _linux_send_keys(target, "Enter")
    return ok


def _linux_send_enter(tty):
    target = _linux_target(tty)
    if not target:
        return False
    ok, _e = _linux_send_keys(target, "Enter")
    return ok


def _linux_send_down(tty):
    target = _linux_target(tty)
    if not target:
        return False
    ok, _e = _linux_send_keys(target, "Down")
    return ok


def _linux_tmux_name(prefix):
    return "%s-%s" % (prefix, time.strftime("%m%d-%H%M%S"))


def _interactive_shell():
    """找本机可用的交互 shell（Linux 上多半是 bash，zsh 不一定装了）。"""
    for c in (os.environ.get("SHELL"), "/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if c and os.path.exists(c):
            return c
    return "/bin/sh"


def _linux_open_terminal(cwd, cmd):
    name = _linux_tmux_name("codeboss-term")
    sh = "cd %s" % shlex.quote(str(cwd))
    if cmd:
        sh += "; " + cmd
    # 命令跑完留一个交互 shell（否则会话随命令结束一起消失，
    # 用户 attach 进去看到的是「会话已死」）
    sh += "; exec %s -i" % shlex.quote(_interactive_shell())
    code, _o, err = _tmux(["new-session", "-d", "-s", name, "-x", "120", "-y", "40", sh],
                          timeout=10)
    if code != 0:
        return False, ((err or "").strip() or "打开失败")[:200], ""
    return True, "ok", name


def _linux_open_wrapped(cwd, argv, name=None):
    name = name or _linux_tmux_name("codeboss")
    # tmux server 的环境是它第一次启动时继承的，PATH 可能很旧；
    # 把我们的 PATH 拼在前面（保留原 PATH，用户自己的路径不丢）
    sh = "export PATH=%s:\"$PATH\" && %s" % (shlex.quote(_EXT_PATH),
                                             shell_cmd(cwd, argv))
    code, _o, err = _tmux(["new-session", "-d", "-s", name, "-x", "120", "-y", "40", sh],
                          timeout=10)
    if code != 0:
        err = (err or "").strip()
        if "duplicate session" in err.lower():     # 同名残留：先收掉再开
            _tmux(["kill-session", "-t", name], timeout=6)
            code, _o, err = _tmux(["new-session", "-d", "-s", name, "-x", "120",
                                    "-y", "40", sh], timeout=10)
        if code != 0:
            return None, None, ((err or "").strip() or "打开失败")[:200]
    code, out, _e = _tmux(["list-panes", "-t", name, "-F", "#{pane_tty}\t#{pane_id}"],
                          timeout=6)
    if code != 0:
        return None, None, "终端开了但找不到面板"
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            return parts[1].strip(), parts[0].strip(), ""
    return None, None, "终端开了但找不到面板"


def _linux_close_window(wid):
    if not wid:
        return False
    code, _o, _e = _tmux(["kill-pane", "-t", str(wid)], timeout=6)
    return code == 0


# ============================== Windows：只托管自己起的会话 ==============================
# Windows 没有可稳定注入的终端标签页（Windows Terminal 没有 AppleScript 那种通道；
# 往别人开的窗口里打字只能靠 SendInput 抢焦点，不可靠也不礼貌）。所以：
#   - host 自己用 ConPTY 起的会话（pty_backend.WinPty）从手机端完全可写，这是主路径；
#   - 别人在 Windows Terminal / cmd 里开的 claude：只读转录 + 可「召回」成新窗口；
#   - 召回窗口里跑的是不带 wrapper 的 `claude --resume`，手机端能看转录，但不能往里打字。

def _win_which(exe):
    import shutil
    return shutil.which(exe) or ""


def _win_open(cwd, argv):
    """在可见终端里跑 argv。优先 Windows Terminal（wt.exe），退 cmd 的 start。
    返回 (ok, err)。"""
    cwd = str(cwd or "")
    wt = _win_which("wt.exe") or _win_which("wt")
    if wt:
        args = [wt, "-d", cwd, "--"]
        args += list(argv)
        try:
            subprocess.Popen(args, close_fds=True)
            return True, ""
        except Exception as e:
            return False, str(e)[:200]
    # 没有 Windows Terminal：START 一个新窗口跑 cmd（命令行里引号要自己带）
    inner = subprocess.list2cmdline([str(a) for a in argv])
    try:
        subprocess.Popen("start \"codeboss\" cmd.exe /d /s /c %s" % inner,
                         shell=True, cwd=cwd, close_fds=True)
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def _win_strip_wrapper(argv):
    """召回时上层传的是 [python, wrapper.py, (--meta …), --, claude, …]。
    Windows 没有 wrapper.py（它依赖 fcntl/termios），把壳剥掉只留真正的命令。"""
    argv = [str(a) for a in argv]
    for i, a in enumerate(argv):
        if a == "--" and i + 1 < len(argv):
            return argv[i + 1:]
    # 没找到 `--` 就退一步：从第一个像 claude 的参数开始
    for i, a in enumerate(argv):
        if os.path.basename(a).lower().startswith("claude"):
            return argv[i:]
    return argv


def _win_open_terminal(cwd, cmd):
    """开一个终端窗口（可带一条命令），命令跑完留在 shell 里。"""
    import pty_backend
    bash = pty_backend.find_git_bash()
    if bash:
        argv = [bash, "-l"]
        if cmd:
            # 跑完命令留一个交互 shell（否则窗口一闪就没了）
            argv += ["-c", '%s; exec "%s" -i' % (cmd, bash.replace('"', '\\"'))]
        ok, err = _win_open(cwd, argv)
    else:
        argv = [_win_which("cmd.exe") or "cmd.exe", "/d"]
        if cmd:
            argv += ["/k", cmd]
        ok, err = _win_open(cwd, argv)
    return (True, "ok", "") if ok else (False, (err or "打开失败")[:200], "")


def _win_open_wrapped(cwd, argv):
    inner = _win_strip_wrapper(argv)
    ok, err = _win_open(cwd, inner)
    if not ok:
        return None, None, (err or "打开失败")[:200]
    # 没有窗口 id / tty 可回传：上层在 Windows 上不做「核实起来了」的轮询
    return "", "", ""


def _win_send_text(tty, text):
    return False, "Windows 只能操作 Codeboss 自己托管的会话（请在手机端新开一个会话）"


def _win_nosupport():
    return False, "Windows 暂不支持直接操作本机其它终端窗口"


# ==================================== 对外接口 ====================================

# 窗口表缓存：一轮发现里 Claude 和 Codex 都要这张表（macOS 上是一次 osascript，
# 约 1s）。没有缓存就每 2s 跑两遍，整轮发现直接翻倍。TTL 比发现间隔短，
# 新开的窗口下一轮就能认出来。
_WMAP = {"at": 0.0, "data": {}, "busy": False}
_WMAP_TTL = 1.5


def _iterm_target(tty):
    """tty 属于 iTerm2 就返回 "it:<sid>"，否则 ""（Terminal.app 走老实现）。"""
    if not IS_MAC:
        return ""
    try:
        v = window_map().get(tty)
    except Exception:
        return ""
    return v[0] if v and isinstance(v[0], str) and v[0].startswith(terms.IT_PREFIX) else ""


def caps(tty, wid=None):
    """这个终端能给哪些能力（手机端按它渲染，别让用户点了才报错）。

    screen 读屏 / input 写文字 / keys 独立按键 / focus 聚焦 / close 关窗。
    只有 wrapper 会话（有 ctl）拿不到这里的答案——那由 server 自己判。
    """
    tty = (tty or "").strip()
    if not tty:
        return frozenset()
    if IS_MAC:
        try:
            v = window_map().get(tty)
        except Exception:
            v = None
        if v and isinstance(v[0], str) and v[0].startswith(terms.IT_PREFIX):
            return terms.CAPS                      # iTerm2：可读屏、可写文字、可关
        if v and isinstance(v[0], int):
            # Terminal.app：do script 自带回车，方向键要辅助功能权限（不稳），不承诺 keys
            return frozenset({"screen", "input", "close"})
        return frozenset()
    if IS_LINUX:
        try:
            if _linux_target(tty):
                return frozenset({"screen", "input", "keys", "close"})
        except Exception:
            pass
        return frozenset()
    return frozenset()


def no_caps_why(tty, wid=None):
    """caps 为空（只读）的原因，随会话下发给手机端。

    以前只给 link=readonly，界面就一句「只能看」，用户不知道是窗口关了、
    iTerm2 没开，还是这个终端压根没有外部通道。这里只读廉价信息，不跑 osascript
    探测（会话列表要刷，探测会卡事件循环）。
    """
    tty = (tty or "").strip()
    if not tty:
        return "这个会话不是在本机终端窗口里起的，只能看转录；点「接管」后可在手机上输入"
    if IS_MAC:
        return ("没找到可读写的终端窗口：窗口可能已关闭，或这个会话在 Warp / VS Code / "
                "ghostty / kitty 这类没有外部通道的终端里。只能看转录，点「接管」后可输入")
    if IS_LINUX:
        return "这个会话不在 tmux 里（或 tmux 面板已关）：只能看转录，点「接管」后可输入"
    return "当前平台没有向该终端注入的能力：只能看转录"


def term_label(tty, wid=None):
    """给手机端看的终端名：iTerm2/Terminal/tmux。判断不出返回 ""。"""
    tty = (tty or "").strip()
    try:
        v = window_map().get(tty)
    except Exception:
        v = None
    if v and isinstance(v[0], str) and v[0].startswith(terms.IT_PREFIX):
        return "iTerm2"
    if IS_LINUX and v:
        return "tmux"
    if IS_MAC and v:
        return "Terminal"
    return ""


def window_map():
    """tty -> (窗口/面板 id, 标题)。过期就后台刷新，**调用方永远不阻塞**。

    ⚠️ 刷新要跑 osascript（macOS 上能卡 2~12s，超时 12s）。以前过期时同步刷新，
    而推送会话列表（h_ext_push → term_label）是在事件循环线程上取的——日志里
    `[loop] 卡顿 3~18s` 的栈就是它。现在只肯读缓存，刷新交给后台线程；首次
    没数据时先返回空表，一两秒后自然补齐（终端名是展示用的，晚几秒无妨）。"""
    if time.time() - _WMAP["at"] < _WMAP_TTL or _WMAP["busy"]:
        return _WMAP["data"]
    _WMAP["busy"] = True

    def _bg():
        try:
            _window_map_uncached()
        except Exception:
            pass
        finally:
            _WMAP["busy"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return _WMAP["data"]


def _window_map_uncached():
    if IS_MAC:
        d = _darwin_window_map()
        try:
            import terms
            # iTerm2 有 AppleScript：补上 tty -> 会话，用户手敲起的会话才有屏可读、有字可打
            d.update(terms.targets())
        except Exception as e:
            print("[term] iTerm2 探测失败：%s" % str(e)[:120], flush=True)
    elif IS_LINUX:
        d = _linux_window_map()
    else:
        d = {}          # Windows：没有可枚举的终端标签页
    _WMAP["data"] = d or {}          # 后台线程在写；单次赋值，读到的都是完整 dict
    _WMAP["at"] = time.time()
    return _WMAP["data"]


def send_text(tty, text):
    """往 tty 所在终端注入一段文本（自带回车）。"""
    tty = (tty or "").strip()
    if not tty:
        return False, "会话缺少 tty 信息，无法安全定位输入（请重新拉起会话）"
    if IS_MAC:
        if not tty.startswith("/dev/ttys"):
            return False, "会话缺少 tty 信息，无法安全定位输入（请重新拉起会话）"
        tgt = _iterm_target(tty)
        if tgt:
            return terms.send_text(tgt, text)
        return _darwin_send_text(tty, text)
    if IS_LINUX:
        if not tty.startswith("/dev/pts"):
            return False, "会话缺少 tty 信息，无法安全定位输入（请重新拉起会话）"
        return _linux_send_text(tty, text)
    if IS_WIN:
        return _win_send_text(tty, text)
    return False, "当前平台暂不支持向外部终端注入"


def send_special(tty, key, kc=None, data=None):
    """往 tty 所在终端注入一个按键。"""
    tty = (tty or "").strip()
    if not tty:
        return False, "未找到对应的终端窗口（可能已关闭）"
    if IS_MAC:
        if not tty.startswith("/dev/ttys"):
            return False, "会话缺少 tty 信息，无法安全定位输入（请重新拉起会话）"
        tgt = _iterm_target(tty)
        if tgt:
            # iTerm2 也能发单键：write text + newline no 往 pty 写转义序列。
            # 以前这里硬返回 False，手机上的方向键/回车全废，确认框只能靠「接管」。
            raw = terms.KEY_BYTES.get(key) or data
            if not raw:
                return False, "iTerm2 发不了这个按键（可在手机端点「接管」转成全彩）"
            return terms.send_key(tgt, raw)
        return _darwin_send_special(tty, key, kc, data)
    if IS_LINUX:
        if not tty.startswith("/dev/pts"):
            return False, "会话缺少 tty 信息，无法安全定位输入（请重新拉起会话）"
        return _linux_send_special(tty, key, kc, data)
    if IS_WIN:
        return _win_send_text(tty, "")
    return False, "当前平台暂不支持向外部终端注入"


# 读屏只回尾部：跑了几千行的 iTerm2/Terminal 窗口整包回传一次就是几百 KB～MB，
# 手机屏幕只看得见最后几十行，前面的只能喂大模型。尾部 200 行够用（可环境变量调）。
try:
    SCREEN_TAIL = max(40, int(os.environ.get("CODEBOSS_SCREEN_TAIL") or 200))
except ValueError:
    SCREEN_TAIL = 200


def _tail(text, n=None):
    lines = text.splitlines()
    n = n or SCREEN_TAIL
    return text if len(lines) <= n else "\n".join(lines[-n:])


def screen(wid, tty=""):
    """读终端当前屏幕文本（只回尾部 SCREEN_TAIL 行）。"""
    ok, text = _screen_raw(wid, tty)
    return (ok, _tail(text)) if ok and text else (ok, text)


def _screen_raw(wid, tty=""):
    if IS_MAC:
        if terms.owns(wid):
            return terms.screen(wid)
        if not wid:
            return False, "未找到对应的 Terminal 窗口（可能已关闭；该终端没有读屏通道，可在手机端接管）"
        try:
            wid = int(wid)
        except Exception:
            return False, "未找到对应的 Terminal 窗口（可能已关闭）"
        return _darwin_screen(wid)
    if IS_LINUX:
        return _linux_screen(wid, tty)
    if IS_WIN:
        return False, "Windows 抓不到本机其它终端窗口的屏幕（会话内容请从转录看）"
    return False, "当前平台暂不支持抓取外部终端屏幕"


def trust_confirm(tty):
    """信任弹窗：默认停在 No，下移一格选中 Yes 再回车。"""
    tty = (tty or "").strip()
    if not tty:
        return False
    if IS_MAC:
        return _darwin_trust_confirm(tty)
    if IS_LINUX:
        return _linux_trust_confirm(tty)
    return False


def send_enter(tty):
    """只发一个回车（不带其它字符）。"""
    tty = (tty or "").strip()
    if not tty:
        return False
    if IS_MAC:
        return _darwin_send_enter(tty)
    if IS_LINUX:
        return _linux_send_enter(tty)
    return False


def send_down(tty):
    """只发一次「下箭头」，不带回车（方向键+回车=重发上一条）。"""
    tty = (tty or "").strip()
    if not tty:
        return False
    if IS_MAC:
        return _darwin_send_down(tty)
    if IS_LINUX:
        return _linux_send_down(tty)
    return False


def open_terminal(cwd, cmd=""):
    """开一个终端窗口（可带一条命令）。返回 (ok, msg, 目标)。"""
    if IS_MAC:
        return _darwin_open_terminal(cwd, cmd)
    if IS_LINUX:
        return _linux_open_terminal(cwd, cmd)
    if IS_WIN:
        return _win_open_terminal(cwd, cmd)
    return False, "当前平台暂不支持打开本机终端", ""


def open_wrapped(cwd, argv, name=None, term=""):
    """开一个终端跑 wrapper + claude。返回 (窗口/面板 id, tty, err)。

    term 是原会话所在的终端（wrapper 记的 TERM_PROGRAM）：换模型/召回时尽量回到
    同一个终端。只有确认能用脚本方式启动的才特殊处理，其余（含未知）落回
    Terminal.app——宁可换终端，也不往 VS Code 这类会把 .sh 当文件打开的 app 里塞。"""
    if IS_MAC:
        # 走哪条路、最后哪条路成功了要能看见：第三方终端「点了没反应」只能靠这行定位
        how = term or "Terminal.app"
        r = None
        if term in ("WarpTerminal", "Warp"):
            r = _darwin_open_warp(cwd, argv, name)
        elif term in ("iTerm.app", "iTerm2"):
            r = _darwin_open_iterm(cwd, argv, name)
        elif term in _SCRIPT_TERMS:
            app, tpl = _SCRIPT_TERMS[term]
            r = _darwin_open_script_term(cwd, argv, name, app, tpl)
        if r is None or r[2]:
            how += "→Terminal.app" + ("(%s)" % (r[2] or "")[:60] if r and r[2] else "")
            r = _darwin_open_wrapped(cwd, argv, name)
        if DBG[0]:   # 只在后台开了日志上报时记（server 切这个开关）
            print("[term] %s %s：wid=%s tty=%s err=%s"
                  % (name, how, r[0], r[1], (r[2] or "")[:120]), flush=True)
        return r
    if IS_LINUX:
        return _linux_open_wrapped(cwd, argv, name)
    if IS_WIN:
        return _win_open_wrapped(cwd, argv)
    return None, None, "当前平台暂不支持打开本机终端"


def close_window(wid):
    """关掉终端窗口/面板（失败也无妨，大不了留在 shell 提示符）。"""
    if not wid:
        return False
    if IS_MAC:
        if terms.owns(wid):
            return terms.close(wid)
        return _darwin_close_window(wid)
    if IS_LINUX:
        return _linux_close_window(wid)
    return False
