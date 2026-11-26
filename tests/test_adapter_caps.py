# SPDX-License-Identifier: AGPL-3.0-or-later

"""适配器能力位 can_resume/can_notify/can_model_switch + _link_caps 统一出口。

跑法： .venv/bin/python -m pytest tests/test_adapter_caps.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agents
import server


def test_base_adapter_defaults_all_false():
    a = agents.AgentAdapter()
    assert a.can_resume is False
    assert a.can_notify is False
    assert a.can_model_switch is False


def test_claude_adapter_full_caps():
    ad = agents.get("claude")
    assert ad is not None
    assert ad.can_resume and ad.can_notify and ad.can_model_switch


def test_codex_adapter_full_caps():
    ad = agents.get("codex")
    assert ad is not None
    assert ad.can_resume and ad.can_notify and ad.can_model_switch


def test_link_caps_adds_resume_for_capable_adapter():
    class _Fake(agents.AgentAdapter):
        key = "fake"
        can_resume = True

    link, caps, why = server._link_caps(
        {"sid": "s", "tty": "", "window_id": None, "agent": "fake"},
        _Fake(), host=False, meta=None)
    assert "resume" in caps


def test_link_caps_no_resume_for_base_adapter():
    link, caps, why = server._link_caps(
        {"sid": "s", "tty": "", "window_id": None, "agent": "x"},
        agents.AgentAdapter(), host=False, meta=None)
    assert "resume" not in caps


def test_agents_installed_rows_carry_caps():
    saved = dict(agents._ADAPTERS)

    class _Fake(agents.AgentAdapter):
        key = "fakecap"
        label = "Fake"
        can_resume = True
        can_notify = True

        def installed(self):
            return True

    try:
        agents.register(_Fake())
        rows = {r["key"]: r for r in server._agents_installed(None)}
        assert rows["fakecap"]["caps"] == ["resume", "notify"]
    finally:
        agents._ADAPTERS.clear()
        agents._ADAPTERS.update(saved)
