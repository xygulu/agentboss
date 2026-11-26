// SPDX-License-Identifier: AGPL-3.0-or-later

import UIKit
import WebKit
import AVFoundation
import MediaPlayer
import Network

/// 页面（ext.html）与原生之间的桥。页面只看到一个同步对象 window.CB：
/// 读值一律从 window.__cbState 取（原生每 3s 刷新一次），动作走 postMessage。
/// 与 Android 壳保持同一套接口名，页面不用为 iOS 改一行。
final class Bridge: NSObject, WKScriptMessageHandler {
    weak var host: MainViewController?
    weak var web: WKWebView?
    var onReload: (() -> Void)?
    /// 页面请求切到「网络」页（接口连续失败 / 壳报断网）：由 MainViewController 接管
    var onOffline: (() -> Void)?
    /// 离线页「开发机列表」：回中继根路径（/ 会选在线那台跳 /d，没有就出设备列表）
    var onHome: (() -> Void)?

    private let cfgKey = "cfg_raw"
    private let eyeKey = "eye_on"

    private var cfgRaw = ""
    private var eyeOn = false
    private var dimAlpha: CGFloat = 0
    private var netPath: NWPath?
    private var netGen = 0
    /// 最近一次「不归我们管」时看到的系统亮度：选「自动/跟随系统」时还原它。
    /// 必须**每次回前台都刷新**——iOS 在 App 进后台时会把背光还给系统/感光，
    /// 只记启动那一刻的值，就会让「自动」还原成一个陈旧的暗值
    /// （昨晚调到 20%，早上点自动 → 屏幕还是暗的）。
    private var systemBrightness = UIScreen.main.brightness
    /// 我们是否正在接管背光（手动档）。没接管过就别去写——写了反而把系统当前的亮度顶掉。
    private var brightOverridden = false

    private var eyeWindow: UIWindow?
    private var dimWindow: UIWindow?
    private var volumeView: MPVolumeView?
    private var volKeys: VolumeKeyInterceptor?
    /// 上一次音量键的方向：只有「上」那一串结束时才算松手（结束录音）。
    private var lastVolKey: VolumeKeyInterceptor.Key = .up
    /// 上次「音量下=发送」的时间，用于 400ms 防抖。
    private var lastSendAt = Date.distantPast
    /// 上一次「说完话」（PTT 结束）的时刻：音量回弹会被误判成下键，这段时间内不发送
    private var lastPttUpAt = Date.distantPast
    /// 音量下键长按（0.8s）= 静音开关：持续按住到点就切，松手不再发送。
    private static let volLongSeconds: TimeInterval = 0.8
    private var volHoldTimer: Timer?
    private var volDownActive = false
    private var volDownLong = false
    /// 常放的静音（0 音量循环）：让音频会话一直活着，音量键才接在媒体音量上。
    private var silentPlayer: AVAudioPlayer?
    /// 静音底座上一秒播到哪儿：3 秒一采样，位置不动 = 播放器其实已经停了
    /// （系统中断后 isPlaying 常常还报 true，只看它是查不出来的）。
    private var keeperPos: TimeInterval = -1
    /// 底座什么时候起好的：刚起 4 秒内不做 liveness 判断（位置还没走起来，会被误判成停了）
    private var keeperAt = Date.distantPast
    private var keeperTimer: Timer?
    private var keeperObserved = false
    /// 底座巡检次数（每 3 秒一次）：用来隔几次记一行会话实况。
    private var keeperTicks = 0
    /// 按下音量上键的触感反馈（复用同一个 generator，免得每次现建、延迟更高）
    private let haptic = UIImpactFeedbackGenerator(style: .light)
    /// 接管前的系统音量：切后台/退出时还回去。
    /// 前台必须把音量留在中间段（0.2~0.8）才测得到按键，所以启动时会挪一次——
    /// 不能就这么留在 80%，用户关掉 App 发现手机音量莫名变小。
    private var volOriginal: Float?

    private var recorder: AVAudioRecorder?
    private var recStart: TimeInterval = 0
    private var recURL: URL?
    private var capTimer: Timer?
    /// 「松开」比「开录」先到了（权限弹窗 / 起录慢）：开录后要立刻补停。
    private var pttStopPending = false
    /// 按下的时刻：权限弹窗把手势吃掉时用它判断「手指早就松了」。
    private var pttPressAt = Date.distantPast

    init(host: MainViewController) {
        self.host = host
        super.init()
        UIDevice.current.isBatteryMonitoringEnabled = true
        cfgRaw = UserDefaults.standard.string(forKey: cfgKey) ?? ""
        eyeOn = UserDefaults.standard.bool(forKey: eyeKey)
        NotificationCenter.default.addObserver(
            self, selector: #selector(deviceChanged),
            name: UIDevice.batteryLevelDidChangeNotification, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(deviceChanged),
            name: UIDevice.batteryStateDidChangeNotification, object: nil)
        /* 前后台：告诉页面「用户看不看得见」。
           WKWebView 放进后台时 document.hidden 常常不变、visibilitychange 也不触发，
           页面于是以为还在前台 → 息屏一整夜不停轮询/重连推流 →
           按「有请求且屏幕亮着才算使用」的口径，本不该计费却把时长烧光了
           （实测一天烧掉 900 分钟）。这里把真实状态告诉页面。 */
        NotificationCenter.default.addObserver(
            self, selector: #selector(appBackground),
            name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(appForeground),
            name: UIApplication.willEnterForegroundNotification, object: nil)
    }

    /// 进后台：页面全睡（推流+轮询都歇，省流量省电；不算失败）。
    @objc private func appBackground() {
        Bridge.dbg("app → 后台")
        js("window.__cbVisible&&window.__cbVisible(0)")
    }

    /// 回前台：页面补一轮全量 + 顺带重探直连。
    @objc private func appForeground() {
        Bridge.dbg("app → 前台")
        /* 后台期间 iOS 已经把我们抢的背光还回去了，此刻读到的是用户/感光的真实值。
           在页面回前台重新下发手动亮度之前先记下来，「自动」才有正确的还原目标。 */
        let b = UIScreen.main.brightness
        if abs(b - systemBrightness) > 0.005 { systemBrightness = b }
        brightOverridden = false
        js("window.__cbVisible&&window.__cbVisible(1)")
    }

    /// 心跳（配合页面的心跳兜底：心跳一停 = App 被挂起 = 页面自己判睡）。
    func beat() {
        js("window.__cbAlive&&window.__cbAlive()")
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        capTimer?.invalidate()
    }

    // MARK: - 注入页面的 JS

    /// documentStart 注入：先放状态缓存（含当前配置），再定义 CB。
    var startJS: String {
        "window.__cbState=\(stateJSON());\n" + Bridge.cbShimJS
    }

