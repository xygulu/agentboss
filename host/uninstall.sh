#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# codeboss 卸载：停服务、删 launchd 配置。默认保留数据（token/会话记录/日志）；
# 加 --purge 连虚拟环境、token、会话记录、日志一起删（zshrc alias 也一并清掉）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.xuxingyu.codeboss"

launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
rm -f ~/Library/LaunchAgents/$LABEL.plist
echo "✓ 服务已停止并移除自启动"

if [ "${1:-}" = "--purge" ]; then
  for mk in codeboss claudecast ptyhost; do   # 新旧命名块都清
    if grep -q "$mk 全彩镜像" ~/.zshrc 2>/dev/null; then
      sed -i '' "/# >>> $mk 全彩镜像/,/# <<< $mk 全彩镜像 <<</ d" ~/.zshrc
    fi
  done
  rm -rf "$HERE/.venv" "$HERE/python" "$HERE/logs" "$HERE/token.txt" "$HERE/sessions.json" \
         "$HERE/config.json" "$HERE/__pycache__"
  echo "✓ 已清除 alias、虚拟环境与全部数据"
else
  echo "• 保留了数据与 zshrc alias；彻底清除请执行：$0 --purge"
fi
