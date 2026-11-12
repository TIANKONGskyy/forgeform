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
                    total, free, name = float(p[1]) / 1024, f, p[0]
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


def _fp8_available() -> bool:
    """diffusers 的 PipelineQuantizationConfig 支持 fp8 吗。"""
    try:
        import diffusers
        from diffusers.models.modeling_utils import ModelMixin
        import torch
        return hasattr(ModelMixin, "enable_layerwise_casting") and hasattr(torch, "float8_e4m3fn")
    except Exception:
        return False


def _gguf_ref(ref: str) -> bool:
    return str(ref).lower().endswith(".gguf")


def choose_quant(prefer: str, est_gb: float, free_gb: float,
                 allow_bnb: bool = True, allow_fp8: bool = True) -> dict:
    """挑一种加载方式。返回 dict(quant=..., offload=..., why=...)。

    `prefer` 是用户/插件显式指定的：auto / none / fp8 / bnb8 / bnb4 / gguf。
    `allow_bnb` / `allow_fp8`：auto 阶梯里是否允许这两档。
      · 默认都允许（"自动量化"）；想要绝对保真可以传 False，退化成 none / offload
    """
    prefer = (prefer or "auto").lower()
    have_bnb = _bnb_available()

    # GGUF 单文件：只能走 from_single_file + GGUF 量化
    if prefer == "gguf":
        return {"quant": "gguf", "offload": "", "why": "指定 GGUF"}
    if not est_gb:
        # 估不出来：保守走 bnb8（有就量化，没有就原样）
        if prefer == "auto" and allow_bnb and have_bnb:
            return {"quant": "bnb8", "offload": "", "why": "模型大小未知，保守用 8bit"}
        return {"quant": ("none" if prefer == "auto" else prefer), "offload": "",
                "why": "模型大小未知"}

    budget = free_gb * SAFE_RATIO
    if prefer != "auto":
        need = est_gb * QUANT_RATIO.get(prefer, 1.0)
        offload = "" if need <= budget else "model"
        return {"quant": prefer, "offload": offload,
                "why": f"指定 {prefer}：需约 {need:.1f} GB / 可用 {free_gb:.1f} GB"}

    # ── auto：从"最保真且最安全"往下试 ──
    #
    # 台阶顺序是踩出来的，不是拍的：
    #   ① none   结果绝对正确，首选
    #   ② fp8    官方支持、误差小；放不下原精度时优先它
    #   ③ bnb8   有风险 —— 实测 bitsandbytes 8bit 量化 Flux2Transformer2DModel 后
    #            出图是纯噪声，不报错、速度还快 2-3 倍，所以特别坑。
    #            现在允许自动选，但**必须**有噪声闸门兜底（direct.py 里的 _noise_score，
    #            阈值 0.015；正常图 0.002-0.003，噪声 0.04+）
    #   ④ bnb4   同上，显存更省、质量更差
    #   ⑤ offload 最后手段：慢，但结果对
    ladder = ["none"]
    if allow_fp8 and _fp8_available():
        ladder.append("fp8")
    if allow_bnb and have_bnb:
        ladder += ["bnb8", "bnb4"]

    tried = []
    for q in ladder:
        need = est_gb * QUANT_RATIO[q]
        tried.append(f"{q}={need:.1f}G")
        if need <= budget:
            why = f"{q}：约 {need:.1f} GB ≤ 预算 {budget:.1f} GB"
            if q in ("bnb8", "bnb4"):
                why += "（量化结果有不确定性，出图会过噪声闸门）"
            return {"quant": q, "offload": "", "why": why}

    # 整块放不下 → CPU offload（慢，但结果是对的）
    return {"quant": "none", "offload": "model",
            "why": (f"显存不够（模型 {est_gb:.1f} GB，预算 {budget:.1f} GB），改用 CPU offload；"
                    f"试过 {', '.join(tried)}")}


def summary(est_gb: float, free_gb: float, decision: dict) -> str:
    return (f"[quant] 模型约 {est_gb:.1f} GB，显存可用 {free_gb:.1f} GB → "
            f"quant={decision['quant']} offload={decision['offload'] or 'none'} "
            f"（{decision['why']}）")


# ══════════════════════════════════════════════════════════════════
# 量化推荐（给用户看的"我该用哪种加载方式"）
# ══════════════════════════════════════════════════════════════════
#
# 和 choose_quant 的分工：
#   choose_quant 是**运行时**决策（真有模型要加载了，挑一条能跑的路）；
#   recommend_quant 是**选型时**的建议（还没下模型，告诉你下哪个版本、要多少内存）。
#
# 16 GB 内存是分水岭。offload / bnb8 都要把**整份权重**放进系统内存，
# 6 GB 的模型加上 Python + CUDA 运行时 + 系统，16 GB 机器必然换页。
# 所以内存不足时唯一干净的路是 **GGUF 量化版**（3-4 GB）或者干脆换小模型。

_VRAM_HEADROOM = 0.82      # 显存里留 18% 别塞满
_RAM_HEADROOM = 0.70       # 内存里留 30% 给系统 + Python + CUDA

# 实测倍率（不是理论值）：
#   Hunyuan3D-2mini 权重 3.6 GB → 峰值显存 6.4 GB（1.8x）、进程 RSS 10.8 GB（3.0x）。
#   多出来的是 conditioner / VAE / 中间张量 / safetensors 加载缓冲。
#   不乘这个倍率的话会算出"16G 内存能跑 7GB 模型"这种结论，实际必然换页。
# 倍率跟模型类型有关，不能一刀切：
#   出图（SD/SDXL）：UNet + VAE + 文本编码器，实测约 1.5x 显存 / 2.0x 内存
#   建模（hy3dgen）：多了 conditioner 和体素解码，实测 1.8x / 3.0x（2mini 3.6GB → 10.8GB）
#   对话（7B Q4）：权重本身小、上下文占一点，约 1.3x / 1.8x
_MULT = {
    "image": (1.5, 2.0),
    "mesh": (1.8, 3.0),
    "llm": (1.3, 1.8),
}
_VRAM_MULT = 1.8      # 默认（mesh）
_RAM_MULT = 3.0


