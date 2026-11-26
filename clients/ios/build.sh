#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later

# 构建 iOS 客户端。
#   ./build.sh                 未签名构建（验证能编译）
#   ./build.sh signed          自动签名构建到第一台已配对真机（签进该设备的描述文件）
#   ./build.sh signed <UDID>   指定设备
#   ./build.sh install [UDID]  签名构建并安装到设备
#   ./build.sh ipa             打成 Ad Hoc 分发的 .ipa（1 年有效、登记设备可装），
#                              交给开发机推给中继，走 iOS 的「检查更新」通道
#   ./build.sh tf              打成 App Store/TestFlight 用的 .ipa（公开内测、不限设备），
#                              再按 codeboss/ios/TESTFLIGHT.md 上传到 App Store Connect
#   ./build.sh tf --upload     打包并直接上传（用 Xcode 里已登录的 Apple ID）
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "需要 xcodegen：brew install xcodegen" >&2
  exit 1
fi

MODE="${1:-unsigned}"
UDID="${2:-}"
if [ -z "$UDID" ]; then
  UDID=$(xcrun devicectl list devices 2>/dev/null \
    | grep 'available (paired)' \
    | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}' | head -1 || true)
fi

xcodegen generate

if [ "$MODE" = "unsigned" ]; then
  xcodebuild -project Codeboss.xcodeproj -scheme Codeboss -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath build \
    CODE_SIGNING_ALLOWED=NO build
elif [ "$MODE" = "ipa" ]; then
  # Ad Hoc 分发（付费开发账号）：描述文件 1 年有效，含账号里登记的设备（最多 100 台/年）。
  # 必须走 archive + exportArchive——免费团队的 development 包只认本机已配对设备，
  # 而且 7 天就过期；对外发的中继包要的是 ad-hoc 这一档。
  # 前提：Xcode 里登录的是 Apple Developer Program 团队，且用户手机 UDID 已登记。
  TEAM_ID="${CB_IOS_TEAM:-$(grep -m1 'DEVELOPMENT_TEAM:' project.yml | awk '{print $2}')}"
  ARCH="build/Codeboss.xcarchive"
  rm -rf "$ARCH" build/export
  xcodebuild -project Codeboss.xcodeproj -scheme Codeboss -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath build \
    -archivePath "$ARCH" -allowProvisioningUpdates archive
  cat > build/ExportOptions.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>ad-hoc</string>
  <key>signingStyle</key><string>automatic</string>
  <key>teamID</key><string>${TEAM_ID}</string>
  <key>stripSwiftSymbols</key><true/>
  <key>compileBitcode</key><false/>
  <key>uploadSymbols</key><false/>
</dict></plist>
PLIST
  xcodebuild -exportArchive -archivePath "$ARCH" -exportPath build/export \
    -exportOptionsPlist build/ExportOptions.plist -allowProvisioningUpdates
  rm -f build/codeboss.ios.ipa
  cp build/export/Codeboss.ipa build/codeboss.ios.ipa
  echo "ipa: $(pwd)/build/codeboss.ios.ipa（$(du -h build/codeboss.ios.ipa | cut -f1)）"
  echo "签名类型: $(security cms -D -i build/export/Codeboss.app/embedded.mobileprovision 2>/dev/null \
    | plutil -extract Name raw -o - - 2>/dev/null || echo '?')"
elif [ "$MODE" = "tf" ]; then
  # TestFlight / App Store 分发：同一套付费团队证书，导出 App Store 档的 ipa。
  # 与 ipa（ad-hoc）的区别：不绑设备 UDID、可由苹果公证分发，走 TestFlight 公开链接
  # 最多 1 万人；上传到 App Store Connect 后每个 build 90 天可用。
  # 前提：App Store Connect 里已建好这个 bundle id 的 App 记录（见 TESTFLIGHT.md）。
  TEAM_ID="${CB_IOS_TEAM:-$(grep -m1 'DEVELOPMENT_TEAM:' project.yml | awk '{print $2}')}"
  ARCH="build/Codeboss-tf.xcarchive"
  rm -rf "$ARCH" build/export-tf
  xcodebuild -project Codeboss.xcodeproj -scheme Codeboss -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath build \
    -archivePath "$ARCH" -allowProvisioningUpdates archive
  cat > build/ExportOptions-tf.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>signingStyle</key><string>automatic</string>
  <key>teamID</key><string>${TEAM_ID}</string>
  <key>stripSwiftSymbols</key><true/>
  <key>compileBitcode</key><false/>
  <key>uploadSymbols</key><true/>
