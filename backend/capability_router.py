# -*- coding: utf-8 -*-
"""能力路由：把对话里的一句话变成一次能力调用（2026-11-27）。

为什么单独一层
--------------
主对话走 LLM 理解需求，但「给它绑骨骼」这类指令**不需要理解**——它就是动作，
交给 LLM 反而会绕：模型可能回一句"好的，骨骼绑定通常需要…"然后什么也不做。

这一层只做一件事：识别动作词 → 从当前作品里取输入 → 调产品入口 → 登记资产。
识别不出来就返回 None，让主流程照常走 LLM。

设计原则（都是踩出来的）
------------------------
- **必须有产物才接**：没有 3D 模型时说"绑骨骼"没有意义，返回 None 让对话正常回答
- **动作词要窄**：`上色` / `贴图` / `材质` 算贴图，但 `设计一个杯子` 不算；
  `动画` 算，但 `帮我做个动画角色模型`（造新东西）不算 —— 后者含"做/设计/生成"
- **参数从话里捞**：`精修 15 步`、`贴图用快速档`、`走路动画`（动作名）
"""
from __future__ import annotations

import re

# 造新东西的词：出现就说明用户在说"生成一个新模型"，不是在改现有模型
_NEW_THING = ("生成", "做一个", "做个", "新建", "画一个", "设计一个", "从零", "再生成一个")

_MOTION_WORDS = {
    "走": "02_01.bvh", "走路": "02_01.bvh", "步行": "02_01.bvh", "散步": "02_01.bvh",
    "跑": "09_01_run.bvh", "跑步": "09_01_run.bvh", "奔跑": "09_01_run.bvh",
    "挥手": "141_16_wave_hello.bvh", "招手": "141_16_wave_hello.bvh", "打招呼": "141_16_wave_hello.bvh",
    "鞠躬": "111_02_bow.bvh", "点个头": "111_02_bow.bvh",
    "站": "77_02_standing.bvh", "待机": "77_02_standing.bvh", "站立": "77_02_standing.bvh",
}

RIG_WORDS = ("绑骨骼", "绑定骨骼", "加骨骼", "骨骼", "骨架", "绑骨", "rig")
PAINT_WORDS = ("上色", "上个色", "上颜色", "涂个色", "贴图", "材质", "涂装", "pbr", "喷漆", "换皮")
REFINE_WORDS = ("精修", "提精度", "提高精度", "精度不够", "更精细", "细化模型", "加细节")
MOTION_WORDS = ("动画", "动作", "走一个", "让它走", "让它跑", "动起来", "做个动作")


def _has_new_thing(text: str) -> bool:
    return any(k in text for k in _NEW_THING)


def _mesh_of(state: dict) -> str:
    """当前作品里可用的网格：优先 3D 模型，其次 STL。"""
    if not isinstance(state, dict):
        return ""
    for key in ("glb_path", "model_path", "stl_path"):
        v = state.get(key) or ""
        if v and isinstance(v, str):
            try:
                from pathlib import Path
                if Path(v).is_file():
                    return v
            except Exception:
                continue
    return ""


def route(text: str, state: dict) -> dict | None:
    """返回 {"action","input","params","note"} 或 None。

    action ∈ rig / paint / refine / motion
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    mesh = _mesh_of(state)
    # 造新东西的句子不拦（"做一个会走路的机器人"）；
    # 但"做个走路动画"里的"做个"是给现有模型做动作，不能被当成造新东西
    concrete_action = any(k in t for k in _MOTION_WORDS) or any(
        k in t for k in RIG_WORDS + PAINT_WORDS + REFINE_WORDS)
    if _has_new_thing(t) and not concrete_action:
        return None

    # ① 骨骼
    if any(k in t for k in RIG_WORDS):
        if not mesh:
            return None
        tier = "riganything" if any(k in t for k in ("riganything", "语义", "手指", "角色骨")) else "template"
        return {"action": "rig", "input": mesh, "params": {"tier": tier},
                "note": "RigAnything 语义骨架" if tier == "riganything" else "模板骨架"}

    # ② 贴图
    if any(k in t for k in PAINT_WORDS):
        if not mesh:
            return None
        fast = any(k in t for k in ("快速", "预览", "快一点", "先看看"))
        ref = (state or {}).get("ref_image") or (state or {}).get("selected_image") or ""
        return {"action": "paint", "input": mesh,
                "params": {"fast": fast, "image": ref},
                "note": "快速档（预览）" if fast else "完整档"}

    # ③ 精修
    if any(k in t for k in REFINE_WORDS):
        if not mesh:
            return None
        m = re.search(r"(\d{1,2})\s*步", t)
        steps = int(m.group(1)) if m else 4
        ref = (state or {}).get("ref_image") or (state or {}).get("selected_image") or ""
        return {"action": "refine", "input": mesh,
                "params": {"steps": steps, "image": ref}, "note": f"{steps} 步"}

    # ④ 动画
    if any(k in t for k in MOTION_WORDS):
        if not mesh and not (state or {}).get("rigged_path"):
            return None
        bvh = ""
        for word, name in _MOTION_WORDS.items():
            if word in t:
                bvh = name
                break
        if not bvh:
            bvh, note = "02_01.bvh", "默认走路（想换就说「跑步 / 挥手 / 鞠躬 / 待机」）"
        else:
            note = bvh
        return {"action": "motion", "input": (state or {}).get("rigged_path") or mesh,
                "params": {"bvh": bvh}, "note": note}
    return None
