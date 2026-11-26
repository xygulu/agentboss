# SPDX-License-Identifier: AGPL-3.0-or-later

"""把 static/src/ 的源码拼成 static/ext.html（手机端实际下发的页面）。

为什么拆：ext.html 到 10k 行时，改收件箱要滚过整个设置页，两处改动常撞在一起。
**运行方式一点没变**——拼出来的还是一个单文件；只是源码按面板躺在 src/js 下。

规矩（CI 会查）：改前端改 src/ 下的文件，然后跑
    python tools/build_page.py
直接改 static/ext.html 会在 CI 里被「生成物过期」挡下来。

跑法：
    python tools/build_page.py          # 重新生成
    python tools/build_page.py --check  # 只检查生成物是不是最新的（CI 用）
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "host" / "static" / "src"
OUT = ROOT / "host" / "static" / "ext.html"

# 每一块对应页面里的一处内联 <script>：顺序就是加载顺序，别随便换。
# 拆分点都在「顶层声明」边界上，拼回去与拆分前逐字节相同（见 --check）。
BLOCKS = {
    "boot": ["00_boot.js"],           # 客户端标识、老内核补丁（最先跑）
    "main": ["01_core.js",            # 全局状态 / 计费请求 / 面板切换与手势
             "02_view.js",            # 会话列表、会话页、草稿、媒体
             "03_voice.js",           # 语音播报、提醒历史与健康、免打扰
             "04_net.js",             # 网络状态、推送、终端浮层、键盘
             "05_cmd.js",             # 命令、弹层、打开会话
             "06_settings.js",        # 设置、计费、版本与更新
             "07_account.js",         # 账号、设备、模型切换、删除会话
             "10_stl.js",             # 3D 模型（STL）预览：手指转视角 + 圈选
             "11_track.js"],          # 功能使用埋点（只记事件名，攒批发给中继）
    "ptt_feedback": ["08_ptt.js",     # 按住说话（壳原生录音 / 浏览器 getUserMedia）
                     "09_feedback.js"],  # 问题建议：表单 / 工单列表 / 详情
}


def build():
    page = (SRC / "page.html").read_text(encoding="utf-8")
    for key, parts in BLOCKS.items():
        mark = "@@JS:%s@@" % key
        if mark not in page:
            raise SystemExit("page.html 里没有占位符 %s" % mark)
        js = "\n".join((SRC / "js" / p).read_text(encoding="utf-8") for p in parts)
        page = page.replace(mark, js)
    return _stamp(page)


def _stamp(page):
    """PAGE_BUILD = 挖空戳位后全文的 sha256[:10]。

    2026-11-18 事故：戳从不更新（一直是手写的 '2026-11-14b'）→ 页面自更新
    （pverCheck 比对 /api/page_ver 的 b）永不触发 → 所有页面修复到不了手机。
    规矩：内容变 → 戳变；同内容重建 → 戳不变（--check 幂等）。
    不能对含戳全文求哈希再放回同一文件（自指）——挖空位是唯一可行口径，
    tests/test_page_stamp.py 按同一算法校验，两处必须一致。"""
    blanked = re.sub(r"PAGE_BUILD='[^']*'", "PAGE_BUILD=''", page)
    want = hashlib.sha256(blanked.encode("utf-8")).hexdigest()[:10]
    out, n = re.subn(r"PAGE_BUILD='[^']*'", "PAGE_BUILD='%s'" % want, page)
    if n == 0:
        raise SystemExit("页面里找不到 PAGE_BUILD 字面量（07_account.js 被动了？）")
    return out


def main():
    out = build()
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != out:
            print("static/ext.html 跟 static/src/ 对不上：\n"
                  "  改前端请改 static/src/，再跑 python tools/build_page.py")
            return 1
        print("static/ext.html 是最新的（%d 行）" % out.count("\n"))
        return 0
    OUT.write_text(out, encoding="utf-8", newline="\n")   # 别让 Windows 上写成 CRLF
    print("已生成 %s（%d 行）" % (OUT, out.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
