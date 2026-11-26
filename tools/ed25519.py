#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Ed25519（RFC 8032）纯 Python 实现 —— 只给发布链路用，别当通用库。

为什么自己写：Mac 自带的 OpenSSL 是 LibreSSL 3.3，`genpkey -algorithm ed25519`
和 `pkeyutl -rawin` 都不支持；而 host 端又不能保证装了 cryptography。
验签一次 ~20ms（256 次点加），发版/更新各一次，足够。

  python3 tools/ed25519.py genkey            → 打印 seed_hex public_hex
  python3 tools/ed25519.py sign <seed_hex> <文件>   → 打印签名 hex（对文件内容签名）
"""
import hashlib
import sys

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % P
    B = (y1 + x1) * (y2 + x2) % P
    C = t1 * 2 * D * t2 % P
    Dd = z1 * 2 * z2 % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _xrecover(y):
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * I % P
    if (x * x - xx) % P:
        return None
    return P - x if x % 2 else x


BY = 4 * pow(5, P - 2, P) % P
BX = _xrecover(BY)
B = (BX, BY, 1, BX * BY % P)


def _compress(pt):
    x, y, z, _ = pt
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        return None
    x = _xrecover(y)
    if x is None:
        return None
    if x & 1 != sign:
        x = P - x
    return (x, y, 1, x * y % P)


def _h(m):
    return hashlib.sha512(m).digest()


def publickey(seed):
    h = _h(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, B))


def sign(seed, msg, pub=None):
    h = _h(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    pub = pub or _compress(_mul(a, B))
    r = int.from_bytes(_h(h[32:] + msg), "little") % L
    R = _compress(_mul(r, B))
    k = int.from_bytes(_h(R + pub + msg), "little") % L
    S = (r + k * a) % L
    return R + int.to_bytes(S, 32, "little")


def verify(pub, msg, sig):
    """签名有效返回 True。任何畸形输入都返回 False，绝不抛异常。"""
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        A = _decompress(pub)
        R = _decompress(sig[:32])
        if A is None or R is None:
            return False
        S = int.from_bytes(sig[32:], "little")
        if S >= L:
            return False
        k = int.from_bytes(_h(sig[:32] + pub + msg), "little") % L
        left = _compress(_mul(S, B))
        right = _compress(_add(R, _mul(k, A)))
        return left == right
    except Exception:
        return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "genkey":
        import os
        seed = os.urandom(32)
        print(seed.hex() + " " + publickey(seed).hex())
    elif cmd == "sign" and len(sys.argv) >= 4:
        seed = bytes.fromhex(sys.argv[2])
        print(sign(seed, open(sys.argv[3], "rb").read()).hex())
    else:
        print(__doc__)
        sys.exit(2)
