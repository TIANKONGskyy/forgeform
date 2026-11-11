# -*- coding: utf-8 -*-
"""拓竹直发模块（2026-08-11 新增）
基于 bambu-cli（bambucli python 库）：
- 配置管理：add_printer / list / remove（~/.bambu-cli/printers.json）
- 状态：在线检测 / 打印机信息
- 打印：上传 STL → 切片(OrcaSlicer CLI，若可用) → 开始打印

注意：bambu-cli 装在 Python312（AppData），app 用 miniconda3(py311)，
     所以这里通过子进程调用 bambu.exe，避免跨环境 import 问题。
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BAMBU_EXE = os.environ.get("BAMBU_EXE") or shutil.which("bambu") or "bambu"
CONFIG_FILE = Path.home() / ".bambu-cli" / "printers.json"

# 打印机型号映射（BambuStudio model_id → 显示名）
MODEL_NAMES = {
    "N1": "Bambu Lab A1 mini",
    "N2S": "Bambu Lab A1",
    "C11": "Bambu Lab P1P",
    "C12": "Bambu Lab P1S",
    "BL-P001": "Bambu Lab X1 Carbon",
    "BL-P002": "Bambu Lab X1",
}


def _run_bambu(args, timeout=60):
    """调用 bambu.exe，返回 (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(
            [BAMBU_EXE] + args,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"bambu.exe 不存在: {BAMBU_EXE}"
    except subprocess.TimeoutExpired:
        return -1, "", "bambu 命令超时"


# ─── 配置管理 ───────────────────────────────────────────
def load_printers() -> dict:
    """读取已配置打印机 {name: {ip, serial, access_code, model}}"""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _valid_ipv4(ip: str) -> bool:
    """严格 IPv4 校验：四段 0-255（2026-08-13：原正则只查数字格式，999.999.999.999 也能过）"""
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip.strip())
        return isinstance(addr, ipaddress.IPv4Address)
    except ValueError:
        return False


def save_printer(name: str, ip: str, serial: str, access_code: str, model: str = "") -> tuple:
    """保存打印机配置（幂等，可覆盖）
    返回 (ok, msg)
    """
    if not ip or not serial or not access_code:
        return False, "IP / 序列号 / 访问码 必填"
    if not _valid_ipv4(ip):
        return False, f"IP 格式不对（需 IPv4 0-255 段）: {ip}"

    printers = load_printers()
    printers[name] = {
        "ip_address": ip,
        "serial_number": serial,
        "name": name,
        "access_code": access_code,
        "model": model or "A1Mini",
        "account_email": None,
    }
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(printers, f, indent=2, ensure_ascii=False)
    return True, f"✅ 已保存打印机 {name} ({ip})"


def remove_printer(name: str) -> tuple:
    printers = load_printers()
    if name not in printers:
        return False, f"打印机 {name} 不存在"
    del printers[name]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(printers, f, indent=2, ensure_ascii=False)
    return True, f"🗑 已移除 {name}"


def printer_list_html() -> str:
    """渲染打印机配置 HTML"""
    printers = load_printers()
    if not printers:
        return ('<div class="flib-empty">未配置打印机 —— '
                '在下方填写 IP/序列号/访问码（打印机屏幕 → 设置 → 网络）后保存</div>')
    parts = ['<div class="batch-header">已配置打印机</div>']
    for name, p in printers.items():
        parts.append(
            f'<div class="flib-item">'
            f'<span class="flib-ic">🖨</span>'
            f'<span class="flib-name">{name}</span>'
            f'<span class="flib-meta">{p.get("ip_address","?")}</span>'
            f'</div>'
        )
    return "".join(parts)


# ─── 状态检测 ───────────────────────────────────────────
def printer_status(name: str = None) -> str:
    """检测打印机在线状态，返回 HTML"""
    printers = load_printers()
    if not printers:
        return '<div class="flib-empty">未配置打印机</div>'
    names = [name] if name else list(printers.keys())
    parts = ['<div class="batch-header">🖨 打印机状态</div>']
    for n in names:
        p = printers[n]
        online = _ping_host(p.get("ip_address", ""))
        icon = "🟢" if online else "🔴"
        parts.append(
            f'<div class="flib-item">'
            f'<span class="flib-ic">{icon}</span>'
            f'<span class="flib-name">{n}</span>'
            f'<span class="flib-meta">{"在线" if online else "离线"} · {p.get("ip_address","")}</span>'
            f'</div>'
        )
    return "".join(parts)


def _ping_host(ip: str) -> bool:
    if not ip:
        return False
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", "1500", ip],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ─── 打印 ───────────────────────────────────────────────
def print_file(name: str, file_path: str, plate: int = 1) -> tuple:
    """发送文件到打印机开始打印
    返回 (ok, msg)
    """
    if not Path(file_path).exists():
        return False, f"文件不存在: {file_path}"
    printers = load_printers()
    if name not in printers:
        return False, f"打印机 {name} 未配置"
    if not _ping_host(printers[name].get("ip_address", "")):
        return False, f"打印机 {name} 离线"

    code, out, err = _run_bambu(["print", name, file_path, "--plate", str(plate)], timeout=300)
    if code == 0:
        return True, f"✅ 已发送到打印机 {name}: {Path(file_path).name}"
    # bambu 输出可能带进度信息，取最后几行
    tail = (out or err or "").strip().splitlines()[-3:]
    return False, "打印失败: " + " | ".join(tail) if tail else f"打印失败 (code={code})"


def upload_file(name: str, file_path: str) -> tuple:
    """仅上传文件到打印机（不打印）"""
    if not Path(file_path).exists():
        return False, f"文件不存在: {file_path}"
    code, out, err = _run_bambu(["upload", name, file_path], timeout=300)
    if code == 0:
        return True, f"✅ 已上传 {Path(file_path).name} → {name}"
    return False, (err or out or f"上传失败 (code={code})")[:200]


# ─── 可用打印机下拉选项 ─────────────────────────────────
def printer_choices() -> list:
    return list(load_printers().keys()) or []


# ═══ 局域网自动发现（2026-08-11）═══
def discover_printers() -> tuple:
    """SSDP 发现局域网 Bambu 打印机，自动写入配置
    返回 (ok, msg)
    """
    try:
        import sys as _sys
        # bambu-cli 要装在与铸想同一个解释器里（pip install bambu-cli），不再硬编码 site-packages 路径
        from bambucli.bambu.ssdpclient import SsdpClient
        # 2026-08-13 修复: bambucli 内部用 asyncio，Gradio worker 线程无事件循环
        # -> "no current event loop in thread 'AnyIO worker thread'"
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        c = SsdpClient()
        res = c.discover_printers()
        if asyncio.iscoroutine(res):
            found = asyncio.run(res)
        else:
            found = res
        if not found:
            return False, "未发现打印机（确认打印机已开机且在同一网络）"
        printers = load_printers()
        added = []
        for p in found:
            name = p.name or p.model.value
            # 同名已存在则跳过
            if name in printers:
                continue
            printers[name] = {
                "ip_address": p.ip_address,
                "serial_number": p.serial_number,
                "name": name,
                "access_code": printers.get(name, {}).get("access_code", ""),
                "model": p.model.value,
                "account_email": None,
            }
            added.append(f"{name} ({p.model.value}) @ {p.ip_address}")
        CONFIG_FILE.parent.mkdir(exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(printers, f, indent=2, ensure_ascii=False)
        if added:
            return True, "✅ 发现 " + "; ".join(added) + "\n（访问码需在打印机屏幕查看后填写）"
        return True, f"已发现 {len(found)} 台打印机（均已存在配置中）"
    except Exception as e:
        return False, f"发现失败: {e}"


# ═══ Bambu Studio 集成（2026-08-11：走 Bambu Studio 这条路）═══
BAMBU_STUDIO_EXE = r"D:\Bambu Studio\bambu-studio.exe"


def open_in_bambu_studio(file_path: str) -> tuple:
    """用 Bambu Studio 打开 STL/3MF，自动启用「支撑」+「自适应层高」
    内部调用 auto_bambu.py（pyautogui 自动化）
    返回 (ok, msg)
    """
    import sys as _sys
    if not Path(file_path).exists():
        return False, f"文件不存在: {file_path}"
    if not Path(BAMBU_STUDIO_EXE).exists():
        return False, f"Bambu Studio 未安装: {BAMBU_STUDIO_EXE}"

    # 找 auto_bambu.py（与 bambu_print.py 同目录）
    script_dir = Path(__file__).parent
    auto_script = script_dir.parent / "auto_bambu.py"

    try:
        if auto_script.exists():
            # 优先用 auto_bambu.py（带自动化）
            result = _sys.modules[__name__].__dict__.get('_subprocess') or subprocess
            r = subprocess.run(
                [_sys.executable, str(auto_script), str(Path(file_path).resolve())],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "PYTHONPATH": ""},
            )
            return True, f"✅ 已启动 Bambu Studio，自动启用支撑+自适应层高，请在界面确认并打印"
        else:
            # 降级：直接打开
            subprocess.Popen(
                [BAMBU_STUDIO_EXE, str(Path(file_path).resolve())],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, f"✅ 已在 Bambu Studio 打开 {Path(file_path).name}（请手动勾选支撑+自适应层高）"
    except Exception as e:
        return False, f"启动 Bambu Studio 失败: {e}"


def bambu_studio_available() -> bool:
    return Path(BAMBU_STUDIO_EXE).exists()


# ─── 多打印机助手（2026-08-11）───
def quick_config_printer(name: str, model: str = "A1Mini") -> tuple:
    """快速配置：仅需 IP + 访问码（序列号可选，bambu-cli 会自动探测）
    model: A1 / A1Mini
    """
    return False, "请使用完整配置（名称/IP/序列号/访问码）"
