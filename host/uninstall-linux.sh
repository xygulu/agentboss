#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# codeboss 卸载（Linux）：停服务、删 systemd --user 单元。
# 默认保留数据（token/会话记录/日志）；加 --purge 连虚拟环境、token、
# 会话记录、日志一起删（shell alias 也一并清掉）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="${CB_LABEL:-codeboss}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now "$LABEL" 2>/dev/null || true
rm -f "$UNIT_DIR/$LABEL.service"
systemctl --user daemon-reload 2>/dev/null || true
# 命令行客户端的软链：指向本目录才删（别动用户自己的同名文件）；bosshost 与兼容名 codeboss 都清
for _d in /usr/local/bin "$HOME/.local/bin"; do
  for _n in bosshost codeboss; do
    _lk="$_d/$_n"
    if [ -L "$_lk" ] && [ "$(readlink -f "$_lk" 2>/dev/null)" = "$HERE/codeboss-cli" ]; then
      rm -f "$_lk"
    fi
  done
done
echo "✓ 服务已停止并移除自启动"

if [ "${1:-}" = "--purge" ]; then
  for mk in codeboss claudecast ptyhost; do   # 新旧命名块都清
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || continue
      if grep -q "$mk 全彩镜像" "$rc" 2>/dev/null; then
        sed -i "/# >>> $mk 全彩镜像/,/# <<< $mk 全彩镜像 <<< d" "$rc"
      fi
    done
  done
  rm -rf "$HERE/.venv" "$HERE/python" "$HERE/logs" "$HERE/token.txt" "$HERE/sessions.json" \
         "$HERE/config.json" "$HERE/__pycache__"
  echo "✓ 已清除 alias、虚拟环境与全部数据"
else
  echo "• 保留了数据与 shell alias；彻底清除请执行：$0 --purge"
fi
