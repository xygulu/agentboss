# codeboss 一键部署（Windows，幂等：重复运行 = 原地升级）
# 依赖 → 虚拟环境 → 计划任务（登录自启 + 崩溃重启）→ 中继登记 → 二维码
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Relay https://relay.example.com
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoEnroll      # 只装，不登记
#   $env:CB_HOME = "D:\Codeboss"   # 自定义安装目录（默认 %LOCALAPPDATA%\Codeboss）
#
# 不需要管理员权限。程序装到用户目录，自启用「计划任务（登录时）」。

param(
    [string]$Relay = "https://relay.example.com",
    [switch]$NoEnroll,
    [switch]$StopOnly          # 只停服务/托盘就退出（NSIS 覆盖文件前调用，见 codeboss.nsi）
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$SRC = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:CB_HOME) {
    $HERE = $env:CB_HOME
} else {
    $HERE = Join-Path $env:LOCALAPPDATA "Codeboss"
}
$TASK = "Codeboss"
# 第一次装（之前没有 config.json）：装完直接把配对二维码窗口摆出来。
# 安装器的详情窗口装完就关，控制台里那串 ASCII 二维码根本来不及扫。
$firstRun = -not (Test-Path (Join-Path $HERE "config.json"))

function Say($m) { Write-Host $m }
function Save-Error($m) {
    try {
        if (Test-Path $HERE) { Set-Content -Path (Join-Path $HERE "install.error.txt") -Value $m -Encoding UTF8 }
    } catch { }
}
function Die($m) { Write-Host "✗ $m" -ForegroundColor Red; Save-Error $m; exit 3 }

