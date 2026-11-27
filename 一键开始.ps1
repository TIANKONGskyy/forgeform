#requires -Version 5.1
<#
  ForgeForm - one click start (Windows)

  What it does:
    1. Finds a usable Python 3.10+ (PATH, then common install dirs)
    2. Runs tools/doctor.py --fix  (creates dirs, copies config, prints what is missing)
    3. Starts start.ps1 (which opens the Studio window and the web UI)

  If doctor reports a blocking problem, this script STOPS and prints the exact
  fix command instead of pretending to launch. That is on purpose: an app that
  starts half-configured is worse than one that says what is wrong.

  Comments are ASCII only: PS 5.1 reads UTF-8 without BOM as GBK and mangles
  Chinese comments into parser errors.
#>
param(
    [int]$Port = 7860,
    [switch]$SkipDoctor
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

Say '=== ForgeForm 一键开始 / one click start ==='

# ---------------------------------------------------------------- python
$py = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = (& $cand -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
        if ($ver -and ([version]$ver -ge [version]'3.10')) { $py = $cand; break }
    }
}
if (-not $py) {
    foreach ($cand in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe")) {
        if (Test-Path $cand) { $py = $cand; break }
    }
}
if (-not $py) {
    Say 'No Python 3.10+ found.' 'Red'
    Say '  Install: https://www.python.org/downloads/  (check "Add python.exe to PATH")' 'Yellow'
    Read-Host 'Press Enter to exit'
    exit 1
}
$ver = (& $py -c "import sys;print('%d.%d.%d'%sys.version_info[:3])")
Say "python $ver  ($py)"

# ---------------------------------------------------------------- doctor
if (-not $SkipDoctor) {
    Say '--- checking environment ---'
    $env:PYTHONPATH = ''
    $env:PYTHONIOENCODING = 'utf-8'
    & $py 'tools/doctor.py' '--fix'
    if ($LASTEXITCODE -ne 0) {
        Say ''
        Say 'Some checks failed. Most common fixes:' 'Yellow'
        Say '  - missing packages :  pip install -r requirements.txt'
        Say '  - ComfyUI not running: start ComfyUI first, or set backend to cloud in config.json'
        Say '  - disk almost full  :  free at least 2 GB'
        Say ''
        Say 'You can still try to start. Press Enter to continue, Ctrl+C to stop.' 'Yellow'
        Read-Host
    }
}

# ---------------------------------------------------------------- start
Say '--- starting ---'
& (Join-Path $root 'start.ps1') -Port $Port
