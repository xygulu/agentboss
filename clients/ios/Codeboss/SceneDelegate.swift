// SPDX-License-Identifier: AGPL-3.0-or-later

import UIKit

/// 单窗口：全屏 WKWebView 壳，窗口在这里建。
///
/// iOS 26 起，链接新 SDK 的 App 必须接 UIScene 生命周期，否则启动时 UIKit 直接
/// 判 runtime issue 把进程打死（EXC_BREAKPOINT in
/// ___UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption）。
/// 0.2.20 用 iOS 27 SDK 打包、还在老式 AppDelegate 里建 window，在 iOS 27.2.1 上
/// 秒崩就是这个原因。
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let ws = scene as? UIWindowScene else { return }
        let win = UIWindow(windowScene: ws)
        win.backgroundColor = .black
        win.rootViewController = MainViewController()
        win.makeKeyAndVisible()
        window = win
        connectionOptions.urlContexts.forEach { open($0.url) }
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        URLContexts.forEach { open($0.url) }
    }

    private func open(_ url: URL) {
        _ = (window?.rootViewController as? MainViewController)?.handleDeepLink(url)
    }
}