</dict></plist>
PLIST
  xcodebuild -exportArchive -archivePath "$ARCH" -exportPath build/export-tf \
    -exportOptionsPlist build/ExportOptions-tf.plist -allowProvisioningUpdates
  rm -f build/codeboss.ios-tf.ipa
  cp build/export-tf/Codeboss.ipa build/codeboss.ios-tf.ipa
  IPA="$(pwd)/build/codeboss.ios-tf.ipa"
  echo "TestFlight ipa: ${IPA}（$(du -h "$IPA" | cut -f1)）"
  # 导出的目录里只有 .ipa，没有 .app——版本号从 ipa 里读
  INFO="$(mktemp -t cb-info).plist"
  unzip -p "$IPA" "Payload/Codeboss.app/Info.plist" >"$INFO" 2>/dev/null || true
  VER=$(plutil -extract CFBundleShortVersionString raw -o - "$INFO" 2>/dev/null || echo '?')
  BLD=$(plutil -extract CFBundleVersion raw -o - "$INFO" 2>/dev/null || echo '?')
  rm -f "$INFO"
  echo "版本: $VER ($BLD)——每次上传 build 号必须比上一次大，改 project.yml 里的 CFBundleVersion 后重跑。"
  echo
  if [ "${2:-}" = "--upload" ] || [ "${CB_TF_UPLOAD:-}" = "1" ]; then
    # 走 Xcode 里已登录的 Apple ID 直接上传（不需要 API 密钥）。
    # 前提：App Store Connect 里已有这个 bundle id 的 App 记录。
    echo "→ 上传到 App Store Connect（用 Xcode 账号）…"
    sed 's#<key>method</key><string>app-store-connect</string>#<key>method</key><string>app-store-connect</string><key>destination</key><string>upload</string>#' \
      build/ExportOptions-tf.plist > build/ExportOptions-upload.plist
    xcodebuild -exportArchive -archivePath "$ARCH" -exportPath build/upload-tf \
      -exportOptionsPlist build/ExportOptions-upload.plist -allowProvisioningUpdates
    echo "✓ 已上传。等几分钟处理完，去 App Store Connect → TestFlight 就能看到 build ${BLD}。"
  else
    echo "下一步（详见 codeboss/ios/TESTFLIGHT.md）："
    echo "  1) 直接上传：./build.sh tf --upload（用 Xcode 里登录的 Apple ID，不需要 API 密钥）"
    echo "  2) 或 Xcode → Window → Organizer → 选这次的 archive → Distribute App"
  fi
  echo "  3) 处理完在 TestFlight 里加自己为内部测试员；外部测试要过一次 Beta App Review"
  echo "  4) 生成公开链接 testflight.apple.com/join/XXXXXXXX，粘进 site/ios.html 的 TF_URL"
elif [ "$MODE" = "signed" ] || [ "$MODE" = "install" ]; then
  if [ -z "$UDID" ]; then
    echo "没找到已配对真机（xcrun devicectl list devices）" >&2
    exit 1
  fi
  # 必须指定具体设备：generic/platform 打出的描述文件不含当前手机，装不进去
  xcodebuild -project Codeboss.xcodeproj -scheme Codeboss -configuration Release \
    -destination "platform=iOS,id=$UDID" -derivedDataPath build \
    -allowProvisioningUpdates build
  APP="build/Build/Products/Release-iphoneos/Codeboss.app"
  if [ "$MODE" = "install" ]; then
    xcrun devicectl device install app --device "$UDID" "$APP"
    echo "首次安装后需在手机「设置 → 通用 → VPN 与设备管理」信任开发者证书。"
  fi
else
  echo "用法: $0 [unsigned|signed|install|ipa|tf] [UDID]" >&2
  exit 2
fi

echo
echo "产物: $(pwd)/build/Build/Products/Release-iphoneos/Codeboss.app"
