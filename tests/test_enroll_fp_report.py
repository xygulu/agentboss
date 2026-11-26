# SPDX-License-Identifier: AGPL-3.0-or-later

"""登记开发机时把硬件指纹一起报给中继（匿名免费版按指纹认同一台机器）。

跑法： .venv/bin/python -m pytest tests/test_enroll_fp_report.py -q
"""
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import plat
import server


class _Resp:
    def read(self):
        return json.dumps({"ok": True, "device_id": "d1", "device_secret": "s1",
                           "pair_code": "c1", "pair_url": "/r/?p=c1"}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_enroll_body_carries_machine_fp(monkeypatch, tmp_path):
    monkeypatch.setattr(plat, "machine_fp", lambda: "f" * 64)
    monkeypatch.setattr(server, "_relay_cfg", lambda: {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "HERE", tmp_path)
    monkeypatch.setattr(server, "_pair_full", lambda base, u: base + u)
    seen = {}

    def fake_urlopen(req, timeout=20):
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rc = server._relay_enroll("https://relay.example", as_json=True)
    assert rc == 0
    assert seen["body"]["fp"] == "f" * 64
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["relay"]["device_id"] == "d1"
