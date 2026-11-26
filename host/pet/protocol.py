# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠线协议：Host 与板子/模拟器之间的事件结构。

wire 里只有能上屏、能念的字，cwd/sid 的敏感部分一律不带。
"""

from dataclasses import dataclass

PROTOCOL = 1


@dataclass
class Badge:
    project: str
    status: str


@dataclass
class PetEvent:
    ref: str
    kind: str
    priority: int
    title: str
    text: str
    line: str = ""
    emotion: str = "calm"
    gesture: str = "none"
    badge: Badge | None = None
    sid: str = ""
    audio: str = ""
    expect: str = ""
    ts: float = 0.0
    count: int = 1

    def wire(self) -> dict:
        return {"v": PROTOCOL, "ref": self.ref, "kind": self.kind,
                "pri": self.priority, "title": self.title, "text": self.text,
                "line": self.line, "emo": self.emotion, "ges": self.gesture,
                "badge": ({"project": self.badge.project, "status": self.badge.status}
                          if self.badge else None),
                "sid": self.sid, "audio": self.audio, "expect": self.expect,
                "ts": self.ts, "count": self.count}

    @classmethod
    def from_wire(cls, d: dict) -> "PetEvent":
        b = d.get("badge")
        return cls(ref=d.get("ref") or "", kind=d.get("kind") or "",
                   priority=int(d.get("pri") or 0),
                   title=d.get("title") or "", text=d.get("text") or "",
                   line=d.get("line") or "", emotion=d.get("emo") or "calm",
                   gesture=d.get("ges") or "none",
                   badge=Badge(b.get("project") or "", b.get("status") or "") if b else None,
                   sid=d.get("sid") or "", audio=d.get("audio") or "",
                   expect=d.get("expect") or "",
                   ts=float(d.get("ts") or 0), count=int(d.get("count") or 1))
