# codeboss 卸载（Windows）
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#   ... -Purge       连 token/日志/数据库一起删（默认保留，重装后仍是同一台设备）
#
# 卸载后手机端的设备记录还在中继上；要彻底删掉，在手机「接入开发机」页里删除它。

param([switch]$Purge)

$ErrorActionPreference = "Continue"
$HERE = if ($env:CB_HOME) { $env:CB_HOME } else { Join-Path $env:LOCALAPPDATA "Codeboss" }
$TASK = "Codeboss"

Write-Host "== codeboss 卸载（Windows） =="

try {
    Stop-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
} catch { }
try {
    Unregister-ScheduledTask -TaskName $TASK -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "✓ 已移除计划任务（$TASK）"
} catch {
    Write-Host "! 移除计划任务失败：$($_.Exception.Message)"
}

# 收掉可能还在跑的 server.py（只认安装目录里的那个）
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine -like "*$HERE*server.py*") {
        try { Stop-Process -Id $_.ProcessId -Force; Write-Host "✓ 已停止进程 $($_.ProcessId)" } catch { }
    }
}

# 托盘：停进程 + 删启动项
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine -like "*tray.py*") {
        try { Stop-Process -Id $_.ProcessId -Force; Write-Host "✓ 已停止托盘 $($_.ProcessId)" } catch { }
    }
}
try {
    foreach ($n in @("BossHost.lnk", "CodeBoss.lnk")) {
        $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) $n
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "✓ 已移除托盘启动项" }
    }
    foreach ($n in @("BossHost 服务.lnk", "CodeBoss 服务.lnk")) {
        $lnk0 = Join-Path ([Environment]::GetFolderPath("Startup")) $n
        if (Test-Path $lnk0) { Remove-Item $lnk0 -Force; Write-Host "✓ 已移除服务启动项" }
    }
} catch { }

# 开始菜单 / 桌面快捷方式（安装时建的）也一起收掉，否则卸载后双击图标又弹出
# 一个「未安装服务」的窗口，用户会以为没卸载成功
try {
    $removed = 0
    foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
        if (-not $dir) { continue }
        foreach ($n in @("BossHost.lnk", "CodeBoss.lnk")) {
            $p = Join-Path $dir $n
            if (Test-Path $p) { Remove-Item $p -Force; $removed++ }
        }
    }
    if ($removed -gt 0) { Write-Host "✓ 已移除开始菜单/桌面快捷方式" }
} catch { }

if ($Purge) {
    if (Test-Path $HERE) {
        try { Remove-Item $HERE -Recurse -Force; Write-Host "✓ 已删除 $HERE（含数据）" }
        catch { Write-Host "! 删除 $HERE 失败（文件被占用？注销后重试）：$($_.Exception.Message)" }
    }
} else {
    Write-Host "• 程序文件保留在 $HERE（加 -Purge 可连数据一起删）"
}

try {
    Remove-NetFirewallRule -DisplayName "Codeboss host 8790" -ErrorAction SilentlyContinue | Out-Null
} catch { }

Write-Host ""
if ($Purge) {
    Write-Host "✓ 卸载完成（服务和数据都已删除）。"
} else {
    Write-Host "✓ 卸载完成（服务和自启已移除，token/会话记录/配置保留在 $HERE）。"
    Write-Host "  重装：运行安装包即可，仍是同一台设备、手机不用重新配对。"
}
Write-Host "• 手机端「接入开发机」里的这台机器记录还在，想删掉可在手机里删除。"
Write-Host "完成。要重新安装：再跑 install.ps1"
