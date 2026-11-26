# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠的慢脑子：host 自己开一个 codex 会话，慢慢把老板摸熟。

快脑子是中继那条 LLM（一两秒就得开口）；这里是聊完一轮后异步跑的慢活：把
「旧笔记 + 最近几句」交给 host 自己的 agent 会话，产出一版新的长期笔记和老板
新提的要求，写回 pet.json。

为什么是**会话**而不是每轮一次性请求：同一个线程一路 resume 下去，它自己攒着
上下文，几周后她对老板的理解是那一条线看出来的；每轮从零猜的笔记合起来永远是
今天这点事。

会话 id 存 pet.json 的 brain 里，同时登记进 owned_sessions：手机上删不掉
（删了她就失忆）。老板在电脑上把它删了，下一轮发现记录没了就重建、重新登记。

跑不起来（没装 codex / 密钥不通 / 超时）就返回 False，调用方退回中继那条快路径。
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

SCHEMA = {
    "type": "object",
    "properties": {
        "memo": {"type": "string"},
        "prefs": {"type": "array", "items": {"type": "string"}},
        "talk": {"type": "string", "enum": ["", "brief", "normal"]},
    },
    "required": ["memo", "prefs"],
    "additionalProperties": False,
}

_LAST = 0.0          # 上一次真跑了的时间：聊得太密就跳几轮，凑下次一起并
_MIN_GAP = 45.0      # 秒
_LOCK = None


