#requires -Version 5.1
<#
  ForgeForm one-shot installer (Windows PowerShell 5.1+)

  Usage (run in an empty folder you want the project in):
      iwr -useb https://gh-proxy.com/https://raw.githubusercontent.com/TIANKONGskyy/forgeform/main/install.ps1 | iex

  What it does:
    1. Downloads the release zip, trying GitHub direct then two mirrors
    2. Extracts it next to the current folder
    3. pip install -r requirements.txt into the CURRENT python
    4. Runs tools/setup.py (hardware probe + profile picker)
    5. Prints the launch command

  It does NOT install Python or ComfyUI. Python 3.11+ must already be on PATH.
#>

param([switch]$SkipSetup, [switch]$SystemPython)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Repo = 'TIANKONGskyy/forgeform'
$Tag  = 'v3.0.0'
$Zip  = 'forgeform-v3.0.0.zip'
$DirName = 'forgeform-v3.0.0'

$Sources = @(
    "https://github.com/$Repo/releases/download/$Tag/$Zip",
    "https://gh-proxy.com/https://github.com/$Repo/releases/download/$Tag/$Zip",
    "https://ghfast.top/https://github.com/$Repo/releases/download/$Tag/$Zip"
)

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

Say '=== ForgeForm installer ==='

# --- 0. python check -------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Say 'python not found on PATH. Install Python 3.11+ first: https://www.python.org/downloads/' 'Red'
    exit 1
}
$pyVer = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])").Trim()
Say "python $pyVer  ($($py.Source))"
if ([version]$pyVer -lt [version]'3.11') {
    Say "Python >= 3.11 required, got $pyVer" 'Red'
    exit 1
}

# --- 1. download -----------------------------------------------------------
$zipPath = Join-Path $env:TEMP $Zip
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

$ok = $false
foreach ($src in $Sources) {
    try {
        Say "download: $src"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Invoke-WebRequest -Uri $src -OutFile $zipPath -UseBasicParsing -TimeoutSec 180
        $mb = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
        if ($mb -lt 1) { throw "file too small ($mb MB)" }
        Say ("  ok  {0} MB in {1:N1}s" -f $mb, $sw.Elapsed.TotalSeconds) 'Green'
        $ok = $true
        break
    } catch {
        Say ("  failed: {0}" -f $_.Exception.Message) 'DarkYellow'
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force -ErrorAction SilentlyContinue }
    }
}
if (-not $ok) {
    Say 'All sources failed. Check network / proxy, then retry.' 'Red'
    exit 1
}

# --- 2. extract ------------------------------------------------------------
$target = (Get-Location).Path
Say "extract to: $target"
Expand-Archive -Path $zipPath -DestinationPath $target -Force
$root = Join-Path $target $DirName
if (-not (Test-Path (Join-Path $root 'app.py'))) {
    # zip prefix may differ; find app.py one level down
    $found = Get-ChildItem $target -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'app.py') } |
             Select-Object -First 1
    if (-not $found) {
        Say 'Extracted archive has no app.py; abort.' 'Red'
        exit 1
    }
    $root = $found.FullName
}
Say "project root: $root" 'Green'

# --- 3. deps ---------------------------------------------------------------
Push-Location $root
try {
    # ★ 默认建 .venv 隔离。直接往系统 Python 里装会降级 starlette/pydantic/pillow，
    #   把机器上别的工具（comfy-cli、bambu、hf 等）连带搞坏 —— 实测踩过。
    if ($SystemPython) {
        $venvPy = 'python'
        Say ''
        Say '[0/2] using SYSTEM python (-SystemPython)'
    } else {
        $venv = Join-Path $root '.venv'
        if (-not (Test-Path $venv)) {
            Say ''
            Say '[0/2] creating .venv (isolated; pass -SystemPython to skip)'
            & python -m venv $venv
            if ($LASTEXITCODE -ne 0) { Say 'venv creation failed.' 'Red'; exit 1 }
        }
        $venvPy = Join-Path $venv 'Scripts\python.exe'
        Say "using: $venvPy" 'Green'
    }

    Say ''
    Say '[1/2] pip install -r requirements.txt'
    & $venvPy -m pip install --upgrade pip -q
    & $venvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Say 'pip failed (see output above).' 'Red'; exit 1 }

    # --- 4. setup wizard ---------------------------------------------------
    Say ''
    if ($SkipSetup) {
        Say '[2/2] skipped (--SkipSetup)'
    } else {
        Say '[2/2] python tools/setup.py   (probes your hardware, picks a profile)'
        & $venvPy tools/setup.py
    }
} finally {
    Pop-Location
}

# --- 5. next step ----------------------------------------------------------
Say ''
Say '=== done ===' 'Green'
Say "next:"
Say "  cd `"$root`""
if (-not $SystemPython) { Say "  .\.venv\Scripts\python.exe app.py" }
else { Say '  python app.py' }
Say '  open http://127.0.0.1:7860'
Say ''
Say 'if something fails, run:  python tools/doctor.py' 'DarkGray'
