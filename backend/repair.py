# -*- coding: utf-8 -*-
"""一键自动修复：诊断 + 修复常见问题"""
import os
import subprocess
import time
import requests
from pathlib import Path

from backend.env import COMFYUI_DIR as _COMFY_DIR, COMFY_PYTHON as _COMFY_PY, LOGS

COMFYUI_DIR = str(_COMFY_DIR)
COMFY_PYTHON = str(_COMFY_PY)
COMFY_ENV = {
    "HF_ENDPOINT": "https://hf-mirror.com",
    "MODEL_CHECKSUM_DISABLED": "1",
    "HF_HUB_DISABLE_XET": "1",
}
COMFY_URL = "http://127.0.0.1:8188"


def _comfy_running() -> bool:
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _find_comfy_pids() -> list:
    """找 ComfyUI 进程（main.py 8188）"""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Where-Object { $_.CommandLine -match 'main.py.*8188' } | "
             "ForEach-Object { $_.ProcessId }"],
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW
        ).decode().strip()
        return [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        return []


def _start_comfyui() -> str:
    """启动 ComfyUI（GPU 模式）"""
    env = dict(os.environ)
    env.update(COMFY_ENV)
    try:
        subprocess.Popen(
            [COMFY_PYTHON, "main.py", "--lowvram", "--listen", "0.0.0.0", "--port", "8188"],
            cwd=COMFYUI_DIR, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "已启动"
    except Exception as e:
        return f"启动失败: {e}"


def _kill_comfyui() -> str:
    pids = _find_comfy_pids()
    if not pids:
        return "无进程"
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    time.sleep(2)
    return f"已杀 {len(pids)} 个进程"


def _check_gpu() -> str:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total,power.limit",
             "--format=csv,noheader,nounits"],
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        ).decode().strip()
        parts = [p.strip() for p in out.split(",")]
        temp, mem_used, mem_total, pl = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
        status = "正常"
        if temp >= 83:
            status = f"⚠️ 温度 {temp}°C 超阈值（≥83°C 会崩），建议降功耗墙"
        elif temp >= 80:
            status = f"⚠️ 温度 {temp}°C 偏高"
        return f"GPU {temp}°C | 显存 {mem_used}/{mem_total}MB | 功耗墙 {pl}W | {status}"
    except Exception as e:
        return f"GPU 查询失败: {e}"


def _check_queue_stuck() -> str:
    """检查队列是否卡死（有任务但 GPU 空闲）"""
    try:
        q = requests.get(f"{COMFY_URL}/queue", timeout=3).json()
        running = q.get("queue_running", [])
        if running:
            try:
                gpu = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                    timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
                ).decode().strip()
                if int(gpu) < 5:
                    # 队列有任务但 GPU 空闲 = 卡住
                    requests.post(f"{COMFY_URL}/interrupt", timeout=5)
                    return "⚠️ 检测到卡死任务，已 interrupt"
            except Exception:
                pass
        return "队列正常" if not running else f"运行中 {len(running)} 任务"
    except Exception:
        return "队列查询失败"


def _scan_recent_errors() -> str:
    """扫描 app 日志最近错误"""
    log_file = Path(LOGS) / f"{time.strftime('%Y-%m-%d')}.jsonl"
    if not log_file.exists():
        return "无日志"
    errs = []
    try:
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                if "error" in line.lower() or "失败" in line or "❌" in line:
                    errs.append(line.strip()[:150])
    except Exception:
        return "日志读取失败"
    if not errs:
        return "最近无错误"
    return f"最近 {len(errs)} 条错误: " + " | ".join(errs[-3:])


def run_auto_repair() -> str:
    """一键自动修复主流程"""
    lines = []
    lines.append(f"🔧 自动修复开始 [{time.strftime('%H:%M:%S')}]")

    # 1. ComfyUI 状态
    if _comfy_running():
        lines.append("✅ ComfyUI 在线 (0.30.1)")
    else:
        lines.append("❌ ComfyUI 离线 → 启动中...")
        lines.append(f"   {_kill_comfyui()}")
        lines.append(f"   {_start_comfyui()}")
        # 等待启动
        for i in range(12):
            time.sleep(5)
            if _comfy_running():
                lines.append("✅ ComfyUI 已恢复在线")
                break
        else:
            lines.append("⚠️ ComfyUI 启动超时（检查 python 环境）")

    # 2. 队列卡死检测
    lines.append(f"📊 队列: {_check_queue_stuck()}")

    # 3. GPU 状态
    lines.append(f"🖥 {_check_gpu()}")

    # 4. 日志错误
    lines.append(f"📋 {_scan_recent_errors()}")

    lines.append(f"✅ 自动修复完成 [{time.strftime('%H:%M:%S')}]")
    return "\n".join(lines)
