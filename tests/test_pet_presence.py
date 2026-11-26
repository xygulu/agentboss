# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.presence import (FakePresenceSampler, Presence, PresenceTracker,
                          _parse_display_power, _parse_hid_idle,
                          _parse_lock, _parse_lsappinfo_name)


def test_parse_mac_outputs():
    assert _parse_hid_idle('"HIDIdleTime" = 120000000000') == 120.0
    assert _parse_lock('"CGSSessionScreenIsLocked" = Yes') is True
    assert _parse_lock("") is False
    assert _parse_lsappinfo_name('"LSDisplayName"="Terminal"') == "Terminal"
    assert _parse_display_power("CurrentPowerState 4") is True
    assert _parse_display_power("CurrentPowerState 1") is False


def test_at_desk_under_threshold():
    st = PresenceTracker().update(Presence(idle_s=5), now=100)
    assert st.at_desk and not st.away and not st.uncertain


def test_uncertain_band_is_silent():
    st = PresenceTracker().update(Presence(idle_s=60), now=100)
    assert st.uncertain and not st.at_desk and not st.away


def test_away_after_90s_or_lock():
    tr = PresenceTracker()
    assert tr.update(Presence(idle_s=91), now=100).away
    tr2 = PresenceTracker()
    assert tr2.update(Presence(idle_s=0, locked=True), now=100).away


def test_return_and_no_flap():
    tr = PresenceTracker()
    tr.update(Presence(idle_s=120), now=100)
    assert tr.update(Presence(idle_s=20), now=105).away   # 20s 仍在迟滞带
    assert tr.update(Presence(idle_s=5), now=112).at_desk  # <10s 才算回来


def test_active_phone_keeps_her_at_desk():
    st = PresenceTracker().update(
        Presence(idle_s=70, phone_present=True, phone_moved_s=3), now=100)
    assert st.at_desk


def test_fake_sampler_is_settable():
    f = FakePresenceSampler(Presence(idle_s=5))
    f.set(Presence(idle_s=120))
    assert f.sample().idle_s == 120
