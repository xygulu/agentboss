# SPDX-License-Identifier: AGPL-3.0-or-later

"""电脑端（Mac 菜单栏 / Windows 托盘 / 安装包 / 文档）对外统一叫「BossHost 工头主机」。

2026-11-20 用户：Mac mini 上装的 host 菜单栏还写着「CodeBoss 开发机」，
电脑端早就统一成 BossHost 了，只是 Linux 命令行改了，Mac/Windows 的文案漏了。
用户随后定：不只是文案，安装包/App 文件名也一起改成 BossHost——
**老安装（CodeBoss.app）启动时自己改名迁移**，安装目录/服务名/token 路径等技术标识不动：
自动更新、计划任务、已有数据全靠这些名字找自己。

口径：
  · 产品名统一「BossHost 工头主机」；按钮「安装 BossHost 服务」；
  · 二维码旁提示按本机产品线说对应的端名：codeboss 是「口袋工头」，
    agentboss 是「代理工头」，认不得的（含以后接入的 deskboss）说通用「工头」；
    安装脚本/说明里没有产品线可读，统一写「工头 App（口袋工头 / 代理工头）」；
  · 文件名：BossHost.app、BossHost-Setup.exe、BossHost-x.y.z-mac.dmg；
    安装目录（~/.codeboss、%LOCALAPPDATA%\\Codeboss）、launchd/计划任务名、
    com.xuxingyu.codeboss 等技术标识保持不变。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(name):
    """文件按组件分目录：根 / host / relay / shared / clients 下找。"""
    for base in (ROOT, ROOT / "host", ROOT / "relay", ROOT / "shared", ROOT / "clients"):
        p = base / name
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(name)


def test_mac_app_bundle_display_name():
    s = read("mac/build-app.sh")
    assert "<key>CFBundleDisplayName</key><string>BossHost 工头主机</string>" in s
    assert "CodeBoss 开发机" not in s, "DMG/安装说明里还留着旧名字"
    assert 'VOLNAME="BossHost 工头主机"' in s


def test_mac_bundle_file_is_bosshost():
    s = read("mac/build-app.sh")
    assert 'APP="$BUILD/BossHost.app"' in s
    assert 'DMG="$REPO/bosshost-$VER-mac.dmg"' in s
    # 老版本 App 更新时硬找 DMG 里的「CodeBoss.app」：挂一个隐藏软链接过去，
    # 老机器才不用手动重装。ditto 会顺着它把 BossHost.app 的内容拷走（已实测）。
    assert 'ln -s BossHost.app "$DMGROOT/CodeBoss.app"' in s


def test_mac_old_install_migrates():
    s = read("mac/main.swift")
    assert 'return dir + "/BossHost.app"' in s, "装到哪里要用新包名"
    assert 'mnt + "/BossHost.app"' in s, "更新时从 DMG 里取的也是新包名"
    assert "DMG 里没有 BossHost.app" in s
    # 老机器上还是 CodeBoss.app：启动时改名再重启，不然 Finder 里永远叫老名字
    assert 'me.lastPathComponent == "CodeBoss.app"' in s
    assert 'appendingPathComponent("BossHost.app")' in s
    assert "if migrateBundleName() { return }" in s, "迁移要在建窗口之前跑"


def test_mac_menu_bar_copy():
    s = read("mac/main.swift")
    assert "CodeBoss 开发机" not in s, "菜单栏/窗口标题还是旧名字"
    assert 'b.toolTip = "BossHost 工头主机"' in s
    assert 'w.title = "BossHost 工头主机"' in s
    assert "安装 BossHost 服务" in s
    assert "安装 CodeBoss 服务" not in s


def test_mac_qr_hint_follows_product_line():
    """一套 host 服务三条线：二维码旁不能写死「口袋工头」，否则 agentboss/deskboss 看到就是错的。"""
    s = read("mac/main.swift")
    assert "手机：口袋工头 App →" not in s, "二维码提示写死了单产品"
    assert 'case "codeboss": return "口袋工头"' in s
    assert 'case "agentboss": return "代理工头"' in s
    assert 'default: return "工头"' in s, "认不得的产品线（含以后的 deskboss）要说通用「工头」"
    assert '手机：\\(Cfg.appName()) App →「接入开发机」→「📷 扫码接入」' in s


def test_branding_app_label():
    import sys
    sys.path.insert(0, str(ROOT / "shared"))
    import branding
    assert branding.app_label(branding.PRODUCT_CODEBOSS) == "口袋工头"
    assert branding.app_label(branding.PRODUCT_AGENTBOSS) == "代理工头"
    assert branding.app_label("deskboss") == "工头", "未来端线没登记的 app 名，别冒充口袋工头"
    assert branding.app_label(None) == "工头"


def test_windows_tray_copy():
    s = read("tray.py")
    assert 'BRAND = "BossHost 工头主机"' in s
    assert "安装 CodeBoss 服务" not in s
    assert "手机 CodeBoss App" not in s
    assert "手机 口袋工头 App" not in s, "托盘二维码提示也要按产品线走"
    assert "branding.app_label(" in s
    assert "BossHost-Setup.exe" in s


def test_windows_installer_and_shortcuts():
    s = read("win/codeboss.nsi")
    assert '!define APPDISPLAY "BossHost 工头主机"' in s
    s = read("install.ps1")
    assert "CodeBoss 开发机" not in s
    assert "手机打开 口袋工头 App" not in s, "安装时还不知道产品线，别指定单端"
    assert "手机打开工头 App（口袋工头 / 代理工头）" in s
    # 快捷方式也叫 BossHost；老安装的 CodeBoss.lnk 装新版时清掉，别出现两个图标
    assert 'Join-Path $startup "BossHost.lnk"' in s
    assert 'Join-Path $startup "CodeBoss.lnk"' in s, "升级时要把老快捷方式清掉"
    s = read("uninstall.ps1")
    assert '"BossHost.lnk", "CodeBoss.lnk"' in s, "老快捷方式卸载时也要能清"
    s = read("win/build-installer.sh")
    assert "BossHost-Setup.exe" in s


def test_relay_serves_bosshost_names_and_keeps_old_urls():
    s = read("relay.py")
    assert 'app.router.add_get("/BossHost-Setup.exe", h_win_exe)' in s
    assert 'app.router.add_get("/bosshost-mac.dmg", h_mac_dmg)' in s
    assert 'app.router.add_get("/CodeBoss-Setup.exe", h_win_exe)' in s, "老链接不能 404"
    assert 'app.router.add_get("/codeboss-mac.dmg", h_mac_dmg)' in s, "老链接不能 404"
    assert '"url": "/bosshost-mac.dmg"' in s, "mac_ver 要给出新下载地址"
    assert '"BossHost-Setup.exe"' in s, "上架接口要认新安装包名"
    assert '"bosshost-mac.dmg"' in s, "OSS 镜像要用新 key"


def test_guide_speaks_bosshost_and_uses_new_file_names():
    s = read("安装与使用说明.md")
    assert "CodeBoss 开发机" not in s
    assert "安装 CodeBoss 服务" not in s
    assert "手机打开 CodeBoss App" not in s
    assert "BossHost 工头主机" in s
    assert "BossHost.app" in s
    assert "BossHost-Setup.exe" in s
    assert "CodeBoss.app" not in s
    assert "CodeBoss-Setup.exe" not in s


def test_linux_installer_copy():
    s = read("install-linux.sh")
    assert "手机打开 CodeBoss App" not in s
    assert "手机打开工头 App（口袋工头 / 代理工头）" in s


def test_linux_cli_copy():
    s = read("codeboss-cli")
    assert "CodeBoss" not in s, "命令行里看到的还是老品牌"
    assert "BossHost 工头主机" in s
    assert "手机打开工头 App（口袋工头 / 代理工头）" in s


def test_guide_qr_hint_is_product_neutral():
    s = read("安装与使用说明.md")
    assert "手机打开 口袋工头 App" not in s
    assert "手机打开工头 App（口袋工头 / 代理工头）" in s
    s = read("mac/build-app.sh")
    assert "手机打开 口袋工头 App" not in s
    assert "手机打开工头 App（口袋工头 / 代理工头）" in s


# ---------- 自建中继 caps 透传（2026-11-23） ----------

def _server():
    import sys
    sys.path.insert(0, str(ROOT / "host"))
    import server
    return server


def _brand_json():
    """直调 h_brand 取应答 JSON（不走 HTTP；fake request 只带它实际读的属性）。"""
    import asyncio
    import json
    server = _server()

    class _Req:
        query = {}
        headers = {}
        cookies = {}

    return json.loads(asyncio.run(server.h_brand(_Req())).text)


def test_brand_caps_passthrough(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "_relay_caps",
                        lambda: {"selfhost": True, "asr": False, "tts": False})
    assert _brand_json()["caps"] == {"selfhost": True, "asr": False, "tts": False}


def test_brand_caps_fail_open(monkeypatch):
    server = _server()
    # 真实失败路径：中继没这端点/断网 → _relay_caps 返回 None（不抛）→ brand 不带键
    import urllib.request as _u

    def _boom(*a, **k):
        raise OSError("relay down")
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": "http://127.0.0.1:1"})
    monkeypatch.setattr(_u, "urlopen", _boom)
    server._CAPS_CACHE["ts"], server._CAPS_CACHE["val"] = 0.0, None
    assert server._relay_caps() is None
    assert "caps" not in _brand_json()      # 不带键 = 页面全功能（Review Focus 3）


def test_caps_negative_cache(monkeypatch):
    # 终审 #6：拉 caps 失败要有短负缓存——中继黑洞化时不能每次 brand 都真打一发
    server = _server()
    import urllib.request as _u
    calls = []

    def _boom(*a, **k):
        calls.append(1)
        raise OSError("relay down")
    monkeypatch.setattr(server, "_relay_cfg", lambda: {"url": "http://127.0.0.1:1"})
    monkeypatch.setattr(_u, "urlopen", _boom)
    server._CAPS_CACHE["ts"], server._CAPS_CACHE["val"] = 0.0, None
    assert server._relay_caps() is None
    assert server._relay_caps() is None
    assert len(calls) == 1, "失败没落负缓存：每次 brand 都会阻塞 3 秒"


def test_brand_caps_off_thread():
    # 终审 #6：_relay_caps 是同步 urlopen(timeout=3)，不能在事件循环上直调
    # （会卡住同机所有 SSE/WS——仓内纪律见 update_check 的「丢进线程池」注释）
    import inspect
    server = _server()
    assert "run_in_executor" in inspect.getsource(server.h_brand)
