#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""Codeboss 托盘客户端（Windows）——和 macOS 菜单栏 App 同一套功能。

用 `.venv\\Scripts\\pythonw.exe` 跑（没有黑框窗口），由 install.ps1 放进「启动」文件夹。
右键菜单（顺序与 mac 一致）：

    服务：运行中 / 已停止
    ─────
    扫这个码接上…
    查看会话列表…
    ─────
    启动服务 / 停止服务
    重新安装服务（覆盖，保留数据）…
    卸载服务…
    套餐与续费…
    检查更新…
    ─────
    打开主窗口
    退出

「打开主窗口」是本机唯一的客户端窗口（对应 mac 主窗口）：一个窗口、四个页
（首页 / 会话 / 套餐 / 更新），页面里带状态、配对二维码、服务起停、套餐付款、
更新、只读日志卡。窗口另起进程画 Tk（崩了不影响托盘和服务），同一时间只有一个窗口
（命名互斥量 logs/ui_cmd.json），托盘菜单/快捷方式再点只会切页并把它拉到前台。

设计口径：
- 只做「接入 + 服务起停 + 账号」，不做第二套会话 UI（会话在手机里看，Windows 这端只看列表）；
- 不打开浏览器：日常入口（会话/主窗口）都留在应用内。只保留套餐页/支付页的兜底按钮
  （中继返回的账号页 / payurl，不带本机 token），行内加载失败或二维码拿不到时才出现；
- 服务真身是计划任务「Codeboss」拉起的那条进程，托盘只负责开关它；
- 所有系统调用都走 CREATE_NO_WINDOW，绝不闪黑框；安装/卸载脚本单独开窗，进度看得见；
- 托盘自己崩了不影响服务；服务崩了计划任务 1 分钟后会自己拉起来。
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pathlib as _pathlib
import sys as _sys
# 仓库按组件分目录（host/ 与 shared/ 是兄弟）；发布包是扁平的，品牌模块就在旁边。
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent / "shared"))
import branding

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "token.txt"
CONFIG_FILE = HERE / "config.json"
VERSION_FILE = HERE / "VERSION"
LOG_FILE = HERE / "logs" / "tray.log"
STOP_FLAG = HERE / "logs" / ".stopped"   # 用户主动「停止服务」的标记（启动脚本见它就不启动）
PORT_FILE = HERE / "logs" / "port.txt"


def port():
    """host 的实际端口。正常是 8790；被别的程序占用时 host 会换端口并写 logs/port.txt
    （换端口不写文件的话，托盘会一直显示「已停止」）。"""
    try:
        n = int((PORT_FILE.read_text(encoding="utf-8").strip() or "0"))
        return n or 8790
    except Exception:
        return 8790


PORT = 8790     # 旧名的兼容保留；新代码一律用 port()


def _app_name():
    """二维码旁提示的端名：按本机产品线（认不得说通用「工头」，别冒充口袋工头）。"""
    try:
        p = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("product")
    except Exception:
        p = None
    return branding.app_label(p)


def _base():
    return "http://127.0.0.1:%d" % port()


TASK = "Codeboss"
DEFAULT_RELAY = "https://relay.example.com"

# 深色主题：和 macOS 主窗口一套（见 mac/main.swift 的 C_BG/C_CARD/C_LINE/…）
C_BG = "#0b0e14"
C_CARD = "#12171f"
C_LINE = "#212936"
C_TEXT = "#e6edf3"
C_MUTED = "#8b949e"
C_GREEN = "#2ea043"
C_GREEN_H = "#3fb950"
C_YELLOW = "#e3b341"
C_RED = "#f85149"
C_BTN = "#1b2230"
C_BTN_H = "#26303f"
FONT = "Microsoft YaHei UI"
BRAND = "BossHost 工头主机"

STATE = {"alive": False, "enabled": None, "checked": 0.0,
         "down": 0, "last_restart": 0.0}   # down：连续几次探活失败（看门狗用）
_MUTEX = None


def _logline(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- 基础工具

def _dec(b: bytes) -> str:
    """schtasks 的 XML 在中文系统上是 UTF-16，netstat 是 GBK，统一兜一遍。"""
    if not b:
        return ""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return b.decode("utf-16")
        except Exception:
            pass
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("gbk")
    except Exception:
        return b.decode("utf-8", "replace")


def _no_window():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def _run(args, timeout=20):
    """跑一条命令，返回 (code, stdout)。永远不弹黑框。"""
    try:
        p = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_no_window())
        return p.returncode, _dec(p.stdout or b"")
    except Exception as e:
        return -1, str(e)


def _spawn_visible(args):
    """单独开一个可见的控制台窗口跑（安装/卸载脚本要看得见进度）。"""
    return subprocess.Popen(list(args), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def _win_msgbox(text, title=BRAND):
    """没有 Tk 也能弹的提示框：pythonw 下没有控制台，出错了不弹窗就完全没反应。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR
    except Exception:
        pass


def token():
    try:
        for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                return t
    except OSError:
        pass
    return ""


def version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def relay_cfg():
    """(relay_url, device_id, device_secret)——secret 只在内存里用，绝不打印。"""
    try:
        r = (json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("relay") or {})
    except Exception:
        r = {}
    u = (r.get("url") or "").rstrip("/").replace("relay.example.com",
                                                 "relay.example.com")
    return (u, r.get("device_id") or "", r.get("device_secret") or "")


def alive(timeout=1.5):
    try:
        with urllib.request.urlopen(_base() + "/api/ping", timeout=timeout) as r:
            STATE["alive"] = (r.status == 200)
    except Exception:
        STATE["alive"] = False
    return STATE["alive"]


def api_get(path, timeout=8):
    req = urllib.request.Request(_base() + path,
                                 headers={"Cookie": "codeboss_t=" + token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_post(path, body, timeout=30):
    req = urllib.request.Request(
        _base() + path, data=json.dumps(body).encode(),
        headers={"Cookie": "codeboss_t=" + token(),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- 服务

def task_enabled():
    """计划任务是否启用（不存在返回 None）。看 XML：不随系统语言变。

    计划任务没登记上的机器（安装时退回了「启动」文件夹兜底）也有 run-server 脚本：
    这种不能当成「没装」——按「用户有没有点过停止服务」判断，否则每次都会
    误报「未安装」把用户引去重装。"""
    code, out = _run(["schtasks", "/Query", "/TN", TASK, "/XML"])
    if code != 0 or "<Task" not in out:
        if (HERE / "run-server.vbs").is_file() or (HERE / "run-server.cmd").is_file():
            STATE["enabled"] = not STOP_FLAG.exists()
            return STATE["enabled"]
        STATE["enabled"] = None
        return None
    m = re.search(r"<Enabled>\s*(true|false)\s*</Enabled>", out, re.I)
    STATE["enabled"] = (m.group(1).lower() == "true") if m else True
    return STATE["enabled"]


def server_pid():
    """监听 8790 的那个进程（= 计划任务拉起的 server.py）。"""
    _code, out = _run(["netstat", "-ano", "-p", "tcp"], timeout=10)
    needle = ":%d" % port()
    for line in out.splitlines():
        if needle not in line or "LISTEN" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            return int(parts[-1])
    return 0


def service_state():
    """'running' | 'stopped' | 'disabled' | 'missing'（和 mac 的三种状态对齐，多一个 disabled）。"""
    a = alive(1.0)
    e = task_enabled()
    if e is None:
        return "running" if a else "missing"
    if a:
        return "running" if e else "running_disabled"
    return "stopped" if e else "disabled"


def _stale_server_pids():
    """所有在跑的自家 server.py 进程——**包括**「进程还在、但监听 socket 已被系统作废」
    的僵尸（它不听 8790，server_pid() 找不到，却占着 server.log 句柄与端口，
    新进程连日志都打不开 → 客户端「启动超时」）。
    三条线索：8790 的监听者、服务自己写的 logs/server.pid、命令行/可执行文件路径
    落在本安装目录里的 python 进程。绝不误杀别的 Python 程序。"""
    pids = set()
    try:
        n = server_pid()
        if n:
            pids.add(n)
    except Exception:
        pass
    try:
        pf = HERE / "logs" / "server.pid"
        n = int((pf.read_text(encoding="utf-8").strip() or "0"))
        if n and n != os.getpid():
            # PID 会被系统回收：确认它现在确实是个 python 进程
            c, o = _run(["tasklist", "/FI", "PID eq %d" % n, "/FO", "CSV", "/NH"], timeout=10)
            if c == 0 and "python" in (o or "").lower():
                pids.add(n)
    except Exception:
        pass
    # 单引号要转义成两个：用户路径里带 ' 时（C:\Users\O'Brien\...）PowerShell 会语法错
    me = str(HERE).replace("'", "''")
    ps = ("$me='%s'; $meS=$me + '\\'; Get-CimInstance Win32_Process | "
          "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and "
          "$_.CommandLine -like '*server.py*' -and ("
          "($_.ExecutablePath -and $_.ExecutablePath.StartsWith($meS, "
          "[StringComparison]::OrdinalIgnoreCase)) -or "
          "($_.CommandLine.IndexOf($me, [StringComparison]::OrdinalIgnoreCase) -ge 0)) } | "
          "Select-Object -ExpandProperty ProcessId") % me
    code, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=25)
    if code == 0:
        for line in (out or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    return [p for p in pids if p != os.getpid()]


def _kill_stale_servers():
    """清掉僵尸 server（含它们卡住的 cmd 父进程）：清完端口和日志句柄才放得开。"""
    killed = []
    for pid in _stale_server_pids():
        _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=20)
        killed.append(pid)
    if killed:
        _logline("清理残留服务进程：%s" % killed)
    return killed


def start_service(timeout=12.0):
    if task_enabled() is None:
        return False, "没找到计划任务「%s」——先点「重新安装服务」安装一次" % TASK
    # 先清僵尸：socket 废掉后进程还活着会把 server.log 锁住，新进程连日志都写不了
    _kill_stale_servers()
    try:
        STOP_FLAG.unlink()          # 清掉「用户主动停止」的标记，否则启动脚本会自己退出
    except OSError:
        pass
    _run(["schtasks", "/Change", "/TN", TASK, "/ENABLE"], timeout=20)
    rc, _o = _run(["schtasks", "/Run", "/TN", TASK], timeout=20)
    if rc != 0:
        # 没登记计划任务（「启动」文件夹兜底安装）：直接拉起启动脚本
        vbs = HERE / "run-server.vbs"
        cmd = HERE / "run-server.cmd"
        try:
            if vbs.is_file():
                subprocess.Popen(["wscript.exe", str(vbs)], close_fds=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            elif cmd.is_file():
                subprocess.Popen(["cmd.exe", "/c", str(cmd)], close_fds=True, cwd=str(HERE),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _logline("计划任务不可用，直接拉起启动脚本")
        except Exception as e:
            return False, "启动失败：%s" % str(e)[:100]
    STATE["enabled"] = True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if alive(0.8):
            return True, ""
        time.sleep(0.6)
    return False, "启动超时。看日志：%s" % (HERE / "logs" / "server.log")


def stop_service():
    _run(["schtasks", "/Change", "/TN", TASK, "/DISABLE"], timeout=20)
    STATE["enabled"] = False      # 立刻告诉看门狗：别把用户刚停的服务又拉起来
    # 标记「是用户主动停的」：新启动脚本里有崩溃自重启循环，没这个标记会马上又拉起来
    try:
        STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        STOP_FLAG.write_text("1")
    except OSError:
        pass
    pid = server_pid()
    if pid:
        _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=20)
    for _ in range(10):
        if not alive(0.5):
            return True, ""
        time.sleep(0.4)
    return False, "进程还在（PID %s），可能权限不够" % (pid or "?")


def restart_service():
    # 不只用监听里的 PID：socket 废掉的僵尸不听 8790，但还占着端口/日志
    _kill_stale_servers()
    time.sleep(0.6)
    return start_service()


def _read_vbs(p):
    """读 run-server.vbs：新版本用 UTF-16（路径有非 ANSI 字符也不会变 "?"），
    老版本是 ANSI/GBK。两种都认，只用来判断里面有没有循环守护标记。"""
    try:
        b = p.read_bytes()
    except OSError:
        return ""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return b.decode("utf-16")
        except Exception:
            return ""
    try:
        return b.decode("mbcs")
    except Exception:
        return b.decode("utf-8", "replace")


def _ensure_supervisor():
    """把老的「一次性」run-server.vbs 升级成「退出就重拉」的循环版。

    老安装包的 vbs 只 sh.Run(...) 一次就退出，计划任务设置的「失败重启」永远不触发
    ——服务一旦退出（Windows 换网络时监听 socket 会被系统作废）就再没人拉它。
    循环版：cmd 退出码非 0（崩溃/被掐）→ 等 5 秒重来；退出码 0（用户点了停止，
    cmd 看到 logs\\.stopped 主动退出）→ 循环结束，交给计划任务下次登录。
    循环开始时会写 logs\\.supervised：服务进程据此判断「自己正在被循环守护」，
    自更新重启时直接退出码 75 交给本脚本重拉，而不是自己拉一个游离进程。
    这里在托盘启动时顺手升级，老用户装完更新包就生效，不用重装。"""
    if os.name != "nt":
        return
    cmd = HERE / "run-server.cmd"
    vbs = HERE / "run-server.vbs"
    if not cmd.is_file():
        return
    cur = _read_vbs(vbs)
    if ".supervised" in cur:
        return
    want = ('Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.CurrentDirectory = "%s"\r\n'
            'If Not fso.FolderExists("%s\\logs") Then fso.CreateFolder("%s\\logs")\r\n'
            "' 循环守护标记：服务进程看到它就知道自己\"有人看着\"，重启时直接退出（码 75）交给本脚本重拉\r\n"
            'fso.CreateTextFile("%s\\logs\\.supervised", True).Close\r\n'
            'Do\r\n'
            '  If fso.FileExists("%s") Then Exit Do\r\n'
            '  rc = sh.Run("cmd /c ""%s""", 0, True)\r\n'
            '  If rc = 0 Then Exit Do\r\n'
            '  WScript.Sleep 5000\r\n'
            'Loop\r\n'
            'On Error Resume Next\r\n'
            'fso.DeleteFile "%s\\logs\\.supervised", True\r\n') % (
                HERE, HERE, HERE, HERE, STOP_FLAG, cmd, HERE)
    try:
        vbs.write_text(want, encoding="utf-16")   # WSH 认 BOM；非 ANSI 路径才不会变 "?"
        _logline("启动脚本已升级为崩溃自动重启版")
    except Exception as e:
        _logline("升级启动脚本失败：%r" % e)


