@echo off
start "ForgeForm" powershell -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0launch-gui.ps1"
