# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠兜底通道：host 把中继设备凭证随配对/配置发给板子。

板子连不上 host（电脑关机/不在同一网）时，用这份凭证调中继 /api/pet/turn。
约定字段：{"base": 中继地址, "dev": 设备 id, "sec": 设备密钥}。

跑法： .venv/bin/python -m pytest tests/test_pet_fallback.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pet.api import cfg_msg, pair_extra
from pet.personas import get_persona

_RELAY = {"url": "https://relay.example.com",
          "device_id": "d" * 16, "device_secret": "s" * 48}


def test_pair_extra_ships_relay_fallback():
    out = pair_extra(get_persona("xiaogongtou"), keycfg={}, relay=_RELAY)
    assert out["fallback"] == {"base": "https://relay.example.com",
                              "dev": "d" * 16, "sec": "s" * 48}


def test_pair_extra_omits_fallback_without_full_credentials():
    persona = get_persona("xiaogongtou")
    for rc in ({}, {"url": "https://r"}, {"url": "https://r", "device_id": "d"},
               {"device_id": "d", "device_secret": "s"}):
        assert "fallback" not in pair_extra(persona, keycfg={}, relay=rc), rc


def test_cfg_msg_carries_fallback_on_reconnect():
    m = cfg_msg(get_persona("xiaogongtou"), keycfg={}, relay=_RELAY)
    assert m["type"] == "cfg"
    assert m["fallback"]["dev"] == "d" * 16


def test_pair_extra_keeps_persona_block_alongside_fallback():
    out = pair_extra(get_persona("xiaogongtou"), keycfg={}, relay=_RELAY)
    assert out["persona"]["name"] == "小工头"


def test_persona_block_carries_id_for_relay_fallback():
    """板子把 persona 原样转给中继兜底，必须能认出是哪个人格——给 id。"""
    out = pair_extra(get_persona("laogongtou"), keycfg={}, relay=_RELAY)
    assert out["persona"]["id"] == "laogongtou"
