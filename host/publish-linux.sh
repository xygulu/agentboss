#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 打 Linux 发行包（自带 x86_64 Python）并上架到中继：/codeboss-linux.tar.gz
# 用法: ./publish-linux.sh [relay_base]      默认 https://relay.example.com
# 令牌：RELAY_ADMIN_TOKEN（优先环境变量，其次 ~/.codeboss/.env）；只在内部使用，不打印。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # host/
ROOT="$(cd "$HERE/.." && pwd)"            # 仓库根
RELAY="${1:-https://relay.example.com}"
RELAY="${RELAY%/}"
VER="$(cat "$HERE/VERSION" 2>/dev/null | tr -d '[:space:]')"
[ -n "$VER" ] || { echo "✗ 缺 VERSION"; exit 1; }

TOK="${RELAY_ADMIN_TOKEN:-}"
if [ -z "$TOK" ] && [ -f "$HOME/.codeboss/.env" ]; then
  TOK="$(grep -E '^[[:space:]]*RELAY_ADMIN_TOKEN=' "$HOME/.codeboss/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
fi
[ -n "$TOK" ] || { echo "✗ 没找到 RELAY_ADMIN_TOKEN"; exit 1; }

NAME="codeboss-$VER"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYVER="${CB_PYC_PY_VER:-3.11}"
CB_PYC_PY_VER="$PYVER" "$HERE/build-pyc.sh" >/dev/null || { echo "✗ 无源码包构建失败"; exit 1; }
cp -R "$HERE/build/pyc/${PYVER//./}" "$TMP/$NAME"

# 自带 Python（linux x86_64）：装完不用机器上先装、也不用联网装解释器
"$ROOT/tools/fetch_python.sh" linux-x64 "$TMP/py" >/dev/null || { echo "✗ 取不到 Linux 的 Python"; exit 1; }
mv "$TMP/py/python" "$TMP/$NAME/python-x64"
rm -rf "$TMP/py"

TAR="$TMP/codeboss-linux.tar.gz"
tar -czf "$TAR" -C "$TMP" "$NAME"

DONE="$(bash "$ROOT/tools/oss_put.sh" "$RELAY" "$TOK" codeboss-linux.tar.gz "$TAR")"
HDR=()
if [ -n "$DONE" ]; then HDR=(-H "X-Oss-Done: 1"); echo "→ 已直传 OSS（中继不再镜像）"; fi

echo "→ 上架 v$VER 到 $RELAY（$(du -h "$TAR" | cut -f1)）…"
RESP="$(curl -sS -m 900 -X POST "$RELAY/r/admin/distpkg?name=codeboss-linux.tar.gz" \
  -H "Authorization: Bearer $TOK" \
  "${HDR[@]+"${HDR[@]}"}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$TAR")"
echo "$RESP"
case "$RESP" in
  *'"ok": true'*|*'"ok":true'*) ;;
  *) echo "✗ 上架失败"; exit 1 ;;
esac
echo "✓ 直链：$RELAY/codeboss-linux.tar.gz"
