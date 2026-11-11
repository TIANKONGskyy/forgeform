@echo off
rem GPU 温度调节器启动包装（供计划任务 DSH-ThermalGovernor 调用）
python "%~dp0thermal_governor.py"
