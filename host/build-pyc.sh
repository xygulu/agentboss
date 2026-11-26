#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 打「无源码」发布目录：build/pyc/<311>/ —— 只有 .pyc + 几行入口薄壳。
# 用法：./build-pyc.sh              默认 Python 3.11
#       CB_PYC_PY_VER=3.12 ./build-pyc.sh
# 产物是完整发行包目录（static/、安装脚本、VERSION 都在），供 publish-host.sh / dist.sh /
# mac/build-app.sh 直接取用；开发仍用 git 里的 .py 源码。
#
# 为什么锁小版本：.pyc 的 magic 号绑死 Python 小版本（3.12 加载 3.11 的 pyc 直接
# "bad magic number"），所以一个发布目录只服务一个版本；安装脚本按 PYC_VER 找解释器，
# 找不到就用 uv 装一个（一次性 ~30MB）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # host/
ROOT="$(cd "$HERE/.." && pwd)"            # 仓库根（shared/ relay/ tools/ 在这）
PYVER="${CB_PYC_PY_VER:-3.11}"
ABI="${PYVER//./}"                       # 3.11 → 311
OUT="$HERE/build/pyc/$ABI"
VER="$(cat "$HERE/VERSION" 2>/dev/null | tr -d '[:space:]')"
[ -n "$VER" ] || { echo "✗ 缺 VERSION 文件"; exit 1; }

# 找匹配小版本的解释器（$CB_PYC_PY 可显式指定）
_want() {
  "$1" -c "import sys;raise SystemExit(0 if '%d.%d' % sys.version_info[:2] == '$PYVER' else 1)" \
    >/dev/null 2>&1
}
PY=""
for c in "$CB_PYC_PY" "python$PYVER" "$HERE/.venv/bin/python" python3; do
  [ -n "$c" ] || continue
  if command -v "$c" >/dev/null 2>&1 && _want "$c"; then PY="$(command -v "$c")"; break; fi
  if [ -x "$c" ] && _want "$c"; then PY="$c"; break; fi
done
if [ -z "$PY" ] && command -v uv >/dev/null 2>&1 && uv python find "$PYVER" >/dev/null 2>&1; then
  PY="$(uv python find "$PYVER")"
fi
[ -n "$PY" ] || {
  echo "✗ 找不到 Python ${PYVER}。装一个：uv python install $PYVER"
  echo "  （或指定：CB_PYC_PY=/path/to/python$PYVER ./build-pyc.sh）"
  exit 3
}
echo "== 编译 pyc（$("$PY" -V 2>&1 | cut -d" " -f2)）=="

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$HERE/plat.py" "$HERE/pty_backend.py" "$ROOT/relay/relay.py" "$HERE/agents.py" "$ROOT/shared/terms.py" \
   "$HERE/agent_codex.py" "$HERE/codex_daemon.py" "$HERE/outcome.py" "$ROOT/shared/branding.py" "$ROOT/shared/brand_icons.py" \
   "$HERE/agent_opencode.py" "$HERE/agent_hermes.py" "$HERE/owned_sessions.py" \
   "$HERE/remind_rules.py" "$HERE/notify.py" "$HERE/boss.py" "$HERE/transcript.py" "$HERE/hooks.py" "$HERE/static_res.py" "$HERE/screen.py" \
   "$HERE/scrollback.py" "$HERE/window_source.py" "$HERE/acp.py" "$HERE/agent_acp.py" "$HERE/art_edit.py" "$HERE/risk_review.py" \
   "$HERE/server.py" "$HERE/wrapper.py" \
   "$HERE/tray.py" "$HERE/ptywin.py" "$TMP/"
