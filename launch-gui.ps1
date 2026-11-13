#requires -Version 5.1
Add-Type -AssemblyName PresentationFramework
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdout = Join-Path $logDir 'launcher.log'
$stderr = Join-Path $logDir 'launcher-error.log'
[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Title="ZYKT铸想" Width="620" Height="440" WindowStartupLocation="CenterScreen" Background="#101622" Foreground="#EDF1FA" ResizeMode="CanMinimize"><StackPanel Margin="36"><TextBlock Text="ZYKT铸想" FontSize="26" Margin="0,0,0,12"/><TextBlock Name="Status" Text="Preparing your creative workspace..." FontSize="15"/><TextBlock Text="Python will be detected or installed automatically." Foreground="#9DA9BD" Margin="0,12,0,18"/><ProgressBar Name="Progress" Height="5" IsIndeterminate="True" Foreground="#9CE4DA"/><TextBox Name="Log" Margin="0,20,0,0" Height="190" Background="#0C1019" Foreground="#9DA9BD" IsReadOnly="True" TextWrapping="Wrap" VerticalScrollBarVisibility="Auto" BorderThickness="0"/><Button Name="Retry" Content="Retry" Margin="0,15,0,0" Height="32" Visibility="Collapsed"/></StackPanel></Window>
'@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$status = $window.FindName('Status')
$log = $window.FindName('Log')
$retry = $window.FindName('Retry')
$progress = $window.FindName('Progress')
# Update first, then launch. The update keeps user data (config / outputs /
# models / .venv) and never blocks startup: if it fails we just run the
# version that is already on disk.
function Invoke-Update {
    $bs = Join-Path $root 'tools\bootstrap.py'
    if (-not (Test-Path $bs)) { return }
    $py = 'python'
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $venvPy) { $py = $venvPy }
    $status.Text = 'Checking for updates...'
    $progress.IsIndeterminate = $true
    try {
        $out = & $py $bs --auto-update 2>&1 | Out-String
        if (Test-Path $stdout) { Add-Content -Path $stdout -Value $out -Encoding UTF8 }
    } catch {
        if (Test-Path $stdout) { Add-Content -Path $stdout -Value ('update skipped: ' + $_.Exception.Message) -Encoding UTF8 }
    }
}

function Start-Launcher {
    Invoke-Update
    $script:proc = Start-Process powershell -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $root 'start.ps1')+'"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $retry.Visibility = 'Collapsed'
    $progress.IsIndeterminate = $true
    $status.Text = 'Preparing your creative workspace...'
}
$retry.Add_Click({ Start-Launcher })
$timer = New-Object Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(700)
$timer.Add_Tick({
    if (Test-Path $stdout) { $log.Text = (Get-Content $stdout -Tail 12 -ErrorAction SilentlyContinue) -join "`r`n"; $log.ScrollToEnd() }
    if ($log.Text -match 'ForgeForm Studio:') { $timer.Stop(); $window.Close(); return }
    if ($script:proc.HasExited) {
        $progress.IsIndeterminate = $false
        $status.Text = 'Setup could not finish. Review the log and retry.'
        if (Test-Path $stderr) { $log.Text += "`r`n" + ((Get-Content $stderr -Tail 10 -ErrorAction SilentlyContinue) -join "`r`n") }
        $retry.Visibility = 'Visible'
    }
})
Start-Launcher
$timer.Start()
$window.Add_Closed({ $timer.Stop() })
$window.ShowDialog() | Out-Null
