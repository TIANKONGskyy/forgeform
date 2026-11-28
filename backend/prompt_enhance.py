# -*- coding: utf-8 -*-
"""提示词扩写的"离线兜底"（2026-11-28）。

问题
----
`pipe.enhance_prompt()` 依赖对话模型（llama-server / Ollama / Strata）。
实测：这三者都没起时，它**静默返回原文**，日志里只有一行 warning——
用户以为"扩写生效了"，其实什么都没发生。

做法
----
有 LLM 就用 LLM；没有就用规则补全（可预期、可测试）：
1. 抽出尺寸（90mm / 9 厘米）并补一句打印尺度约束
2. 补摄影/建模常用约束（single object / pure white background / no text）
3. 拆成 IMAGE / 3D 两段（3D 段强调结构、壁厚、水密）

这样"没有大模型"时扩写仍然有用，而且不会假装做过 LLM。
"""
from __future__ import annotations

import re

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)")


def extract_size_mm(text: str) -> float | None:
    """从文本里抽尺寸，统一成毫米；抽不到返回 None。"""
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    return val * 10 if unit in ("cm", "厘米") else val


def offline_enhance(prompt: str) -> dict:
    """规则扩写：返回 {'image': ..., 'three_d': ...}。"""
    p = (prompt or "").strip()
    if not p:
        return {"image": "", "three_d": ""}

    size = extract_size_mm(p)
    size_hint = f"，成品最大尺寸约 {size:.0f}mm" if size else ""

    image = (f"{p}，single object，纯白背景，柔和均匀光照，居中构图，"
             f"无文字无水印，产品摄影风格{size_hint}")
    three = (f"{p}。建模要求：单一实体、封闭水密、壁厚不小于 2mm、"
             f"底面平整可直接打印{size_hint}；不要细小镂空和悬空结构")
    return {"image": image, "three_d": three}


def enhance_with_fallback(prompt: str, llm_fn=None) -> tuple[dict, str]:
    """先试 LLM，没有再走规则。返回 (sections, 用了哪种方式)��"""
    if llm_fn is not None:
        try:
            out = llm_fn(prompt)
            if out and str(out).strip() and str(out).strip() != prompt.strip():
                return {"image": str(out), "three_d": str(out)}, "llm"
        except Exception:
            pass
    return offline_enhance(prompt), "offline"
