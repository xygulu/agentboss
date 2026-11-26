# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

from pet import brain


class FakePet:
    """够用的 Director 替身：笔记/要求/说法方式 + pet.json 那两个键。"""

    def __init__(self, memo="", prefs=None, talk="", brain=None):
        self.data = {"mem": {"memo": memo}, "prefs": list(prefs or []),
                     "talk": talk}
        if brain:
            self.data["brain"] = brain
        self.store = self

    def get(self, k, d=None):
        return self.data.get(k, d)

    def set(self, k, v):
        self.data[k] = v

    def memo(self):
        return str((self.data.get("mem") or {}).get("memo") or "")

    def save_memo(self, t):
        self.data.setdefault("mem", {})["memo"] = t

    def prefs(self):
        return [str(x) for x in (self.data.get("prefs") or [])][-8:]

    def add_pref(self, t):
        self.data["prefs"] = [p for p in self.prefs() if p != t] + [t]

    def talk(self):
        return self.data.get("talk") or ""

    def set_talk(self, t):
        self.data["talk"] = t

    def save(self):
        pass


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(brain, "_LAST", 0.0)
    monkeypatch.setattr(brain, "_LOCK", None)
    monkeypatch.setattr(brain, "_engine", lambda: ("codex", "/usr/bin/codex"))
    monkeypatch.setenv("CODEBOSS_PET_BRAIN", str(tmp_path))
    import owned_sessions
    monkeypatch.setattr(owned_sessions, "PATH", tmp_path / "owned.json")
    return owned_sessions


def events(msg, tid="tid-1"):
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": tid}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": msg}}),
    ])


def test_argv_resumes_the_saved_session():
    """有会话 id 就 resume 同一条：她越聊越懂老板靠的是这条线不断。"""
    a = brain.argv("/x/codex", "abc-123", Path("/t/s.json"), Path("/t/o.json"),
                   "hi", {"provider": "minimax", "model": "MiniMax-M3"})
    assert a[:3] == ["/x/codex", "exec", "resume"]
    assert "abc-123" in a
    assert "-m" in a and "MiniMax-M3" in a
    assert "--output-schema" in a and "-o" in a
    fresh = brain.argv("/x/codex", "", Path("/t/s.json"), Path("/t/o.json"),
                       "hi", {"provider": "minimax", "model": "m"})
    assert fresh[:2] == ["/x/codex", "exec"]
    assert "resume" not in fresh


def test_learn_applies_memo_prefs_and_registers_session(monkeypatch, tmp_path):
    own = setup(monkeypatch, tmp_path)
    pet = FakePet(memo="老板爱喝冰美式")
    msg = json.dumps({"memo": "老板叫星宇；爱喝冰美式", "prefs": ["叫我哥"],
                      "talk": "brief"}, ensure_ascii=False)

    async def fake_run(argv_, cwd, env_, timeout):
        assert "老板爱喝冰美式" in argv_[-1]        # 旧笔记要交给会话合并
        return events(msg), 0

    monkeypatch.setattr(brain, "_run", fake_run)
    assert asyncio.run(brain.learn(pet, [{"role": "user", "text": "叫我哥"}],
                                   "今天好累", "嗯，歇会")) is True
    assert "星宇" in pet.memo() and "冰美式" in pet.memo()
    assert pet.prefs() == ["叫我哥"]
    assert pet.talk() == "brief"
    assert pet.get("brain")["sid"] == "tid-1"
    assert own.has("tid-1")                     # 手机上删不掉
    assert own.why("tid-1") == "桌宠的慢脑子"


