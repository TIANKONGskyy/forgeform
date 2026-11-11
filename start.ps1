#requires -Version 5.1
<#
  ForgeForm launcher - double-click 启动.bat, or run this file directly.

  First run : create .venv, install deps, start the app, open the browser
  Next runs : just start the app and open the browser (takes ~10s)

  Notes:
    - All logging is ASCII-only on purpose: PS 5.1 reads UTF-8 without BOM as GBK
      and mangles Chinese comments into parser errors.
    - The app keeps running in this window. Close the window to stop it.
#>
param(
    [int]$Port = 7860,
    [switch]$Reinstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Btn($msg) { Write-Host $msg -ForegroundColor White -BackgroundColor DarkBlue }

Say '=========================================='
Say '  ForgeForm  -  one-click launcher'
Say '=========================================='
Say "folder: $root"
Say ''

# ── 1. python ──────────────────────────────────────────────
# A machine can have Python 3.15 on PATH and nothing else. numpy/pymeshlab have no
# 3.14+ wheels, so pip dies. Strategy: find a supported interpreter anywhere, and if
# there is none, install 3.12.9 for the current user (no admin, no UAC) and keep going.

function Find-SupportedPython {
    # 1) py launcher (most reliable, bypasses PATH order)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('3.12', '3.13', '3.11')) {
            $cand = & py "-$v" -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $cand) {
                return @{ exe = $cand.Trim(); ver = $v; how = 'py launcher' }
            }
        }
    }
    # 2) well-known install locations (PATH may be stale right after an install)
    $known = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        'C:\Python312\python.exe'
    )
    foreach ($k in $known) {
        if (Test-Path $k) {
            $v = (& $k -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null).Trim()
            if ($v -in @('3.11', '3.12', '3.13')) { return @{ exe = $k; ver = $v; how = 'install dir' } }
        }
    }
    # 3) whatever `python` resolves to
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) {
        $v = (& $onPath.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null).Trim()
        if ($v -in @('3.11', '3.12', '3.13')) {
            return @{ exe = $onPath.Source; ver = $v; how = 'PATH' }
        }
    }
    return $null
}

