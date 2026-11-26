// SPDX-License-Identifier: AGPL-3.0-or-later

import UIKit
import AVFoundation
import MediaPlayer

/// 音量键接管（iOS 近似版）。
///
/// iOS 没有 Android 那种硬件按键事件（dispatchKeyEvent 无对应物），
/// 音量键的「按下 / 松开」拿不到。能观测到的只有音量数值的变化：
///
///   · AVAudioSession.sharedInstance().outputVolume 的 KVO
///   · MPVolumeView 内置滑块的 value
///
/// 一次按键 = 音量变一格（1/16）；按住不放会连续变格。
/// 所以把连续的变化合并成一次「按下」，停顿时判为「松开」。
///
/// 副作用：接管期间音量会被拉回基准值（用户音量不漂，也不会顶到头
/// 导致后续按键测不到）。
final class VolumeKeyInterceptor: NSObject {
    enum Key { case up, down }

    /// 一串变化的开始（= 按下）。
    var onPress: ((Key) -> Void)?
    /// 一串变化结束（= 松开）。
    var onRelease: (() -> Void)?
    /// 每一格变化。
    var onStep: ((Key) -> Void)?

    /// 超过这么久没有新变化 = 松手。要比系统按住不放时的「首次连发延迟」
    /// （约 0.5s）长一点，否则按住不放会被误判成松手，录音刚开头就被掐掉。
    private let burstGap: TimeInterval = 0.7
    /// 一格 = 1/16 = 0.0625，取一半当门槛。
    private let stepThreshold: Float = 0.03
    /// 偏离基准超过这么多就把音量拉回去（别顶到头，那样就测不到后续按键）。
    /// 一格是 0.0625，所以 0.04 = **变一格就拉回**：按住时音量只晃一下，
    /// 背景音乐听不出音量在爬。0.06 要等两格，晃动翻倍。
    private let restoreDrift: Float = 0.04
    /// 忽略「自己造成的回落」的时间窗（回声马上就到，窗开小点，免得吃掉真按键）。
    private let echoWindow: TimeInterval = 0.35
    /// 同一瞬间的反向变化（滑块与系统音量的两次上报）当成重复。
    private let dupWindow: TimeInterval = 0.15

    private weak var volumeView: MPVolumeView?
    private var slider: UISlider?
    private var sliderObs: NSKeyValueObservation?
    private var sessionObs: NSKeyValueObservation?

    /// 上一次看到的音量（任何信号源都更新它）。
    private var lastLevel: Float = 0
    /// 自己改的目标音量：落到它附近的读数是回声，不算按键。
    private var expected: (level: Float, at: Date)?
    /// 接管前的音量：漂了就拉回来。**只在中间区间**用：顶在 16/16 时按「上」、
    /// 或者到底 0 时按「下」，音量根本不会变，也就观测不到那一下。
    private var baseline: Float = 0
    /// 这段时间内的读数一律忽略（切音频类别时系统音量会跳一下）。
    private var suppressUntil = Date.distantPast

    /// 一个还没坐实的读数：等 120ms 看它是真按键还是一闪而过的假读数。
    private struct Pending {
        var level: Float     // 这个读数
        var prev: Float      // 变之前的值
        var key: Key
    }
    private var pending: Pending?
    private var confirmTimer: Timer?
    /// 读数要撑过这么久才算按键。够短（按键手感不受影响），
    /// 又够长（假读数几毫秒就被另一个信号源改回来）。
    private let confirmWindow: TimeInterval = 0.12
    /// 近期用过的基准值：我们 restore() 会把系统音量写回基准，这个写入是排队的，
    /// 偶尔几十毫秒到几秒后才落地（换类别/录完回底座时）。读数跳回某个**旧基准**
    /// 就是这么来的——不是按键（0.2.29 实机：录完 2.9 秒后自己回到 0.30，
    /// 被当成音量下键，手机弹「没有可发送的内容」）。
    private var pastBaselines: [(level: Float, at: Date)] = []
    private let pastBaseWindow: TimeInterval = 8