function Get-OurProcesses {
    # 本安装目录里的服务/托盘进程。按**可执行文件绝对路径**判断：run-server.cmd 里
    # 写的是相对路径（".venv\Scripts\python.exe" server.py），命令行里查不到安装目录，
    # 只按 CommandLine -like "*$HERE*" 会漏掉它们（升级时文件被占用、卸载后服务还在跑）。
    $list = @()
    # 补上结尾的 "\"：否则 D:\Codeboss 会误匹配到隔壁的 D:\Codeboss-old
    $hereN = $HERE.TrimEnd('\', '/') + '\'
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = "" + $_.ExecutablePath
            $cmd = "" + $_.CommandLine
            $mine = ($exe -and $exe.StartsWith($hereN, [StringComparison]::OrdinalIgnoreCase)) -or
                    ($cmd -and $cmd.IndexOf($HERE, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            if ($mine -and $_.ProcessId -ne $PID) { $list += $_ }
        }
    } catch { }
    return $list
}

function Stop-Ours([switch]$KeepMainWindow) {
    # 停计划任务 + 本目录里的进程。KeepMainWindow：安装/升级时留着「主窗口」进程
    # （tray.py --main）——用户可能正从这个窗口点「重新安装」，杀了它窗口会中途消失。
    try { Stop-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue } catch { }
    try { & schtasks.exe /End /TN $TASK 2>$null | Out-Null } catch { }
    foreach ($p in (Get-OurProcesses)) {
        if ($KeepMainWindow -and ("" + $p.CommandLine) -like "*--main*") { continue }
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Milliseconds 500
}

if ($StopOnly) {
    # NSIS 覆盖安装时先调这个：自带 Python 正跑在 $HERE 里，文件被锁着，
    # 不停掉就会弹「Error opening file for writing」把安装中断在半更新状态。
    Stop-Ours
    exit 0
}

function Invoke-Native {
    # 跑原生程序：stdout/stderr 先落文件，再由本脚本按 UTF-8 读回来显示。
    # 三个原因：① 安装依赖时有实时进度（秒数 + pip 当前在做什么）；
    # ② PowerShell 5.1 直接解码原生输出时，碰到二维码的半块字符（▀▄█）会抛
    #    「索引超出了数组界限」，把安装打断；③ 5.1 的 Start-Process 对象在轮询过
    #    HasExited 之后读不到 ExitCode（真机实测恒为 $null，会把成功当失败），
    #    所以让 cmd 把退出码写进文件，不依赖 $p.ExitCode。
    param([string]$Exe, [string[]]$ArgList, [string]$Log, [string]$WorkDir,
          [switch]$Quiet, [switch]$Show)
    $tmpO = [IO.Path]::GetTempFileName()
    $tmpE = [IO.Path]::GetTempFileName()
    $tmpC = [IO.Path]::GetTempFileName()
    $tmpB = [IO.Path]::GetTempFileName() + ".cmd"
    $argLine = ($ArgList | ForEach-Object {
        if ($_ -match '[\s&|<>^]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
    # 用 .cmd 包一层：重定向交给 cmd 自己做，退出码也由它落盘
    $body = "@echo off`r`n`"$Exe`" $argLine > `"$tmpO`" 2> `"$tmpE`"`r`necho %ERRORLEVEL% > `"$tmpC`"`r`n"
    $startDir = $(if ($WorkDir) { $WorkDir } else { (Get-Location).Path })
    try {
        [IO.File]::WriteAllText($tmpB, $body, [Text.Encoding]::Default)
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList ('/c "' + $tmpB + '"') `
             -WorkingDirectory $startDir -NoNewWindow -PassThru
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while (-not $p.HasExited) {
            Start-Sleep -Milliseconds 600
            try { $p.Refresh() } catch { }
            if ($Quiet) { continue }
            $hint = ""
            try {
                $hint = Get-Content $tmpE -Tail 1 -Encoding UTF8 -ErrorAction SilentlyContinue
                if (-not $hint) {
                    $hint = Get-Content $tmpO -Tail 1 -Encoding UTF8 -ErrorAction SilentlyContinue
                }
            } catch { }
            $hint = ("" + $hint)
            # pip 的进度条用 \r 反复刷同一行：只留最后一段
            $parts = @($hint.Split([char]13))
            $hint = $parts[$parts.Count - 1].Trim()
            if ($hint.Length -gt 64) { $hint = $hint.Substring(0, 64) }
            Write-Host ("`r  {0,4}s  {1}" -f [int]$sw.Elapsed.TotalSeconds, $hint) -NoNewline
        }
        $p.WaitForExit()
        if (-not $Quiet) { Write-Host ("`r  {0,4}s  " -f [int]$sw.Elapsed.TotalSeconds) -NoNewline; Write-Host "" }
        $code = -1
        try {
            $c = @(Get-Content $tmpC -ErrorAction Stop)[0]
            if ($null -ne $c) { $code = [int](("" + $c).Trim()) }
        } catch { }
        if ($Show) {
            Get-Content $tmpO -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
            Get-Content $tmpE -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        }
        if ($Log) {
            try {
                Get-Content $tmpO -Encoding UTF8 -ErrorAction SilentlyContinue | Out-File -FilePath $Log -Append -Encoding UTF8
                Get-Content $tmpE -Encoding UTF8 -ErrorAction SilentlyContinue | Out-File -FilePath $Log -Append -Encoding UTF8
            } catch { }
        }
        return $code
    } finally {
        Remove-Item $tmpO, $tmpE, $tmpC, $tmpB -ErrorAction SilentlyContinue
    }
}

# 图形安装包没有控制台：把出错位置也写进文件，安装器会把它显示出来
trap {
    $m = "✗ 安装中断：$($_.Exception.Message)"
    try { $m += "`r`n位置：$($_.InvocationInfo.PositionMessage)" } catch { }
    Say $m
    Save-Error $m
    exit 1
}

try {
    $logDir = $HERE
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    Start-Transcript -Path (Join-Path $logDir "install.log") -Append -Force | Out-Null
} catch { }

Remove-Item (Join-Path $HERE "install.error.txt") -ErrorAction SilentlyContinue | Out-Null
Say "== codeboss 部署（Windows） =="

# 原生程序的输出一律按 UTF-8 写/读：Python 打的二维码是 ▀▄█ 半块字符，
# 走系统代码页（936）会乱码；PowerShell 5.1 直接解码原生 stdout 时碰到
# 这类多字节字符还会抛「索引超出了数组界限」，把安装打断（用户实测过）。
$env:PYTHONIOENCODING = "utf-8"

# 升级时先停掉在跑的服务：.venv/python 里的解释器和 server.py 被占用就删不掉/覆盖不了。
# 停完接着往下装，最后再启回来（数据都保留）。主窗口进程留着（见 Stop-Ours）。
try { Stop-Ours -KeepMainWindow } catch { Say "! 停旧进程没成功（$($_.Exception.Message)），继续安装" }
Start-Sleep -Milliseconds 300

# ---------------------------------------------------------------- 1) 选解释器
# 先读 PYC_VER 找匹配的解释器；没有这个文件时任意 Python >= 3.9 都行。
$PYC_VER = ""
$pycv = Join-Path $SRC "PYC_VER"
if (Test-Path $pycv) { $PYC_VER = (Get-Content $pycv -TotalCount 1).Trim() }

function PyVersion($exe) {
    try {
        $v = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) { return "$v".Trim() }
    } catch { }
    return ""
}

function Probe-Mirror($u) {
    # 探一下这个源通不通、多快（毫秒）；不通返回 -1。只发 HEAD，不下载内容
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $req = [Net.HttpWebRequest]::Create($u)
        $req.Method = "HEAD"
        $req.Timeout = 4000
        $req.ReadWriteTimeout = 4000
        $req.UserAgent = "codeboss"
        $resp = $req.GetResponse()
        $resp.Close()
        $sw.Stop()
        return [int]$sw.ElapsedMilliseconds
    } catch {
        return -1
    }
}

function Sort-Mirrors($urls) {
    # 自动选源：先测速，快的排前面；探不通的照样排在最后再试一遍
    # （探测被拦、下载却可以的情况是有的，不能因为探测失败就放弃某个源）
    # 探测结果不再逐条打印：用户只关心「装得顺不顺」，
    # 自动选源的细节留在注释和 pip 日志里就够了。
    $ok = @()
    $bad = @()
    foreach ($u in $urls) {
        $ms = Probe-Mirror $u
        Write-Host "." -NoNewline   # 每个源一个点：告诉用户"在测"，不出细节
        if ($ms -ge 0) {
            $ok += [pscustomobject]@{ u = $u; ms = $ms }
        } else {
            $bad += $u
        }
    }
    Write-Host ""
    $sorted = @($ok | Sort-Object ms | ForEach-Object { $_.u })
    return @($sorted + $bad)
}

function Find-Python {
    $want = $PYC_VER
    # 安装包自带的解释器优先：装完不受机器环境影响，也不需要联网装 Python
    if ($want) {
        $bp = Join-Path $HERE "python\python.exe"
        if (Test-Path $bp) {
            $bv = PyVersion $bp
            if ($bv -eq $want) { return @{ exe = $bp; args = @() } }
        }
    }
    $cands = @()
    if ($want) {
        $cands += "python$want"
        $cands += "python" + ($want -replace "\.", "")
        if (Get-Command py -ErrorAction SilentlyContinue) {
            # py -3.11 -c ... 这种带参数的形式单独处理
            try {
                $v = & py "-$want" -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and "$v".Trim() -eq $want) {
                    $launcher = (Get-Command py).Source
                    return @{ exe = $launcher; args = @("-$want") }
                }
            } catch { }
        }
    }
    $cands += "python"
    $cands += "python3"
    foreach ($c in $cands) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $v = PyVersion $cmd.Source
        if (-not $v) { continue }
        if ($want) {
            if ($v -eq $want) { return @{ exe = $cmd.Source; args = @() } }
        } elseif ([version]$v -ge [version]"3.9") {
            return @{ exe = $cmd.Source; args = @() }
        }
    }
    # PATH 里没有时再看官方安装包的标准落点：按用户装的在 LOCALAPPDATA，按机器装的在 Program Files
    if ($want) {
        $n = $want -replace "\.", ""
        $dirs = @("C:\Python$n")
        foreach ($base in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
            if ($base) { $dirs += (Join-Path $base "Programs\Python\Python$n"); $dirs += (Join-Path $base "Python$n") }
        }
        foreach ($d in $dirs) {
            $exe = Join-Path $d "python.exe"
            if (-not (Test-Path $exe)) { continue }
            $v = PyVersion $exe
            if ($v -eq $want) { return @{ exe = $exe; args = @() } }
        }
    }
    return $null
}

