// SPDX-License-Identifier: AGPL-3.0-or-later

import UIKit
import WebKit
import Network
import AVFoundation

/// 唯一界面：全屏 WKWebView 指向 Codeboss 中继（或局域网地址）。
/// 页面本身是 ext.html；壳只补 iOS 拿不到/不稳的能力：
/// 原生录音（按住说话）、扫码配对、亮度/压暗/护眼浮层、常亮、电量与网络状态。
final class MainViewController: UIViewController {
    private static let urlKey = "server_url"
    private static let defaultURL = "https://relay.example.com/"

    private var web: WKWebView!
    private var bridge: Bridge!
    private var shimTimer: Timer?
    private var retryTimer: Timer?
    private var monitor: NWPathMonitor?
    private var showingOffline = false
    private var loading = false
    private var netOk = true                 /// NWPathMonitor 最近一次说「手机有网」
    private var offlineSoon: DispatchWorkItem?   /// 断网 6 秒后切网络页（瞬断不算）

    override var prefersStatusBarHidden: Bool { true }
    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .allButUpsideDown }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        bridge = Bridge(host: self)
        bridge.onReload = { [weak self] in self?.loadServer() }
        bridge.onOffline = { [weak self] in self?.showOffline() }   // 页面请求切网络页
        bridge.onHome = { [weak self] in self?.loadHome() }         // 断网页 → 开发机列表
        setupWeb()
        bridge.web = web
        bridge.startVolumeView(in: view)
        bridge.applyStoredSettings()
        startTimers()
        startNetworkMonitor()
        loadServer()
    }

    // MARK: - Web

    private func setupWeb() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        /* 后台「终端」页靠 UA 认 App + 读版本（和安卓壳同一个约定 CodebossShell/x.y.z）。
           用 applicationNameForUserAgent 是官方 API：它把这段拼在标准 UA 末尾，
           机型和系统版本照旧保留（后台靠 UA 里的 iPhone 认机型）。 */
        let ver = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0"
        cfg.applicationNameForUserAgent = "CodebossShell/" + ver
        cfg.userContentController.add(bridge, name: "cb")
        cfg.userContentController.addUserScript(WKUserScript(
            source: bridge.startJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))

        web = WKWebView(frame: view.bounds, configuration: cfg)
        web.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.scrollView.keyboardDismissMode = .interactive
        web.allowsBackForwardNavigationGestures = false
        web.allowsLinkPreview = false
        web.isOpaque = false
        web.backgroundColor = .black
        view.addSubview(web)
    }

    private func serverURL() -> URL? {
        var s = UserDefaults.standard.string(forKey: Self.urlKey) ?? Self.defaultURL
        s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { s = Self.defaultURL }
        /* 老版本存下来的还是旧中继域名：跟着切到新域名（协议/端口原样保留） */
        s = s.replacingOccurrences(of: "relay.example.com", with: "relay.example.com",
                                   options: .caseInsensitive)
        if !s.contains("://") { s = "https://" + s }
        return URL(string: s)
    }

    /// 壳的产品线：构建期 CBProduct（project.yml / Info.plist），缺省工头线。
    /// 每个 App 都必须自报家门——host 一个进程同时伺候多条线的客户端，
    /// 不报只能回落 host 自己的线，那就成了「这条线的 App 长得像另一条线」。
    private var productLine: String {
        (Bundle.main.infoDictionary?["CBProduct"] as? String) ?? "codeboss"
    }

    /// 首屏 URL 也带 ?product=（安卓壳同款）：cookie 还没写进去/被清的窗口靠它兜底。
    private func withProduct(_ u: URL) -> URL {
        guard var c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return u }
        var q = c.queryItems ?? []
        if q.contains(where: { $0.name == "product" }) { return u }
        q.append(URLQueryItem(name: "product", value: productLine))
        c.queryItems = q
        return c.url ?? u
    }

    func loadServer() {
        guard !loading, let u = serverURL() else { return }
        if !netOk {                       /* 手机没网：别撞一次注定失败的加载，直接给网络页 */
            showOffline()
            return
        }
        loading = true
        showingOffline = false
        seedProductCookie { [weak self] in
            guard let self = self else { return }
            var req = URLRequest(url: self.withProduct(u))
            req.timeoutInterval = 20
            req.cachePolicy = .reloadRevalidatingCacheData
            self.web.load(req)
        }
    }

    /// 壳是哪个端：生产页靠 `cb_product` cookie 判产品线（中继会把它搬成 X-Product）。
    /// 不种的话 host 只能回落到它自己的 product，工头壳里会把老板端的能力一起显示出来
    /// （2026-11-18 安卓端同款 bug，中继侧已由 relay._fwd_headers 收口）。
    private func seedProductCookie(_ done: (() -> Void)? = nil) {
        let product = productLine
        var hosts = ["relay.example.com"]
        if let h = serverURL()?.host, !hosts.contains(h) { hosts.append(h) }
        let store = WKWebsiteDataStore.default().httpCookieStore
        let group = DispatchGroup()
        for h in hosts {
            group.enter()
            let props: [HTTPCookiePropertyKey: Any] = [
                .name: "cb_product", .value: product, .domain: h, .path: "/",
                .secure: h == "relay.example.com",
            ]
            if let c = HTTPCookie(properties: props) {
                store.setCookie(c) { group.leave() }
            } else {
                group.leave()
            }
        }
        group.notify(queue: .main) { done?() }
    }

    /// 断网页「开发机列表」：切到一台连不上的开发机卡住时，回中继根路径换一台。
    /// 不用 web.goBack()——历史里退一格还是那台坏掉的开发机页面。
    private func loadHome() {
        guard var c = serverURL().flatMap({ URLComponents(url: $0, resolvingAgainstBaseURL: false) })
        else { return }
        c.path = "/"
        c.query = nil
        c.fragment = nil
        guard let u = c.url else { return }
        if !netOk { showOffline(); return }     // 手机没网：去了也是白跑
        loading = true
        showingOffline = false
        var req = URLRequest(url: withProduct(u))
        req.timeoutInterval = 20
        req.cachePolicy = .reloadRevalidatingCacheData
        web.load(req)
    }

    private func startTimers() {
        shimTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.bridge.pushState()
            self?.bridge.beat()      /* 页面心跳：App 被挂起时心跳会停，页面据此判后台 */
        }
        retryTimer = Timer.scheduledTimer(withTimeInterval: 12.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if self.showingOffline { self.loadServer() }
        }
    }

    private func startNetworkMonitor() {
        let m = NWPathMonitor()
        m.pathUpdateHandler = { [weak self] path in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.bridge.updatePath(path)
                self.netOk = (path.status == .satisfied)
                if self.netOk {
                    self.cancelOfflineSoon()
                    if self.showingOffline { self.loadServer() }
                } else {
                    self.scheduleOfflineSoon()
                }
            }
        }
        m.start(queue: DispatchQueue(label: "codeboss.net"))
        monitor = m
    }

    /// 已经没网：过 6 秒还连不上就换成网络页，别让人对着一堆点不动的按钮干等
    /// （切 Wi-Fi / 进电梯的瞬断在下一次 path 回调里会被取消）。
    private func scheduleOfflineSoon() {
        if showingOffline || offlineSoon != nil { return }
        let w = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            self.offlineSoon = nil
            if self.showingOffline || self.netOk { return }
            if self.bridge.recordingNow {           // 正在说话：录完再说
                self.scheduleOfflineSoon()
                return
            }
            self.showOffline()
        }
        offlineSoon = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 6, execute: w)
    }

    private func cancelOfflineSoon() {
        offlineSoon?.cancel()
        offlineSoon = nil
    }

    private func showOffline() {
        cancelOfflineSoon()
        if showingOffline { return }
        showingOffline = true
        web.loadHTMLString(Bridge.offlineHTML, baseURL: nil)
    }

    // MARK: - 扫码配对

    func presentScanner() {
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        switch status {
        case .authorized:
            showScanner()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] ok in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if ok { self.showScanner() }
                    else { self.js("window.__cbQrDenied && window.__cbQrDenied()") }
                }
            }
        default:
            js("window.__cbQrDenied && window.__cbQrDenied()")
        }
    }

    private func showScanner() {
        let sc = QRScannerController(
            onResult: { [weak self] text in
                self?.dismiss(animated: true) {
                    self?.js("window.__cbQr && window.__cbQr(\(JS.str(text)))")
                }
            },
            onCancel: { [weak self] in self?.dismiss(animated: true) })
        sc.modalPresentationStyle = .fullScreen
        present(sc, animated: true)
    }

    private func js(_ code: String) {
        web.evaluateJavaScript(code, completionHandler: nil)
    }

    func handleDeepLink(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "codeboss" else { return false }
        var target: String?
        if let c = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            target = c.queryItems?.first(where: { $0.name == "url" })?.value
            if target == nil, let q = c.query, q.contains("://") { target = q }
        }
        guard let t = target, !t.isEmpty else { return false }
        UserDefaults.standard.set(t, forKey: Self.urlKey)
        loadServer()
        return true
    }
}