$found = Find-SupportedPython
if (-not $found) {
    Say 'No usable Python found (need 3.11 / 3.12 / 3.13).' 'Yellow'
    Say 'numpy and pymeshlab have no wheels for 3.14+, so those versions cannot be used.' 'DarkGray'
    Say ''
    Btn 'downloading Python 3.12.9 and installing it for the current user'
    Say '  (about 30 MB; installs to %LOCALAPPDATA%\Programs\Python; no admin needed)' 'DarkGray'

    $exe = Join-Path $env:TEMP 'python-3.12.9-amd64.exe'
    $sources = @(
        'https://mirrors.huaweicloud.com/python/3.12.9/python-3.12.9-amd64.exe',
        'https://mirrors.aliyun.com/python-release/windows/python-3.12.9-amd64.exe',
        'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe'
    )
    $got = $false
    foreach ($src in $sources) {
        try {
            Say "  download: $src"
            $sw = [Diagnostics.Stopwatch]::StartNew()
            Invoke-WebRequest -Uri $src -OutFile $exe -UseBasicParsing -TimeoutSec 300
            $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
            if ($mb -lt 10) { throw "file too small ($mb MB)" }
            Say ("  ok {0} MB in {1:N1}s" -f $mb, $sw.Elapsed.TotalSeconds) 'Green'
            $got = $true
            break
        } catch {
            Say ("  failed: {0}" -f $_.Exception.Message) 'DarkYellow'
            if (Test-Path $exe) { Remove-Item $exe -Force -ErrorAction SilentlyContinue }
        }
    }

    if (-not $got) {
        Say 'Download failed. Install Python 3.12 by hand, then run this again:' 'Red'
        Say '  https://www.python.org/downloads/release/python-3129/' 'Yellow'
        Read-Host 'Press Enter to close'
        exit 1
    }

    Say '  installing (silent, ~1-2 min)...'
    # InstallAllUsers=0 -> per-user, no admin/UAC. Include_launcher=1 so `py -3.12` works.
    Start-Process -FilePath $exe -Wait -ArgumentList `
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0', 'Include_launcher=1', 'Include_pip=1'
    Say '  install finished, looking for it again...' 'DarkGray'

    $found = Find-SupportedPython
    if (-not $found) {
        Say 'Still cannot find Python 3.11-3.13 after install.' 'Red'
        Say 'Open a NEW window and run 启动.bat again (PATH needs a refresh).' 'Yellow'
        Read-Host 'Press Enter to close'
        exit 1
    }
}

$pyExe = $found.exe
$pyVer = $found.ver
Say "python $pyVer  ok  ($pyExe, via $($found.how))" 'Green'

# ── 2. venv + deps ─────────────────────────────────────────
$venv = Join-Path $root '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
$stamp = Join-Path $venv '.deps-ok'

if ($Reinstall -and (Test-Path $venv)) {
    Say 'removing .venv (-Reinstall)'
    Remove-Item $venv -Recurse -Force
}

if (-not (Test-Path $venvPy)) {
    Say ''
    Btn '[1/2] first run: creating .venv (about 10s)'
    & $pyExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { Say 'venv creation failed' 'Red'; Read-Host 'Enter'; exit 1 }
}

if (-not (Test-Path $stamp)) {
    Btn '[2/2] installing dependencies (3-5 min, one time only)'
    & $venvPy -m pip install --upgrade pip -q
    & $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Say 'pip install failed. Scroll up for the reason.' 'Red'
        Read-Host 'Press Enter to close'
        exit 1
    }
    New-Item -ItemType File -Path $stamp -Force | Out-Null
    Say 'dependencies ok' 'Green'
} else {
    Say 'dependencies already installed (pass -Reinstall to redo)' 'DarkGray'
}

# ── 3. port check ──────────────────────────────────────────
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Say "port $Port already in use (pid $($listen[0].OwningProcess))." 'Yellow'
    Say 'If ForgeForm is already running, just open http://127.0.0.1:'$Port 'Yellow'
    Say 'Opening the page...'
    Start-Process "http://127.0.0.1:$Port"
    Read-Host 'Press Enter to close'
    exit 0
}

# ── 3.5 first-run wizard ───────────────────────────────────
# 没有 config.json = 第一次跑。先让向导探硬件、选方案、装模型，
# 然后再起服务 —— 否则用户进到界面里点生成全是红叉，不知道缺什么。
$cfg = Join-Path $root 'config.json'
if (-not (Test-Path $cfg)) {
    Say ''
    Btn '[setup] first run - probing hardware and picking a profile'
    Say '  (this decides which models to download; you can press Ctrl+C and'  'DarkGray'
    Say '   edit config.json by hand later if you prefer)' 'DarkGray'
    Say ''
    & $venvPy (Join-Path $root 'tools\setup.py')
    if ($LASTEXITCODE -ne 0) {
        Say 'setup wizard did not finish. Starting the app anyway.' 'Yellow'
    }
}

# ── 4. launch ──────────────────────────────────────────────
Say ''
Btn "starting ForgeForm on port $Port ..."
$env:PYTHONPATH = ''
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:FOUNDRY_PORT = "$Port"

$proc = Start-Process -FilePath $venvPy -ArgumentList 'app.py' -WorkingDirectory $root -PassThru -NoNewWindow

# wait for the port, then open the browser
$url = "http://127.0.0.1:$Port"
$opened = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 800
    $ok = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($ok) {
        Start-Sleep -Seconds 2
        Say ''
        Say "  ready: $url" 'Green'
        Say '  (this window is the server - closing it stops ForgeForm)' 'DarkGray'
        Say ''
        Start-Process $url
        $opened = $true
        break
    }
    if ($proc.HasExited) {
        Say ''
        Say "app exited with code $($proc.ExitCode) - see the errors above." 'Red'
        Read-Host 'Press Enter to close'
        exit 1
    }
    if ($i % 5 -eq 0) { Write-Host '.' -NoNewline }
}

if (-not $opened) {
    Say ''
    Say "port $Port did not open within 90s. Open $url manually to check." 'Yellow'
}

# keep this window alive as the server console
Wait-Process -Id $proc.Id
Say 'ForgeForm stopped.'
Read-Host 'Press Enter to close'
