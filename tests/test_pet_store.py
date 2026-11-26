# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.store import Store


def test_missing_file_returns_defaults(tmp_path):
    s = Store(tmp_path / "pet.json")
    assert s.get("persona") == "xiaogongtou"
    assert s.get("queue") == []
    assert s.get("seen_seq") == 0


def test_corrupt_file_is_backed_up(tmp_path):
    p = tmp_path / "pet.json"
    p.write_text("{bad", encoding="utf-8")
    s = Store(p)
    assert s.get("persona") == "xiaogongtou"
    assert (tmp_path / "pet.json.bad").exists()


def test_save_is_atomic_and_roundtrips(tmp_path):
    p = tmp_path / "pet.json"
    s = Store(p)
    s.set("persona", "laogongtou")
    s.set("queue", [{"ref": "n1"}])
    s.save()
    assert Store(p).get("persona") == "laogongtou"
    assert Store(p).get("queue") == [{"ref": "n1"}]
    assert not (tmp_path / "pet.json.tmp").exists()


def test_unknown_keys_survive_load(tmp_path):
    p = tmp_path / "pet.json"
    p.write_text('{"v": 1, "future": {"x": 1}}', encoding="utf-8")
    assert Store(p).get("future") == {"x": 1}