// MARK: - Navigation / JS alerts

extension MainViewController: WKNavigationDelegate, WKUIDelegate {
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        /* 页面要换了（重载/跳转）：手指底下的按钮马上就不存在了，WebKit 的
           触摸序列会断 → 松手事件送不回来。趁还在录，主动停掉，别让麦克风
           一直开着（按钮显示归零、其实还在录的那种「停不下来」）。 */
        bridge.stopRecordingIfAny()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loading = false
        cancelOfflineSoon()
        bridge.pushState()
        /* 换页/刷新时 WebKit 会把音频会话停掉：静音底座要是跟着死了，第一次按
           音量上键只是调音量（改的是铃声，host 收不到任何变化），得先点一下屏幕上
           的录音键才恢复。加载完立刻重撑一次。 */
        bridge.keeperWake()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleFailure(error)
    }

    private func handleFailure(_ error: Error) {
        loading = false
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        // 只有「真的连不上」才换离线页。打不开某个内容（比如点「更新 App」
        // 跳到 /codeboss.apk，WKWebView 显示不了这个安装包）不算断网，
        // 否则会莫名其妙把人弹到「手机没网了」。
        if e.domain == NSURLErrorDomain {
            switch e.code {
            case NSURLErrorNotConnectedToInternet, NSURLErrorCannotFindHost,
                 NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost,
                 NSURLErrorTimedOut, NSURLErrorDNSLookupFailed:
                break                       // 真·没网
            default:
                return
            }
        } else if e.domain == WKError.errorDomain {
            return                          // WebKit 自己的错（下载/策略拦下），不是网络
        }
        showOffline()
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        let scheme = (url.scheme ?? "").lowercased()
        /* 手机没网时不让 WebView 去加载 http(s)：那会停在一张「网页无法打开」上，
           还把完整地址露在外面。拉起支付宝/微信付款也一样——没网只会弹它们自己的错。
           这些都直接取消并换成我们自己的网络页。 */
        let isMain = (navigationAction.targetFrame?.isMainFrame ?? true)
        let needsNet = (scheme == "http" || scheme == "https" || scheme == "alipays"
                        || scheme == "alipay" || scheme == "weixin")
        if !netOk && isMain && needsNet {
            decisionHandler(.cancel)
            DispatchQueue.main.async { self.showOffline() }
            return
        }
        if scheme == "http" || scheme == "https" || scheme == "about" || scheme == "file" || scheme.isEmpty {
            decisionHandler(.allow)
            return
        }
        // alipays://、weixin:// 等付款/外部 App：交给系统打开，WebView 不导航
        UIApplication.shared.open(url, options: [:], completionHandler: nil)
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "好", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "确定", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }
}
