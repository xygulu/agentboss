#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""PTY 后端 —— 把「起一个假终端跑 claude」这层按平台隔开。

  Unix（macOS/Linux）：pty.openpty() + subprocess.Popen，主进程用
      loop.add_reader(master) 读输出（和原来的 Session._spawn 逐字一致）。
  Windows：ConPTY（pywinpty）。没有可交给 add_reader 的 fd，所以起一个读线程，
      读到数据用 loop.call_soon_threadsafe 投回事件循环，接口对上层是一样的。

server.py 的 Session 只认这里的 PtyProc：
    spawn(cmd, cwd, env, cols, rows) -> PtyProc
    p.fd                         Unix 的可读 fd（Windows 为 None）
    p.pid
    p.start_reader(loop, on_data, on_eof)
    p.read(n)                    阻塞读（Windows 读线程内部用）
    p.write(bytes)
    p.resize(cols, rows)
    p.kill()
    p.close()

Windows 还有两个公共工具（server.py 也拿来拼命令）：
    find_git_bash() -> str        Git for Windows 的 bash.exe（Claude Code 在
                                  Windows 上必须有它；有它就所有命令都走 bash，
                                  POSIX 风格的 cd/引号/管道全都原样能用）
    wrap_argv(argv) -> list       把一条命令包成当前平台真正能执行的形式
