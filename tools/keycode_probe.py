# SPDX-License-Identifier: AGPL-3.0-or-later

"""判定 System Events 按键到底送到了哪个窗口：
scratch 窗口里跑 `cat > /tmp/cbkey.log`，用 host 同一套 KEYCODE_AS 发 ↑，
看日志文件有没有收到字节；同时读别的窗口的内容做对照。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

LOG = "/tmp/cbkey.log"


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
win_id = int(win_id)
tty = tty.strip()
time.sleep(1.5)

script = server.KEYCODE_AS.replace("__KC__", "126").replace("__TTY__", tty)
code, out, err = osa(script)
print("KEYCODE_AS:", code, repr(out), repr(err))
time.sleep(1.2)

n = Path(LOG).stat().st_size if Path(LOG).exists() else -1
print("cat 收到字节数:", n)
if n > 0:
    print("内容:", repr(Path(LOG).read_bytes()[:40]))

osa('tell application "Terminal" to close window id %d' % win_id)
