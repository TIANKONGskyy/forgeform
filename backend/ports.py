# -*- coding: utf-8 -*-
"""端口冲突的统一处理：**先分清"占着的是自己人还是别人"**。

## 之前为什么会出问题（实测）

    pid 43624  app.py   → 7860
    pid 45684  app.py   → 7862   ← 第二个实例，自动换端口活下来的
    studio.py   认为 app 在 7860

双击第二次时，`app.py` 发现 7860 被占，就自动换到 7862 **继续跑**。
于是同一个应用有两份，各自加载一遍模型 —— 显存被劈成两半，
用户看到的是"越来越慢、偶尔 OOM"，但界面上一切正常。

**判断"是不是自己人"不能只看端口通不通**，要看对面是谁。所以这里靠
HTTP 内容签名（页面里有"铸想/ForgeForm"、或返回 studio 的 API 结构）区分：

    自己人占着 → 不要开新实例，告诉他"已经在跑"并退出
    别人占着   → 换一个空闲端口继续

## 另一个坑

杀进程不能"按端口杀" —— 端口可能是同事的 nginx。这里按
**命令行里是否含我们的脚本名 + 我们的仓库路径** 来认，认出才杀。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 我方页面的签名（app 的 Gradio 页 / studio 的 SPA / api 的 health 都能命中）
_SIGNATURES = ("铸想", "ForgeForm", "forgeform", "ZYKT", "studio.js", "STUDIO_TOKEN")


def is_free(host: str, port: int, timeout: float = 0.35) -> bool:
    """端口空不空。Windows 的 SO_REUSEADDR 会让 bind 假成功，所以用 connect 判。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) != 0


