"""Best-effort local view suggestions; uncertainty is never silently discarded.

## 来源
2026-11-26 从 Codex 那边并进开源铸想（`backend/view_classifier.py`）。
配合 `Pipeline.generate_3d_multiview()` 使用：用户传 2~8 张图，
这里给出 front/back/left/right 建议；**判不准的一律进 unresolved，绝不瞎猜**——
因为四视图顺序错了，重建结果会直接歪掉（实测：只错一张仰角，表面误差从 1.13mm 涨到 2.48mm）。

## 模型
默认走本机 Ollama（`http://127.0.0.1:11434/api/chat`，模型 `qwen3.8:latest`）。
换模型/换端口用环境变量：
    FORGEFORM_VIEW_VLM_MODEL   模型名（默认 qwen3.8:latest）
    FORGEFORM_VIEW_VLM_URL     Ollama 根地址（默认 http://127.0.0.1:11434）
"""
import base64
import io
import json
import os
from pathlib import Path

import requests
from PIL import Image, ImageOps

VIEWS = ("front", "back", "left", "right")
ALLOWED = set(VIEWS) | {"top", "bottom", "oblique", "unknown"}

OLLAMA_URL = os.environ.get("FORGEFORM_VIEW_VLM_URL", "http://127.0.0.1:11434").rstrip("/")
VLM_MODEL = os.environ.get("FORGEFORM_VIEW_VLM_MODEL", "qwen3.8:latest")


def _prepare_image(path):
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((768, 768))
        data = io.BytesIO()
        img.save(data, format="JPEG", quality=88)
        return base64.b64encode(data.getvalue()).decode("ascii")


def classify_image(path, session=requests, model=None):
    """Classify one view using an installed local VLM; result is only a suggestion."""
    model = model or VLM_MODEL
    prompt = (
        "Classify the camera view of the main object in this image. "
        "Choose exactly one of front, back, left, right, top, bottom, oblique, unknown. "
        "Front/back mean looking toward the object's front/back, not its position in the image. "
        "For a person or animal left/right mean the subject's anatomical side. "
        "For vehicles front is the nose, back is the tail. Use oblique for a diagonal view. "
        "If it cannot be determined from the image choose unknown. "
        "Return JSON only: {\"view\":\"...\",\"confidence\":0.0}."
    )
    response = session.post(f"{OLLAMA_URL}/api/chat", json={
        "model": model, "messages": [{"role": "user", "content": prompt,
                                     "images": [_prepare_image(path)]}],
        "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "num_predict": 120},
    }, timeout=240)
    response.raise_for_status()
    result = json.loads(response.json().get("message", {}).get("content", "{}"))
    view = str(result.get("view", "unknown")).strip().lower()
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"view": view if view in ALLOWED else "unknown", "confidence": confidence}


def assign_views(paths, classifier=classify_image):
    """Return complete view suggestions or unresolved inputs with reasons."""
    paths = [str(Path(p)) for p in paths if p]
    if not paths:
        raise ValueError("请上传至少一张图片")
    if len(paths) > 8:
        raise ValueError("一次最多上传 8 张图片")
    if len(paths) == 1:
        return {"front": paths[0]}, [], []
    suggestions, unresolved, results = {}, [], []
    for path in paths:
        result = classifier(path)
        view = result["view"]
        results.append({"path": path, **result})
        if view not in VIEWS or result["confidence"] < 0.7:
            unresolved.append(path)
        elif view in suggestions:
            # Two pictures cannot both occupy a direction: neither is chosen.
            unresolved.extend([suggestions.pop(view), path])
        else:
            suggestions[view] = path
    # The current Comfy3D list node loses labels if earlier slots are empty.
    present = [key for key in VIEWS if key in suggestions]
    expected = list(VIEWS[:len(present)])
    if not unresolved and present != expected:
        # ★ 2026-11-26 修：原来这里返回 ({}, []) —— 视图有空洞（例如只认出
        #   front/left/right，缺 back）时，调用方拿到的既没有建议也没有原因，
        #   界面上就是"点了没反应"。现在把空洞之后的视图都标成待确认，
        #   调用方至少能提示"请补背视图"。
        #   注意只在 unresolved 为空时判断：重复/低置信已经给出原因了，
        #   再叠一层会把无关的图也列进去。
        hole = next((k for k in expected if k not in present), None)
        if hole:
            later = VIEWS[VIEWS.index(hole):]
            unresolved.extend(suggestions[k] for k in present if k in later)
    if unresolved:
        return {}, sorted(set(unresolved)), results
    return suggestions, [], results
