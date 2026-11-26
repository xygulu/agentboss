#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# codeboss 一键部署（幂等，可重复执行=升级）：依赖 → launchd 自启 → zshrc alias → 配置
# 两种安装源：
#   · git 仓库内运行  → 原地安装/升级（支持服务内一键 git 更新）
#   · DMG/压缩包运行  → 程序装到 ~/.codeboss，装完安装包可推出删除，服务照常运行
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
# CB_LABEL / CB_HOME / CB_NO_SHELL 只给 Mac 客户端的自测用（临时 label + 临时目录，
# 不碰本机真服务、不改 zshrc）；正常安装不设这些变量，行为与以前一致。
LABEL="${CB_LABEL:-com.xuxingyu.codeboss}"
PLIST_DST=~/Library/LaunchAgents/$LABEL.plist

echo "== codeboss 部署 =="

# 0) 环境
if [ "$(uname -s)" != "Darwin" ]; then echo "✗ 仅支持 macOS"; exit 1; fi
# python3 不再是硬前置：安装包里自带解释器（见 0.5 / 0.9），机器上有没有都一样

# 0.5) 定安装位置
if git -C "$SRC" rev-parse --show-toplevel >/dev/null 2>&1; then
  HERE="$SRC"   # git 部署：原地升级
  if git -C "$HERE" pull --ff-only 2>/dev/null; then echo "✓ 已同步最新代码"; fi