def scratch() -> Path:
    p = Path(os.environ.get("CODEBOSS_PET_BRAIN")
             or (Path.home() / ".codeboss" / "pet_brain"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _codex() -> str:
    try:
        import agent_codex
        return agent_codex.codex_bin() or ""
    except Exception:
        return ""


def _top(text: str, key: str) -> str:
    m = re.search(r'(?m)^\s*%s\s*=\s*"([^"]*)"' % re.escape(key), text)
    return m.group(1) if m else ""


def _provider() -> dict:
    """codex 现在用哪家、钥匙叫什么、去哪拿。

    钥匙从 cc-switch 里同一条 base_url 的配置拿，只塞进这次子进程的 env：
    不落盘、不进日志。拿不到就把 env_key 留着，让 codex 自己去环境里找。"""
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return {}
    prov = _top(text, "model_provider")
    model = _top(text, "model")
    blk = ""
    if prov:
        m = re.search(r'(?m)^\s*\[model_providers\.%s\]\s*$(.*?)(?=^\s*\[|\Z)'
                      % re.escape(prov), text, re.S)
        blk = m.group(1) if m else ""
    return {"provider": prov, "model": model, "env_key": _top(blk, "env_key"),
            "base_url": _top(blk, "base_url")}


def _has_base(cfg: str, base_url: str) -> bool:
    """这份配置里有没有哪家 provider 块正好是这条 base_url。

    只按「块里的 base_url 相等」认：配置全文里别的块/注释也会提到这条地址
    （cc-switch 存的是一整份 config），整段字符串比对会拿错人家的钥匙——
    2026-11-21 实测拿 DeepSeek 的钥匙去连 MiniMax，清一色 401。"""
    for m in re.finditer(r'(?m)^\s*\[model_providers\.[^\]]+\]\s*$(.*?)(?=^\s*\[|\Z)',
                         cfg or "", re.S):
        if _top(m.group(1), "base_url") == base_url:
            return True
    return False


def _secret(base_url: str, prov_id: str = "") -> str:
    """cc-switch 里配到这条 base_url 的 codex 密钥（没有就空串）。

    cc-switch 每条存的是**一整份** config（同一家会带上别家的 provider 块），
    所以先认「顶层 model_provider 就是这家、且 base_url 对得上」的那条，
    都不对再退回只比 base_url。"""
    if not base_url:
        return ""
    import sqlite3
    db = Path.home() / ".cc-switch" / "cc-switch.db"
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
        rows = con.execute("SELECT settings_config FROM providers"
                           " WHERE app_type='codex'").fetchall()
        con.close()
    except Exception:
        return ""
    loose = ""
    for (sc,) in rows:
        try:
            d = json.loads(sc) or {}
        except ValueError:
            continue
        cfg = d.get("config") or ""
        if not _has_base(cfg, base_url):
            continue
        key = (d.get("auth") or {}).get("OPENAI_API_KEY") or ""
        if not key:
            continue
        if prov_id and _top(cfg, "model_provider") == prov_id:
            return key
        loose = loose or key
    return loose


def env(prov=None) -> dict:
    p = _provider() if prov is None else prov
    e = dict(os.environ)
    k = p.get("env_key") or ""
    if k and not e.get(k):
        s = _secret(p.get("base_url") or "", p.get("provider") or "")
        if s:
            e[k] = s
    return e


def prompt(old_memo: str, prefs, hist, user) -> str:
    """给慢脑子的一轮活。只喂老板自己说过的话。

    她的回话一个字都不进：陪聊时她自己会顺口编细节（老板只说「那咋玩？」，
    她回「绿豆汤快好了吧」），11-21 半夜这些编出来的东西全被当事实记进了
    长期笔记——老板压根没喝绿豆汤。语音转写的半截话（「对啊」「一个」）
    听不出是谁是什么，也不许当成事实。
    """
    lines = ["你在给一块桌上的小工头维护长期记忆（她叫桌宠，陪老板说话）。"]
    lines.append("旧笔记：%s" % (old_memo or "（空）"))
    if prefs:
        lines.append("已经记下的要求：%s" % "；".join(prefs[-8:]))
    said = [m for m in (hist or []) if m.get("role") == "user"]
    if said:
        lines.append("老板最近说过（语音转写，半截话很多，看不懂就别记）：")
        for m in said[-6:]:
            lines.append("  %s" % str(m.get("text") or "")[:80])
    lines += [
        "老板刚说：%s" % str(user or "")[:200],
        "",
        "只写老板本人亲口说过、且听得懂的事。看不出是谁/是什么的半截话不记；"
        "问句不是要求（问「你接了 Codex 吗」不等于要接 Codex）；"
        "老板这轮否定了旧笔记里的哪条，就从笔记里删掉。"
        "更新笔记：记老板是谁、做过什么、喜好脾气、反复出现的项目和身边的人；"
        "一次性的闲聊不记。笔记用第三人称、分号隔开、不超过 300 字。",
        "prefs 只放老板这轮**新**提的、还没记下的要求（说法/语气/称呼这类），"
        "一句一条、60 字以内；没有就空数组。",
        "talk：老板刚嫌你话多就填 brief，让你多讲就填 normal，没提就空串。",
        "只输出 JSON。",
    ]
    return "\n".join(lines)


def _claude() -> str:
    """没装 codex 的机器用 Claude Code 顶：host 本来就同时接这两家。"""
    import shutil
    for p in (shutil.which("claude"),
              str(Path.home() / ".npm-global/bin/claude"),
              str(Path.home() / ".local/bin/claude"),
              "/opt/homebrew/bin/claude"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def _engine():
    """慢脑子用哪家 agent：codex 优先（按会话攒上下文做得顺），没有就用 claude。"""
    b = _codex()
    if b:
        return "codex", b
    b = _claude()
    if b:
        return "claude", b
    return None


def argv(codex: str, sid: str, schema: Path, out: Path, text: str, prov=None):
    p = _provider() if prov is None else prov
    a = [codex, "exec"]
    if sid:
        a += ["resume", sid]
    if p.get("provider"):
        a += ["-c", 'model_provider="%s"' % p["provider"]]
    if p.get("model"):
        a += ["-m", p["model"]]
    a += ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
          "--skip-git-repo-check", "--output-schema", str(schema),
          "-o", str(out), "--json", text]
    return a


def argv_claude(binpath: str, sid: str, schema: Path, text: str):
    """claude -p：-r 续同一条会话，--json-schema 让它直接给结构化结果。"""
    a = [binpath, "-p", text, "--output-format", "json"]
    if sid:
        a += ["-r", sid]
    try:
        a += ["--json-schema", schema.read_text(encoding="utf-8")]
    except OSError:
        pass
    return a


def _claude_result(stdout: str):
    """claude --output-format json 的整块结果 → (物件, 会话 id)。"""
    try:
        d = json.loads((stdout or "").strip())
    except ValueError:
        return None, ""
    if not isinstance(d, dict):
        return None, ""
    got = d.get("structured_output")
    if not isinstance(got, dict):
        got = _obj(d.get("result") or "")
    return got, str(d.get("session_id") or "")


def _last_message(stdout: str) -> str:
    """--json 事件流里最后一条 agent_message 的正文（-o 在 resume 下不一定写）。"""
    last = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        it = e.get("item") or {}
        if e.get("type") == "item.completed" and it.get("type") == "agent_message":
            last = str(it.get("text") or last)
    return last


def _obj(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?|\n?```$", "", t).strip()
    try:
        d = json.loads(t)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _thread_id(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        if '"thread.started"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("thread_id"):
            return str(e["thread_id"])
    return ""


def apply(pet, got, sid="", eng="codex"):
    """把慢脑子产出的东西落到 pet.json；返回有没有真改到东西。"""
    if not isinstance(got, dict):
        return False
    # 模型偶尔把 memo 写成 notes（schema 不是硬约束）：两边都认
    memo = str(got.get("memo") or got.get("notes") or "").strip()[:800]
    prefs = [str(x).strip()[:60] for x in (got.get("prefs") or [])
             if isinstance(x, (str, int, float)) and str(x).strip()]
    talk = str(got.get("talk") or "")
    changed = False
    if memo and memo != pet.memo():
        pet.save_memo(memo)
        changed = True
    for p in prefs:
        if p not in pet.prefs():
            pet.add_pref(p)
            changed = True
    if talk in ("brief", "normal") and talk != pet.talk():
        pet.set_talk(talk)
        changed = True
    if sid:
        import owned_sessions
        cur = pet.store.get("brain") or {}
        if sid != str(cur.get("sid") or "") or eng != str(cur.get("eng") or "codex"):
            if cur.get("sid") and cur["sid"] != sid:
                owned_sessions.drop(cur["sid"])   # 旧会话（多半是电脑上删了）不再护着
            pet.store.set("brain", {"sid": sid, "eng": eng, "at": time.time()})
            pet.save()
            changed = True
        owned_sessions.add(sid, "桌宠的慢脑子")
    return changed


async def _run(argv_, cwd, env_, timeout):
    """跑一次 agent；超时/起不来返回 (stdout, None)。"""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv_, cwd=cwd, env=env_, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return (raw or b"").decode("utf-8", "replace"), proc.returncode
    except (asyncio.TimeoutError, OSError):
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return "", None


async def learn(pet, hist, user, reply, timeout=150.0) -> bool:
    """聊完一轮：让 host 的会话把这一轮并进长期记忆。成功 True（不必再走中继）。"""
    global _LAST, _LOCK
    now = time.time()
    if now - _LAST < _MIN_GAP:
        return False               # 说太密，留给中继快路径；下次一起并
    eng = _engine()
    if not eng:
        return False
    kind, binpath = eng
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    if _LOCK.locked():
        return False
    async with _LOCK:
        d = scratch()
        schema, out = d / "schema.json", d / "out.json"
        try:
            schema.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
        except OSError:
            return False
        try:
            out.unlink()
        except OSError:
            pass
        brain = pet.store.get("brain") or {}
        sid = str(brain.get("sid") or "") if brain.get("eng", "codex") == kind else ""
        if sid and kind == "codex":
            try:
                import agent_codex
                if not agent_codex.rollout_path(sid):
                    sid = ""          # 电脑上删了：重建一条
            except Exception:
                pass
        text = prompt(pet.memo(), pet.prefs(), hist, user)
        if kind == "codex":
            a = argv(binpath, sid, schema, out, text)
        else:
            a = argv_claude(binpath, sid, schema, text)
        try:
            stdout, rc = await _run(a, str(d), env(), timeout)
        finally:
            _LAST = time.time()
        if rc is None:
            return False
        if kind == "codex":
            got = _obj(stdout) or _obj(_last_message(stdout))
            if got is None:
                try:
                    got = _obj(out.read_text(encoding="utf-8"))
                except OSError:
                    got = None
            tid = _thread_id(stdout) or sid
        else:
            got, tid = _claude_result(stdout)
            tid = tid or sid
        ok = apply(pet, got, tid, kind)
        print("[pet-brain] rc=%s sid=%s ok=%s"
              % (rc, (tid or "")[:8], ok), flush=True)
        return got is not None
