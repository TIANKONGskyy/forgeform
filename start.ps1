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
# Prefer the py launcher: a machine can have 3.15 on PATH and 3.12 installed beside it,
# and only 3.12/3.13 have numpy/pymeshlab wheels. Trying `py -3.12` first saves the user
# from having to fix PATH by hand.
$pyExe = $null
$pyVer = $null
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    foreach ($v in @('3.12', '3.13', '3.11')) {
        $cand = & py "-$v" -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $cand) {
            $pyExe = $cand.Trim()
            $pyVer = $v
            Say "found python $v via py launcher: $pyExe" 'Green'
            break
        }
    }
}
if (-not $pyExe) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        Say 'python not found.' 'Red'
        Say 'Install Python 3.12 (tick "Add python.exe to PATH"):' 'Yellow'
        Say '  https://www.python.org/downloads/release/python-3129/' 'Yellow'
        Read-Host 'Press Enter to close'
        exit 1
    }
    $pyExe = $py.Source
    $pyVer = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])").Trim()
}
# Upper bound matters: pymeshlab / numpy wheels stop at cp313, so a 3.14+ interpreter
# fails at pip with "No matching distribution found for pymeshlab". Seen on a 3.15.
if (([version]$pyVer -lt [version]'3.11') -or ([version]$pyVer -ge [version]'3.14')) {
    Say "Python $pyVer is not supported - need 3.11, 3.12 or 3.13." 'Red'
    Say 'Newer Pythons have no numpy/pymeshlab wheels yet.' 'Yellow'
    Say 'Install 3.12 (tick "Add python.exe to PATH"), then run this again:' 'Yellow'
    Say '  https://www.python.org/downloads/release/python-3129/' 'Yellow'
    Read-Host 'Press Enter to close'
    exit 1
}
Say "python $pyVer  ok  ($pyExe)" 'Green'

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
