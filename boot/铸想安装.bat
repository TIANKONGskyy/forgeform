@echo off
chcp 65001 >nul
title ForgeForm Setup
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 pause
endlocal
