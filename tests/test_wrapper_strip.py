#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""wrapper._strip_graphics 自检：python test_wrapper_strip.py"""
from wrapper import _strip_graphics

GS = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"

def run(chunks):
    carry, out = b"", b""
    for c in chunks:
        o, carry = _strip_graphics(carry, c)
        out += o
    return out, carry

# 1) 整段 APC 丢掉，前后普通文本保留
assert run([b"a" + GS + b"b"]) == (b"ab", b"")
# 2) 串被切成两半也要剥干净
assert run([b"a\x1b_Gi=31,s", b"=1;AA", b"AA\x1b\\b"]) == (b"ab", b"")
# 3) DCS 同理
assert run([b"x\x1bP1$r0m\x1b\\y"]) == (b"xy", b"")
# 4) 半截串留到下次，不带残渣
assert run([b"a\x1b_Gxx"]) == (b"a", b"\x1b_Gxx")
# 5) 普通 CSI 样式码/普通 ESC 序列原样保留
assert run([b"\x1b[31mred\x1b[0m"]) == (b"\x1b[31mred\x1b[0m", b"")
# 6) 末尾孤立 ESC 留到下个 chunk，不会吞下一个字符
o, c = _strip_graphics(b"", b"x\x1b")
assert (o, c) == (b"x", b"\x1b")
assert _strip_graphics(c, b"[1my") == (b"\x1b[1my", b"")
print("OK")
