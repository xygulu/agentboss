# SPDX-License-Identifier: AGPL-3.0-or-later

"""真拍照（B1）：<input type=file accept=image/*> 要给「拍照」入口，不只是相册。

壳侧源码断言（真机效果留发版闸门）：
- onShowFileChooser 的 chooser 里带 ACTION_IMAGE_CAPTURE 初始 Intent；
- CAMERA 权限没给时先申请（本包声明了 CAMERA，未授权直接起拍照会 SecurityException）；
- 拍照回来的 data 是 null（结果写在 EXTRA_OUTPUT）：要按预先给的 content:// 回填；
- FileProvider 允许向 capture/ 子目录写（原来只读 openwith/）。

跑法：.venv/bin/python -m pytest tests/test_camera_capture.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

MAIN = (HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal" / "MainActivity.java").read_text("utf-8")
PROV = (HERE / "clients" / "android" / "src" / "com" / "agentboss" / "terminal" / "OpenFileProvider.java").read_text("utf-8")


def test_chooser_offers_camera():
    assert "ACTION_IMAGE_CAPTURE" in MAIN, "选图回调没有拍照 Intent：用户只能从相册选图"
    assert "EXTRA_INITIAL_INTENTS" in MAIN, "拍照没有作为 chooser 的初始 Intent 挂进去"


def test_camera_output_is_content_uri():
    assert "OpenFileProvider.AUTHORITY" in MAIN, "拍照输出没有走自家 FileProvider（file:// 在 targetSdk>=24 会被拒）"
    assert "EXTRA_OUTPUT" in MAIN, "拍照没有指定输出文件"


def test_camera_permission_requested_first():
    assert "PERM_CAMERA_FILE" in MAIN, "起拍照前没有申请 CAMERA 权限的路径"


def test_capture_result_delivered_when_data_null():
    assert "pendingCapture" in MAIN, "拍照返回 data=null 时没有可回填的 URI，页面收不到照片"


def test_provider_allows_capture_write():
    assert "capture" in PROV, "FileProvider 不认识 capture/ 目录，拍照写不进去"
    assert 'mode.contains("w")' in PROV or "MODE_WRITE_ONLY" in PROV, "FileProvider 只读，相机没法写照片"
