# -*- coding: utf-8 -*-
"""生成后端的选择器。

铸想原来直接 `ComfyUIClient(config)` 硬连 ComfyUI。现在有三种模式：

    "backend": { "mode": "comfyui" }  # 默认，行为与以前完全一致
    "backend": { "mode": "direct" }   # 不依赖 ComfyUI，直接 diffusers / hy3dgen
    "backend": { "mode": "auto" }     # 先探测这台机器有什么，再按阶段自动路由

三种实现暴露**同一组方法**（`build_workflow` / `submit_workflow` / `wait_for_completion` /
`get_result_*` / `upload_image`），所以 `pipeline.py` 一行都不用改。

  docs/BACKENDS.md  直连模式与实测数据
  docs/PLUGINS.md   模型插件（一个 YAML 接一个模型）
"""
from __future__ import annotations

MODE_COMFYUI = "comfyui"
MODE_DIRECT = "direct"
MODE_HYBRID = "auto"

_ALIASES = {
    "comfy": MODE_COMFYUI,
    "comfyui": MODE_COMFYUI,
    "direct": MODE_DIRECT,
    "builtin": MODE_DIRECT,
    "native": MODE_DIRECT,
    "auto": MODE_HYBRID,
    "hybrid": MODE_HYBRID,
}


def backend_mode(config: dict) -> str:
    raw = (config or {}).get("backend") or {}
    if isinstance(raw, dict):
        mode = raw.get("mode") or MODE_COMFYUI
    elif isinstance(raw, str):
        mode = raw
    else:
        mode = MODE_COMFYUI
    return _ALIASES.get(str(mode).strip().lower(), MODE_COMFYUI)


def make_backend(config: dict):
    """按 config 造一个后端。默认是 comfyui —— 不设就不改变任何现有行为。"""
    mode = backend_mode(config)
    if mode == MODE_DIRECT:
        from .direct import DirectBackend
        return DirectBackend(config)
    if mode == MODE_HYBRID:
        from .hybrid import HybridBackend
        return HybridBackend(config)
    from ..comfy_client import ComfyUIClient
    return ComfyUIClient(config)
