# SPDX-License-Identifier: AGPL-3.0-or-later

"""APK 槽自证：包名/sha 对不上就拒收，槽里旧包不动。

2026-11-18 生产事故：codeboss 槽被推成 agentboss 的包（932,823 B），
`/codeboss.apk` 全体工头用户下到的是老板 App，灰度基线还把 code=5 钉成默认，
一错就是一整天。根因是「帧里说自己是 codeboss」全凭 host 自觉。

跑法： .venv/bin/python -m pytest tests/test_apk_slot_guard.py -q
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relay


class _Db:
    def execute(self, *a, **k):
        return None


@pytest.fixture
def slots(tmp_path, monkeypatch):
    """两个槽落在 tmp，归档/基线/OSS 全部哑掉（只看落盘结果）。"""
    binp = tmp_path / "codeboss-apk.bin"
    metap = tmp_path / "codeboss-apk.json"
    binp.write_bytes(b"OLD" * 4000)          # 槽里已有的好包
    metap.write_text(json.dumps({"code": 191, "ver": "0.11.26"}))
    monkeypatch.setattr(relay, "APK_FILE", str(binp))
    monkeypatch.setattr(relay, "APK_META", str(metap))
    monkeypatch.setattr(relay, "_db", lambda app: _Db())
    monkeypatch.setattr(relay, "_apk_archive", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_apk_baseline", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_oss_mirror", lambda *a, **k: None)
    return binp, metap


def test_pkg_mismatch_rejected_and_slot_kept(slots):
    binp, metap = slots
    blob = b"NEW" * 8000
    with pytest.raises(ValueError):
        relay._apk_store(None, "devX", blob,
                         {"product": "codeboss", "pkg": "com.agentboss.terminal",
                          "code": 5, "ver": "0.1.3"})
    assert binp.read_bytes() == b"OLD" * 4000
    assert json.loads(metap.read_text())["code"] == 191


def test_sha_mismatch_rejected(slots):
    binp, _ = slots
    blob = b"NEW" * 8000
    with pytest.raises(ValueError):
        relay._apk_store(None, "devX", blob,
                         {"product": "codeboss", "pkg": "com.codeboss.terminal",
                          "sha256": "0" * 64, "code": 192, "ver": "0.11.27"})
    assert binp.read_bytes() == b"OLD" * 4000


def test_good_frame_stored(slots):
    binp, metap = slots
    blob = b"NEW" * 8000
    relay._apk_store(None, "devX", blob,
                     {"product": "codeboss", "pkg": "com.codeboss.terminal",
                      "sha256": hashlib.sha256(blob).hexdigest(),
                      "code": 192, "ver": "0.11.27"})
    assert binp.read_bytes() == blob
    m = json.loads(metap.read_text())
    assert m["code"] == 192 and m["product"] == "codeboss" and m["size"] == len(blob)


def test_end_frame_carries_pkg_and_sha():
    """尾帧合成元数据：ver/code/product 以尾帧为准，pkg/sha256 不能被丢掉——
    丢了上面那层校验就是死代码。"""
    m = relay._apk_end_meta({"code": 192, "ver": "0.11.27", "product": "codeboss",
                             "pkg": "com.codeboss.terminal", "sha256": "ab" * 32},
                            {"meta": {"ver": "旧", "code": 1}})
    assert m == {"ver": "0.11.27", "code": 192, "product": "codeboss",
                 "pkg": "com.codeboss.terminal", "sha256": "ab" * 32}


def test_old_host_without_pkg_still_stored(slots):
    """老 host 不带 pkg/sha：照旧收（只加校验，不做硬门槛）。"""
    binp, _ = slots
    blob = b"NEW" * 8000
    relay._apk_store(None, "devX", blob, {"product": "codeboss", "code": 192, "ver": "0.11.27"})
    assert binp.read_bytes() == blob


# --- host 侧：从 APK 里认出真包名 -------------------------------------------

def _fake_apk(tmp_path, manifest: bytes):
    import zipfile
    p = tmp_path / "fake.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
    return p


def test_apk_pkg_reads_package(tmp_path):
    import server
    p = _fake_apk(tmp_path, b"\x03\x00\x08\x00com.agentboss.terminal\x00")
    assert server._apk_pkg(p) == "com.agentboss.terminal"


def test_apk_pkg_none_when_unknown(tmp_path):
    import server
    p = _fake_apk(tmp_path, b"\x03\x00\x08\x00com.other.app\x00")
    assert server._apk_pkg(p) is None