def who_is(port: int, host: str = "127.0.0.1", timeout: float = 2.5) -> dict:
    """看占着这个端口的是谁。

    返回 {"busy": bool, "ours": bool, "kind": 'app'|'studio'|'api'|'other', "hint": str}
    """
    if is_free(host, port):
        return {"busy": False, "ours": False, "kind": "free", "hint": ""}
    base = f"http://{host}:{port}"
    body = ""
    # 1) studio 有 JSON 状态接口，最可靠
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if isinstance(d, dict) and "job" in d:
            return {"busy": True, "ours": True, "kind": "studio",
                    "hint": f"已有 Studio 在 {port} 运行"}
    except Exception:
        pass
    # 2) api_server 的免鉴权探活
    try:
        with urllib.request.urlopen(base + "/v1/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if isinstance(d, dict) and d.get("ok") and "queue" in d:
            return {"busy": True, "ours": True, "kind": "api",
                    "hint": f"已有 API 服务在 {port} 运行"}
    except Exception:
        pass
    # 3) 首页签名（app 的 Gradio 页、或者是别人的网站）
    try:
        with urllib.request.urlopen(base + "/", timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "ignore")
    except Exception:
        body = ""
    if body and any(s in body for s in _SIGNATURES):
        return {"busy": True, "ours": True, "kind": "app",
                "hint": f"已有创作空间在 {port} 运行"}
    return {"busy": True, "ours": False, "kind": "other",
            "hint": f"端口 {port} 被别的程序占用"}


def pick_free(preferred: int, span: int = 20, host: str = "127.0.0.1") -> int:
    """从 preferred 开始找一个空闲端口；都占了就返回 0。"""
    for p in range(preferred, preferred + max(1, span)):
        if is_free(host, p):
            return p
    return 0


def _our_pids(markers: tuple[str, ...]) -> list[int]:
    """按"命令行里有一个参数的文件名等于我们的脚本"来认进程。

    ★ 这里踩过两次坑，都是致命的：

    ① 用 `ConvertTo-Json` 拿进程列表 —— 中文 Windows 上 PowerShell 的输出编码
       和 Python 的 utf-8 解码打架，函数静默返回空，该杀的没杀。
    ② 改成"整条命令行包含 app.py 就算" —— 会把**调用方自己**认进去：
       一个 `python -c "...kill_stale('app.py')..."` 的命令行里就含 `app.py`，
       于是它把自己杀了，把上层 job 也带崩（实测 exit code 1）。

    所以现在：把命令行拆成参数，**比较参数的 basename**，且排除自己和父进程。
    """
    out: list[int] = []
    me = os.getpid()
    parent = 0
    try:
        parent = os.getppid()
    except Exception:
        pass
    root = str(ROOT).lower().replace("\\", "/")

    def _match(cmd: str, cwd: str = "") -> bool:
        """命令行里有"文件名等于我们脚本"的参数，且(命令行含仓库路径 或 工作目录在仓库里)。

        为什么要 cwd：Studio 常常被用**相对路径**启动（`tools/studio.py`），
        命令行里根本没有仓库绝对路径，只按路径匹配会漏掉它（实测漏了，
        于是一个旧 Studio 一直占着 7861，新起的那个以为自己该复用）。
        """
        low = cmd.lower().replace("\\", "/")
        cwd_l = str(cwd or "").lower().replace("\\", "/")
        parts = cmd.replace("/", "\\").split()
        names = {os.path.basename(x.strip('"').lower()) for x in parts}
        if not any(m.lower() in names for m in markers):
            return False
        return (root in low) or ("forgeform" in low) or (root in cwd_l)

    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                pid = int(proc.info["pid"])
                if pid in (me, parent):
                    continue
                cmdline = proc.info.get("cmdline") or []
                cwd = proc.cwd() if hasattr(proc, "cwd") else ""
            except Exception:
                continue
            if not cmdline:
                continue
            # 别把 python -c "..." 当成我们的脚本
            if "-c" in cmdline:
                continue
            if _match(" ".join(cmdline), cwd):
                out.append(pid)
        return out
    except Exception:
        pass

    try:
        script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                  "| Select-Object ProcessId,CommandLine | Format-Table -HideTableHeaders "
                  "-AutoSize | Out-String -Width 400")
        ps = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (ps.stdout or b"").decode("utf-8", "replace")
        if "app.py" not in raw and "studio.py" not in raw:
            raw = (ps.stdout or b"").decode("gbk", "replace")
        for line in raw.splitlines():
            m = re.match(r"\s*(\d+)\s+(.*)", line)
            if not m:
                continue
            pid, cmd = int(m.group(1)), m.group(2).strip()
            if pid in (me, parent) or "-c " in cmd:
                continue
            if _match(cmd):
                out.append(pid)
    except Exception:
        pass
    return out


def kill_stale(script: str, keep_pid: int = 0, wait: float = 3.0) -> list[int]:
    """杀掉我们自己的旧实例（同脚本、同仓库、不是当前进程）。"""
    killed = []
    for pid in _our_pids((script,)):
        if pid in (keep_pid, 0):
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed.append(pid)
        except Exception:
            pass
    if killed:
        time.sleep(wait)
    return killed


def resolve(preferred: int, script: str = "", span: int = 20,
            host: str = "127.0.0.1") -> dict:
    """入口统一调用：给出"该用哪个端口"，或"别开了"。

    返回 {"action": 'use'|'reuse'|'fail', "port": int, "why": str}
    """
    info = who_is(preferred, host)
    if not info["busy"]:
        return {"action": "use", "port": preferred, "why": "端口空闲"}
    if info["ours"]:
        # 自己人占着 → 别再开一份（这会劈开显存，实测过）
        return {"action": "reuse", "port": preferred,
                "why": info["hint"] + "，直接用它"}
    # 别人占着 → 换端口
    port = pick_free(preferred + 1, span, host)
    if not port:
        return {"action": "fail", "port": 0,
                "why": f"{preferred}~{preferred + span} 全被占用"}
    return {"action": "use", "port": port,
            "why": f"{preferred} 被别人占用（{info['hint']}），改用 {port}"}
