# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口核心 Phase 1 · Task 6：gemini ACP 适配器（协议源窗口）。

全链：AcpWindow 起 fixture 假服务器 → session/new → prompt → session/update
落 hist（chunk 文本 + 工具行）、seq 递增、scrollback 落盘；崩溃 → exited、
hist 保留、respawn 干净；h_ext_live 的 protocol 分支返回手机可渲染的 hist。

跑法： .venv/bin/python -m pytest tests/test_agent_acp.py -q
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pyte.screens
import pytest

import acp
import agent_acp
import scrollback

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "acp_fake.py")]


def _row(text):
    return {i: pyte.screens.Char(ch) for i, ch in enumerate(text)}


def _plain(row):
    return "".join(c.data for _, c in sorted(row.items()))


def _lines(w):
    return [_plain(r) for r in w.hist]


def test_full_chain_prompt_to_hist(tmp_path, monkeypatch):
    """(a) prompt → hist 含 chunk 文本与「工具」行、seq 递增、落盘。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)

    async def run():
        w = agent_acp.AcpWindow("acp-t1", cwd="/tmp")
        await w.start(FAKE)
        assert w.state == "running"
        r = await w.prompt("你好")
        assert r["stopReason"] == "end_turn"
        lines = _lines(w)
        assert any("你好" in ln for ln in lines), "chunk 文本要进 hist"
        assert any(ln.startswith("工具 ") for ln in lines), "tool_call 要成「工具」行"
        s0 = w.seq
        await w.prompt("再来一句")
        assert w.seq > s0, "seq 随 hist 行数递增"
        await w.close()
        assert w.conn.proc.returncode is not None
        # 落仓在 close 兜底冲盘后可见（逐事件写盘已被防抖取代——这正是修复点）
        saved = scrollback.load("acp-t1")
        assert any("你好" in _plain(r2) for r2 in saved), "hist 落进 scrollback 仓"
    asyncio.run(run())


def test_adapter_shape(monkeypatch, tmp_path):
    """适配器注册进 agents：key/label/launch_argv 读 boss.json。"""
    import agents as _ag
    ad = _ag.get("gemini")
    assert ad is not None, "gemini 适配器要注册进 agents"
    assert ad.label and "ACP" in ad.label
    assert ad.discover() == []
    # acpm.command 可配置（换 agent 是配置不是代码）：boss.json 缺失 → 缺省命令
    monkeypatch.setattr(agent_acp, "BOSS_CFG", tmp_path / "_nope_boss.json")
    argv = ad.launch_argv("/tmp")
    if argv is not None:                        # 机器上装了 gemini-acp 时
        assert argv == ["gemini-acp"]


def test_crash_exited_hist_retained_respawn_clean(tmp_path, monkeypatch):
    """(b) ACPM_FAKE_DIE 崩溃：状态 exited、hist/仓保留、respawn 干净不留旧连接。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    scrollback.save("acp-t2", [_row("旧的一条")])       # 预埋：重启后要认得

    async def run():
        monkeypatch.setenv("ACPM_FAKE_DIE", "1")
        w = agent_acp.AcpWindow("acp-t2", cwd="/tmp")
        await w.start(FAKE)
        assert any("旧的一条" in ln for ln in _lines(w)), "start 从仓里播种历史"
        with pytest.raises(acp.AcpError):
            await w.prompt("x")
        await asyncio.sleep(0.05)                # reader finally → 死亡回调
        assert w.state == "exited"
        assert any("旧的一条" in ln for ln in _lines(w)), "崩溃不清 hist"

        monkeypatch.delenv("ACPM_FAKE_DIE")
        old_conn = w.conn
        await w.start(FAKE)                      # respawn：同一窗口、干净重连
        assert w.state == "running"
        assert w.conn is not old_conn
        assert old_conn.proc.returncode is not None, "旧连接的进程确已退出"
        r = await w.prompt("活过来了")
        assert r["stopReason"] == "end_turn"
        assert any("你好" in ln for ln in _lines(w)), "respawn 后新对话的 chunk 也进 hist"
        await w.close()
    asyncio.run(run())


