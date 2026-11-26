# SPDX-License-Identifier: AGPL-3.0-or-later

"""工头事实层（M1）：活的状态只由会话/审批/提醒事实算，不调模型。"""
import json
import time

import boss


def _sess(sid, **kw):
    d = {"sid": sid, "title": "会话-" + sid, "agent": "claude", "alive": True,
         "state": "idle", "last": 1000.0, "started": 900.0, "cwd": "/Users/x/proj-" + sid,
         "proj": "proj-" + sid}
    d.update(kw)
    return d


def _pend(sid, **kw):
    d = {"sid": sid, "title": "会话-" + sid, "agent": "claude", "rid": "pm_1",
         "ask": {"action": "推送", "target": "main"}, "risk": [], "why": "老板让发布",
         "options": [{"id": "allow", "label": "批准", "is_deny": False},
                     {"id": "deny", "label": "拒绝", "is_deny": True}],
         "text": "Bash 需要确认", "ts": 1000.0}
    d.update(kw)
    return d


def _done(sid, ts=1000.0, **kw):
    d = {"sid": sid, "title": "会话-" + sid, "kind": "done", "ts": ts,
         "summary": "三件套发完了", "about": "发版", "state": "", "text": "已完成"}
    d.update(kw)
    return d


def test_board_splits_running_waiting_done():
    now = 2000.0
    sessions = [
        _sess("run1", state="busy", last=1990.0),
        _sess("wait1", state="busy", last=1995.0),
        _sess("done1", alive=False, state="exited", last=1500.0),
    ]
    pending = [_pend("wait1")]
    hist = [_done("done1", ts=1500.0)]
    b = boss.project(sessions=sessions, pending=pending, hist=hist,
                     artifacts={}, verdicts={}, now=now, machine="macmini4")
    assert [w["sid"] for w in b["now"]] == ["run1"]
    assert [w["sid"] for w in b["decide"]] == ["wait1"]
    assert [w["sid"] for w in b["done"]] == ["done1"]
    assert b["counts"] == {"now": 1, "idle": 0, "decide": 1, "done": 1}
    assert b["decide"][0]["rid"] == "pm_1"
    assert b["decide"][0]["ask"]["target"] == "main"


def test_pending_beats_done_for_same_session():
    now = 2000.0
    sessions = [_sess("s1", state="busy", last=1999.0)]
    b = boss.project(sessions=sessions, pending=[_pend("s1")],
                     hist=[_done("s1", ts=1998.0)], artifacts={}, verdicts={},
                     now=now, machine="m")
    assert b["now"] == []
    assert b["done"] == []
    assert [w["sid"] for w in b["decide"]] == ["s1"]


def test_accepted_done_is_hidden():
    now = 2000.0
    sessions = [_sess("d1", alive=False, state="exited", last=1500.0)]
    verdicts = {"d1": {"verdict": "accepted", "ts": 1600.0}}
    b = boss.project(sessions=sessions, pending=[], hist=[_done("d1", ts=1500.0)],
                     artifacts={}, verdicts=verdicts, now=now, machine="m")
    assert b["done"] == []
    assert b["counts"]["done"] == 0


def test_done_work_carries_artifacts():
    now = 2000.0
    sessions = [_sess("d1", alive=False, state="exited", last=1500.0)]
    arts = {"d1": [{"name": "index.html", "rel": "dist/index.html", "kind": "page",
                    "size": 120, "mtime": 1499}]}
    b = boss.project(sessions=sessions, pending=[], hist=[_done("d1")],
                     artifacts=arts, verdicts={}, now=now, machine="m")
    w = b["done"][0]
    assert w["artifacts"][0]["name"] == "index.html"
    assert w["state"] == "done"


def test_empty_board_sentence_is_not_system_speak():
    b = boss.project(sessions=[], pending=[], hist=[], artifacts={}, verdicts={},
                     now=1000.0, machine="m")
    assert b["counts"] == {"now": 0, "idle": 0, "decide": 0, "done": 0}
    assert "没活" in b["say"] or "派" in b["say"]


def test_sentence_counts():
    b = boss.project(sessions=[_sess("a", state="busy", last=1000.0),
                               _sess("b", state="busy", last=1000.0)],
                     pending=[_pend("c")], hist=[_done("d")], artifacts={},
                     verdicts={}, now=1100.0, machine="m")
    assert "2" in b["say"] and "1" in b["say"]


