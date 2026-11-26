# SPDX-License-Identifier: AGPL-3.0-or-later

"""在场判断：老板在不在电脑前。

键盘/鼠标空闲时间 + 锁屏 + 显示器 + 前台 App + 手机有没有在动（手机在桌上
不等于人在）。采样是阻塞子进程，调用方负责放进 executor。
"""

import re
import subprocess
import time
from dataclasses import dataclass

AWAY_S = 90        # 键盘鼠标 90 秒没动 → 认为不在
UNCERTAIN_S = 45   # 45–90 秒：不确定，默认不出声
RETURN_S = 10      # 回到 10 秒内才算真回来（防抖）
PHONE_ACTIVE_S = 30


@dataclass
class Presence:
    idle_s: float = 0.0
    locked: bool = False
    display_asleep: bool = False
    front_app: str = ""
    phone_present: bool | None = None
    phone_moved_s: float | None = None
    ts: float = 0.0


@dataclass
class PresenceState:
    at_desk: bool
    away: bool
    uncertain: bool
    away_for: float
    reason: str


def _parse_hid_idle(text: str) -> float:
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) / 1e9 if m else 0.0


def _parse_lock(text: str) -> bool:
    m = re.search(r'"CGSSessionScreenIsLocked"\s*=\s*(Yes|true|1)', text or "")
    return bool(m)


def _parse_lsappinfo_name(text: str) -> str:
    m = re.search(r'"LSDisplayName"="([^"]+)"', text or "")
    return m.group(1) if m else ""


def _parse_display_power(text: str) -> bool:
    m = re.search(r'CurrentPowerState\s+(\d+)', text or "")
    return True if not m else int(m.group(1)) >= 4


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return p.stdout or ""
    except Exception:
        return ""


class PresenceTracker:
    def __init__(self):
        self._away_since = 0.0
        self._away = False

    def update(self, p: Presence, now: float) -> PresenceState:
        phone_active = (p.phone_present is True and p.phone_moved_s is not None
                        and p.phone_moved_s < PHONE_ACTIVE_S)
        reason = ""
        away = False
        if p.locked:
            away, reason = True, "locked"
        elif p.display_asleep:
            away, reason = True, "display"
        elif p.idle_s >= AWAY_S:
            away, reason = True, "idle"
        elif self._away and p.idle_s >= RETURN_S and not phone_active:
            away, reason = True, "hysteresis"
        if away:
            if not self._away:
                self._away_since = now
            self._away = True
            return PresenceState(False, True, False, max(0.0, now - self._away_since), reason)
        self._away = False
        self._away_since = 0.0
        if phone_active:
            return PresenceState(True, False, False, 0.0, "phone")
        if p.idle_s >= UNCERTAIN_S:
            return PresenceState(False, False, True, 0.0, "uncertain")
        return PresenceState(True, False, False, 0.0, "active")


class MacPresenceSampler:
    def __init__(self):
        self._phone_present = None
        self._phone_moved_s = None

    def note_phone(self, present: bool, moved_s: float | None, now: float):
        self._phone_present = present
        self._phone_moved_s = moved_s

    def sample(self) -> Presence:
        idle = _parse_hid_idle(_run(["ioreg", "-c", "IOHIDSystem"]))
        locked = _parse_lock(_run(["ioreg", "-n", "Root", "-d1"]))
        front = ""
        asn = _run(["lsappinfo", "front"]).strip()
        if asn:
            front = _parse_lsappinfo_name(_run(["lsappinfo", "info", "-only", "name", asn]))
        on = _parse_display_power(_run(["pmset", "-g", "powerstate", "IODisplayWrangler"]))
        return Presence(idle_s=idle, locked=locked, display_asleep=not on,
                        front_app=front, phone_present=self._phone_present,
                        phone_moved_s=self._phone_moved_s, ts=time.time())


class FakePresenceSampler:
    def __init__(self, p: Presence | None = None):
        self.p = p or Presence()

    def set(self, p: Presence):
        self.p = p

    def note_phone(self, present: bool, moved_s: float | None, now: float):
        self.p.phone_present = present
        self.p.phone_moved_s = moved_s

    def sample(self) -> Presence:
        return self.p
