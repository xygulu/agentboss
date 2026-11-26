#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""入口模块必须认领 "server" 这个名字。

生产是 `python server.py`，模块名 `__main__`。notify / hooks / transcript 里写的是
`import server`——不认领就会**再加载一份 server.py**，两份模块级状态各走各的。
2026-11-14 的实际症状：另一份的 `_listen_n()` 永远是 0 → `_smart_ok()` 判「没有
客户端在收」→ 语音不合成、概括清空，手机收到提醒却没声音。
"""
import os
import runpy
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PATHS = [ROOT, os.path.join(ROOT, "host"), os.path.join(ROOT, "shared")]
SERVER = os.path.join(ROOT, "host", "server.py")

PROBE = """
import runpy, sys
sys.path[:0] = %r
sys.argv = ["server.py", "--hooks", "status"]
try:
    runpy.run_path(%r, run_name="__main__")
except SystemExit:
    pass
import server
print("ALIAS=" + server.__name__)
""" % (PATHS, SERVER)

# 无源码包：入口薄壳 `from server_impl import main` → 模块名是 server_impl，不是 __main__。
PROBE_PKG = """
import importlib.util, sys
sys.path[:0] = %r
sys.argv = ["server.py", "--hooks", "status"]
spec = importlib.util.spec_from_file_location("server_impl", %r)
m = importlib.util.module_from_spec(spec)
sys.modules["server_impl"] = m
spec.loader.exec_module(m)
print("CLAIMED=" + str(sys.modules.get("server") is m))
""" % (PATHS, SERVER)


class ModuleAliasTest(unittest.TestCase):
    def test_import_server_is_the_running_main(self):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT,
                             capture_output=True, text=True, timeout=180)
        line = [x for x in out.stdout.splitlines() if x.startswith("ALIAS=")]
        self.assertTrue(line, "探针没跑出 ALIAS（stderr: %s）" % out.stderr[-800:])
        # 认领成功时拿到的是入口那份（__name__ 还是 __main__）；
        # 没认领就是新加载的一份，__name__ == "server"。
        self.assertEqual(line[-1][len("ALIAS="):], "__main__")

    def test_packaged_entry_claims_server(self):
        """无源码包（server_impl）也得认领，否则 `import server` 会加载入口薄壳，
        薄壳顶层 `main()` 在进程内再起一份服务——自己杀自己。"""
        out = subprocess.run([sys.executable, "-c", PROBE_PKG], cwd=ROOT,
                             capture_output=True, text=True, timeout=180)
        line = [x for x in out.stdout.splitlines() if x.startswith("CLAIMED=")]
        self.assertTrue(line, "探针没跑出 CLAIMED（stderr: %s）" % out.stderr[-800:])
        self.assertEqual(line[-1], "CLAIMED=True")


if __name__ == "__main__":
    unittest.main()