$PY = Find-Python
if (-not $PY) {
    Say "· 没找到 Python$(if ($PYC_VER) { " $PYC_VER" })，自动装一个（不用你操作）…"
    $ver = if ($PYC_VER) { $PYC_VER } else { "3.12" }

    # 1) uv：独立解释器，不动系统
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        $uvpath = Join-Path $env:LOCALAPPDATA "uv\uv.exe"
        if (Test-Path $uvpath) { $uv = @{ Source = $uvpath } }
    }
    if ($uv) {
        foreach ($m in @("https://registry.npmmirror.com/-/binary/python-build-standalone", "", "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download")) {
            try {
                if ($m) {
                    $env:UV_PYTHON_INSTALL_MIRROR = $m
                    & $uv.Source python install $ver 2>$null | Out-Null
                    Remove-Item Env:UV_PYTHON_INSTALL_MIRROR -ErrorAction SilentlyContinue
                } else {
                    & $uv.Source python install $ver 2>$null | Out-Null
                }
                $found = (& $uv.Source python find $ver 2>$null)
                if ($LASTEXITCODE -eq 0 -and $found) { $PY = @{ exe = "$found".Trim(); args = @() }; break }
            } catch { }
        }
    }

    # 2) winget 有就直接装官方 Python（Win10 不一定带 winget，装不上就走下一步）
    if (-not $PY -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Say "· 用 winget 装 Python $ver（要一两分钟）…"
        try { winget install -e --id "Python.Python.$ver" --silent --accept-package-agreements --accept-source-agreements *>$null } catch { }
        $PY = Find-Python
    }

    # 3) 兜底：官方安装包静默装（按用户装，不需要管理员、不改系统 PATH；镜像优先，官网兜底）
    if (-not $PY -and $PYC_VER) {
        $tag = if ($PYC_VER -eq "3.11") { "3.11.9" } else { "$PYC_VER.9" }
        $sum = if ($PYC_VER -eq "3.11") { "5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde" } else { "" }
        $kept = $false
        $urls = @(
            "https://cdn.npmmirror.com/binaries/python/$tag/python-$tag-amd64.exe",
            "https://mirrors.huaweicloud.com/python/$tag/python-$tag-amd64.exe",
            "https://www.python.org/ftp/python/$tag/python-$tag-amd64.exe")
        foreach ($u in (Sort-Mirrors $urls)) {
            $exe = Join-Path $env:TEMP "codeboss-python-$tag.exe"
            Say "· 下载 Python $tag …"
            try {
                if (Test-Path $exe) { Remove-Item $exe -Force -ErrorAction SilentlyContinue }
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $u -OutFile $exe -UseBasicParsing -TimeoutSec 600
                if ($sum) {
                    $got = (Get-FileHash -Path $exe -Algorithm SHA256).Hash.ToLower()
                    if ($got -ne $sum) {
                        Say "! 下载的安装包校验不过，换个源再试"
                        Remove-Item $exe -Force -ErrorAction SilentlyContinue
                        continue
                    }
                }
                $kept = $true
                break
            } catch { Say "! 下载失败：$($_.Exception.Message)" }
        }
        if ($kept) {
            $exe = Join-Path $env:TEMP "codeboss-python-$tag.exe"
            Say "· 静默安装 Python $tag（装在当前用户下）…"
            try {
                Start-Process -FilePath $exe -Wait -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 Include_test=0 Shortcuts=0"
            } catch { Say "! Python 安装器报错：$($_.Exception.Message)" }
            Remove-Item $exe -Force -ErrorAction SilentlyContinue
            $PY = Find-Python
        }
    }
}
if (-not $PY) {
    Die "装不上 Python$(if ($PYC_VER) { " $PYC_VER" })，自动安装也没成功（多半是没联网）。连上网重跑一次安装包即可；要手动装的话去 python.org 下 Python $(if ($PYC_VER) { $PYC_VER } else { '3.12' })，安装时勾上 Add python.exe to PATH。"
}
Say "✓ Python$(if ($PYC_VER) { " $PYC_VER" }): $($PY.exe) $($PY.args -join ' ')"

