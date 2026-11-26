# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.events import coalesce_key, from_notify, merge
from pet.protocol import Badge, PetEvent, PROTOCOL


def test_perm_maps_to_p0_choice_and_hides_cwd():
    ev = from_notify({"id": 7, "sid": "s1", "kind": "perm", "title": "改配置",
                      "about": "在改配置", "say": "要你确认", "cwd": "/secret"})
    assert ev.priority == 0
    assert ev.expect == "choice"
    assert ev.ref == "n7"
    assert ev.badge == Badge("改配置", "waiting")
    wire = ev.wire()
    assert wire["v"] == PROTOCOL
    assert "cwd" not in wire
    assert "/secret" not in str(wire)


def test_done_is_p2_and_title_falls_back_to_about():
    ev = from_notify({"id": 2, "sid": "s1", "kind": "done", "about": "跑完了"})
    assert ev.priority == 2
    assert ev.title == "跑完了"
    assert ev.badge.status == "done"


def test_unknown_kind_is_ignored():
    assert from_notify({"id": 1, "sid": "s1", "kind": "weird"}) is None


def test_long_title_is_truncated():
    ev = from_notify({"id": 3, "sid": "s1", "kind": "perm", "title": "x" * 100})
    assert len(ev.title) <= 24


def test_merge_sums_counts_and_keeps_earliest():
    a = PetEvent("n1", "done", 2, "A", "", ts=10)
    b = PetEvent("n2", "done", 2, "A", "", ts=11)
    m = merge(a, b)
    assert m.ref == "n1" and m.count == 2 and m.ts == 10
    assert coalesce_key(m) == ("", "done")


def test_wire_roundtrip():
    ev = PetEvent("n1", "perm", 0, "A", "B", line="说", emotion="anxious",
                  gesture="look_up", badge=Badge("A", "waiting"), sid="s1",
                  audio="/api/pet/audio/x", expect="choice", ts=1.5, count=2)
    assert PetEvent.from_wire(ev.wire()) == ev
