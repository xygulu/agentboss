#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 用本机 Android SDK 直接出 APK（不依赖 Gradle / Android Studio）。
# 单工程双产品：PRODUCT=agentboss（默认）/ codeboss —— 包名、图标、应用名、
# 版本 lineage、签名 key、cb_product 种子全由 products/<线>/product.json 与
# products/<线>/android/res 覆盖出（详见 docs/superpowers/specs/2026-11-18-unified-monorepo-design.md）。
set -e
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export PATH="$JAVA_HOME/bin:$PATH"

# 位置参数和环境变量都认：`./build.sh codeboss` 之前被静默当成 agentboss 出了一遍
PRODUCT="${1:-${PRODUCT:-agentboss}}"
SDK="$HOME/Library/Android/sdk"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
PLATFORM="$SDK/platforms/android-34/android.jar"
ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
OUT="$ROOT/build/$PRODUCT"
APK="$OUT/$PRODUCT-terminal.apk"

PROD_JSON="$REPO/products/$PRODUCT/product.json"
[ -f "$PROD_JSON" ] || { echo "✗ 未知产品：$PRODUCT（products/$PRODUCT/product.json 不存在）"; exit 1; }
eval "$(python3 - "$PROD_JSON" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1]))
print("VCODE=%s" % shlex.quote(str(d["version_code"])))
print("VNAME=%s" % shlex.quote(str(d["version_name"])))
print("KS=%s" % shlex.quote(d["keystore"]))
PY
)"
KEYSTORE="$REPO/$KS"
[ -f "$KEYSTORE" ] || { echo "✗ keystore 不在：$KEYSTORE"; exit 1; }

PKG_OLD="com.agentboss.terminal"
PKG_NEW="com.$PRODUCT.terminal"

