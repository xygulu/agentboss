# SPDX-License-Identifier: AGPL-3.0-or-later

"""Claude Code 的 hook 安装/卸载/自愈：提醒链路的入口。

搬到单独文件的原因：这段要写用户家目录的 ~/.claude/settings.json —— 公共地盘，
cc-switch 切供应商、Claude Code 自己写 permissions 都会整份重写它，历史上出过
「提醒突然没了、还没报错」。改这段时要连着自愈逻辑一起读，埋进 1.4 万行里没人读。

PORT / TOKEN_FILE 属于 server（入口常量），在用到时延迟取，避免模块级成环。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import agent_codex
import agents

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"


# ---- hooks 安装：把 Claude Code 的四个事件接到本机 /api/session_event ----
HOOK_SH = HERE / "hook.sh"       # 生成物：带本机绝对路径，别提交
HOOK_CMD = HERE / "hook.cmd"     # Windows 版（cmd.exe；Windows 10 自带 curl.exe）
_CLAUDE_SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))
_HOOK_SPEC = [("UserPromptSubmit", None), ("Stop", None), ("StopFailure", None),
              ("PermissionRequest", None), ("Notification", None)]


def _hook_cmd():
    # Windows 统一走 `cmd /c "…\hook.cmd"`：Claude Code 有 Git Bash 时 hook 命令是用
    # bash 跑的，bash 里 `cmd /c "C:\…"` 一样能执行；没 bash 时 cmd 直接执行。
    if IS_WIN:
        return 'cmd /c "%s"' % HOOK_CMD
    return str(HOOK_SH)


def _write_hook_sh():
    """上报脚本：token 运行期从 token.txt 读——不写进脚本、不进命令行/日志。"""
    import server           # 端口和 token 路径是入口常量，调用时才取，免得成环
    PORT, TOKEN_FILE = server.PORT, server.TOKEN_FILE
    if IS_WIN:
        body = (
            "@echo off\r\n"
            "rem Codeboss 事件上报（server.py --hooks install 生成；删掉 = 停用提醒）\r\n"
            "setlocal EnableExtensions\r\n"
            'set "T="\r\n'
            'for /f "usebackq delims=" %%%%i in ("%s") do if not defined T set "T=%%%%i"\r\n'
            'if not defined T set "T=%%CODEBOSS_TOKEN%%"\r\n'
            "if not defined T exit /b 0\r\n"
            'curl.exe -s -m 2 -o NUL -X POST "http://127.0.0.1:%d/api/session_event" '
            '-H "Cookie: codeboss_t=%%T%%" -H "Content-Type: application/json" --data-binary @-\r\n'
            "exit /b 0\r\n" % (TOKEN_FILE, PORT))
        HOOK_CMD.write_text(body, encoding="utf-8", newline="")
        return
    body = (
        "#!/bin/sh\n"
        "# Codeboss 事件上报（server.py --hooks install 生成；删掉 = 停用提醒）\n"
        "# 两种调法：Claude 的 hook（JSON 从 stdin 来）；Codex 的 notify\n"
        "# （$1=codex，JSON 在 $2——Codex 把 payload 追加成最后一个 argv）。\n"
        "# notify 链可能绕回本脚本（别的工具把自己的 previous notify 指回我们，\n"
        "# 比如 Sky Computer Use 的 turn-ended）：转发时带 NEST=1，绕回来的那次直接退，\n"
        "# 否则一次完成会无限转圈、每 10 秒重复播报。\n"
        "if [ \"${CODEBOSS_HOOK_NEST:-}\" = \"1\" ]; then exit 0; fi\n"
        "URL=\"http://127.0.0.1:%d/api/session_event\"\n"
        "CHAIN=\"%s\"\n"
        "if [ \"$1\" = \"hermes\" ]; then\n"
        "  # hermes 的 shell hook（config.yaml hooks:）JSON 从 stdin 来，原样转发；\n"
        "  # 翻译成 Stop 在服务端（agent_hermes.notify_event）做。\n"
        "  T=$(head -n1 \"%s\" 2>/dev/null)\n"
        "  [ -n \"$T\" ] || T=\"${CODEBOSS_TOKEN:-}\"\n"
        "  [ -n \"$T\" ] || exit 0\n"
        "  curl -s -m 2 -o /dev/null -X POST \"$URL\" \\\n"
        "    -H \"Cookie: codeboss_t=$T\" -H \"Content-Type: application/json\" --data-binary @-\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"codex\" ]; then\n"
        "  T=$(head -n1 \"%s\" 2>/dev/null)\n"
        "  [ -n \"$T\" ] || T=\"${CODEBOSS_TOKEN:-}\"\n"
        "  if [ -n \"$T\" ] && [ -n \"${2:-}\" ]; then\n"
        "    printf '%%s' \"$2\" | curl -s -m 2 -o /dev/null -X POST \"$URL\" \\\n"
        "      -H \"Cookie: codeboss_t=$T\" -H \"Content-Type: application/json\" --data-binary @-\n"
        "  fi\n"
        "  # 装 CodeBoss 之前你自己的 notify（装的时候存下来的），照旧转给它\n"
        "  if [ -x \"$CHAIN\" ]; then CODEBOSS_HOOK_NEST=1 \"$CHAIN\" \"${2:-}\" >/dev/null 2>&1; fi\n"
        "  exit 0\n"
        "fi\n"
        "T=$(head -n1 \"%s\" 2>/dev/null)\n"
        "[ -n \"$T\" ] || T=\"${CODEBOSS_TOKEN:-}\"\n"
        "[ -n \"$T\" ] || exit 0\n"
        "curl -s -m 2 -o /dev/null -X POST \"$URL\" \\\n"
        "  -H \"Cookie: codeboss_t=$T\" -H \"Content-Type: application/json\" --data-binary @-\n"
        "exit 0\n" % (PORT, str(agent_codex.notify_chain_path()), TOKEN_FILE,
                      TOKEN_FILE, TOKEN_FILE))
    # 内容没变就不重写：hermes hooks doctor 查「审批后脚本被改过」（mtime 漂移），
    # 自愈守卫每次重写都会把用户的同意作废掉。
    try:
        if HOOK_SH.exists() and HOOK_SH.read_text(encoding="utf-8") == body:
            return
    except OSError:
        pass
    HOOK_SH.write_text(body, encoding="utf-8")
    HOOK_SH.chmod(0o700)


def _load_claude_settings():
    try:
        return json.loads(_CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("读不了 %s：%s" % (_CLAUDE_SETTINGS, e))
        return None


def _save_claude_settings(cfg):
    _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if _CLAUDE_SETTINGS.exists():
        try:
            (_CLAUDE_SETTINGS.parent / "settings.json.codeboss-bak").write_text(
                _CLAUDE_SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    tmp = _CLAUDE_SETTINGS.parent / "settings.json.cbtmp"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_CLAUDE_SETTINGS)


def _is_cb_group(g):
    return any((h.get("command") or "") == _hook_cmd() or "api/session_event" in (h.get("command") or "")
               for h in (g.get("hooks") or []) if isinstance(h, dict))


def _hooks_mutate(on):
    """Claude 的 hook 装/卸（写 ~/.claude/settings.json）。"""
    cfg = _load_claude_settings()
    if cfg is None:
        return 1
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):     # 有人把 hooks 写成别的类型：别硬改
        print("settings.json 里的 hooks 不是对象，先手动看一眼：%s" % _CLAUDE_SETTINGS)
        return 1
    if on:
        _write_hook_sh()
    for ev, matcher in _HOOK_SPEC:
        arr = hooks.get(ev)
        arr = arr if isinstance(arr, list) else []
        arr = [g for g in arr if not (isinstance(g, dict) and _is_cb_group(g))]
        if on:
            g = {"hooks": [{"type": "command", "command": _hook_cmd(),
                            "async": True, "timeout": 5}]}
            if matcher:
                g["matcher"] = matcher
            arr.append(g)
        if arr:
            hooks[ev] = arr
        else:
            hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks", None)
    _save_claude_settings(cfg)
    return 0


HOOKS_OFF = HERE / "logs" / ".hooks-off"


def _hooks_installed():
    """Claude 的五个事件是不是都还挂着我们的上报脚本。"""
    cfg = _load_claude_settings()
    if cfg is None:
        return True          # 读不了就别乱动
    hooks = cfg.get("hooks") or {}
    if not isinstance(hooks, dict):
        return True
    for ev, _m in _HOOK_SPEC:
        if not any(isinstance(g, dict) and _is_cb_group(g) for g in (hooks.get(ev) or [])):
            return False
    return True


def _hooks_mutate_all(on):
    """给所有 agent 装/卸 hook。返回 0 全成功。"""
    rc = 0
    for ad in agents.adapters():
        try:
            if ad.hook_install(on) != 0:
                rc = 1
        except Exception as e:
            print("[hooks] %s 装卸失败: %s" % (ad.key, str(e)[:120]), flush=True)
            rc = 1
    return rc


def _hooks_installed_all():
    """所有 agent 的 hook 是不是都还挂着。"""
    for ad in agents.adapters():
        try:
            if not ad.hooks_installed():
                return False
        except Exception:
            return False
    return True


def _hooks_guard():
    """提醒的自愈：发现 hooks 被别人抹掉了就重新接上。

    ⚠️ 提醒全靠 ~/.claude/settings.json 里的 hooks 往 /api/session_event 上报。
    而那个文件是公共地盘：cc-switch 切供应商、Claude Code 自己写 permissions/model
    都会整份重写它 —— 实测 2026-11-06 07:34 一次模型切换之后 hooks 就没了，
    表现就是「突然没有提醒了」，而且没有任何报错，用户只会觉得功能坏了。

    所以：启动查一次、之后每 5 分钟查一次，缺了就照原样接回去。
    用户用 `--hooks remove` 明确关掉过的话，留一个标记文件，这里就不碰。
    """
    try:
        if HOOKS_OFF.exists():
            return False
        if _hooks_installed_all():
            return False
        if _hooks_mutate_all(True) == 0:
            print("[hooks] 提醒事件被外部改写掉了，已自动接回（新开的会话生效）", flush=True)
            return True
    except Exception as e:
        print("[hooks] 自愈失败: %s" % str(e)[:120], flush=True)
    return False




async def _hooks_guard_loop(app):
    async def _run():
        await asyncio.sleep(20)
        while True:
            await asyncio.get_event_loop().run_in_executor(None, _hooks_guard)
            await asyncio.sleep(300)
    app["hooks_guard_task"] = asyncio.create_task(_run())


def _hooks_cli(act):
    if act in ("install", "remove"):
        if act == "install":
            try:
                HOOKS_OFF.unlink()      # 显式装回来 = 取消「明确关掉」的标记
            except OSError:
                pass
        else:
            try:
                HOOKS_OFF.parent.mkdir(parents=True, exist_ok=True)
                HOOKS_OFF.write_text("1")
            except OSError:
                pass
        if _hooks_mutate_all(act == "install") != 0:
            return 1
        if act == "install":
            for ad in agents.adapters():
                spec = ad.hook_spec()
                if not spec:
                    print("%s 这台平台不接提醒（跳过）" % ad.label)
                    continue
                print("%s 已接事件提醒：%s"
                      % (ad.label, " / ".join(ev + ((":" + m) if m else "") for ev, m in spec)))
            print("脚本 %s（token 运行期从 token.txt 读）" % (HOOK_CMD if IS_WIN else HOOK_SH))
            print("新开的会话生效；卸载用 --hooks remove")
        else:
            print("已卸载事件提醒（其它 hooks 未动）")
        return 0
    print("脚本：%s（%s）" % (HOOK_SH, "存在" if HOOK_SH.exists() else "缺失"))
    for ad in agents.adapters():
        have, path = ad.hook_status()
        print("%s 已接事件：%s" % (ad.label, ", ".join(have) if have else "无"))
        if path:
            print("%s 设置文件：%s" % (ad.label, path))
    return 0