def test_ext_live_protocol_branch(tmp_path, monkeypatch):
    """h_ext_live 对 protocol 源：返回手机可渲染的 hist + seq + kind。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    import server

    async def _find(sid):
        return {"sid": sid, "agent": "gemini", "acp": 1, "state": "running"}

    async def _ok(req):
        return None

    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(server, "_find_session", _find)
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/api/ext_live/acp-t3")
    req.match_info["sid"] = "acp-t3"

    out = {}

    async def run():
        # 全程一个事件循环：子进程的 Future 绑在创建它的 loop 上，跨 loop 关会炸
        w = agent_acp.AcpWindow("acp-t3", cwd="/tmp")
        await w.start(FAKE)
        await w.prompt("手机上看得见")   # fake 回固定的「你好」chunk
        resp = await server.h_ext_live(req)
        out["resp"] = resp
        await w.close()
        agent_acp._WINDOWS.pop("acp-t3", None)
    asyncio.run(run())

    resp = out["resp"]
    assert resp.status == 200
    d = json.loads(resp.body)
    assert d["ok"] == 1
    assert d["kind"] == "protocol"
    assert "你好" in d["hist"]
    assert isinstance(d["seq"], int) and d["seq"] >= 1


# ---------- 评审修复轮（Critical #1 / Important #3 #4 #5）：端到端接线 ----------

class _FakeProto:
    """StreamReader 的 protocol 只在背压/唤醒时被摸到；喂完就 eof，全部 no-op。"""

    def __getattr__(self, k):
        return lambda *a, **kw: None


def _post_json(path, obj):
    """make_mocked_request 喂 POST JSON：request.json() 要 StreamReader。"""
    import asyncio as _aio
    from aiohttp.streams import StreamReader
    sr = StreamReader(_FakeProto(), 2 ** 16)
    sr.feed_data(json.dumps(obj).encode())
    sr.feed_eof()
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request("POST", path, payload=sr)


def _get(path):
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", path)
    req.match_info["sid"] = path.rsplit("/", 1)[-1]
    return req


async def _ok(request):
    return None


def test_open_send_close_del_endpoints(tmp_path, monkeypatch):
    """(Critical #1) 手机全链：＋开窗 → 发消息 → 关（进程停、hist 留）→ 删（仓清）。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    import server
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(agent_acp, "acpm_command", lambda: list(FAKE))
    out = {}

    async def run():
        # 1) ＋ 开窗：协议源不开终端，host 直连 stdio
        resp = await server.h_ext_open(_post_json("/api/ext_open",
                                                  {"agent": "gemini", "cwd": str(tmp_path)}))
        d = json.loads(resp.body)
        assert d.get("ok"), d
        sid = d["sid"]
        assert sid.startswith("acp-")
        w = agent_acp.window(sid)
        assert w is not None and w.state == "running"

        # 2) 发消息：prompt 走 ACP，chunk 落 hist
        resp = await server.h_ext_send(_post_json("/api/ext_send",
                                                  {"sid": sid, "text": "手机上发一句", "cid": "t-c1"}))
        assert json.loads(resp.body).get("ok")
        assert any("你好" in ln for ln in _lines(w))

        # 3) 关：进程停、窗口变已退出（hist 保留可召回）
        resp = await server.h_ext_sess_close(_post_json("/api/ext_sess_close", {"sid": sid}))
        d = json.loads(resp.body)
        assert d.get("ok") and d.get("killed")
        assert w.state == "exited"
        assert w.conn.proc.returncode is not None
        resp = await server.h_ext_live(_get("/api/ext_live/" + sid))
        assert resp.status == 200
        assert "你好" in json.loads(resp.body)["hist"]

        # 4) 删：窗口表清掉、滚动仓清掉
        resp = await server.h_ext_sess_del(_post_json("/api/ext_sess_del", {"sid": sid}))
        assert json.loads(resp.body).get("ok")
        assert agent_acp.window(sid) is None
        assert not scrollback.load(sid)
        out["sid"] = sid
    asyncio.run(run())


def test_ghost_after_restart_visible_and_readable(tmp_path, monkeypatch):
    """(Important #4/#3) 杀 host 重启（窗口表没了）：列表还有已退出的鬼影行、
    hist 从仓里可读、推送载荷也带（SSE 是列表主通道，缺了会把徽标冲掉）。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    import server
    monkeypatch.setattr(server, "require_token", _ok)
    monkeypatch.setattr(agent_acp, "acpm_command", lambda: list(FAKE))
    out = {}

    async def run():
        resp = await server.h_ext_open(_post_json("/api/ext_open",
                                                  {"agent": "gemini", "cwd": str(tmp_path)}))
        sid = json.loads(resp.body)["sid"]
        await server.h_ext_send(_post_json("/api/ext_send",
                                           {"sid": sid, "text": "hi", "cid": "t-c2"}))
        await server.h_ext_sess_close(_post_json("/api/ext_sess_close", {"sid": sid}))
        out["sid"] = sid

        # 模拟重启：窗口表整个蒸发（进程内字典），仓还在
        agent_acp._WINDOWS.clear()
    asyncio.run(run())
    sid = out["sid"]
    assert scrollback.load(sid), "close 时要把尾部也冲进仓"

    # 列表（pull 路）：鬼影行 = 已退出 + 协议源
    rows = asyncio.run(server._ext_sessions_items(_get("/api/ext_sessions")))
    g = next((r for r in rows if r["sid"] == sid), None)
    assert g is not None, "重启后 ACP 窗口不许从列表上蒸发（T3 静默孤儿化）"
    assert g["state"] == "exited" and g.get("acp") == 1
    assert g["source"]["kind"] == "protocol"

    # 推送（push 路）：载荷同样要有，且带 source
    monkeypatch.setattr(server, "_cached_sessions", lambda: [])
    monkeypatch.setattr(server, "_closed_sessions", lambda seen: [])
    payload = server._push_sessions_payload()
    g2 = next((r for r in payload["sessions"] if r["sid"] == sid), None)
    assert g2 is not None, "SSE 推送载荷缺 ACP 行会几秒内冲掉手机端列表"
    assert g2["source"]["kind"] == "protocol"

    # hist 从仓里回来；给鬼影发消息要显式报「已结束」
    resp = asyncio.run(server.h_ext_live(_get("/api/ext_live/" + sid)))
    assert resp.status == 200
    assert "你好" in json.loads(resp.body)["hist"]
    async def _send_ghost():
        return await server.h_ext_send(_post_json("/api/ext_send",
                                                  {"sid": sid, "text": "还活着吗", "cid": "t-c3"}))
    resp = asyncio.run(_send_ghost())       # _post_json 要在有事件循环的上下文里构造
    assert not json.loads(resp.body).get("ok")
    assert "结束" in json.loads(resp.body).get("error", "")


def test_on_update_scrollback_debounce_and_flush(tmp_path, monkeypatch):
    """(Important #5) 逐事件同步落盘会拖住事件循环：与镜像同款防抖
    （16 行/3s），关/死时兜底冲一次。"""
    monkeypatch.setattr(scrollback, "SCROLLBACK_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(agent_acp.scrollback, "save",
                        lambda sid, rows: calls.append((sid, len(rows))))

    w = agent_acp.AcpWindow("acp-d1")
    for i in range(5):                      # 5 行：不触发
        w._on_update("session/update", {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "行-%02d" % i}}})
    assert calls == [], "不足 16 行不该写盘（防抖）"

    for i in range(5, 16):                  # 攒到 16 行：触发
        w._on_update("session/update", {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "行-%02d" % i}}})
    assert len(calls) == 1 and calls[0][1] == 16, "16 行要触发一次全量落盘"

    w._on_update("session/update", {"update": {          # 尾部 1 行：先攒着
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "尾巴"}}})
    n = len(calls)
    asyncio.run(w.close())                  # 关窗兜底冲盘（close 是 async）
    assert len(calls) == n + 1 and calls[-1][1] == 17