# 包（目录）也要整棵带上：pet 是包，漏了它就是 2026-11-20 那次——
# server.py 一 import 就 ModuleNotFoundError，装好的机器全起不来。
rm -rf "$TMP/pet"
cp -R "$HERE/pet" "$TMP/pet"
rm -rf "$TMP/pet/__pycache__"
# -b：老式布局，pyc 与控制器的名字同目录（无源导入要这个）；-OO：去掉 docstring（代码里没有 assert）
(cd "$TMP" && "$PY" -OO -m compileall -b -q .)
mv "$TMP/server.pyc"  "$TMP/server_impl.pyc"
mv "$TMP/wrapper.pyc" "$TMP/wrapper_impl.pyc"
mv "$TMP/tray.pyc"    "$TMP/tray_impl.pyc"
mv "$TMP/ptywin.pyc"  "$TMP/ptywin_impl.pyc"

rm -rf "$OUT"
mkdir -p "$OUT"

# 入口薄壳：文件名必须保持不变（launchd/systemd/alias/更新器都用 server.py、wrapper.py）。
# 老安装里是 .py 源码，导入时源码优先于 .pyc —— 所以壳在导入 impl 之前把它删掉。
# git 检出不动（开发机，源码就是工作区）。
cat > "$OUT/server.py" <<'STUB'
#!/usr/bin/env python3
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_in_git = False
_p = _here
for _ in range(4):
    if (_p / ".git").exists():
        _in_git = True
        break
    _p = _p.parent
if not _in_git:
    for _n in ("plat", "pty_backend", "relay", "agents", "agent_codex", "codex_daemon", "outcome", "remind_rules", "notify", "boss", "transcript", "hooks", "static_res", "screen", "scrollback", "window_source", "acp", "agent_acp", "brand_icons", "art_edit", "risk_review", "server_impl", "wrapper_impl"):
        _old = _here / (_n + ".py")
        if _old.exists():
            try:
                _old.unlink()
            except OSError:
                pass
    _pd = _here / "pet"          # 包里的旧源码同理：源码优先于 .pyc，不删就还是旧代码
    if _pd.is_dir():
        for _old in _pd.glob("*.py"):
            try:
                _old.unlink()
            except OSError:
                pass

try:
    from server_impl import main
except ImportError as _e:
    if "bad magic number" not in str(_e):
        raise
    _want = ""
    try:
        _want = (_here / "PYC_VER").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    sys.stderr.write(
        "✗ 启动失败：当前解释器是 Python %d.%d，安装包要求 Python %s。\n"
        "  修复：删除 %s 后，用安装包里的安装脚本（install.sh / install-linux.sh）重装一次（数据保留）。\n"
        % (sys.version_info[0], sys.version_info[1], _want or "（未知）", _here / ".venv")
    )
    sys.exit(3)

# 只有当入口跑才起服务。这个文件可能被 `import server` 当模块加载（插件模块里的延迟
# 导入），无守卫时会在**正在服务的进程内部**再起一个 main()，占着自己的端口等 20 秒、
# 然后把自己杀掉（2026-11-14 全量 0.9.x 每 30 秒重启一次的原因）。
if __name__ == "__main__":
    sys.exit(main())
STUB
cat > "$OUT/wrapper.py" <<'STUB'
#!/usr/bin/env python3
import sys

from wrapper_impl import main

sys.exit(main())
STUB
cat > "$OUT/tray.py" <<'STUB'
#!/usr/bin/env python3
import sys

from tray_impl import _icon_image, main

if __name__ == "__main__":
    sys.exit(main())
STUB
cat > "$OUT/ptywin.py" <<'STUB'
#!/usr/bin/env python3
import sys

from ptywin_impl import main

if __name__ == "__main__":
    sys.exit(main())
STUB
chmod +x "$OUT/server.py" "$OUT/wrapper.py" "$OUT/tray.py" "$OUT/ptywin.py"

cp "$TMP/server_impl.pyc" "$TMP/wrapper_impl.pyc" "$TMP/tray_impl.pyc" \
   "$TMP/ptywin_impl.pyc" \
   "$TMP/plat.pyc" "$TMP/pty_backend.pyc" "$TMP/relay.pyc" "$TMP/agents.pyc" "$TMP/terms.pyc" \
   "$TMP/agent_codex.pyc" "$TMP/codex_daemon.pyc" "$TMP/outcome.pyc" "$TMP/remind_rules.pyc" "$TMP/notify.pyc" "$TMP/boss.pyc" "$TMP/transcript.pyc" "$TMP/hooks.pyc" "$TMP/static_res.pyc" "$TMP/screen.pyc" \
   "$TMP/scrollback.pyc" "$TMP/window_source.pyc" "$TMP/acp.pyc" "$TMP/agent_acp.pyc" "$TMP/art_edit.pyc" "$TMP/risk_review.pyc" \
   "$TMP/branding.pyc" "$TMP/brand_icons.pyc" "$TMP/agent_opencode.pyc" "$TMP/agent_hermes.pyc" "$TMP/owned_sessions.pyc" "$OUT/"