    /// 每次刷新状态（轻量，不带 CB 定义）。
    /// 用 Object.assign 合并而不是整个替换：页面里的 shim 可能缓存了
    /// window.__cbState 的引用，整体替换会让它永远读到第一次注入的旧值。
    func pushState() {
        web?.evaluateJavaScript("Object.assign(window.__cbState||(window.__cbState={}),\(stateJSON()));",
                                completionHandler: nil)
    }

    /// 网络变化推给页面（页面据此静默拆直连、重开推流）。
    /// ⚠️ gen 只在「实质变了」时才加：NWPathMonitor 会因为路径细节抖动频繁回调
    /// （同一 WiFi 也会连着触发好几次）。以前每个回调都 gen+1 → 页面把刚打通的
    /// P2P DataChannel 当「换网」拆掉重连，实测每 10~60 秒重建一次，通道永远
    /// 活不过一分钟（日志里 direct[ok] 与 direct_info 交替刷屏就是这么来的）。
    func updatePath(_ p: NWPath?) {
        let first = netPath == nil
        let oldKey = netKey(netPath)
        netPath = p
        if !first && netKey(p) != oldKey { netGen += 1 }
        pushState()
        pushNet()
    }

    /// 网络身份：在线与否 + 走的哪个接口。同值 = 同一条网，不算换网。
    private func netKey(_ p: NWPath?) -> String {
        guard let p = p else { return "nil" }
        let on = p.status == .satisfied
        var s = on ? "on" : "off"
        if on {
            if p.usesInterfaceType(.wifi) { s += "|wifi" }
            else if p.usesInterfaceType(.cellular) { s += "|cell" }
            else if p.usesInterfaceType(.wiredEthernet) { s += "|eth" }
            else { s += "|other" }
        }
        return s
    }

    private func pushNet() {
        web?.evaluateJavaScript("window.__cbNetState&&window.__cbNetState(\(netJSON()));",
                                completionHandler: nil)
    }

    private func stateJSON() -> String {
        let s: [String: Any] = [
            "battery": batteryJSON(),
            "net": netJSON(),
            "cfg": cfgJSON(),
            "ptt": recorder != nil ? "rec" : "idle",
            "brightness": String(format: "%.3f", UIScreen.main.brightness),
            "volume": volumeLevel(),
            "maxVolume": 16,
            "eyeSys": (eyeOn && eyeWindow?.isHidden == false) ? "1" : "0",
            "appVer": appVerJSON(),
        ]
        let data = (try? JSONSerialization.data(withJSONObject: s, options: [])) ?? Data("{}".utf8)
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    /// App 自己的版本号（页面「客户端 vX」那行显示；iOS 不能自更新，只报版本）。
    private func appVerJSON() -> String {
        let info = Bundle.main.infoDictionary
        let ver = (info?["CFBundleShortVersionString"] as? String) ?? "0"
        let build = (info?["CFBundleVersion"] as? String) ?? "0"
        return "{\"ver\":\"\(ver)\",\"code\":\(build)}"
    }

    private func batteryJSON() -> String {
        let l = UIDevice.current.batteryLevel
        guard l >= 0 else { return "" }
        let st = UIDevice.current.batteryState
        let ch = st == .charging || st == .full
        return "{\"l\":\(Int((l * 100).rounded())),\"c\":\(ch ? 1 : 0)}"
    }

    private func netJSON() -> String {
        let online = netPath?.status == .satisfied
        let wifi = online && (netPath?.usesInterfaceType(.wifi) ?? false)
        let cell = online && (netPath?.usesInterfaceType(.cellular) ?? false)
        return "{\"online\":\(online),\"wifi\":\(wifi),\"wifiOn\":\(wifi),"
            + "\"mobile\":\(cell),\"mobileOn\":\(cell),\"mobileConn\":\(cell),"
            + "\"ssid\":\"\",\"locPerm\":\"1\",\"gen\":\(netGen)}"
    }

    /// 已知五个键取实值，其余键（bright 等）原样透传——与 Android getCfg 同语义。
    private func cfgJSON() -> String {
        var o: [String: Any] = [:]
        if !cfgRaw.isEmpty,
           let d = cfgRaw.data(using: .utf8),
           let parsed = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
            o = parsed
        }
        if o["mode"] == nil { o["mode"] = "hold" }
        if o["autosend"] == nil { o["autosend"] = false }
        if o["stream"] == nil { o["stream"] = true }
        o["keepon"] = o["keepon"] as? Bool ?? false
        o["eye"] = eyeOn
        let data = (try? JSONSerialization.data(withJSONObject: o, options: [])) ?? Data("{}".utf8)
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    // MARK: - 消息

    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "cb",
              let d = message.body as? [String: Any],
              let m = d["m"] as? String else { return }
        func num(_ k: String) -> Double { (d[k] as? NSNumber)?.doubleValue ?? 0 }
        func bool(_ k: String) -> Bool {
            if let b = d[k] as? Bool { return b }
            return (d[k] as? NSNumber)?.boolValue ?? false
        }
        switch m {
        case "pttDown": pttDown()
        case "pttUp": pttUp()
        case "setCfg": setCfg((d["v"] as? String) ?? "")
        case "log":
            let msg = (d["v"] as? String) ?? ""
            NSLog("js: %@", msg)
            Bridge.dbg("js: " + msg)      // 页面里的诊断也进 cbvol.log，捞得出来
        case "setKeepOn": setKeepOn(bool("v"))
        case "setBrightness": setBrightness(num("v"))
        case "setVolume": setVolume(Int(num("v")))
        case "setDim": setDim(num("v"))
        case "setEye": _ = setEye(bool("v"))
        case "scanQr": host?.presentScanner()
        case "needMedia":
            // 页面要 WebRTC 的本机真地址（Safari 没麦克风权限时会把地址藏成
            // .local，开发机解析不了 → 打洞必挂）。先跟系统要麦克风权限。
            requestMic { ok in
                self.js("window.__cbMedia && window.__cbMedia(\(ok))")
            }
        case "openWifi": openSettings()
        case "openExternal": openExternal((d["v"] as? String) ?? "")
        case "refresh", "retry": onReload?()
        case "offline": onOffline?()
        case "home": onHome?()
        default: break
        }
    }

    private func setCfg(_ json: String) {
        guard !json.isEmpty else { return }
        cfgRaw = json
        UserDefaults.standard.set(json, forKey: cfgKey)
        pushState()
    }

    /// 页面要把某个链接交给系统打开（iOS 更新走 itms-services 安装清单，
    /// 在 WebView 里点不动，得让 Safari/系统接手）。
    private func openExternal(_ raw: String) {
        guard let u = URL(string: raw), let sch = u.scheme?.lowercased(), !sch.isEmpty else { return }
        guard sch == "http" || sch == "https" || sch == "itms-services" else { return }
        UIApplication.shared.open(u, options: [:], completionHandler: nil)
    }

