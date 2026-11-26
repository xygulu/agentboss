# SPDX-License-Identifier: AGPL-3.0-or-later

"""控制通道自检：起一个 wrapper（子命令 cat），经 unix socket 写 ESC[A，
看 cat 是否收到字节——这是方向键的新通路（原始字节进 pty，无回车）。
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402

SID = "ctlselftest"
meta_path = HERE / "host" / "logs" / "ansi" / (SID + ".meta.json")
log_path = HERE / "host" / "logs" / "ansi" / (SID + ".log")
for p in (meta_path, log_path):
    p.unlink(missing_ok=True)

proc = subprocess.Popen([sys.executable, str(HERE / "host" / "wrapper.py"),
                         "--meta", json.dumps({"sid": SID}), "--", "cat"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
time.sleep(1.5)
meta = json.loads(meta_path.read_text())
print("meta ctl:", meta.get("ctl"))
assert meta.get("ctl"), "meta 里没有 ctl 路径"

ok = server._pty_ctl_write(meta, b"\x1b[A")
print("_pty_ctl_write ->", ok)
time.sleep(1.0)

n = log_path.stat().st_size if log_path.exists() else -1
data = log_path.read_bytes() if n > 0 else b""
print("cat 回显字节:", n, repr(data[:40]))
# pty 有回显：ESC 显示成 ^[
print("PASS" if b"^[[A" in data or b"\x1b[A" in data else "FAIL：字节没进 pty")

proc.terminate()
proc.wait(timeout=5)
meta_path.unlink(missing_ok=True)
log_path.unlink(missing_ok=True)
