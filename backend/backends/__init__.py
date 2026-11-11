# -*- coding: utf-8 -*-
"""生成后端的选择器。

铸想原来直接 `ComfyUIClient(config)` 硬连 ComfyUI。现在多了一层：

    config.json → "backend": { "mode": "comfyui" }   # 默认，行为完全不变
    config.json → "backend": { "mode": "direct" }    # 不依赖 ComfyUI，直接调 diffusers / hy3dgen

两种实现暴露**同一组方法**（`build_workflow` / `submit_workflow` / `wait_for_completion` /
`get_result_*` / `upload_image`），所以 `pipeline.py` 一行都不用改。
见 docs/BACKENDS.md。
"""
from __future__ import annotations

MODE_COMFYUI = "comfyui"
MODE_DIRECT = "direct"


def backend_mode(config: dict) -> str:
    raw = (config or {}).get("backend") or {}
    mode = (raw.get("mode") if isinstance(raw, dict) else raw) or MODE_COMFYUI
    return str(mode).strip().lower()


def make_backend(config: dict):
    """按 config 造一个后端。默认观是 comfyui —— 不设就不改变任何现有行为。"""
    mode = backend_mode(config)
    if mode == MODE_DIRECT:
        from .direct import DirectBackend
        return DirectBackend(config)
    from ..comfy_client import ComfyUIClient
    return ComfyUIClient(config)
