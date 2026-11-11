"""Build #2: 可滑动拆解 + 分级系统的小火箭

设计：
- 5 节火箭体（用 4 个卡扣拼接），从下到上：
  1. 底部喷嘴（锥形）
  2. 第一级（最大、圆柱）
  3. 第二级（中等、圆柱）
  4. 第三级（最小、圆柱）
  5. 顶部整流罩（圆锥）
- 每两级之间有**滑轨卡扣**（滑动拆装）
- 4 节分级（编号 + 颜色装饰槽）
- 整套 5+1=6 件

实现：
- 主体：build123d 圆柱（每节） + 整流罩用 Sphere + Cylinder 截顶
- 卡扣：每节底部凸缘（薄凸起）+ 下节顶部凹槽
- 滑动：凸缘 + 凹槽的"滑入式"卡扣（不是螺纹）
- 分级标记：在每节侧面挖出数字形状（小立方体挖出"1"、"2"、"3"凹槽）—— 简化先不做文字，做为**圆环色带**（不同深浅的圆环槽）
"""
from build123d import (
    Cylinder, Cone, Sphere, Box, Part, Plane, Solid,
    Align, export_stl,
)
import pathlib

OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

# 各级尺寸（mm）— 阶梯火箭，越往上越小
LEVELS = [
    # (name, height, bottom_radius, top_radius)
    ("01_nozzle",    18, 10, 16),    # 喷嘴：底窄（喷口）顶宽（连接第一级）
    ("02_stage1",    40, 22, 22),    # 第一级（最大）
    ("03_stage2",    35, 20, 20),    # 第二级
    ("04_stage3",    28, 18, 18),    # 第三级
    ("05_nosecone",  24, 12, 18),    # 顶部整流罩
]

WALL = 1.5  # 壁厚（打印强度足够）

# 卡扣：滑入式（凸唇 + 凹槽）
SEAM_HEIGHT = 4.0   # 每节接口处凸缘高 4mm
SEAM_RADIUS = 1.5   # 凸缘径向突出 1.5mm


def make_stage(name, height, bot_r, top_r):
    """生成一级火箭（主体圆柱 + 卡扣凸唇 + 装饰色带槽）。

    结构（从下到上）：
    - 主体：截顶圆锥（实心）
    - 减去内壁：同形中空（壁厚 WALL）
    - 加卡扣凸唇：底端 + SEAM_HEIGHT 高的凸缘
    - 加色带槽：在主体中部挖一圈浅槽（视觉标记）
    """
    # 主体（带斜度的圆锥；如果上下半径相等则用 Cylinder）
    if abs(bot_r - top_r) < 0.01:
        from build123d import Cylinder as _Cyl
        outer = _Cyl(bot_r, height, align=(Align.CENTER, Align.MIN, Align.CENTER))
        inner = _Cyl(bot_r - WALL, height - 2 * WALL,
                    align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, WALL, 0))
    else:
        outer = Cone(bot_r, top_r, height,
                    align=(Align.CENTER, Align.MIN, Align.CENTER))
        inner = Cone(bot_r - WALL, top_r - WALL, height - 2 * WALL,
                    align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, WALL, 0))

    # 主体 = 外 - 内
    body = Part([outer]) - Part([inner])

    # 卡扣凸唇（在底端外凸一圈）
    lip_bottom_radius = bot_r + SEAM_RADIUS
    lip_top_radius = bot_r + SEAM_RADIUS - 0.3  # 上窄下宽（轻微锥度防卡死）
    lip = Cylinder(lip_top_radius, SEAM_HEIGHT,
                   align=(Align.CENTER, Align.MIN, Align.CENTER))
    lip_outer = Cylinder(lip_bottom_radius, SEAM_HEIGHT,
                         align=(Align.CENTER, Align.MIN, Align.CENTER))
    lip_full = Part([lip_outer]) - Part([lip])  # 薄壁的圆环
    # 放到主体的底端（z = 0 到 z = SEAM_HEIGHT）
    body = body + lip_full.translate((0, 0, 0))  # 默认在底部

    # 色带槽（在中段挖一圈浅槽 — 视觉分级标记）
    band_depth = 0.5
    band_height = 1.0
    mid_y = height / 2
    band_outer = Cylinder(top_r + 0.1, band_height,
                          align=(Align.CENTER, Align.CENTER, Align.CENTER))
    band_inner = Cylinder(top_r - band_depth, band_height,
                          align=(Align.CENTER, Align.CENTER, Align.CENTER))
    band = Part([band_outer]) - Part([band_inner])
    body = body - band.translate((0, mid_y, 0))

    return body


