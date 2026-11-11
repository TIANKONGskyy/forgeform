# -*- coding: utf-8 -*-
"""零件1 —— 参数化建模代码（CadQuery）

背景（2026-09-23）：桌面上的 `零件1.SLDPRT` 是 **SolidWorks 原生件**，全盘扫过
（D:\\AI-Workshop + 整个桌面，排除缓存）**没有任何脚本提到它** → 原件背后没有代码。
这份是**按实测几何反向复现**的参数化版本：

    85 × 44 × 6 mm 平板
    · 四角 R4 圆角
    · 左边（短边）中间切一个 ⌀22 半圆缺口（槽口通厚度）
    · 两个 ⌀6 通孔，孔心距右端 21.3mm，y = 8 / 36（即上下各距边 8mm）

实测依据（在 z=3 中截面切片量的）：
    外接框 85.00 × 44.00，厚度 6.00（顶点只在 z=0 / z=6 两层 → **无倒角**）
    缺口：半圆，半径 ≈11（开口从 y≈12 到 y≈34）
    孔：两个内环各 77 点，等效半径 2.99 → ⌀6，圆心在截面系 (9.55, ±14)
    → 折算到零件坐标（左下为原点）= (63.7, 8) 与 (63.7, 36)

跑法：
    D:\\comfy312\\Scripts\\python.exe D:\\AI-Workshop\\parts_cad\\gen_part1.py
产物：零件1_rebuilt.stl / .step + 俯视预览 PNG（与原件的渲染图可对比）

要改尺寸只动下面 PARAMS 区，别改几何代码。
"""
from pathlib import Path

import cadquery as cq
from cadquery import exporters

# ── 参数 ──────────────────────────────────────────────────────────────
L = 85.0          # 长（X）
W = 44.0          # 宽（Y）
T = 6.0           # 厚（Z）

CORNER_R = 5.0    # 四角圆角（截面圆拟合 R4.99）

NOTCH_D = 20.0    # 左端半圆缺口直径（截面圆拟合 r=9.98，圆心在短边中点高度）
NOTCH_CY = W / 2  # 缺口圆心的高度位置（默认在宽度中点）

HOLE_D = 6.0      # 通孔直径
HOLE_X_FROM_LEFT = 52.04   # 孔心距**左端**（实测：截面点云相对左边线量得 52.04）
HOLE_Y1 = 8.0              # 孔1 距下边
HOLE_Y2 = W - 8.0          # 孔2 距下边（= 距上边 8）

OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_STL = OUT_DIR / "零件1_rebuilt.stl"
OUT_STEP = OUT_DIR / "零件1_rebuilt.step"

# ── 建模 ──────────────────────────────────────────────────────────────
# 1) 底板 + 四角圆角
plate = (
    cq.Workplane("XY")
    .rect(L, W)
    .extrude(T)
    .edges("|Z")
    .fillet(CORNER_R)
)

# 2) 左端半圆缺口：圆柱圆心必须落在**左边线**上（CadQuery 的 rect() 以原点为中心，
#    所以左边线在 x = -L/2 —— 我第一版写在 x=0，结果切出一个完整的 ⌀20 圆洞，
#    体积比原件少 946mm³，正是多切的那半个圆 ✗）
notch = (
    cq.Workplane("XY")
    .center(-L / 2, NOTCH_CY - W / 2)
    .circle(NOTCH_D / 2)
    .extrude(T)
)
part = plate.cut(notch)

# 3) 两个 ⌀6 通孔
part = (
    part.faces(">Z")
    .workplane()
    .pushPoints([(HOLE_X_FROM_LEFT - L / 2, HOLE_Y1 - W / 2),
                 (HOLE_X_FROM_LEFT - L / 2, HOLE_Y2 - W / 2)])
    .hole(HOLE_D)
)

# ── 导出 + 自检 ───────────────────────────────────────────────────────
OUT_DIR.mkdir(parents=True, exist_ok=True)
exporters.export(part, str(OUT_STL), tolerance=0.05, angularTolerance=0.05)
exporters.export(part, str(OUT_STEP))

solids = part.solids().vals()
bb = part.val().BoundingBox()
import math
vol = sum(s.Volume() for s in solids)
print(f"实体数 {len(solids)}")
print(f"包围盒 {bb.xlen:.2f} × {bb.ylen:.2f} × {bb.zlen:.2f} mm")
print(f"体积 {vol:.0f} mm^3（原件实测 21033）")
print(f"→ {OUT_STL}")
print(f"→ {OUT_STEP}")