else
  HERE="${CB_HOME:-$HOME/Library/codeboss}"
  mkdir -p "$HERE"
  # 只复制程序文件，不碰数据（token.txt / sessions.json / config.json / logs / .venv 原地保留）
  for f in server.py wrapper.py relay.py plat.py pty_backend.py agents.py terms.py agent_codex.py requirements.txt \
           install.sh install-linux.sh \
           codeboss-cli uninstall.sh uninstall-linux.sh dist.sh VERSION PYC_VER \
           README.md 安装.command 安装与使用说明.md 规划-全平台与CCswitch.md; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$HERE/"
  done
  # 无源码包：核心代码只有 .pyc（server.py/wrapper.py 是入口薄壳）
  cp -f "$SRC"/*.pyc "$HERE/" 2>/dev/null || true
  # 同名旧 .py 会压过新 .pyc（Python 优先源码）：老 branding.py 赖着不走，
  # 机器「更新成功」却一直跑老代码。包里编成 pyc 的同名旧源码拷完就清掉；
  # 入口薄壳 server.py/wrapper.py 除外。
  for p in "$SRC"/*.pyc; do
    [ -e "$p" ] || continue
    b=$(basename "$p" .pyc)
    { [ "$b" = server ] || [ "$b" = wrapper ]; } && continue
    rm -f "$HERE/$b.py"
  done
  # pet 是包（目录），*.pyc 通配符覆盖不到它：漏掉就会升级后还跑旧的桌宠代码
  # （2026-11-20：0.9.62 装了、pet/*.pyc 还是上一版，fallback 一直下发不出来）
  if [ -d "$SRC/pet" ]; then rm -rf "$HERE/pet"; cp -R "$SRC/pet" "$HERE/"; fi
  # 网页资源：新包里编在 static_pack.pyc（没有 static/ 目录），老包里才是目录
  rm -rf "$HERE/static"
  if [ -d "$SRC/static" ]; then cp -R "$SRC/static" "$HERE/"; fi
  # 自带 Python（安装包按架构带一份）：装进安装目录，之后不依赖机器上有没有 Python
  _arch="$(uname -m)"; [ "$_arch" = "x86_64" ] && _arch="x64"
  if [ -d "$SRC/python-$_arch" ]; then
    rm -rf "$HERE/python"
    cp -R "$SRC/python-$_arch" "$HERE/python"
    echo "✓ 自带 Python 已就位（不用机器上先装）"
  fi
  chmod +x "$HERE/install.sh" "$HERE/install-linux.sh" "$HERE/uninstall.sh" \
           "$HERE/uninstall-linux.sh" "$HERE/dist.sh" "$HERE/安装.command" "$HERE/codeboss-cli" 2>/dev/null || true
  echo "✓ 程序安装到 ${HERE}（数据保留，安装包可推出/删除）"
fi
PY="$HERE/.venv/bin/python"
BUNDLED="$HERE/python/bin/python3"
mkdir -p "$HERE/logs"

# 0.9) 无源码包锁死 Python 小版本（.pyc 跨小版本直接 bad magic number）：
#      先找现成匹配的解释器，没有就用 uv 装一个（一次性，约 30MB）
PYC_VER="$(cat "$HERE/PYC_VER" 2>/dev/null | tr -d '[:space:]')"
PYBIN=""
if [ -n "$PYC_VER" ]; then
  _pyver_ok() {
    "$1" -c "import sys;raise SystemExit(0 if '%d.%d' % sys.version_info[:2] == '$PYC_VER' else 1)" \
      >/dev/null 2>&1
  }
  # 安装包自带的解释器优先：版本钉死，不受机器环境影响（也就不用联网装）
  if [ -x "$BUNDLED" ] && _pyver_ok "$BUNDLED"; then
    PYBIN="$BUNDLED"
  else
    for _c in "$CB_PYTHON" "python$PYC_VER" python3; do
      [ -n "$_c" ] || continue
      _bin="$_c"
      if command -v "$_c" >/dev/null 2>&1; then _bin="$(command -v "$_c")"; fi
      if [ -x "$_bin" ] && _pyver_ok "$_bin"; then PYBIN="$_bin"; break; fi
    done
  fi
  if [ -z "$PYBIN" ] && [ -z "$BUNDLED" ] && ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 这台机器既没有 python3，安装包里也没有自带解释器。"
    echo "  先装命令行工具（弹出窗口点「安装」），装完再运行一次本安装脚本："
    xcode-select --install >/dev/null 2>&1 || true
    exit 3
  fi
  if [ -z "$PYBIN" ]; then
    echo "· 需要 Python ${PYC_VER}，正在准备（一次性，约 30MB）…"
    if ! command -v uv >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
      export PATH="$HOME/.local/bin:$PATH"
    fi
    if command -v uv >/dev/null 2>&1; then
      uv python install "$PYC_VER" >/dev/null 2>&1 || true
      PYBIN="$(uv python find "$PYC_VER" 2>/dev/null || true)"
    fi
  fi
  [ -n "$PYBIN" ] || { echo "✗ 装不出 Python ${PYC_VER}（需要联网）；安装中止"; exit 3; }
  echo "✓ Python ${PYC_VER}：$PYBIN"
fi

# 1) venv + 依赖（默认源失败自动换清华镜像）
rm -rf "$HERE/.venv.old"        # 上一轮换解释器留下的旧环境（服务早重启过了）
# 旧虚拟环境的小版本不对（从源码包升级到无源码包时最常见）就重建
if [ -n "$PYC_VER" ] && [ -x "$PY" ] && ! _pyver_ok "$PY"; then
  echo "· 旧虚拟环境不是 Python ${PYC_VER}，重建…"
  rm -rf "$HERE/.venv"
fi
if [ ! -x "$PY" ]; then
  if [ -n "$PYBIN" ]; then "$PYBIN" -m venv "$HERE/.venv"
  else python3 -m venv "$HERE/.venv"; fi
fi
# 自检：无源码包的 .pyc 必须和解释器小版本一致，否则服务起不来（bad magic number）。
# 在这里就拦住并给出能照做的提示，别等 launchd 反复重启、用户只看到一个 traceback。
if [ -f "$HERE/server_impl.pyc" ]; then
  if ! "$PY" - "$HERE/server_impl.pyc" <<'PYEOF'
import sys, importlib.util
m = open(sys.argv[1], 'rb').read(4)
sys.exit(0 if m == importlib.util.MAGIC_NUMBER else 1)
PYEOF
  then
    echo "✗ 安装包的 .pyc 需要 Python ${PYC_VER:-?}，但当前虚拟环境是 $("$PY" -V 2>&1)"
    echo "  装好 Python ${PYC_VER:-?} 后重跑本脚本，或改用源码包安装。"
    exit 3
  fi
fi
# 注意：这条检查要覆盖 requirements.txt 里的每个关键包（qrcode 出配对二维码用），
# 否则升级时新增的依赖永远装不上。venv 可能是 uv 建的（没有 pip），装依赖时退到 uv。
# 自动选源：先测速（HEAD，4 秒超时），快的排前面；探不通的排最后照样试一遍
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

pip_install() {   # 官方源 / 清华镜像，先测速再试
  if ! "$PY" -m pip --version >/dev/null 2>&1; then "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true; fi
  if "$PY" -m pip --version >/dev/null 2>&1; then
    # --only-binary=av：av(PyAV) 是原生扩展，老/新 Python 上最新版往往只有源码包，
    # pip 会挑最新版去编译，没有 pkg-config/ffmpeg 必挂（实测 3.9.6：
    # "Failed to build 'av'... pkg-config is required"）。强制它只考虑**有轮子的版本**，
    # pip 就会自动退到和当前 Python 匹配的那一版（3.9 → av 14.2.0，4 秒装完）。
    "$PY" -m pip -q install --only-binary=av -r "$HERE/requirements.txt" ||
      "$PY" -m pip -q install --only-binary=av -r "$HERE/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
  elif command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$PY" -r "$HERE/requirements.txt" ||
      echo "! 依赖安装失败，请手动：uv pip install --python $PY -r $HERE/requirements.txt"
  else
    echo "! 未找到 pip/uv，依赖可能不完整：$HERE/requirements.txt"
  fi
}

if ! "$PY" -c "import aiohttp, pyte, qrcode" >/dev/null 2>&1; then
  pip_install
fi

# aiortc 单独查：缺了就没有广域网 P2P（手机上显示「开发机没启用 P2P」）。
# 装它用 --only-binary=av（原因见上面 pip_install 里的注释），失败不阻断安装。
if ! "$PY" -c "import aiortc" >/dev/null 2>&1; then
  echo "· 缺 aiortc（广域网 P2P 打洞要它），补装…"
  "$PY" -m pip -q install --only-binary=av "aiortc>=1.10" >/dev/null 2>&1 ||
    "$PY" -m pip -q install --only-binary=av "aiortc>=1.10" \
      -i https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1 || true
  if "$PY" -c "import aiortc" >/dev/null 2>&1; then
    echo "· aiortc 就绪（$("$PY" -c 'import aiortc;print(aiortc.__version__)')）"
  else
    echo "! aiortc 装不上：这台开发机的广域网 P2P 关闭，只走局域网直连/中继（不影响其它功能）"
  fi
fi

# 2) launchd（开机自启 + 崩溃拉起）——plist 用实际安装路径现生成
# 历史命名迁移（ptyhost/claudecast → codeboss，一次性）：卸旧 label、删旧 plist
# 自测（CB_LABEL）跳过：别把本机真服务顶掉
if [ -z "$CB_LABEL" ]; then
  launchctl bootout gui/$(id -u)/com.xuxingyu.ptyhost 2>/dev/null || true
  launchctl bootout gui/$(id -u)/com.xuxingyu.claudecast 2>/dev/null || true
  rm -f ~/Library/LaunchAgents/com.xuxingyu.ptyhost.plist ~/Library/LaunchAgents/com.xuxingyu.claudecast.plist
fi
mkdir -p "$(dirname "$PLIST_DST")"
cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$HERE/server.py</string>
    </array>
    <key>WorkingDirectory</key><string>$HERE</string>
    <key>RunAtLoad</key><true/>
    <!-- 只拉起「异常退出」：干净退出（重复实例检测、客户端主动停止）不再重启，
         崩溃/端口被抢仍然拉。85/75 之类「重启我」的退出码都算失败。 -->
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$HERE/logs/server.log</string>
    <key>StandardErrorPath</key><string>$HERE/logs/server.err.log</string>
</dict>
</plist>
EOF
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
# 客户端「停止服务」用的是 launchctl disable（跨重启持久）；不先 enable，
# bootstrap 会一直报 "Service is disabled"，安装脚本看起来就是装不上。
launchctl enable gui/$(id -u)/$LABEL 2>/dev/null || true
# bootout 是异步的：立即 bootstrap 会偶发 Input/output error，重试即可
_OK=""
for _i in 1 2 3 4 5; do
  sleep 1
  if launchctl bootstrap gui/$(id -u) "$PLIST_DST" 2>/dev/null; then _OK=1; break; fi
done
[ -n "$_OK" ] || { echo "✗ launchd 注册失败，请重新执行本安装"; exit 1; }

# 2b) 装完/更新完把菜单栏 App 拉起来：用户装完/更新完就该看到图标，而不是自己去
#     「应用程序」里找。App 正在跑时 open 只是激活它；机器上没有 App、或没有图形
#     会话（SSH 里装）就静默跳过。
_CBAPP=""
for _a in /Applications/BossHost.app /Applications/CodeBoss.app \
          "$HOME/Applications/BossHost.app" "$HOME/Applications/CodeBoss.app"; do
  [ -d "$_a" ] && _CBAPP="$_a" && break
done
if [ -n "$_CBAPP" ] && [ -z "$CB_NO_OPEN_APP" ]; then
  open "$_CBAPP" >/dev/null 2>&1 && echo "✓ 已打开菜单栏 App"
fi

# 3) zshrc alias（幂等）：手敲 claude 也透明接入全彩镜像；历史命名块（ptyhost/claudecast）一并替换
#    CB_NO_SHELL=1（客户端自测）跳过：不碰用户 shell 配置
if [ -n "$CB_NO_SHELL" ]; then echo "• CB_NO_SHELL 已设，跳过 zshrc 配置"; fi
if [ -z "$CB_NO_SHELL" ]; then
for _old in ptyhost claudecast; do
  if grep -q "$_old 全彩镜像" ~/.zshrc 2>/dev/null; then
    sed -i '' "/# >>> $_old 全彩镜像/,/# <<< $_old 全彩镜像 <<</ d" ~/.zshrc
    echo "✓ 已替换旧 $_old alias 块"
  fi
done
if ! grep -q "codeboss 全彩镜像" ~/.zshrc 2>/dev/null; then
  cat >> ~/.zshrc <<EOF

# >>> codeboss 全彩镜像：手敲的 claude 透明过一层 pty 代理（终端显示不变，多 tee 一份彩色流给手机端；撤销=删这 3 行）
alias claude='$PY $HERE/wrapper.py -- claude'
# <<< codeboss 全彩镜像 <<<
EOF
  echo "✓ 已写入 zshrc alias（新开终端标签生效）"
else
  echo "• zshrc alias 已存在，跳过"
fi
fi

# 4) 配置（auto_migrate：服务自动把空闲的旧会话转全彩；基建目录保护不迁）
if [ ! -f "$HERE/config.json" ]; then
  printf '{\n  "auto_migrate": true,\n  "protect_cwd_prefixes": ["%s"]\n}\n' "$HOME" > "$HERE/config.json"
fi

# 5) 本机 claude 检测（服务会自动发现并遥控 Terminal.app 里的 claude 会话）
CLAUDE_HINT="未检测到 claude 命令——装好 Claude Code 后新开终端敲 claude 即自动接入"
for c in "$HOME/.npm-global/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude "$HOME/.local/bin/claude"; do
  [ -x "$c" ] && CLAUDE_HINT="✓ 本机 claude：${c}（会话发现自动生效）" && break
done
command -v claude >/dev/null && CLAUDE_HINT="✓ 本机 claude：$(command -v claude)（会话发现自动生效）"

sleep 2
TOKEN=""
[ -f "$HERE/token.txt" ] && TOKEN=$(head -n 1 "$HERE/token.txt")
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 本机IP)"
# 端口以 host 实际监听为准（8790 被别的程序占用时它会换端口，见 logs/port.txt）
LPORT="$(tr -d '[:space:]' < "$HERE/logs/port.txt" 2>/dev/null)"
[ -n "$LPORT" ] || LPORT=8790
# 带 token 的访问链接只走二维码：terminal 回滚区/日志里不留明文密钥
URL="http://$IP:$LPORT/?t=$TOKEN"
echo "✓ 部署完成"
echo "  $CLAUDE_HINT"
echo "  首次遥控 Terminal 会弹权限弹窗，点「允许」即可"
echo ""
echo "  手机（同一 Wi-Fi，$IP:$LPORT）扫码打开："
QRPY=""
for p in "$PY" python3; do
  if "$p" -c "import qrcode" >/dev/null 2>&1; then QRPY="$p"; break; fi
done
if [ -z "$TOKEN" ]; then
  echo "  （未找到 token.txt，服务首次启动后会生成，稍后重跑本脚本即可出码）"
elif [ -z "$QRPY" ]; then
  echo "  缺少二维码组件，先执行：$PY -m pip install qrcode"
  echo "  然后重跑本脚本。访问密钥见 $HERE/token.txt（勿贴到聊天/日志）。"
else
  "$QRPY" - "$URL" <<'PYEOF'
import sys, qrcode
q = qrcode.QRCode(border=1)
q.add_data(sys.argv[1])
q.print_ascii(invert=True)
PYEOF
fi
