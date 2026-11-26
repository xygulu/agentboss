# SPDX-License-Identifier: AGPL-3.0-or-later

"""产品线上报与能力面过滤（设计文档 D5/C2：缺省 codeboss，agentboss 全量）。

跑法： .venv/bin/python -m pytest tests/test_product_gate.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agents
import server


def _with_ad(key):
    """注册一个 fake adapter，返回还原函数。"""
    saved = dict(agents._ADAPTERS)

    class _Fake(agents.AgentAdapter):
        def installed(self):
            return True

    _Fake.key = key
    _Fake.label = key.capitalize()
    agents.register(_Fake())
    return lambda: agents._ADAPTERS.clear() or agents._ADAPTERS.update(saved)


# ---- _client_product ----

def test_client_product_header_wins():
    assert server._client_product({"X-Product": "agentboss"}, {}) == "agentboss"


def test_client_product_cfg_fallback():
    assert server._client_product({}, {"dev_client_product": "agentboss"}) == "agentboss"


def test_client_product_default(monkeypatch):
    # 缺省链最后一跳跟随 host 产品线；显式隔离本机 config（开发机 config.json 有 product=agentboss）
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert server._client_product({}, {}) == "codeboss"


def test_client_product_invalid_header_falls_back():
    assert server._client_product({"X-Product": "nonsense"}, {}) == "codeboss"


# ---- 批次一：壳不发头，改种 cookie cb_product（头 > cookie > dev 开关 > host 线）----

def test_client_product_cookie_cb_product():
    assert server._client_product({}, {}, cookies={"cb_product": "agentboss"}) == "agentboss"


def test_client_product_header_beats_cookie():
    assert server._client_product({"X-Product": "codeboss"}, {},
                                  cookies={"cb_product": "agentboss"}) == "codeboss"


def test_client_product_bad_cookie_ignored():
    assert server._client_product({}, {}, cookies={"cb_product": "junk"}) == "codeboss"


def test_client_product_for_request(monkeypatch):
    # _client_product_for(request)：headers + cookies 合并入口，调用点机械替换的靶子
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    class _Req:
        headers = {"X-Product": "codeboss"}
        cookies = {"cb_product": "agentboss"}

    assert server._client_product_for(_Req()) == "codeboss"


# ---- I-6：壳不发 X-Product 时跟随 host 登记的产品线 ----
# （agentboss 手机没有 X-Product 发送方，缺省 codeboss 会把 opencode/hermes 全滤掉）

def test_client_product_falls_back_to_host_line(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"product": "agentboss"})
    assert server._client_product({}, {}) == "agentboss"


def test_client_product_codeboss_host_default(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert server._client_product({}, {}) == "codeboss"


# ---- 批次一 Task 3：cookie 产品线端到端（face 过滤 codeboss 只见 claude/codex）----

def _fake_request(cookies):
    import types
    return types.SimpleNamespace(headers={}, cookies=cookies)


def test_cookie_codeboss_face_filters_hermes():
    req = _fake_request({"cb_product": "codeboss"})
    face = server._agent_face(server._client_product_for(req))
    sessions = [{"sid": "1", "agent": "claude"}, {"sid": "2", "agent": "hermes"},
                {"sid": "3", "agent": "opencode"}]
    out = server._face_only(sessions, face)
    assert {s["agent"] for s in out} == {"claude", "opencode"}, "codeboss 见 coding 面：hermes 不在内"


def test_cookie_agentboss_face_sees_all():
    req = _fake_request({"cb_product": "agentboss"})
    face = server._agent_face(server._client_product_for(req))
    assert face is None


# ---- _agent_face ----

def test_face_codeboss_is_coding_only():
    # codeboss = claude/codex/opencode（2026-11-18 产品线口径），hermes 只属于 agentboss
    assert server._agent_face("codeboss") == frozenset({"claude", "codex", "opencode"})


def test_face_agentboss_sees_all():
    assert server._agent_face("agentboss") is None


# ---- _host_product ----

def test_host_product_default(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert server._host_product() == "codeboss"


def test_host_product_from_cfg(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"product": "agentboss"})
    assert server._host_product() == "agentboss"


def test_host_product_invalid_cfg(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"product": "xxx"})
    assert server._host_product() == "codeboss"


# ---- _agents_installed 过滤 ----

def test_agents_installed_filtered():
    restore = _with_ad("opencode")
    try:
        keys_full = {a["key"] for a in server._agents_installed(None)
                     if a["key"] == "opencode"}
        keys_code = {a["key"] for a in server._agents_installed("codeboss")
                     if a["key"] == "opencode"}
        assert "opencode" in keys_full
        assert "opencode" in keys_code, "codeboss 也支持 opencode"
    finally:
        restore()


# ---- 首屏 URL 的 ?product=（2026-11-20：两个 App 打开一个样）----
# 壳种 cookie 和 WebView 发第一个请求之间有个窗口：装机第一次打开 / 刚配对换地址
# 时首屏请求还没有 cb_product，host 只能按自己的线渲染标题/页头图标。壳现在在首屏
# URL 上带 product=，服务端必须认它（页面渲染和 API 过滤都走 _client_product_for）。

def test_client_product_for_query_param(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    class _Req:
        headers = {}
        cookies = {}
        query = {"product": "agentboss"}

    assert server._client_product_for(_Req()) == "agentboss"


def test_client_product_for_query_beats_cookie(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    class _Req:
        headers = {}
        cookies = {"cb_product": "agentboss"}
        query = {"product": "codeboss"}

    assert server._client_product_for(_Req()) == "codeboss"


def test_client_product_for_bad_query_ignored(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    class _Req:
        headers = {}
        cookies = {"cb_product": "agentboss"}
        query = {"product": "nonsense"}

    assert server._client_product_for(_Req()) == "agentboss"
