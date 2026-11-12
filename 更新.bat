@echo off
rem ForgeForm updater - double click this file to update an existing install.
rem ASCII only: cmd.exe in a CP936 console mangles UTF-8 Chinese in .bat files.
chcp 65001 >nul 2>&1
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
if errorlevel 1 (
  echo.
  echo Update failed. See the messages above.
  pause
)