"""

import codecs
import os
import shutil
import subprocess
import sys
import threading

IS_WIN = os.name == "nt"

_COMSPEC = os.environ.get("COMSPEC") or "cmd.exe"


# ---------------------------------------------------------------- Windows 工具

_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def find_git_bash() -> str:
    """Git for Windows 的 bash.exe：Claude Code 在 Windows 上就靠它跑命令。
    先看官方环境变量（Claude Code 自己也认这个），再看常见安装位，最后 PATH。"""
    if not IS_WIN:
        return "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or ""
    if env and os.path.exists(env):
        return env
    for c in _BASH_CANDIDATES:
        if c and os.path.exists(c):
            return c
    found = shutil.which("bash")
    return found or ""


def _quote_sh(s) -> str:
    s = str(s)
    if not s:
        return "''"
    # 单引号包裹；内嵌单引号用 '\'' 拼接。路径里的反斜杠原样保留。
    return "'" + s.replace("'", "'\\''") + "'"


def wrap_argv(argv) -> list:
    """一条命令 -> 本平台可直接 spawn 的 argv。

    Windows：有 Git Bash 就走 `bash -lc '<命令>'`——这样 `claude`（可能是
    claude.cmd / claude.exe）、`cd`、引号、管道的行为和其它平台一致；没有 bash
    时退回 cmd.exe /c（Claude Code 本身就跑不起来，属于兜底）。
    """
    argv = [str(a) for a in argv]
    if not IS_WIN:
        return argv
    bash = find_git_bash()
    if bash:
        return [bash, "-lc", " ".join(_quote_sh(a) for a in argv)]
    return [_COMSPEC, "/d", "/s", "/c", subprocess.list2cmdline(argv)]


def shell_wrap(command: str) -> list:
    """一段 shell 脚本 -> 本平台可执行的 argv（cmdrun 那类整段命令用）。"""
    if not IS_WIN:
        return [os.environ.get("SHELL") or "/bin/sh", "-c", command]
    bash = find_git_bash()
    if bash:
        return [bash, "-lc", command]
    return [_COMSPEC, "/d", "/s", "/c", command]


def win_no_window() -> int:
    """子进程不弹黑框（Windows 专用；其它平台返回 0）。"""
    if not IS_WIN:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


# ---------------------------------------------------------------- Unix：openpty

class UnixPty:
    def __init__(self, cmd, cwd, env, cols, rows):
        import fcntl
        import pty
        import struct
        import termios
        self._fcntl = fcntl
        self._termios = termios
        self._struct = struct
        master, slave = pty.openpty()
        self.fd = master
        self._set_winsize(cols, rows)

        def _preexec():
            # 新会话组 + 把 slave 设成控制终端：只 setsid 不给 ctty 的话，
            # sudo 会认为「没有终端」，直接报 "a password is required" 而不弹密码提示
            # （手机端命令终端里跑 sudo 就永远输不了密码）。
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        self.proc = subprocess.Popen(
            list(cmd), stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd, env=env, preexec_fn=_preexec, close_fds=True,
        )
        os.close(slave)
        self.pid = self.proc.pid
        self._thread = None

    def _set_winsize(self, cols, rows):
        try:
            self._fcntl.ioctl(self.fd, self._termios.TIOCSWINSZ,
                              self._struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    # -- 读 --
    def start_reader(self, loop, on_data, on_eof):
        def _r():
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    loop.remove_reader(self.fd)
                except Exception:
                    pass
                on_eof()
                return
            on_data(chunk)
        loop.add_reader(self.fd, _r)

    def read(self, n=65536):
        return os.read(self.fd, n)

    # -- 写 / 尺寸 / 结束 --
    def write(self, data: bytes):
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, cols, rows):
        self._set_winsize(cols, rows)

    def kill(self):
        import signal
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
        except OSError:
            pass

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------- Windows：ConPTY

class WinPty:
    """pywinpty >= 2 的 ConPTY。

    注意事项（都是实测踩过的口径）：
    - spawn 的 argv 必须是 Windows 真能 CreateProcess 的东西，所以上层统一先用
      wrap_argv 包一层（bash -lc …）；
    - 输出是 UTF-8 的 VT 序列，pyte 照常吃；read() 返回 str，这里统一转 bytes；
    - 写反向：pyte/手机端给的是 bytes，用增量解码器转 str，避免多字节中文被切断。
    """

    def __init__(self, cmd, cwd, env, cols, rows):
        from winpty import PtyProcess   # 只有 Windows 装了这个包
        self._enc = codecs.getincrementaldecoder("utf-8")("replace")
        env = {str(k): str(v) for k, v in (env or {}).items()}
        self.proc = PtyProcess.spawn(
            list(cmd), cwd=str(cwd) if cwd else None, env=env,
            dimensions=(int(rows), int(cols)),
        )
        self.pid = getattr(self.proc, "pid", None)
        self.fd = None
        self._stop = False
        self._thread = None

    def start_reader(self, loop, on_data, on_eof):
        def _loop():
            while not self._stop:
                try:
                    data = self.proc.read(65536)
                except EOFError:
                    break
                except Exception:
                    break
                if not data:
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8", "replace")
                try:
                    loop.call_soon_threadsafe(on_data, data)
                except RuntimeError:      # 事件循环已关（服务退出）
                    return
            try:
                loop.call_soon_threadsafe(on_eof)
            except RuntimeError:
                pass
        self._thread = threading.Thread(target=_loop, name="pty-read", daemon=True)
        self._thread.start()

    def read(self, n=65536):
        return self.proc.read(n)

    def write(self, data: bytes):
        try:
            text = self._enc.decode(data if isinstance(data, bytes) else bytes(data))
        except Exception:
            return
        if not text:
            return
        try:
            self.proc.write(text)
        except Exception:
            pass

    def resize(self, cols, rows):
        try:
            self.proc.setwinsize(int(rows), int(cols))
        except Exception:
            pass

    def kill(self):
        self._stop = True
        # ConPTY 的 terminate(force=True) 只杀直接子进程；claude 还带 node 子进程，
        # 用 taskkill /T 整棵树收掉，免得残留占着工作目录（下次更新会覆盖失败）。
        pid = self.pid
        if pid:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=win_no_window())
                return
            except Exception:
                pass
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass

    def close(self):
        self._stop = True
        try:
            self.proc.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 对上层

def spawn(cmd, cwd, env, cols, rows):
    if IS_WIN:
        return WinPty(wrap_argv(cmd), cwd, env, cols, rows)
    return UnixPty(cmd, cwd, env, cols, rows)


def backend_name() -> str:
    if IS_WIN:
        return "windows"
    return "unix"