function Invoke-Py {
    param([string[]]$PyArgs)
    if ($PY.args.Count -gt 0) {
        & $PY.exe @($PY.args + $PyArgs)
    } else {
        & $PY.exe @PyArgs
    }
}

# ---------------------------------------------------------------- 2) 装程序文件
New-Item -ItemType Directory -Force -Path $HERE | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $HERE "logs") | Out-Null
try { $srcFull = (Resolve-Path $SRC).Path } catch { $srcFull = $SRC }
try { $hereFull = (Resolve-Path $HERE).Path } catch { $hereFull = $HERE }
if ($srcFull -ne $hereFull) {
    $files = @("server.py", "wrapper.py", "relay.py", "plat.py", "pty_backend.py", "agents.py", "terms.py",
               "agent_codex.py",
               "tray.py", "ptywin.py", "requirements.txt", "install.ps1", "uninstall.ps1", "install.sh",
               "install-linux.sh", "uninstall.sh", "uninstall-linux.sh", "dist.sh",
               "codeboss-cli", "VERSION", "PYC_VER", "README.md", "安装与使用说明.md")
    try {
        foreach ($f in $files) {
            $p = Join-Path $SRC $f
            if (Test-Path $p) { Copy-Item $p (Join-Path $HERE $f) -Force }
        }
        Get-ChildItem -Path $SRC -Filter "*.pyc" -File | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $HERE $_.Name) -Force
            # 同名旧 .py 会压过新 .pyc（Python 优先源码）：老 branding.py 赖着不走，
            # 机器「更新成功」却一直跑老代码。入口薄壳 server.py/wrapper.py 除外。
            if ($_.BaseName -ne "server" -and $_.BaseName -ne "wrapper") {
                $stalePy = Join-Path $HERE ($_.BaseName + ".py")
                if (Test-Path $stalePy) { Remove-Item $stalePy -Force }
            }
        }
        # pet 是包（目录），*-.pyc 的通配符拷不到它：漏了服务起不来（2026-11-21 Linux 那台）
        $petSrc = Join-Path $SRC "pet"
        if (Test-Path $petSrc) {
            $petDst = Join-Path $HERE "pet"
            if (Test-Path $petDst) { Remove-Item $petDst -Recurse -Force }
            Copy-Item $petSrc $petDst -Recurse -Force
        }
        # 目录要按「拷内容」处理：目标已存在时 Copy-Item 会把整个源目录塞进去（变成 static\static）
        if (Test-Path (Join-Path $SRC "static")) {
            $sd = Join-Path $HERE "static"
            New-Item -ItemType Directory -Force -Path $sd | Out-Null
            Copy-Item (Join-Path $SRC "static\*") $sd -Recurse -Force
        }
        # 自带解释器：解压后双击 安装.cmd 时也要一起搬过去
        if (Test-Path (Join-Path $SRC "python")) {
            $pd = Join-Path $HERE "python"
            New-Item -ItemType Directory -Force -Path $pd | Out-Null
            Copy-Item (Join-Path $SRC "python\*") $pd -Recurse -Force
        }
    } catch {
        Die "复制程序文件失败：$($_.Exception.Message)。旧的托盘/服务可能还在跑，注销重登后重跑本安装包。"
    }
    Say "✓ 程序安装到 $HERE（数据保留，安装包可删）"
} else {
    Say "✓ 已在安装目录内运行，跳过文件复制（原地升级）"
}

