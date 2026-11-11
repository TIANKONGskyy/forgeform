# -*- coding: utf-8 -*-
"""更新桌面快捷方式图标为生成的 AI 创意工坊主题图标"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
icon = r"D:\AI-Workshop\assets\app_icon.ico"

ps_code = r"""
$d = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell

$full = 'AI ' + [string][char]0x521B + [string][char]0x610F + [string][char]0x5DE5 + [string][char]0x574A
$p = Join-Path $d ($full + '.lnk')
$s = $ws.CreateShortcut($p)
$s.TargetPath = 'powershell.exe'
$s.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "D:\AI-Workshop\start-all.ps1"'
$s.WorkingDirectory = 'D:\AI-Workshop'
$s.Description = 'AI Workshop launcher'
$s.IconLocation = 'D:\AI-Workshop\assets\app_icon.ico,0'
$s.WindowStyle = 1
$s.Save()
Write-Output ("icon set: " + $s.IconLocation)

$lnk2 = Join-Path $d 'AI_Workshop_log.lnk'
$s2 = $ws.CreateShortcut($lnk2)
$s2.TargetPath = 'powershell.exe'
$s2.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "D:\AI-Workshop\start-all.ps1"'
$s2.WorkingDirectory = 'D:\AI-Workshop'
$s2.Description = 'AI Workshop launcher with log window'
$s2.IconLocation = 'D:\AI-Workshop\assets\app_icon.ico,0'
$s2.WindowStyle = 1
$s2.Save()
Write-Output ("icon set: " + $s2.IconLocation)
"""

r = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_code],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
)
print(r.stdout)
if r.stderr.strip():
    print("ERR:", r.stderr.strip()[:300])

for f in os.listdir(desktop):
    if f.endswith(".lnk") and ("AI" in f or "创意" in f):
        print("desktop:", repr(f))
