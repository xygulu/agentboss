# SPDX-License-Identifier: AGPL-3.0-or-later

"""转录解析的直读用例：jsonl 进、消息/命令/标题出。

以前这些逻辑埋在 server.py 里只能靠整机跑，格式一漂（Claude 改字段）没人当着它面红。
"""
import json

import transcript


def _jsonl(tmp_path, lines, name="s1.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                 encoding="utf-8")
    return p


def test_parse_keeps_text_tools_and_full_bash_cmd(tmp_path):
    long = "git commit -m 'a' && " * 20          # 超过 160 字：detail 截断，cmd 留全文
    p = _jsonl(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "看下这个"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "name": "Bash", "input": {"command": long}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]}},
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": "旁路"}},
        {"type": "summary", "summary": "meta 行"},
    ])
    msgs = transcript.parse_transcript(p)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    tools = msgs[1]["tools"]
    assert tools[0]["name"] == "Bash" and tools[0]["cmd"] == long
    assert tools[0]["detail"].endswith("…")
    assert tools[1]["detail"] == "/a/b.py"
    # 认不出的行（summary / sidechain）不进来
    assert all("旁路" not in m["text"] for m in msgs)


def test_cmds_prefers_full_command_and_drops_truncated(tmp_path):
    msgs = [{"role": "assistant", "text": "", "tools": [
        {"name": "Bash", "detail": "/x…", "cmd": "/x --full"},
        {"name": "Bash", "detail": "echo hi…"},          # 没 cmd 且截断 → 跑不了，丢
        {"name": "Read", "detail": "/a"},
    ]}]
    assert transcript._transcript_cmds(msgs) == ["/x --full"]


def test_meta_prefers_ai_title_over_first_user_message(tmp_path):
    p = _jsonl(tmp_path, [
        {"type": "user", "cwd": "/work/proj",
         "message": {"role": "user", "content": [{"type": "text", "text": "帮我把首页改一下"}]}},
        {"type": "ai-title", "aiTitle": "改首页"},
    ])
    assert transcript._transcript_meta(p) == ("/work/proj", "改首页")


def test_meta_falls_back_to_user_message_and_skips_command_noise(tmp_path):
    p = _jsonl(tmp_path, [
        {"type": "user", "cwd": "/w",
         "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "user", "message": {"role": "user", "content": "第二个才是真问题"}},
    ])
    assert transcript._transcript_meta(p) == ("/w", "第二个才是真问题")


def _closed(tmp_path, monkeypatch, files):
    """files: [(项目目录名, sid, cwd, 标题, mtime)] → 按 _claude_closed_sessions 的树摆好。"""
    import os
    base = tmp_path / ".claude" / "projects"
    for proj, sid, cwd, title, mt in files:
        d = base / proj
        d.mkdir(parents=True, exist_ok=True)
        p = d / (sid + ".jsonl")
        p.write_text(json.dumps({"type": "user", "cwd": cwd,
                                 "message": {"role": "user", "content": title}}) + "\n",
                     encoding="utf-8")
        os.utime(p, (mt, mt))
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda cls: tmp_path))
    return base


def test_closed_sessions_dedupes_same_sid_across_project_dirs(tmp_path, monkeypatch):
    """迁移后同一个 sid 可能在新旧两个项目目录各有一份转录（2026-11-20 aiboss 合并）。
    以前两份都进列表：手机上同一条会话出现两次、最新那份还不一定排在前面。"""
    _closed(tmp_path, monkeypatch, [
        ("-old", "dup", "/Users/me/old", "旧的一份", 100),
        ("-new", "dup", "/Users/me/new", "新的一份", 200),
        ("-new", "other", "/Users/me/new", "别的会话", 150),
    ])
    rows = transcript._claude_closed_sessions(set(), 20)
    assert [r["sid"] for r in rows] == ["dup", "other"]      # 不重复
    assert rows[0]["cwd"] == "/Users/me/new"                 # 留 mtime 最新的那份


def test_closed_sessions_default_keeps_more_than_20(tmp_path, monkeypatch):
    """默认上限要盖得住一台机器的历史会话：20 条时合并迁移进来的 aiboss 会话
    被别的项目挤出列表（用户反馈「aiboss 下的会话没有都列出来」）。"""
    _closed(tmp_path, monkeypatch, [
        ("-p", "s%02d" % i, "/Users/me/p", "会话%d" % i, 100 + i) for i in range(25)
    ])
    assert len(transcript._claude_closed_sessions(set())) == 25


def test_meta_skips_continuation_boilerplate(tmp_path):
    """上下文用完续开的会话，首条「用户消息」是续写说明，不是用户说的话：
    以前拿它当标题，列表里全是「This session is being co…」。"""
    p = _jsonl(tmp_path, [
        {"type": "user", "cwd": "/w", "message": {"role": "user", "content":
            "This session is being continued from a previous conversation that ran out of context. "
            "The summary below covers the earlier portion…"}},
        {"type": "user", "cwd": "/w", "message": {"role": "user", "content": "继续改打印机固件"}},
    ])
    assert transcript._transcript_meta(p) == ("/w", "继续改打印机固件")


