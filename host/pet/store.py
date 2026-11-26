# SPDX-License-Identifier: AGPL-3.0-or-later

"""pet.json：桌宠那点自己的状态（人格、开关、队列、短期记忆）。原子写。"""

import copy
import json
import os
from pathlib import Path

DEFAULTS = {"v": 1, "persona": "xiaogongtou", "voice_on": True, "dnd": [23, 8],
            "seen_seq": 0, "queue": [], "devices": {},
            "mem": {"chat": [], "done_date": "", "done_today": 0}}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        data = copy.deepcopy(DEFAULTS)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return data
        except OSError:
            return data
        try:
            got = json.loads(raw)
            if not isinstance(got, dict):
                raise ValueError("not a dict")
        except ValueError:
            bad = self.path.with_name(self.path.name + ".bad")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            return data
        for k, v in data.items():
            got.setdefault(k, v)
        return got

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