mkdir -p "$OUT/pet"
cp "$TMP"/pet/*.pyc "$OUT/pet/"
[ -f "$TMP/pet/sim.html" ] && cp "$TMP/pet/sim.html" "$OUT/pet/"
printf '%s\n' "$PYVER" > "$OUT/PYC_VER"

# 非代码文件：安装脚本、静态页、说明（与本目录源码一一对应，绝不带数据文件）
for f in requirements.txt install.sh install-linux.sh install.ps1 uninstall.ps1 安装.cmd \
         codeboss-cli uninstall.sh uninstall-linux.sh dist.sh \
         VERSION README.md 安装.command 安装与使用说明.md; do
  [ -f "$HERE/$f" ] && cp -f "$HERE/$f" "$OUT/"
done
# 发布物不留注释：脚本里的说明性文字（含打包/实现细节）在拷进发行目录后统一去掉，
# 仓库里的源码原样不动。
for f in server.py wrapper.py tray.py ptywin.py install.sh install-linux.sh uninstall.sh uninstall-linux.sh \
         dist.sh codeboss-cli install.ps1 uninstall.ps1 安装.cmd 安装.command requirements.txt; do
  [ -f "$OUT/$f" ] && "$PY" "$ROOT/tools/strip_comments.py" "$OUT/$f"
done
# 守卫：.ps1 开头必须「恰好一个」UTF-8 BOM。少了 PS5.1 按本地代码页解、中文乱码；
# 多了（strip 不幂等时）PS5.1 首行解析失败，安装器直接退 1、连日志都建不出来。
for f in install.ps1 uninstall.ps1; do
  [ -f "$OUT/$f" ] || continue
  _h="$(head -c 6 "$OUT/$f" | xxd -p)"
  case "$_h" in
    efbbbfefbbbf*) echo "✗ $f 开头有两个 BOM（PowerShell 5.1 会解析失败）"; exit 1 ;;
    efbbbf*) ;;
    *) echo "✗ $f 缺 UTF-8 BOM（Windows PowerShell 5.1 中文会乱码）"; exit 1 ;;
  esac
done
# 网页资源不落盘：删注释、去缩进后压进 static_pack.pyc —— 装完在安装目录下翻不到
# 可读的前端源码（xterm 等第三方库保持原样，保留许可声明）
mkdir -p "$TMP/static"
cp -R "$HERE/static/." "$TMP/static/"
rm -rf "$TMP/static/src"                   # 前端源码不进包（pack_static 本来也只收顶层文件）
rm -f "$TMP/static/_preview.html"          # 开发用的预览页，里面带 token，绝不进包
for f in ext.html term.html upd.js; do
  [ -f "$TMP/static/$f" ] && "$PY" "$ROOT/tools/strip_web.py" "$TMP/static/$f"
done
"$PY" "$ROOT/tools/pack_static.py" "$TMP/static" "$TMP/static_pack.py"
(cd "$TMP" && "$PY" -OO -m compileall -b -q static_pack.py)
cp "$TMP/static_pack.pyc" "$OUT/"
chmod +x "$OUT/install.sh" "$OUT/install-linux.sh" "$OUT/uninstall.sh" \
         "$OUT/uninstall-linux.sh" "$OUT/dist.sh" "$OUT/codeboss-cli" "$OUT/安装.command" 2>/dev/null || true

echo "✓ 无源码发布目录：${OUT}（v${VER}，Python ${PYVER}）"
ls -l "$OUT" | sed 's/^/    /'
