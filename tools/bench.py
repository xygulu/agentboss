# SPDX-License-Identifier: AGPL-3.0-or-later

"""host 接口耗时测量。token 只从 token.txt 读，走 Cookie 头，不进命令行。"""
import http.client
import sys
import time
from pathlib import Path

TOK = Path(__file__).resolve().parent.parent / "token.txt"
token = TOK.read_text().split("\n")[0].strip()


def timed(path, method="GET", body=None, port=8790, host="127.0.0.1"):
    c = http.client.HTTPConnection(host, port, timeout=30)
    hdr = {"Cookie": "codeboss_t=" + token}
    if body is not None:
        hdr["Content-Type"] = "application/json"
    t0 = time.time()
    c.request(method, path, body=body, headers=hdr)
    r = c.getresponse()
    data = r.read()
    dt = time.time() - t0
    c.close()
    return dt, r.status, data


if __name__ == "__main__":
    for p in sys.argv[1:] or ["/api/ext_sessions", "/api/ext_sessions", "/api/ext_sessions"]:
        dt, st, data = timed(p)
        print("%-22s %6.3fs  %s  %d bytes" % (p, dt, st, len(data)))
