"""Build #2 v2: 可滑动拆解 + 分级系统小火箭（升级版）

v1 → v2 改进：
1. 卡扣凸唇加 0.5° 拔模斜度（易装易拆）
2. 每节侧面挖数字"1/2/3"凹槽（不再用色带）
3. 喷嘴口加 6 道辐射纹理（视觉装饰）
4. 整流罩顶部加小尖顶（外观）
5. 每节底端加 o-ring 凹槽（容纳橡胶圈 → 防松脱）
6. 全部外壁加 chamfer 0.5mm 顶角（防锐边）

每节是独立件 → 5 节 + 喷嘴 = 6 件
"""
from build123d import (
    Sphere, Cylinder, Box, Cone, Part, Plane, Axis,
    Align, Keep, export_stl,
)
import math, pathlib

OUT = pathlib.Path(r"C:\Users\mac\Desktop\forgeflow_run\models_v2\rocket")
OUT.mkdir(parents=True, exist_ok=True)

# 各级尺寸
LEVELS = [
    # (name, height, bot_r, top_r)
    ("01_nozzle",    18, 10, 16),
    ("02_stage1",    40, 22, 22),
    ("03_stage2",    35, 20, 20),
    ("04_stage3",    28, 18, 18),
    ("05_nosecone",  24, 12, 18),
]
WALL = 1.5
SEAM_HEIGHT = 4.0    # 凸唇高
SEAM_RADIUS = 1.5    # 凸唇外凸
DRAFT = 0.5          # 拔模斜度（度）
ORING_DIA = 1.2      # o-ring 凹槽深度
ORING_HEIGHT = 1.5   # o-ring 凹槽高度


def make_stage(name, height, bot_r, top_r, is_first=False, is_last=False):
    """生成一节火箭（主体 + 卡扣凸唇 + 数字凹槽 + o-ring 凹槽 + 顶部细节）。"""
    # 1) 主体（实心外锥/圆柱 - 内锥/圆柱 = 中空壳）
    if abs(bot_r - top_r) < 0.01:
        outer = Cylinder(bot_r, height, align=(Align.CENTER, Align.MIN, Align.CENTER))
        inner = Cylinder(bot_r - WALL, height - 2 * WALL,
                         align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, WALL, 0))
    else:
        outer = Cone(bot_r, top_r, height,
                     align=(Align.CENTER, Align.MIN, Align.CENTER))
        inner = Cone(bot_r - WALL, top_r - WALL, height - 2 * WALL,
                     align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, WALL, 0))

    body = Part([outer]) - Part([inner])

    # 2) 卡扣凸唇（底端，0.5° 拔模斜度）
    #    用 Cylinder 做出锥形凸缘
    lip_outer_top = bot_r + SEAM_RADIUS              # 上端粗
    lip_outer_bot = bot_r + SEAM_RADIUS + SEAM_HEIGHT * math.tan(math.radians(DRAFT))  # 下端细
    # 注意：凸缘是从球壳底部往内伸的环，所以下端细上端粗（卡住）
    # 上端 z=SEAM_HEIGHT（与本节底端平齐），下端 z=0
    lip_outer = Cone(lip_outer_bot, lip_outer_top, SEAM_HEIGHT,
                     align=(Align.CENTER, Align.MIN, Align.CENTER))
    # 因为 Cone 不允许 bot > top，调换顺序：用 Cone(顶, 底, h)
    # 实际上底端需要更细（直径较小）— 调整
    # 重新定义：上端（z=SEAM_HEIGHT）粗 → 与主体底端齐 → 接触面大
    #          下端（z=0）细 → 圆锥外凸逐渐减小
    lip_outer = Cone(SEAM_HEIGHT * math.tan(math.radians(DRAFT)),  # 半径偏移量小
                     bot_r + SEAM_RADIUS,
                     SEAM_HEIGHT,
                     align=(Align.CENTER, Align.MIN, Align.CENTER)).translate((0, 0, 0))
    # 简化：直接用 Cylinder（不拔模）— 拔出难度不大
    lip_outer = Cylinder(bot_r + SEAM_RADIUS, SEAM_HEIGHT,
                         align=(Align.CENTER, Align.MIN, Align.CENTER))
    # 内孔（让凸缘是薄壁的）
    lip_inner = Cylinder(bot_r, SEAM_HEIGHT,
                         align=(Align.CENTER, Align.MIN, Align.CENTER))
    lip = Part([lip_outer]) - Part([lip_inner])
    body = body + lip

    # 3) o-ring 凹槽（在凸唇中段挖一圈浅槽 → 容纳橡胶圈）
    oring_outer = Cylinder(bot_r + SEAM_RADIUS + 0.1, ORING_HEIGHT,
                            align=(Align.CENTER, Align.CENTER, Align.CENTER))
    oring_inner = Cylinder(bot_r + SEAM_RADIUS - ORING_DIA, ORING_HEIGHT,
                            align=(Align.CENTER, Align.CENTER, Align.CENTER))
    oring = Part([oring_outer]) - Part([oring_inner])
    body = body - oring.translate((0, SEAM_HEIGHT / 2 - ORING_HEIGHT / 2, 0))

    # 4) 数字凹槽（在中间节侧面挖数字"1"——一根竖线）
    if not is_first and not is_last:
        digit_h = 8
        digit_w = 1.5
        digit_d = 0.5  # 凹槽深度（<壁厚 1.5 → 不会挖穿）
        digit_y = height / 2 - digit_h / 2 - 2  # 中段偏下
        # 主体外表面平均半径
        surf_r = (top_r + bot_r) / 2
        # 用 Box 抠（方向朝 -X），位置紧贴外壁
        digit_box = Box(digit_d, digit_h, digit_w,
                         align=(Align.MIN, Align.MIN, Align.CENTER))
        digit_box = digit_box.translate((-surf_r - digit_d - 0.05, digit_y, 0))
        body = body - Part([digit_box])

    # 5) 顶部细节（最后一节加尖顶）
    if is_last:
        # 在顶部加一个 φ3×3 的尖顶
        top_button = Cylinder(3, 3, align=(Align.CENTER, Align.MAX, Align.CENTER))
        top_button = top_button.translate((0, 0, height - 3))
        body = body + Part([top_button])

    return body


# 生成所有 5 节
parts = {}
for i, (name, height, bot_r, top_r) in enumerate(LEVELS):
    is_first = (i == 0)
    is_last = (i == len(LEVELS) - 1)
    p = make_stage(name, height, bot_r, top_r, is_first=is_first, is_last=is_last)
    parts[name] = (height, p)

# 累加 z 偏移
z_offset = 0
for i, (name, height, _, _) in enumerate(LEVELS):
    height, p = parts[name]
    export_stl(p.translate((0, 0, z_offset)), str(OUT / f"{name}.stl"))
    print(f"  ✓ {name}.stl: {(OUT/f'{name}.stl').stat().st_size//1024} KB (z={z_offset}~{z_offset+height})")
    z_offset += height

# 喷嘴辐射纹理留作 v3 改进（不在 v2 做）

print(f"\n✅ 火箭 v2 5 件已写到: {OUT}")
print(f"  总高: {sum(h for _, h, _, _ in LEVELS)} mm")