[ -x "$JAVA_HOME/bin/javac" ] || { echo "缺 JDK: brew install openjdk@17"; exit 1; }
[ -f "$PLATFORM" ] || { echo "缺 android-34 platform"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "== 产品 $PRODUCT（$PKG_NEW v$VNAME code $VCODE）=="

echo "== aapt2 compile =="
# res 合并：base + products/<线>/android/res 覆盖（整文件替换，同名资源后到为准）
RES="$OUT/res_merged"
rm -rf "$RES"          # 每次从零合并：残留的旧 overlay 会让这条线带上上一条线的资源
cp -R "$ROOT/res" "$RES"
OVERLAY="$REPO/products/$PRODUCT/android/res"
[ -d "$OVERLAY" ] && cp -R "$OVERLAY/." "$RES/"
"$BT/aapt2" compile --dir "$RES" -o "$OUT/res.zip"

# manifest 与 Java 源按产品改写包名（authority 字面量随同一处 sed）；
# 版本号也一并写入——aapt2 link 的 --version-* 旗标压不过 manifest 静态属性
MANIFEST="$OUT/AndroidManifest.xml"
sed -e "s/$PKG_OLD/$PKG_NEW/g" \
    -e "s/android:versionCode=\"[0-9]*\"/android:versionCode=\"$VCODE\"/" \
    -e "s/android:versionName=\"[^\"]*\"/android:versionName=\"$VNAME\"/" \
    "$ROOT/AndroidManifest.xml" > "$MANIFEST"

echo "== assets =="
# 壳里的网页资源（离线页、shim）打包前压缩：仓库保留可读源码，APK 里不带注释和缩进
ASSETS="$OUT/assets"
mkdir -p "$ASSETS"
cp -R "$ROOT/assets/." "$ASSETS/"
for f in offline.html shim.js; do
  [ -f "$ASSETS/$f" ] && python3 "$REPO/tools/strip_web.py" "$ASSETS/$f"
done

echo "== aapt2 link =="
"$BT/aapt2" link -o "$OUT/base.apk" \
  -I "$PLATFORM" \
  --manifest "$MANIFEST" \
  -R "$OUT/res.zip" \
  -A "$ASSETS" \
  --java "$OUT/gen" \
  --auto-add-overlay \
  --version-code "$VCODE" --version-name "$VNAME" \
  --min-sdk-version 24 --target-sdk-version 28

echo "== javac =="
# 源码按产品改写包名（package 声明 / AUTHORITY 字面量一并覆盖）
SRCP="$OUT/src"
cp -R "$ROOT/src" "$SRCP"
find "$SRCP" -name '*.java' -exec sed -i '' "s/$PKG_OLD/$PKG_NEW/g" {} +
LIBS_REL="$(cd "$ROOT" && find libs -name '*.jar' 2>/dev/null | tr '\n' ':')"
LIBS_REL="${LIBS_REL%:}"
# javac/d8 的位置参数不能带空格（仓库可能在「威刚 1T」这种路径）：
# 进仓库根喂相对路径，绝对路径只走已加引号的选项。
REL_OUT="${OUT#$ROOT/}"
if ! ( cd "$ROOT" && javac -source 8 -target 8 -nowarn -bootclasspath "$PLATFORM" \
        -classpath "$LIBS_REL" -d "$REL_OUT/classes" \
        $(find "${SRCP#$ROOT/}" -name '*.java') $(find "$REL_OUT/gen" -name '*.java') ) \
        2>"$OUT/javac.err"; then
  grep -v "bootstrap class path" "$OUT/javac.err" || true
  echo "javac 失败"; exit 1
fi

echo "== 产品线自证 =="
# 每个 App 都必须带上自己的产品线信号（cb_product cookie / 首屏 ?product= / 查更新）。
# 值只认构建产物里的 R.string.cb_product——产品覆盖漏写 strings.xml 就会静默继承
# base（agentboss），两个 App 拿同一条线的能力面。这里直接拦死，不出包。
GOT_LINE="$("$BT/aapt2" dump resources "$OUT/base.apk" 2>/dev/null \
  | grep -A1 'string/cb_product' | sed -n 's/.*"\(.*\)".*/\1/p' | head -1)"
if [ "$GOT_LINE" != "$PRODUCT" ]; then
  echo "✗ 产品线自证不对：APK 里 cb_product='$GOT_LINE'，应为 '$PRODUCT'" >&2
  echo "  → 补 products/$PRODUCT/android/res/values/strings.xml（<string name=\"cb_product\">$PRODUCT</string>）" >&2
  exit 1
fi
echo "cb_product=$GOT_LINE"

echo "== d8 =="
# libs 里的 jar 必须作为位置参数一起 dex，否则真机运行期 NoClassDefFoundError
( cd "$ROOT" && "$BT/d8" --lib "$PLATFORM" --min-api 24 --output "$OUT/dex" \
  $(find "$REL_OUT/classes" -name '*.class') $(find libs -name '*.jar' 2>/dev/null) )

echo "== package =="
cp "$OUT/base.apk" "$OUT/unsigned.apk"
( cd "$OUT" && zip -q -j unsigned.apk dex/classes.dex )

"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

echo "== sign（$PRODUCT：$KS）=="
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:codeboss \
  --key-pass pass:codeboss --out "$APK" "$OUT/aligned.apk"
"$BT/apksigner" verify --print-certs "$APK" | head -5

# 版本信息落盘：host 的 /api/apk_ver 读它给壳做自动更新（版本源=打包后的真值）
BADGE="$("$BT/aapt2" dump badging "$APK" | head -1)"
VCode="$(echo "$BADGE" | grep -o "versionCode='[0-9]*'" | head -1 | grep -o '[0-9]*')"
VName="$(echo "$BADGE" | grep -o "versionName='[^']*'" | head -1 | cut -d"'" -f2)"
printf '{"ver":"%s","code":%s,"built":%d,"bytes":%d}\n' \
  "$VName" "$VCode" "$(date +%s)" "$(wc -c < "$APK" | tr -d ' ')" > "$OUT/apk.json"

echo
echo "APK: $APK  ($(du -h "$APK" | cut -f1))  v$VName(code $VCode)"
