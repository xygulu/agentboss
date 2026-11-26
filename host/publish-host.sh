#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 把 host 源码包发布到中继 —— Mac 客户端「检查更新」的更新源。
# 用法: ./publish-host.sh [relay_base]      默认 https://relay.example.com
# 令牌：RELAY_ADMIN_TOKEN（优先环境变量，其次 ~/.codeboss/.env 里的同名行）；
#      令牌只在脚本内部使用，不进命令行参数、不打印。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # host/
ROOT="$(cd "$HERE/.." && pwd)"            # 仓库根
RELAY="${1:-https://relay.example.com}"
RELAY="${RELAY%/}"

VER="$(cat "$HERE/VERSION" 2>/dev/null | tr -d '[:space:]')"
[ -n "$VER" ] || { echo "✗ 缺 VERSION 文件"; exit 1; }

TOK="${RELAY_ADMIN_TOKEN:-}"
if [ -z "$TOK" ] && [ -f "$HOME/.codeboss/.env" ]; then
  TOK="$(grep -E '^[[:space:]]*RELAY_ADMIN_TOKEN=' "$HOME/.codeboss/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
fi
[ -n "$TOK" ] || { echo "✗ 没找到 RELAY_ADMIN_TOKEN（环境变量或 ~/.codeboss/.env）"; exit 1; }

# 公证用的钥匙串 profile（notarytool store-credentials 存过的那份名字）。
# 和令牌一样从 ~/.codeboss/.env 取，不然每次发版都要手动 export，忘了就静默跳过公证。
if [ -z "${AC_KEYCHAIN_PROFILE:-}" ] && [ -f "$HOME/.codeboss/.env" ]; then
  AC_KEYCHAIN_PROFILE="$(grep -E '^[[:space:]]*AC_KEYCHAIN_PROFILE=' "$HOME/.codeboss/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  [ -n "$AC_KEYCHAIN_PROFILE" ] && export AC_KEYCHAIN_PROFILE
fi

NAME="codeboss-$VER"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# 默认发无源码包（.pyc + 入口薄壳）；CB_PUB_SRC=1 发源码包，只给「引导版」用：
# 老版本（没有 pyc 守卫）收到无源码包会照直覆盖 git 检出，所以让所有机器先升到
# 带守卫的源码版，再推无源码包。build-pyc.sh 只读源码，绝不带
# token.txt / sessions.json / logs / .venv / config.json
mkdir -p "$TMP/$NAME"
if [ -n "${CB_PUB_SRC:-}" ]; then
  echo "→ 打源码包（引导版）…"
  for f in server.py wrapper.py plat.py pty_backend.py agents.py agent_codex.py codex_daemon.py outcome.py remind_rules.py notify.py boss.py transcript.py hooks.py static_res.py screen.py scrollback.py window_source.py acp.py agent_acp.py art_edit.py risk_review.py tray.py ptywin.py agent_opencode.py agent_hermes.py owned_sessions.py requirements.txt \
           install.sh install-linux.sh install.ps1 uninstall.ps1 安装.cmd \
           codeboss-cli uninstall.sh uninstall-linux.sh dist.sh VERSION \
           README.md 安装.command 安装与使用说明.md; do
    [ -f "$HERE/$f" ] && cp -f "$HERE/$f" "$TMP/$NAME/"
  done
  for f in relay/relay.py shared/terms.py shared/branding.py shared/brand_icons.py; do
    cp -f "$ROOT/$f" "$TMP/$NAME/$(basename "$f")"
  done
  # 发布物不留注释（同 build-pyc.sh）：说明性文字只留在仓库源码里
  for f in install.sh install-linux.sh uninstall.sh uninstall-linux.sh dist.sh codeboss-cli \
           install.ps1 uninstall.ps1 安装.cmd 安装.command requirements.txt server.py wrapper.py tray.py ptywin.py agent_codex.py codex_daemon.py outcome.py remind_rules.py notify.py boss.py transcript.py hooks.py static_res.py screen.py; do
    [ -f "$TMP/$NAME/$f" ] && python3 "$ROOT/tools/strip_comments.py" "$TMP/$NAME/$f"
  done
  cp -R "$HERE/static" "$TMP/$NAME/"
  cp -R "$HERE/pet" "$TMP/$NAME/" && rm -rf "$TMP/$NAME/pet/__pycache__"
  rm -rf "$TMP/$NAME/static/src"        # 前端源码（ext.html 的来源），只在仓库里
  rm -f "$TMP/$NAME/static/_preview.html"
  for f in ext.html term.html upd.js; do
    [ -f "$TMP/$NAME/static/$f" ] && python3 "$ROOT/tools/strip_web.py" "$TMP/$NAME/static/$f"
  done
