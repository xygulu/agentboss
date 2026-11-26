#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 打发行包：bosshost-<时间>.tar.gz（不含 token/会话记录/日志/venv 等本机数据）
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # host/
ROOT="$(cd "$HERE/.." && pwd)"            # 仓库根
OUT="$(dirname "$HERE")"
NAME="bosshost-$(date +%Y%m%d-%H%M)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 代码打成 .pyc（无源码）+ 静态页 + 安装脚本；绝不带
# token.txt / sessions.json / logs / .venv / config.json
PYVER="${CB_PYC_PY_VER:-3.11}"
CB_PYC_PY_VER="$PYVER" "$HERE/build-pyc.sh" >/dev/null || { echo "✗ 无源码包构建失败"; exit 1; }
ABI="${PYVER//./}"
cp -R "$HERE/build/pyc/$ABI" "$TMP/$NAME"

# 自带 Python：两个 Mac 架构各一份（装完不依赖机器上的 python3，也不用联网装）
for a in arm64 x64; do
  "$ROOT/tools/fetch_python.sh" "mac-$a" "$TMP/py-$a" >/dev/null \
    || { echo "✗ 取不到 mac-$a 的 Python"; exit 1; }
  mv "$TMP/py-$a/python" "$TMP/$NAME/python-$a"
  rm -rf "$TMP/py-$a"
done

TAR="$OUT/$NAME.tar.gz"
tar -czf "$TAR" -C "$TMP" "$NAME"
echo "✓ 发行包：$TAR"

# DMG：挂载即见的安装盘（内容平铺在卷根目录，双击 安装.command 即装；服务装到 ~/.codeboss，与盘无关）
DMGROOT="$TMP/dmg"
mkdir -p "$DMGROOT"
cp -R "$TMP/$NAME/" "$DMGROOT/"
DMG="$OUT/$NAME.dmg"
hdiutil create -volname "BossHost 工头主机 安装" -srcfolder "$DMGROOT" -ov -format UDZO "$DMG" >/dev/null
echo "✓ 镜像：$DMG"

# macOS 菜单栏客户端（有 swiftc 才打；DMG 里是 BossHost.app，自带 host 源码）
if command -v swiftc >/dev/null && [ -x "$ROOT/clients/mac/build-app.sh" ]; then
  echo "== macOS 菜单栏客户端 =="
  "$ROOT/clients/mac/build-app.sh" || echo "• Mac 客户端打包失败（跳过；不影响其他产物）"
else
  echo "• 没有 swiftc，跳过 macOS 客户端"
fi

echo "  内容清单："
tar -tzf "$TAR" | sed 's/^/    /'
