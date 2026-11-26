# SPDX-License-Identifier: AGPL-3.0-or-later

"""结果核验自检：从转录证据判断「完成 / 失败 / 未核实」。

跑法：
    .venv/bin/python -m pytest tools/test_outcome.py -q
    .venv/bin/python tools/test_outcome.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import outcome   # noqa: E402


def prompt(text):
    return {"type": "user", "message": {"content": text}}


def use(uid, name, **inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name,
                                     "input": inp}]}}


def result(uid, text, err=None):
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": uid,
                                     "content": text, "is_error": err}]}}


def text_reply(s):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": s}]}}


def classify(rows):
    turns = outcome.split_turns(rows)
    return outcome.classify(outcome.turn_evidence(turns[-1]))


def test_tests_pass_is_done_verified():
    r = classify([prompt("修一下登录"),
                  use("a", "Bash", command="pytest -q"),
                  result("a", "15 passed in 1.2s"),
                  text_reply("修好了")])
    assert r["state"] == "done_verified"
    assert "15/15" in r["label"]


def test_tests_fail_is_failed():
    r = classify([prompt("跑测试"),
                  use("a", "Bash", command="npm test"),
                  result("a", "Tests: 2 failed, 10 passed")])
    assert r["state"] == "failed"
    assert "2" in r["label"]


def test_verifier_exit_code_is_failed():
    r = classify([prompt("构建"),
                  use("a", "Bash", command="npm run build"),
                  result("a", "Exit code 1\nCannot find module 'x'", err=True)])
    assert r["state"] == "failed"
    assert "npm run build" in r["label"]


def test_permission_denied_is_blocked_not_failed():
    r = classify([prompt("删掉它"),
                  use("a", "Bash", command="rm -rf data"),
                  result("a", "Permission for this action was denied by the user", err=True)])
    assert r["state"] == "blocked"


def test_edit_without_verifier_is_not_called_done():
    r = classify([prompt("改下标题"),
                  use("a", "Edit", file_path="/p/a.html"),
                  result("a", "ok"),
                  text_reply("改好了")])
    assert r["state"] == "done_unverified"
    assert "1 个文件" in r["label"]


def test_chat_only_turn():
    r = classify([prompt("只回复两个字：收到"), text_reply("收到")])
    assert r["state"] == "done_unverified"
    assert "没有工具操作" in r["label"]


def test_error_then_fix_then_pass_counts_as_done():
    """中途报错后修好并跑过测试：不能因为前面失败就判失败。"""
    r = classify([prompt("修"),
                  use("a", "Bash", command="pytest"),
                  result("a", "1 failed, 3 passed"),
                  use("b", "Edit", file_path="/p/x.py"),
                  result("b", "ok"),
                  use("c", "Bash", command="pytest"),
                  result("c", "4 passed in 0.3s")])
    assert r["state"] == "done_verified"


def test_non_verifier_error_is_not_a_task_failure():
    """grep 没匹配到东西这种非零退出，不能说任务失败。"""
    r = classify([prompt("找找"),
                  use("a", "Bash", command="grep -r nothing ."),
                  result("a", "Exit code 1", err=True),
                  text_reply("没找到")])
    assert r["state"] == "done_unverified"


def test_mentioning_build_sh_is_not_running_it():
    """`grep "build.sh"` 只是提到了验证脚本，不能当作验证跑过了。"""
    r = classify([prompt("看看脚本"),
                  use("a", "Bash", command='grep -n "notarytool" mac/build-app.sh | head'),
                  result("a", "12: notarytool submit"),
                  text_reply("在 12 行")])
    assert r["state"] == "done_unverified"


def test_background_launch_is_not_done():
    """扔进后台的命令没有结果，不能算完成。"""
    r = classify([prompt("构建"),
                  use("a", "Bash", command="./build.sh ipa"),
                  result("a", "Command running in background with ID: b123. "
                              "Output is being written to: /tmp/b123.output")])
    assert r["state"] == "done_unverified"


def test_pipeline_swallowed_build_failure_is_failed():
    """`build.sh | tail` 的退出码是 tail 的，失败只能从输出文字看出来。"""
    r = classify([prompt("打包"),
                  use("a", "Bash", command="./build.sh tf --upload 2>&1 | tail -8"),
                  result("a", "** BUILD FAILED **\nThe following build commands failed:\n"
                              "  SwiftCompile normal arm64")])
    assert r["state"] == "failed"
    assert "测试" not in r["label"]          # 构建失败别冒充「测试没通过」


def test_infra_error_is_not_task_failure():
    """auto mode 判定超时＝工具根本没跑，不是任务失败。"""
    r = classify([prompt("发布"),
                  use("a", "Bash", command="./build.sh tf"),
                  result("a", "auto mode cannot determine the safety of Bash right now.",
                         err=True)])
    assert r["state"] == "done_unverified"


def test_notification_prefix():
    """提醒文字：有证据就标 ✅/❌/⛔，未核实保持原话。"""
    import server
    assert server._oc_text({"state": "done_verified", "label": "完成 · 测试 12/12 通过"},
                           "修好了").startswith("✅ 完成 · 测试 12/12 通过")
    assert server._oc_text({"state": "failed", "label": "失败 · pytest 退出码 1"},
                           "搞定了").startswith("❌ 失败")
    assert server._oc_text({"state": "done_unverified", "label": "本轮结束 · 改了 2 个文件"},
                           "搞定了") == "搞定了"


def test_voice_says_same_as_inbox_card():
    """语音播报念的是收件箱那张卡上的同一件事：动作：对象 + 高风险提示。"""
    import server
    assert server._say_decision(
        "要不要发版？",
        {"tool": "Bash", "action": "运行命令", "target": "rm -rf build/"},
        [{"level": "high", "text": "删除目录，不可撤销"}],
    ) == "要确认：运行命令：rm -rf build/。删除目录，不可撤销"
    # 没有结构化输入（跑完在问你）：念 card 上那句 act
    assert server._say_decision("概括", None, None, "要不要发布这次改动？") == "要不要发布这次改动？"
    assert server._say_decision("要不要发布？", None, None) == "要不要发布？"
    # 低风险不念风险，只有动作
    assert server._say_decision("概括", {"action": "修改文件", "target": ""},
                                [{"level": "low", "text": "小改动"}]) == "要确认：修改文件"


def test_ask_question_is_a_choice_not_a_reply():
    """「选一项」和「批准/拒绝」是两回事：AskUserQuestion 要带出问句和选项。"""
    import server
    ti = {"questions": [{"question": "用哪个部署方式？",
                         "options": [{"label": "蓝绿"}, {"label": " 直接覆盖 "}]}]}
    assert server._ti_target("AskUserQuestion", ti) == "用哪个部署方式？"
    assert server._ask_opts(ti) == ["蓝绿", "直接覆盖"]
    assert server._ask_opts({"questions": []}) == []


def test_act_conflict_sees_through_yijiang_phrasing():
    """概括说「已将查看器发布为网页」，就别再问「要不要发布这次改动？」（2026-11-14 真事）。"""
    import server
    assert server._act_conflict("已将查看器发布为网页，可访问链接在线查看", "要不要发布这次改动？")
    assert server._act_conflict("已成功推送到远端", "要不要推送？")
    assert server._act_conflict("已把改动发布到 GitHub", "要不要发布？")
    # 还没做、只是可以做的，不能误判成做完
    assert not server._act_conflict("已确认无误，可以发布", "要不要发布这次改动？")


def test_act_must_be_quoted_from_the_reply():
    """act 得是这轮回复里的原话：模型自己造的「要不要再发一次？」不认（2026-11-14 esp32c3）。"""
    import server
    reply = "已将查看器发布为网页。\n- 修改模型后运行 `./model/build-site.sh` 即可更新。需要自由看模型时再添加。"
    assert not server._act_grounded("要不要发布这次改动？", reply)
    assert server._act_grounded("已将查看器发布为网页", reply)
    # 空白/反引号不一样不影响（摘抄时常丢格式）
    assert server._act_grounded("修改模型后运行 ./model/build-site.sh 即可更新", reply)
    assert not server._act_grounded("", reply)
    assert not server._act_grounded("要不要发布？", "")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL OK")