    private var lastKey: Key = .up
    private var lastStepAt = Date.distantPast
    /// 最近一次真按键的时刻：restore() 的写入只会在按键后排队，
    /// 晚落地的旧音量也只可能是那之后冒出来的。
    private var lastBurstAt = Date.distantPast
    /// 这一串变化的第一格（= 按下的时刻）。
    private var burstStart = Date.distantPast
    private var burstTimer: Timer?
    private var attachTries = 0

    /// 最近一次音量变化的时间。
    var lastChangeAt: Date { lastStepAt }
    /// 这一串按住持续了多久（松手判定用；点一下 ≈ 0）。
    var holdSeconds: TimeInterval { lastStepAt.timeIntervalSince(burstStart) }
    /// 这一串变了几格：只手滑一格 = 点一下；按住会连续变格（系统约 0.5s 后连发）。
    /// 比「持续了多久」可靠——切音频类别时那 0.25 秒的抑制窗会把中间的格吞掉，
    /// 持续时间算出来是 0，按住说话就被当成点按取消掉了（实锤 bug）。
    private(set) var burstSteps = 0
    var tapOnly: Bool { burstSteps < 2 }

    init(volumeView: MPVolumeView) {
        self.volumeView = volumeView
        let v = AVAudioSession.sharedInstance().outputVolume
        baseline = VolumeKeyInterceptor.usable(v)
        lastLevel = v
        super.init()
        sessionObs = AVAudioSession.sharedInstance()
            .observe(\.outputVolume, options: [.new]) { [weak self] _, ch in
                guard let self = self, let v = ch.newValue else { return }
                DispatchQueue.main.async { self.levelChanged(v) }
            }
        attachSlider()
    }

    /// 按键靠「音量变了」来判定，所以基准必须留在中间：0.2 约 3/16，0.8 约 13/16。
    /// 顶在两端时，往那个方向按音量不变 → 一格事件都没有（日志里 lvl=16 就是这个坑）。
    private static func usable(_ v: Float) -> Float { min(max(v, 0.2), 0.8) }

    deinit {
        sessionObs?.invalidate()
        sliderObs?.invalidate()
        burstTimer?.invalidate()
        confirmTimer?.invalidate()
    }

