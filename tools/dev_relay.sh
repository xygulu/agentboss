#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 本地后台验收夹具：临时 DB + 固定令牌，只给开发/测试用，绝不用于生产
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
D="${CB_ADMIN_DEV_DIR:-/tmp/cbadmin-dev}"
mkdir -p "$D"
exec env RELAY_ADMIN_TOKEN=devtoken "$ROOT/.venv/bin/python3" "$ROOT/relay/relay.py" \
  --host 127.0.0.1 --port "${CB_ADMIN_DEV_PORT:-18891}" --db "$D/relay.db"