# ---------------------------------------------------------------- 3) 解释器 + 依赖
# PowerShell 5.1 的坑：$ErrorActionPreference='Stop' 时，原生程序往 stderr 写一行
# （pip 的 "scripts ... not on PATH" 警告之类）会被包成 ErrorRecord 直接当致命错误，
# 安装就在这一行"中断"，其实 pip 一切都正常。这一段全程只看 $LASTEXITCODE，
# 先把 EAP 放成 Continue，依赖装完再恢复。
$EAP_SAVED = $ErrorActionPreference
$ErrorActionPreference = "Continue"
# 自带 Python 的 Scripts 也放进 PATH：pip 装 console script 时就不会再打
# "scripts ... are not on PATH" 的警告（顺带让 python/pip 命令在安装目录里可用）
$env:PATH = "$(Join-Path $HERE 'python\Scripts');$(Join-Path $HERE 'python');$env:PATH"
$BUNDLED = Join-Path $HERE "python\python.exe"
$VPY = Join-Path $HERE ".venv\Scripts\python.exe"
$RUNREL = ".venv\Scripts\python.exe"
$TRAYREL = ".venv\Scripts\pythonw.exe"
if ((Test-Path $BUNDLED) -and ("$($PY.exe)" -eq "$BUNDLED")) {
    $VPY = $BUNDLED
    $RUNREL = "python\python.exe"
    $TRAYREL = "python\pythonw.exe"
    Say "✓ 用安装包自带的 Python（不受机器环境影响）"
} else {
    $needVenv = $true
    if (Test-Path $VPY) {
        $vv = PyVersion $VPY
        if ($vv -and (-not $PYC_VER -or $vv -eq $PYC_VER)) {
            $needVenv = $false
            Say "✓ 复用已有虚拟环境（$HERE\.venv）"
        }
    }
    if ($needVenv) {
        try {
            if (Test-Path (Join-Path $HERE ".venv")) { Remove-Item (Join-Path $HERE ".venv") -Recurse -Force }
        } catch {
            Die "旧环境删不掉（$($_.Exception.Message)）：上面可能还有进程在跑。注销重登后再重跑本安装包。"
        }
        Invoke-Py @("-m", "venv", (Join-Path $HERE ".venv"))
        if (-not (Test-Path $VPY)) { Die "建不出虚拟环境（$HERE\.venv）" }
        Say "✓ venv 就绪（$HERE\.venv）"
    }
}