    /// MPVolumeView 的滑块是内部子视图，可能晚一步才建好：多试几次。
    private func attachSlider() {
        guard let s = findSlider(volumeView) else {
            attachTries += 1
            if attachTries < 6 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    self?.attachSlider()
                }
            }
            return
        }
        guard s !== slider else { return }
        sliderObs?.invalidate()
        slider = s
        sliderObs = s.observe(\.value, options: [.new]) { [weak self] _, ch in
            guard let self = self, let v = ch.newValue else { return }
            DispatchQueue.main.async { self.levelChanged(v) }
        }
        /* 滑块到位时音量可能正顶在两端：主动挪进可用区间（setBase 里会 restore）。
           只在启动/首次接管时做，之后用户怎么调都尊重，不再插手。 */
        let v = AVAudioSession.sharedInstance().outputVolume
        if v < 0.2 || v > 0.8 { setBase(level: v) }
    }

    private func findSlider(_ view: UIView?) -> UISlider? {
        guard let v = view else { return nil }
        if let s = v as? UISlider { return s }
        for sub in v.subviews {
            if let s = findSlider(sub) { return s }
        }
        return nil
    }

    /// 音量基准变了（页面滑条改的、音频类别切了）：把当前读数当成已知值，
    /// 之后的变化才算按键——这样「自己造成的跳变」不会被误当成用户按键。
    /// 落在两端时把基准夹回中间，并真的把音量挪过去：否则那一个方向永远测不到。
    func setBase(level: Float) {
        pending = nil                    // 自己造成的跳变：挂起中的读数作废
        confirmTimer?.invalidate()
        let b = VolumeKeyInterceptor.usable(level)
        if abs(b - baseline) > 0.005 { rememberBaseline(baseline) }
        baseline = b
        expected = (level, Date())
        lastLevel = level
        if abs(level - b) > 0.01 { restore() }
    }

    /// 交还控制权（切后台）：把音量放到指定值，而且**不夹回中间段**。
    /// 前台要留在中间段才测得到按键；离开时得把用户原本的音量还回去。
    func restoreTo(_ level: Float) {
        pending = nil
        confirmTimer?.invalidate()
        if abs(level - baseline) > 0.005 { rememberBaseline(baseline) }
        baseline = level
        expected = (level, Date())
        lastLevel = level
        guard let s = slider else { return }
        s.setValue(level, animated: false)
        s.sendActions(for: .touchUpInside)
    }

    /// 切音频类别时系统音量会跳一下：这段时间内的读数一律不算按键。
    /// 窗口要短（< burstGap），否则按住不放时的连续变格会被吃掉。
    /// 记一个刚退役的基准值，给「排队写入晚落地」的读数对照用。
    private func rememberBaseline(_ level: Float) {
        let now = Date()
        pastBaselines = pastBaselines.filter { now.timeIntervalSince($0.at) < pastBaseWindow }
        pastBaselines.append((level, now))
    }

    /// 这个读数是不是我们自己早先写进去、后来才落地的音量？
    /// 只有刚按过键（那才会有 restore() 的写入排队）才可能是；平时没写过，
    /// 就不能拿「旧基准」当借口吃掉用户的真按键。
    private func isLateEcho(_ v: Float, now: Date) -> Bool {
        guard abs(v - baseline) > 0.02 else { return false }   // 回到当前基准 = 正常收敛
        guard now.timeIntervalSince(lastBurstAt) < pastBaseWindow else { return false }
        return pastBaselines.contains {
            now.timeIntervalSince($0.at) < pastBaseWindow && abs(v - $0.level) < 0.02
        }
    }

    func suppress(_ seconds: TimeInterval = 0.25) {
        pending = nil
        confirmTimer?.invalidate()
        suppressUntil = Date().addingTimeInterval(seconds)
    }

    // MARK: - 信号

    private func levelChanged(_ v: Float) {
        let now = Date()
        /* 每一次读数都留痕：按键没反应时，日志里「有没有这一行」直接分成两种病：
           一行都没有 = 音量键根本没改媒体音量（会话没撑住/按的是铃声音量）；
           有这行但没 press = 变化被判成回声/重复，或者方向撞到端点了。 */
        Bridge.dbg("CBVOL lvl=\(String(format: "%.3f", v)) was=\(String(format: "%.3f", lastLevel)) sup=\(now < suppressUntil)")
        if now < suppressUntil {
            pending = nil
            confirmTimer?.invalidate()
            lastLevel = v
            /* 抑制窗里也要把音量拉回中间：不拉的话按住会一路冲到 16/16，
               系统随即不再发变化 → 0.7 秒后当成松手，按住说话被掐断。 */
            if abs(v - baseline) > restoreDrift { restore() }
            return
        }
        // 落在自己刚设的目标附近 = 回声（系统音量回落 / 滑块同步），忽略。
        if let e = expected, now.timeIntervalSince(e.at) < echoWindow,
           abs(v - e.level) < 0.02 {
            lastLevel = v
            return
        }
        // 跳回某个刚退役的基准 = 我们自己排队写入的音量晚落地，不是按键。
        if isLateEcho(v, now: now) {
            lastLevel = v
            Bridge.dbg("CBVOL 旧基准回声 lvl=\(String(format: "%.3f", v)) base=\(String(format: "%.3f", baseline))")
            if abs(v - baseline) > restoreDrift { restore() }
            return
        }
        /* 滑块和会话音量是两个信号源，会短暂各说各话：换音频类别/录完回到
           底座时，一个源蹦出 0.30→0.25，几毫秒后另一个源又报回 0.30。以前这种
           一闪而过的假读数直接当按键，用户根本没碰音量键，手机上却弹
           「没有可发送的内容」（0.2.29 实机日志 cbvol.log：每次「抢回音频路由」
           后面紧跟一条 press down lvl=0.25）。真实按键改的是系统音量，读数
           撑得住、而且按住不放还会继续往同方向走。所以先挂起，等第二个读数：
             · 又回到原值 → 假读数，丢掉
             · 同方向再进一步/确认 → 真按键，立刻发
             · 120ms 内没有下文 → 读数撑住了，真按键 */
        if let p = pending {
            let dir: Float = p.level > p.prev ? 1 : -1
            if abs(v - p.prev) < 0.02 {
                pending = nil
                confirmTimer?.invalidate()
                lastLevel = v
                Bridge.dbg("CBVOL 假读数丢弃 lvl=\(String(format: "%.3f", p.level)) → 回到 \(String(format: "%.3f", v))")
                return
            }
            if abs(v - p.level) < 0.02 || (v - p.prev) * dir >= (p.level - p.prev) * dir {
                pending = nil
                confirmTimer?.invalidate()
                lastLevel = v
                step(p.key)
                return
            }
        }
        let delta = v - lastLevel
        lastLevel = v
        guard abs(delta) >= stepThreshold else { return }
        let key: Key = delta > 0 ? .up : .down
        // 同一瞬间的反向读数 = 滑块与系统音量的两次上报，当重复丢掉。
        if key != lastKey, now.timeIntervalSince(lastStepAt) < dupWindow {
            return
        }
        pending = Pending(level: v, prev: v - delta, key: key)
        confirmTimer?.invalidate()
        confirmTimer = Timer.scheduledTimer(withTimeInterval: confirmWindow, repeats: false) { [weak self] _ in
            guard let self = self, let p = self.pending else { return }
            self.pending = nil
            if abs(self.lastLevel - p.level) < 0.02 {
                Bridge.dbg("CBVOL 读数撑住确认 \(p.key == .up ? "up" : "down") lvl=\(String(format: "%.3f", p.level))")
                self.step(p.key)
            } else {
                Bridge.dbg("CBVOL 未确认丢弃 lvl=\(String(format: "%.3f", p.level)) now=\(String(format: "%.3f", self.lastLevel))")
            }
        }
    }

    private func step(_ key: Key) {
        let now = Date()
        let sameBurst = lastKey == key && now.timeIntervalSince(lastStepAt) < burstGap
        lastKey = key
        lastStepAt = now
        lastBurstAt = now
        if !sameBurst {
            burstStart = now
            burstSteps = 0
            Bridge.dbg("CBVOL press \(key == .up ? "up" : "down") lvl=\(lastLevel)")
            onPress?(key)
        } else {
            Bridge.dbg("CBVOL step  \(key == .up ? "up" : "down") lvl=\(lastLevel) hold=\(String(format: "%.2f", holdSeconds))")
        }
        burstSteps += 1
        onStep?(key)
        // 停顿时算松手
        burstTimer?.invalidate()
        burstTimer = Timer.scheduledTimer(withTimeInterval: burstGap, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            Bridge.dbg("CBVOL release steps=\(self.burstSteps) hold=\(String(format: "%.2f", self.holdSeconds))")
            self.onRelease?()
        }
        // 别让音量顶到头：漂了就拉回基准，抬手后音频不会突然变大。
        if abs(lastLevel - baseline) > restoreDrift {
            restore()
        }
    }

    /// 把音量拉回基准值。
    private func restore() {
        guard let s = slider else { return }
        expected = (baseline, Date())
        s.setValue(baseline, animated: false)
        s.sendActions(for: .touchUpInside)
        lastLevel = baseline
    }
}
