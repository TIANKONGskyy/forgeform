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

# ── Self-check before anything else ────────────────────────
# Desktop shortcut + update probe. The probe uses a 4s timeout, so a machine
# without internet still starts in about four seconds instead of hanging.
# Nothing is installed without the user saying yes.
$bootstrap = Join-Path $root 'tools\bootstrap.py'
if (Test-Path $bootstrap) {
    $sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($sysPy) {
        try {
            $bj = & $sysPy $bootstrap --quiet 2>$null | Out-String
            if ($bj -match '\{.*\}') {
                $info = $bj.Substring($bj.IndexOf('{')) | ConvertFrom-Json
                $upd = $info.update
                if ($upd.ok -and $upd.newer) {
                    Say ("New version available: " + $upd.latest + "  (current " + $upd.current + ")") 'Yellow'
                    Say "  " + $upd.url
                    Say '  Run 更新.bat to upgrade (keeps config and models).' 'DarkYellow'
                } elseif ($upd.ok) {
                    Say ("Version " + $upd.current + " is up to date.") 'DarkGray'
                } else {
                    Say 'No network, skipping update check.' 'DarkGray'
                }
                if ($info.steampp -and -not $info.steampp.installed) {
                    Say 'Steam++ not found - model downloads may be slow.' 'DarkYellow'
                    Say '  Install it if you want faster downloads: https://steampp.net/' 'DarkGray'
                }
            }
        } catch {
            Say 'Self-check skipped.' 'DarkGray'
        }
    }
    Say ''
}

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

        exit 1
    }
}

$pyExe = $found.exe
$pyVer = $found.ver
Say "python $pyVer  ok  ($pyExe, via $($found.how))" 'Green'

# Open the graphical control center before installing dependencies or models.
$env:PYTHONPATH = ''
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
& $pyExe (Join-Path $root 'tools\studio.py') --port ($Port + 1) --app-port $Port
exit $LASTEXITCODE
