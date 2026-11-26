// SPDX-License-Identifier: AGPL-3.0-or-later

import Foundation

enum JS {
    /// 把任意 Swift 字符串安全地塞进 JS 字符串字面量（含引号/换行/控制字符）。
    static func str(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [s], options: [])) ?? Data("[]".utf8)
        let arr = String(data: data, encoding: .utf8) ?? "[]"
        return String(arr.dropFirst().dropLast())
    }
}
