#requires -Version 5.1
<#
  ForgeForm one-shot installer (Windows PowerShell 5.1+)

  Usage (run in an empty folder you want the project in):
      iwr -useb https://gh-proxy.com/https://raw.githubusercontent.com/TIANKONGskyy/forgeform/main/install.ps1 | iex

  What it does:
    1. Resolves the newest release tag (falls back to the pinned one)
    2. Downloads the release zip, trying mirrors first (github.com direct is
       usually blocked / very slow on mainland networks)
    3. Extracts it into .\<forgeform-version>\
    4. pip install -r requirements.txt into the CURRENT python
    5. Runs tools/doctor.py --fix so you see exactly what is missing
    6. Prints the launch command

  It does NOT install Python or ComfyUI. Python 3.10+ must already be on PATH.

  Notes:
    - All comments are ASCII: PS 5.1 reads UTF-8 without BOM as GBK and turns
      Chinese comments into parser errors.
    - Mirror order matters: gh-proxy works from CN, github.com direct usually
      times out. A short per-source timeout keeps the fallback fast.
#>

param(
    [string]$Tag = '',
    [switch]$SkipSetup,
    [switch]$SkipDoctor
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Repo = 'TIANKONGskyy/forgeform'
$PinnedTag = 'v3.8.8'
$TimeoutSec = 30      # per source; github.com direct must not hang for 3 minutes

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
if ([version]$pyVer -lt [version]'3.10') {
    Say 'Need Python 3.10+ (3.12 recommended).' 'Red'
    exit 1
}

# --- 1. resolve tag --------------------------------------------------------
if (-not $Tag) {
    $Tag = $PinnedTag
    try {
        $probe = "https://gh-proxy.com/https://github.com/$Repo/releases/latest"
        $resp = Invoke-WebRequest -Uri $probe -MaximumRedirection 0 -ErrorAction SilentlyContinue -UseBasicParsing -TimeoutSec $TimeoutSec
        if ($resp.Headers.Location) {
            $m = [regex]::Match($resp.Headers.Location, '/tag/(v[0-9.]+)')
            if ($m.Success) { $Tag = $m.Groups[1].Value }
        }
    } catch {
        # 3xx is expected here; PS throws on MaximumRedirection 0 in some versions
        if ($_.Exception.Response -and $_.Exception.Response.Headers.Location) {
            $loc = $_.Exception.Response.Headers.Location.ToString()
            $m = [regex]::Match($loc, '/tag/(v[0-9.]+)')
            if ($m.Success) { $Tag = $m.Groups[1].Value }
        }
    }
}
Say "release tag: $Tag"

$Zip = "forgeform-$Tag.zip"
$DirName = "forgeform-$($Tag.TrimStart('v'))"

# Mirror first: on mainland networks github.com direct commonly times out.
$Sources = @(
    "https://gh-proxy.com/https://github.com/$Repo/releases/download/$Tag/$Zip",
    "https://ghfast.top/https://github.com/$Repo/releases/download/$Tag/$Zip",
    "https://github.com/$Repo/releases/download/$Tag/$Zip"
)

# --- 2. download -----------------------------------------------------------
$zipPath = Join-Path $env:TEMP $Zip
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
$ok = $false
foreach ($src in $Sources) {
    $host_ = ([uri]$src).Host
    Say "download: $host_ ..." 'DarkGray'
    try {
        Invoke-WebRequest -Uri $src -OutFile $zipPath -UseBasicParsing -TimeoutSec $TimeoutSec
        $len = (Get-Item $zipPath).Length
        if ($len -gt 1MB) {
            Say ("  ok " + [math]::Round($len / 1MB, 2) + " MB") 'Green'
            $ok = $true
            break
        }
        Say '  too small, trying next mirror' 'Yellow'
    } catch {
        Say ("  failed: " + $_.Exception.Message.Substring(0, [Math]::Min(70, $_.Exception.Message.Length))) 'Yellow'
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force -ErrorAction SilentlyContinue }
    }
}
if (-not $ok) {
    Say 'All mirrors failed. Download manually from the releases page and unzip.' 'Red'
    Say "  https://github.com/$Repo/releases" 'Yellow'
    exit 1
}

# --- 3. extract ------------------------------------------------------------
$target = Join-Path (Get-Location) '.'
Say "extracting to $target"
Expand-Archive -Path $zipPath -DestinationPath $target -Force
$root = Join-Path $target $DirName
if (-not (Test-Path $root)) {
    # zip may carry a different top folder name; pick the first forgeform-* dir
    $cand = Get-ChildItem $target -Directory -Filter 'forgeform-*' | Select-Object -First 1
    if ($cand) { $root = $cand.FullName }
}
if (-not (Test-Path $root)) {
    Say 'extraction produced no forgeform folder' 'Red'
    exit 1
}
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
Say "project: $root" 'Green'

# --- 4. python deps --------------------------------------------------------
if (-not $SkipSetup) {
    Say 'installing python deps (pip) ...'
    Push-Location $root
    try {
        & python -m pip install -q --disable-pip-version-check -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Say 'pip reported an error; continuing' 'Yellow' }
    } finally {
        Pop-Location
    }
}

# --- 5. environment check --------------------------------------------------
if (-not $SkipDoctor) {
    Say ''
    Say '--- environment check ---'
    Push-Location $root
    try {
        $env:PYTHONPATH = ''
        $env:PYTHONIOENCODING = 'utf-8'
        & python tools/doctor.py --fix
    } catch {
        Say 'doctor failed to run; retry manually: python tools/doctor.py --fix' 'Yellow'
    } finally {
        Pop-Location
    }
}

Say ''
Say 'done. launch with:' 'Green'
Say "  cd `"$root`"" 'White'
Say '  python app.py        (or double-click the one-click start script)' 'White'