function Pip-Install {
    param([string[]]$PipArgs)
    # 装的第一次才测速，后面几次沿用同一个顺序
    if ($null -eq $script:PIP_SOURCES) {
        Say "→ 自动检测最快的下载镜像…"
        $script:PIP_SOURCES = Sort-Mirrors @("https://pypi.tuna.tsinghua.edu.cn/simple",
                                            "https://mirrors.aliyun.com/pypi/simple",
                                            "https://pypi.org/simple")
    }
    $sources = $script:PIP_SOURCES
    # pip 的输出不能白扔：装不上时，错在哪个包/哪个源全在这份日志里
    $plog = Join-Path $HERE "install-pip.log"
    foreach ($s in $sources) {
        $a = @("-m", "pip", "install", "--timeout", "8", "--retries", "1")
        if ($s) { $a += @("-i", $s) }
        $a += $PipArgs
        # 不加 -q：安装过程的行会被显示成实时进度（见 Invoke-Native）
        if ((Invoke-Native -Exe $VPY -ArgList $a -Log $plog) -eq 0) { return $true }
    }
    return $false
}

function Pip-Tail {
    # 出错时把 pip 日志最后几行带进错误信息，省得用户再翻日志
    try {
        $p = Join-Path $HERE "install-pip.log"
        if (Test-Path $p) { return ((Get-Content $p -Tail 4) -join " ") }
    } catch { }
    return ""
}

& $VPY -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { & $VPY -m ensurepip --upgrade 2>$null | Out-Null }

if (-not (Pip-Install @("--upgrade", "pip"))) { Say "! pip 升级失败，继续用现有版本" }

$needDeps = $true
& $VPY -c "import aiohttp, pyte, qrcode, winpty, pystray, PIL" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $needDeps = $false
    Say "✓ 依赖已就绪"
}
if ($needDeps) {
    Say "→ 正在安装依赖（首次约 1-2 分钟，自动选最快的镜像，请稍候）…"
    if (-not (Pip-Install @("--only-binary=av", "-r", (Join-Path $HERE "requirements.txt")))) {
        # aiortc 在部分 Python 版本上没有 Windows 轮子：核心依赖单独装，P2P 缺了不阻断
        Say "! 整包依赖装不上，退回逐个装核心依赖（P2P 可能不可用）"
        Pip-Install @("aiohttp>=3.9", "pyte==0.8.2", "qrcode>=7.4", "pywinpty>=2.0",
                      "pystray>=0.19", "Pillow>=9.0") | Out-Null
        Pip-Install @("--only-binary=av", "aiortc>=1.10") | Out-Null
    }
}

& $VPY -c "import aiohttp, pyte, qrcode, winpty" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Die "依赖装不上（需要联网）。Python：$(& $VPY -V 2>&1)。pip 最后几行：$(Pip-Tail)。连上网重跑一次安装包即可，pip 日志在 $HERE\install-pip.log。"
}
& $VPY -c "import pystray, PIL" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Say "! 托盘依赖（pystray/Pillow）没装上：不影响服务，但没有托盘图标" }
& $VPY -c "import aiortc" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Say "! 没有 aiortc：这台机器的广域网 P2P 关闭，只走局域网直连/中继（不影响其它功能）" }

$ErrorActionPreference = $EAP_SAVED   # 依赖装完，恢复严格模式（后面文件操作要能中断）

# 解释器小版本必须和程序一致，否则服务起不来
$implPyc = Join-Path $HERE "server_impl.pyc"
if (Test-Path $implPyc) {
    & $VPY -c "import sys,importlib.util;m=open(sys.argv[1],'rb').read(4);sys.exit(0 if m==importlib.util.MAGIC_NUMBER else 1)" $implPyc 2>$null
    if ($LASTEXITCODE -ne 0) {
        Die "安装包的 .pyc 需要 Python $PYC_VER，但虚拟环境是 $(& $VPY -V 2>&1)。装好对应 Python 后重跑本脚本。"
    }
}

# ---------------------------------------------------------------- 4) 启动脚本 + 计划任务
$runner = Join-Path $HERE "run-server.cmd"
$vbs = Join-Path $HERE "run-server.vbs"
try {
    Set-Content -Path $runner -Encoding Default -Value @"
@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
rem 用户主动「停止服务」会留下 logs\.stopped：别让崩溃自重启循环又把它拉起来
if exist logs\.stopped exit /b 0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"$RUNREL" server.py >> logs\server.log 2>&1
exit /b %ERRORLEVEL%
"@
    # vbs 写成 UTF-16（Unicode）：路径里有当前代码页表示不了的字符时（英文版 Windows +
    # 中文用户名），ANSI 写会把 $HERE 变成 "?"，每次登录 wscript 弹「找不到路径」。
    # WSH 认 BOM，Unicode 脚本文件照常执行。
    Set-Content -Path $vbs -Encoding Unicode -Value @"
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$HERE"
If Not fso.FolderExists("$HERE\logs") Then fso.CreateFolder("$HERE\logs")
' 循环守护标记：服务进程看到它就知道自己"有人看着"，重启时直接退出（码 75）交给本脚本重拉
fso.CreateTextFile("$HERE\logs\.supervised", True).Close
Do
  If fso.FileExists("$HERE\logs\.stopped") Then Exit Do
  rc = sh.Run("cmd /c ""$(Join-Path $HERE 'run-server.cmd')""", 0, True)
  If rc = 0 Then Exit Do
  WScript.Sleep 5000
Loop
On Error Resume Next
fso.DeleteFile "$HERE\logs\.supervised", True
"@
} catch {
    Die "写启动脚本失败：$($_.Exception.Message)"
}

