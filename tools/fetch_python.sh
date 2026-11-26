#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 取一份可移植的 Python 运行时（python-build-standalone），解到 <目标目录>/python。
# 安装包把它一起打进去，装完就不再依赖机器上有没有 Python、也不用联网装解释器。
#
# 用法：fetch_python.sh <win|mac-arm64|mac-x64|linux-x64|linux-arm64> <目标目录>
#
# 版本钉死，和 build-pyc.sh 的 PYC_VER 对齐（.pyc 的 magic 号绑小版本）。
# 下载缓存在 ~/.cache/codeboss-pystand；sha256 取自 release 的 SHA256SUMS，逐个校验。
# 直连 GitHub 不通时自动走本机 7897 代理（国内开发机基本都要）。
set -e

PYST_TAG="20261026"
PYST_VER="3.11.18"

case "$1" in
  win)        ST="x86_64-pc-windows-msvc" ;;
  mac-arm64)  ST="aarch64-apple-darwin" ;;
  mac-x64)    ST="x86_64-apple-darwin" ;;
  linux-x64)  ST="x86_64-unknown-linux-gnu" ;;
  linux-arm64) ST="aarch64-unknown-linux-gnu" ;;
  "")         echo "用法：fetch_python.sh <win|mac-arm64|mac-x64|linux-x64|linux-arm64> <目标目录>"; exit 2 ;;
  *)          echo "✗ 不认识的平台：$1"; exit 2 ;;
esac
DEST="$2"
[ -n "$DEST" ] || { echo "✗ 缺目标目录"; exit 2; }
mkdir -p "$DEST"

NAME="cpython-$PYST_VER+$PYST_TAG-$ST-install_only_stripped.tar.gz"
REL="https://github.com/astral-sh/python-build-standalone/releases/download/$PYST_TAG"
CACHE="$HOME/.cache/codeboss-pystand"
mkdir -p "$CACHE"

_dl() {   # $1=url $2=输出
  curl -fsSL -m 900 -o "$2" "$1" 2>/dev/null && return 0
  curl -fsSL -m 900 -x http://127.0.0.1:7897 -o "$2" "$1" 2>/dev/null && return 0
  return 1
}

if [ ! -f "$CACHE/SHA256SUMS" ]; then
  _dl "$REL/SHA256SUMS" "$CACHE/SHA256SUMS" || { echo "! 拿不到校验清单"; rm -f "$CACHE/SHA256SUMS"; }
fi
WANT=""
[ -f "$CACHE/SHA256SUMS" ] && WANT="$(grep -F " $NAME" "$CACHE/SHA256SUMS" | cut -d' ' -f1)"

if [ ! -f "$CACHE/$NAME" ]; then
  echo "→ 下载 Python 运行时 $ST（约 30MB，一次性，之后走缓存）…"
  _dl "$REL/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$NAME")" "$CACHE/$NAME" \
    || { echo "! 下载失败"; rm -f "$CACHE/$NAME"; }
fi
if [ ! -f "$CACHE/$NAME" ]; then
  echo "✗ 没拿到 $NAME"
  exit 1
fi
GOT="$(shasum -a 256 "$CACHE/$NAME" | cut -d' ' -f1)"
if [ -n "$WANT" ] && [ "$GOT" != "$WANT" ]; then
  echo "✗ 校验不过：$GOT（应为 $WANT）"
  rm -f "$CACHE/$NAME"
  exit 1
fi

rm -rf "$DEST/python"
tar -xzf "$CACHE/$NAME" -C "$DEST"
# 只留跑服务要用的：编译头、链接库、tkinter/tcl、IDLE、测试/文档数据、字节码缓存全删
# （Windows 是 Lib/ 平铺，mac/Linux 是 lib/python3.x/，两套路径都扫）
_P="$DEST/python"
rm -rf "$_P/include" "$_P/libs" "$_P/tcl" "$_P/share" "$_P/Lib/tkinter" "$_P/Lib/idlelib" \
       "$_P/Lib/test" "$_P/Lib/pydoc_data" \
       "$_P"/lib/tcl* "$_P"/lib/tk* "$_P"/lib/itcl* "$_P"/lib/libtcl* "$_P"/lib/libtk* \
       "$_P"/lib/python3.*/tkinter "$_P"/lib/python3.*/idlelib "$_P"/lib/python3.*/test \
       "$_P"/lib/python3.*/pydoc_data "$_P"/lib/python3.*/config-* "$_P"/lib/pkgconfig
find "$_P" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
# mac 的 python3.11 是静态链接的，lib/libpython3.11.dylib 只是给「嵌入解释器」用的
# 17MB 副本，服务用不上（实测 aiortc/aiohttp 等原生扩展都走 dynamic lookup）。
if [ "$1" = "mac-arm64" ] || [ "$1" = "mac-x64" ]; then
  rm -f "$_P"/lib/libpython3*.dylib
fi

# 包和本机同平台时自检一下（别的平台的包只能等到目标机上跑：ELF/exe 在这里执行不了）
_HOST_OS="$(uname -s)"; _HOST_ARCH="$(uname -m)"
_SELF=""
case "$1" in
  mac-arm64) [ "$_HOST_OS" = Darwin ] && [ "$_HOST_ARCH" = arm64 ] && _SELF=1 ;;
  mac-x64)   [ "$_HOST_OS" = Darwin ] && [ "$_HOST_ARCH" = x86_64 ] && _SELF=1 ;;
  linux-x64) [ "$_HOST_OS" = Linux ] && [ "$_HOST_ARCH" = x86_64 ] && _SELF=1 ;;
  linux-arm64) [ "$_HOST_OS" = Linux ] && [ "$_HOST_ARCH" = aarch64 ] && _SELF=1 ;;
esac
if [ -n "$_SELF" ] && [ -x "$DEST/python/bin/python3" ]; then
  V="$(PYTHONDONTWRITEBYTECODE=1 "$DEST/python/bin/python3" -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  [ -n "$V" ] || { echo "✗ 自带 Python 跑不起来"; exit 1; }
  # 自检会顺带生成 __pycache__，再清一遍
  find "$_P" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  echo "  ✓ 自带 Python $V（$(du -sh "$DEST/python" | cut -f1)）"
else
  echo "  ✓ 自带 Python $ST（$(du -sh "$DEST/python" | cut -f1)）"
fi
