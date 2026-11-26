# SPDX-License-Identifier: AGPL-3.0-or-later

"""CodeBoss / AgentBoss 同装时的“别打架”（2026-11-21 用户）。

两家是独立安装包（com.codeboss.terminal / com.agentboss.terminal），各存各的
prefs，却写同一套系统音量与铃声模式。三处会打架，各有各的修法：

1. 静音是边沿记账（SA_WE：上次静过没有）——另一家的滑条拖一下（0 值写铃声流
   会顺手解除 RINGER_MODE_SILENT）就能把这边静音悄悄打开，而记账还“以为已静音”。
   → enforceSysAudio 的静音方向改成**电平**：每次 tick 读系统真值，漂了就重写。
   恢复方向仍只在“账上记着是我们静的”时做：另一家静的音不是我们能替它开的。
2. 滑条各显示各的旧值 → 打开设置以系统真值为准（逻辑在 tests/test_audio_coexist.mjs）。
3. 两家的播报都直接 MediaPlayer/TTS start()，同时提醒就叠音 →
   播报前要 AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK，另一家收到“可压低”回调把自己
   压到 20%（不掐断：播报不许截断），念完 abandon。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BG = ROOT / "clients" / "android" / "src" / "com" / "agentboss" / "terminal" / "BgService.java"
MA = ROOT / "clients" / "android" / "src" / "com" / "agentboss" / "terminal" / "MainActivity.java"


def _src(p):
    return p.read_text(encoding="utf-8")


def test_mute_is_level_triggered():
    src = _src(BG)
    assert "sysSilentNow" in src, "要有“系统现在真的静着吗”的真值判断"
    m = re.search(r"public static void enforceSysAudio\([\s\S]*?\n    \}", src)
    assert m, "找不到 enforceSysAudio()"
    body = m[0]
    assert "sysSilentNow(" in body, (
        "静音方向要电平判断：每 30 秒 tick 读一次系统真值，跟期望不一致就重写。"
        "只认 SA_WE 记账时，另一家 App 拖一下滑条就能把这边静音打开而这边不知道")
    assert "want == we" not in body, "边沿判断（want == we 就返回）必须去掉"


def test_unmute_only_when_we_muted():
    src = _src(BG)
    m = re.search(r"public static void enforceSysAudio\([\s\S]*?\n    \}", src)
    assert m
    body = m[0]
    # 恢复方向：只有账上记着“是我们静的”才恢复。另一家静的音不许替它打开。
    assert re.search(r"if \(!want\)\s*\{[\s\S]{0,160}?SA_WE|if \(!want\)\s*\{[\s\S]{0,160}?\bwe\b",
                     body), "不想要静音时要先看记账，别无条件恢复"


def test_speech_requests_audio_focus():
    bg = _src(BG)
    assert "AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK" in bg, (
        "播报前要申请可压低焦点，否则两家的语音直接叠在一起")
    assert "requestAudioFocus" in bg
    assert "abandonAudioFocus" in bg, "念完要放手（另一家被压低的能恢复音量）"
    assert "AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK" in bg, (
        "另一家播报时我们收到“可压低”回调，要压自己而不是掐断")
    assert re.search(r"setVolume\(", bg), "压低的落点是 MediaPlayer.setVolume"
    # 播放入口都要申请
    for fn in ("private void playFile(", "private void playCue("):
        m = re.search(re.escape(fn) + r"[\s\S]*?\n    \}", bg)
        assert m, f"找不到 {fn}"
        assert "focusForSpeech" in m[0], f"{fn} 开始放之前要申请焦点"
    # 念完（队列空）放手
    m = re.search(r"private void sayFinished\(\)\s*\{[\s\S]*?\n    \}", bg)
    assert m and "focusDone" in m[0], "队列念空时要 abandon"


def test_foreground_speech_also_uses_focus():
    ma = _src(MA)
    assert "focusForSpeech" in ma, "前台侧播报（TTS/内置音/合成整句）也要申请焦点"
    assert "focusDone" in ma, "前台侧播报念完要放手"
    for fn in ("private void speakText(", "private void playCue(", "private void playSayFile("):
        m = re.search(re.escape(fn) + r"[\s\S]*?\n    \}", ma)
        assert m, f"找不到 {fn}"
        assert "focusForSpeech" in m[0], f"{fn} 开始放之前要申请焦点"


def test_same_reminder_announced_once():
    """两家同装时同一条提醒会各念一遍（同一个 id，两条 SSE 都收）→ 回声。
    去重靠「认领」：占优那家（codeboss）收到就记下 id；另一家出声前先问一声，
    对方接了就不出声（通知照弹，只是静音）。"""
    bg = _src(BG)
    assert "claimId" in bg and "peerClaimed" in bg, "要有认领/询问两个动作"
    assert re.search(r"setResultCode\(1\)|RESULT_OK", bg), "被问到时要用结果码回答「我接了」"
    assert re.search(r"registerReceiver\(", bg), "要注册接收询问的 receiver"
    assert "R.string.cb_product" in bg, (
        "谁占优按产品线判（cb_product），不许写死包名——build.sh 会把 "
        "com.agentboss.terminal 全量改写成 com.<product>.terminal")
    m = re.search(r"private void handleNotify\([\s\S]*?\n    \}", bg)
    assert m, "找不到 handleNotify()"
    body = m[0]
    assert "peerClaimed(" in body, "收到提醒、出声之前要先问另一家"
    assert re.search(r"peerClaimed\([\s\S]{0,200}?sound\s*=\s*false[\s\S]{0,80}?voice\s*=\s*false", body), \
        "对方接了：这一条自己不响也不念"


def test_page_volume_sync_node_gate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不在，跳过页面接线检查")
    r = subprocess.run([node, str(ROOT / "tests" / "test_audio_coexist.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
