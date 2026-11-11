@echo off
rem ForgeForm launcher - double click this file.
rem ASCII only: cmd.exe in a CP936 console mangles UTF-8 Chinese in .bat files.
chcp 65001 >nul 2>&1
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo Launcher exited with an error. See the messages above.
  pause
)