$taskOk = $true
try {
    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TASK -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
} catch {
    $taskOk = $false
    Say "! 计划任务没登记上（$($_.Exception.Message)），改用「启动」文件夹自启"
}
if ($taskOk) {
    # 以前计划任务注册失败时建过「CodeBoss 服务.lnk」兜底；现在任务正常了，
    # 兜底项留着会变成两套守护同时抢 8790（每 5 秒互刷），清掉
    try {
        $old = Join-Path ([Environment]::GetFolderPath("Startup")) "CodeBoss 服务.lnk"
        if (Test-Path $old) { Remove-Item $old -Force }
    } catch { }
    Say "✓ 计划任务已就绪（$TASK，登录自启，崩溃 1 分钟后重启）"
} else {
    # 兜底：放到启动文件夹，效果一样（登录时拉起服务），只是没有崩溃自动重启
    try {
        $startup0 = [Environment]::GetFolderPath("Startup")
        $ws0 = New-Object -ComObject WScript.Shell
        $old0 = Join-Path $startup0 "CodeBoss 服务.lnk"
        if (Test-Path $old0) { Remove-Item $old0 -Force }
        $lnk0 = $ws0.CreateShortcut((Join-Path $startup0 "BossHost 服务.lnk"))
        $lnk0.TargetPath = "wscript.exe"
        $lnk0.Arguments = "`"$vbs`""
        $lnk0.WorkingDirectory = $HERE
        $lnk0.Description = "BossHost 工头主机服务"
        $lnk0.Save()
        Say "✓ 已加入「启动」文件夹（登录后自动启动服务）"
    } catch {
        Say "! 自启也没建成：$($_.Exception.Message)（服务可以手动开：双击 $HERE\run-server.vbs）"
    }
}

# ---------------------------------------------------------------- 4b) 托盘客户端（登录自启的快捷方式）
# 生成快捷方式图标
$ico = Join-Path $HERE "codeboss.ico"
try {
    & $VPY -c "import sys; sys.path.insert(0, sys.argv[1]); import tray; tray._icon_image().save(sys.argv[2], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" $HERE $ico 2>$null | Out-Null
} catch { }
try {
    $startup = [Environment]::GetFolderPath("Startup")
    $oldLnk = Join-Path $startup "CodeBoss.lnk"
    if (Test-Path $oldLnk) { Remove-Item $oldLnk -Force }
    $lnkPath = Join-Path $startup "BossHost.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = Join-Path $HERE $TRAYREL
    $lnk.Arguments = "`"$(Join-Path $HERE 'tray.py')`""
    $lnk.WorkingDirectory = $HERE
    $lnk.Description = "BossHost 工头主机（托盘）"
    if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
    $lnk.Save()
    Say "✓ 托盘已加入启动项（登录后自动出现，右键图标就是菜单）"
} catch {
    Say "! 托盘启动项没建成（不影响服务）：$($_.Exception.Message)"
}

# 开始菜单 + 桌面：双击就是「打开主窗口」（托盘已经在跑也不会叠出第二个图标）。
# 以前只往「启动」文件夹放快捷方式，用户在开始菜单和桌面上什么都看不到。
try {
    $ws2 = New-Object -ComObject WScript.Shell
    $shown = 0
    foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
        if (-not $dir) { continue }
        $p = Join-Path $dir "BossHost.lnk"
        $oldP = Join-Path $dir "CodeBoss.lnk"
        if (Test-Path $oldP) { Remove-Item $oldP -Force }
        $l = $ws2.CreateShortcut($p)
        $l.TargetPath = Join-Path $HERE $TRAYREL
        $l.Arguments = "`"$(Join-Path $HERE 'tray.py')`" --main"
        $l.WorkingDirectory = $HERE
        $l.Description = "BossHost 工头主机"
        if (Test-Path $ico) { $l.IconLocation = "$ico,0" }
        $l.Save()
        $shown++
    }
    if ($shown -gt 0) { Say "✓ 开始菜单/桌面已建快捷方式（双击打开主窗口）" }
} catch {
    Say "! 开始菜单/桌面快捷方式没建成（不影响使用）：$($_.Exception.Message)"
}

# 防火墙：LAN 直连要能被手机连 8790。加规则需要管理员，失败不阻断（中继照常可用）
try {
    if (-not (Get-NetFirewallRule -DisplayName "Codeboss host 8790" -ErrorAction SilentlyContinue)) {
        # 8790 被别的程序占用时 host 会顺延换端口（8791…8800），一条规则把整段放开
        New-NetFirewallRule -DisplayName "Codeboss host 8790" -Direction Inbound -Protocol TCP `
            -LocalPort "8790-8800" -Action Allow -Profile Private -ErrorAction Stop | Out-Null
        Say "✓ 防火墙已放行 8790-8800（专用网络）"
    }
} catch {
    Say "! 没加防火墙规则（需要管理员）。想用局域网直连：管理员 PowerShell 执行"
    Say "  New-NetFirewallRule -DisplayName 'Codeboss host 8790' -Direction Inbound -Protocol TCP -LocalPort 8790-8800 -Action Allow"
}

