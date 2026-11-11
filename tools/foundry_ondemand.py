"""铸想常驻在线 / 模型按需加载。

架构：
  browser -> 127.0.0.1:7860 (本代理，常驻)
              -> 启动 Gradio(7861)，界面始终在线
              -> ComfyUI(8188) 进程也在线，但不预加载任何模型
  模型只在用户提交问题 / 点生成时，由 app.py / conversation.py 懒加载。
  这里**不再按空闲时间杀后端**；否则用户看到的就是"铸想离线"。
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROXY_PORT = 7860
APP_PORT = 7861
COMFY_PORT = 8188
IDLE_SECONDS = 0   # 0 = 不自动关闭；界面常驻在线，模型按需加载

WORKSHOP = Path(__file__).resolve().parent.parent
LOG = WORKSHOP / "logs" / "foundry-ondemand.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

PY_APP = sys.executable
PY_COMFY = os.environ.get("COMFY_PYTHON") or sys.executable

app_proc: subprocess.Popen | None = None
comfy_proc: subprocess.Popen | None = None
managed = False
starting = False
last_activity = 0.0
active_clients = 0
lock = asyncio.Lock()
comfy_lock = asyncio.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


async def wait_port(port: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        await asyncio.sleep(1.0)
    return False


def start_proc(exe: str, args: list[str], cwd: str, env: dict | None = None) -> subprocess.Popen:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    out = open(LOG, "a", encoding="utf-8", errors="replace")
    return subprocess.Popen([exe, *args], cwd=cwd, env=env,
                            stdout=out, stderr=subprocess.STDOUT,
                            creationflags=flags)


async def _start_comfy_later() -> None:
    """Gradio 先上线；ComfyUI 进程随后启动，但不预热任何模型。"""
    global comfy_proc
    await asyncio.sleep(6)
    if port_open(COMFY_PORT):
        log("ComfyUI already running")
        return
    async with comfy_lock:
        if port_open(COMFY_PORT):
            return
        log("start ComfyUI 8188 (no model preload)")
        cenv = os.environ.copy()
        cenv["PYTHONIOENCODING"] = "utf-8"
        cenv["PYTHONUNBUFFERED"] = "1"
        comfy_proc = start_proc(
            PY_COMFY,
            ["main.py", "--port", str(COMFY_PORT),
             "--disable-cuda-malloc", "--use-pytorch-cross-attention"],
            os.environ.get("COMFYUI_DIR", str(Path(PY_COMFY).parent.parent)),
            cenv,
        )
        if not await wait_port(COMFY_PORT, 150):
            log("ComfyUI failed to listen in 150s (Gradio stays online)")


async def ensure_backend() -> bool:
    global app_proc, managed, starting, last_activity
    if port_open(APP_PORT):
        last_activity = time.time()
        return True
    async with lock:
        if port_open(APP_PORT):
            last_activity = time.time()
            return True
        starting = True
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = ""
            env["PYTHONIOENCODING"] = "utf-8"
            env["FOUNDRY_PORT"] = str(APP_PORT)
            log("start Gradio 7861 (always online, models lazy)")
            app_proc = start_proc(PY_APP, ["app.py"], str(WORKSHOP), env)
            if not await wait_port(APP_PORT, 120):
                log("Gradio failed to listen in 120s")
                return False
            managed = True
            last_activity = time.time()
            asyncio.create_task(_start_comfy_later())
            log("Gradio online")
            return True
        finally:
            starting = False


def kill_tree(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass


def stop_backend(reason: str) -> None:
    global app_proc, comfy_proc, managed
    if not managed:
        return
    log(f"stop backend: {reason}")
    kill_tree(app_proc)
    kill_tree(comfy_proc)
    app_proc = comfy_proc = None
    managed = False


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global last_activity
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            last_activity = time.time()
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global last_activity, active_clients
    active_clients += 1
    try:
        ok = await ensure_backend()
        if not ok:
            try:
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n\r\nFoundry failed to start, check logs")
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return
        try:
            br, bw = await asyncio.open_connection("127.0.0.1", APP_PORT)
        except Exception as e:
            log(f"connect backend failed: {e}")
            writer.close()
            return
        last_activity = time.time()
        await asyncio.gather(pump(reader, bw), pump(br, writer))
    finally:
        active_clients -= 1


async def idle_loop() -> None:
    if IDLE_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(5)
        if (managed and active_clients == 0 and last_activity
                and (time.time() - last_activity) > IDLE_SECONDS):
            stop_backend(f"idle {IDLE_SECONDS}s")


async def main() -> None:
    log(f"proxy listening 0.0.0.0:{PROXY_PORT} -> {APP_PORT} (keep-online, lazy models)")
    server = await asyncio.start_server(handle, "0.0.0.0", PROXY_PORT)
    asyncio.create_task(idle_loop())
    # 开机就把 Gradio 拉起来；不预热任何模型。
    asyncio.create_task(ensure_backend())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        stop_backend("proxy interrupted")
