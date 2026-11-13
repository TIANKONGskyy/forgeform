# -*- coding: utf-8 -*-
"""把参考图处理成"适合重建"的样子，再喂给 3D 模型。

## 为什么需要这一步

Meshy / Tripo / 腾讯混元在做图生 3D 之前都会先过一道图像预处理，原因很实在：

1. **背景会被当成几何重建出来** —— 我们实测过：产品摄影的灰底 + 地面阴影，
   2mini 会一起重建，模型底下多一块平板（见 docs/MODEL-BENCH.md）。
   提示词里加 "white background" 能减轻，但**去背景（rembg）才是彻底的**。
2. **主体占画面的比例决定有效分辨率** —— 一张 1024² 的图，主体只占中间 40%，
   等于给模型看了 400px 的细节。裁掉空白再放大，同样 1024² 里主体占满。
3. **边缘要清晰** —— octree 是靠图像特征推轮廓的，边缘糊 → 轮廓糊。

## 做什么

    rembg 去背景 → 按 alpha 紧裁 → 居中补成正方形 → unsharp 锐化 → 输出 PNG

不做超分：`realesrgan` 没装，而且实测把 512 放大到 1024 对 2mini 没帮助
（它内部还是缩到 512），真正有用的是"去背景 + 主体占满"。
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def _remove_bg(img: Image.Image) -> Image.Image:
    """rembg 去背景。失败就原样返回（不能因为去背景失败就不建模）。"""
    try:
        from rembg import remove
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = remove(buf.getvalue())
        return Image.open(io.BytesIO(out)).convert("RGBA")
    except Exception:
        return img.convert("RGBA")


def _tight_crop(img: Image.Image, pad_ratio: float = 0.04) -> Image.Image:
    """按 alpha 通道紧裁，留一点边。"""
    alpha = np.array(img.split()[-1])
    ys, xs = np.where(alpha > 16)
    if len(xs) == 0 or len(ys) == 0:
        return img
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    pad = int(max(x1 - x0, y1 - y0) * pad_ratio)
    box = (max(0, x0 - pad), max(0, y0 - pad),
           min(img.width, x1 + pad), min(img.height, y1 + pad))
    return img.crop(box)


def _square(img: Image.Image, size: int = 1024) -> Image.Image:
    """居中补成正方形（透明底），再缩放到 size。"""
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def _sharpen(img: Image.Image, amount: float = 1.4) -> Image.Image:
    """unsharp mask。只作用于 RGB，alpha 不动。"""
    rgb = img.convert("RGB").filter(
        ImageFilter.UnsharpMask(radius=2, percent=int(amount * 100), threshold=3))
    out = rgb.convert("RGBA")
    out.putalpha(img.split()[-1])
    return out


def prepare(img_path: str, out_path: str | None = None, size: int = 1024,
            do_remove_bg: bool = True, do_sharpen: bool = True) -> dict:
    """跑完整预处理，返回 {ok, path, steps, before, after}。

    任何一步失败都降级继续 —— 建模不能因为预处理失败就中断。
    """
    src = Path(img_path)
    if not src.exists():
        return {"ok": False, "error": f"图片不存在: {img_path}"}

    steps = []
    img = Image.open(src).convert("RGBA")
    before = img.size

    if do_remove_bg:
        got = _remove_bg(img)
        if np.array(got.split()[-1]).min() < 250:
            img = got
            steps.append("去背景")
        else:
            steps.append("去背景（无变化）")

    cropped = _tight_crop(img)
    if cropped.size != img.size:
        img = cropped
        steps.append(f"紧裁 {before[0]}×{before[1]} → {img.width}×{img.height}")

    if max(img.size) != size:
        img = _square(img, size)
        steps.append(f"补方+缩放到 {size}²")

    if do_sharpen:
        img = _sharpen(img)
        steps.append("锐化")

    dst = Path(out_path) if out_path else src.with_name(src.stem + "_prep.png")
    img.save(dst)
    return {"ok": True, "path": str(dst), "steps": steps,
            "before": list(before), "after": list(img.size)}


def prepare_inplace(img_path: str, **kw) -> dict:
    """预处理后写回一个同目录的 `_prep.png`，原图不动。"""
    return prepare(img_path, **kw)
