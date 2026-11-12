#requires -Version 5.1
<#
  ForgeForm updater.

  Downloads the latest release zip, extracts it to a temp dir, and merges the
  new source over the current install. User data is preserved:
    config.json / outputs / models / .venv / logs / projects / temp

  ASCII-only on purpose: PS 5.1 reads UTF-8 without BOM as GBK and mangles
  Chinese comments into parser errors.
#>
param(
    [string]$Repo = 'TIANKONGskyy/forgeform',
    [string]$Version = 'latest',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

Say '=========================================='
Say '  ForgeForm  -  updater'
Say '=========================================='

# ── current version ────────────────────────────────────────
$localVer = 'unknown'
$vf = Join-Path $root 'VERSION'
if (Test-Path $vf) { $localVer = (Get-Content $vf -Raw).Trim() }
Say "current version: $localVer"
Say "install dir:     $root"
Say ''

# ── download ───────────────────────────────────────────────
if ($Version -eq 'latest') {
    $api = "https://api.github.com/repos/$Repo/releases/latest"
    try {
        $rel = Invoke-RestMethod -Uri $api -UseBasicParsing -TimeoutSec 30
        $tag = $rel.tag_name
    } catch {
        Say "cannot read latest release ($($_.Exception.Message))" 'Yellow'
        Say 'falling back to tag v3.0.0' 'Yellow'
        $tag = 'v3.0.0'
    }
} else {
    $tag = $Version
}
Say "target release: $tag"

$zipName = 'forgeform-v3.0.0.zip'
$sources = @(
    "https://github.com/$Repo/releases/download/$tag/$zipName",
    "https://gh-proxy.com/https://github.com/$Repo/releases/download/$tag/$zipName",
    "https://ghfast.top/https://github.com/$Repo/releases/download/$tag/$zipName"
)

$tmp = Join-Path $env:TEMP ('forgeform-update-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zip = Join-Path $tmp $zipName
$got = $false
foreach ($src in $sources) {
    try {
        Say "download: $src"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Invoke-WebRequest -Uri $src -OutFile $zip -UseBasicParsing -TimeoutSec 180
        $mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
        if ($mb -lt 1) { throw "file too small ($mb MB)" }
        Say ("  ok  {0} MB in {1:N1}s" -f $mb, $sw.Elapsed.TotalSeconds) 'Green'
        $got = $true
        break
    } catch {
        Say ("  failed: {0}" -f $_.Exception.Message) 'DarkYellow'
        if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
    }
}
if (-not $got) {
    Say 'All sources failed. Check network / proxy, then retry.' 'Red'
    exit 1
}

# ── extract ────────────────────────────────────────────────
Say ''
Say 'extracting...'
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$newRoot = Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'app.py') } | Select-Object -First 1
if (-not $newRoot) {
    Say 'extracted archive has no app.py; abort.' 'Red'
    exit 1
}
$newVer = 'unknown'
$nvf = Join-Path $newRoot.FullName 'VERSION'
if (Test-Path $nvf) { $newVer = (Get-Content $nvf -Raw).Trim() }
Say "package version: $newVer"

if ($newVer -ne 'unknown' -and $newVer -eq $localVer) {
    Say ''
    Say "already on $localVer - nothing to do." 'Green'
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Read-Host 'Press Enter to close'
    exit 0
}

# ── merge via updater.py ───────────────────────────────────
Say ''
Say 'merging new files (user data is kept)...'
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

$args = @((Join-Path $root 'tools\updater.py'), '--from', $newRoot.FullName, '--to', $root)
if ($DryRun) { $args += '--dry-run' }
& $py @args
if ($LASTEXITCODE -ne 0) {
    Say 'merge failed.' 'Red'
    Read-Host 'Press Enter to close'
    exit 1
}

if ($DryRun) {
    Say ''
    Say '(dry run - nothing written)' 'Yellow'
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Read-Host 'Press Enter to close'
    exit 0
}

# ── deps may have changed ──────────────────────────────────
$req = Join-Path $root 'requirements.txt'
$stamp = Join-Path $root '.venv\.deps-ok'
if ((Test-Path $py) -and (Test-Path $req)) {
    if (-not (Test-Path $stamp) -or (Get-Item $req).LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
        Say ''
        Say 'requirements changed - installing...'
        & $py -m pip install -r $req -q
        if ($LASTEXITCODE -eq 0) { New-Item -ItemType File -Path $stamp -Force | Out-Null }
        else { Say 'pip failed; run 启动.bat again to retry.' 'Yellow' }
    }
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Say ''
Say '=== updated ===' 'Green'
Say "  $localVer -> $newVer"
Say ''
Say 'next:'
Say '  double-click 启动.bat'
Read-Host 'Press Enter to close'
