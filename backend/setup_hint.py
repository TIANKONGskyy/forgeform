# -*- coding: utf-8 -*-
"""首屏环境提示条（2026-11-28）。

用户装完解压、直接 `python app.py`，此时往往**一个模型都没有**，
但界面上看不出"少了什么、该跑哪条命令"，只能去点生成然后撞一堵墙。

这里做一个**纯本地、零网络**的检查（只看文件是否存在，不连 ComfyUI），
在聊天区上方输出一条提示；一切就绪时整条隐藏，不打扰。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _hy3dgen_ok(cfg: dict) -> bool:
    slot = (((cfg.get("models") or {}).get("3d") or {}).get("hunyuan_mini") or {})
    p = slot.get("path") if isinstance(slot, dict) else None
    if not p:
        return False
    return (Path(p) / "config.json").is_file() or (Path(p) / "model.fp16.safetensors").is_file()


def _image_ok(cfg: dict) -> bool:
    """任一图像模型的 checkpoint 落地就算"有出图"（原来只认 z_image）。"""
    paths = (cfg.get("paths") or {})
    comfy = str(paths.get("comfyui") or "")
    if not comfy or "${" in comfy:
        return False
    base = Path(comfy) / "models"
    img = ((cfg.get("models") or {}).get("image") or {})
    for _key, slot in img.items():
        if not isinstance(slot, dict):
            continue
        ckpt = str(slot.get("checkpoint") or "").strip()
        if not ckpt:
            continue
        for sub in ("diffusion_models", "unet", "checkpoints", "clip"):
            if (base / sub / ckpt).is_file():
                return True
    return False


def status() -> dict:
    cfg = _cfg()
    return {"mesh": _hy3dgen_ok(cfg), "image": _image_ok(cfg)}


def banner_html() -> str:
    """返回提示条 HTML；全都就绪时返回空串。"""
    st = status()
    if st["mesh"] and st["image"]:
        return ""

    missing = []
    if not st["mesh"]:
        missing.append("建模权重")
    if not st["image"]:
        missing.append("出图模型")

    cmds = []
    if not st["mesh"]:
        cmds.append("python tools/bootstrap_models.py --only direct")
    if not st["image"]:
        cmds.append("python tools/bootstrap_models.py --only image")

    cmd_line = "python tools/first_run.py  " + ("（连出图一起装：--with-image）" if not st["image"] else "")
    return (
        '<div class="ff-setup-hint">'
        '<div class="ff-setup-hint-title">还差：' + "、".join(missing) + "</div>"
        '<div class="ff-setup-hint-body">在项目目录执行：'
        '<code>' + cmd_line + "</code>"
        "（第一次约 2–8 分钟，之后就一直可用）</div>"
        '<div class="ff-setup-hint-foot">已经装了却没识别到？跑 '
        "<code>python tools/doctor.py --fix</code> 再刷新本页。</div>"
        "</div>"
    )


def is_ready() -> bool:
    st = status()
    return bool(st["mesh"] and st["image"])
