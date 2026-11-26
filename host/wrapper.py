#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""pty 代理：在 Terminal.app 里经它启动 claude，把带 ANSI 样式的原始输出流 tee 一份到
logs/ansi/<sid>.log，ptyhost 用它给手机端做全彩实时镜像（xterm.js 重放原始字节流）。

用法: wrapper.py [--meta '{"sid":"xx","cwd":"..."}'] <cmd> [args...]
Terminal.app 标签页照常显示（本进程只是透传输入输出），关掉标签页即结束。
"""
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import tty
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs" / "ansi"


def _strip_graphics(carry, data):
    """剥掉 APC/DCS 控制串（`ESC _ … ESC \\`、`ESC P … ESC \\`）。

    支持 kitty 图形协议的终端（Warp/kitty/WezTerm/Ghostty）里，Claude Code 会发
    图形探针和图片；镜像（pyte）不认这两种串，会把载荷当文字画到手机上
    ——实测 Warp 里屏幕上多出一行 `Gi=31,s=1,v=1…`。手机本来也画不了图，直接丢。"""
    buf = carry + data
    out = bytearray()
    i, n = 0, len(buf)
    while i < n:
        j = buf.find(b"\x1b", i)
        if j < 0:
            out += buf[i:]
            break
        out += buf[i:j]
        if j + 1 >= n:                 # 末尾只剩一个 ESC：等下一个 chunk
            return bytes(out), buf[j:]
        if buf[j + 1:j + 2] in (b"_", b"P"):
            k = buf.find(b"\x1b\\", j + 2)
            if k < 0:                  # 串还没结束：整段留给下个 chunk
                return bytes(out), buf[j:]
            i = k + 2
            continue
        out += buf[j:j + 2]
        i = j + 2
    return bytes(out), b""


def _pin_titles(pin, carry, data):
    """把 agent 写的 OSC 0/1/2 标题串（`ESC ]2;… BEL` 或 `… ESC \\` 结尾）换成钉住的
    名字——电脑终端 tab 的标题里「进程名 -- 参数」那截是 Terminal 按前台进程拼的，
    agent 的 OSC 只能盖掉标题段；钉住后手机上的名字（用户改名/ai-title）tab 跟着走。
    pin 为空 = 没钉：原样放行。跨 chunk 断串用 carry。"""
    if not pin:
        return carry + data, b""
    buf = carry + data
    out = bytearray()
    i, n = 0, len(buf)
    while i < n:
        j = buf.find(b"\x1b]", i)
        if j < 0:
            if buf.endswith(b"\x1b"):      # 尾巴挂着孤 ESC：`]` 可能在下个 chunk
                return bytes(out + buf[i:n - 1]), b"\x1b"
            out += buf[i:]
            break
        out += buf[i:j]
        if j + 4 > n:                  # ESC ] 后不满 4 字节，判不了是不是标题：等下个 chunk
            return bytes(out), buf[j:]
        if buf[j + 2:j + 3] in (b"0", b"1", b"2") and buf[j + 3:j + 4] == b";":
            k = buf.find(b"\x07", j + 4)
            k2 = buf.find(b"\x1b\\", j + 4)
            if k >= 0:
                i = k + 1
            elif k2 >= 0:
                i = k2 + 2
            else:                      # 串还没结束：整段留给下个 chunk
                return bytes(out), buf[j:]
            out += b"\x1b]2;" + pin + b"\x07"
            continue
        out += buf[j:j + 2]            # 其他 OSC（7=cwd、104=重置…）：原样保留
        i = j + 2
    return bytes(out), b""


def main() -> int:
    args = sys.argv[1:]
    extra = {}
    if args and args[0] == "--meta":
        try:
            extra = json.loads(args[1])
        except Exception:
            extra = {}
        args = args[2:]
    if args and args[0] == "--":  # 约定分隔符（zshrc alias 用它防歧义）
        args = args[1:]
    if not args:
        print("usage: wrapper.py [--meta json] cmd [args...]", file=sys.stderr)
        return 2

    sid = str(extra.get("sid") or uuid.uuid4().hex[:8])
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / (sid + ".log")
    meta_path = LOG_DIR / (sid + ".meta.json")

    pid, fd = pty.fork()
    if pid == 0:  # 子进程：换成真正的命令，这个 pty 就是它的控制终端
        try:
            ef = extra.get("envfile")
            if ef:
                # 模型切换的 env 注入：密钥只在这个 0600 文件里，读完即删，
                # 不落命令行/ps/日志（server.py h_ext_model_switch 写入）
                try:
                    os.environ.update(json.loads(Path(ef).read_text()))
                    os.unlink(ef)
                except Exception:
                    pass
            os.execvp(args[0], args)
        except Exception:
            pass
        os._exit(127)

    meta = {"sid": sid, "pid": pid, "cmd": args, "log": str(log_path),
            # 宿主终端（Apple_Terminal / WarpTerminal / iTerm.app…）：换模型、关闭后
            # 召回都按这个回到原终端，而不是一律塞进 Terminal.app。meta 退出后还在盘上。
            "term": os.environ.get("TERM_PROGRAM", "")}
    meta.update(extra)
    # 宿主终端的 tty（wrapper 自己的 stdin/stdout 挂在哪个终端上）。
    # Linux 上用来把「新建的会话」和它的 meta 对上（无头机器没人能点信任框，
    # 得让 host 自己认领并确认）。
    try:
        meta["tty"] = os.ttyname(sys.stdout.fileno())
    except Exception:
        pass
    # 控制通道：host 通过这个 unix socket 把原始字节写进 pty（方向键等）。
    # 终端那条路（do script）会自带回车，方向键+回车=重发上一条，不能用；
    # 合成按键（System Events）又要辅助功能权限，实测在这台机器上静默丢弃。
    ctl_path = LOG_DIR / (sid + ".sock")
    try:
        ctl_path.unlink()
    except OSError:
        pass
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ctl.bind(str(ctl_path))
    ctl.listen(8)
    ctl.setblocking(False)
    meta["ctl"] = str(ctl_path)
    # 协议版本：host 只对 wv>=1 的 wrapper 推标题（旧 wrapper 会把控制字节当按键打进 pty）
    meta["wv"] = 1
    meta_path.write_text(json.dumps(meta, ensure_ascii=False))

    log = open(log_path, "ab", buffering=0)

    def sync_winsize(*_):
        # 跟随 Terminal 标签页的窗口尺寸，保证里面 claude 的 TUI 布局正确；
        # 尺寸同时写进 meta，手机端全彩镜像按同样的列数建网格（否则光标定位全错位）
        try:
            sz = fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
            rows, cols = struct.unpack("HH", sz[:4])  # winsize 是 4 个 short，取前两个
            meta["rows"], meta["cols"] = int(rows), int(cols)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False))
        except (OSError, struct.error):
            pass

    signal.signal(signal.SIGWINCH, sync_winsize)
    sync_winsize()

    saved = None
    try:
        saved = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin)
    except termios.error:
        pass  # stdin 不是终端（管道调试场景）：跳过 raw 模式，透传照常

    stdin_fd = sys.stdin.fileno()
    status = 0
    # ⏺(U+23FA)/⏵(U+23F5) 在手机 WebView 上落 emoji 字体（圆圈/三角带方块底）；落日志前等长换成 ●/▸。
    # 纯字节替换（3↔3 等长不错位），跨 chunk 断字用 carry 缓冲；
    # os.write(stdout) 的真终端回显保持原样，只有日志文件走这条路径。
    _TEE_PAIRS = [
        ("⏺".encode("utf-8"), "●".encode("utf-8")),   # E2 8F BA → E2 97 8F
        ("⏵".encode("utf-8"), "▸".encode("utf-8")),   # E2 8F B5 → E2 96 B8
    ]

    def _tee_replace(carry, data):
        buf = carry + data
        out = buf
        for old, new in _TEE_PAIRS:
            out = out.replace(old, new)
        for keep in (2, 1):   # 尾巴挂着某图案前半截：留到下个 chunk 再判（图案都 3 字节，首字节 E2）
            if len(buf) >= keep and any(buf.endswith(old[:keep]) for old, _ in _TEE_PAIRS):
                return out[:len(buf) - keep], buf[-keep:]
        return out, b""

    carry = b""
    gcarry = b""      # 被切成两半的 APC/DCS 控制串
    tcarry = b""      # 被切成两半的 OSC 标题串
    pin = b""         # 钉住的 tab 标题（host 经 ctl 推 `NUL T:名字\\n`）；空 = 不钉
    try:
        while True:
            watchers = [fd, ctl] + ([stdin_fd] if stdin_fd >= 0 else [])
            r, _, _ = select.select(watchers, [], [], 1.0)
            if stdin_fd >= 0 and stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    os.write(fd, data)
                else:
                    stdin_fd = -1  # 终端侧关闭：不再读输入，等子进程自己退出
            if ctl in r:
                try:
                    conn, _ = ctl.accept()
                except OSError:
                    conn = None
                if conn is not None:
                    try:
                        conn.settimeout(2)
                        acc = b""
                        while True:
                            d = conn.recv(4096)
                            if not d:
                                break
                            acc += d
                            if acc.startswith(b"\x00T:"):
                                # 控制消息：钉/换 tab 标题。NUL 打字打不出来，按键流撞不上。
                                if b"\n" not in acc:
                                    continue  # 消息被 recv 切断：拼够换行再判
                                line, _rest = acc.split(b"\n", 1)
                                pin = line[3:]
                                tcarry = b""  # 换钉后旧 carry 里的半截 agent 串作废
                                try:      # 立刻刷到终端 tab，不等下一段输出
                                    os.write(sys.stdout.fileno(),
                                             b"\x1b]2;" + pin + b"\x07")
                                except OSError:
                                    pass
                                acc = b""
                            else:
                                os.write(fd, acc)   # 原始字节进 pty：方向键等，不带回车
                                acc = b""
                    except OSError:
                        pass
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass
            if fd in r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    if carry:
                        log.write(_tee_replace(b"", carry)[0])  # 收尾清缓冲（只剩半截字符）
                    break
                clean, gcarry = _strip_graphics(gcarry, data)
                out_b, carry = _tee_replace(carry, clean)
                log.write(out_b)  # 原样 tee（ANSI 样式码全留；图形协议串已剥；⏺/⏵ 等长换 ●/▸）
                term_b, tcarry = _pin_titles(pin, tcarry, data)  # 只动终端这条路
                os.write(sys.stdout.fileno(), term_b)
    finally:
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        if saved is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
        try:
            ctl.close()
        except OSError:
            pass
        try:
            ctl_path.unlink()
        except OSError:
            pass
        meta["exited"] = True
        meta["exit_code"] = os.waitstatus_to_exitcode(status) if status else 0
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
    return meta.get("exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
