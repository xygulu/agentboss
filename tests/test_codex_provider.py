# SPDX-License-Identifier: AGPL-3.0-or-later

"""Codex 供应商（cc-switch codex 页）：读列表 / 切配置 / 不动别的内容。

全部在自己的临时 HOME + CODEX_HOME 里，不读也不写真实的 ~/.codex 和 ~/.cc-switch。
（原 tools/test_codex_provider.py：脚本式写法在 import 时清 HOME，全量 pytest 跑时
 server 已被别的用例导入、路径常量指向真实机器 → collection error。改成用例 + 夹具。）

跑法：
    .venv/bin/python -m pytest tests/test_codex_provider.py -q
"""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402

BLOCK_A = '''[model_providers.a]
name = "A"
base_url = "https://a.example/v1"
wire_api = "responses"
requires_openai_auth = true
'''

TEXT_A = '''model_provider = "a"
model = "model-a"
model_reasoning_effort = "high"

notify = ["/x/hook.sh", "codex"]

''' + BLOCK_A

TEXT_B = '''model_provider = "b"
model = "model-b"

[model_providers.b]
name = "B"
base_url = "https://b.example/v1"
wire_api = "responses"
requires_openai_auth = true
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假的 cc-switch 库（两家 codex + 一家 claude）+ 空的 ~/.codex。"""
    home = tmp_path / "home"
    codex = home / ".codex"
    cc = home / ".cc-switch"
    cc.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    db = cc / "cc-switch.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE providers(id TEXT, app_type TEXT, name TEXT,"
                " settings_config TEXT, sort_index INT, is_current INT)")
    for i, (pid, name, text, key) in enumerate([
            ("id-a", "A", TEXT_A, "sk-aaa"),
            ("id-b", "B", TEXT_B, "sk-bbb")]):
        con.execute("INSERT INTO providers VALUES(?,?,?,?,?,?)",
                    (pid, "codex", name,
                     json.dumps({"auth": {"OPENAI_API_KEY": key}, "config": text}),
                     i, 1 if i == 0 else 0))
    con.execute("INSERT INTO providers VALUES(?,?,?,?,?,?)",
                ("id-c", "claude", "C", "{}", 0, 0))
    con.commit()
    con.close()
    monkeypatch.setattr(server, "CC_DB", db)
    return {"home": home, "codex": codex, "cc": cc, "db": db}


def test_list_parses_without_secrets(env):
    """列表：只认 codex，解析出 base_url/model，默认响应不带密钥。"""
    provs = server._cc_codex_providers()
    assert [p["name"] for p in provs] == ["A", "B"], provs
    assert provs[0]["base_url"] == "https://a.example/v1" and provs[0]["model"] == "model-a"
    assert "auth" not in provs[0] and "config" not in provs[0]
    secrets = server._cc_codex_providers(with_secret=True)
    assert secrets[1]["auth"]["OPENAI_API_KEY"] == "sk-bbb"


def test_machine_model_empty_without_config(env):
    """没有 config.toml 时如实返回空，不猜。"""
    assert server._codex_machine_model() == {"base_url": "", "model": ""}


def test_switch_keeps_unrelated_config(env):
    """切换：写 config.toml + auth.json，notify/projects 一个字节不动。"""
    codex = env["codex"]
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(
        'model_provider = "a"\nmodel = "model-a"\n\nnotify = ["/x/hook.sh", "codex"]\n\n'
        '[projects."/tmp/p"]\ntrust_level = "trusted"\n\n' + TEXT_A, encoding="utf-8")
    secrets = server._cc_codex_providers(with_secret=True)
    ok, why = server._codex_apply_provider(secrets[1])
    assert ok, why
    text = (codex / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "b"' in text and 'model = "model-b"' in text
    assert 'notify = ["/x/hook.sh", "codex"]' in text
    assert 'trust_level = "trusted"' in text
    assert text.count("[model_providers.b]") == 1
    assert server._codex_machine_model() == {"base_url": "https://b.example/v1",
                                             "model": "model-b"}
    auth = json.loads((codex / "auth.json").read_text(encoding="utf-8"))
    assert auth == {"OPENAI_API_KEY": "sk-bbb"}
    assert (os.stat(codex / "auth.json").st_mode & 0o777) == 0o600


def test_switch_same_provider_is_noop(env):
    """重复切同一家 = noop，不重复写块。"""
    codex = env["codex"]
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(TEXT_B, encoding="utf-8")
    (codex / "auth.json").write_text('{"OPENAI_API_KEY": "sk-bbb"}', encoding="utf-8")
    before = (codex / "config.toml").read_text(encoding="utf-8")
    resp = asyncio.run(server._codex_model_switch({"provider_id": "id-b"}))
    assert json.loads(resp.body)["via"] == "noop", resp.body
    assert (codex / "config.toml").read_text(encoding="utf-8") == before


def test_switch_back_keeps_both_blocks(env, monkeypatch):
    """切回 A：B 的块留着（只是不再被使用），auth 跟着换。
    起点是「正在跑 B、文件里已有 A 的块」——跟 test_switch_keeps_unrelated_config 同一条线。"""
    codex = env["codex"]
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(TEXT_B + "\n" + BLOCK_A, encoding="utf-8")
    (codex / "auth.json").write_text('{"OPENAI_API_KEY": "sk-bbb"}', encoding="utf-8")
    monkeypatch.setattr(server, "_codex_probe", lambda prov: None)  # 假 URL 过不了真探测
    resp = asyncio.run(server._codex_model_switch({"provider_id": "id-a"}))
    assert json.loads(resp.body)["ok"] is True, resp.body
    text = (codex / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "a"' in text and text.count("[model_providers.a]") == 1
    assert "[model_providers.b]" in text
    assert json.loads((codex / "auth.json").read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-aaa"
    # cc-switch 的 is_current 跟着变（手机端下次看「机器默认」不会指错）
    con = sqlite3.connect(env["db"])
    cur = con.execute("SELECT id FROM providers WHERE app_type='codex' AND is_current=1").fetchall()
    con.close()
    assert cur == [("id-a",)], cur