# 用户在托盘里点过「停止服务」会留下 logs\.stopped；重装/升级就是要把服务装好并跑起来，
# 先清掉这个标记，否则启动脚本看到它立刻退出，安装最后报「服务还没响应」（用户以为装坏了）
Remove-Item (Join-Path $HERE "logs\.stopped") -ErrorAction SilentlyContinue | Out-Null

if ($taskOk) {
    try { Start-ScheduledTask -TaskName $TASK } catch { Say "! 计划任务没能立刻启动：$($_.Exception.Message)（下次登录会自动跑）" }
} else {
    try { Start-Process -FilePath "wscript.exe" -ArgumentList "`"$vbs`"" -WorkingDirectory $HERE } catch { }
}
Start-Sleep -Seconds 3
# 端口以 host 实际监听为准（8790 被别的程序占用时它会换端口，见 logs\port.txt）
$HPORT = 8790
try {
    $pf = Join-Path $HERE "logs\port.txt"
    if (Test-Path $pf) { $n = (Get-Content $pf -Raw).Trim(); if ($n -match '^\d+$') { $HPORT = [int]$n } }
} catch { }
$alive = $false
try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$HPORT/api/ping"
    $alive = ($r.StatusCode -eq 200)
} catch { }
if ($alive) { Say "✓ 服务已启动（http://127.0.0.1:$HPORT）" }
else { Say "! 服务还没响应，看日志：$HERE\logs\server.log" }

# ---------------------------------------------------------------- 5) 中继登记 + 二维码
if (-not $NoEnroll) {
    Say ""
    Say "== 登记到中继（手机扫码配对） =="
    # 二维码是 ▀▄█ 半块字符：统一交给 Invoke-Native 落文件再按 UTF-8 读回来打印，
    # 不让 PowerShell 5.1 直接解码原生输出（会抛「索引超出了数组界限」）。
    $ea = @((Join-Path $HERE "server.py"), "--enroll", $Relay)
    if ($env:COMPUTERNAME) { $ea += $env:COMPUTERNAME }
    $rc = Invoke-Native -Exe $VPY -ArgList $ea -WorkDir $HERE -Quiet -Show
    if ($rc -ne 0) {
        Say "! 登记没成功（退出码 $rc）。装好后在托盘图标右键 →「扫这个码接上…」可以重试。"
    }
}

# 立刻把托盘拉起来（不用等下次登录）
if (Test-Path (Join-Path $HERE "tray.py")) {
    try {
        Start-Process -FilePath (Join-Path $HERE $TRAYREL) `
            -ArgumentList "`"$(Join-Path $HERE 'tray.py')`"" -WorkingDirectory $HERE
        Say "✓ 托盘已启动（右下角通知区图标：右键 = 菜单，双击 = 主窗口）"
        if ($firstRun -and -not $NoEnroll) {
            Start-Sleep -Seconds 1
            Start-Process -FilePath (Join-Path $HERE $TRAYREL) `
                -ArgumentList "`"$(Join-Path $HERE 'tray.py')`" --qr" -WorkingDirectory $HERE
            Say "✓ 已弹出配对二维码窗口（手机扫码就能接上）"
        }
    } catch { }
}

Say ""
Say "下一步：手机打开工头 App（口袋工头 / 代理工头） →「接入开发机」→「📷 扫码接入」，扫上面的二维码。"
Say "之后日常都从托盘图标操作；重装/升级只需再跑一次本脚本；卸载：uninstall.ps1"
try { Stop-Transcript | Out-Null } catch { }
exit 0
