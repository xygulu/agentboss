# SPDX-License-Identifier: AGPL-3.0-or-later

"""槽体检：meta 说的 size/sha 和盘上文件对不对得上。

2026-11-18 那次串包，中继自己的 meta 明明写着 codeboss，盘上却是老板的包——
「版本号说得对」不等于「发出去的包对」。这个接口就是让后台一眼看出这件事。

跑法： .venv/bin/python -m pytest tests/test_admin_slots.py -q
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


def _put(tmp_path, product, blob, meta):
    binp = tmp_path / (product + ".bin")
    metap = tmp_path / (product + ".json")
    binp.write_bytes(blob)
    metap.write_text(json.dumps(meta))
    return str(binp), str(metap)


def test_file_ok_false_when_sha_mismatch(tmp_path, monkeypatch):
    blob = b"REAL" * 3000
    bad = {"code": 192, "ver": "0.11.27", "size": len(blob),
           "sha256": "0" * 64, "pkg": "com.codeboss.terminal"}
    slots = {"codeboss": _put(tmp_path, "codeboss", blob, bad),
             "agentboss": _put(tmp_path, "agentboss", b"", {})}
    monkeypatch.setattr(relay, "_apk_slot", lambda p: slots[relay.branding.valid_product(p)])
    rows = {r["product"]: r for r in relay._slot_health()}
    assert rows["codeboss"]["file_ok"] is False
    assert rows["codeboss"]["on_disk"] == len(blob)


def test_file_ok_true_when_sha_matches(tmp_path, monkeypatch):
    blob = b"REAL" * 3000
    good = {"code": 192, "ver": "0.11.27", "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}
    slots = {"codeboss": _put(tmp_path, "codeboss", blob, good),
             "agentboss": _put(tmp_path, "agentboss", b"", {})}
    monkeypatch.setattr(relay, "_apk_slot", lambda p: slots[relay.branding.valid_product(p)])
    rows = {r["product"]: r for r in relay._slot_health()}
    assert rows["codeboss"]["file_ok"] is True


def test_file_ok_none_for_old_meta_without_sha(tmp_path, monkeypatch):
    """老 meta 没 sha：只比 size，比不了就 None，不谎报 True。"""
    blob = b"REAL" * 3000
    old = {"code": 191, "ver": "0.11.26", "size": len(blob)}
    slots = {"codeboss": _put(tmp_path, "codeboss", blob, old),
             "agentboss": _put(tmp_path, "agentboss", b"", {})}
    monkeypatch.setattr(relay, "_apk_slot", lambda p: slots[relay.branding.valid_product(p)])
    rows = {r["product"]: r for r in relay._slot_health()}
    assert rows["codeboss"]["file_ok"] is True          # size 对得上
    assert rows["agentboss"]["file_ok"] is None          # 空槽：没包可验，不谎报


def test_empty_slot_reported(tmp_path, monkeypatch):
    slots = {"codeboss": (str(tmp_path / "nope.bin"), str(tmp_path / "nope.json")),
             "agentboss": (str(tmp_path / "nope2.bin"), str(tmp_path / "nope2.json"))}
    monkeypatch.setattr(relay, "_apk_slot", lambda p: slots[relay.branding.valid_product(p)])
    rows = {r["product"]: r for r in relay._slot_health()}
    assert rows["codeboss"]["on_disk"] == 0 and rows["codeboss"]["file_ok"] is None
