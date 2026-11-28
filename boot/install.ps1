param(
    [string]$Dest = (Join-Path $env:USERPROFILE 'ForgeForm'),
    [string]$LocalPkg = '',
    [switch]$NoDeps,
    [switch]$NoDoctor
)
# ============================================================
#  铸想 ForgeForm 一键安装（Windows，双击运行）
#  做的事：
#    1. 检查环境（系统 / 显卡 / 磁盘 / 网络）
#    2. 没有 Python 就自动装一个便携版（不碰系统 Python）
#    3. 从 GitHub 拉主包（镜像加速 + 多连接；tar 可边下边解）
#    4. 装运行依赖，跑自检，给出启动方式
#  全程不需要管理员权限，不改系统环境变量。
# ============================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Home2 = Join-Path $env:LOCALAPPDATA 'ForgeForm'
$Runtime = Join-Path $Home2 'runtime'
$PkgDir = Join-Path $Home2 'pkg'
$Banner = @'

  ============================================
    铸想 ForgeForm 一键安装
    本地出图 -> 建模 -> 可打印 STL
  ============================================

'@

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Ok($msg) { Say "  [OK]   $msg" 'Green' }
function Warn($msg) { Say "  [WARN] $msg" 'Yellow' }
function Bad($msg) { Say "  [FAIL] $msg" 'Red' }

function Test-Python($exe) {
    try {
        $v = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return [version]$v }
    } catch { }
    return $null
}

function Find-Python {
    $cands = @()
    foreach ($n in @('python', 'python3', 'py')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $cands += $c.Source }
    }
    foreach ($p in @(
        (Join-Path $Runtime 'python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        'D:\miniconda3\python.exe'
    )) { if (Test-Path $p) { $cands += $p } }
    foreach ($exe in $cands) {
        $v = Test-Python $exe
        if ($v -and $v -ge [version]'3.10') { return @{ exe = $exe; ver = "$v" } }
    }
    return $null
}

function Get-PythonRuntime {
    Say "  没有可用的 Python（需要 3.10+），自动下载便携版…" 'Yellow'
    $dl = Join-Path $Home2 'python-embed.zip'
    New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
    $urls = @(
        'https://registry.npmmirror.com/-/binary/python/3.12.7/python-3.12.7-embed-amd64.zip',
        'https://mirrors.huaweicloud.com/python/3.12.7/python-3.12.7-embed-amd64.zip',
        'https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip'
    )
    foreach ($u in $urls) {
        try {
            Say "    取 $($u.Split('/')[2]) …"
            Invoke-WebRequest -Uri $u -OutFile $dl -UseBasicParsing -TimeoutSec 60
            if ((Get-Item $dl).Length -gt 5MB) { break }
        } catch { Warn "    失败：$($_.Exception.Message)" ; $dl = $null }
    }
    if (-not $dl -or -not (Test-Path $dl)) { Bad "Python 便携版下载失败，请检查网络"; exit 1 }
    $pyDir = Join-Path $Runtime 'python'
    if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
    Expand-Archive -Path $dl -DestinationPath $pyDir -Force
    # 便携版默认禁用 site-packages 和 pip，这里打开并装 pip
    $pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
    if ($pth) {
        $lines = Get-Content $pth.FullName
        if (-not ($lines -match 'import site')) { Add-Content $pth.FullName 'import site' }
        if (-not ($lines -match 'Lib\\site-packages')) { Add-Content $pth.FullName 'Lib\site-packages' }
    }
    $getpip = Join-Path $Home2 'get-pip.py'
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing -TimeoutSec 60
    & (Join-Path $pyDir 'python.exe') $getpip --no-warn-script-location | Out-Null
    Ok "Python 便携版就绪：$pyDir"
    return (Join-Path $pyDir 'python.exe')
}

function Invoke-Boot([string[]]$bootArgs) {
    # 引擎优先用刚刚找到的 Python；没有就先装便携版
    $py = $script:PyExe
    if (-not $py) { $py = Get-PythonRuntime }
    $engine = Join-Path $Root 'ff_boot.py'
    if (-not (Test-Path $engine)) { $engine = Join-Path $Root 'boot\ff_boot.py' }
    if (-not (Test-Path $engine)) { Bad "安装引擎 ff_boot.py 不在同目录"; exit 1 }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    # 必须 Out-Host：直接输出会被当成函数返回值，退出码就和日志混在一起
    & $py $engine @bootArgs | Out-Host
    return $LASTEXITCODE
}