def test_second_round_resumes_the_stored_session(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    import agent_codex
    monkeypatch.setattr(agent_codex, "rollout_path", lambda s: Path("/x/rollout"))
    pet = FakePet(brain={"sid": "tid-9"})
    seen = {}

    async def fake_run(argv_, cwd, env_, timeout):
        seen["argv"] = argv_
        return events('{"memo":"m","prefs":[]}'), 0

    monkeypatch.setattr(brain, "_run", fake_run)
    asyncio.run(brain.learn(pet, [], "x", "y"))
    assert seen["argv"][1:4] == ["exec", "resume", "tid-9"]


def test_learn_falls_back_when_agent_unavailable(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)

    async def fake_run(argv_, cwd, env_, timeout):
        return "", None

    monkeypatch.setattr(brain, "_run", fake_run)
    pet = FakePet()
    assert asyncio.run(brain.learn(pet, [], "x", "y")) is False
    assert pet.memo() == ""


def test_learn_skips_when_talking_too_fast(monkeypatch, tmp_path):
    """一分钟内聊三句：不跑三次 agent（慢又费），留给中继快路径合并。"""
    setup(monkeypatch, tmp_path)
    pet = FakePet()
    calls = []

    async def fake_run(argv_, cwd, env_, timeout):
        calls.append(1)
        return events('{"memo":"m","prefs":[]}'), 0

    monkeypatch.setattr(brain, "_run", fake_run)
    asyncio.run(brain.learn(pet, [], "a", "b"))
    assert asyncio.run(brain.learn(pet, [], "c", "d")) is False
    assert len(calls) == 1


def test_deleted_on_computer_starts_a_fresh_session(monkeypatch, tmp_path):
    """老板在电脑上把会话删了：没找到记录就重开一条，并把新的登记上。"""
    own = setup(monkeypatch, tmp_path)
    import agent_codex
    monkeypatch.setattr(agent_codex, "rollout_path", lambda s: None)
    pet = FakePet(brain={"sid": "gone"})
    seen = {}

    async def fake_run(argv_, cwd, env_, timeout):
        seen["argv"] = argv_
        return events('{"memo":"m","prefs":[]}', tid="tid-new"), 0

    monkeypatch.setattr(brain, "_run", fake_run)
    asyncio.run(brain.learn(pet, [], "x", "y"))
    assert "resume" not in seen["argv"]
    assert pet.get("brain")["sid"] == "tid-new"
    assert own.has("tid-new")


def test_apply_does_not_duplicate_or_downgrade(monkeypatch, tmp_path):
    owned = setup(monkeypatch, tmp_path)
    pet = FakePet(memo="m", prefs=["别啰嗦"], talk="brief")
    assert brain.apply(pet, {"memo": "m", "prefs": ["别啰嗦"], "talk": ""}) is False
    assert owned.ids() == set()                  # 没给 sid 就不登记
    assert pet.talk() == "brief"                 # 空 talk 不把她改回 normal


def test_provider_reads_env_key_from_codex_config(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text(
        'model_provider = "minimax"\nmodel = "MiniMax-M3"\n\n'
        '[model_providers.minimax]\nbase_url = "https://api.minimaxi.com/v1"\n'
        'env_key = "MINIMAX_API_KEY"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    p = brain._provider()
    assert p == {"provider": "minimax", "model": "MiniMax-M3",
                 "env_key": "MINIMAX_API_KEY",
                 "base_url": "https://api.minimaxi.com/v1"}


def test_apply_accepts_notes_alias(monkeypatch, tmp_path):
    """线上实测模型把 memo 写成 notes；不能因此当她没记住。"""
    setup(monkeypatch, tmp_path)
    pet = FakePet()
    assert brain.apply(pet, {"notes": "老板叫星宇", "prefs": []}) is True
    assert pet.memo() == "老板叫星宇"


def test_engine_prefers_codex_then_claude(monkeypatch):
    monkeypatch.setattr(brain, "_codex", lambda: "/x/codex")
    monkeypatch.setattr(brain, "_claude", lambda: "/x/claude")
    assert brain._engine() == ("codex", "/x/codex")
    monkeypatch.setattr(brain, "_codex", lambda: "")
    assert brain._engine() == ("claude", "/x/claude")
    monkeypatch.setattr(brain, "_claude", lambda: "")
    assert brain._engine() is None


def test_claude_engine_resumes_and_reads_structured_output(monkeypatch, tmp_path):
    """没装 codex 的机器用 claude：-r 续会话，structured_output 就是结果。"""
    monkeypatch.setattr(brain, "_LAST", 0.0)
    monkeypatch.setattr(brain, "_LOCK", None)
    monkeypatch.setattr(brain, "_engine", lambda: ("claude", "/x/claude"))
    monkeypatch.setenv("CODEBOSS_PET_BRAIN", str(tmp_path))
    import owned_sessions
    monkeypatch.setattr(owned_sessions, "PATH", tmp_path / "owned.json")
    owned = owned_sessions
    pet = FakePet(brain={"sid": "cl-1", "eng": "claude"})
    seen = {}

    async def fake_run(argv_, cwd, env_, timeout):
        seen["argv"] = argv_
        return json.dumps({"session_id": "cl-1", "structured_output": {
            "memo": "老板叫星宇", "prefs": ["叫我哥"], "talk": "brief"}}), 0

    monkeypatch.setattr(brain, "_run", fake_run)
    assert asyncio.run(brain.learn(pet, [], "叫我哥", "好，哥")) is True
    assert seen["argv"][:2] == ["/x/claude", "-p"]
    assert "-r" in seen["argv"] and "cl-1" in seen["argv"]
    assert pet.memo() == "老板叫星宇" and pet.prefs() == ["叫我哥"]
    assert owned.has("cl-1")

    # 换了家：旧会话 id 不能拿去 resume
    monkeypatch.setattr(brain, "_engine", lambda: ("codex", "/x/codex"))
    monkeypatch.setattr(brain, "_LAST", 0.0)
    monkeypatch.setattr(brain, "_LOCK", None)

    async def fake_run2(argv_, cwd, env_, timeout):
        seen["argv2"] = argv_
        return events('{"memo":"m","prefs":[]}', tid="tid-2"), 0

    monkeypatch.setattr(brain, "_run", fake_run2)
    asyncio.run(brain.learn(pet, [], "x", "y"))
    assert "resume" not in seen["argv2"]
    assert pet.get("brain")["sid"] == "tid-2"
    assert pet.get("brain")["eng"] == "codex"


def test_prompt_carries_memo_and_only_chat():
    t = brain.prompt("老板爱喝冰美式", ["别啰嗦"],
                     [{"role": "user", "text": "昨天在写代码"},
                      {"role": "assistant", "text": "绿豆汤快好了吧"}],
                     "今天吃啥")
    assert "冰美式" in t and "别啰嗦" in t and "昨天在写代码" in t
    assert "绿豆汤" not in t, "她自己的回话不许进笔记素材（会把自己的编造记成事实）"
    assert "prefs" in t and "只输出 JSON" in t
