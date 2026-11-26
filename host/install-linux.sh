#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# codeboss 一键部署（Linux，幂等，可重复执行=升级）
# 依赖 → systemd --user 自启（+ loginctl enable-linger 常驻）→ shell alias → 配置 → 二维码
# 两种安装源：
#   · git 仓库内运行  → 原地安装/升级
#   · 压缩包/发行包运行 → 程序装到 ${XDG_DATA_HOME:-~/.local/share}/codeboss
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
LABEL="${CB_LABEL:-codeboss}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "== codeboss 部署（Linux） =="

# 0) 环境
if [ "$(uname -s)" != "Linux" ]; then echo "✗ 这是 Linux 安装脚本，macOS 请用 install.sh"; exit 1; fi
_arch="$(uname -m)"
case "$_arch" in x86_64) _arch="x64" ;; aarch64|arm64) _arch="arm64" ;; esac
# 安装包里自带了对应架构的 Python（python-<arch>/）就不需要机器上装
HAVE_BUNDLE=""
[ -d "$SRC/python-$_arch" ] && HAVE_BUNDLE=1
if [ -z "$HAVE_BUNDLE" ] && ! command -v python3 >/dev/null 2>&1; then
  echo "✗ 缺少 python3。Debian/Ubuntu：sudo apt install -y python3 python3-venv python3-pip"
  exit 3
fi
if [ -z "$HAVE_BUNDLE" ] && ! python3 -m venv --help >/dev/null 2>&1; then
  echo "! 这台机器没有 venv 模块（Debian/Ubuntu：sudo apt install -y python3-venv）；"
  echo "  脚本会退到 virtualenv 或系统 python + pip --user，先继续。"
fi

# 0.5) 定安装位置
if git -C "$SRC" rev-parse --show-toplevel >/dev/null 2>&1; then
  HERE="$SRC"   # git 部署：原地升级
  if git -C "$HERE" pull --ff-only 2>/dev/null; then echo "✓ 已同步最新代码"; fi