def test_ui_disabled_by_default_and_dev_allowlist(tmp_path):
    p = tmp_path / "boss.json"
    assert boss.ui_enabled("d1", p) is False
    p.write_text(json.dumps({"enabled": True, "devs": []}), encoding="utf-8")
    assert boss.ui_enabled("d1", p) is True
    p.write_text(json.dumps({"enabled": True, "devs": ["d2"]}), encoding="utf-8")
    assert boss.ui_enabled("d1", p) is False
    assert boss.ui_enabled("d2", p) is True


def test_transitions_recorded_once(tmp_path):
    p = tmp_path / "events.jsonl"
    st = tmp_path / "state.json"
    works = [{"sid": "s1", "state": "running", "ts": 1000.0},
             {"sid": "s2", "state": "waiting", "ts": 1000.0}]
    boss.record_transitions(works, events_path=p, state_path=st, now=1000.0)
    boss.record_transitions(works, events_path=p, state_path=st, now=1010.0)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    ev = json.loads(lines[0])
    assert ev["sid"] == "s1" and ev["state"] == "running" and ev["basis"]


def test_closed_session_without_done_event_is_skipped():
    b = boss.project(sessions=[_sess("x", alive=False, state="exited", last=1000.0)],
                     pending=[], hist=[], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert b["now"] == [] and b["done"] == [] and b["decide"] == []


# ---- 端点层：灰度闸门 + 三块板数据（走真实 handler）--------------------------

def _req(method="GET", path="/api/boss/board", **kw):
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request(method, path, **kw)


def _wire(monkeypatch, tmp_path, sessions=None, pending=None, hist=None):
    import server
    cfg = tmp_path / "boss.json"
    monkeypatch.setattr(boss, "UI_CFG", cfg)
    monkeypatch.setattr(boss, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(boss, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(boss, "SAYS_PATH", tmp_path / "says.json")
    monkeypatch.setattr(boss, "BRAIN_PATH", tmp_path / "brain.json")

    async def _ok(request):
        return None
    monkeypatch.setattr(server, "require_token", _ok)

    async def _find(sid):
        return next((x for x in sessions or [] if x.get("sid") == sid), None)
    monkeypatch.setattr(server, "_find_session", _find)

    async def _ses(request):
        return sessions or []
    monkeypatch.setattr(server, "_ext_sessions_items", _ses)
    monkeypatch.setattr(server, "_pending_list", lambda: pending or [])
    monkeypatch.setattr(server, "_nh_list", lambda **kw: hist or [])
    monkeypatch.setattr(server, "_face_for", lambda r: None)
    return server, cfg


def test_board_endpoint_is_off_without_config(tmp_path, monkeypatch):
    import asyncio
    server, cfg = _wire(monkeypatch, tmp_path)
    r = asyncio.run(server.h_boss_board(_req()))
    d = json.loads(r.body)
    assert d["enabled"] is False and d["ok"] == 0

    cfg.write_text(json.dumps({"enabled": True, "devs": ["other"]}),
                   encoding="utf-8")
    r = asyncio.run(server.h_boss_board(_req(path="/api/boss/board?dev=d1")))
    assert json.loads(r.body)["enabled"] is False


def test_board_endpoint_returns_work_when_enabled(tmp_path, monkeypatch):
    import asyncio
    now = time.time()
    sessions = [{"sid": "s1", "title": "发版", "agent": "claude", "alive": True,
                 "state": "busy", "last": now - 5, "cwd": "/Users/x/proj"}]
    server, cfg = _wire(monkeypatch, tmp_path, sessions=sessions)
    cfg.write_text(json.dumps({"enabled": True, "devs": []}), encoding="utf-8")
    r = asyncio.run(server.h_boss_board(_req(path="/api/boss/board?dev=d1")))
    d = json.loads(r.body)
    assert d["enabled"] is True
    assert d["counts"]["now"] == 1 and d["now"][0]["sid"] == "s1"
    # 状态跃迁留痕：第一次看到这块板就写一行
    assert json.loads((tmp_path / "events.jsonl").read_text(
        encoding="utf-8").strip().splitlines()[0])["state"] == "running"


def test_work_and_verdict_endpoints_are_gated(tmp_path, monkeypatch):
    import asyncio
    server, cfg = _wire(monkeypatch, tmp_path)
    assert asyncio.run(server.h_boss_work(
        _req(path="/api/boss/work/s1"))).status == 403

    async def _json():
        return {"sid": "s1", "verdict": "accepted"}
    req = _req("POST", "/api/boss/verdict")
    req.json = _json
    assert asyncio.run(server.h_boss_verdict(req)).status == 403
    assert boss.load_state(tmp_path / "state.json")["verdicts"] == {}

    cfg.write_text(json.dumps({"enabled": True, "devs": []}), encoding="utf-8")
    r = asyncio.run(server.h_boss_verdict(req))
    assert r.status == 200
    st = boss.load_state(tmp_path / "state.json")
    assert st["verdicts"]["s1"]["verdict"] == "accepted"


# ---- 判断质量：临时目录 / host 自己的 scratch 不算老板的活 --------------------

def test_scratch_cwd_is_not_work():
    """实测踩到：/private/tmp 里「回复：好」的一次性会话、~/.codeboss/pet_brain 里
    桌宠记忆维护，都被当成「交了活」上板。老板的活只发生在真项目目录里。"""
    now = 2000.0
    home = str(__import__("pathlib").Path.home())
    hist = [_done("real", ts=1900.0, cwd="/Users/x/proj", summary="发版完成"),
            _done("tmp1", ts=1950.0, cwd="/private/tmp", summary="确认回复"),
            _done("tmp2", ts=1960.0, cwd="/private/var/folders/xy/T/tmpab12",
                  summary="确认回复"),
            _done("pet1", ts=1970.0, cwd=home + "/.codeboss/pet_brain",
                  summary="维护桌宠长期记忆")]
    b = boss.project(sessions=[], pending=[], hist=hist, artifacts={},
                     verdicts={}, now=now, machine="m")
    assert [w["sid"] for w in b["done"]] == ["real"]
    assert b["counts"]["done"] == 1


def test_scratch_running_session_is_not_now():
    now = 2000.0
    sessions = [_sess("run1", state="busy", last=1999.0, cwd="/Users/x/proj"),
                _sess("tmp1", state="busy", last=1999.0, cwd="/tmp")]
    b = boss.project(sessions=sessions, pending=[], hist=[], artifacts={},
                     verdicts={}, now=now, machine="m")
    assert [w["sid"] for w in b["now"]] == ["run1"]


def test_approval_in_scratch_dir_still_asks():
    """审批不能因为目录被过滤掉——放行是老板必须看见的事。"""
    b = boss.project(sessions=[], pending=[_pend("tmp1")], hist=[], artifacts={},
                     verdicts={}, now=2000.0, machine="m")
    assert [w["sid"] for w in b["decide"]] == ["tmp1"]


def test_card_sentence_has_no_section_prefix():
    """卡头已经写着「已交活 / 等你定」，句子再带一次「交了活：」就是同一件事两遍。"""
    b = boss.project(sessions=[_sess("d1", alive=False, state="exited", last=1500.0)],
                     pending=[], hist=[_done("d1")], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert b["done"][0]["say"] == "三件套发完了"
    b2 = boss.project(sessions=[], pending=[_pend("w1")], hist=[], artifacts={},
                      verdicts={}, now=2000.0, machine="m")
    assert not b2["decide"][0]["say"].startswith("卡住了")


# ---- 正在跑的会话必须看得到（发现出来的会话没有 alive 字段）------------------

def _disc(sid, **kw):
    """agents 发现出来的电脑会话：有 pid、没有 alive 字段。"""
    d = {"sid": sid, "title": "会话-" + sid, "agent": "claude", "pid": 1234,
         "state": "busy", "last": 1990.0, "cwd": "/Users/x/proj-" + sid,
         "proj": "proj-" + sid}
    d.update(kw)
    return d


def test_discovered_running_session_shows_in_now():
    b = boss.project(sessions=[_disc("run1")], pending=[], hist=[], artifacts={},
                     verdicts={}, now=2000.0, machine="m")
    assert [w["sid"] for w in b["now"]] == ["run1"]
    assert b["now"][0]["state"] == "running"


def test_new_turn_beats_old_done_event():
    """交过活之后又开了新一轮：板上应该回到「现在」，不能一直停在「已交活」。"""
    b = boss.project(sessions=[_disc("s1", last=1990.0)], pending=[],
                     hist=[_done("s1", ts=1500.0)], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert [w["sid"] for w in b["now"]] == ["s1"]
    assert b["done"] == []


def test_done_event_after_last_output_stays_done():
    b = boss.project(sessions=[_disc("s1", state="idle", last=1900.0)], pending=[],
                     hist=[_done("s1", ts=1950.0)], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert b["done"] and [w["sid"] for w in b["done"]] == ["s1"]
    assert b["now"] == []


def test_busy_but_silent_for_long_is_idle():
    """适配器报 busy，但 39 分钟没输出：这是「停在上一轮」，不能写成「在跑着」。"""
    b = boss.project(sessions=[_disc("s1", state="busy", last=1000.0)], pending=[],
                     hist=[], artifacts={}, verdicts={}, now=4000.0, machine="m")
    assert b["idle"][0]["state"] == "idle" and b["now"] == []


def test_say_counts_running_and_stopped_separately():
    """停着的会话不能被说成「在跑」——它是在等老板吩咐。"""
    b = boss.project(sessions=[_disc("a", state="busy", last=1990.0),
                               _disc("b", state="idle", last=1500.0)],
                     pending=[], hist=[], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert "1 件在跑" in b["say"] and "1 件等你吩咐" in b["say"]


# ---- 「停在上一轮」不是「现在」：它是在等你吩咐 ----------------------------

def test_idle_sessions_get_their_own_board():
    b = boss.project(sessions=[_disc("run1", state="busy", last=1990.0),
                               _disc("idle1", state="idle", last=1500.0)],
                     pending=[], hist=[], artifacts={}, verdicts={},
                     now=2000.0, machine="m")
    assert [w["sid"] for w in b["now"]] == ["run1"]
    assert [w["sid"] for w in b["idle"]] == ["idle1"]
    assert b["counts"] == {"now": 1, "idle": 1, "decide": 0, "done": 0}
    assert "1 件等你吩咐" in b["say"]


# ---- M2 · 工头大脑 v1：模型只说「怎么说」，不说「是什么」-------------------
# 状态仍然只由事实算；模型只把跃迁说成老板能听懂的一句。

def test_say_key_is_per_turn_not_per_poll():
    """同一轮里输出一直在涨（ts 变），不能每 20 秒重算一句；换了一轮才重算。"""
    w = {"sid": "s1", "state": "running", "about": "发版", "ts": 1000.0}
    assert boss.say_key(dict(w, ts=1300.0)) == boss.say_key(w)
    assert boss.say_key(dict(w, about="跑回归")) != boss.say_key(w)


def test_clean_say_rejects_completion_while_running():
    """硬约束：在跑的时候不能由模型宣布「做完了」——完成只由事实判定。"""
    assert boss.clean_say("发版做完了", {"state": "running"}) == ("", "facts")
    assert boss.clean_say("我还在跑回归", {"state": "running"})[1] == "llm"


def test_clean_say_strips_quotes_and_caps_length():
    say, src = boss.clean_say('“我把三台机器都升级了。”', {"state": "done"})
    assert (say, src) == ("我把三台机器都升级了。", "llm")
    assert len(boss.clean_say("一" * 200, {"state": "done"})[0]) == 60
    assert boss.clean_say("  \n ", {"state": "idle"}) == ("", "facts")


def test_apply_says_replaces_fact_sentence():
    b = boss.project(sessions=[_disc("s1")], pending=[], hist=[], artifacts={},
                     verdicts={}, now=2000.0, machine="m")
    says = {boss.say_key(b["now"][0]): {"say": "我在给发版收尾。", "src": "llm",
                                        "ts": 1.0}}
    boss.apply_says(b, says)
    assert b["now"][0]["say"] == "我在给发版收尾。"
    assert b["now"][0]["say_src"] == "llm" and b["meta"]["brain"] == "llm"
    b2 = boss.project(sessions=[_disc("s2")], pending=[], hist=[], artifacts={},
                      verdicts={}, now=2000.0, machine="m")
    boss.apply_says(b2, {})
    assert b2["now"][0]["say_src"] == "facts" and b2["meta"]["brain"] == "facts"


def test_say_facts_carry_what_the_model_may_say():
    b = boss.project(sessions=[], pending=[], hist=[_done("d1", about="发版")],
                     artifacts={}, verdicts={}, now=2000.0, machine="m")
    f = boss.say_facts(b["done"][0])
    assert "发版" in f and "三件套发完了" in f


def test_budget_stops_at_max_and_resets_next_day(tmp_path):
    p = tmp_path / "brain.json"
    assert boss.budget_take(p, maxn=2, day="2026-11-22")
    assert boss.budget_take(p, maxn=2, day="2026-11-22")
    assert not boss.budget_take(p, maxn=2, day="2026-11-22")
    assert boss.budget_take(p, maxn=2, day="2026-11-23")


def test_brain_cfg_can_be_switched_off(tmp_path):
    cfg = tmp_path / "boss.json"
    cfg.write_text(json.dumps({"enabled": True, "brain": {"on": False}}),
                   encoding="utf-8")
    assert boss.brain_cfg(cfg)["on"] is False
    assert boss.brain_cfg(tmp_path / "missing.json")["max"] == 200


def test_board_uses_model_say_on_next_poll(tmp_path, monkeypatch):
    """模型慢不能拖住板子：第一次先给事实句并标 pending，下一轮换成它说的。"""
    import asyncio
    sessions = [{"sid": "s1", "title": "发版", "agent": "claude", "alive": True,
                 "state": "busy", "last": time.time() - 5, "cwd": "/Users/x/proj",
                 "about": "发版"}]
    server, cfg = _wire(monkeypatch, tmp_path, sessions=sessions)
    cfg.write_text(json.dumps({"enabled": True, "devs": []}), encoding="utf-8")
    seen = {}

    async def fake(prompt, text, purpose, timeout=20):
        seen["purpose"], seen["text"] = purpose, text
        return "我在给发版收尾。"

    monkeypatch.setattr(server.notify, "_llm_text", fake)

    async def go():
        r1 = await server.h_boss_board(_req(path="/api/boss/board?dev=d1"))
        await asyncio.sleep(0.05)
        r2 = await server.h_boss_board(_req(path="/api/boss/board?dev=d1"))
        return json.loads(r1.body), json.loads(r2.body)

    d1, d2 = asyncio.run(go())
    assert d1["now"][0].get("say_pending") and d1["now"][0]["say_src"] == "facts"
    assert d2["now"][0]["say"] == "我在给发版收尾。"
    assert d2["now"][0]["say_src"] == "llm" and d2["meta"]["brain"] == "llm"
    assert seen["purpose"] == "boss_say" and "发版" in seen["text"]


def test_work_endpoint_opens_idle_work(tmp_path, monkeypatch):
    """停在上一轮的那件活也要能点进去改一句（以前 work 只在 now/decide/done 里找）。"""
    import asyncio
    sessions = [{"sid": "idle1", "title": "拓竹", "agent": "claude", "alive": True,
                 "state": "idle", "last": time.time() - 3000,
                 "cwd": "/Users/x/proj"}]
    server, cfg = _wire(monkeypatch, tmp_path, sessions=sessions)
    cfg.write_text(json.dumps({"enabled": True, "devs": [], "brain": {"on": False}}),
                   encoding="utf-8")
    r = asyncio.run(server.h_boss_work(
        _req(path="/api/boss/work/idle1?dev=d1", match_info={"sid": "idle1"})))
    d = json.loads(r.body)
    assert d["work"] and d["work"]["sid"] == "idle1"


# ---- 看得到它在跑什么：一件活页的「它现在」（转录尾巴，不是终端回放）--------

def test_live_pick_takes_last_say_and_tool():
    """老板发完一句话最想知道：它现在在干什么、刚说了什么。"""
    import server
    msgs = [{"role": "user", "text": "发版", "tools": []},
            {"role": "assistant", "text": "", "tools": [{"name": "Bash",
                                                        "detail": "npm test"}]},
            {"role": "assistant", "text": "测试全绿，开始发版", "tools": []}]
    d = server._boss_live_pick(msgs)
    assert d["say"] == "测试全绿，开始发版"
    assert "Bash" in d["tool"] and "npm test" in d["tool"]
    assert server._boss_live_pick([]) == {"say": "", "tool": ""}
    assert len(server._boss_live_pick(
        [{"role": "assistant", "text": "一" * 500, "tools": []}])["say"]) <= 301


def test_work_endpoint_includes_live_for_running_work(tmp_path, monkeypatch):
    import asyncio
    sessions = [{"sid": "run1", "title": "发版", "agent": "claude", "alive": True,
                 "state": "busy", "last": time.time() - 3, "cwd": "/Users/x/proj"}]
    server, cfg = _wire(monkeypatch, tmp_path, sessions=sessions)
    cfg.write_text(json.dumps({"enabled": True, "devs": [], "brain": {"on": False}}),
                   encoding="utf-8")

    async def _find(sid):
        return sessions[0]

    async def _live(sid, s):
        return {"say": "正在跑回归", "tool": "Bash · npm test"}

    monkeypatch.setattr(server, "_find_session", _find)
    monkeypatch.setattr(server, "_boss_live", _live)
    r = asyncio.run(server.h_boss_work(
        _req(path="/api/boss/work/run1?dev=d1", match_info={"sid": "run1"})))
    d = json.loads(r.body)
    assert d["live"]["tool"] == "Bash · npm test"


# ---- 产出 = 它这一轮/上一轮真写过的文件，不是 cwd 里最新的大杂烩 ---------------

def test_work_artifacts_come_from_what_agent_wrote(tmp_path, monkeypatch):
    """用户：产出里没有这一轮上一轮产出的内容。按 cwd 扫 mtime 只会捞出日志/数据库，
    还会漏掉写在 cwd 之外的（worktree、记忆目录）；改成以转录里 Write/Edit 为准。"""
    import asyncio
    proj = tmp_path / "proj"
    proj.mkdir()
    out = tmp_path / "elsewhere" / "spec.md"
    out.parent.mkdir()
    out.write_text("hello", encoding="utf-8")
    sessions = [{"sid": "s1", "title": "发版", "agent": "claude", "alive": True,
                 "state": "busy", "last": time.time() - 3, "cwd": str(proj)}]
    server, cfg = _wire(monkeypatch, tmp_path, sessions=sessions)
    cfg.write_text(json.dumps({"enabled": True, "devs": [], "brain": {"on": False}}),
                   encoding="utf-8")

    async def _live(sid, s):
        return {"say": "", "tool": "", "files": [str(out)]}

    monkeypatch.setattr(server, "_boss_live", _live)
    r = asyncio.run(server.h_boss_work(
        _req(path="/api/boss/work/s1?dev=d1", match_info={"sid": "s1"})))
    d = json.loads(r.body)
    arts = d["artifacts"]
    assert arts and arts[0]["name"] == "spec.md"
    assert arts[0]["path"] == str(out.resolve())
    assert arts[0]["kind"] == "doc"

    # 会话 cwd 之外的路径：只有它自己写过的才给预览
    p, _mime = asyncio.run(server._ext_file_meta("s1", str(out)))
    assert p == out.resolve()
    try:
        asyncio.run(server._ext_file_meta("s1", "/etc/hosts"))
    except server._ExtPathError:
        pass
    else:
        raise AssertionError("没写过的 cwd 外文件不该放行")


# ---- 产出兜底不许把运行时噪音当交付物 ---------------------------------------

def test_art_fallback_drops_runtime_noise():
    """写过的文件都失效/没有时才轮到 cwd 扫描兜底；logs/、.sock、数据库这些
    运行时噪音不该占「产出」（用户验证时看到的全是 ansi 日志）。"""
    import server
    assert not server._boss_art_ok("host/logs/ansi/b1533510.log")
    assert not server._boss_art_ok("logs/ansi/x.log")
    assert not server._boss_art_ok("host/logs/ansi/6f852452.sock")
    assert not server._boss_art_ok("host/codeboss.db")
    assert server._boss_art_ok("static/index.html")
    assert server._boss_art_ok("host/server.py")
    assert server._boss_art_ok("debug.log")        # 根目录的交付日志不拦
