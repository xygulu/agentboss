# SPDX-License-Identifier: AGPL-3.0-or-later

"""全彩镜像跨重生续命：wrapper 日志按会话 sid 命名后，升级那一刻已经分家的
旧 hex 日志要在第一次新式重生时搬一次家。

现象（2026-11-25）：切模型/召回重生 wrapper 时日志用随机 hex 名，旧进程攒的
终端滚动历史（deskboss 2.9MB）被撇在旧 hex 日志里；手机端全彩镜像按 pid 认
meta 只读新日志——看起来就是「历史没了，只剩一页都不到」。

跑法： .venv/bin/python -m pytest tests/test_mirror_seed.py -q
"""
import json
import os
import time

import server


def _meta(tmp_path, hex_sid, resume_sid, log_name, age=0, fork=False):
    """摆一个 wrapper 世代的 meta：cmd 里带 --resume <resume_sid>（fork 的带 --fork-session）。"""
    d = tmp_path / "ansi"
    d.mkdir(exist_ok=True)
    log = d / log_name
    log.write_bytes(b"\x1b[31mold-terminal-output\x1b[0m")
    cmd = ["claude", "--resume", resume_sid]
    if fork:
        cmd.append("--fork-session")
    m = {"sid": hex_sid, "log": str(log), "cmd": cmd, "exited": True}
    f = d / (hex_sid + ".meta.json")
    f.write_text(json.dumps(m), encoding="utf-8")
    t = time.time() - age
    os.utime(f, (t, t))
    return log


def test_seed_merges_chain_in_order(tmp_path):
    """整条前代链按死亡时间正序全续：只挑最近一代会把穷世代当独苗
    （0.9.102 首版只续到 2KB，历史在它前面的祖辈日志里）。"""
    old = _meta(tmp_path, "aaaa1111", "s-1", "aaaa1111.log", age=100)
    mid = _meta(tmp_path, "cccc3333", "s-1", "cccc3333.log", age=50)
    new = _meta(tmp_path, "bbbb2222", "s-1", "bbbb2222.log", age=10)
    assert server._mirror_seed_log("s-1", tmp_path / "ansi") == \
        [str(old), str(mid), str(new)]


def test_seed_skips_fork_windows(tmp_path):
    """fork 的 cmd 也带着父会话 sid，但它是另一条对话：绝不能把它的终端输出
    混进父会话的滚动历史。"""
    _meta(tmp_path, "aaaa1111", "s-1", "aaaa1111.log", age=100, fork=True)
    assert server._mirror_seed_log("s-1", tmp_path / "ansi") == []


def test_seed_skips_when_new_log_exists(tmp_path):
    """新式日志已存在（升级后重生过一次）：续命靠 append，不再搬家。"""
    d = tmp_path / "ansi"
    _meta(tmp_path, "aaaa1111", "s-1", "aaaa1111.log", age=100)
    (d / ("s-1.log")).write_bytes(b"")
    assert server._mirror_seed_log("s-1", tmp_path / "ansi") == []


def test_seed_skips_unrelated_sessions(tmp_path):
    """别的会话（cmd 不带这个 sid）的日志不是种子。"""
    _meta(tmp_path, "aaaa1111", "other-sid", "aaaa1111.log", age=0)
    assert server._mirror_seed_log("s-1", tmp_path / "ansi") == []


def test_seed_merge_caps_to_newest_tail(tmp_path):
    """整链合并带上限：超限从最老头部丢、保最新尾巴。回放冷启只看最后 ~1MB
    （_mirror_tail_start），把 61MB 祖辈日志全并进来只会让回放把整份读完
    （2026-11-25 08:36 喂 61MB 卡 host 50 秒、中继被拖断重连）。"""
    d = tmp_path / "ansi"
    d.mkdir()
    a = d / "a.log"; a.write_bytes(b"A" * 100)
    b = d / "b.log"; b.write_bytes(b"B" * 100)
    n = server._mirror_seed_merge("s-1", [str(a), str(b)], ansi_dir=d, cap=150)
    assert n == 150
    assert (d / "s-1.log").read_bytes() == b"A" * 50 + b"B" * 100


def test_seed_merge_under_cap_keeps_everything(tmp_path):
    """没超限就整链全并（这才是续命的本意）。"""
    d = tmp_path / "ansi"
    d.mkdir()
    a = d / "a.log"; a.write_bytes(b"ABCD")
    n = server._mirror_seed_merge("s-1", [str(a)], ansi_dir=d, cap=1 << 20)
    assert n == 4
    assert (d / "s-1.log").read_bytes() == b"ABCD"


def test_mirror_state_rebuilds_when_log_changes(monkeypatch, tmp_path):
    """世代换了（日志文件换掉），镜像状态必须重冷启：旧 off 位点对着新文件
    继续「增量」，会把整份合并历史从头喂一遍——61MB 喂 50 秒，轮询全堵死。"""
    monkeypatch.setattr(server, "MIRROR_TAIL_BYTES", 10)
    server._MIRROR.pop("sX-logchg", None)
    big = tmp_path / "big.log"
    big.write_bytes(b"\x1b[31m" + b"x" * 200)          # 205B，尾巴里没有 ESC
    st = server._mirror_get("sX-logchg", 80, 24, log=str(big), size=205)
    assert st["off"] == 195                             # 冷启从尾巴进
    st2 = server._mirror_get("sX-logchg", 80, 24, log=str(big), size=205)
    assert st2 is st                                    # 同一文件：命中缓存
    new = tmp_path / "new.log"
    new.write_bytes(b"x" * 500)
    st3 = server._mirror_get("sX-logchg", 80, 24, log=str(new), size=500)
    assert st3 is not st                                # 换文件：重冷启
    assert st3["off"] == 490
    server._MIRROR.pop("sX-logchg", None)