else
  HERE="${CB_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codeboss}"
  mkdir -p "$HERE"
  # 只复制程序文件，不碰数据（token.txt / sessions.json / config.json / logs / .venv 原地保留）。
  # 在安装目录里重跑（原地升级）时 SRC 就是 HERE，自己拷自己会报错，跳过。
  if [ "$SRC" != "$HERE" ]; then
    for f in server.py wrapper.py relay.py plat.py pty_backend.py agents.py terms.py agent_codex.py \
             requirements.txt install.sh install-linux.sh \
             codeboss-cli uninstall.sh uninstall-linux.sh dist.sh VERSION PYC_VER README.md 安装与使用说明.md; do
      [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$HERE/"
    done
    # 无源码包：核心代码只有 .pyc（server.py/wrapper.py 是入口薄壳）
    cp -f "$SRC"/*.pyc "$HERE/" 2>/dev/null || true
    # 同名旧 .py 会压过新 .pyc（Python 优先源码）：老 branding.py 赖着不走，
    # 新机器「更新成功」却一直跑老代码（/api/brand 500 就是它）。包里编成
    # pyc 的同名旧源码拷完就清掉；入口薄壳 server.py/wrapper.py 除外。
    for p in "$SRC"/*.pyc; do
      [ -e "$p" ] || continue
      b=$(basename "$p" .pyc)
      { [ "$b" = server ] || [ "$b" = wrapper ]; } && continue
      rm -f "$HERE/$b.py"
    done
    # pet 是包（目录），*.pyc 通配符覆盖不到它。漏了就是 2026-11-21 那台：
    # 装上后 `import pet` 直接 ModuleNotFoundError，服务起不来、自动更新也跟着死。
    if [ -d "$SRC/pet" ]; then rm -rf "$HERE/pet"; cp -R "$SRC/pet" "$HERE/"; fi
    # 网页资源：新包里编在 static_pack.pyc（没有 static/ 目录），老包里才是目录
    rm -rf "$HERE/static"
    if [ -d "$SRC/static" ]; then cp -R "$SRC/static" "$HERE/"; fi
    # 自带 Python（安装包按架构带一份）：装进安装目录，之后不依赖机器环境
    if [ -d "$SRC/python-$_arch" ]; then
      rm -rf "$HERE/python"
      cp -R "$SRC/python-$_arch" "$HERE/python"
      echo "✓ 自带 Python 已就位（不用机器上先装）"
    fi
    echo "✓ 程序安装到 ${HERE}（数据保留，安装包可删）"
  else
    echo "✓ 已在安装目录内运行，跳过文件复制（原地升级）"
  fi
  chmod +x "$HERE/install.sh" "$HERE/install-linux.sh" "$HERE/uninstall.sh" \
           "$HERE/uninstall-linux.sh" "$HERE/dist.sh" "$HERE/codeboss-cli" 2>/dev/null || true
fi
PY="$HERE/.venv/bin/python"
BUNDLED="$HERE/python/bin/python3"
mkdir -p "$HERE/logs"

# 0.9) 无源码包锁死 Python 小版本（.pyc 跨小版本直接 bad magic number）：
#      先找现成匹配的解释器，没有就用 uv 装一个（一次性，约 30MB）。
#      下载先直连、失败自动换国内镜像：有的网络连 astral.sh / GitHub 会直接掐断
#      TLS，不换源就永远装不上（Ubuntu 22.04 自带 3.10，实测卡在这里）。
PYC_VER="$(cat "$HERE/PYC_VER" 2>/dev/null | tr -d '[:space:]')"
PYPICK=""
_pyver_ok() {
  "$1" -c "import sys;raise SystemExit(0 if '%d.%d' % sys.version_info[:2] == '$PYC_VER' else 1)" \
    >/dev/null 2>&1
}

# 自动选源：先测速（HEAD，4 秒超时），快的排前面；探不通的排最后照样试一遍
# （探测被拦、下载却可以的情况是有的，所以探不通的源不放弃）
_fast_first() {
  _fast=""; _slow=""
  for _u in "$@"; do
    [ -n "$_u" ] || continue
    _t="$(curl -sS -I -m 4 -o /dev/null -w '%{time_total}' "$_u" 2>/dev/null)" || _t=""
    if [ -n "$_t" ]; then
      _fast="$_fast $_u|$_t"
    else
      _slow="$_slow $_u"
    fi
  done
  { for _e in $_fast; do printf '%s %s\n' "${_e##*|}" "${_e%|*}"; done | sort -n | cut -d' ' -f2-
    for _u in $_slow; do printf '%s\n' "$_u"; done
  }
}

# pip 下载源：清华 / 阿里云 / 直连，先测速再试（--timeout 8 是「不可达就赶紧换」，
# 默认 15 秒 × 5 次重试像卡死）。
_pip_try() {
  _py="$1"; shift
  for _src in $(_fast_first https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple) ""; do
    if [ -n "$_src" ]; then
      "$_py" -m pip -q install --timeout 8 --retries 1 -i "$_src" "$@" && return 0
    else
      "$_py" -m pip -q install --timeout 8 --retries 1 "$@" && return 0
    fi
  done
  return 1
}

# 找一个能用的 uv（已装 → ~/.local/bin → pip 装）
_ensure_uv() {
  for _u in uv "$HOME/.local/bin/uv"; do
    if command -v "$_u" >/dev/null 2>&1; then command -v "$_u"; return 0; fi
    if [ -x "$_u" ]; then echo "$_u"; return 0; fi
  done
  echo "· 没有 uv，先装下载器（自动选可达的 PyPI 源）…" >&2
  if _pip_try "${PYPICK:-python3}" --user uv >/dev/null 2>&1; then
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  fi
  # 最后试官方脚本（能直连 astral.sh 的网络直接用，不通就放弃）
  curl -LsSf --max-time 20 https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv 2>/dev/null || return 1
}

# uv 装 Python：国内优先 npmmirror（清华没有 Python 本体镜像；npmmirror 同样快），
#   然后 GitHub 直连，最后南大/ghproxy 两个 GitHub 代理。
_uv_py_install() {
  _uv="$1"; _ver="$2"
  for _m in $(_fast_first \
    "https://registry.npmmirror.com/-/binary/python-build-standalone" \
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/releases/download" \
    "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download") ""; do
    if [ -z "$_m" ]; then
      echo "· 换 GitHub 直连再试…" >&2
      if UV_HTTP_TIMEOUT=60 "$_uv" python install "$_ver" >/dev/null 2>&1; then return 0; fi
    else
      echo "· 走镜像装 Python：$_m" >&2
      if UV_HTTP_TIMEOUT=60 UV_PYTHON_INSTALL_MIRROR="$_m" \
           "$_uv" python install "$_ver" >/dev/null 2>&1; then return 0; fi
    fi
  done
  return 1
}

if [ -n "$PYC_VER" ]; then
  # 安装包自带的解释器优先：版本钉死，不受机器环境影响（也就不用联网装）
  if [ -x "$BUNDLED" ] && _pyver_ok "$BUNDLED"; then
    PYPICK="$BUNDLED"
  else
    for _c in "$PY" "${CB_PYTHON:-}" "python$PYC_VER" python3; do
      [ -n "$_c" ] || continue
      _bin="$_c"
      if command -v "$_c" >/dev/null 2>&1; then _bin="$(command -v "$_c")"; fi
      if [ -x "$_bin" ] && _pyver_ok "$_bin"; then PYPICK="$_bin"; break; fi
    done
  fi
  if [ -z "$PYPICK" ]; then
    echo "· 这台机器没有 Python ${PYC_VER}，正在准备（一次性，约 30MB，自动选镜像）…"
    UV_BIN="$(_ensure_uv || true)"
    if [ -n "$UV_BIN" ]; then
      _uv_py_install "$UV_BIN" "$PYC_VER" || true
      PYPICK="$("$UV_BIN" python find "$PYC_VER" 2>/dev/null || true)"
    fi
  fi
  [ -n "$PYPICK" ] || {
    echo "✗ 装不出 Python ${PYC_VER}（需要联网；已试直连 + 清华/阿里 PyPI + npmmirror/南大 GitHub 镜像）"
    echo "  可手动装一个再重跑本脚本，例如："
    echo "    Ubuntu/Debian: sudo apt install -y software-properties-common && sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python${PYC_VER} python${PYC_VER}-venv"
    echo "    或：curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install ${PYC_VER}"
    exit 3
  }
  echo "✓ Python ${PYC_VER}：$PYPICK"
fi
if [ -z "$PYPICK" ]; then PYPICK="python3"; fi

# 1) venv + 依赖（默认源失败自动换清华镜像）
#    有些发行版把 ensurepip 从 python3-venv 里拆出去了，venv 装得出来但里面没有 pip。
#    这时依次退：标准 venv → virtualenv（自带 pip）→ 系统 python + pip --user
#    （造一个 .venv/bin/{python,pip} 的等价布局，脚本其余部分的路径都不用改）。
PIP_EXTRA=""
make_venv() {
  # 重复执行=升级：现成的虚拟环境能用（且小版本对得上）就复用，别每次重建
  # （重建要重下全部依赖；版本不对则 pyc 加载会 bad magic number，必须重建）
  if [ -x "$HERE/.venv/bin/python" ] && "$HERE/.venv/bin/python" -m pip --version >/dev/null 2>&1 &&
     { [ -z "$PYC_VER" ] || _pyver_ok "$HERE/.venv/bin/python"; }; then
    echo "✓ 复用已有虚拟环境（$HERE/.venv）"
    return 0
  fi
  rm -rf "$HERE/.venv.old"
  [ -d "$HERE/.venv" ] && rm -rf "$HERE/.venv"
  if "$PYPICK" -m venv "$HERE/.venv" >/dev/null 2>&1 &&
     "$HERE/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "✓ venv 就绪（$HERE/.venv）"
    return 0
  fi
  rm -rf "$HERE/.venv"
  echo "· 标准 venv 不可用（多半缺 python3-venv / ensurepip），改用 virtualenv…"
  command -v virtualenv >/dev/null 2>&1 || python3 -m pip install -q --user virtualenv >/dev/null 2>&1 || true
  for _ve in virtualenv "$HOME/.local/bin/virtualenv"; do
    command -v "$_ve" >/dev/null 2>&1 || [ -x "$_ve" ] || continue
    "$_ve" -p "$PYPICK" "$HERE/.venv" >/dev/null 2>&1 || continue
    if "$HERE/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
      echo "✓ virtualenv 就绪（$HERE/.venv）"
      return 0
    fi
  done
  rm -rf "$HERE/.venv"
  echo "· 装不出虚拟环境，改用系统 python3 + pip --user…"
  python3 -m pip --version >/dev/null 2>&1 || {
    echo "✗ 这台机器既没有可用的 venv，也没有 pip。装完再跑一次："
    echo "    Debian/Ubuntu: sudo apt install -y python3-venv python3-pip"
    exit 3
  }
  mkdir -p "$HERE/.venv/bin"
  ln -sf "$PYPICK" "$HERE/.venv/bin/python"
  ln -sf "$PYPICK" "$HERE/.venv/bin/python3"
  printf '#!/bin/sh\nexec "%s" -m pip "$@"\n' "$PYPICK" > "$HERE/.venv/bin/pip"
  chmod +x "$HERE/.venv/bin/pip"
  PIP_EXTRA="--user"
  echo "✓ 系统 python 就绪（$HERE/.venv 是兼容布局，依赖装在 ~/.local）"
}
make_venv

# 自检：无源码包的 .pyc 必须和解释器小版本一致，否则服务起不来（bad magic number）。
# 在这里就拦住并给出能照做的提示，别等 systemd 反复重启、用户只看到一个 traceback。
if [ -f "$HERE/server_impl.pyc" ]; then
  if ! "$PY" - "$HERE/server_impl.pyc" <<'PYEOF'
import sys, importlib.util
m = open(sys.argv[1], 'rb').read(4)
sys.exit(0 if m == importlib.util.MAGIC_NUMBER else 1)
PYEOF
  then
    echo "✗ 安装包的 .pyc 需要 Python ${PYC_VER:-?}，但当前虚拟环境是 $("$PY" -V 2>&1)"
    echo "  这台机器没装上 Python ${PYC_VER:-?}（需要联网或用 uv 装）。装好后重跑本脚本；"
    echo "  或改用源码包（CB_PUB_SRC=1 发的包）安装。"
    exit 3
  fi
fi

pip_install() {
  if ! "$PY" -m pip --version >/dev/null 2>&1; then "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true; fi
  if "$PY" -m pip --version >/dev/null 2>&1; then
    # --only-binary=av：av(PyAV) 是原生扩展，源码包常要 pkg-config/ffmpeg 才能编，
    # 强制只考虑有轮子的版本，pip 会自动退到匹配当前 Python 的那一版。
    # 源自动切换：直连 PyPI 不通就依次换清华、阿里云（见 _pip_try）。
    _pip_try "$PY" $PIP_EXTRA --only-binary=av -r "$HERE/requirements.txt"
  elif command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$PY" -r "$HERE/requirements.txt" ||
      uv pip install -q --python "$PY" --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r "$HERE/requirements.txt" ||
      echo "! 依赖安装失败，请手动：uv pip install --python $PY -r $HERE/requirements.txt"
  else
    echo "! 未找到 pip/uv，依赖可能不完整：$HERE/requirements.txt"
  fi
}

if ! "$PY" -c "import aiohttp, pyte, qrcode" >/dev/null 2>&1; then
  pip_install
fi

# aiortc 单独查：缺了就没有广域网 P2P（手机上显示「开发机没启用 P2P」）。失败不阻断安装。
if ! "$PY" -c "import aiortc" >/dev/null 2>&1; then
  echo "· 缺 aiortc（广域网 P2P 打洞要它），补装…"
  _pip_try "$PY" $PIP_EXTRA --only-binary=av "aiortc>=1.10" >/dev/null 2>&1 || true
  if "$PY" -c "import aiortc" >/dev/null 2>&1; then
    echo "· aiortc 就绪（$("$PY" -c 'import aiortc;print(aiortc.__version__)')）"
  else
    echo "! aiortc 装不上：这台开发机的广域网 P2P 关闭，只走局域网直连/中继（不影响其它功能）"
  fi
fi

# 2) systemd --user 单元（开机自启 + 崩溃拉起）
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$LABEL.service" <<EOF
[Unit]
Description=BossHost 工头主机
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=$PY $HERE/server.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:$HERE/logs/server.log
StandardError=append:$HERE/logs/server.err.log

[Install]
WantedBy=default.target
EOF

# 没登录也要常驻（否则 SSH 断开后服务就没了）
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" 2>/dev/null || \
    echo "! loginctl enable-linger 失败（需 root：loginctl enable-linger ${USER}）；不开的话退出登录服务会停"
fi

systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable "$LABEL" >/dev/null 2>&1 || true
systemctl --user restart "$LABEL" 2>/dev/null || {
  echo "✗ systemd --user 启动失败，看日志：journalctl --user -u $LABEL -n 50"
  exit 1
}
echo "✓ systemd --user 服务已就绪（${LABEL}）"

# 2.5) 命令行客户端：终端输入 bosshost 就是菜单（状态/二维码/会话/启停/更新…，
#      与 macOS 菜单栏 App 同一套功能）。装成软链，指向安装目录里的 codeboss-cli，
#      升级时脚本随程序文件一起更新，不用重装软链。codeboss 留作兼容别名。
if [ -f "$HERE/codeboss-cli" ]; then
  chmod +x "$HERE/codeboss-cli" 2>/dev/null || true
  CLI_DIR="/usr/local/bin"
  [ -w "$CLI_DIR" ] || CLI_DIR="$HOME/.local/bin"
  mkdir -p "$CLI_DIR" 2>/dev/null || true
  if ln -sf "$HERE/codeboss-cli" "$CLI_DIR/bosshost" 2>/dev/null; then
    ln -sf "$HERE/codeboss-cli" "$CLI_DIR/codeboss" 2>/dev/null || true
    echo "✓ 命令行客户端：${CLI_DIR}/bosshost（终端输入 bosshost 打开菜单；老名字 codeboss 也还能用）"
    case ":$PATH:" in
      *":$CLI_DIR:"*) ;;
      *) echo "  ! ${CLI_DIR} 不在 PATH 里，把这行加进 ~/.bashrc：export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
  else
    echo "! 命令行客户端软链失败；可直接运行 $HERE/codeboss-cli"
  fi
fi

# 3) shell alias（幂等）：手敲 claude 也透明接入全彩镜像
#    Linux 上多半是 bash（mac 是 zshrc），两个都写，哪个存在写哪个
if [ -n "$CB_NO_SHELL" ]; then echo "• CB_NO_SHELL 已设，跳过 shell 配置"; fi
if [ -z "$CB_NO_SHELL" ]; then
_write_alias() {
  _rc="$1"
  [ -f "$_rc" ] || return 0
  for _old in ptyhost claudecast; do
    if grep -q "$_old 全彩镜像" "$_rc" 2>/dev/null; then
      sed -i "/# >>> $_old 全彩镜像/,/# <<< $_old 全彩镜像 <<</ d" "$_rc"
      echo "✓ 已替换旧 $_old alias 块（${_rc}）"
    fi
  done
  if ! grep -q "codeboss 全彩镜像" "$_rc" 2>/dev/null; then
    cat >> "$_rc" <<EOF2

# >>> codeboss 全彩镜像：手敲的 claude 透明过一层 pty 代理（终端显示不变，多 tee 一份彩色流给手机端；撤销=删这 3 行）
alias claude='$PY $HERE/wrapper.py -- claude'
# <<< codeboss 全彩镜像 <<<
EOF2
    echo "✓ 已写入 alias（${_rc}，新开终端生效）"
  else
    echo "• alias 已存在，跳过（${_rc}）"
  fi
}
for _rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$_rc" ] && _write_alias "$_rc"
done
if [ ! -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.zshrc" ]; then
  _write_alias "$HOME/.bashrc"
fi
fi

# 4) 配置（auto_migrate：服务自动把空闲的旧会话转全彩；基建目录保护不迁）
if [ ! -f "$HERE/config.json" ]; then
  printf '{\n  "auto_migrate": true,\n  "protect_cwd_prefixes": ["%s"]\n}\n' "$HOME" > "$HERE/config.json"
fi

# 5) 本机 claude 检测（服务会自动发现并遥控终端里的 claude 会话）
CLAUDE_HINT="未检测到 claude 命令——装好 Claude Code 后新开终端敲 claude 即自动接入"
for c in /usr/bin/claude /usr/local/bin/claude "$HOME/.local/bin/claude" \
         "$HOME/.npm-global/bin/claude" /snap/bin/claude; do
  [ -x "$c" ] && CLAUDE_HINT="✓ 本机 claude：${c}（会话发现自动生效）" && break
done
command -v claude >/dev/null && CLAUDE_HINT="✓ 本机 claude：$(command -v claude)（会话发现自动生效）"

# tmux 是「可见终端」的等价物：召回会话/抓屏/往终端注入都靠它
TMUX_HINT="✓ tmux 已安装（召回会话、抓屏、注入都靠它）"
if ! command -v tmux >/dev/null 2>&1; then
  TMUX_HINT="! 没装 tmux：召回会话（手机端点「打开终端」）不可用。Debian/Ubuntu：sudo apt install -y tmux"
fi

echo "✓ 部署完成"
echo "  $CLAUDE_HINT"
echo "  $TMUX_HINT"

# 6) 中继登记 + 绑定二维码：手机 App 扫码即绑定，之后不在同一网络也能远程用。
#    重复运行本脚本（升级）会换一张新码，不会重复登记。
RELAY="${CB_RELAY:-https://relay.example.com}"
echo ""
PAIR_OK=""
if "$PY" -c "import json,sys;r=(json.load(open(r'$HERE/config.json')).get('relay') or {});sys.exit(0 if (r.get('device_id') and r.get('device_secret')) else 1)" 2>/dev/null; then
  echo "== 这台机器已登记过，换一张新的配对码 =="
  "$PY" "$HERE/server.py" --paircode && PAIR_OK=1 || true
else
  echo "== 登记这台开发机 =="
  "$PY" "$HERE/server.py" --enroll "$RELAY" "$(hostname)" && PAIR_OK=1 || true
fi
if [ -n "$PAIR_OK" ]; then
  systemctl --user restart "$LABEL" 2>/dev/null || true
  echo ""
  echo "✓ 服务已重启。手机打开工头 App（口袋工头 / 代理工头） →「接入开发机」→「📷 扫码接入」，扫上面的二维码（15 分钟内有效）"
  echo "  绑定后这台开发机会出现在设备栏，不在同一网络也能远程用。"
else
  echo ""
  echo "! 中继登记失败（多半是这台机器不通外网）。服务已装好，可以先按局域网方式用："
  # 局域网备用：手机系统相机扫（App 的扫码器只认配对码，不认这个地址）
  TOKEN=""
  for _i in $(seq 1 20); do
    [ -f "$HERE/token.txt" ] && { TOKEN=$(head -n 1 "$HERE/token.txt"); [ -n "$TOKEN" ] && break; }
    sleep 1
  done
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$IP" ] && IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  [ -z "$IP" ] && IP="127.0.0.1"
  if [ -z "$TOKEN" ]; then
    echo "  （token.txt 还没生成，稍后重跑本脚本即可出码）"
  else
    # 端口以 host 实际监听为准（8790 被别的程序占用时它会换端口，见 logs/port.txt）
    LPORT="$(tr -d '[:space:]' < "$HERE/logs/port.txt" 2>/dev/null)"
    [ -n "$LPORT" ] || LPORT=8790
    URL="http://$IP:$LPORT/?t=$TOKEN"
    echo "  手机用系统相机扫（同一个 WiFi，$IP:$LPORT）："
    QRPY=""
    for p in "$PY" python3; do
      if "$p" -c "import qrcode" >/dev/null 2>&1; then QRPY="$p"; break; fi
    done
    if [ -n "$QRPY" ]; then
      "$QRPY" - "$URL" <<'PYEOF'
import sys, qrcode
q = qrcode.QRCode(border=1)
q.add_data(sys.argv[1])
q.print_ascii(invert=True)
PYEOF
    else
      echo "  缺少二维码组件，先执行：$PY -m pip install qrcode"
    fi
    echo "  能连外网之后随时补登记：重新运行本安装脚本即可"
  fi
fi
echo ""
echo "  服务状态：systemctl --user status $LABEL"
echo "  看日志：journalctl --user -u $LABEL -f"
