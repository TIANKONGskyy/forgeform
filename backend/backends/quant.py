# -*- coding: utf-8 -*-
"""显存自适应：同一份配置在 8G / 16G / 24G / 32G 卡上都能跑起来。

铸想要在"各个主流电脑"上跑，就不能假定用户有 32 GB 显存。这里的思路：

    先量出「这张卡现在还剩多少显存」+「这个模型要多少」
    再挑一种放得下的加载方式：原样 → 8bit → 4bit → CPU offload

`quant: auto`（默认）走上面这条链；也可以强制某一档来复现问题。

设计取舍：
  · **不按显卡型号猜**。同一张卡在不同系统配置下可用显存差很多，只信 nvidia-smi 的实测值。
  · **优先保住"整块进显存"**。offload 每次换入换出，实测 32 GB 模型在 32 GB 卡上
    第一步要 69 秒、之后每步 4-8 秒；能整块放下就快 3-4 倍。
  · **量化优先 8bit 而不是 4bit**。8bit 对画质几乎无感，4bit 开始能看出来；
    只有 8bit 也放不下才退 4bit。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GB = 1024 ** 3


# ────────────────────────────────────────────────────────────────
# 硬件探测
# ────────────────────────────────────────────────────────────────
def probe_vram() -> tuple[float, float, str]:
    """返回 (总显存 GB, 空闲显存 GB, 卡名)。没有 NVIDIA 卡时返回 (0, 0, '')。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return 0.0, 0.0, ""
        parts = [p.strip() for p in line[0].split(",")]
        name = parts[0] if parts else ""
        total = float(parts[1]) / 1024 if len(parts) > 1 and parts[1].isdigit() else 0.0
        free = float(parts[2]) / 1024 if len(parts) > 2 and parts[2].isdigit() else 0.0

        # 多卡时取空闲最多的那张
        for extra in line[1:]:
            p = [x.strip() for x in extra.split(",")]
            if len(p) > 2 and p[2].isdigit():
                f = float(p[2]) / 1024
                if f > free:
                    free, name = f, p[0]
        return total, free, name
    except Exception:
        return 0.0, 0.0, ""


def torch_free_vram() -> float:
    """PyTorch 视角的可用显存（比 nvidia-smi 更准，不含其他进程的缓存）。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        free, total = torch.cuda.mem_get_info()
        return free / GB
    except Exception:
        return 0.0


def estimate_model_gb(ref: str) -> float:
    """估一个模型要占多少显存（GB）。

    本地目录 → 权重文件之和（目录里可能还有 readme/图，只数权重后缀）。
    单个文件 → 文件大小。
    HF repo id → 按参数量从名字里猜，猜不到就返回 0（调用方按保守档处理）。
    """
    if not ref:
        return 0.0
    p = Path(ref)
    if p.exists():
        if p.is_file():
            return p.stat().st_size / GB
        total = 0
        for ext in ("*.safetensors", "*.bin", "*.gguf", "*.ckpt", "*.pt"):
            for f in p.rglob(ext):
                total += f.stat().st_size
        return total / GB

    # 名字里带参数量的 repo（4b / 9b / 20b / 125b）
    import re
    low = ref.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*b(?![a-z])", low)
    if m:
        params = float(m.group(1))
        return params * 2 / 1.0 * 0.9   # bf16 ≈ 2 字节/参数，0.9 是经验系数
    return 0.0


def disk_free_gb(path: str) -> float:
    try:
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(str(probe)).free / GB
    except Exception:
        return 0.0


# ────────────────────────────────────────────────────────────────
# 决策
# ────────────────────────────────────────────────────────────────
# 量化后大致占原来多少（含 text_encoder/vae，它们通常不量化）
QUANT_RATIO = {
    "none": 1.00,
    "fp8": 0.55,
    "bnb8": 0.58,
    "bnb4": 0.35,
}
# 放得下的判定：给激活值、attention 峰值留点余量
SAFE_RATIO = 0.82


def _bnb_available() -> bool:
    try:
        import bitsandbytes  # noqa: F401
        return True
    except Exception:
        return False


def _gguf_ref(ref: str) -> bool:
    return str(ref).lower().endswith(".gguf")


def choose_quant(prefer: str, est_gb: float, free_gb: float) -> dict:
    """挑一种加载方式。返回 dict(quant=..., offload=..., why=...)。

    `prefer` 是用户/插件显式指定的：auto / none / fp8 / bnb8 / bnb4 / gguf。
    """
    prefer = (prefer or "auto").lower()
    have_bnb = _bnb_available()

    # GGUF 单文件：只能走 from_single_file + GGUF 量化
    if prefer == "gguf":
        return {"quant": "gguf", "offload": "", "why": "指定 GGUF"}
    if not est_gb:
        # 估不出来：保守走 bnb8（有就量化，没有就原样）
        if prefer == "auto" and have_bnb:
            return {"quant": "bnb8", "offload": "", "why": "模型大小未知，保守用 8bit"}
        return {"quant": ("none" if prefer == "auto" else prefer), "offload": "",
                "why": "模型大小未知"}

    budget = free_gb * SAFE_RATIO
    if prefer != "auto":
        need = est_gb * QUANT_RATIO.get(prefer, 1.0)
        offload = "" if need <= budget else "model"
        return {"quant": prefer, "offload": offload,
                "why": f"指定 {prefer}：需约 {need:.1f} GB / 可用 {free_gb:.1f} GB"}

    # auto：从最保真往下试。
    # ⚠️ 故意**不自动选 bnb**：实测 bitsandbytes 8bit 量化 Flux2Transformer2DModel 后
    # 出图是纯噪声（不报错、速度还更快，所以特别坑）。bnb 只保留为显式选项，
    # 想试就写 quant: bnb8，但别指望它对所有模型都成立。
    for q in ("none",):
        need = est_gb * QUANT_RATIO[q]
        if need <= budget:
            return {"quant": q, "offload": "",
                    "why": f"{q}：约 {need:.1f} GB ≤ 预算 {budget:.1f} GB"}

    # 整块放不下 → CPU offload（慢，但结果是对的）
    return {"quant": "none", "offload": "model",
            "why": f"显存不够（模型 {est_gb:.1f} GB > 预算 {budget:.1f} GB），改用 CPU offload"}


def summary(est_gb: float, free_gb: float, decision: dict) -> str:
    return (f"[quant] 模型约 {est_gb:.1f} GB，显存可用 {free_gb:.1f} GB → "
            f"quant={decision['quant']} offload={decision['offload'] or 'none'} "
            f"（{decision['why']}）")
