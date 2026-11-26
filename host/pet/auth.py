# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌宠专用鉴权：板子拿自己的 token，不碰 Host 全权限 token。

配对码给手机/板子用一次就作废，6 位、10 分钟。
"""

import hmac
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import web

TOKEN_PATH = Path(__file__).resolve().parent.parent / "pet_token.txt"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def pet_token(path: Path = TOKEN_PATH) -> str:
    try:
        got = Path(path).read_text(encoding="utf-8").strip()
        if HEX64.match(got):
            return got
    except OSError:
        pass
    tok = secrets.token_hex(32)
    p = Path(path)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, tok.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    return tok


async def require_pet(request):
    got = (request.headers.get("X-Pet-Token")
           or request.query.get("pt") or "")
    if not hmac.compare_digest(got, pet_token(TOKEN_PATH)):
        raise web.HTTPUnauthorized(text="bad pet token")


class PairManager:
    def __init__(self, ttl=600):
        self.ttl = ttl
        self._codes = {}

    def _prune(self, now):
        for c, exp in list(self._codes.items()):
            if exp <= now:
                self._codes.pop(c, None)

    def start(self, now=None) -> str:
        now = time.time() if now is None else now
        self._prune(now)
        while True:
            code = str(secrets.randbelow(900000) + 100000)
            if code not in self._codes:
                self._codes[code] = now + self.ttl
                return code

    def claim(self, code, device, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        exp = self._codes.get(code)
        if exp is None or exp <= now:
            return None
        self._codes.pop(code, None)
        return pet_token(TOKEN_PATH)
