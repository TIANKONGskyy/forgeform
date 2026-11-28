# -*- coding: utf-8 -*-
"""显存/模型并发防护（2026-11-28）。

为什么需要
----------
这台机器只有一张 32G 卡，而铸想会同时用到三类吃显存的东西：

    ComfyUI（出图/贴图/多视图）· hy3dgen 直连（建模）· 对话模型（Strata/Ollama）

用户真实会踩的：先出图（ComfyUI 占 16–24G），接着点建模 → 两头都占着 → OOM，
然后界面只报一句 "CUDA out of memory"。

这个模块做三件事：
1. **同一时刻只允许一个"重活"**（hold），后来的要么排队等，要么拿到明确提示
2. hold 前调用注册的"让出"回调（释放 ComfyUI / 卸载对话模型）
3. 记录持有者与最近一次释放原因，供 UI/日志展示

不做的事：不主动杀进程、不改 ComfyUI 内部策略 —— 只做顺序与让出。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

# 让出回调：按注册顺序调用（先释放最占显存的）
_RELEASERS: list[tuple[str, callable]] = []
_LOCK = threading.RLock()
_STATE = {"holder": None, "since": 0.0, "waiting": 0, "last_release": ""}


def register_releaser(name: str, fn) -> None:
    """注册"把显存让出来"的回调（例如 pipeline._free_vram）。"""
    with _LOCK:
        if name not in [n for n, _ in _RELEASERS]:
            _RELEASERS.append((name, fn))


def _release_all(keep: str = "") -> list[str]:
    freed = []
    for name, fn in list(_RELEASERS):
        if name == keep:
            continue
        try:
            fn()
            freed.append(name)
        except Exception:
            pass
    return freed


@dataclass
class Guard:
    """重活占用器。用 with gpu_guard.hold("image") 包住生成逻辑。"""

    kind: str
    wait_s: float = 0.0
    released: list[str] = field(default_factory=list)

    def __enter__(self):
        t0 = time.time()
        with _LOCK:
            if _STATE["holder"] and _STATE["holder"] != self.kind:
                # 有别人占着：先让对方让出（这是"顺序"，不是并行）
                if self.wait_s > 0:
                    _STATE["waiting"] += 1
                    deadline = t0 + self.wait_s
                    while _STATE["holder"] and time.time() < deadline:
                        time.sleep(0.2)
                    _STATE["waiting"] -= 1
                self.released = _release_all(keep=self.kind)
                if self.released:
                    _STATE["last_release"] = f"{time.time():.0f} 释放 {'/'.join(self.released)} 给 {self.kind}"
            _STATE["holder"] = self.kind
            _STATE["since"] = time.time()
        return self

    def __exit__(self, *exc):
        with _LOCK:
            if _STATE["holder"] == self.kind:
                _STATE["holder"] = None
        return False


def hold(kind: str, wait_s: float = 0.0) -> Guard:
    """占用一个"重活"槽位；kind 形如 image / mesh / paint / rig / motion。"""
    return Guard(kind, wait_s)


@contextmanager
def exclusive(kind: str, wait_s: float = 0.0):
    with hold(kind, wait_s):
        yield


def status() -> dict:
    with _LOCK:
        return {
            "holder": _STATE["holder"],
            "busy_s": round(time.time() - _STATE["since"], 1) if _STATE["holder"] else 0,
            "waiting": _STATE["waiting"],
            "last_release": _STATE["last_release"],
            "releasers": [n for n, _ in _RELEASERS],
        }


def busy_message() -> str:
    st = status()
    if not st["holder"]:
        return ""
    return (f"显卡正被「{st['holder']}」占着（已 {st['busy_s']:.0f}s）。"
            f"等它跑完再试；如果卡住了，重启一次铸想会释放。")


def guarded(kind: str, wait_s: float = 0.0):
    """装饰器：把函数体包进 hold(kind)，退出自动释放。"""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            with hold(kind, wait_s):
                return fn(*a, **k)

        wrapper._ff_guarded = True
        wrapper._ff_kind = kind
        return wrapper
    return deco
