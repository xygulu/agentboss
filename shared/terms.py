#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""终端适配器：tty -> 它属于哪个终端窗口，以及能不能读屏/写文字/关窗。

为什么单独一层：会话发现靠 `claude agents --json`，跟终端无关；但「把字打进
用户自己也开着的那个窗口」必须经过终端自己的 API。macOS 上原来只有 Terminal.app
有通道，iTerm2 明明有 AppleScript，用户手敲起的会话却只能看转录。这里补的就是这段。

先只收「有稳定 API」的终端：
  Terminal.app（沿用 plat.py 的旧实现，不在这里重写）
  iTerm2     （AppleScript：列 tty、读 contents、write text、关闭）
  tmux       （Linux，沿用 plat.py 的旧实现）
其余（Warp / VS Code / ghostty / kitty / Windows Terminal）没有可靠写入口，
不假装支持：会话照样能发现、能看转录，要输入就走「接管」转成 wrapper 会话。

id 约定：Terminal.app 用整数窗口号；iTerm2 用字符串 "it:<session-id>"。
调用方（plat.py）只做类型分派，不需要知道各终端的细节。
"""
import subprocess
import time

IT_PREFIX = "it:"

_PROBE = {"at": 0.0, "ok": False}
_PROBE_TTL = 60.0


def _run(script, timeout=10):
    """跑一段 AppleScript（脚本从 stdin 进，免去多层引号转义）。"""
    script = script.replace('tell application "iTerm2"',
                            'tell application "%s"' % APP)
    try:
        p = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


APP = "iTerm2"          # iTerm2 3.5+ 的应用名；老版本（iTerm）会自动回退


def available():
    """iTerm2 在不在、且正在运行（没运行就别去 tell，会把它拉起来）。60s 缓存。"""
    global APP
    now = time.time()
    if now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["ok"]
    ok = False
    for app in ("iTerm2", "iTerm"):
        code, out, _err = _run('application "%s" is running' % app, timeout=5)
        if code == 0 and out.strip().lower() == "true":
            APP, ok = app, True
            break
    _PROBE["at"], _PROBE["ok"] = now, ok
    return ok


_LIST_AS = '''
tell application "iTerm2"
  set out to ""
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          set out to out & (tty of s) & tab & (id of s) & tab & (name of s) & tab & (id of w) & linefeed
        end try
      end repeat
    end repeat
  end repeat
  return out
end tell
'''


def targets():
    """{tty: ("it:<session-id>", 标题)}；iTerm2 没运行就返回空。"""
    if not available():
        return {}
    code, out, _err = _run(_LIST_AS, timeout=8)
    if code != 0:
        return {}
    m = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 3:
            continue
        tty, sid, title = p[0].strip(), p[1].strip(), p[2].strip()
        if tty.startswith("/dev/") and sid:
            m[tty] = (IT_PREFIX + sid, title)
    return m


def owns(wid):
    return isinstance(wid, str) and wid.startswith(IT_PREFIX)


def _sid(wid):
    return wid[len(IT_PREFIX):]


def _with_session(wid, body):
    """在「找到这个 session 之后」执行 body（已用 sess 变量指到它）。

    按 id 逐层遍历而不是 `session id "x"`：不同 iTerm2 版本的字典对
    `session id …` 的直接引用支持不一致，遍历实测最稳。
    """
    return ('tell application "iTerm2"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      repeat with s in sessions of t\n'
            '        if (id of s) is "%s" then\n'
            '          set sess to s\n' % _esc(_sid(wid)) +
            body +
            '        end if\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return "notfound"\n'
            'end tell\n')


def send_text(wid, text):
    """写一段文字（iTerm2 的 write text 会自动回车）。"""
    script = _with_session(wid, '          tell sess to write text "%s"\n'
                                '          return "ok"\n' % _esc(text))
    code, out, err = _run(script, timeout=12)
    if code != 0:
        return False, ((err or out)[:200] or "iTerm2 写入失败")
    if out.strip() != "ok":
        return False, "找不到原来的 iTerm2 会话（窗口可能已关闭）"
    return True, "ok"


def screen(wid):
    script = _with_session(wid, '          return contents of sess\n')
    code, out, err = _run(script, timeout=8)
    if code != 0:
        return False, ("读取失败: %s" % ((err or out)[:200]))
    if out.strip() == "notfound":
        return False, "找不到原来的 iTerm2 会话（窗口可能已关闭）"
    return True, out


def close(wid):
    script = _with_session(wid, '          close sess\n          return "ok"\n')
    code, out, _err = _run(script, timeout=6)
    return code == 0 and out.strip() == "ok"


# 单键的原始字节：iTerm2 的 write text 本质是往 pty 写「用户敲的字节」，
# 方向键/回车/控制键就是这几串转义序列（和 server.py 的 _SPECIAL_BYTES 同义）。
KEY_BYTES = {
    "esc": b"\x1b", "tab": b"\t", "enter": b"\r",
    "up": b"\x1b[A", "down": b"\x1b[B", "left": b"\x1b[D", "right": b"\x1b[C",
    "shift_tab": b"\x1b[Z", "space": b" ", "backspace": b"\x7f",
    "ctrl_c": b"\x03", "ctrl_o": b"\x0f", "ctrl_l": b"\x0c", "ctrl_d": b"\x04",
    "ctrl_a": b"\x01", "ctrl_e": b"\x05", "ctrl_u": b"\x15", "ctrl_k": b"\x0b",
    "ctrl_w": b"\x17", "ctrl_r": b"\x12",
}


def _asc_expr(data):
    """把原始字节写成 AppleScript 表达式：控制字符用 character id（和
    plat.py 信任框确认同一个招），可见字符原样加引号。"""
    parts = []
    for b in data:
        ch = chr(b)
        if 32 <= b < 127 and ch not in '"\\':
            parts.append('"%s"' % ch)
        else:
            parts.append("(character id %d)" % b)
    return "(" + " & ".join(parts) + ")" if parts else '""'


def send_key(wid, data):
    """把一个按键（原始字节）写进 iTerm2 会话，不追加回车。

    以前这条路直接判「iTerm2 只能整段写文字」→ 手机上的方向键/回车全废，
    Claude 的确认框（信任目录、权限确认）在电脑那头等不到人，用户只能在手机上
    「接管」（等于 kill 掉重开）。write text 本来就是往 pty 写字节，转义序列一样生效。
    """
    script = _with_session(wid, '          tell sess to write text %s newline no\n'
                                '          return "ok"\n' % _asc_expr(data))
    code, out, err = _run(script, timeout=12)
    if code != 0:
        return False, ((err or out)[:200] or "iTerm2 按键失败")
    if out.strip() != "ok":
        return False, "找不到原来的 iTerm2 会话（窗口可能已关闭）"
    return True, "ok"


CAPS = frozenset({"screen", "input", "keys", "close"})