# ---------------------------------------------------------------- 安装 / 卸载脚本

def _find_script(name):
    """安装/卸载脚本：先看安装目录，再看托盘自己所在的目录（= 解压出来的安装包）。"""
    for p in (HERE / name, Path(__file__).resolve().parent / name):
        if p.is_file():
            return p
    return None


def run_script(name, *args):
    """在新控制台窗口里跑 install.ps1 / uninstall.ps1，返回错误串（空 = 已启动）。"""
    p = _find_script(name)
    if not p:
        return "找不到 %s（安装包不完整？）" % name
    cwd = str(p.parent)
    argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)]
    argv += list(args)
    try:
        _spawn_visible(argv)
        return ""
    except Exception as e:
        return "启动失败：%s" % str(e)[:120]


# ---------------------------------------------------------------- 二维码

def _qr_image(data, box=8):
    import qrcode
    qr = qrcode.QRCode(border=2, box_size=box,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#101319", back_color="white")
    return img.get_image() if hasattr(img, "get_image") else img


def pair_url():
    """换一张配对码（没登记就先登记）。返回 (url, code, err)。"""
    base, dev, sec = relay_cfg()
    base = base or DEFAULT_RELAY
    if dev and sec:
        code, out = _run([sys.executable, str(HERE / "server.py"), "--paircode", "--json"],
                         timeout=45)
    else:
        name = os.environ.get("COMPUTERNAME") or "windows"
        code, out = _run([sys.executable, str(HERE / "server.py"), "--enroll", base, name,
                          "--json"], timeout=60)
        if code == 0:
            try:
                restart_service()      # 新登记的设备号要重启服务才生效
            except Exception:
                pass
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("ok") and d.get("pair_url"):
            return d["pair_url"], d.get("pair_code") or "", ""
        return None, "", d.get("error") or "换码失败"
    tail = ((out or "").strip().splitlines() or ["没有输出"])[-1][:120]
    return None, "", "换码失败：%s" % tail


# ---------------------------------------------------------------- Tk 小工具

UI_MUTEX_NAME = "CodebossUIWindow_v1"
TRAY_MUTEX_NAME = "CodebossTrayMutex_v1"     # 托盘守护进程的互斥量（_single_instance）
UI_CMD_FILE = HERE / "logs" / "ui_cmd.json"
# 正在显示的窗口自报家门（pid + 自己加载的版本）：自更新换掉 tray_impl.pyc 后，
# 还开着的旧窗口内存里是旧代码。新进程据此认出旧窗口，让它自己退出后接管。
UI_ALIVE_FILE = HERE / "logs" / "ui_alive.json"
_UI_HANDLE = None

# 和 macOS 主窗口一套配色（见 mac/main.swift 的 C_BG/C_CARD/...）
C_SEL = "#14321f"          # 选中的套餐卡底色


def _is_err(r):
    return isinstance(r, tuple) and r and r[0] == "__err__"


def _bg(root, work, done=None):
    """后台跑 work()，结果回主线程交给 done（Tk 不是线程安全的）。"""
    q = queue.Queue()

    def run():
        try:
            q.put((True, work()))
        except Exception as e:
            q.put((False, str(e)))

    threading.Thread(target=run, daemon=True).start()

    def poll():
        try:
            ok, val = q.get_nowait()
        except queue.Empty:
            try:
                root.after(120, poll)
            except Exception:
                pass
            return
        if done:
            try:
                done(val if ok else ("__err__", val))
            except Exception as e:
                # 以前这里静默吞掉：窗口回调一出错，界面就永远停在「正在读取…」，
                # 用户看到的是「点了没反应」。至少写进 tray.log，方便定位
                _logline("窗口回调异常：%r" % e)

    try:
        root.after(120, poll)
    except Exception:
        pass


def _page_after(root, st, ms, fn):
    """只在当前页还活着时执行定时任务（切页/关窗后自动作废）。"""
    seq = st.get("seq")

    def run():
        if seq != st.get("seq"):
            return
        try:
            if not root.winfo_exists():
                return
            fn()
        except Exception:
            pass

    try:
        root.after(ms, run)
    except Exception:
        pass


def _page_bg(root, st, work, done):
    """_bg 的「页级」版本：切页后后台结果直接丢掉，不对已销毁的控件动手。"""
    seq = st.get("seq")

    def cb(r):
        if seq != st.get("seq"):
            return
        try:
            done(r)
        except Exception as e:
            _logline("页面回调异常：%r" % e)

    _bg(root, work, cb)


def _scroll_frame(parent, wheel=None):
    """可滚动容器（小屏救命）：内容比可视区高时出滚动条。

    wheel 是窗口级共享字典（{"canvas": ...}）：滚轮只在根窗口绑一次
    （见 _wheel_handler），切页重建 canvas 不会串页、也不会越绑越多。
    """
    import tkinter as tk
    outer = tk.Frame(parent, bg=C_BG)
    canvas = tk.Canvas(outer, bg=C_BG, highlightthickness=0, bd=0)
    sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=C_BG)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)

    def _fit(_e=None):
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=canvas.winfo_width())
        except Exception:
            pass

    inner.bind("<Configure>", _fit)
    canvas.bind("<Configure>", _fit)
    if wheel is not None:
        wheel["canvas"] = canvas
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return outer, inner


def _wheel_handler(wheel):
    """根窗口只绑这一次：鼠标在哪个页面上就滚哪个 canvas。"""
    def on_wheel(e):
        c = wheel.get("canvas")
        if c is None:
            return
        try:
            if not c.winfo_exists():
                return
            # 用事件自带的位置判断「鼠标在不在这个页面上」（不要用 winfo_pointerxy：
            # 那是真实光标位置，和这条事件可能不是一回事）
            x, y = e.x_root, e.y_root
            if not (c.winfo_rootx() <= x <= c.winfo_rootx() + c.winfo_width()
                    and c.winfo_rooty() <= y <= c.winfo_rooty() + c.winfo_height()):
                return
            d = -1 if e.delta > 0 else 1
            if abs(e.delta) >= 120:
                d = int(-e.delta / 120)
            c.yview_scroll(d, "units")
        except Exception:
            pass
    return on_wheel


def _scroll_top(w):
    """把这一页滚回顶部。w 是 canvas 里内嵌的 frame，master 就是 canvas。
    换内容（下单页/刷新后的套餐列表）一定要回顶，否则短内容配着旧滚动位置
    就是一片空白，用户以为「点开通没反应」。"""
    try:
        w.master.yview_moveto(0)
    except Exception:
        pass