    // MARK: - 屏幕：常亮 / 亮度 / 压暗 / 护眼

    func applyStoredSettings() {
        var o: [String: Any] = [:]
        if !cfgRaw.isEmpty,
           let d = cfgRaw.data(using: .utf8),
           let parsed = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
            o = parsed
        }
        setKeepOn(o["keepon"] as? Bool ?? false)
        if let b = o["bright"] as? NSNumber, (o["brightAuto"] as? Bool) != true {
            applyBright(Double(truncating: b))
        } else {
            /* 自动档：一个字节都别写。启动时写一次就等于把「App 启动那一刻的亮度」
               钉死，之后页面每次回前台再写一遍，用户调的、感光的全被顶回去。 */
            dimAlpha = 0
        }
        applyOverlays()
    }

    func setKeepOn(_ on: Bool) {
        DispatchQueue.main.async {
            UIApplication.shared.isIdleTimerDisabled = on
        }
    }

    func getBrightness() -> String {
        String(format: "%.3f", UIScreen.main.brightness)
    }

    func setBrightness(_ v: Double) {
        guard v > 0 else {                   // 0 = 跟随系统：把背光还给系统
            DispatchQueue.main.async {
                /* 没接管过就不写。写一次 UIScreen.brightness 是全局的，
                   等于把系统当前的亮度（含感光结果）钉死在那一刻。 */
                if self.brightOverridden {
                    UIScreen.main.brightness = self.systemBrightness
                    self.brightOverridden = false
                }
                self.pushState()
            }
            return
        }
        DispatchQueue.main.async {
            UIScreen.main.brightness = CGFloat(min(max(v, 0.02), 1.0))
            self.brightOverridden = true
            self.pushState()
        }
    }

    /// 页面传的滑条值 s（1..100），与页面同一套 s² 映射 + 暗档叠黑罩。
    private func applyBright(_ s: Double) {
        let v = min(max(s, 1), 100)
        brightOverridden = true
        UIScreen.main.brightness = CGFloat(min(max(pow(v / 100, 2), 0.02), 1.0))
        dimAlpha = v < 40 ? CGFloat((40 - v) / 39 * 0.85) : 0
        applyOverlays()
    }

    func setDim(_ o: Double) {
        DispatchQueue.main.async {
            self.dimAlpha = CGFloat(min(max(o, 0), 0.85))
            self.applyOverlays()
        }
    }

    func setEye(_ on: Bool) -> String {
        DispatchQueue.main.async {
            self.eyeOn = on
            UserDefaults.standard.set(on, forKey: self.eyeKey)
            self.applyOverlays()
            self.pushState()
            self.web?.evaluateJavaScript("window.__cbEyeSys&&window.__cbEyeSys();",
                                         completionHandler: nil)
        }
        return "ok"
    }

    /// 全屏浮层：护眼 = 暖色罩（#FF9329 α0.129，与 Android 一致）；压暗 = 黑罩 α≤0.85。
    /// 浮层不接收触摸；压在 App 自己的界面上，能罩住设置页与弹层。
    private func applyOverlays() {
        let scene = host?.view.window?.windowScene
        if eyeOn {
            if eyeWindow == nil {
                eyeWindow = makeOverlay(scene: scene,
                                        color: UIColor(red: 1, green: 0.576, blue: 0.161, alpha: 0.129))
            }
            eyeWindow?.isHidden = false
        } else {
            eyeWindow?.isHidden = true
        }
        if dimAlpha > 0 {
            if dimWindow == nil {
                dimWindow = makeOverlay(scene: scene, color: .black)
            }
            dimWindow?.rootViewController?.view.alpha = dimAlpha
            dimWindow?.isHidden = false
        } else {
            dimWindow?.isHidden = true
        }
        pushState()
    }

    private func makeOverlay(scene: UIWindowScene?, color: UIColor) -> UIWindow {
        let w = UIWindow(frame: UIScreen.main.bounds)
        if let s = scene { w.windowScene = s }
        w.windowLevel = UIWindow.Level(rawValue: 10000001)   // 压过键盘窗口，和 Android 悬浮层一致
        w.isUserInteractionEnabled = false
        w.backgroundColor = .clear
        let vc = UIViewController()
        vc.view.backgroundColor = color
        vc.view.isUserInteractionEnabled = false
        w.rootViewController = vc
        w.isHidden = false
        return w
    }

    // MARK: - 媒体音量（MPVolumeView 隐藏滑条）

    func startVolumeView(in view: UIView) {
        let v = MPVolumeView(frame: CGRect(x: -200, y: -200, width: 120, height: 40))
        v.showsRouteButton = false
        v.alpha = 0.01
        view.addSubview(v)
        volumeView = v
        volOriginal = AVAudioSession.sharedInstance().outputVolume
        let it = VolumeKeyInterceptor(volumeView: v)
        it.onPress = { [weak self] k in self?.volPress(k) }
        it.onRelease = { [weak self] in self?.volRelease() }
        volKeys = it
        startVolumeKeeper()
        /* 被电话/别的 App 抢走、或切后台再回来：会话可能已经没了，静音也就停了。
           **1 秒一巡检**（不是 3 秒）：用户按下音量键到看见反应就那么点时间，
           底座死了要在他再按之前就补上（录音期间让位）。 */
        keeperTimer?.invalidate()
        keeperTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.keeperEnsure()
        }
        if !keeperObserved {
            keeperObserved = true
            NotificationCenter.default.addObserver(
                self, selector: #selector(volKeeperWake),
                name: UIApplication.didBecomeActiveNotification, object: nil)
            NotificationCenter.default.addObserver(
                self, selector: #selector(volGiveBack),
                name: UIApplication.didEnterBackgroundNotification, object: nil)
            /* 中断/音频服务重置时重撑底座；路由变化**只记日志**——
               我们自己 setCategory 也会触发路由变化（reason=3/8），
               0.2.28 里在路由变化上重放底座，结果自己触发自己，一秒内
               重放十几次（实机日志 14:40:30 的风暴），会话被翻来覆去重配。 */
            let nc = NotificationCenter.default
            nc.addObserver(forName: AVAudioSession.interruptionNotification,
                           object: nil, queue: .main) { [weak self] n in
                let t = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) ?? 0
                let began = t == AVAudioSession.InterruptionType.began.rawValue
                Bridge.dbg("CBVOL 中断 \(began ? "开始" : "结束") type=\(t)")
                /* 被抢走的当下底座就作废：播放器还活着、会话其实已经冷了，
                   isPlaying 报 true 也看不出来。不作废的话要等巡检才发现。 */
                if began { self?.stopVolumeKeeper() } else { self?.rearmKeeper(reason: "中断结束") }
            }
            nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                           object: nil, queue: .main) { [weak self] _ in
                Bridge.dbg("CBVOL 音频服务被重置 → 重放底座")
                self?.rearmKeeper(reason: "reset")
            }
            nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                           object: nil, queue: .main) { n in
                let r = (n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) ?? 0
                Bridge.dbg("CBVOL 路由变了 reason=\(r)")
            }
        }
    }

    /// 切后台：音量键交还系统，把启动时挪动过的音量还回去。
    /// 回来时 keeper 会重新夹回中间段（startVolumeKeeper 里 setBase）。
    @objc private func volGiveBack(_ n: Notification) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, self.recorder == nil, let orig = self.volOriginal else { return }
            /* 别还到 0.2 以下：音量键只认中间段（0.2～0.8），还到最低档的话
               下次回前台系统音量被按上键顶回来，还得重新夹一次，用户听着像
               音量自己乱跳。宁可还回中间段。 */
            let back = max(orig, 0.2)
            self.volKeys?.restoreTo(back)
            Bridge.dbg("CBVOL give back lvl=\(Int((back * 16).rounded()))")
            self.volOriginal = nil
        }
    }

    /// 切回前台：会话多半在后台被系统收了，静音底座要重新撑起来；
    /// 页面的音频会话可能也压在我们头上，晚一拍把路由抢回来（见 reclaimAudio）。
    @objc private func volKeeperWake(_ n: Notification) {
        DispatchQueue.main.async { [weak self] in self?.keeperEnsure() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.reclaimAudio() }
    }

    /// 底座巡检（1 秒一次）：会话冷了就补上，别等用户按键才发现。
    private func keeperEnsure() {
        dispatchPrecondition(condition: .onQueue(.main))
        if reclaiming { return }                    // 正在抢路由：别插手
        guard recorder == nil else { return }
        /* 后台激活不了会话（561015905），每秒试一次只会刷屏——
           回前台时 didBecomeActive 会重新撑。 */
        guard UIApplication.shared.applicationState == .active else { return }
        let ses = AVAudioSession.sharedInstance()
        /* iOS 没有「类别变了」的通知。换页时 WebKit、系统抢路由都会把类别改成
           别的（playAndRecord 之类），这时音量键落在媒体音量上就接不回来——
           按下去改的是铃声，outputVolume 一个事件都不发。只能自己盯着类别。 */
        let cat = ses.category
        if cat != .playback, cat != .ambient {
            Bridge.dbg("CBVOL 类别被改 cat=\(cat.rawValue) → 重放底座")
            rearmKeeper(reason: "cat")
            return
        }
        guard let p = silentPlayer else { startVolumeKeeper(); return }
        /* 系统会悄悄把会话停掉（切后台、来电、别的 App 抢音频路由、**页面重载**），
           而播放器对象照旧报 isPlaying=true。这时候音量键改的是**铃声**音量，
           outputVolume 一个事件都不发——上键就「彻底没反应」，下键也一样。
           （2026-11-10 实机日志：keeper on 之后 4 分钟里一条 lvl= 都没有，
           用屏幕上的麦克风按钮录一次才恢复；2026-11-12 同一现象：刷新页面后
           第一次按音量上键只是调音量，按一下屏幕录音键才恢复。）
           isPlaying 靠不住，看播放位置：1 秒里位置没往前走 = 真停了，整个换一个。
           8 秒的静音循环，位置 0→1→…→7→0，跨过 8 就折回来算，免得把循环当成停了。 */
        let pos = p.currentTime
        var moved = (pos - keeperPos).truncatingRemainder(dividingBy: 8)
        if moved < 0 { moved += 8 }
        let alive = p.isPlaying && (moved > 0.3 || Date().timeIntervalSince(keeperAt) < 3)
        keeperPos = pos
        if !alive {
            Bridge.dbg("CBVOL keeper 已停 pos=\(String(format: "%.2f", pos)) playing=\(p.isPlaying) → 重起")
            stopVolumeKeeper()
            startVolumeKeeper()
            return
        }
        keeperTicks += 1
        if keeperTicks % 10 == 0 {
            /* 定时把会话的实况记下来：按键收不到时，这一行能看出是
               「会话冷了」（别的 App 在放声音 / 位置停住）还是「按键真没来」。 */
            Bridge.dbg("CBVOL 会话 cat=\(ses.category.rawValue) playing=\(p.isPlaying)"
                       + " pos=\(String(format: "%.2f", pos))"
                       + " vol=\(String(format: "%.2f", ses.outputVolume))"
                       + " other=\(ses.isOtherAudioPlaying)")
        }
    }

    /// 整个重放静音底座：会话冷了、类别被改过、页面重载之后都用这个。
    /// 不靠「播放器位置还在走」去猜——2026-11-12 实机日志里位置照走、会话已经
    /// 冷掉的情况真出现过（keeper on 之后 60 秒里一条 lvl= 都没有，
    /// 音量键全改到铃声上去了）。
    func rearmKeeper(reason: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, self.recorder == nil else { return }
            self.stopVolumeKeeper()
            self.startVolumeKeeper(rebase: false)
            Bridge.dbg("CBVOL 重放底座(\(reason))")
        }
    }

    /// 页面（重新）加载完：WebKit 换页会把音频会话弄掉，而且页面自己的
    /// getUserMedia 探洞（直连拿真实 ICE 候选用）会激活 WebContent 进程的
    /// 音频会话、把我们打断——之后音量键落在它那边，我们的 outputVolume
    /// KVO 一条都不来（2026-11-12 实机：底座明明在放，按键照样只调音量）。
    /// 用户按一下屏幕录音键就恢复（原生录音把路由抢回本进程）——
    /// 这里把这一下自动化（见 reclaimAudio）。
    func keeperWake() {
        rearmKeeper(reason: "page")
        /* 探洞在 didFinish 后一两秒内跑；晚一点抢一次，再兜一次。 */
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.reclaimAudio() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self] in self?.reclaimAudio() }
    }

    /// 正在抢路由（reclaimAudio 进行中）
    private var reclaiming = false

    /// 静默录 ~300ms 就丢：把音频路由从 WebKit（页面音频/getUserMedia）手里
    /// 抢回本进程，音量键重新落到我们的会话上。这就是「按一下屏幕录音键
    /// 就恢复」的自动化版——实机两次验证过这是唯一有效的恢复手段。
    /// 副作用：状态栏的橙色麦克风点会闪一下。
    func reclaimAudio() {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            guard self.recorder == nil, !self.reclaiming else { return }
            guard UIApplication.shared.applicationState == .active else { return }
            guard AVAudioSession.sharedInstance().recordPermission == .granted else { return }
            self.reclaiming = true
            /* 换类别/激活路由时系统会把媒体音量挪一下（实机日志 0.30→0.25），
               这一下不是按键：挡掉，别让它变成「没有可发送的内容」。 */
            self.volKeys?.suppress(1.0)
            self.stopVolumeKeeper()
            let ses = AVAudioSession.sharedInstance()
            do {
                try ses.setCategory(.playAndRecord, mode: .default,
                                    options: [.mixWithOthers, .defaultToSpeaker, .allowBluetooth])
                try ses.setActive(true)
                let url = FileManager.default.temporaryDirectory.appendingPathComponent("reclaim.m4a")
                try? FileManager.default.removeItem(at: url)
                let r = try AVAudioRecorder(url: url, settings: [
                    AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                    AVSampleRateKey: 16000.0,
                    AVNumberOfChannelsKey: 1,
                    AVEncoderBitRateKey: 32000,
                ])
                guard r.prepareToRecord(), r.record() else {
                    self.reclaiming = false
                    self.startVolumeKeeper()
                    return
                }
                Bridge.dbg("CBVOL 抢回音频路由（静默录 300ms）")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                    r.stop()
                    try? FileManager.default.removeItem(at: url)
                    self?.reclaiming = false
                    self?.startVolumeKeeper()   // 底座回到位（playback 档）
                }
            } catch {
                Bridge.dbg("CBVOL 抢回失败 \(error)")
                self.reclaiming = false
                self.startVolumeKeeper()
            }
        }
    }

    // MARK: - 音量键的底座：常放一段静音

    /// iOS 没有 Android 那种硬件按键事件；能观测到的只有音量数值的变化，
    /// 而**只有音频会话活着（有音频在放）时，音量键改的才是媒体音量**——
    /// 空闲时按音量键改的是铃声音量，outputVolume / MPVolumeView 一个都不报，
    /// 音量上键于是完全没反应。这里常放一段 0 音量的循环静音，把会话撑住：
    /// 按键变化就能被 VolumeKeyInterceptor 看到，「上=按住说」才立得住。
    /// `.ambient` + mixWithOthers：不打断别人的音乐，也不受静音开关影响（本来就没声）。
    private static func silentWav() -> Data {
        /* 8 秒而不是 1 秒：liveness 靠 3 秒采一次 currentTime 判断，1 秒循环会
           每次都采到同一个相位（3 秒 ≈ 3 个整圈），停了也看不出来。 */
        let rate = 8000, n = rate * 8          // 8 秒 8kHz 单声道 16bit
        var d = Data("RIFF".utf8)
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        u32(UInt32(36 + n * 2)); d.append(Data("WAVEfmt ".utf8))
        u32(16); u16(1); u16(1)            // PCM，单声道
        u32(UInt32(rate)); u32(UInt32(rate * 2)); u16(2); u16(16)
        d.append(Data("data".utf8)); u32(UInt32(n * 2)); d.append(Data(count: n * 2))
        return d
    }

    private func startVolumeKeeper(rebase: Bool = true) {
        dispatchPrecondition(condition: .onQueue(.main))
        guard recorder == nil else { return }
        let ses = AVAudioSession.sharedInstance()
        /* 切类别/激活路由时系统会把媒体音量跳回自己的值（实机日志里 0.200→0.250），
           那一跳会被拦成「按了一下音量上键」，于是自己开始录音、录完再来一跳——
           死循环。开头几百毫秒的读数不算按键。 */
        volKeys?.suppress(0.35)
        /* 底座用 **playback**，不用 playAndRecord（0.2.16 起是后者，为了按下录音快
           300ms）。playAndRecord 是「录音档」：会占着音频路由，页面自己的声音
           （提醒音是 WebAudio，语音播报是 <audio>）由 WebKit 重新配置会话时才放得
           出来，配不上就整段没声——2026-11-10 实机反馈「提醒音和语音播报都没有声音」，
           而 playAndRecord 期间音量键又照常能被看到，正好对上。
           录音时临时切回 playAndRecord（见 pttDown），录完再切回来。
           mixWithOthers：不打断别人正在放的音乐。 */
        var ok = false
        do {
            /* 类别已经是自己的就别再切：切一次要把会话停掉重配，系统顺手把媒体
               音量弹回它的值（实机日志 0.05→0.20），用户听着像音量自己跳了。 */
            if ses.category != .playback {
                try ses.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            }
            try ses.setActive(true)
            ok = true
        } catch {
            Bridge.dbg("CBVOL keeper playback 失败 \(error)")
        }
        if !ok {
            do {
                if ses.category != .ambient {
                    try ses.setCategory(.ambient, mode: .default, options: [.mixWithOthers])
                }
                try ses.setActive(true)
            } catch {
                Bridge.dbg("CBVOL keeper 失败 \(error)")
                return
            }
        }
        if let p = silentPlayer {
            /* 底座还在放就别重建：会话是热的，按下音量键立刻就收得到。 */
            keeperPos = p.currentTime
            keeperAt = Date()
            if rebase { volKeys?.setBase(level: ses.outputVolume) }
            return
        }
        do {
            let p = try AVAudioPlayer(data: Self.silentWav())
            p.numberOfLoops = -1
            p.volume = 0
            guard p.prepareToPlay(), p.play() else {
                Bridge.dbg("CBVOL keeper 静音没放起来")
                return
            }
            silentPlayer = p
            keeperPos = p.currentTime
            keeperAt = Date()
            if volOriginal == nil { volOriginal = ses.outputVolume }  // 回前台后重新记
            /* 基准 = 当前读数（别当成按键）。重放底座时不动基准：
               基准已经是对的，动了反而把用户的音量拽走（每次刷新都跳一下很烦）。 */
            if rebase { volKeys?.setBase(level: ses.outputVolume) }
            /* 出声问题时一行看全：类别、路由、音量。页面那边放没放得出声音，
               配合「有没有 route 变化」就能分开是路由还是页面根本没播。 */
            Bridge.dbg("CBVOL keeper on lvl=\(volumeLevel()) cat=\(ses.category.rawValue)"
                       + " route=" + (ses.currentRoute.outputs.first?.portType.rawValue ?? "-")
                       + " vol=\(String(format: "%.2f", ses.outputVolume))")
        } catch {
            Bridge.dbg("CBVOL keeper 播放器失败 \(error)")
        }
    }

    /// 录音要独占音频会话（类别切 playAndRecord）：先让位，录完再回来。
    private func stopVolumeKeeper() {
        guard silentPlayer != nil else { return }
        silentPlayer?.stop()
        silentPlayer = nil
        keeperPos = -1
        keeperAt = Date.distantPast
        Bridge.dbg("CBVOL keeper off")
    }

    private func volumeLevel() -> Int {
        let s = Double(AVAudioSession.sharedInstance().outputVolume)
        return Int((s * 16).rounded())
    }

    func setVolume(_ v: Int) {
        DispatchQueue.main.async {
            let level = Float(min(max(v, 0), 16)) / 16
            guard let slider = self.findSlider(self.volumeView) else { return }
            self.volKeys?.setBase(level: level)   // 自己改的，别当成按键
            slider.value = level
            slider.sendActions(for: .touchUpInside)
            self.pushState()
        }
    }

    // MARK: - 音量键：上=说话，下=发送（与 Android 壳同语义）

    /// 页面设置里的说话模式（hold=按住说，toggle=点按说）。
    private func mode() -> String {
        guard !cfgRaw.isEmpty,
              let d = cfgRaw.data(using: .utf8),
              let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
              let m = o["mode"] as? String else { return "hold" }
        return m
    }

    /// 按下的即时反馈：震一下 + 页面先亮「录音中」。
    /// 原生这边切类别、建录音器要几十到几百毫秒（蓝牙还要更久），等它起好了
    /// 页面才亮，手指按住会先愣一下——用户反馈「按下去有点慢」就是这个。
    /// 起录失败会走 __pttError / __pttCancel 把界面收回去，提前亮不会有残留。
    private func pttFlagPress() {
        haptic.prepare()
        haptic.impactOccurred()
        js("window.__pttStart && window.__pttStart()")
    }

    private func volPress(_ key: VolumeKeyInterceptor.Key) {
        dispatchPrecondition(condition: .onQueue(.main))
        lastVolKey = key
        Bridge.dbg("CBVOL key=\(key == .up ? "up" : "down") mode=\(mode()) rec=\(recorder != nil)")
        switch key {
        case .up:
            if mode() == "toggle" {
                if recorder != nil { pttUp() } else { pttFlagPress(); pttDown() }
            } else if recorder == nil {
                pttFlagPress()
                pttDown()
            }
        case .down:
            /* 短按发送、长按 0.8 秒静音——和安卓壳同一套语义。
               按下只起计时：iOS 拿不到「真正松手」，松手是这一串音量变化停 0.7 秒后
               由 volRelease 判的，所以发送也挪到那时（见 volRelease）。 */
            if recorder != nil { return }                    // 录音中不抢
            volDownActive = true
            volDownLong = false
            volHoldTimer?.invalidate()
            volHoldTimer = Timer.scheduledTimer(withTimeInterval: Bridge.volLongSeconds,
                                                repeats: false) { [weak self] _ in
                guard let self = self, self.volDownActive else { return }
                self.volHoldTimer = nil
                self.volDownLong = true
                self.js("window.__cbMuteToggle && window.__cbMuteToggle()")
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                Bridge.dbg("CBVOL long(down) -> mute toggle")
            }
        }
    }

    /// 一串音量变化停了 = 松手：按住模式下停录音；音量下键则按短按/长按分流。
    private func volRelease() {
        if lastVolKey == .down {
            let active = volDownActive
            let wasLong = volDownLong
            volDownActive = false
            volDownLong = false
            volHoldTimer?.invalidate()
            volHoldTimer = nil
            if !active { return }          // 按下的那一下没归我们管（录音中），别补发
            if wasLong {
                Bridge.dbg("CBVOL release(down) long=1 已切静音，不发送")
                return
            }
            if recorder != nil { return }                            // 录音中不抢
            if Date().timeIntervalSince(lastSendAt) < 0.4 { return } // 连按防抖
            /* 刚说完话 1.5 秒内不发送：松手时音量要拉回基准，这个回弹偶尔被
               识别成一次「音量下键」，转写还没回来就调 __cbSend(true)，
               用户看到的是莫名其妙的「没有可发送的内容」。 */
            if Date().timeIntervalSince(lastPttUpAt) < 1.5 {
                Bridge.dbg("CBVOL release(down) 刚结束录音，忽略这次下键")
                return
            }
            lastSendAt = Date()
            Bridge.dbg("CBVOL release(down) -> send")
            js("window.__cbSend && window.__cbSend(true)")
            return
        }
        guard mode() != "toggle", recorder != nil else { return }
        // 只是点了一下（没有连续变格）→ 别把这段静音送去识别。
        // 用「变了几格」判断，不用持续时间：切录音类别的抑制窗会把中间的格吞掉，
        // 按时间算出来是 0，按住说话会被当成点按取消（0.2.13 实锤）。
        let tap = volKeys?.tapOnly ?? true
        Bridge.dbg("CBVOL release steps=\(volKeys?.burstSteps ?? -1) tap=\(tap)")
        pttUp(cancel: tap)
    }

    private func findSlider(_ view: UIView?) -> UISlider? {
        guard let v = view else { return nil }
        if let s = v as? UISlider { return s }
        for sub in v.subviews {
            if let s = findSlider(sub) { return s }
        }
        return nil
    }

    func openSettings() {
        DispatchQueue.main.async {
            if let u = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(u, options: [:], completionHandler: nil)
            }
        }
    }

    @objc private func deviceChanged() {
        pushState()
    }

    // MARK: - 按住说话（AVAudioRecorder → m4a → base64 分块交给页面上传）

    private func requestMic(_ done: @escaping (Bool) -> Void) {
        if #available(iOS 17.0, *) {
            AVAudioApplication.requestRecordPermission { ok in
                DispatchQueue.main.async { done(ok) }
            }
        } else {
            AVAudioSession.sharedInstance().requestRecordPermission { ok in
                DispatchQueue.main.async { done(ok) }
            }
        }
    }

    private func pttDown() {
        dispatchPrecondition(condition: .onQueue(.main))
        Bridge.dbg("CBVOL pttDown rec=\(recorder != nil) pending=\(pttStopPending)")
        pttStopPending = false                      // 新的一次按下：清掉上一次的「迟到的松开」
        pttPressAt = Date()
        if recorder != nil {
            js("window.__pttStart && window.__pttStart()")
            return
        }
        let status = AVAudioSession.sharedInstance().recordPermission
        if status == .granted { startRec() }
        else if status == .denied {
            js("window.__pttError && window.__pttError('App 没有麦克风权限')")
        } else {
            requestMic { [weak self] ok in
                guard let self = self else { return }
                guard ok else {
                    self.js("window.__pttError && window.__pttError('App 没有麦克风权限')")
                    return
                }
                // 首次弹麦克风权限框时，系统把这次手势吃了（touchend 来不了）。
                // 等用户点完「允许」，手指早就松了——别再开录，否则按钮会永远
                // 停在「录音中」，还占着麦克风。
                if Date().timeIntervalSince(self.pttPressAt) > 1.2 {
                    Bridge.dbg(String(format: "CBVOL perm ok but press 已过 %.1fs → 不开录",
                                      Date().timeIntervalSince(self.pttPressAt)))
                    self.js("window.__pttCancel && window.__pttCancel()")
                    return
                }
                self.startRec()
            }
        }
    }

    private func startRec() {
        let t0 = Date()
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("ptt.m4a")
            try? FileManager.default.removeItem(at: url)
            let set: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 16000.0,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 32000,
                AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
            ]
            let ses = AVAudioSession.sharedInstance()
            // 常放的那段静音要让开：录音要独占类别（playAndRecord），
            // 录完在 pttUp 里重新撑起来（不撑的话音量键当场就失灵）。
            // 会话是热的（底座刚在放/刚抢回过路由），切类别只要 ~200ms。
            stopVolumeKeeper()
            if ses.category != .playAndRecord {
                try ses.setCategory(.playAndRecord, mode: .default,
                                    options: [.mixWithOthers, .defaultToSpeaker, .allowBluetooth])
                Bridge.dbg("CBVOL rec 切类别 \(Bridge.ms(since: t0))ms")
            }
            try ses.setActive(true)      // 已激活时是空操作，很快
            Bridge.dbg("CBVOL rec 激活 \(Bridge.ms(since: t0))ms cat=\(ses.category.rawValue)")
            // 切了音频类别（铃声档↔媒体档），系统音量会跳一下：吸收掉，
            // 但只闷 0.25s——闷久了会把「按住不放」的连续变格也吃掉。
            volKeys?.suppress(0.25)
            volKeys?.setBase(level: AVAudioSession.sharedInstance().outputVolume)
            let r = try AVAudioRecorder(url: url, settings: set)
            guard r.prepareToRecord(), r.record() else {
                js("window.__pttError && window.__pttError('录音器启动失败')")
                startVolumeKeeper()
                return
            }
            recorder = r
            recURL = url
            recStart = Date().timeIntervalSince1970
            Bridge.dbg("CBVOL rec start total=\(Bridge.ms(since: t0))ms")
            js("window.__pttStart && window.__pttStart()")
            capTimer?.invalidate()
            capTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
                self?.pttUp()
            }
            pushState()
            // 手指在录音器起好之前就松开了：这一段不要，开录即停
            if pttStopPending {
                pttStopPending = false
                Bridge.dbg("CBVOL rec start but stop pending → 立刻停")
                pttUp(cancel: true)
            }
        } catch {
            NSLog("codeboss rec: %@", String(describing: error))
            js("window.__pttError && window.__pttError('录音失败')")
            startVolumeKeeper()      // 起录失败：把静音底座放回去，别让音量键跟着哑掉
        }
    }

    /// 页面被换掉/重载时调用：正在录就停掉。
    /// 页面一换，手指底下的按钮就从 DOM 里没了，WebKit 的触摸序列断了 →
    /// touchend 送不回来 → 录音会一直挂着、麦克风一直被占（iOS 实锤：
    /// 日志里只有 pttDown 没有 pttUp）。
    func stopRecordingIfAny() {
        dispatchPrecondition(condition: .onQueue(.main))
        guard recorder != nil else { return }
        Bridge.dbg("CBVOL 页面切换 → 停掉录音")
        pttUp(cancel: true)
    }

    /// 正在录音（网络页的自动切换要避开这一小会儿：话说到一半被切走，这条语音就废了）
    var recordingNow: Bool { recorder != nil }

    private func pttUp(cancel: Bool = false) {
        dispatchPrecondition(condition: .onQueue(.main))
        Bridge.dbg("CBVOL pttUp rec=\(recorder != nil) cancel=\(cancel)")
        lastPttUpAt = Date()
        guard let r = recorder else {
            // 还没开录就松手了（起录要几十毫秒，权限弹窗更久）：记下来，
            // 等真的开录时立刻停掉，不然会卡在「录音中」停不下来。
            pttStopPending = true
            js("window.__pttCancel && window.__pttCancel()")
            return
        }
        capTimer?.invalidate()
        capTimer = nil
        let ms = Int(((Date().timeIntervalSince1970 - recStart) * 1000).rounded())
        Bridge.dbg("CBVOL rec stop ms=\(ms) cancel=\(cancel)")
        r.stop()
        recorder = nil
        /* 不 setActive(false)：底座的静音还在放，会话要一直活着，音量键才有事件。
           类别由 startVolumeKeeper 切回 playback（录音档占着路由会让页面没声音）。 */
        // 会话状态变了，系统音量可能跳一下：吸收掉，别当成用户按键
        volKeys?.suppress(0.25)
        startVolumeKeeper()      // 静音底座回到位（里面会按当前读数重设基准）
        pushState()
        js("window.__pttDone && window.__pttDone(\(ms))")
        guard !cancel, ms >= 300, let url = recURL else {
            js("window.__pttCancel && window.__pttCancel()")
            if let u = recURL { try? FileManager.default.removeItem(at: u) }
            recURL = nil
            return
        }
        recURL = nil
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let data = (try? Data(contentsOf: url)) ?? Data()
            try? FileManager.default.removeItem(at: url)
            guard !data.isEmpty else {
                DispatchQueue.main.async {
                    self.js("window.__pttError && window.__pttError('没收到音频')")
                }
                return
            }
            let b64 = data.base64EncodedString()
            let chunk = 65536
            let total = (b64.count + chunk - 1) / chunk
            let chars = Array(b64)
            for i in 0..<total {
                let a = i * chunk
                let b = min(a + chunk, chars.count)
                let part = String(chars[a..<b])
                DispatchQueue.main.async {
                    self.js("window.__pttAudioChunk&&window.__pttAudioChunk(\(i),\(total),\"\(part)\")")
                }
            }
            DispatchQueue.main.async {
                self.js("window.__pttGo&&window.__pttGo(\(ms))")
            }
        }
    }

    private func js(_ code: String) {
        web?.evaluateJavaScript(code, completionHandler: nil)
    }

    /// 毫秒计时（调试用：掐一下「按下 → 真开录」各段的耗时）。
    static func ms(since t: Date) -> Int {
        Int((Date().timeIntervalSince(t) * 1000).rounded())
    }

    /// 调试日志：控制台 + App 容器里的 cbvol.log（控制台挂载不稳，用文件兜底，
    /// 方便用 `devicectl device copy from` 捞出来看）。
    static func dbg(_ s: String) {
        print(s)
        guard let dir = FileManager.default.urls(for: .documentDirectory,
                                                in: .userDomainMask).first else { return }
        let url = dir.appendingPathComponent("cbvol.log")
        let fm = FileManager.default
        if let a = try? fm.attributesOfItem(atPath: url.path),
           let n = a[.size] as? Int, n > 200_000 {
            try? fm.removeItem(at: url)          // 别让它无限长
        }
        if !fm.fileExists(atPath: url.path) {
            fm.createFile(atPath: url.path, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        defer { try? h.close() }
        h.seekToEndOfFile()
        let line = String(format: "%.3f %@\n", Date().timeIntervalSince1970, s)
        h.write(Data(line.utf8))
    }

    // MARK: - 静态 JS

    static let cbShimJS = """
    (function(){
      if (window.CB && window.CB.__ios) return;
      window.__cbState = window.__cbState || {};
      function st(k, d){ var S = window.__cbState || {}; var v = S[k]; return (v === undefined || v === null) ? d : v; }
      function post(o){ try { webkit.messageHandlers.cb.postMessage(o); } catch (e) {} }
      window.CB = {
        __ios: 1,
        battery: function(){ return String(st('battery','')); },
        netState: function(){ return String(st('net','')); },
        getCfg: function(){ return String(st('cfg','')); },
        setCfg: function(j){ post({m:'setCfg', v:String(j)}); try{ (window.__cbState=window.__cbState||{}).cfg=String(j); }catch(e){} },
        log: function(m){ post({m:'log', v:String(m)}); },
        pttDown: function(){ post({m:'pttDown'}); },
        pttUp: function(){ post({m:'pttUp'}); },
        pttState: function(){ return String(st('ptt','idle')); },
        getBrightness: function(){ return String(st('brightness','1')); },
        setBrightness: function(v){ post({m:'setBrightness', v:Number(v)||0}); },
        getVolume: function(){ return Number(st('volume', 8)); },
        getMaxVolume: function(){ return Number(st('maxVolume', 16)); },
        setVolume: function(v){ post({m:'setVolume', v:Number(v)||0}); },
        setKeepOn: function(on){ post({m:'setKeepOn', v:!!on}); },
        setDim: function(o){ post({m:'setDim', v:Number(o)||0}); },
        dimCap: function(){ return '1'; },
        setEye: function(on){ post({m:'setEye', v:!!on}); return 'ok'; },
        eyeSys: function(){ return String(st('eyeSys','0')); },
        scanQr: function(){ post({m:'scanQr'}); },
        openExternal: function(u){ post({m:'openExternal', v:String(u||'')}); },
        openWifiSettings: function(){ post({m:'openWifi'}); },
        refresh: function(){ post({m:'refresh'}); },
        retry: function(){ post({m:'retry'}); },
        offline: function(){ post({m:'offline'}); },
        home: function(){ post({m:'home'}); },
        native_: function(){ return '3'; }
      };
      /* App 版本号（跨壳都有；Android 壳另有 apkVer 做自更新） */
      window.CB.appVer = function(){ return String(st('appVer','{}')); };
      /* 音量下键 = 发送（Android 壳在 shim.js 里做，iOS 没有 shim.js，这里补上） */
      window.__cbSend = function (byKey) {
        var ta = document.getElementById('txt');
        if (!ta || !ta.value || !ta.value.trim()) {
          if (byKey) { try { show('没有可发送的内容', true); } catch (e) { } }
          return;
        }
        try {
          if (typeof window.sendMsg === 'function') { window.sendMsg(); show('已发送'); return; }
        } catch (e) { }
        var b = document.getElementById('send');
        if (b) { try { b.click(); show('已发送'); return; } catch (e) { } }
        if (byKey) { try { show('这个页面没有发送按钮', true); } catch (e) { } }
      };
    })();
    """

    static let offlineHTML = """
    <!doctype html><html lang="zh"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <title>Codeboss</title>
    <style>
    html,body{height:100%;margin:0;background:#0b0e14;color:#e6edf3;
      font:16px/1.55 -apple-system,"PingFang SC",sans-serif;-webkit-user-select:none}
    .box{max-width:440px;margin:0 auto;padding:64px 22px;box-sizing:border-box;text-align:center}
    .ico{font-size:40px;line-height:1}
    h1{font-size:19px;font-weight:600;margin:14px 0 6px}
    p{margin:0;color:#8b949e;font-size:13.5px;white-space:pre-line}
    .card{margin:22px 0;background:#12171F;border:1px solid #30363D;border-radius:14px;
      padding:13px 14px;display:flex;justify-content:space-between;font-size:14px}
    .card b{color:#8b949e;font-weight:400}
    .big{display:block;width:100%;padding:15px;border-radius:12px;border:0;margin-top:18px;
      background:#2EA043;color:#fff;font-size:16px;font-weight:600;font-family:inherit}
    .ghost{display:block;width:100%;padding:12px;margin-top:8px;border-radius:12px;
      border:1px solid #30363D;background:none;color:#58A6FF;font-size:14px;font-family:inherit}
    .hint{margin-top:12px;color:#6e7681;font-size:12px}
    </style></head><body><div class="box">
    <div class="ico">⚡</div>
    <h1 id="t">断网了</h1>
    <p id="lead">现在连不上。检查一下手机的网络（WiFi / 蜂窝数据），网一通就自动重连。</p>
    <div class="card"><b>这台手机</b><span id="net">读取中…</span></div>
    <button class="big" onclick="retry()">重 试</button>
    <button class="ghost" onclick="home()">开发机列表（换一台）</button>
    <div class="hint" id="h">正在自动重试…</div>
    </div><script>
    function say(t){ var h=document.getElementById('h'); if(h) h.textContent=t; }
    function paint(){
      var d=null;
      try{ d=JSON.parse(CB.netState()); }catch(e){}
      var n=document.getElementById('net');
      if(n&&d) n.textContent=d.wifi?'WiFi':(d.mobile?'移动数据':(d.online?'在线':'离线'));
      /* 只说「断网」，不再分手机没网 / 开发机没回应。文案固定在 HTML 里。 */
    }
    function retry(){
      var ok=true; try{ ok=JSON.parse(CB.netState()).online!==false; }catch(e){}
      if(!ok){ say('还是没有网络，连上后会自动重载'); return; }
      say('正在重试…'); try{ CB.retry(); }catch(e){}
    }
    /* 换一台：切到连不上的开发机卡在断网页时，直接回中继的会话列表 */
    function home(){
      var ok=true; try{ ok=JSON.parse(CB.netState()).online!==false; }catch(e){}
      if(!ok){ say('手机没网，连上后会自动重载到开发机列表'); return; }
      say('正在打开开发机列表…'); try{ CB.home(); }catch(e){}
    }
    paint(); setInterval(paint,3000);
    </script></body></html>
    """
}
