#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 把 Mac DMG 发布到中继 —— /bosshost-mac.dmg 免登录直链下载（和 APK 同款），
# Mac 客户端「检查更新」也用它提示新客户端。
# 用法: ./publish-dmg.sh [dmg路径] [relay_base]   默认 repo 根下最新的 bosshost-*-mac.dmg
# 令牌：RELAY_ADMIN_TOKEN（优先环境变量，其次 ~/.codeboss/.env 里的同名行）；
#       令牌只在脚本内部使用，不进命令行参数、不打印。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"      # host/
ROOT="$(cd "$HERE/.." && pwd)"            # 仓库根
RELAY="${2:-https://relay.example.com}"
RELAY="${RELAY%/}"

DMG="${1:-$(ls -t "$HERE"/../bosshost-*-mac.dmg 2>/dev/null | head -1)}"
[ -n "$DMG" ] && [ -f "$DMG" ] || { echo "✗ 没找到 DMG（先跑 mac/build-app.sh）"; exit 1; }

VER="$(basename "$DMG" | sed -E 's/.*bosshost-([0-9][0-9.]*(-[a-z0-9]+)*)-mac\.dmg/\1/')"
[ -n "$VER" ] || { echo "✗ 从文件名认不出版本（要形如 bosshost-1.2.3-mac.dmg）"; exit 1; }

TOK="${RELAY_ADMIN_TOKEN:-}"
if [ -z "$TOK" ] && [ -f "$HOME/.codeboss/.env" ]; then
  TOK="$(grep -E '^[[:space:]]*RELAY_ADMIN_TOKEN=' "$HOME/.codeboss/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
fi
[ -n "$TOK" ] || { echo "✗ 没找到 RELAY_ADMIN_TOKEN（环境变量或 ~/.codeboss/.env）"; exit 1; }

DONE="$(bash "$ROOT/tools/oss_put.sh" "$RELAY" "$TOK" bosshost-mac.dmg "$DMG")"
HDR=()
if [ -n "$DONE" ]; then HDR=(-H "X-Oss-Done: 1"); echo "→ 已直传 OSS（中继不再镜像）"; fi

echo "→ 发布 $(basename "$DMG")（v$VER，$(du -h "$DMG" | cut -f1)）到 $RELAY …"
RESP="$(curl -sS -m 300 -X POST "$RELAY/r/admin/macdmg?ver=$VER" \
  -H "Authorization: Bearer $TOK" \
  "${HDR[@]+"${HDR[@]}"}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$DMG")"
echo "$RESP"
case "$RESP" in
  *'"ok": true'*|*'"ok":true'*) ;;
  *) echo "✗ 发布失败"; exit 1 ;;
esac
echo "→ 中继当前 DMG："
curl -sS -m 20 "$RELAY/api/mac_ver"
echo