def _label(root, text, size=11, fg=C_TEXT, bg=C_BG, bold=False, **kw):
    import tkinter as tk
    return tk.Label(root, text=text, fg=fg, bg=bg,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def _button(root, text, cmd, bg=C_BTN, hover=C_BTN_H, fg=C_TEXT, size=10, **kw):
    """扁平按钮。悬浮色记在控件属性上：按钮底色被代码改过之后，
    鼠标移开也能回到「当前」底色，而不是创建时的旧色。"""
    import tkinter as tk
    b = tk.Button(root, text=text, command=cmd, bg=bg, fg=fg, activebackground=hover,
                  activeforeground=fg, relief="flat", bd=0, font=(FONT, size),
                  cursor="hand2", padx=10, pady=5, **kw)
    b._cb_bg = bg
    b._cb_hover = hover
    b.bind("<Enter>", lambda e: _safe_cfg(b, bg=b._cb_hover))
    b.bind("<Leave>", lambda e: _safe_cfg(b, bg=b._cb_bg))
    return b


def _safe_cfg(w, **kw):
    try:
        w.configure(**kw)
    except Exception:
        pass


def _set_btn(b, text=None, bg=None, hover=None, fg=None):
    if b is None:
        return
    try:
        if text is not None:
            b.configure(text=text)
        if bg is not None:
            b.configure(bg=bg)
            b._cb_bg = bg
        if hover is not None:
            b._cb_hover = hover
        if fg is not None:
            b.configure(fg=fg, activeforeground=fg)
    except Exception:
        pass


def _fit_image(pic, maxsize=240):
    """付款码统一缩到 ≤240 并垫一层白底（透明 PNG 在深色窗口里扫不出来）。"""
    from PIL import Image
    pic = pic.convert("RGBA")
    side = max(pic.size)
    if side > maxsize:
        pic = pic.resize((max(1, int(pic.width * maxsize / side)),
                          max(1, int(pic.height * maxsize / side))), Image.LANCZOS)
    bg = Image.new("RGB", pic.size, "white")
    bg.paste(pic, mask=pic.split()[-1])
    return bg


def _qr_photo(data, maxsize=200):
    """按「整数倍放大」画二维码：像素边界清晰，手机才好扫。"""
    from PIL import Image, ImageTk
    img = _qr_image(data, box=1)
    n = max(img.size)
    scale = max(1, int(maxsize // max(1, n)))
    if scale > 1:
        img = img.resize((n * scale, n * scale), Image.NEAREST)
    return ImageTk.PhotoImage(img)


def _icon_small(size):
    """托盘 16/24/32、窗口 54/64 用：缩出对应尺寸，避免细节糊成一团。"""
    try:
        from PIL import Image
        im = _icon_image()
        return im.resize((max(8, int(size)), max(8, int(size))), Image.LANCZOS)
    except Exception:
        return None


def _set_icon(root):
    """所有 Tk 窗口同一个图标（必须留引用，否则 PhotoImage 会被回收）。"""
    try:
        from PIL import ImageTk
        im = _icon_small(64)
        if im is None:
            return
        ph = ImageTk.PhotoImage(im)
        root._cb_icon = ph          # 保命引用
        root.iconphoto(True, ph)
    except Exception:
        pass


def _startfile(path):
    try:
        os.startfile(path)          # noqa: S606 —— 只给用户明确点的「浏览器兜底」用
    except Exception as e:
        _logline("打不开：%s" % str(e)[:100])


def _fmt_dur(m):
    """分钟 → 「45分」/「2时10分」，和 mac 主窗口、手机设置页一个写法。"""
    m = max(0, int(m or 0))
    if m < 60:
        return "%d分" % m
    h, r = divmod(m, 60)
    return ("%d时%d分" % (h, r)) if r else ("%d时" % h)


def _fmt_use(j):
    """套餐用量一句话（首页状态卡用）。

    套餐额度和使用时长包分开写、绝不相加——加总的「总上限」分不清哪部分是套餐、
    哪部分是买的包，用户会以为套餐变大了（中继 use_min_day=0 表示不限时长）。
    拿不到 usage/limits 就返回空串，卡片少显示一行，别写假数。
    """
    j = j or {}
    us = j.get("usage") or {}
    lim = j.get("limits") or {}
    if not us and not lim:
        return ""
    try:
        used = int(us.get("use_min_day") or 0)
        cap = int(lim.get("use_min_day") or 0)
        cred = int(us.get("use_credit_min") or 0)
    except Exception:
        return ""
    if cap == 0:
        out = "不限时长 · 今日已用 " + _fmt_dur(used)
    elif cap < 120:
        # 短额度两边都用分钟，别写成「剩余 45分 / 1时」；也别写「/ 60 分钟」，那行会折行
        out = "剩余 %d/%d分钟" % (max(0, cap - used), cap)
    else:
        out = "剩余 " + _fmt_dur(max(0, cap - used)) + " / " + _fmt_dur(cap)
    if cred > 0:
        out += " · 时长包余 " + _fmt_dur(cred)
    return out


# ---------------------------------------------------------------- 单窗口（首页 / 会话 / 套餐 / 更新）

def _ui_alive_ver():
    """正在显示的窗口自报的版本；没窗口/太久没心跳/读不到都算没有。"""
    try:
        d = json.loads(UI_ALIVE_FILE.read_text(encoding="utf-8"))
        ver = str(d.get("ver") or "")
        ts = float(d.get("ts") or 0)
    except Exception:
        return ""
    return ver if ver and time.time() - ts < 120 else ""


def _ui_ask_old_quit():
    """已经开着的窗口如果是「自更新前」的旧代码，请它退出（它自己会配合）。
    返回 True 表示可以等它释放互斥量后接管。"""
    ver = _ui_alive_ver()
    if not ver or ver == version():
        return False
    try:
        UI_CMD_FILE.parent.mkdir(parents=True, exist_ok=True)
        UI_CMD_FILE.write_text(json.dumps({"cmd": "quit", "ts": time.time()}),
                               encoding="utf-8")
        _logline("旧窗口还是 %s（现在是 %s），请它退出后重建" % (ver, version()))
        return True
    except Exception:
        return False


def _ui_wait_mutex(k32, secs=3.0):
    """等旧窗口退出、互斥量释放后抢过来；抢到返回句柄，超时返回 None。"""
    for _ in range(max(1, int(secs * 4))):
        time.sleep(0.25)
        h = k32.CreateMutexW(None, True, UI_MUTEX_NAME)
        if k32.GetLastError() != 183:
            return h
    return None


def _ui_register(page, act):
    """命名互斥量：同一时间只有一个客户端窗口。

    已经有窗口时：把目标页写进 logs/ui_cmd.json、把旧窗口拉到前台，然后自己退出。
    如果那扇窗口是自更新前的旧代码（版本对不上），先请它退出，由本进程接手——
    否则更新完界面还是旧的，得等用户手动关窗口/重启。
    """
    if os.name != "nt":
        return True, None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, True, UI_MUTEX_NAME)
        if k32.GetLastError() == 183:            # ERROR_ALREADY_EXISTS
            if _ui_ask_old_quit():
                k32.CloseHandle(h)               # 别让这个句柄把旧互斥量续命
                h2 = _ui_wait_mutex(k32)
                if h2:
                    return True, h2
            try:
                UI_CMD_FILE.parent.mkdir(parents=True, exist_ok=True)
                UI_CMD_FILE.write_text(json.dumps(
                    {"page": page, "act": act, "ts": time.time()}), encoding="utf-8")
            except Exception:
                pass
            _ui_activate()
            return False, None
        return True, h
    except Exception:
        return True, None


def _tray_running():
    """托盘进程在不在：在的话窗口点「关闭」只是收进托盘，从托盘图标还能点回来。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenMutexW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenMutexW(0x00100000, False, TRAY_MUTEX_NAME)   # SYNCHRONIZE
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:
        return False


def _ui_activate():
    if os.name != "nt":
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, BRAND)
        if hwnd:
            u.ShowWindow(hwnd, 9)                # SW_RESTORE
            u.SetForegroundWindow(hwnd)
    except Exception:
        pass


# 套餐/升级入口暂时整体关闭（和中继 PLANS_OPEN、App 的 PLANS_OFF 一起开一起关）：
# 窗口里不出现「套餐」页，托盘菜单也不出现「套餐与续费…」。
PLANS_OFF = True
PAGE_NAMES = tuple(p for p in (("home", "首页"), ("sessions", "会话"),
                               ("plans", "套餐"), ("update", "更新"))
                   if not (PLANS_OFF and p[0] == "plans"))


def open_window(page="home", act=""):
    """所有入口（托盘菜单、桌面快捷方式、--qr/--sessions/…）都走这里。"""
    return win_window(page or "home", act or "")


def win_window(page="home", act=""):
    """唯一的客户端窗口：一个窗口、四个页，和 mac 主窗口一个用途。"""
    global _UI_HANDLE
    import tkinter as tk

    ok, h = _ui_register(page, act)
    if not ok:
        return 0
    _UI_HANDLE = h

    root = tk.Tk()
    root.title(BRAND)
    root.configure(bg=C_BG)
    root.resizable(True, True)
    root.minsize(360, 460)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    # 1366×768 / 150% 缩放下 660 高的窗口会被屏幕裁掉，这里按屏幕收
    ww = min(420, max(320, sw - 40))
    hh = min(660, max(400, sh - 80))
    root.geometry("%dx%d+%d+%d" % (ww, hh, (sw - ww) // 2, max(0, (sh - hh) // 3)))
    _set_icon(root)

    wheel = {"canvas": None}
    st = {"page": "", "seq": 0, "canvas": None, "cmd_ts": 0.0, "act": "",
          "goto": None, "qr_done": False, "qr_busy": False, "qr_manual": False,
          "upd_checked": False}
    st["ver0"] = version()          # 本进程加载的版本：自更新后和磁盘上的对不上

    # 心跳：把「我是哪个版本在显示」写给后面启动的进程。自更新换了 tray_impl.pyc
    # 之后，新进程靠它认出这扇旧窗口，请它退出后重建（否则界面永远是旧的）。
    def _alive_tick():
        try:
            UI_ALIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
            UI_ALIVE_FILE.write_text(json.dumps(
                {"pid": os.getpid(), "ver": st["ver0"], "ts": time.time()}),
                encoding="utf-8")
        except Exception:
            pass
        try:
            root.after(10000, _alive_tick)
        except Exception:
            pass

    _alive_tick()

    # 品牌行（图标 + 名称 + 版本 + 一句简介）
    head = tk.Frame(root, bg=C_BG)
    head.pack(fill="x", padx=16, pady=(14, 8))
    try:
        from PIL import ImageTk
        logo_img = _icon_small(54)
        if logo_img is not None:
            logo = ImageTk.PhotoImage(logo_img)
            lab = tk.Label(head, image=logo, bg=C_BG, bd=0)
            lab.image = logo
            lab.pack(side="left", padx=(0, 10))
    except Exception:
        pass
    tt = tk.Frame(head, bg=C_BG)
    tt.pack(side="left")
    _label(tt, BRAND, 15, bold=True).pack(anchor="w")
    verlab = _label(tt, "手机派活，电脑干活　·　v" + (version() or "?"), 9, fg=C_MUTED)
    verlab.pack(anchor="w")
    # 版本标签跟着磁盘上的 VERSION 走：服务自更新后 tray 进程还跑着旧代码，
    # 以前这行永远停在窗口创建时的版本号（用户看到「更新完版本号不变」）。
    st["verlab"] = verlab
    st["ver_disk"] = version()

    def _diskver_tick():
        try:
            v = version()
            if v and v != st.get("ver_disk"):
                st["ver_disk"] = v
                st["verlab"].configure(text="手机派活，电脑干活　·　v" + v)
        except Exception:
            pass
        try:
            root.after(10000, _diskver_tick)
        except Exception:
            pass

    _diskver_tick()

    # 顶部分段导航
    nav_bar = tk.Frame(root, bg=C_BG)
    nav_bar.pack(fill="x", padx=16, pady=(0, 8))
    nav = {}
    for k, nm in PAGE_NAMES:
        b = _button(nav_bar, nm, lambda k=k: show(k), size=10)
        b.pack(side="left", fill="x", expand=True, padx=(0, 5))
        nav[k] = b
    nav[PAGE_NAMES[-1][0]].pack_configure(padx=0)

    tk.Frame(root, bg=C_LINE, height=1).pack(fill="x", padx=16)

    outer, body = _scroll_frame(root, wheel)
    outer.pack(fill="both", expand=True, pady=(8, 0))

    def show(p, a=""):
        if p not in dict(PAGE_NAMES):
            p = "home"
        st["page"] = p
        st["act"] = a or ""
        st["seq"] += 1
        for w in body.winfo_children():
            w.destroy()
        for k, b in nav.items():
            on = (k == p)
            _set_btn(b, bg=(C_GREEN if on else C_BTN),
                     hover=(C_GREEN_H if on else C_BTN_H),
                     fg=("white" if on else C_TEXT))
        try:
            wheel["canvas"].yview_moveto(0)
        except Exception:
            pass
        builder = {"home": page_home, "sessions": page_sessions,
                   "plans": page_plans, "update": page_update}.get(p) or page_home
        try:
            builder(root, st, body)
        except Exception as e:
            _logline("页 %s 画不出来：%r" % (p, e))
            _label(body, "页面出错了：%s" % str(e)[:140], 9, fg=C_RED,
                   wraplength=300, justify="left").pack(anchor="w", padx=16, pady=12)

    st["goto"] = show

    # 其它进程（托盘菜单/快捷方式）要求切页：500ms 轮询一次
    try:
        d0 = json.loads(UI_CMD_FILE.read_text(encoding="utf-8"))
        st["cmd_ts"] = float(d0.get("ts") or 0)
    except Exception:
        st["cmd_ts"] = 0.0

    def poll_cmd():
        try:
            if not root.winfo_exists():
                return
        except Exception:
            return
        try:
            if UI_CMD_FILE.exists():
                d = json.loads(UI_CMD_FILE.read_text(encoding="utf-8"))
                ts = float(d.get("ts") or 0)
                if ts > st["cmd_ts"]:
                    st["cmd_ts"] = ts
                    if d.get("cmd") == "quit":
                        # 新进程发现这扇窗口是自更新前的旧代码，请它让位
                        v = version()
                        if v and st.get("ver0") and v != st["ver0"]:
                            _logline("程序已更新 %s → %s，旧窗口退出让新进程接管"
                                     % (st["ver0"], v))
                            try:
                                root.destroy()
                            except Exception:
                                pass
                            return
                    else:
                        # 窗口可能被收进托盘了：先恢复再切页
                        try:
                            if root.state() == "withdrawn":
                                root.deiconify()
                            root.lift()
                        except Exception:
                            pass
                        show(str(d.get("page") or "home"), str(d.get("act") or ""))
                        _ui_activate()
        except Exception:
            pass
        try:
            root.after(500, poll_cmd)
        except Exception:
            pass

    root.bind("<MouseWheel>", _wheel_handler(wheel))

    def on_close():
        # 右上角 × 默认收进托盘（程序和服务继续跑）；托盘没在跑才真的退出
        if _tray_running():
            _logline("窗口收进托盘，程序继续在后台运行")
            try:
                root.withdraw()
            except Exception:
                pass
            return
        try:
            root.destroy()
        except Exception:
            pass

    try:
        root.protocol("WM_DELETE_WINDOW", on_close)
    except Exception:
        pass
    show(page, act)
    root.after(500, poll_cmd)
    root.mainloop()
    return 0


# ---------------------------------------------------------------- 页：首页

def page_home(root, st, body):
    import tkinter as tk

    # 每次进首页重新判断：二维码一律默认收起，点「显示配对二维码」才展开（本次打开页面的临时意愿）
    st["qr_done"] = False
    st["qr_manual"] = False
    st["prev_enrolled"] = None

    # 状态卡
    card = tk.Frame(body, bg=C_CARD)
    card.pack(fill="x", padx=16, pady=(6, 10))
    top = tk.Frame(card, bg=C_CARD)
    top.pack(fill="x", padx=12, pady=(12, 2))
    st_line = _label(top, "服务　读取中…", 12, bg=C_CARD, bold=True)
    st_line.pack(side="left")
    _label(top, "v" + (version() or "?"), 9, fg=C_MUTED, bg=C_CARD).pack(side="right", pady=(3, 0))
    info = _label(card, "", 9, fg=C_MUTED, bg=C_CARD, justify="left", anchor="w", wraplength=300)
    info.pack(fill="x", padx=12, pady=(0, 4))
    say_lbl = _label(card, "", 9, fg=C_MUTED, bg=C_CARD, justify="left", anchor="w", wraplength=300)
    say_lbl.pack(fill="x", padx=12)
    bar = tk.Frame(card, bg=C_CARD)
    bar.pack(fill="x", padx=12, pady=(8, 12))
    primary = _button(bar, "显示配对二维码", None, bg=C_GREEN, hover=C_GREEN_H, fg="white", size=11)
    primary.pack(side="left", fill="x", expand=True)
    toggle_b = _button(bar, "停止服务", None, size=9)
    toggle_b.pack(side="left", padx=(8, 0))

    def say(msg):
        _safe_cfg(say_lbl, text=msg or " ")

    # 配对二维码卡：默认不显示。还没接入时自动出现；已经接入就收起来，只有点
    # 「显示配对二维码」才展开——已接入还一直摆着二维码既占地方，每次进首页
    # 还会白跑一次换码（老实现每 20 秒刷新也会重新生成一张）。
    qr_card = tk.Frame(body, bg=C_CARD)
    _label(qr_card, "配对二维码", 11, bg=C_CARD, bold=True).pack(anchor="w", padx=12, pady=(10, 0))
    _label(qr_card, "手机 %s App →「接入开发机」→「📷 扫码接入」" % _app_name(), 9,
           fg=C_MUTED, bg=C_CARD, wraplength=300, justify="left").pack(anchor="w", padx=12,
                                                                      pady=(2, 6))
    holder = _label(qr_card, "正在生成…", 9, fg=C_MUTED, bg=C_CARD, wraplength=300)
    holder.pack(pady=(2, 4))
    code_lbl = _label(qr_card, "", 10, bg=C_CARD, wraplength=300)
    code_lbl.pack()
    qr_btn = _button(qr_card, "刷新二维码", None, size=9)
    qr_btn.pack(pady=(6, 12))

    def draw_qr(u, c, err=""):
        if err:
            holder.configure(image="", text=err, fg=C_RED)
            code_lbl.configure(text="")
            return
        try:
            ph = _qr_photo(u, 200)
        except Exception as e:
            # 缺 Pillow/qrcode：以前会永远停在「正在生成…」，现在至少告诉人怎么办
            holder.configure(image="", text="二维码画不出来：%s" % str(e)[:100], fg=C_RED)
            code_lbl.configure(text=("配对码 %s" % c) if c else "")
            return
        holder._cb_qr = ph              # 保住引用，否则图会被回收
        holder.configure(image=ph, text="")
        code_lbl.configure(text=("配对码 %s（15 分钟内有效）" % c) if c else "15 分钟内有效",
                           fg=C_TEXT)

    def gen_qr(*_):
        if st.get("qr_busy"):
            return                  # 连点刷新会同时跑两次 --enroll，服务被反复重启
        st["qr_busy"] = True
        st["qr_done"] = True
        seq = st["seq"]
        _set_btn(qr_btn, text="正在生成…")
        _safe_cfg(qr_btn, state="disabled")
        holder.configure(image="", text="正在生成…", fg=C_MUTED)
        code_lbl.configure(text="")

        def done(r):
            _safe_cfg(qr_btn, state="normal")
            _set_btn(qr_btn, text="刷新二维码")
            if _is_err(r):
                draw_qr("", "", "换码失败：%s" % str(r[1])[:100])
                return
            u, c, err = r
            draw_qr(u, c, err)

        def cb(r):
            # 生成过程中切了页也要把 busy 清掉，否则回来点「刷新」永远没反应
            st["qr_busy"] = False
            if seq != st["seq"]:
                return
            try:
                done(r)
            except Exception as e:
                _logline("二维码回调异常：%r" % e)

        _bg(root, pair_url, cb)

    qr_btn.configure(command=gen_qr)

    def reveal_qr():
        """把二维码卡放出来（放在服务卡前面）。"""
        if not qr_card.winfo_manager():
            qr_card.pack(fill="x", padx=16, pady=(0, 10), before=svc)

    def hide_qr():
        if qr_card.winfo_manager():
            qr_card.pack_forget()

    # 服务卡（重新安装 / 卸载的二次确认都在这里，不再单开窗口）
    svc = tk.Frame(body, bg=C_CARD)
    svc.pack(fill="x", padx=16, pady=(0, 10))

    def render_svc(kind=""):
        for w in svc.winfo_children():
            w.destroy()
        _label(svc, "服务", 11, bg=C_CARD, bold=True).pack(anchor="w", padx=12, pady=(10, 6))
        if not kind:
            row = tk.Frame(svc, bg=C_CARD)
            row.pack(fill="x", padx=12, pady=(0, 12))
            _button(row, "重新安装（覆盖，保留数据）",
                    lambda: render_svc("reinstall"), size=9).pack(fill="x", pady=2)
            _button(row, "卸载服务", lambda: render_svc("uninstall"), size=9).pack(fill="x", pady=2)
            return
        if kind == "reinstall":
            _label(svc, "用当前程序文件覆盖安装目录里的程序（token、会话记录、配置都保留），"
                        "装完服务会自动重启。\n会另开一个窗口显示安装进度。",
                   9, fg=C_MUTED, bg=C_CARD, justify="left", wraplength=300).pack(anchor="w", padx=12)
            row = tk.Frame(svc, bg=C_CARD)
            row.pack(fill="x", padx=12, pady=(8, 12))

            def do_reinstall():
                err = run_script("install.ps1")
                say("✗ " + err if err else "→ 已在新窗口里开始安装…装完点「刷新状态」")
                _page_after(root, st, 1500, refresh_all)

            _button(row, "覆盖安装", do_reinstall, bg=C_GREEN, hover=C_GREEN_H,
                    fg="white", size=9).pack(side="left")
            _button(row, "取消", lambda: render_svc(""), size=9).pack(side="left", padx=8)
            return

        # 卸载：「彻底删除」紧跟「只卸载」是手滑高发区——第一次点只进入确认态
        _label(svc, "卸载后手机将无法连接这台电脑。\n只卸载：保留 token、会话记录、日志和配置\n"
                    "彻底删除：连虚拟环境、数据和密钥一起清掉（不可恢复）",
               9, fg=C_MUTED, bg=C_CARD, justify="left", wraplength=300).pack(anchor="w", padx=12)
        warn = _label(svc, "", 9, fg=C_RED, bg=C_CARD, justify="left", wraplength=300)
        warn.pack(anchor="w", padx=12)
        armed = {"v": False}
        row = tk.Frame(svc, bg=C_CARD)
        row.pack(fill="x", padx=12, pady=(8, 12))

        def do_uninstall(purge=False, btn=None):
            if purge and not armed["v"]:
                armed["v"] = True
                warn.configure(text="再点一次「彻底删除」才会真的删掉数据（不可恢复）。"
                                    "只想换新环境？用左边「只卸载（保留数据）」。")
                _set_btn(btn, text="确认彻底删除（不可恢复）", bg="#7a2c2c", hover="#8d3333")
                return
            err = run_script("uninstall.ps1", *(("-Purge",) if purge else ()))
            warn.configure(text=("✗ " + err) if err else "→ 已在新窗口里开始卸载…",
                           fg=C_RED if err else C_GREEN_H)

        _button(row, "只卸载（保留数据）", lambda: do_uninstall(False), size=9).pack(side="left")
        pb = _button(row, "彻底删除", None, bg="#5a2d2d", hover="#6d3636", size=9)
        pb.configure(command=lambda: do_uninstall(True, pb))
        pb.pack(side="left", padx=6)
        _button(row, "取消", lambda: render_svc(""), size=9).pack(side="left")

    # 最近日志卡（只读；「打开日志」入口已去掉，后续做日志上报）
    lg = tk.Frame(body, bg=C_CARD)
    lg.pack(fill="x", padx=16, pady=(0, 16))
    _label(lg, "最近日志", 11, bg=C_CARD, bold=True).pack(anchor="w", padx=12, pady=(10, 4))
    txt = tk.Text(lg, height=7, bg="#0e1218", fg=C_MUTED, relief="flat", bd=0,
                  font=("Consolas", 8), padx=8, pady=6, wrap="word")
    txt.pack(fill="x", padx=12, pady=(0, 12))
    txt.configure(state="disabled")

    def load_log():
        f = HERE / "logs" / "server.log"
        if not f.exists():
            f = LOG_FILE
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
        except Exception as e:
            lines = ["读不到日志：%s" % str(e)[:80]]
        try:
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("end", "\n".join(lines) or "（还没有日志）")
            txt.see("end")
            txt.configure(state="disabled")
        except Exception:
            pass

    # 状态刷新
    st_hold = {"st": ""}

    def refresh_all():
        def work():
            s = service_state()
            d = {"st": s, "ver": version()}
            base, dev, sec = relay_cfg()
            d["enrolled"] = bool(dev and sec)
            if s in ("running", "running_disabled"):
                try:
                    d["sess"] = len((api_get("/api/sessions", timeout=8) or {}).get("sessions") or [])
                except Exception:
                    d["sess"] = None
                try:
                    p = api_get("/api/plan", timeout=8)
                    d["plan"] = (p or {}).get("name") or (p or {}).get("plan") or ""
                    d["use"] = _fmt_use(p or {})
                except Exception:
                    d["plan"] = ""
                    d["use"] = ""
            return d

        def done(r):
            if _is_err(r):
                say("✗ 读取状态失败：%s" % str(r[1])[:80])
                return
            s = r["st"]
            st_hold["st"] = s
            txt_map = {
                "running": ("● 运行中", C_GREEN_H),
                "running_disabled": ("● 运行中（已停用开机自启）", C_GREEN_H),
                "stopped": ("○ 已停止", C_RED),
                "disabled": ("○ 已停止（开机不自启）", C_RED),
                "missing": ("○ 未安装服务", C_MUTED),
            }
            t, fg = txt_map.get(s, ("? 状态未知", C_MUTED))
            st_line.configure(text="服务　" + t, fg=fg)
            bits = ["中继　" + ("已登记" if r.get("enrolled") else "未登记")]
            if r.get("sess") is not None:
                bits.append("会话　%d 个" % r["sess"])
            if r.get("plan"):
                bits.append("套餐　%s" % r["plan"])
            if r.get("use"):
                bits.append(r["use"])
            # 每行两项、显式换行：纯靠 wraplength 折行会从「会话 0 / 个」中间断开
            info.configure(text="\n".join("　·　".join(bits[i:i + 2])
                                          for i in range(0, len(bits), 2)))
            if s == "missing":
                _set_btn(primary, text="安装 BossHost 服务")
            elif s in ("stopped", "disabled"):
                _set_btn(primary, text="启动服务")
            else:
                # 已接入也一样：平时不摆二维码，点这个按钮才展开
                _set_btn(primary, text="显示配对二维码")
            _set_btn(toggle_b, text=("停止服务（开机不再自启）"
                                     if s in ("running", "running_disabled")
                                     else "启动服务（恢复开机自启）"))
            # 二维码只在「还没接入」或用户主动点「显示配对二维码」时出现；已接入就收起
            enrolled = bool(r.get("enrolled"))
            if enrolled and not st.get("prev_enrolled"):
                st["qr_manual"] = False     # 刚扫完码接入成功：二维码自动收起
            st["prev_enrolled"] = enrolled
            wanted = (not enrolled) or bool(st.get("qr_manual"))
            if wanted:
                reveal_qr()
            else:
                hide_qr()
            if s in ("running", "running_disabled"):
                if wanted and not st.get("qr_done"):
                    gen_qr()
            elif wanted and not st.get("qr_done"):
                holder.configure(image="", text="服务没在运行，扫了也连不上。先启动服务。",
                                 fg=C_RED, wraplength=300)
                code_lbl.configure(text="")
            load_log()

        _page_bg(root, st, work, done)

    def do_toggle():
        s = st_hold.get("st") or ""
        if s == "missing":
            home_primary()
            return
        stop = s in ("running", "running_disabled")
        say("→ 正在%s…" % ("停止" if stop else "启动"))

        def done(r):
            if _is_err(r):
                say("✗ %s" % str(r[1])[:100])
            else:
                ok, err = r
                say("✓ 服务已%s" % ("停止（开机不再自启）" if stop else "启动") if ok
                    else "✗ " + (err or "启动失败"))
            _page_after(root, st, 300, refresh_all)

        _page_bg(root, st, (stop_service if stop else start_service), done)

    def home_primary():
        s = st_hold.get("st") or ""
        if s == "missing":
            err = run_script("install.ps1")
            say("✗ " + err if err else "→ 已在新窗口里开始安装…装完点「刷新状态」")
            _page_after(root, st, 1500, refresh_all)
        elif s in ("stopped", "disabled"):
            do_toggle()
        else:
            # 已经接入过：用户点「显示配对二维码」才展开（换手机/重新接入）
            st["qr_manual"] = True
            reveal_qr()
            gen_qr()
            _page_after(root, st, 300, refresh_all)

    primary.configure(command=home_primary)
    toggle_b.configure(command=do_toggle)
    render_svc(st.get("act") or "")
    st["act"] = ""
    refresh_all()
    _page_after(root, st, 20000, refresh_all)


# ---------------------------------------------------------------- 页：会话（只看列表，不开浏览器）

def page_sessions(root, st, body):
    import tkinter as tk

    head = tk.Frame(body, bg=C_BG)
    head.pack(fill="x", padx=16, pady=(6, 8))
    cnt = _label(head, "读取中…", 9, fg=C_MUTED, wraplength=250, justify="left")
    cnt.pack(side="left")
    _button(head, "刷新", lambda: load(False), size=9).pack(side="right")

    box = tk.Frame(body, bg=C_BG)
    box.pack(fill="x", padx=16, pady=(0, 16))

    def load(auto=True):
        if not auto:
            cnt.configure(text="读取中…", fg=C_MUTED)

        def work():
            if not alive(1.5):
                raise RuntimeError("服务没在运行")
            return api_get("/api/sessions", timeout=10)

        def done(r):
            for w in box.winfo_children():
                w.destroy()
            if _is_err(r):
                cnt.configure(text="读不到会话：%s" % str(r[1])[:60], fg=C_RED)
                return
            ss = (r or {}).get("sessions") or []
            if not ss:
                cnt.configure(text="没有会话。在这台电脑上敲 claude 就会出现在这里",
                              fg=C_MUTED)
                return
            live = sum(1 for s in ss if s.get("alive"))
            cnt.configure(text="共 %d 个会话（%d 个在跑）· 在手机上打开查看" % (len(ss), live),
                          fg=C_MUTED)
            home = os.path.expanduser("~")
            for s in ss[:200]:
                card = tk.Frame(box, bg=C_CARD)
                card.pack(fill="x", pady=2)
                top = tk.Frame(card, bg=C_CARD)
                top.pack(fill="x", padx=10, pady=(8, 0))
                nm = (s.get("name") or s.get("sid") or "")[:34]
                _label(top, nm or "(未命名)", 10, bg=C_CARD).pack(side="left")
                if s.get("alive"):
                    stxt = "● 运行中" + (" %d 端" % s["clients"] if s.get("clients") else "")
                    fg = C_GREEN_H
                else:
                    stxt, fg = "已退出", C_MUTED
                _label(top, stxt, 9, fg=fg, bg=C_CARD).pack(side="right")
                cwd = s.get("cwd") or ""
                if home and cwd.lower().startswith(home.lower()):
                    cwd = "~" + cwd[len(home):]
                _label(card, cwd or " ", 8, fg=C_MUTED, bg=C_CARD, anchor="w",
                       wraplength=300, justify="left").pack(fill="x", padx=10, pady=(0, 8))

        _page_bg(root, st, work, done)

    def tick():
        load(True)
        _page_after(root, st, 6000, tick)

    load(False)
    _page_after(root, st, 6000, tick)


# ---------------------------------------------------------------- 页：套餐与续费

def _sku_tier(k, s):
    t = str((s or {}).get("tier") or "")
    if t in ("pack", "pro", "max"):
        return t
    # 老中继没有 tier：时长包 days<=0，其余按 max_ 前缀分档
    try:
        if int((s or {}).get("days") or 0) <= 0:
            return "pack"
    except Exception:
        return "pack"
    return "max" if str(k).startswith("max") else "pro"


def _sku_sort_key(k, s):
    try:
        return int((s or {}).get("days") or 0) or int((s or {}).get("min") or 0)
    except Exception:
        return 0


def _sku_min(s):
    try:
        return int((s or {}).get("min") or 0)
    except Exception:
        return 0


def _sku_days(s):
    try:
        return int((s or {}).get("days") or 0)
    except Exception:
        return 0


def _plan_lim(plans, tier, key):
    try:
        return int(((plans.get(tier) or {}).get("limits") or {}).get(key) or 0)
    except Exception:
        return 0


def _sku_unit(k, s, plans):
    """折合单价（元/分钟），只用来比价，不是使用承诺。

    订阅按「天数 × 每天可用分钟」算，Max 不限时长按 24 小时/天折算；
    时长包按包内总分钟。和 App 里 /r/plans 的价格表一个口径。
    """
    try:
        money = float((s or {}).get("money") or 0)
    except Exception:
        return 0.0
    mins = _sku_min(s)
    if not mins:
        per_day = _plan_lim(plans, _sku_tier(k, s), "use_min_day") or 1440
        mins = _sku_days(s) * per_day
    return (money / mins) if mins > 0 else 0.0


def _sku_off(k, s, base_k, sk, plans):
    """同档相对基础档的单价变动（%）：正=便宜，负=更贵。"""
    u = _sku_unit(k, s, plans)
    u0 = _sku_unit(base_k, sk.get(base_k) or {}, plans)
    if u <= 0 or u0 <= 0:
        return 0.0
    return (1.0 - u / u0) * 100.0


def _off_txt(pct):
    """折扣文案；后台把大档调得比基础档还贵时如实写「贵 X%」，不装成省 0%。"""
    if abs(pct) < 0.05:
        return ""
    return ("省 %.1f%%" % pct) if pct > 0 else ("贵 %.1f%%" % -pct)


def _pack_dur(mins):
    """时长包时长：6 小时 / 90 分钟。"""
    mins = int(mins or 0)
    if mins >= 60 and mins % 60 == 0:
        return "%d 小时" % (mins // 60)
    return "%d 分钟" % mins


def _plan_feat_lines(tier, plans):
    """档位权益，和 App 里 /r/plans 的卡片同一套写法。

    全部取自中继下发的 plans（plans.json 的原始配置）；拿不到这块数据时返回空，
    宁可不显示也不能编「不限时 / 不限量」——假权益比看不到更糟。
    """
    p = (plans or {}).get(tier) or {}
    lim = p.get("limits") or {}
    fea = p.get("features") or {}
    if not lim and not fea:
        return []

    def iv(n):
        try:
            return int(lim.get(n) or 0)
        except Exception:
            return 0

    out = []
    h, c = iv("hosts"), iv("clients")
    if h > 0:
        out.append(("开发机 %d 台" % h, True))
    if c > 0:
        out.append(("客户端 %d 个（24 小时内活跃）" % c, True))
    um = iv("use_min_day")
    if um <= 0:
        out.append(("远程使用：不限时长", True))
    elif um % 60 == 0:
        out.append(("每天远程使用 %d 小时" % (um // 60), True))
    else:
        out.append(("每天远程使用 %d 分钟" % um, True))
    llm = iv("llm_day")
    out.append(("模型调用 %s（语音概括 / 命令识别共用）"
                % ("不限" if llm <= 0 else "%d 次/天" % llm), True))
    out.append(("命令终端 · 新建会话和终端", bool(fea.get("terminal"))))
    out.append(("语音转写不限量（计入使用时长）", True))
    out.append(("进度提醒：卡住 / 完成主动推手机", bool(fea.get("remind"))))
    out.append(("自动化：语音识别、AI 总结、进度识别", bool(fea.get("auto"))))
    return out


def page_plans(root, st, body):
    import tkinter as tk

    hint = _label(body, "正在读取套餐…", 9, fg=C_MUTED, wraplength=340,
                  justify="left", anchor="w")
    hint.pack(fill="x", padx=16, pady=(6, 2))
    content = tk.Frame(body, bg=C_BG)
    content.pack(fill="x", pady=(0, 16))

    # 三组各自记选中的档位（Pro / Max 订阅、使用时长包），和 App 的 SEL 一致
    sel = {"pro": "", "max": "", "pack": "", "type": "alipay"}
    buy_btns = {}          # tier -> 下单按钮
    chip_btns = {}         # (tier, sku) -> 时长选项按钮
    cur_label = {}         # tier -> 下单按钮当前文案（下单失败时恢复）
    otn = {"v": ""}
    plan_base = {"v": ""}
    paid = {"v": False}

    def clear():
        for w in content.winfo_children():
            w.destroy()
        buy_btns.clear()
        chip_btns.clear()
        cur_label.clear()

    def open_account_page():
        base = plan_base["v"] or relay_cfg()[0] or DEFAULT_RELAY
        _startfile(base.rstrip("/") + "/r/plans")

    def set_type(t):
        # 微信渠道暂未开通：界面先只渲染支付宝；这个函数、sel["type"] 和中继里的
        # wxpay 分支都原样保留，开通后加回渠道按钮即可。
        sel["type"] = t

    def build(j):
        clear()
        if _is_err(j):
            hint.configure(text="读不到套餐：%s" % str(j[1])[:100], fg=C_RED)
            _button(content, "重试", load, size=9).pack(fill="x", padx=16, pady=6)
            _button(content, "在浏览器打开套餐页", open_account_page,
                    size=9).pack(fill="x", padx=16, pady=2)
            return
        if not (j and j.get("ok")):
            hint.configure(text=(j or {}).get("error") or "读不到套餐：服务没运行或还没接入中继",
                           fg=C_RED)
            _button(content, "重试", load, size=9).pack(fill="x", padx=16, pady=6)
            _button(content, "在浏览器打开套餐页", open_account_page,
                    size=9).pack(fill="x", padx=16, pady=2)
            return
        acc = j.get("account") or {}
        an = acc.get("name") or ""
        ph = acc.get("phone") or ""
        em = acc.get("email") or ""
        pn = j.get("name") or j.get("plan") or "—"
        plan = str(j.get("plan") or "free")
        cur = str(j.get("sku") or "") if plan != "free" else ""
        # 体验月限一次：支付成功过就永久置灰（到期回免费版时 cur 为空，不能再靠它判断）
        trial_used = bool(j.get("trial_used"))
        plan_base["v"] = (j.get("relay_base") or "").rstrip("/")
        head = ("%s · %s" % (an, ph or em)) if an else "未绑定账号"
        curtxt = pn
        try:
            exp = float(j.get("expires") or 0)
        except Exception:
            exp = 0.0
        if exp > 0 and plan != "free":
            curtxt += " · 至 " + time.strftime("%Y-%m-%d", time.localtime(exp))
        use = _fmt_use(j)
        note = "　✓ 已到账，套餐已更新" if paid["v"] else ""
        paid["v"] = False
        hint.configure(text="%s　当前：%s%s%s" % (head, curtxt,
                                             ("　" + use) if use else "", note),
                       fg=C_GREEN_H if note else C_MUTED)

        sk = j.get("skus") or {}
        plans = j.get("plans") or {}
        if not sk:
            hint.configure(text="中继没有下发套餐（稍后再试）", fg=C_MUTED)
            return
        # 档位/价格/时长全部按中继下发走：后台加档、改价、下架都不用改客户端
        subs = {"pro": [], "max": []}
        packs = []
        for k, s in sk.items():
            if not isinstance(s, dict) or s.get("sell") is False:
                continue
            t = _sku_tier(k, s)
            if t == "pack":
                packs.append(k)
            elif t in subs:
                subs[t].append(k)
        for t in subs:
            subs[t].sort(key=lambda k: _sku_sort_key(k, sk[k]))
        packs.sort(key=lambda k: _sku_sort_key(k, sk[k]))

        SUF = {"trial": "体验月 · 限一次", "month": "月付",
               "quarter": "季付", "year": "年付"}

        def suf(k):
            return SUF.get(str(k).split("_")[-1],
                           str((sk.get(k) or {}).get("name") or k))

        def is_trial(k):
            return str(k) == "trial" or str(k).endswith("_trial")

        def base_of(tier):
            """基础档（比价基准 + 默认选中）：优先月付，没有就取最短那档。"""
            keys = subs.get(tier) or []
            m = "max_month" if tier == "max" else "month"
            return m if m in keys else (keys[0] if keys else "")

        def rank(k):
            """比高低：Max 永远高于 Pro，同档再比天数。"""
            t = _sku_tier(k, sk.get(k) or {})
            return (100000 if t == "max" else 0) + _sku_days(sk.get(k) or {})

        def money_of(k):
            return str((sk.get(k) or {}).get("money") or "")

        def buy_text(k):
            """按钮写清楚这一单是什么：开通 / 续费 / 升级 / 预约降档（哪天生效）。"""
            money = money_of(k)
            q = (j.get("quotes") or {}).get(k) or {}
            try:
                qp, full = float(q.get("pay") or 0), float(money or 0)
            except Exception:
                qp, full = 0.0, 0.0
            if qp > 0 and qp < full:
                cr = str(q.get("credit") or "").rstrip("0").rstrip(".")
                return "升级 · 补差价 ¥%.2f（已抵扣 ¥%s）" % (qp, cr)
            if cur:
                if rank(k) < rank(cur):
                    day = ""
                    try:
                        day = time.strftime("%Y-%m-%d",
                                            time.localtime(float(j.get("expires") or 0)))
                    except Exception:
                        pass
                    return ("预约 · ¥%s（%s 起生效）" % (money, day)) if day else \
                        ("预约 · ¥%s（到期后生效）" % money)
                if k == cur:
                    return "续费 · ¥%s" % money
                return "升级 · ¥%s" % money
            return "立即开通 · ¥%s" % money

        def pick(tier, k):
            sel[tier] = k
            for (t, key), b in chip_btns.items():
                if t != tier:
                    continue
                on = (key == k)
                _set_btn(b, bg=(C_SEL if on else C_BTN),
                         hover=(C_SEL if on else C_BTN_H),
                         fg=(C_GREEN_H if on else C_TEXT))
            lab = buy_text(k)
            cur_label[tier] = lab
            _set_btn(buy_btns.get(tier), text=lab)

        def pick_pack(k):
            sel["pack"] = k
            for (t, key), b in chip_btns.items():
                if t != "pack":
                    continue
                on = (key == k)
                _set_btn(b, bg=(C_SEL if on else C_BTN),
                         hover=(C_SEL if on else C_BTN_H),
                         fg=(C_GREEN_H if on else C_TEXT))
            lab = "立即购买 · ¥" + money_of(k)
            cur_label["pack"] = lab
            _set_btn(buy_btns.get("pack"), text=lab)
            _safe_cfg(buy_btns.get("pack"), state="normal")

        def chips(card, tier):
            """订阅时长选项：体验月 / 月付 / 季付 / 年付，折扣跟同档月付比。"""
            keys = subs.get(tier) or []
            if not keys:
                return
            grid = tk.Frame(card, bg=C_CARD)
            grid.pack(fill="x", padx=10, pady=(6, 2))
            grid.columnconfigure(0, weight=1)
            grid.columnconfigure(1, weight=1)
            base = base_of(tier)
            for i, k in enumerate(keys):
                off = ""
                if k != base and not is_trial(k):
                    off = _off_txt(_sku_off(k, sk.get(k) or {}, base, sk, plans))
                txt = "¥%s\n%s%s" % (money_of(k), suf(k),
                                     ("　" + off) if off else "")
                b = _button(grid, txt, lambda k=k, t=tier: pick(t, k), size=8,
                            justify="center")
                b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
                # 体验月每个账号限一次：已付费（含以前买过、现在已到期）就不给再买
                if (cur or trial_used) and is_trial(k):
                    _safe_cfg(b, state="disabled", disabledforeground="#5b636d")
                chip_btns[(tier, k)] = b

        def plan_card(tier, badge=""):
            p = plans.get(tier) or {}
            keys = subs.get(tier) or []
            if not p and not keys:
                return
            card = tk.Frame(content, bg=C_CARD, highlightthickness=1,
                            highlightbackground=C_LINE)
            card.pack(fill="x", padx=16, pady=(2, 6))
            top = tk.Frame(card, bg=C_CARD)
            top.pack(fill="x", padx=12, pady=(10, 0))
            _label(top, p.get("name") or tier, 12, bg=C_CARD,
                   bold=True).pack(side="left")
            if badge:
                _label(top, badge, 8, fg="#1b1b1b",
                       bg=C_YELLOW).pack(side="right", padx=(6, 0))
            if tier == "free":
                price = "¥0 / 永久"
            else:
                m = money_of(base_of(tier))
                price = ("¥%s / 月起" % m) if m else ""
            if price:
                _label(top, price, 9, bg=C_CARD, fg=C_MUTED).pack(side="right")
            notes = {
                "free": "够用就免费，不绑卡、不限期。",
                "pro": "多机、多说，还多了进度提醒和自动化。",
                "max": "不限时长，额度拉满，适合重度远程开发。",
            }
            _label(card, notes.get(tier, ""), 8, fg=C_MUTED, bg=C_CARD,
                   wraplength=330, justify="left", anchor="w").pack(
                fill="x", padx=12, pady=(3, 4))
            for txt, ok in _plan_feat_lines(tier, plans):
                _label(card, ("✓ " if ok else "– ") + txt, 8,
                       fg=(C_GREEN_H if ok else "#5b636d"), bg=C_CARD,
                       wraplength=330, justify="left", anchor="w").pack(
                    fill="x", padx=12)
            if tier == "free":
                b = _button(card, "免费使用中" if plan == "free"
                            else "到期后自动回到免费版", lambda: None, size=9)
                _safe_cfg(b, state="disabled", disabledforeground="#5b636d")
                b.pack(fill="x", padx=12, pady=(8, 10))
                return
            chips(card, tier)
            b = _button(card, "立即开通", lambda t=tier: pay_now(t), bg=C_GREEN,
                        hover=C_GREEN_H, fg="white", size=10)
            b.pack(fill="x", padx=12, pady=(6, 3))
            buy_btns[tier] = b
            _label(card, "支付宝付款 · 开通后立即生效", 8, fg=C_MUTED,
                   bg=C_CARD).pack(pady=(0, 10))
            # 默认选中：现档就在这组就选它（续费），否则选月付
            d = cur if (cur and _sku_tier(cur, sk.get(cur) or {}) == tier) else base_of(tier)
            if (cur or trial_used) and is_trial(d):
                d = base_of(tier)      # 体验月已买过就不能再选
            if d:
                pick(tier, d)

        def pack_card():
            if not packs:
                return
            card = tk.Frame(content, bg=C_CARD, highlightthickness=1,
                            highlightbackground=C_LINE)
            card.pack(fill="x", padx=16, pady=(2, 6))
            _label(card, "使用时长包", 12, bg=C_CARD, bold=True).pack(
                anchor="w", padx=12, pady=(10, 0))
            _label(card, "不订阅也能买；没有有效期。当天套餐额度用完后，按分钟接着用"
                         "（语音也一样可用）。Max 有效期内不扣包，到期后才开始用。",
                   8, fg=C_MUTED, bg=C_CARD, wraplength=330, justify="left",
                   anchor="w").pack(fill="x", padx=12, pady=(3, 4))
            grid = tk.Frame(card, bg=C_CARD)
            grid.pack(fill="x", padx=10, pady=(2, 2))
            grid.columnconfigure(0, weight=1)
            grid.columnconfigure(1, weight=1)
            pbase = packs[0]
            for i, k in enumerate(packs):
                off = ""
                if k != pbase:
                    off = _off_txt(_sku_off(k, sk.get(k) or {}, pbase, sk, plans))
                txt = "%s\n¥%s%s" % (_pack_dur(_sku_min(sk.get(k) or {})),
                                     money_of(k), ("　" + off) if off else "")
                b = _button(grid, txt, lambda k=k: pick_pack(k), size=8,
                            justify="center")
                b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
                chip_btns[("pack", k)] = b
            b = _button(card, "先选一个使用时长包", lambda: pay_now("pack"),
                        bg=C_GREEN, hover=C_GREEN_H, fg="white", size=10)
            _safe_cfg(b, state="disabled", disabledforeground="#5b636d")
            b.pack(fill="x", padx=12, pady=(6, 3))
            buy_btns["pack"] = b
            _label(card, "支付宝付款 · 买完立即到账", 8, fg=C_MUTED,
                   bg=C_CARD).pack(pady=(0, 10))

        def price_card():
            rows = []
            for tier, title in (("pro", "Pro 订阅"), ("max", "Max 订阅")):
                keys = subs.get(tier) or []
                if not keys:
                    continue
                base = base_of(tier)
                rows.append((title, "", "", "", True))
                for k in keys:
                    if is_trial(k):
                        continue        # 体验月是限一次促销价，不参与比价
                    off = "" if k == base else _off_txt(
                        _sku_off(k, sk.get(k) or {}, base, sk, plans))
                    rows.append((suf(k), "¥" + money_of(k),
                                 "¥%.5f/分钟" % _sku_unit(k, sk.get(k) or {}, plans),
                                 off or "—", False))
            if packs:
                rows.append(("使用时长包（不过期）", "", "", "", True))
                for k in packs:
                    off = "" if k == packs[0] else _off_txt(
                        _sku_off(k, sk.get(k) or {}, packs[0], sk, plans))
                    rows.append((_pack_dur(_sku_min(sk.get(k) or {})),
                                 "¥" + money_of(k),
                                 "¥%.5f/分钟" % _sku_unit(k, sk.get(k) or {}, plans),
                                 off or "—", False))
            if not rows:
                return
            card = tk.Frame(content, bg=C_CARD, highlightthickness=1,
                            highlightbackground=C_LINE)
            card.pack(fill="x", padx=16, pady=(2, 6))
            _label(card, "价格与折扣", 12, bg=C_CARD, bold=True).pack(
                anchor="w", padx=12, pady=(10, 0))
            _label(card, "单价统一折成元/分钟；折扣是同档里跟最短那档比。"
                         "Max 不限时长，按 24 小时/天折算（只是比价口径）。",
                   8, fg=C_MUTED, bg=C_CARD, wraplength=330, justify="left",
                   anchor="w").pack(fill="x", padx=12, pady=(3, 4))
            tb = tk.Frame(card, bg=C_CARD)
            tb.pack(fill="x", padx=12, pady=(0, 10))
            for i, w in enumerate((3, 2, 3, 2)):
                tb.columnconfigure(i, weight=w)
            for i, (h, anch) in enumerate((("档位", "w"), ("价格", "e"),
                                           ("折合单价", "e"), ("折扣", "e"))):
                _label(tb, h, 8, fg=C_MUTED, bg=C_CARD).grid(
                    row=0, column=i, sticky=anch, pady=(0, 2))
            for i, (nm, pr, un, off, grp) in enumerate(rows, start=1):
                if grp:
                    _label(tb, nm, 8, fg=C_MUTED, bg=C_CARD, bold=True).grid(
                        row=i, column=0, columnspan=4, sticky="w", pady=(6, 1))
                    continue
                _label(tb, nm, 8, fg=C_TEXT, bg=C_CARD).grid(
                    row=i, column=0, sticky="w", pady=1)
                _label(tb, pr, 8, fg=C_TEXT, bg=C_CARD).grid(
                    row=i, column=1, sticky="e", pady=1)
                _label(tb, un, 8, fg=C_MUTED, bg=C_CARD).grid(
                    row=i, column=2, sticky="e", pady=1)
                _label(tb, off, 8, fg=C_GREEN_H, bg=C_CARD).grid(
                    row=i, column=3, sticky="e", pady=1)

        def trust_card():
            card = tk.Frame(content, bg=C_CARD, highlightthickness=1,
                            highlightbackground=C_LINE)
            card.pack(fill="x", padx=16, pady=(2, 6))
            _label(card, "放心用", 12, bg=C_CARD, bold=True).pack(
                anchor="w", padx=12, pady=(10, 2))
            for t in ("一次付款，不会自动续费；到期自动回到免费版，不影响继续使用。",
                      "额度只按实际用量扣：一个自然分钟里有用过才算一分钟，不用不扣。",
                      "会话和密钥都在你自己的电脑上，中继只转发，不保存对话内容。"):
                _label(card, "· " + t, 8, fg=C_MUTED, bg=C_CARD,
                       wraplength=330, justify="left", anchor="w").pack(
                    fill="x", padx=12)
            tk.Frame(card, bg=C_CARD, height=8).pack()

        def pay_now(tier):
            if not (sel.get(tier) or ""):
                return
            start_pay(tier)

        plan_card("free")
        plan_card("pro", "推荐")
        plan_card("max")
        pack_card()
        price_card()
        trust_card()
        _scroll_top(body)        # 换了内容/刷新后从顶部看

    def load():
        hint.configure(text="正在读取套餐…", fg=C_MUTED)
        _page_bg(root, st, lambda: api_get("/api/plan", timeout=10), build)

    def start_pay(tier):
        k = sel.get(tier) or ""
        btn = buy_btns.get(tier)
        if not k or btn is None:
            return
        _safe_cfg(btn, state="disabled")
        _set_btn(btn, text="正在下单…")

        def work():
            return api_post("/api/pay_create", {"plan": k, "type": sel["type"]})

        def done(r):
            _safe_cfg(btn, state="normal")
            _set_btn(btn, text=cur_label.get(tier) or "立即开通")
            if _is_err(r):
                hint.configure(text="下单失败：%s" % str(r[1])[:80], fg=C_RED)
                return
            if not (r or {}).get("ok"):
                hint.configure(text=(r or {}).get("error") or "下单失败，稍后再试", fg=C_RED)
                return
            otn["v"] = r.get("out_trade_no") or ""
            hint.configure(text="扫下面的码付款，付完会自动刷新", fg=C_MUTED)
            show_pay(r)

        _page_bg(root, st, work, done)

    def check():
        if not otn["v"]:
            return 0
        j = api_post("/api/pay_status", {"otn": otn["v"]}, timeout=8)
        return int(((j or {}).get("order") or {}).get("status") or 0)

    def show_pay(r):
        import base64
        import io
        from PIL import Image, ImageTk

        clear()
        card = tk.Frame(content, bg=C_CARD)
        card.pack(fill="x", padx=16, pady=(4, 8))
        _label(card, "扫码付款", 12, bg=C_CARD, bold=True).pack(anchor="w", padx=12,
                                                             pady=(10, 6))
        pic = None
        img = r.get("img") or ""
        if "," in img[:64] and img.lstrip().startswith("data:"):
            img = img.split(",", 1)[1]
        if img:
            try:
                pic = Image.open(io.BytesIO(base64.b64decode(img)))
                pic.load()
            except Exception:
                pic = None
        if pic is None:
            data = r.get("qrcode") or r.get("payurl") or ""
            if data:
                pic = _qr_image(data, box=8)
        holder = _label(card, "", bg=C_CARD)
        if pic is not None:
            try:
                photo = ImageTk.PhotoImage(_fit_image(pic, 240))
                holder._cb_pay = photo
                holder.configure(image=photo)
                holder.pack(pady=(2, 6))
            except Exception as e:
                pic = None
                holder.configure(text="二维码画不出来：%s" % str(e)[:80], fg=C_RED)
                holder.pack(pady=(2, 6))
        else:
            holder.configure(text="二维码生成失败", fg=C_RED)
            holder.pack(pady=(2, 6))
        status = _label(card, "用手机支付宝扫码付款" if pic is not None
                        else "二维码生成失败，点下面的按钮在浏览器里打开支付页",
                        9, fg=C_MUTED, bg=C_CARD, wraplength=300, justify="left")
        status.pack(padx=12, pady=(0, 8))
        # 浏览器兜底：只有二维码拿不到时才出现（用户指定保留）
        if r.get("payurl") and pic is None:
            _button(card, "在浏览器打开支付页",
                    lambda: _startfile(r.get("payurl")), size=9).pack(
                fill="x", padx=12, pady=(0, 10))
        bar = tk.Frame(content, bg=C_BG)
        bar.pack(fill="x", padx=16, pady=(4, 12))
        _button(bar, "返回套餐列表", lambda: st["goto"]("plans"), size=9).pack(side="left")
        _button(bar, "我已完成支付", lambda: poll_once(status), bg=C_GREEN,
                hover=C_GREEN_H, fg="white", size=9).pack(side="right")
        _scroll_top(body)        # 付款卡从顶部显示，别停在旧滚动位置
        poll_loop(status)

    def poll_once(status=None):
        def done(r):
            if _is_err(r):
                _safe_cfg(status, text="查询失败：%s" % str(r[1])[:60], fg=C_RED)
                return
            if r == 1:
                _safe_cfg(status, text="✓ 已到账，套餐已更新", fg=C_GREEN_H)
                paid["v"] = True
                _page_after(root, st, 800, load)
            else:
                _safe_cfg(status, text="还没收到，等一下再试（支付宝入账有延迟）", fg=C_MUTED)

        _page_bg(root, st, check, done)

    def poll_loop(status):
        def tick():
            def done(r):
                if r == 1:
                    _safe_cfg(status, text="✓ 已到账，套餐已更新", fg=C_GREEN_H)
                    paid["v"] = True
                    _page_after(root, st, 800, load)
                else:
                    _page_after(root, st, 2500, tick)

            _page_bg(root, st, check, done)

        _page_after(root, st, 2500, tick)

    load()


# ---------------------------------------------------------------- 页：检查更新

def page_update(root, st, body):
    import tkinter as tk

    card = tk.Frame(body, bg=C_CARD)
    card.pack(fill="x", padx=16, pady=(6, 10))
    _label(card, "版本更新", 11, bg=C_CARD, bold=True).pack(anchor="w", padx=12, pady=(10, 2))
    cur_lab = _label(card, "当前版本 v" + (version() or "?"), 9, fg=C_MUTED,
                     bg=C_CARD)
    cur_lab.pack(anchor="w", padx=12)
    msg = _label(card, "", 9, fg=C_MUTED, bg=C_CARD, wraplength=300, justify="left", anchor="w")
    msg.pack(fill="x", padx=12, pady=(6, 10))

    bar = tk.Frame(body, bg=C_BG)
    bar.pack(fill="x", padx=16)
    _button(bar, "检查更新", lambda: check(), size=10).pack(fill="x", pady=2)
    upd_btn = {"w": None}

    def check(*_):
        msg.configure(text="正在检查…", fg=C_MUTED)
        if upd_btn["w"] is not None:
            try:
                upd_btn["w"].destroy()
            except Exception:
                pass
            upd_btn["w"] = None

        def work():
            if not alive(1.5):
                raise RuntimeError("服务没在运行，先启动服务")
            return api_get("/api/version?force=1", timeout=60)

        def done(r):
            if _is_err(r):
                msg.configure(text="检查失败：%s" % str(r[1])[:120], fg=C_RED)
                return
            if not (r or {}).get("ok"):
                msg.configure(text="检查失败：%s" % ((r or {}).get("err") or "中继/GitHub 都连不上"),
                              fg=C_RED)
                return
            cur = (r or {}).get("version") or version() or "?"
            if not (r or {}).get("update"):
                msg.configure(text="已是最新版本（v%s）" % cur, fg=C_TEXT)
                return
            new = (r or {}).get("relay_ver") or (r or {}).get("gh_tag") or "新版本"
            # 先问再动：更新会重启服务，正在跑的会话会断一下
            msg.configure(text="发现新版本 v%s（当前 v%s）。\n\n更新会下载并重启服务，"
                               "正在跑的会话会断开一下（约十秒后自动恢复）。" % (new, cur),
                          fg=C_TEXT)
            upd_btn["w"] = _button(bar, "立即更新", do_update, bg=C_GREEN,
                                   hover=C_GREEN_H, fg="white", size=10)
            upd_btn["w"].pack(fill="x", pady=2)

        _page_bg(root, st, work, done)

    def do_update():
        if upd_btn["w"] is not None:
            upd_btn["w"].destroy()
            upd_btn["w"] = None
        msg.configure(text="正在下载安装…（完成后服务会自动重启，窗口可以关掉）", fg=C_MUTED)

        def work():
            return api_post("/api/update", {}, timeout=300)

        def done(r):
            if _is_err(r):
                msg.configure(text="更新失败：%s" % str(r[1])[:120], fg=C_RED)
                return
            if (r or {}).get("ok"):
                # 磁盘上的 VERSION 已经变了：先把本页的版本号刷掉，别停在旧值
                try:
                    cur_lab.configure(text="当前版本 v" + (version() or "?"))
                    st["ver_disk"] = version()
                    if st.get("verlab"):
                        st["verlab"].configure(text="手机派活，电脑干活　·　v" + (version() or "?"))
                except Exception:
                    pass
                msg.configure(text="✓ 已更新到最新版本，服务会自动重启（几秒后可重新扫码/使用）",
                              fg=C_GREEN_H)
            elif (r or {}).get("uptodate"):
                msg.configure(text="已是最新版本（v%s）" % (version() or "?"))
            else:
                msg.configure(text=(r or {}).get("why") or "更新失败", fg=C_RED)

        _page_bg(root, st, work, done)

    if not st.get("upd_checked"):
        st["upd_checked"] = True
        check()


def _spawn_self(*args):
    """另起一个自己画窗口（单窗口崩了不影响托盘和其它窗口）。"""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + list(args),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
        return True
    except Exception as e:
        _logline("spawn %s 失败：%s" % (" ".join(args), e))
        # pythonw 下没有控制台：不弹窗的话就是「点了没反应」
        _win_msgbox("窗口打不开：%s\n\n%s" % (str(e)[:160], HERE))
        return False


# ---------------------------------------------------------------- 托盘

def _single_instance():
    """同一登录会话里只允许一个托盘（重复点快捷方式不会叠出一堆图标）。"""
    global _MUTEX
    if os.name != "nt":
        return True
    import ctypes
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, TRAY_MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


# 品牌图标（1024 缩到 128 的 PNG，内联）：发布包里不带独立图片资源，
# Windows 快捷方式的 .ico 和托盘图标都从这一份来（brand/icons/39-short-slit.png）。
_ICON_PNG_B64 = (
    "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"
)


def _icon_image():
    """托盘/快捷方式图标：深底 + 橙色机身 + 绿色提示符。
    PIL 缺失时退回手绘老图案——总比没有图标强。"""
    try:
        import base64 as _b64, io as _io
        from PIL import Image as _Im
        return _Im.open(_io.BytesIO(_b64.b64decode(_ICON_PNG_B64))).convert("RGBA")
    except Exception:
        return _icon_draw()


def _icon_draw():
    """兜底手绘：与 App 图标同一套元素（深底圆角 + 橙色机身 + 绿色 >_）。
    正常路径永远走 _ICON_PNG_B64；只有 base64 解不出来时才用这张。"""
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([7, 2, 57, 61], radius=13, fill=(16, 23, 34, 255),
                        outline=(240, 132, 80, 255), width=3)
    d.line([(20, 20), (35, 31), (20, 42)], fill=(163, 230, 53, 255), width=5,
           joint="curve")
    d.rounded_rectangle([37, 37, 47, 41], radius=2, fill=(163, 230, 53, 255))
    return img


def _state_text(icon=None, item=None):
    if STATE["alive"]:
        return "服务：运行中"
    if STATE["enabled"] is None:
        return "服务：未安装（点「重新安装服务」）"
    if STATE["enabled"] is False:
        return "服务：已停止（开机不自启）"
    return "服务：已停止"


def _toggle_text(icon=None, item=None):
    if STATE.get("busy"):
        return "启动中…" if not STATE["alive"] else "停止中…"
    return "停止服务" if STATE["alive"] else "启动服务"


def main():
    mode = ""
    for m in ("--qr", "--sessions", "--plans", "--update", "--uninstall", "--reinstall",
              "--main", "--window", "--daemon"):
        if m in sys.argv:
            mode = m
            break
    try:
        # 所有窗口入口都汇到同一个客户端窗口（已有窗口时只切页并拉到前台）
        if mode in ("--qr", "--main"):
            return open_window("home")
        if mode == "--sessions":
            return open_window("sessions")
        if mode == "--plans":
            return open_window("plans")
        if mode == "--update":
            return open_window("update")
        if mode == "--uninstall":
            return open_window("home", "uninstall")
        if mode == "--reinstall":
            return open_window("home", "reinstall")
        if mode == "--window":
            pg, act = "home", ""
            for flag, key in (("--page", "pg"), ("--act", "act")):
                if flag in sys.argv:
                    i = sys.argv.index(flag)
                    val = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
                    if key == "pg":
                        pg = val or "home"
                    else:
                        act = val or ""
            return open_window(pg, act)
    except Exception as e:
        _logline("%s 崩溃：%r" % (mode, e))
        if isinstance(e, ImportError) and "tkinter" in str(e):
            # 老安装包自带的 Python 被删掉了 tkinter：所有窗口都会静默失败，
            # 这里至少让用户知道该怎么办（重跑新安装包即可，数据保留）。
            _win_msgbox("BossHost 工头主机缺少界面组件（tkinter），窗口打不开。\n\n"
                        "请重新运行安装包 BossHost-Setup.exe 覆盖安装一次即可修好，"
                        "数据、配对都不会丢。")
        raise

    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as e:
        _logline("缺少托盘依赖：%s" % e)
        try:
            if sys.stderr:
                sys.stderr.write("缺少托盘依赖 pystray/Pillow：%s\n"
                                 "修复：重跑安装目录里的 install.ps1\n" % e)
        except Exception:
            pass
        try:      # 没有控制台，弹个窗让人知道发生了什么
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(BRAND, "托盘组件缺失（pystray/Pillow）。\n\n"
                                 "修复：重新运行安装目录里的 install.ps1（或安装包里的 安装.cmd）。")
            r.destroy()
        except Exception:
            pass
        return 3

    if not _single_instance():
        return 0

    def notify(icon, msg, title=BRAND):
        try:
            icon.notify(msg, title)
        except Exception:
            pass

    def bg_notify(icon, work, ok_msg):
        """长操作放到后台线程，免得把托盘菜单卡住。"""
        def run():
            try:
                ok, err = work()
            except Exception as e:
                ok, err = False, str(e)[:100]
            notify(icon, ok_msg if ok else err)
            refresh(icon, force=True)

        threading.Thread(target=run, daemon=True).start()

    def do_main(icon, item=None):
        _spawn_self("--main")

    def do_qr(icon, item=None):
        _spawn_self("--qr")

    def do_sessions(icon, item=None):
        _spawn_self("--sessions")

    def do_plans(icon, item=None):
        _spawn_self("--plans")

    def do_update(icon, item=None):
        _spawn_self("--update")

    def do_reinstall(icon, item=None):
        _spawn_self("--reinstall")

    def do_uninstall(icon, item=None):
        _spawn_self("--uninstall")

    def do_toggle(icon, item=None):
        # 「启动中」不能重复点：start_service 开头会清僵尸进程，两次并发会互杀
        if STATE.get("busy"):
            return
        if STATE["alive"]:
            work, ok_msg = stop_service, "服务已停止（开机不再自启）"
        elif STATE["enabled"] is None:
            _spawn_self("--main")
            return
        else:
            work, ok_msg = start_service, "服务已启动"
        STATE["busy"] = True
        try:
            icon.update_menu()
        except Exception:
            pass

        def run():
            try:
                ok, err = work()
            except Exception as e:
                ok, err = False, str(e)[:100]
            finally:
                STATE["busy"] = False
            notify(icon, ok_msg if ok else err)
            refresh(icon, force=True)

        threading.Thread(target=run, daemon=True).start()

    def do_quit(icon, item=None):
        icon.stop()

    def refresh(icon, force=False):
        now = time.time()
        if force or now - STATE["checked"] > 12:
            alive(1.0)
            task_enabled()
            STATE["checked"] = now
        else:
            alive(1.0)
        try:
            if STATE["enabled"] is None:
                title = BRAND + " · 未安装"
            else:
                title = BRAND + " · %s" % ("运行中" if STATE["alive"] else "已停止")
            icon.title = title
            icon.update_menu()
        except Exception:
            pass

    def watchdog():
        """服务挂了自动拉起。只在「计划任务处于启用」时动手——用户点「停止服务」会把
        任务禁用，这里就不会去打扰他。连续 4 次（约 12 秒）探不通才重启，避开偶发卡顿；
        两次重启之间至少隔 2 分钟，防止死循环刷进程。"""
        if STATE["alive"]:
            STATE["down"] = 0
            return
        if STATE["enabled"] is not True:
            return
        STATE["down"] = STATE.get("down", 0) + 1
        now = time.time()
        if STATE["down"] < 4 or now - STATE.get("last_restart", 0.0) < 120:
            return
        STATE["last_restart"] = now
        STATE["down"] = 0
        ok, err = start_service(timeout=25)
        _logline("看门狗：服务未响应，自动重启%s%s"
                 % ("成功" if ok else "失败", "" if ok else "（%s）" % (err or "")))

    def poll(icon):
        while True:
            time.sleep(3)
            try:
                refresh(icon)
            except Exception:
                pass
            try:
                watchdog()
            except Exception as e:
                _logline("看门狗异常：%r" % e)

    menu = Menu(
        MenuItem(_state_text, None, enabled=lambda item: False),
        Menu.SEPARATOR,
        MenuItem("扫这个码接上…", do_qr),
        MenuItem("查看会话列表…", do_sessions),
        Menu.SEPARATOR,
        MenuItem(_toggle_text, do_toggle),
        MenuItem("重新安装服务（覆盖，保留数据）…", do_reinstall),
        MenuItem("卸载服务…", do_uninstall),
        *([] if PLANS_OFF else [MenuItem("套餐与续费…", do_plans)]),
        MenuItem("检查更新…", do_update),
        Menu.SEPARATOR,
        MenuItem("打开主窗口", do_main, default=True),
        MenuItem("退出", do_quit),
    )

    icon = pystray.Icon("codeboss", _icon_image(), BRAND, menu)
    _ensure_supervisor()
    alive(1.0)
    not_installed = (task_enabled() is None)
    STATE["checked"] = time.time()
    threading.Thread(target=poll, args=(icon,), daemon=True).start()
    _logline("托盘启动 pid=%d installed=%s" % (os.getpid(), not not_installed))
    if not_installed:
        _spawn_self("--main")     # 首次运行/没装服务：直接把「三步接入」引导摆出来
    icon.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except Exception as e:
        _logline("未捕获异常：%r" % e)
        raise
