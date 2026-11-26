# SPDX-License-Identifier: AGPL-3.0-or-later

"""窗口来源抽象（窗口核心 Phase 1）：每个窗口行标明内容从哪条通道来。

pty      终端回显（claude/codex 的 wrapper PTY、Windows ConPTY、host 自管）——
         现有镜像链路，full_color 同属此类。
protocol 结构化通道（Codex 桌面版 /rpc；T6 起的 ACP 适配器）——语义（决策/
         审批事实）直接来自通道本体，不用从 ANSI 里猜。
screen   屏幕源占位：截屏/图像通道，Phase 1 明确不实装。显式标出来，让
         「降级了」看得见（live 不支持、无语义），而不是静默当 pty 处理。

source_of 只认行上既有字段：kind 字段恒为 "computer"（desktop 行也是），
不能拿它区分 protocol/pty；desktop（Codex 桌面版 /rpc）和 acp（T6 ACP 行）
才是协议通道的判别依据。未知来源一律 screen stub，不猜。
"""


class WindowSource:
    kind = "pty"            # "pty" | "protocol" | "screen"
    semantics = True        # 这条通道能不能给出结构化语义（决策/审批事实）

    def live_supported(self):
        return self.kind != "screen"


class ScreenSource(WindowSource):
    """占位：永不实装于本计划。live 不支持是它的本质，不是待修的 bug。"""

    kind = "screen"
    semantics = False

    def live_supported(self):
        return False


def source_of(sid, item):
    """窗口行 → {"kind","sem","stub"}。sid 目前只做日志用途，判别全看行字段。"""
    if item.get("desktop") or item.get("acp"):
        return {"kind": "protocol", "sem": 1, "stub": 0}
    if item.get("kind") == "computer":
        return {"kind": "pty", "sem": 1, "stub": 0}
    return {"kind": "screen", "sem": 0, "stub": 1}
