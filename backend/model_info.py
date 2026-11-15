# -*- coding: utf-8 -*-
"""模型元数据：把 config 里那些只有 filename 的模型翻成人能看懂的一张表。

## 为什么需要

`config.json` 里每个模型只写 `checkpoint: "flux-2-klein-9b-Q5_K_M.gguf"`。
用户看到这个，能猜到是 FLUX.2 Klein、9B、Q5_K_M 量化 —— 但猜不到：
  · 谁做的（Black Forest Labs / 阿里 / 腾讯 / 智谱）
  · 什么架构（DiT / UNet / 3D flow matching）
  · 用什么推理框架（ComfyUI / 直连 diffusers / hy3dgen）
  · 在这台机器上跑一张要多久

这个模块把答案集中在一处。**耗时不写死** —— 从 `backend/timings` 的实测
样本里读，没有样本就显示「未测」。

## 维护约定

新增模型时在这里加一条；找不到会走 `_infer()` 从 filename 猜，
猜不出来就标「未知」。宁可显示未知，也不要编。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# ── 厂商 / 系列 ───────────────────────────────────────────────
# key 是 config 里 models.<stage>.<key> 的名字
VENDORS: dict[str, dict] = {
    # ── 出图 ──
    "flux": {
        "vendor": "Black Forest Labs",
        "family": "FLUX.1",
        "arch": "DiT (12B) + T5XXL 文本编码",
        "license": "FLUX.1-dev Non-Commercial",
        "note": "28 步，质量最好但最慢；显存要 20 GB 以上才舒服",
    },
    "flux_klein": {
        "vendor": "Black Forest Labs",
        "family": "FLUX.2 Klein",
        "arch": "DiT (9B) + Qwen3 文本编码",
        "license": "FLUX.2 Klein 非商用",
        "note": "8 步，速度与细节平衡；细节比 Z-Image 好一档",
    },
    "flux2_dev": {
        "vendor": "Black Forest Labs",
        "family": "FLUX.2 Dev",
        "arch": "DiT (32B) + Mistral 24B 文本编码",
        "license": "FLUX.2 Dev 非商用",
        "note": "权重 18.65 + 13.35 + 0.31 = 32.3 GB，32G 卡跑不满",
    },
    "z_image": {
        "vendor": "Tongyi (阿里)",
        "family": "Z-Image Turbo",
        "arch": "DiT (6B) + Qwen3-4B 文本编码",
        "license": "Apache-2.0",
        "note": "8 步极快，但输出偏扁平大色块 —— 细节靠提示词硬撑",
    },
    "qwen21_ref": {
        "vendor": "Alibaba Qwen",
        "family": "Qwen-Image 2.1",
        "arch": "DiT + Qwen3-VL-8B 视觉文本编码（参考图条件）",
        "license": "Qwen License",
        "note": "能读参考图；int8_convrot 是 Kijai 的旋转变换量化",
    },
    # ── 建模 ──
    "hunyuan21": {
        "vendor": "Tencent Hunyuan",
        "family": "Hunyuan3D 2.1",
        "arch": "DiT flow matching (6.86 GB) + DINOv2 图像编码 + VAE (0.61 GB)",
        "license": "Tencent Hunyuan Community License",
        "note": "单图最强档；512 octree 出 200k 面约 6 分钟",
    },
    "hunyuan_mini": {
        "vendor": "Tencent Hunyuan",
        "family": "Hunyuan3D 2mini",
        "arch": "DiT flow matching (3.6 GB) + DINO + VAE",
        "license": "Tencent Hunyuan Community License",
        "note": "直连 hy3dgen，省内存；512 octree 峰值 5.35 GB 显存",
    },
    "hunyuan3d": {
        "vendor": "Tencent Hunyuan",
        "family": "Hunyuan3D V2",
        "arch": "DiT flow matching + FlashVDM",
        "license": "Tencent Hunyuan Community License",
        "note": "V2 Fast（20 步 + FlashVDM），走 ComfyUI 3D-Pack",
    },
    "multiview": {
        "vendor": "Tencent Hunyuan",
        "family": "Hunyuan3D V2 MV",
        "arch": "Zero123Plus 6 视图 → ShapeGen MV 多视图重建",
        "license": "Tencent Hunyuan Community License",
        "note": "需要 Zero123Plus + 2mv 权重（约 12 GB），本机未下",
    },
    "triposplat": {
        "vendor": "Tripo AI",
        "family": "TripoSplat",
        "arch": "原生前馈 3D 高斯 → mesh",
        "license": "Tripo 非商用",
        "note": "快；权重 triposplat_fp16 + 独立 VAE 编解码",
    },
}

# ── 量化识别 ─────────────────────────────────────────────────
# 从 checkpoint 文件名里认量化档。顺序有讲究：先匹配更具体的。
_QUANT_PATTERNS = [
    (r"int8_convrot", "INT8（convrot 旋转变换）"),
    (r"int8", "INT8"),
    (r"Q8_0", "Q8_0（8 bit GGUF）"),
    (r"Q6_K", "Q6_K（6 bit GGUF）"),
    (r"Q5_K_M", "Q5_K_M（5 bit GGUF，K-quant medium）"),
    (r"Q5_K_S", "Q5_K_S（5 bit GGUF，K-quant small）"),
    (r"Q4_K_M", "Q4_K_M（4 bit GGUF，K-quant medium）"),
    (r"Q4_K_S", "Q4_K_S（4 bit GGUF，K-quant small）"),
    (r"Q4_0", "Q4_0（4 bit GGUF）"),
    (r"Q3_K_M", "Q3_K_M（3 bit GGUF）"),
    (r"fp16", "FP16（半精度）"),
    (r"bf16", "BF16（bfloat16）"),
    (r"fp8", "FP8（8 bit 浮点）"),
    (r"fp32", "FP32（全精度）"),
]


def quant_of(checkpoint: str) -> str:
    """从权重文件名认量化。认不出返回「未标注」。"""
    s = str(checkpoint or "")
    for pat, label in _QUANT_PATTERNS:
        if re.search(pat, s, re.I):
            return label
    return "未标注"


def param_count(name: str, checkpoint: str = "") -> str:
    """从名字里抽参数量（9B / 32B / 4B / 6B / 125B …）。"""
    for src in (name, checkpoint):
        m = re.search(r"(\d+(?:\.\d+)?)\s*B\b", str(src or ""))
        if m:
            return f"{m.group(1)}B"
    return ""


def backend_of(cfg_model: dict, global_mode: str = "") -> str:
    """推理框架：config 的 backend 字段 > 全局 mode。"""
    b = str(cfg_model.get("backend") or "").strip()
    if b:
        return {
            "hy3dgen": "hy3dgen（本地 Python 包，直连）",
            "hy21": "ComfyUI 3D-Pack（Hunyuan3D 2.1 专用节点）",
            "comfy3d_pipeline": "ComfyUI 3D-Pack（V2 节点）",
            "multiview": "ComfyUI 3D-Pack（多视图）",
            "native_comfyui": "ComfyUI 原生节点",
            "direct": "diffusers（直连）",
        }.get(b, b)
    if str(cfg_model.get("checkpoint") or "").endswith(".gguf"):
        return "ComfyUI + ComfyUI-GGUF"
    if cfg_model.get("checkpoint"):
        return "ComfyUI"
    return global_mode or "未知"


# ── 对话模型（LLM）────────────────────────────────────────────
# 铸想的 agent 走 Strata（llama.cpp 封装的 OpenAI 兼容服务）。
# 吞吐是**本机实测**，缓存在 outputs/llm_bench.json，跑一次 bench() 会刷新。
LLM_VENDOR = {
    "vendor": "Alibaba Qwen",
    "family": "Qwen3.8-Flash-Next",
    "arch": "MoE 125B（每 token 约激活 10B）",
    "license": "Qwen License",
    "quant": "IQ3_S（3 bit GGUF，llama.cpp）",
    "backend": "llama.cpp（Strata 0.1.47 封装，127.0.0.1:8091）",
}

_BENCH_JSONL = Path(__file__).resolve().parent.parent / "outputs" / "llm_bench.jsonl"
_BENCH_LEGACY = Path(__file__).resolve().parent.parent / "outputs" / "llm_bench.json"
# 滚动窗口：只保留最近 N 次。样本太多没意义（驱动/负载变了老数据就该淘汰），
# 太少又会被单次抖动带偏 —— 20 次约等于"最近几天的真实感受"。
_BENCH_KEEP = 20
_MIN_TOK_S = 1.0        # 低于这个当异常样本丢弃（模型刚加载时会有 0.3 tok/s 的假值）


def _read_samples() -> list[dict]:
    """读全部样本（按时间正序）。会顺手把旧的单条 json 迁进 jsonl。"""
    out: list[dict] = []
    if _BENCH_JSONL.exists():
        try:
            for line in _BENCH_JSONL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if float(rec.get("predicted_tok_s") or 0) >= _MIN_TOK_S:
                    out.append(rec)
        except Exception:
            pass
    # 旧版只存一条 json —— 迁移一次，迁完删掉避免重复
    if _BENCH_LEGACY.exists():
        try:
            rec = json.loads(_BENCH_LEGACY.read_text(encoding="utf-8")) or {}
            if float(rec.get("predicted_tok_s") or 0) >= _MIN_TOK_S:
                rec.setdefault("source", "legacy")
                out.append(rec)
                append_sample(rec, trim=False)
            _BENCH_LEGACY.unlink()
        except Exception:
            pass
    return out


def append_sample(rec: dict, trim: bool = True) -> None:
    """追加一条样本，超窗口就裁掉最老的。任何异常都不往外抛（不影响主流程）。"""
    try:
        rec = dict(rec)
        rec.setdefault("measured_at", time.strftime("%Y-%m-%d %H:%M"))
        _BENCH_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(_BENCH_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not trim:
            return
        lines = [ln for ln in _BENCH_JSONL.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) > _BENCH_KEEP:
            _BENCH_JSONL.write_text("\n".join(lines[-_BENCH_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def llm_bench() -> dict:
    """聚合最近几次样本：中位生成速度 / 中位预填 / 样本数 / 最近一次。

    用中位不用均值 —— 冷启动、显存争抢、投机解码接受率波动都会拉出长尾，
    均值会被单次坏样本带偏。
    """
    samples = _read_samples()
    if not samples:
        return {}
    pred = [float(s.get("predicted_tok_s") or 0) for s in samples if s.get("predicted_tok_s")]
    prm = [float(s.get("prompt_tok_s") or 0) for s in samples if s.get("prompt_tok_s")]
    last = samples[-1]
    return {
        "n": len(samples),
        "predicted_tok_s": round(_median(pred), 1),
        "predicted_min": round(min(pred), 1) if pred else 0.0,
        "predicted_max": round(max(pred), 1) if pred else 0.0,
        "prompt_tok_s": round(_median(prm), 1),
        "last_predicted": last.get("predicted_tok_s"),
        "last_at": last.get("measured_at"),
        "window": _BENCH_KEEP,
        "samples": samples[-8:],
    }


def record_usage(timings: dict | None, usage: dict | None = None,
                 wall_sec: float | None = None, source: str = "chat") -> None:
    """从一次真实对话调用的返回里抽吞吐并记样本。

    llama.cpp 的 `timings.predicted_per_second` 是纯生成速度，最准。
    流式没有 timings 时退而用 `completion_tokens / (wall - 首字延迟)`，
    这仍然比"总时间 / token"准（后者把预填和排队都算进去了）。
    """
    tim = timings or {}
    usg = usage or {}
    pred = float(tim.get("predicted_per_second") or 0)
    prm = float(tim.get("prompt_per_second") or 0)
    if pred <= 0 and usg.get("completion_tokens") and wall_sec:
        # 粗略兜底：按整段墙钟算，source 标成 estimated 以便日后区分
        pred = float(usg["completion_tokens"]) / max(0.1, float(wall_sec))
        source = source + "-估算"
    if pred < _MIN_TOK_S:
        return
    append_sample({
        "model": usg.get("model") or "qwen3.8-flash-next-iq3_s",
        "predicted_tok_s": round(pred, 1),
        "prompt_tok_s": round(prm, 1),
        "prompt_tokens": usg.get("prompt_tokens"),
        "completion_tokens": usg.get("completion_tokens"),
        "draft_accepted": f'{tim.get("draft_n_accepted", 0)}/{tim.get("draft_n", 0)}',
        "wall_sec": round(wall_sec, 2) if wall_sec else None,
        "source": source,
    })


def bench_llm(url: str = "http://127.0.0.1:8091/v1", model: str = "",
              timeout: int = 180) -> dict:
    """主动跑一次基准，样本进滚动窗口。返回这一次的记录（不是聚合值）。"""
    import urllib.request

    body = {
        "model": model or "qwen3.8-flash-next-iq3_s",
        "messages": [{"role": "user",
                      "content": "用两句话说明 3D 打印的 FDM 原理。"}],
        "stream": False, "temperature": 0.2, "max_tokens": 300,
        "reasoning_effort": "none",
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read().decode())
    wall = time.time() - t0
    tim = d.get("timings") or {}
    usage = d.get("usage") or {}
    rec = {
        "model": body["model"],
        "wall_sec": round(wall, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "predicted_tok_s": round(float(tim.get("predicted_per_second") or 0), 1),
        "prompt_tok_s": round(float(tim.get("prompt_per_second") or 0), 1),
        "draft_accepted": f'{tim.get("draft_n_accepted", 0)}/{tim.get("draft_n", 0)}',
        "source": "bench",
    }
    append_sample(rec)
    return rec


def describe_llm(model: str = "qwen3.8-flash-next-iq3_s") -> dict:
    """对话模型的画像（吞吐 = 最近 N 次的中位，不是单次）。"""
    d = dict(LLM_VENDOR)
    d["name"] = model
    d["key"] = "llm"
    b = llm_bench()
    if b.get("predicted_tok_s"):
        lo, hi = b.get("predicted_min", 0), b.get("predicted_max", 0)
        d["timing"] = (f'中位 {b["predicted_tok_s"]} tok/s 生成'
                       f'（区间 {lo}–{hi}）· 预填 {b.get("prompt_tok_s")} tok/s'
                       f'· n={b["n"]}/{b["window"]}，最近 {b.get("last_at")}')
    else:
        d["timing"] = "未测（跑一次 tools/bench_llm.py，或正常对话几轮后自动累积）"
    return d


# 模型 key → timings.py 里的阶段名
_TIMING_STAGE = {
    "z_image":    "image_512",
    "flux_klein": "image_512",
    "flux":       "hd_1024",
    "flux2_dev":  "hd_1024",
    "qwen21_ref": "qwen21_t2i",
    "hunyuan_mini": "3d_std",
    "hunyuan21":  "3d_std",
    "hunyuan3d":  "3d_std",
    "multiview":  "3d_multiview",
    "triposplat": "3d_std",
}


def describe(model_key: str, cfg_model: dict, global_mode: str = "",
             timing_stage: str = "") -> dict:
    """拼出这个模型的完整画像。"""
    meta = VENDORS.get(model_key, {})
    ckpt = str(cfg_model.get("checkpoint") or "")
    timing = ""
    _ts = _TIMING_STAGE.get(model_key, "") or timing_stage
    if _ts:
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import backend.timings as T
            if T.samples(_ts) > 0:
                med, hi = T.estimate(_ts)
                # ★ 2026-11-15：标清数据来源。本机样本（滚动窗口）和出厂参考
                #   不是一回事 —— 换显卡能差 4 倍，用户有权知道看的是哪个。
                src = "本机" if T.is_local(_ts) else "出厂参考"
                timing = (f"中位 {T.human(med)}（最长 {T.human(hi)}，"
                          f"n={T.samples(_ts)}，{src}）")
            else:
                timing = "本机未测（没有样本）"
        except Exception:
            timing = "未测"
    return {
        "key": model_key,
        "name": cfg_model.get("name") or model_key,
        "vendor": meta.get("vendor", "未知"),
        "family": meta.get("family", ""),
        "arch": meta.get("arch", ""),
        "license": meta.get("license", ""),
        "note": meta.get("note", ""),
        "params": param_count(cfg_model.get("name", ""), ckpt),
        "quant": quant_of(ckpt),
        "checkpoint": ckpt,
        "backend": backend_of(cfg_model, global_mode),
        "steps": cfg_model.get("steps"),
        "cfg": cfg_model.get("cfg"),
        "sampler": cfg_model.get("sampler") or cfg_model.get("scheduler") or "",
        "timing": timing,
    }