else
  PYVER="${CB_PYC_PY_VER:-3.11}"
  echo "→ 编译无源码包（Python ${PYVER}）…"
  CB_PYC_PY_VER="$PYVER" "$HERE/build-pyc.sh" >/dev/null || { echo "✗ 无源码包构建失败"; exit 1; }
  ABI="${PYVER//./}"
  PKGSRC="$HERE/build/pyc/$ABI"
  [ -f "$PKGSRC/server_impl.pyc" ] || { echo "✗ 无源码包不完整：$PKGSRC"; exit 1; }
  # 守卫（2026-11-20 教训）：server.py 里 import 的本仓库模块，包里必须都有。
  # 少了就是装上一开就 ModuleNotFoundError——本机和服务端全下线，且没有回滚入口。
  for m in $(grep -hoE '^(import|from) [a-z_]+' "$HERE/server.py" | awk '{print $2}' | sort -u); do
    { [ -f "$HERE/$m.py" ] || [ -d "$HERE/$m" ] || [ -f "$ROOT/shared/$m.py" ] || [ -f "$ROOT/relay/$m.py" ]; } || continue
    { [ -f "$PKGSRC/$m.pyc" ] || [ -f "$PKGSRC/$m.py" ] || [ -d "$PKGSRC/$m" ]; } || \
      { echo "✗ 打包缺本地模块：$m（补进 build-pyc.sh 的拷贝清单）"; exit 1; }
  done
  cp -R "$PKGSRC/." "$TMP/$NAME/"
fi
# 目录型模块（pet/ 等）安装脚本必须显式拷：*.pyc 通配符覆盖不到目录。
# 漏掉 = 装上后 import 就 ModuleNotFoundError，进程死了连自动更新都跑不了（整个机器躺平）。
for _d in pet static; do
  [ -d "$TMP/$NAME/$_d" ] || continue
  for _s in install.sh install-linux.sh install.ps1; do
    [ -f "$TMP/$NAME/$_s" ] || continue
    grep -q -- "$_d" "$TMP/$NAME/$_s" || {
      echo "✗ 包里带 $_d/，但 $_s 里没有拷贝它的步骤"; exit 1; }
  done
done

tar -czf "$TMP/pkg.tgz" -C "$TMP" "$NAME"

# 更新包签名：host 里内置公钥，中继被拿下也换不了包。私钥只在维护者 Mac 上。
SEED="$HOME/.codeboss/host-signing.seed"
[ -f "$SEED" ] || { echo "✗ 缺 $SEED（host 更新包签名私钥，见 tools/ed25519.py genkey）"; exit 1; }
SHA="$(shasum -a 256 "$TMP/pkg.tgz" | cut -d' ' -f1)"
printf '%s' "$VER|$SHA" > "$TMP/msg.bin"
SIG="$(python3 "$ROOT/tools/ed25519.py" sign "$(tr -d '[:space:]' < "$SEED")" "$TMP/msg.bin")"
[ ${#SIG} -eq 128 ] || { echo "✗ 签名失败"; exit 1; }

DONE="$(bash "$ROOT/tools/oss_put.sh" "$RELAY" "$TOK" codeboss-host.tar.gz "$TMP/pkg.tgz")"
HDR=()
if [ -n "$DONE" ]; then HDR=(-H "X-Oss-Done: 1"); echo "→ 已直传 OSS（中继不再镜像）"; fi

echo "→ 发布 v$VER 到 $RELAY …"
RESP="$(curl -sS -m 120 -X POST "$RELAY/r/admin/hostpkg" \
  -H "Authorization: Bearer $TOK" \
  -H "X-Pkg-Sig: $SIG" \
  "${HDR[@]+"${HDR[@]}"}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$TMP/pkg.tgz")"
echo "$RESP"
case "$RESP" in
  *'"ok": true'*|*'"ok":true'*) ;;
  *) echo "✗ 发布失败"; exit 1 ;;
