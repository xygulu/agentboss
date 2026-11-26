# SPDX-License-Identifier: AGPL-3.0-or-later

"""本机桌面窗口（BossHost）要看全量 agent，手机端仍按产品线能力面。

2026-11-22 用户：BossHost 窗口里没有 hermes。原因不是没发现——`_agent_face`
按产品线过滤，codeboss 只留 claude/codex/opencode。但桌面窗口不种 cb_product、
也不带 X-Product，跟手机走同一个落点，被误当成 codeboss 客户端。
约束：app 端会话类型的显示不许变——codeboss 不显示 hermes，agentboss 才全量。

判据：127.0.0.1 进来、没声明产品线、也不是中继隧道 / P2P 转进来的 = 桌面窗口。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server


class Req:
    def __init__(self, remote="127.0.0.1", headers=None, cookies=None, query=None):
        self.remote = remote
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query = query or {}


def test_desktop_window_is_full_face():
    r = Req()                                   # 菜单栏 App：127.0.0.1 + ?t=，没有产品线提示
    assert server._desktop_window(r) is True
    assert server._face_for(r) is None, "桌面窗口看全量（hermes 也要能管）"
    assert server._face_product(r) is None


def test_relay_tunneled_phone_keeps_codeboss_face():
    """中继转发的请求也从 127.0.0.1 进来（cookie 被中继剥掉），
    靠 _relay_serve 注入的 X-CB-Via 认出来——否则 codeboss 手机会冒出 hermes。"""
    r = Req(headers={"X-CB-Via": "relay"})
    assert server._desktop_window(r) is False
    assert server._face_for(r) == server._CODING_FACE


def test_p2p_direct_keeps_face():
    """P2P 直连同样从 127.0.0.1 进来（cookie 被剥掉），带 dc=1。"""
    r = Req(query={"dc": "1"})
    assert server._desktop_window(r) is False
    assert server._face_for(r) == server._CODING_FACE


def test_explicit_product_on_loopback_wins():
    r = Req(query={"product": "codeboss"})
    assert server._desktop_window(r) is False, "显式声明了产品线就按产品线"
    assert server._face_for(r) == server._CODING_FACE
    r2 = Req(headers={"X-Product": "codeboss"})
    assert server._face_for(r2) == server._CODING_FACE
    r3 = Req(cookies={"cb_product": "codeboss"})
    assert server._face_for(r3) == server._CODING_FACE


def test_lan_phone_is_not_desktop():
    r = Req(remote="192.168.1.9")
    assert server._desktop_window(r) is False


def test_agentboss_still_sees_everything():
    for r in (Req(headers={"X-Product": "agentboss"}),
              Req(cookies={"cb_product": "agentboss"})):
        assert server._face_for(r) is None


def test_face_only_keeps_hermes_for_desktop():
    rows = [{"sid": "c1", "agent": "claude"}, {"sid": "h1", "agent": "hermes"}]
    desktop = server._face_only(rows, server._face_for(Req()))
    assert [x["sid"] for x in desktop] == ["c1", "h1"]
    phone = server._face_only(rows, server._face_for(
        Req(headers={"X-CB-Via": "relay", "X-Product": "codeboss"})))
    assert [x["sid"] for x in phone] == ["c1"]


def test_relay_serve_marks_tunneled_requests():
    """两条隧道入口（普通 / 流式）都要打 X-CB-Via，少一条就漏一类请求。"""
    src = (HERE / "host" / "server.py").read_text(encoding="utf-8")
    m = src.split("async def _relay_serve(", 1)[1]
    assert '"X-CB-Via"' in m, "中继隧道转进来的请求要带内部标记"
    s = src.split("async def _relay_serve_stream(", 1)[1].split("async def _relay_serve(", 1)[0]
    assert '"X-CB-Via"' in s, "流式隧道也要带标记"


def test_face_helpers_wired_in_handlers():
    """会话列表 / agent 列表 / 台账 / 待决定 都走 _face_for，别残留旧口径。"""
    src = (HERE / "host" / "server.py").read_text(encoding="utf-8")
    assert src.count("_agent_face(_client_product_for(request))") == 1, \
        "只剩 _face_for 自己那一处；别处按旧口径过滤会把桌面窗口一起挡掉"
    assert "_agents_installed(_face_product(request))" in src
