# SPDX-License-Identifier: AGPL-3.0-or-later

"""原生模型切换（不碰真实 ~/.claude/settings.json）。

用临时 HOME 起一份假 settings，走 _native_provider / _native_apply：
  1) 无 ANTHROPIC_MODEL：写 model 字段（= /model 的效果）
  2) 有 ANTHROPIC_MODEL：同步覆盖，否则它压住 settings.model
  3) 空档：清回默认
  4) 没 cc-switch：provider 列表为空，客户端回落到原生 provider

（原 tools/test_native_model.py：脚本式写法在 import 时换 HOME，全量 pytest 跑时
 server 已被别的用例导入、_cc_providers 读到真实机器上的 cc-switch → collection error。）

跑法：
    .venv/bin/python -m pytest tests/test_native_model.py -q
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


@pytest.fixture
def sp(tmp_path, monkeypatch):
    """临时 HOME + 不存在的 cc-switch 库；返回 settings.json 路径。"""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(server, "CC_DB", tmp_path / "none" / "cc-switch.db")
    return home / ".claude" / "settings.json"


def write(p, cfg):
    p.write_text(json.dumps(cfg), encoding="utf-8")


def read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_official_env_writes_model(sp):
    """官方登录环境：只有 model，写 model 字段就等于 claude 自己的 /model。"""
    write(sp, {"model": "sonnet", "env": {}})
    p = server._native_provider()
    assert p["id"] == server.NATIVE_ID and p["model"] == "sonnet", p
    server._native_apply("opus")
    c = read(sp)
    assert c["model"] == "opus" and "ANTHROPIC_MODEL" not in c["env"], c


def test_env_override_syncs(sp):
    """第三方环境：env 有 ANTHROPIC_MODEL 时切换必须同步，否则它压住 settings.model。"""
    write(sp, {"model": "opus", "env": {"ANTHROPIC_MODEL": "deepseek-chat",
                                        "ANTHROPIC_BASE_URL": "https://x/anthropic"}})
    assert server._native_provider()["model"] == "deepseek-chat"   # env 优先
    server._native_apply("haiku")
    c = read(sp)
    assert c["model"] == "haiku" and c["env"]["ANTHROPIC_MODEL"] == "haiku", c
    assert c["env"]["ANTHROPIC_BASE_URL"] == "https://x/anthropic", c   # base_url 不受影响


def test_empty_tier_resets(sp):
    """空档 = 默认：两个都清掉。"""
    write(sp, {"model": "opus", "env": {"ANTHROPIC_MODEL": "opus"}})
    server._native_apply("")
    c = read(sp)
    assert "model" not in c and "ANTHROPIC_MODEL" not in c["env"], c


def test_no_cc_switch_returns_empty(sp):
    """没 cc-switch 时 _cc_providers 为空 → h_ext_model 回落原生 provider。"""
    assert server._cc_providers() == []