def recommend_quant(model_gb: float, vram_gb: float, ram_gb: float,
                    prefer: str = "auto", has_gguf: bool = False,
                    kind: str = "mesh") -> dict:
    """按显存/内存推荐一种加载方式。

    参数
      model_gb   模型全精度体积（GB）
      vram_gb    可用显存
      ram_gb     总内存
      prefer     用户偏好（auto/none/gguf/bnb8/bnb4），非 auto 时直接照办并评估可行性
      has_gguf   这个模型有没有 GGUF 量化版可下

    返回
      {"quant", "offload", "fits", "vram_need_gb", "ram_need_gb", "why", "alternatives"}
    """
    vm, rm = _MULT.get((kind or "mesh").lower(), (_VRAM_MULT, _RAM_MULT))
    model_gb = max(float(model_gb or 0), 0.0)
    vram_gb = max(float(vram_gb or 0), 0.0)
    ram_gb = max(float(ram_gb or 0), 0.0)
    prefer = (prefer or "auto").lower()

    vram_budget = vram_gb * _VRAM_HEADROOM
    ram_budget = ram_gb * _RAM_HEADROOM

    def mk(quant: str, offload: str, fits: bool, why: str,
           vram_need: float, ram_need: float, alts: list | None = None) -> dict:
        return {"quant": quant, "offload": offload, "fits": fits, "why": why,
                "vram_need_gb": round(vram_need, 1), "ram_need_gb": round(ram_need, 1),
                "alternatives": alts or []}

    # 权重体积 → 真实占用（含中间张量和运行时）
    v_need = model_gb * vm
    r_need = model_gb * rm

    # ── 用户指定了级别：只评估可行性，不改他的选择 ──
    if prefer != "auto":
        if prefer == "gguf":
            est = model_gb * 0.30          # Q4 约 30% 的体积
            ev, er = est * vm, est * rm
            fits = ev <= vram_budget or er <= ram_budget
            return mk("gguf", "", fits,
                      f"指定 GGUF：权重 {est:.1f} GB，实际占显存约 {ev:.1f} / 内存约 {er:.1f} GB",
                      min(ev, vram_budget), er, [])
        need = model_gb * QUANT_RATIO.get(prefer, 1.0)
        nv, nr = need * vm, need * rm
        if nv <= vram_budget and nr <= ram_budget:
            return mk(prefer, "", True,
                      f"指定 {prefer}：实际需显存约 {nv:.1f} GB ≤ 预算 {vram_budget:.1f} GB",
                      nv, nr, [])
        fits_ram = nr <= ram_budget
        return mk(prefer, "model", fits_ram,
                  (f"指定 {prefer}：实际需显存约 {nv:.1f} GB，超出预算 {vram_budget:.1f} GB；"
                   f"offload 到内存需约 {nr:.1f} GB（预算 {ram_budget:.1f} GB）"),
                  min(nv, vram_budget), nr,
                  [{"quant": "gguf", "why": f"内存只有 {ram_budget:.0f} GB 可用，GGUF 约 "
                                            f"{model_gb * 0.30:.1f} GB 更稳"}] if not fits_ram else [])

    # ── auto ──
    # 1) 原精度塞得进显存：最好，不量化、不 offload
    if v_need <= vram_budget and r_need <= ram_budget:
        return mk("none", "", True,
                  f"原精度权重 {model_gb:.1f} GB，实际需显存约 {v_need:.1f} GB"
                  f"（预算 {vram_budget:.1f} GB）", v_need, r_need,
                  [{"quant": "gguf", "why": "想省显存可以换 GGUF，质量损失很小"}] if has_gguf else [])

    # 2) 放不下显存，但内存够装整份权重 → offload，保持原精度
    if r_need <= ram_budget:
        return mk("none", "model", True,
                  (f"显存放不下（需约 {v_need:.1f} > {vram_budget:.1f} GB），"
                   f"但内存够（需约 {r_need:.1f} ≤ {ram_budget:.1f} GB），用 offload 换速度"),
                  vram_budget, r_need,
                  [{"quant": "gguf", "why": f"GGUF 约 {model_gb * 0.30:.1f} GB，"
                                            f"能全部进显存，比 offload 快 2-3 倍"}] if has_gguf else [])

    # 3) 内存也装不下：**只有 GGUF 这条路**
    gguf_est = model_gb * 0.30
    gv, gr = gguf_est * vm, gguf_est * rm
    if has_gguf and (gv <= vram_budget or gr <= ram_budget):
        return mk("gguf", "", gv <= vram_budget,
                  (f"原精度实际要 {r_need:.1f} GB 内存，超出预算 {ram_budget:.1f} GB；"
                   f"必须用 GGUF 量化版（权重约 {gguf_est:.1f} GB，占显存约 {gv:.1f} GB）"),
                  min(gv, vram_budget), gr, [])

    # 4) 连量化版都放不下 —— 老实说跑不动
    return mk("none", "", False,
              (f"这个模型原精度 {model_gb:.1f} GB，实际要 {r_need:.1f} GB 内存，"
               f"预算只有 {ram_budget:.1f} GB（{ram_gb:.0f} GB 内存）；没有更小的 GGUF 版本"),
              vram_budget, ram_budget,
              [{"quant": "smaller", "why": "换更小的模型（3D 用 2mini、出图用 SD1.5 / SDXL-Turbo）"}])