esac
# 同版本发布 Mac 客户端：界面只显示一个版本号，服务或客户端任一更新都 bump VERSION。
# 打 DMG 需要 swiftc（Xcode CLT）；跳过用 SKIP_MAC=1。
if [ -z "${SKIP_MAC:-}" ] && command -v swiftc >/dev/null && [ -x "$ROOT/clients/mac/build-app.sh" ]; then
  echo "→ 打 Mac 客户端 v$VER …"
  if "$ROOT/clients/mac/build-app.sh"; then
    DMG="$(cd "$HERE/.." && pwd)/bosshost-$VER-mac.dmg"
    if [ -f "$DMG" ]; then
      DONE="$(bash "$ROOT/tools/oss_put.sh" "$RELAY" "$TOK" bosshost-mac.dmg "$DMG")"
      HDR=()
      if [ -n "$DONE" ]; then HDR=(-H "X-Oss-Done: 1"); fi
      echo "→ 上架客户端 DMG v$VER …"
      RESP2="$(curl -sS -m 300 -X POST "$RELAY/r/admin/macdmg?ver=$VER" \
        -H "Authorization: Bearer $TOK" \
        "${HDR[@]+"${HDR[@]}"}" \
        -H "Content-Type: application/octet-stream" \
        --data-binary @"$DMG")"
      echo "$RESP2"
      case "$RESP2" in
        *'"ok": true'*|*'"ok":true'*) ;;
        *) echo "⚠ DMG 上架失败（host 包已发布，服务更新不受影响）" ;;
      esac
    fi
  else
    echo "⚠ DMG 打包失败（跳过；host 包已发布）"
  fi
else
  echo "• 跳过 Mac 客户端（无 swiftc 或 SKIP_MAC=1）"
fi

# Windows 安装包必须跟着发：官网「下载 Windows 版」直连的就是它。以前是手工打的，
# 结果官网写着 0.9.78、下载下来还是 0.9.38 的包（2026-11-21）。现在发布时现打现传，
# 打不出来就报错——能少发一次，不能发个对不上版本号的。
if [ -z "${SKIP_WIN:-}" ]; then
  if ! command -v makensis >/dev/null; then
    echo "✗ 缺 makensis，Windows 安装包没更新（官网会继续发旧版）：brew install makensis"
    exit 1
  fi
  echo "→ 打 Windows 安装包 v$VER …"
  if OUT="$TMP/BossHost-Setup.exe" "$ROOT/clients/win/build-installer.sh" "$TMP/pkg.tgz" \
     && [ -s "$TMP/BossHost-Setup.exe" ]; then
    DONE="$(bash "$ROOT/tools/oss_put.sh" "$RELAY" "$TOK" BossHost-Setup.exe "$TMP/BossHost-Setup.exe")"
    HDR=()
    if [ -n "$DONE" ]; then HDR=(-H "X-Oss-Done: 1"); fi
    echo "→ 上架 Windows 安装包 v$VER …"
    RESP3="$(curl -sS -m 300 -X POST "$RELAY/r/admin/distpkg?name=BossHost-Setup.exe" \
      -H "Authorization: Bearer $TOK" \
      "${HDR[@]+"${HDR[@]}"}" \
      -H "Content-Type: application/octet-stream" \
      --data-binary @"$TMP/BossHost-Setup.exe")"
    echo "$RESP3"
    case "$RESP3" in
      *'"ok": true'*|*'"ok":true'*) ;;
      *) echo "✗ Windows 安装包上架失败（官网下载会拿到旧包）"; exit 1 ;;
    esac
  else
    echo "✗ Windows 安装包打包失败（官网下载会拿到旧包）"; exit 1
  fi
else
  echo "• 跳过 Windows 安装包（SKIP_WIN=1）——官网下载会还是旧版"
fi

echo "→ 中继版本状态（灰度/全员基线）："
curl -sS -m 20 -H "Authorization: Bearer $TOK" "$RELAY/r/admin_api/canary" | python3 -c '
import json, sys
d = json.load(sys.stdin)
c = d.get("default") or {}
print("  最新包 v%s · 全员基线 v%s · APK 基线 %s · 灰度规则 %d 条"
      % ((d.get("pkgs") or ["?"])[0], c.get("ver") or "=最新", c.get("apk") or "=最新",
         len(d.get("rules") or [])))
print("  新版本默认只推给灰度名单（后台「版本」页加对象）；自己测好后点「全量发布」。")
' || echo "  （取状态失败，不影响已上架的包）"
echo
