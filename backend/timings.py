"""各阶段耗时估算 —— **全部来自实测**，不靠拍脑袋。

数据来源：`logs/*.jsonl` 里"任务已提交 → 已保存"配对算出的真实耗时
（2026-09-23 当天样本，见下表 n）；新数据会改变这些值，改动时请一并更新样本数。

    阶段                      样本  中位    最快  最慢
    生图 512 预览(z_image)      —   ~15s   ~10s  ~40s   ← 混合来源（含模型冷加载时更久）
    高清重绘 1024              11    18s    15s   312s   ← 312s 是显存争抢时的极端值
    3D 快速 octree=256          2   148s   139s   158s
    3D 标准 octree=512          7   134s    86s   940s   ← 940s 同上，极端值
    STL 导出                   26     2s     0s    48s   ← 48s 是 94MB 大件
    多视图 multiview            1   137s   137s   137s

写法约定：每个阶段给 (中位, 上限) —— 界面显示"约 X"，进度环按中位推进，
超过上限就停在 ~95% 等真实完成（宁可让它等，也不要编一个假进度）。
"""
from __future__ import annotations

# 阶段 → (中位秒, 上限秒, 样本数)
# 上限取"多数情况不会超过"的值：极端值是显存争抢/大件造成的，不该写进常规估算
MEASURED: dict[str, tuple[float, float, int]] = {
    # 512 预览：2026-09-23 实测（含模型冷加载）flux_klein 50s、z_image 37s。
    # 早期写 15s 是错的 ✗ —— 那时没算上 ComfyUI 里模型已被卸掉、这一轮要重新加载的时间。
    "image_512":   (50.0, 90.0, 2),      # 生图 512 预览（1 步；默认模型 flux_klein）
    "hd_1024":     (18.0, 60.0, 11),    # 高清重绘 1024
    "3d_fast":     (148.0, 300.0, 2),    # Hunyuan3D 2.1 快速 octree=256
    "3d_std":      (134.0, 300.0, 7),    # Hunyuan3D 2.1 标准 octree=512
    "3d_multiview": (137.0, 300.0, 1),   # Zero123Plus 6 视图 → ShapeGen MV
    "stl":         (2.0,   45.0, 26),    # GLB → STL（水密化+缩放）
    "stl_big":     (30.0,  60.0, 4),     # 超大 GLB（>50MB）才会这么久
    # Qwen-Image-2.1 本地实测（2026-09-23，v0.37.2 + int8 权重）：768 首图 48s（含冷加载）
    "qwen21_t2i":  (55.0, 150.0, 1),
    "qwen21_i2i":  (60.0, 180.0, 0),   # 参考图驱动：还没实测，先占位（跑完第一张会填）
}

_LABEL = {
    "image_512": "生图（512 预览）",
    "hd_1024": "高清重绘 1024",
    "3d_fast": "3D 建模（快速档）",
    "3d_std": "3D 建模",
    "3d_multiview": "3D 建模（多视图）",
    "stl": "GLB → STL",
    "stl_big": "GLB → STL（大件）",
    "qwen21_t2i": "Qwen-Image-2.1 本地出图",
    "qwen21_i2i": "Qwen-Image-2.1 参考图出图",
}


def estimate(stage: str) -> tuple[float, float]:
    """(中位秒, 上限秒)。未知阶段给一个保守值而不是崩掉。"""
    med, hi, _n = MEASURED.get(stage, (20.0, 120.0, 0))
    return med, hi


def samples(stage: str) -> int:
    return MEASURED.get(stage, (0, 0, 0))[2]


def label(stage: str) -> str:
    return _LABEL.get(stage, stage)


def human(sec: float) -> str:
    """把秒数说成人话：<60s 用秒，否则 m 分 s 秒。"""
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{sec:.0f} 秒"
    m, s = divmod(int(round(sec)), 60)
    return f"{m} 分 {s} 秒" if s else f"{m} 分钟"


def stage_for_3d(model_key: str, quality_name: str) -> str:
    """3D 阶段 key：多视图/快速/标准分开算（实测差不少）。"""
    if "multiview" in (model_key or ""):
        return "3d_multiview"
    return "3d_fast" if (quality_name or "").startswith("快速") else "3d_std"


def stage_for_stl(glb_bytes: int) -> str:
    """STL 阶段 key：大件单独一档（94MB 那种实测要 48s）。"""
    return "stl_big" if (glb_bytes or 0) > 50 * 1024 * 1024 else "stl"


def progress_line(stage: str, elapsed: float = 0.0, done: bool = False) -> str:
    """一行人类可读的进度文案（含实测依据）。"""
    med, hi = estimate(stage)
    if done:
        return f"{label(stage)} 完成，实际 {human(elapsed)}"
    return f"{label(stage)} 进行中…实测通常 {human(med)}（最长见过 {human(hi)}），已用 {human(elapsed)}"
