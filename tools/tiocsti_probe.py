# SPDX-License-Identifier: AGPL-3.0-or-later

"""试 TIOCSTI：把原始字节直接推进 tty 输入队列（不附带回车、不抢焦点）。
scratch 窗口跑 `cat > /tmp/cbkey.log`，推 ESC[A，看 cat 收到没有。
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

LOG = "/tmp/cbkey.log"
TIOCSTI = getattr(__import__("termios"), "TIOCSTI", 0x80047472)


def osa(script, timeout=20):
    p = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


Path(LOG).unlink(missing_ok=True)
code, out, err = osa('tell application "Terminal"\n'
                      'set t to do script "cat > %s"\n'
                      'return ((id of front window) as text) & "," & (tty of t)\n'
                      'end tell' % LOG)
print("scratch:", code, out or err)
win_id, tty = out.split(",", 1)
tty = tty.strip()
time.sleep(1.5)

try:
    fd = os.open(tty, os.O_RDWR | os.O_NONBLOCK)
    for b in b"\x1b[A":
        fcntl.ioctl(fd, TIOCSTI, bytes([b]))
    os.close(fd)
    print("TIOCSTI: 写入成功")
except Exception as e:
    print("TIOCSTI 失败:", type(e).__name__, e)

time.sleep(1.0)
n = Path(LOG).stat().st_size if Path(LOG).exists() else -1
print("cat 收到字节数:", n, repr(Path(LOG).read_bytes()[:20]) if n > 0 else "")
print("PASS" if n >= 3 else "FAIL：TIOCSTI 用不了")

osa('tell application "Terminal" to close window id %s' % win_id)
