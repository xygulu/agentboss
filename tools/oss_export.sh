#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# OSS 导出：把私有仓快照清洗成可推送的开源仓（全新单 commit 历史）。
# 用法: bash tools/oss_export.sh <目标目录>
# 闸门：秘密扫描 + 出仓扫描全绿才 git init；任一失败删目标目录退出。
# 本脚本绝不 push —— 推送是用户点名批准后的手动动作（计划 Global Constraint）。
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="${1:?用法: oss_export.sh <目标目录>}"
MAN="$SRC/tools/oss_export.manifest.txt"
SED_T="$SRC/tools/oss_export.replace.sed"

cleanup(){ rm -rf "$DST"; }
trap cleanup ERR

rm -rf "$DST"; mkdir -p "$DST"

# 1) manifest 白名单快照
rsync -a --filter="merge $MAN" "$SRC/" "$DST/"
[ -f "$DST/relay/relay.py" ] || { echo "✗ manifest 没带上 relay/relay.py"; exit 1; }

# 2) 受测替换表（仅 UTF-8 文本；二进制/历史编码文件不动）
find "$DST" -type f | grep -vE '\.(png|jpg|jpeg|ico|keystore|db|pyc|apk|ipa|dmg|gz|woff2?|ttf|bin|zip|tar|so|dylib)$' |
while IFS= read -r f; do
  # python3 是闸门 3 的硬依赖，这里借它验 UTF-8，防 sed 撞二进制字节
  python3 -c 'import sys; open(sys.argv[1], encoding="utf-8").read()' "$f" 2>/dev/null || continue
  LC_ALL=en_US.UTF-8 sed -E -f "$SED_T" "$f" > "$f.t" && mv "$f.t" "$f"
done

# 3) 闸门 1：秘密扫描（sync-dist 现行正则 + xoxb/glpat/AKIA/AIza）。
#    白名单（逐条理由）：
#    _SIG_PUB      = server.py 内置 ed25519 验签公钥，公开物
#    oss_export.sh = 扫描器自身的正则字面量
#    libs/README   = 三方库 SHA-256 校验记录
#    install.ps1   = Python 下载包完整性校验值
#    test_core.py  = RFC 8032 ed25519 标准测试向量
HITS="$(LC_ALL=C grep -rEn --binary-files=without-match \
  'LTAI[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|BEGIN [A-Z]+ PRIVATE KEY|[0-9a-f]{40,}|xoxb-|glpat-|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}' \
  "$DST" | grep -v '_SIG_PUB' | grep -v 'oss_export\.sh' | grep -v 'libs/README\.md' | grep -v 'install\.ps1' | grep -v 'test_core\.py' | head -8 || true)"
if [ -n "$HITS" ]; then
  echo "✗ 秘密扫描命中，拒出仓（泄漏→换密钥；确有必要→改规则并写明理由）："; echo "$HITS"; exit 1
fi

# 4) 闸门 2：出仓清单——替换左值必须清零，禁带文件必须不存在
for w in 'relay.example.com' 'download.example.com' 'example.com' 'example' 'uSSCxEwE' '浙ICP备' '/home/dev' '~/.codeboss' 'github.com/example'; do
  # 豁免导出工具自身（左值在表/脚本里作为数据存在）
  H="$(grep -rIlF "$w" "$DST" | grep -v '/oss_export\.' || true)"
  [ -z "$H" ] || { echo "✗ 出仓清单命中 [$w]："; echo "$H"; exit 1; }
done
# 个人手机号形态（spec §139；豁免导出工具自身与测试保留假号 138000000xx/13800138000）
PH="$(grep -rInE '(^|[^0-9])1[3-9][0-9]{9}([^0-9]|$)' "$DST" | grep -v '/oss_export\.' | grep -vE '138000000[0-9]{2}|13800138000' | head -5 || true)"
[ -z "$PH" ] || { echo "✗ 疑似手机号出仓（人工确认后打码或改规则）："; echo "$PH"; exit 1; }
BAD="$(find "$DST" \( -name '*.keystore' -o -name 'relay.db*' -o -name 'plans.json' -o -name '*.seed' \) -print || true)"
[ -z "$BAD" ] || { echo "✗ 禁带文件存在："; echo "$BAD"; exit 1; }

# 5) 全新历史：init + 单 commit
printf '__pycache__/\n*.pyc\n.venv/\nlogs/\n' > "$DST/.gitignore"
git -C "$DST" init -q
git -C "$DST" add -A
git -C "$DST" -c user.name="oss-export" -c user.email="oss-export@users.noreply.github.com" \
  commit -qm "agentboss：手机上的 agent 控制端（host / 自建中继 / App / 工具）"

# 6) 闸门 3：导出仓内核心测试子集（CB_OSS_EXPORT_SKIP_TESTS=1 可跳）
if [ "${CB_OSS_EXPORT_SKIP_TESTS:-}" != "1" ]; then
  (cd "$DST" && "${PYTHON:-python3}" -m pytest -q \
    tests/test_relay_selfhost.py tests/test_relay_single_file.py tests/test_license.py)
fi

N="$(git -C "$DST" log --oneline | wc -l | tr -d ' ')"
echo "✓ 导出完成：${DST}（$N commit，$(find "$DST" -type f -not -path '*/.git/*' | wc -l | tr -d ' ') 文件）"
