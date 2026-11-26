# SPDX-License-Identifier: AGPL-3.0-or-later

"""方向键机制自检：新开一个 scratch Terminal 窗口（不动用户现有窗口），
跑一条命令 → 清屏 → 用 host 同一套 KEYCODE_AS 脚本发 ↑ → 看历史有没有被调出来。
只验证「按键能不能落到终端」，不碰任何真实会话。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

MARK = "CBKEYTEST_%d" % int(time.time())

AS_NEW = ('tell application "Terminal"\n'
          'set t to do script "sh"\n'
          'return ((id of front window) as text) & "," & (tty of t)\n'
          'end tell')


def osa(script, timeout=20):
    p = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def read_tab(win_id, tty):
    script = ('tell application "Terminal" to get contents of tab 1 of window id %d'
              % int(win_id))
    c, out, err = osa(script)
    return out if c == 0 else ""


code, out, err = osa(AS_NEW)
print("开窗口:", code, out or err)
if code != 0:
    sys.exit(1)
win_id, tty = out.split(",", 1)
win_id = int(win_id)
tty = tty.strip()
print("win=%s tty=%s" % (win_id, tty))

time.sleep(1.2)
osa('tell application "Terminal" to do script "echo %s" in tab 1 of window id %d'
    % (MARK, win_id))
time.sleep(1.0)
osa('tell application "Terminal" to do script "clear" in tab 1 of window id %d' % win_id)
time.sleep(1.0)
before = read_tab(win_id, tty)
print("清屏后含标记:", MARK in before)

# host 里按键走的正是这条路径。历史里最后一条是 clear，↑ 两次才轮到 echo MARK
s = {"tty": tty}
t0 = time.time()
ok, msg = server.ext_send_special(s, "up")
print("ext_send_special(up) ->", ok, repr(msg), "耗时 %.2fs" % (time.time() - t0))
time.sleep(0.6)
ok2, msg2 = server.ext_send_special(s, "up")
print("ext_send_special(up) #2 ->", ok2, repr(msg2))

time.sleep(1.0)
after = read_tab(win_id, tty)
print("按↑后含标记:", MARK in after)
if MARK in after:
    print("PASS：方向键机制正常")
else:
    print("FAIL：↑ 没落到终端")
    print("--- 屏幕 ---")
    print(after[-800:])

osa('tell application "Terminal" to close window id %d' % win_id)