def test_closed_sessions_collapses_continuation_chain(tmp_path, monkeypatch):
    """续篇和原件是同一条对话：列表只留最近活跃的那条。
    手机上以前是两条同名会话（2026-11-20 PVE / BambuHelper 各一对）。"""
    base = _closed(tmp_path, monkeypatch, [
        ("-p", "parent", "/w/pve", "PVE 集群风扇狂转", 100),
        ("-p", "child", "/w/pve", "续篇", 200),
    ])
    # 续篇首条用户消息就是续写说明，里面带着上一份转录的路径
    p = base / "-p" / "child.jsonl"
    p.write_text(json.dumps({"type": "user", "cwd": "/w/pve", "message": {"role": "user",
        "content": "This session is being continued from a previous conversation… 完整转录："
                   + str(base / "-p" / "parent.jsonl")}}) + "\n"
                  + json.dumps({"type": "user", "cwd": "/w/pve", "message": {"role": "user",
                                                                             "content": "续篇"}}) + "\n",
                 encoding="utf-8")
    import os
    os.utime(p, (200, 200))
    rows = transcript._claude_closed_sessions(set(), 20)
    assert [r["sid"] for r in rows] == ["child"]        # 留最近的续篇


def test_closed_sessions_continuation_keeps_newer_original(tmp_path, monkeypatch):
    """续篇也不是永远更新：原件后来又接着用了，就留原件。"""
    base = _closed(tmp_path, monkeypatch, [
        ("-p", "parent", "/w/pve", "原件后来又用了", 300),
        ("-p", "child", "/w/pve", "续篇", 200),
    ])
    p = base / "-p" / "child.jsonl"
    p.write_text(json.dumps({"type": "user", "cwd": "/w/pve", "message": {"role": "user",
        "content": "This session is being continued from a previous conversation… "
                   + str(base / "-p" / "parent.jsonl")}}) + "\n",
                 encoding="utf-8")
    import os
    os.utime(p, (200, 200))
    assert [r["sid"] for r in transcript._claude_closed_sessions(set(), 20)] == ["parent"]


def test_closed_sessions_fork_with_living_parent_stays_separate(tmp_path, monkeypatch):
    """fork / 双窗口分头聊：原件在 fork 出生后还活着（>10 分钟）＝ 早就分道扬镳的两条
    对话，关掉原件也要留在列表当「已退出」，不许被活着的 fork 吞掉（2026-11-24
    agentboss 被副本吞）。"""
    import os
    base = _closed(tmp_path, monkeypatch, [
        ("-p", "orig", "/w/aiboss", "agentboss", 5000),
        ("-p", "fork", "/w/aiboss", "agentboss 副本", 1300),
    ])
    p = base / "-p" / "fork.jsonl"
    p.write_text(json.dumps({"type": "user", "cwd": "/w/aiboss",
                             "timestamp": "1970-01-01T00:20:00.000Z",   # 出生 = epoch 1200
                             "message": {"role": "user",
        "content": "This session is being continued from a previous conversation… "
                   + str(base / "-p" / "orig.jsonl")}}) + "\n",
                 encoding="utf-8")
    os.utime(p, (1300, 1300))
    # 原件活到 5000，fork 出生 1200 —— 原件在 fork 出生后还聊了 3800 秒
    rows = transcript._claude_closed_sessions(set(), 20)
    assert [r["sid"] for r in rows] == ["orig", "fork"]     # 两条都留，谁也不吞谁


def test_closed_sessions_shows_last_cwd_after_session_moves(tmp_path, monkeypatch):
    """会话搬家（~/agentboss → aiboss 单仓）后，已退出列表的 cwd 该显示它现在
    住哪，不是出生地（2026-11-24 agentboss 卡片 cwd 还是旧仓库）。"""
    import os
    d = tmp_path / ".claude" / "projects" / "-p"
    d.mkdir(parents=True)
    p = d / "moved.jsonl"
    p.write_text(
        json.dumps({"type": "user", "cwd": "/Users/me/old",
                    "message": {"role": "user", "content": "agentboss"}}) + "\n"
        + json.dumps({"type": "user", "cwd": "/Volumes/me/aiboss",
                      "message": {"role": "user", "content": "搬家后的话"}}) + "\n",
        encoding="utf-8")
    os.utime(p, (200, 200))
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda cls: tmp_path))
    rows = transcript._claude_closed_sessions(set(), 20)
    assert rows[0]["cwd"] == "/Volumes/me/aiboss"


def test_project_jsonl_finds_by_sid_when_cwd_escaping_differs(tmp_path, monkeypatch):
    """项目目录名各平台转义不一样：按 cwd 拼不出来时靠 sid 兜底扫描。"""
    base = tmp_path / ".claude" / "projects"
    d = base / "-weird-name"
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda cls: tmp_path))
    assert transcript.project_jsonl("/completely/different", "abc") == d / "abc.jsonl"
    assert transcript.project_jsonl("/completely/different", "nope") is None


def test_recent_files_lists_only_writes_newest_first(tmp_path):
    """工头「产出」要的是它真动手写的文件——不按 cwd 猜，也不把日志当产出。
    写文件外面（worktree、记忆目录）也算，所以要完整路径，不能拿 160 字截断版。"""
    long = "/Users/x/" + "d" * 200 + "/out.md"
    p = _jsonl(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "改一下"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/p/a.py"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": long}}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "touch /p/x"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/p/b.py"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/p/a.py"}}]}},
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant",
         "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "/p/byt.py"}}]}},
    ])
    assert transcript.recent_files(p) == ["/p/a.py", long]
