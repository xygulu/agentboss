# iOS 走 TestFlight 公开内测

目标：任何人拿到一个链接，就能在自己的 iPhone 上装 CodeBoss，不用登记 UDID、不用第三方签名。

前提：**已付费的 Apple Developer Program 团队**（本仓库团队号 `8J6H7W5M7S`）。
免费个人团队没有 TestFlight，只能给本人已配对的设备签 7 天包。

---

## 一次性：建 App 记录

1. 打开 <https://appstoreconnect.apple.com> → 「我的 App」→「+」→「新建 App」。
2. 平台 **iOS**，名称 `CodeBoss`（App Store 里要唯一，被占就加后缀），
   主要语言「简体中文」，套装 ID 选 `com.example.codeboss.ios`，SKU 随便填 `codeboss-ios`。
3. 建好后记下 App 的 Apple ID（一串数字）——altool 上传要用。
4. 在「用户和访问」里建议建一个 App Store Connect API 密钥（`.p8`，
   Key ID + Issuer ID），上传脚本用得上；密钥只放本机，不进仓库。

## 每次发内测包

1. 改版本号：`project.yml` 里 `CFBundleShortVersionString`（对外版本）和
   `CFBundleVersion`（build 号，**每次上传必须递增**）。
2. 打包：

   ```bash
   cd codeboss/ios
   ./build.sh tf
   # → build/codeboss.ios-tf.ipa
   ```

3. 上传（二选一）：

   ```bash
   xcrun altool --upload-app -f build/codeboss.ios-tf.ipa -t ios \
     --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
   ```

   或 Xcode → Window → Organizer → 选刚才的 archive → Distribute App → App Store Connect。

4. 等 App Store Connect → TestFlight 里构建处理完成（几分钟到半小时，会收到邮件）。
5. 第一次新 App 要先填「测试信息」：测试重点、联系邮箱、是否需要登录（不需要）。
6. **外部测试**（给非团队成员装）需要过一次 **Beta App Review**，通常 1～2 天；
   通过后打开「公开链接」，得到 `https://testflight.apple.com/join/XXXXXXXX`。

## 把链接接到官网（唯一要改的地方）

编辑 `site/ios.html`，只改这一行：

```js
var TF_URL = 'https://testflight.apple.com/join/XXXXXXXX';
```

留空时，页面会显示「内测邀请准备中」，并把按钮导向微信交流群二维码等邀请；
填入后自动变成「在 TestFlight 中打开」，顶部状态变「内测已开放」。
然后发布站点（`bash site/publish.sh`，需单独确认）。

## 内测的硬约束

- 公开链接最多 **10,000** 名测试员；内部测试员（团队成员）最多 100。
- 每个上传的 build **90 天**后过期；过期前重新上传 build 并推到公开链接即可。
  已安装的用户在 TestFlight 里点「更新」覆盖安装，配对和设置保留。
- 公开链接不是永久的：可以随时关闭，关闭后新用户装不上（已装的还能用到 build 过期）。
- 审核是轻量的 Beta 审查，但 App 不能有崩溃、不能要用户装第三方签名描述文件。
- 中国区 App Store 上架另需 **ICP/APP 备案**；TestFlight 内测阶段不受影响。

## 和 Ad Hoc 包的关系

| | `./build.sh ipa` | `./build.sh tf` |
|---|---|---|
| 分发方式 | Ad Hoc，给已登记 UDID 的设备 | TestFlight 公开链接 |
| 设备上限 | 100 台/年，需登记 | 10,000 人，无需登记 |
| 有效期 | 描述文件 1 年 | build 90 天 |
| 用途 | 开发机推给中继、走 App 内「检查更新」 | 对外公开内测 |

两者可以并存：现有 iOS 自动更新通道继续用 ad-hoc 包，不想等 TestFlight 的人也能装；
TestFlight 包是给陌生用户的正式门面。

## 常见问题

- **上传报 ITMS-4238 Redundant Binary Upload**：`CFBundleVersion` 重复了，改大重打。
- **上传报 missing compliance**：Info.plist 里设置 `ITSAppUsesNonExemptEncryption = false`
  （只用了系统 HTTPS，属于豁免）。
- **TestFlight 里点安装没反应**：先确认 TestFlight 已登录 Apple ID，且链接没被关闭/名额没满。
- **装上打不开/闪退**：看 Xcode Organizer 或 App Store Connect 的崩溃日志；内测包同样要求稳定。
