# SPDX-License-Identifier: AGPL-3.0-or-later

"""notify 事件 → 桌宠事件：定优先级、定角标、合并同一件事。"""

from dataclasses import replace

from .protocol import Badge, PetEvent

PRIORITY = {"perm": 0, "ask": 0, "input": 0,
            "stuck": 1, "limit": 1,
            "done": 2, "progress": 3}
STATUS = {"perm": "waiting", "ask": "waiting", "input": "waiting",
          "stuck": "error", "limit": "error", "done": "done"}
EXPECT = {"perm": "choice", "ask": "choice", "input": "ack", "stuck": "ack"}


def priority_of(kind: str) -> int | None:
    return PRIORITY.get(kind)


def from_notify(ev: dict) -> PetEvent | None:
    kind = ev.get("kind") or ""
    pri = priority_of(kind)
    if pri is None:
        return None
    title = (ev.get("title") or ev.get("about") or "那个项目").strip()[:24]
    text = (ev.get("summary") or ev.get("act") or ev.get("about") or "").strip()[:80]
    st = STATUS.get(kind)
    return PetEvent(ref="n%s" % ev.get("id"), kind=kind, priority=pri,
                    title=title, text=text,
                    badge=Badge(title, st) if st else None,
                    sid=ev.get("sid") or "", expect=EXPECT.get(kind, ""),
                    ts=float(ev.get("ts") or 0),
                    count=max(1, int(ev.get("rep") or 1)))


def coalesce_key(ev: PetEvent) -> tuple[str, str]:
    return (ev.sid, ev.kind)


def merge(a: PetEvent, b: PetEvent) -> PetEvent:
    """同一会话同一类事的多次事件合成一条：保留最早那条的脸，次数相加。"""
    first, second = (a, b) if a.ts <= b.ts else (b, a)
    return replace(first, count=a.count + b.count,
                   badge=first.badge or second.badge,
                   text=first.text or second.text,
                   line=first.line or second.line)