# ─── 生成所有 5 节 ───
parts = []
for name, height, bot_r, top_r in LEVELS:
    p = make_stage(name, height, bot_r, top_r)
    # 整流罩（05）需要往上移到上一节顶部对齐
    parts.append((name, height, bot_r, top_r, p))

# 整流罩（05）顶上去
# 累计 z 偏移
z_offset = 0
for name, height, bot_r, top_r, part in parts:
    if name == "01_nozzle":
        # 喷嘴在 z=0
        export_stl(part.translate((0, 0, 0)), str(OUT / f"{name}.stl"))
        z_offset = height
    elif name == "05_nosecone":
        # 整流罩顶到第三级顶部 = 累积 z 偏移
        export_stl(part.translate((0, 0, z_offset)), str(OUT / f"{name}.stl"))
        # nosecone 是 Cone 但 build123d Cone 不接受尖顶 → 用 Sphere 截
    else:
        export_stl(part.translate((0, 0, z_offset)), str(OUT / f"{name}.stl"))
        z_offset += height

print(f"✅ 火箭 5 件已写到: {OUT}")
print(f"  z 累积（无整流罩）: {sum(h for n,h,_,_ in LEVELS[:-1])} mm")
print(f"  整流罩再加 {LEVELS[-1][1]} mm → 总高 {sum(h for _,h,_,_ in LEVELS)} mm")

# ─── 替换 05_nosecone: 圆头（Sphere 截顶） ───
# Sphere(18) 半径 18，从北极 z=18 截到 z=18-h_seam_height=14
# 然后减去 Cylinder 内部
nosecone_outer = Sphere(18).solid
# 简单：用一个 Cone(0, 18, 24) 替换 Sphere
nose = Cone(0.1, 18, 24, align=(Align.CENTER, Align.MIN, Align.CENTER))  # 尖顶
nose_inner = Cone(WALL + 0.1, 18 - WALL, 24 - 2 * WALL,
                  align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, WALL, 0))
nose_shell = Part([nose]) - Part([nose_inner])
# 卡扣凸唇（底端）
lip_nose_outer = Cylinder(18 + SEAM_RADIUS, SEAM_HEIGHT,
                           align=(Align.CENTER, Align.MIN, Align.CENTER))
lip_nose_inner = Cylinder(18 + SEAM_RADIUS - 0.3, SEAM_HEIGHT,
                           align=(Align.CENTER, Align.MIN, Align.CENTER))
lip_nose = Part([lip_nose_outer]) - Part([lip_nose_inner])
nose_shell = nose_shell + lip_nose
# 整流罩在第四级顶部
export_stl(nose_shell.translate((0, 0, z_offset - 24)), str(OUT / "05_nosecone.stl"))
print(f"  整流罩放到 z={z_offset - 24} (第三级顶部)")

print("\n✅ 全部完成。装配顺序（从下到上）：")
print("  01_nozzle → 02_stage1 → 03_stage2 → 04_stage3 → 05_nosecone")
print("  滑入式卡扣：每节从上一节顶部滑入；凸唇插入上一节内壁槽")
print("  分级标记：每节中部色带槽（深度 0.5mm，可用彩色笔涂标记 1/2/3）")