# Codeboss iOS 客户端

一个全屏 WKWebView 壳：界面是对面的 `static/ext.html`（与控制台网页同一份），
原生只补网页在 iOS 上拿不到或不好用的能力。

## 结构

| 文件 | 作用 |
| --- | --- |
| `Codeboss/AppDelegate.swift` | 纯代码启动，无 Storyboard / Scene Manifest；处理 `codeboss://` 深链 |
| `Codeboss/MainViewController.swift` | WKWebView、离线页与自动重试、网络监听、扫码入口、JS alert/confirm |
| `Codeboss/Bridge.swift` | 页面桥 `window.CB`、PTT 录音、亮度/压暗/护眼浮层、音量、电量、配置 |
| `Codeboss/VolumeKeyInterceptor.swift` | 音量键接管（iOS 没有硬件按键事件，用音量变化近似） |
| `Codeboss/QRScannerController.swift` | AVFoundation 扫码配对 |
| `Codeboss/Util.swift` | 字符串 → JS 字面量转义 |
| `project.yml` | xcodegen 工程定义（iOS 15+，bundle `com.example.codeboss.ios`） |

## 构建

```bash
brew install xcodegen        # 只需一次
./build.sh                   # 未签名构建，验证能否编译
./build.sh signed            # 自动签名构建到第一台已配对真机
./build.sh install           # 签名构建 + 安装到真机（可传 UDID 指定设备）
./build.sh ipa               # 打成 .ipa（Payload/Codeboss.app 的 zip）
```

产物：`build/Build/Products/Release-iphoneos/Codeboss.app`

`generic/platform=iOS` 打出的描述文件不含当前手机，装不进去——所以 `signed/install`
一定要带具体设备（脚本会自动从 `xcrun devicectl list devices` 取第一台）。

免费开发者证书首次安装后，需在手机上
「设置 → 通用 → VPN 与设备管理 → Apple Development: …」点「信任」才能打开。

## 音量键接管（iOS 近似版）

iOS 没有 Android 那种硬件按键事件，音量键的按下/松开拿不到。能观测的只有
`AVAudioSession.outputVolume` 的 KVO 和 `MPVolumeView` 内置滑块的 value：
一次按键 = 音量变一格（1/16），按住不放会连续变格。所以把连续变化合并成一次
「按下」，停顿时（0.7s 静默）判为「松开」，方向看增量正负。

- 音量上键 = 按住说话；音量下键 = 发送（与安卓一致）。
- 接管期间音量会被拉回基准值：用户音量不漂，也不会顶到头导致后续按键测不到。
- 近似带来的误差：按住时第一格有约 0.5s 的系统连发延迟；点按与短按分界
  0.3s（短于它算「取消」，不传音频）。

### 静音底座（必须有，否则音量键完全没反应）

`outputVolume` 的 KVO **只在 App 的音频会话活着时才会报**：空闲时按音量键改的是
「铃声音量」，MediaPlayer 和 AVAudioSession 都不会通知我们，上键按下去什么都不发生
（2026-11-09 用户报的就是这个）。所以 `Bridge` 常放一段 0 音量、8kHz、循环的静音
（`silentWav()`），类别用 `.ambient + mixWithOthers`：会话一直活着，音量键改的是
媒体音量，变化就能观测到；不打断别人的音乐，也没声音。

- 录音要独占会话类别（`.playAndRecord`）：`startRec()` 里先 `stopVolumeKeeper()`，
  `pttUp()` 里再撑起来；起录失败也会放回去，否则音量键跟着哑掉。
- 切后台/来电/被别的 App 抢走会话可能把静音停掉：3 秒一次的 `keeperEnsure()` 兜底，
  加上 `interruptionNotification` / `didBecomeActive` 两个通知。
- 代价：App 前台一直有一个「正在播放（无声）」的音频会话，耗电可忽略。

调试日志：控制台不稳，`Bridge.dbg` 同时写 App 容器里的 `Documents/cbvol.log`，
用 `xcrun devicectl device copy from --device <UDID> --domain-type appDataContainer
--domain-identifier com.example.codeboss.ios --source Documents/cbvol.log` 捞出来看。

## 按住说话的两个坑（都修过）

- **松手不停**：原生起录要几十毫秒（`prepareToRecord`），手指先松开的话页面
  还没收到 `__pttStart`，`down` 还是 false，release 被当成「没在按」直接丢掉 →
  录音停不下来、按钮永远停在「录音中」。现在页面按下时立刻置按下态，
  松手一定发 `pttUp`；原生再兜一层「迟到的松开」（`pttStopPending`）。
- **权限弹窗吃掉手势**：第一次按会弹麦克风授权框，系统把这次手势吞了，等用户
  点完「允许」手指早就松了——所以权限回来时若距按下超过 1.2s 就不再开录。

## 更新

iOS 不许网页直接装包，只能走系统的 itms-services 安装清单：

- 开发机把 `ios/build/codeboss.ios.ipa` 推给中继（跟安卓 APK 同一套 ws 帧），
  中继落盘后 `/api/ios_ver` 免鉴权答版本号。
- 页面「检查更新」查 `/api/ios_ver`，发现新版给「更新 App」→ 打开中继的
  `/ios-install` 安装页 → 里面的 `itms-services://` 链接让系统按 `/ios.plist`
  去装 `/codeboss.ipa`。
- 壳提供 `CB.openExternal(url)` 把链接交给系统（WebView 里点不动它ms-services）。
- 装的是开发签名版，只在已登记的设备上装得上；第一次打开可能要去
  「设置 → 通用 → VPN 与设备管理」信任开发者。


## 页面桥（与 Android 壳同名）

页面同步读取 `window.__cbState`（原生每 3 秒刷新一次，注入于 documentStart），
动作通过 `webkit.messageHandlers.cb.postMessage({m, v})` 交给原生。

- 读：`battery` `netState` `getCfg` `pttState` `getBrightness` `getVolume` `getMaxVolume` `dimCap` `eyeSys` `appVer`
- 写 / 动作：`setCfg` `pttDown` `pttUp` `setBrightness` `setDim` `setVolume` `setKeepOn` `setEye` `scanQr` `openWifiSettings` `openExternal` `refresh` `retry` `log`
- 未提供（因此页面对应卡片自动隐藏）：`apkVer` `checkUpdate` `installPerm` `getRunMode` `setRunMode` `toggleMobile` `openNetPanel` `requestLocation`

按住说话：原生 `AVAudioRecorder` 录 16k 单声道 AAC（m4a）→ base64 分块交给
`__pttAudioChunk` → `__pttGo` 由页面用 WebView 网络栈上传 `/api/ptt`（cookie 自动带上）。

扫码：`CB.scanQr()` → 原生相机 → `window.__cbQr(text)`；无权限回 `window.__cbQrDenied()`。
页面自己解析 `?p=CODE` 并调用 `/r/pair`。

## 限制

- 亮度走 `UIScreen.main.brightness`（改的是系统亮度，会保留到退出 App 之后）。
- 护眼/压暗是高层级浮层窗口，能罩住 App 界面；系统键盘是否被罩取决于 iOS 窗口层级。
- 没有 kiosk / 桌面接管。
- 默认地址：`https://relay.example.com/`（可在深链 `codeboss://open?url=…` 里覆盖）。
