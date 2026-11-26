#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 自建中继一键安装（Ubuntu/Debian，systemd）。用法：
#   sudo bash install-selfhost.sh [--port 8791] [--dir /opt/codeboss-relay]
# 做四件事：建目录拷 relay.py → venv 装 aiohttp → 写 systemd unit（EnvironmentFile）→ enable --now。
# 配置在 /etc/codeboss/selfhost.env（样例见 tools/selfhost/selfhost.env.sample），改完 restart 即生效。
set -euo pipefail

PORT=8791
DIR=/opt/codeboss-relay
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT=$2; shift 2 ;;
    --dir)  DIR=$2;  shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# 两种布局都认：aiboss 主仓（tools/selfhost/ 下，relay 在 ../../relay/）和
# 公开分发仓 codeboss-relay（脚本与 relay.py 同目录平铺）。
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/../../relay/relay.py" ]; then RELAY_SRC="$HERE/../../relay/relay.py"
elif [ -f "$HERE/relay.py" ]; then RELAY_SRC="$HERE/relay.py"
else echo "✗ 找不到 relay.py：主仓在 tools/selfhost/ 下跑，分发仓在仓库根跑" >&2; exit 1; fi

mkdir -p "$DIR" /etc/codeboss
cp "$RELAY_SRC" "$DIR/relay.py"
[ -f /etc/codeboss/selfhost.env ] || cp "$HERE/selfhost.env.sample" /etc/codeboss/selfhost.env

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" -q install aiohttp

cat > /etc/systemd/system/codeboss-relay.service <<UNIT
[Unit]
Description=CodeBoss self-hosted relay
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=/etc/codeboss/selfhost.env
ExecStart=$DIR/venv/bin/python $DIR/relay.py --host 0.0.0.0 --port $PORT --db $DIR/relay.db
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now codeboss-relay
echo "装好了。下一步：编辑 /etc/codeboss/selfhost.env（至少填 RELAY_ADMIN_TOKEN），
然后 systemctl restart codeboss-relay。完整说明见 https://example.com/selfhost.html"
