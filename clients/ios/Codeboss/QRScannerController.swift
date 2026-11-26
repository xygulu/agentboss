// SPDX-License-Identifier: AGPL-3.0-or-later

import UIKit
import AVFoundation
import AudioToolbox

/// 扫码配对：全屏相机预览 + 取景框，识别到二维码后回传原文。
/// 页面（ext.html）收到 window.__cbQr(text) 后自己解析配对码并绑定。
final class QRScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    private let onResult: (String) -> Void
    private let onCancel: () -> Void

    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "codeboss.qr")
    private var preview: AVCaptureVideoPreviewLayer?
    private var overlay = UIView()
    private var holeLayer = CAShapeLayer()
    private var frameLayer = CAShapeLayer()
    private var started = false
    private var scanned = false

    init(onResult: @escaping (String) -> Void, onCancel: @escaping () -> Void) {
        self.onResult = onResult
        self.onCancel = onCancel
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override var prefersStatusBarHidden: Bool { true }
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .portrait }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        setupCamera()
        setupOverlay()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        guard !started, session.inputs.count > 0 else { return }
        started = true
        queue.async { [weak self] in self?.session.startRunning() }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        queue.async { [weak self] in self?.session.stopRunning() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
        if let c = preview?.connection, c.isVideoOrientationSupported {
            c.videoOrientation = .portrait
        }
        layoutOverlay()
    }

    // MARK: - 相机

    private func setupCamera() {
        guard let dev = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: dev) else {
            showTip("相机不可用")
            return
        }
        session.beginConfiguration()
        if session.canAddInput(input) { session.addInput(input) }
        let out = AVCaptureMetadataOutput()
        if session.canAddOutput(out) {
            session.addOutput(out)
            out.setMetadataObjectsDelegate(self, queue: .main)
            if out.availableMetadataObjectTypes.contains(.qr) {
                out.metadataObjectTypes = [.qr]
            } else {
                out.metadataObjectTypes = out.availableMetadataObjectTypes
            }
        }
        session.commitConfiguration()

        let p = AVCaptureVideoPreviewLayer(session: session)
        p.videoGravity = .resizeAspectFill
        p.frame = view.bounds
        view.layer.addSublayer(p)
        preview = p
    }

    private func setupOverlay() {
        overlay.backgroundColor = UIColor.black.withAlphaComponent(0.55)
        overlay.isUserInteractionEnabled = false
        overlay.layer.mask = holeLayer
        view.addSubview(overlay)

        let stroke = frameLayer
        stroke.strokeColor = UIColor.white.withAlphaComponent(0.9).cgColor
        stroke.fillColor = UIColor.clear.cgColor
        stroke.lineWidth = 2
        view.layer.addSublayer(stroke)

        let tip = UILabel()
        tip.text = "对准电脑屏幕上的二维码"
        tip.textColor = .white
        tip.font = .systemFont(ofSize: 14)
        tip.textAlignment = .center
        tip.tag = 901
        view.addSubview(tip)

        let close = UIButton(type: .system)
        close.setTitle("‹ 返回", for: .normal)
        close.setTitleColor(.white, for: .normal)
        close.titleLabel?.font = .systemFont(ofSize: 16)
        close.addTarget(self, action: #selector(cancel), for: .touchUpInside)
        close.tag = 902
        view.addSubview(close)
    }

    private func holeRect() -> CGRect {
        let w = view.bounds.width, h = view.bounds.height
        let side = min(w, h) * 0.66
        return CGRect(x: (w - side) / 2, y: (h - side) / 2 - h * 0.06,
                      width: side, height: side)
    }

    private func layoutOverlay() {
        overlay.frame = view.bounds
        let hole = holeRect()
        let path = UIBezierPath(rect: overlay.bounds)
        path.append(UIBezierPath(roundedRect: hole, cornerRadius: 18).reversing())
        holeLayer.path = path.cgPath

        frameLayer.path = UIBezierPath(roundedRect: hole.insetBy(dx: -12, dy: -12),
                                       cornerRadius: 26).cgPath
        view.viewWithTag(901)?.frame = CGRect(x: 0, y: hole.maxY + 18,
                                              width: view.bounds.width, height: 22)
        view.viewWithTag(902)?.frame = CGRect(x: 18, y: view.safeAreaInsets.top + 6,
                                              width: 88, height: 40)
    }

    private func showTip(_ text: String) {
        let l = UILabel()
        l.text = text
        l.textColor = .white
        l.textAlignment = .center
        l.frame = view.bounds
        l.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(l)
    }

    @objc private func cancel() {
        onCancel()
    }

    // MARK: - 识别

    func metadataOutput(_ output: AVCaptureMetadataOutput,
                        didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !scanned else { return }
        guard let obj = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
              let text = obj.stringValue, !text.isEmpty else { return }
        scanned = true
        AudioServicesPlaySystemSound(SystemSoundID(kSystemSoundID_Vibrate))
        queue.async { [weak self] in self?.session.stopRunning() }
        onResult(text)
    }
}
