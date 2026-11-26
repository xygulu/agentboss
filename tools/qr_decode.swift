// SPDX-License-Identifier: AGPL-3.0-or-later

// 用 macOS 自带的 Vision 框架解二维码，打印所有 payload。
//   swiftc -O tools/qr_decode.swift -o /tmp/qrdec
//   /tmp/qrdec group.png
// 输出：每行 "PAYLOAD: <内容>"；解不到返回 1，读不了图返回 2。
// 用途：换群码前先确认新图确实能扫出链接，避免把糊图/截错图推到官网。
import Foundation
import Vision
import AppKit

let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let cg = rep.cgImage else {
    print("ERR: cannot load \(path)")
    exit(2)
}
let req = VNDetectBarcodesRequest()
req.symbologies = [.qr]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do { try handler.perform([req]) } catch {
    print("ERR: \(error)")
    exit(3)
}
let obs = (req.results ?? []).filter { $0.symbology == .qr }
if obs.isEmpty {
    print("NO-QR \(path)")
    exit(1)
}
for o in obs { print("PAYLOAD: \(o.payloadStringValue ?? "")") }
