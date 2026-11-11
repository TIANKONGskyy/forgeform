"""GPU 互斥状态：对话（Strata 8091）与出图/建模（ComfyUI 8188）不能同时占显存。

只读探测，不启动/停止任何服务：
- ComfyUI 队列有 running/pending → 正在出图/建模
- Strata /status 的 busy/queued → 正在对话
"""
from __future__ import annotations

import json
import time
import urllib.request

STRATA = "http://127.0.0.1:8091"
COMFY = "http://127.0.0.1:8188"


def _get(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def strata_alive() -> bool:
    return _get(f"{STRATA}/v1/models") is not None


def strata_busy() -> bool:
    d = _get(f"{STRATA}/status") or {}
    return bool(d.get("busy") or d.get("queued"))


def comfy_busy() -> bool:
    d = _get(f"{COMFY}/queue") or {}
    return bool(d.get("queue_running") or d.get("queue_pending"))


def is_generating() -> bool:
    """出图/建模进行中（ComfyUI 在跑）。"""
    return comfy_busy()


def is_chatting() -> bool:
    """Strata 正在回答（含排队）。"""
    return strata_alive() and strata_busy()


def state() -> str:
    if is_generating():
        return "generating"
    if is_chatting():
        return "chatting"
    return "idle"


def wait_chat_idle(timeout: float = 30.0) -> bool:
    """等当前对话回答完；超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_chatting():
            return True
        time.sleep(0.5)
    return not is_chatting()
