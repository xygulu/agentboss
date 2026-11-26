# SPDX-License-Identifier: AGPL-3.0-or-later

"""host 自己给自己起的会话：手机上不许删。

桌宠的慢脑子、以后别的后台活，都是 host 拿 agent 开的一个会话——删了她就失忆，
下次又得从零重新摸老板。所以这些会话登记在此，删除接口见到就拒绝：
要删请在电脑上自己删（codex 里 / 终端里），host 下一轮发现记录没了会重建并重新登记。
"""

import json
import os
import time
from pathlib import Path

PATH = Path.home() / ".codeboss" / "owned_sessions.json"


def _load() -> dict:
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_name(PATH.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)


def ids() -> set:
    return set(_load())


def has(sid) -> bool:
    return bool(sid) and str(sid) in _load()


def add(sid, why="") -> bool:
    sid = str(sid or "").strip()
    if not sid:
        return False
    d = _load()
    if sid in d and d[sid].get("why") == why:
        return False
    d[sid] = {"why": why, "at": time.time()}
    _save(d)
    return True


def drop(sid):
    d = _load()
    if str(sid or "") in d:
        del d[str(sid)]
        _save(d)


def why(sid) -> str:
    return str((_load().get(str(sid or "")) or {}).get("why") or "")
