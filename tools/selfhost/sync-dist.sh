#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 把自建中继发布物同步到公开分发仓 github.com/example/codeboss-relay 并推送。
# 用法: bash tools/selfhost/sync-dist.sh [mirror目录]    # 默认 ~/codeboss-relay
# aiboss 主仓保持私有；对外只有这几个文件。relay.py 从仓库源码原样带走（无内联变换）。
# 每次发版（或改了 tools/selfhost/、docs/selfhost-relay.md）后跑一遍。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIRROR="${1:-$HOME/codeboss-relay}"
GHrepo="example/codeboss-relay"

if [ ! -d "$MIRROR/.git" ]; then
  git init -q -b main "$MIRROR"
  git -C "$MIRROR" remote add origin "https://github.com/$GHrepo.git"
fi

SRC="${CB_DIST_SRC:-$ROOT/relay/relay.py}"   # 测试用替身源码；正常不设

# 秘密扫描：relay.py 是「私有仓里的公开文件」——写死一个密钥/内部地址下次 sync 就公开。
# 先扫再拷：命中时镜像里连密钥残影都不留。
# 规则（宁可误报让人看一眼）：阿里云 AK(LTAI)/OpenAI 风格(sk-)/GitHub PAT(ghp_)/私钥块/40 位以上裸 hex。
HITS="$(LC_ALL=C grep -nE 'LTAI[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|BEGIN [A-Z]+ PRIVATE KEY|[0-9a-f]{40,}' \
  "$SRC" | head -5 || true)"
if [ -n "$HITS" ]; then
  echo "✗ 秘密扫描命中，拒推。逐条确认后（泄漏→换密钥；确有必要→改扫描规则并写明理由）："
  echo "$HITS"
  exit 1
fi

cp "$SRC" "$MIRROR/relay.py"

cp "$ROOT/tools/selfhost/install-selfhost.sh" "$MIRROR/"
cp "$ROOT/tools/selfhost/selfhost.env.sample" "$MIRROR/"
# 分发仓里 relay.py 与 compose 同目录，挂载路径相应改短（主仓版挂 ../../relay/）
sed 's|\.\./\.\./relay/relay.py:/app/relay.py:ro|./relay.py:/app/relay.py:ro|' \
  "$ROOT/tools/selfhost/docker-compose.yml" > "$MIRROR/docker-compose.yml"
# README = docs/selfhost-relay.md 改写：路径去前缀 + 顶部注明来源
{
  echo "# CodeBoss 自建中继（私有化部署）"
  echo
  echo "> 本仓库是自建中继的官方分发仓，只含中继单文件与部署脚本；主项目 [aiboss](https://github.com/example/aiboss) 另见。"
  echo
  sed 's|tools/selfhost/||g' "$ROOT/docs/selfhost-relay.md"
} > "$MIRROR/README.md"

git -C "$MIRROR" add -A
git -C "$MIRROR" commit -q -m "sync: $(cd "$ROOT" && git log -1 --format='%h %s')" || true
if [ "${CB_DIST_NOPUSH:-}" = 1 ]; then
  echo "已生成（CB_DIST_NOPUSH=1，不推送）→ $MIRROR"
else
  git -C "$MIRROR" push -q origin main 2>/dev/null || \
    gh repo create "$GHrepo" --public --description "CodeBoss 自建中继（单文件，半小时私有化部署）" --source "$MIRROR" --push
  echo "已同步 → https://github.com/$GHrepo"
fi