# ---------------------------------------------------------------- 1. 环境
Say $Banner 'Cyan'
Say '[1/5] 环境检查' 'White'
$os = [System.Environment]::OSVersion.Version
Say "  系统：Windows $($os.Major).$($os.Minor) ($env:PROCESSOR_ARCHITECTURE)"
$gpu = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
if ($gpu) { Ok "显卡：$gpu" } else { Warn "没检测到 NVIDIA 显卡（只能用 CPU 跑，会很慢）" }
$free = [math]::Round((Get-PSDrive ($env:USERPROFILE.Substring(0,1))).Free / 1GB, 1)
Say "  磁盘剩余：$free GB（建议 ≥ 30 GB）"
if ($free -lt 10) { Bad "磁盘太小，先清理再装"; exit 1 }

$script:PyExe = $null
$found = Find-Python
if ($found) { $script:PyExe = $found.exe; Ok "Python：$($found.ver)（$($found.exe)）" }
else { Warn "本机没有 3.10+ 的 Python（稍后自动装便携版，不影响系统）" }
if (-not $found) { $script:PyExe = Get-PythonRuntime }

# ---------------------------------------------------------------- 2. 网络
Say ''
Say '[2/5] 网络与加速' 'White'
$mirrorOk = $null
foreach ($m in @('https://gh-proxy.com/', 'https://ghfast.top/')) {
    try {
        $t0 = Get-Date
        $null = Invoke-WebRequest -Uri ($m + 'https://github.com') -Method Head -UseBasicParsing -TimeoutSec 8
        Say "  $($m) 可用（$([math]::Round(((Get-Date)-$t0).TotalMilliseconds)) ms）" 'Green'
        $mirrorOk = $m; break
    } catch { Say "  $($m) 不可用" 'DarkGray' }
}
if (-not $mirrorOk) {
    try { $null = Invoke-WebRequest -Uri 'https://github.com' -Method Head -UseBasicParsing -TimeoutSec 8; $mirrorOk = ''; Ok "GitHub 直连可用" }
    catch { Warn "GitHub 与镜像都不通，仍会尝试下载（可能失败）" }
}

# ---------------------------------------------------------------- 3. 版本
Say ''
Say '[3/5] 查询最新版本' 'White'
$enginePath = Join-Path $Root 'ff_boot.py'
if (-not (Test-Path $enginePath)) { $enginePath = Join-Path $Root 'boot\ff_boot.py' }
$tag = (& $script:PyExe $enginePath tag 2>$null)
if (-not $tag) { $tag = 'v3.14.3'; Warn "查不到最新 tag，用内置版本 $tag" } else { Ok "最新版本：$tag" }

# ---------------------------------------------------------------- 4. 下载 + 解压
Say ''
Say '[4/5] 下载并安装（边下边解）' 'White'
$bootArgs = @('install', '--tag', $tag, '--dest', $Dest)
if ($LocalPkg) { $bootArgs += @('--local', $LocalPkg) }
$code = Invoke-Boot $bootArgs
Say "  （引擎退出码：$code）" 'DarkGray'
if ($code -ne 0) { Bad "下载/解压失败（退出码 $code）"; Read-Host "按回车退出"; exit 1 }

# ---------------------------------------------------------------- 5. 依赖
Say ''
Say '[5/5] 安装运行依赖' 'White'
$req = Join-Path $Dest 'requirements.txt'
if ($NoDeps) { Warn 'skip deps (-NoDeps)' }
elseif (Test-Path $req) {
    $mirrors = @('https://pypi.tuna.tsinghua.edu.cn/simple', 'https://mirrors.aliyun.com/pypi/simple')
    $done = $false
    foreach ($m in $mirrors) {
        Say "  用 $($m.Split('/')[2]) 安装…"
        & $script:PyExe -m pip install -q -r $req -i $m --trusted-host $m.Split('/')[2]
        if ($LASTEXITCODE -eq 0) { $done = $true; break }
    }
    if ($done) { Ok "依赖装好" } else { Warn "依赖安装有问题，稍后可手动跑：python -m pip install -r requirements.txt" }
}

$doctor = Join-Path $Dest 'tools\doctor.py'
if ((Test-Path $doctor) -and (-not $NoDoctor)) { Say ''; Say '  自检：' 'White'; & $script:PyExe $doctor --fix }

Say ''
Say '  安装完成！' 'Green'
Say "  目录：$Dest"
Say '  启动：双击目录里的 一键开始.bat，或执行 python app.py'
Say '  浏览器打开：http://127.0.0.1:7860'
Say ''
Say '  下一步（可选）：装出图/建模权重' 'White'
Say "  cd `"$Dest`""
Say '  python tools\first_run.py --with-image'
Say ''
Read-Host '按回车关闭'
