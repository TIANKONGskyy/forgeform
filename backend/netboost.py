# -*- coding: utf-8 -*-
"""网络加速：检测 Steam++（Watt Toolkit），并在下载慢时提供一键开启。

## 为什么需要

国内直连 GitHub Release 实测 **0.17 MB/s**（512 KB 用了 3 秒），
模型动辄 3–9 GB，按这个速度要几小时。走 `gh-proxy.com` 反代能到 0.34 MB/s，
但大文件仍然慢。Steam++（Watt Toolkit）的 GitHub 加速能到几 MB/s。

## 设计取舍

**不自动装。** 装机器的软件不经用户同意就静默安装是不对的，也容易触发安全软件。
所以：
  · 本机已装 → 提供"启动"按钮（一条命令）
  · 没装 → 给下载链接，让用户自己决定
  · 下载完成后**不自动启动**，只弹一次提示，用户点才启

检测不依赖注册表（Steam++ 有便携版），直接看几个常见路径 + 进程。
"""
from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

# 常见安装位置（按可能性排序）
CANDIDATES = [
    r"C:\Program Files\Steam++\Steam++.exe",
    r"C:\Program Files (x86)\Steam++\Steam++.exe",
    r"C:\Program Files\SteamTools\SteamTools.exe",
    r"C:\Program Files (x86)\SteamTools\SteamTools.exe",
]
USER_HINTS = [
    Path.home() / "AppData" / "Local" / "Steam++",
    Path.home() / "AppData" / "Roaming" / "Steam++",
]
DOWNLOAD_URL = "https://steampp.net/"
PROCESS_NAMES = ("Steam++", "SteamTools")


def find_install() -> str:
    """返回可执行文件路径，找不到返回空串。"""
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    # 便携版常见位置：用户目录下任意 Steam++ 目录里的 exe
    for base in (Path.home() / "AppData" / "Local", Path.home() / "Desktop", Path("D:/")):
        try:
            for p in base.glob("**/Steam++*.exe"):
                return str(p)
        except Exception:
            continue
    return ""


def is_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        low = (out.stdout or "").lower()
        return any(n.lower() in low for n in PROCESS_NAMES)
    except Exception:
        return False


def detect() -> dict:
    path = find_install()
    return {
        "installed": bool(path),
        "path": path,
        "running": is_running(),
        "download_url": DOWNLOAD_URL,
    }


def launch() -> dict:
    """启动 Steam++。不等待它初始化完成（那要几十秒）。"""
    info = detect()
    if not info["installed"]:
        return {"ok": False, "error": "这台机器上没有检测到 Steam++", **info}
    if info["running"]:
        return {"ok": True, "already": True, **info}
    try:
        subprocess.Popen(
            [info["path"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": True, "launched": True, **info}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", **info}


def speed_test(url: str, bytes_to_read: int = 512 * 1024, timeout: int = 15) -> dict:
    """读一小段估算速度。失败返回 ok=False，不抛异常。"""
    import urllib.error

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                chunk = r.read(bytes_to_read)
            dt = max(time.time() - t0, 1e-6)
            mbps = len(chunk) / 1024 / 1024 / dt
            return {"ok": True, "mbps": round(mbps, 2), "bytes": len(chunk),
                    "sec": round(dt, 2)}
        except Exception as exc:
            if attempt == 1:
                return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            time.sleep(1)
    return {"ok": False, "error": "未知"}


# 低于这个速度就算"慢"，提示用户开加速
SLOW_THRESHOLD_MBPS = 0.6


def advise() -> dict:
    """给界面用的一句话判断：当前下载链路要不要提示开加速。"""
    probe = "https://gh-proxy.com/https://github.com/TIANKONGskyy/forgeform/releases/download/v3.1.2/forgeform-v3.1.2.zip"
    st = speed_test(probe)
    boost = detect()
    slow = bool(st.get("ok") and st["mbps"] < SLOW_THRESHOLD_MBPS)
    return {
        "slow": slow,
        "threshold_mbps": SLOW_THRESHOLD_MBPS,
        "speed": st,
        "steampp": boost,
        "advice": (
            "下载速度偏慢，模型有 3–9 GB，可能要很久。开启 Steam++ 的 GitHub 加速通常会快很多。"
            if slow else
            "下载速度正常。"
        ),
    }
