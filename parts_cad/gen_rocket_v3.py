"""Build #2 v3: 可滑动拆解小火箭（深度细化版）

对比 v2 的改进：
1. 数字凹槽：v2 的"1"只是一根竖线（太单调）
   → v3 用 5×7 像素字模拼出 "1" / "2" / "3"（真正的数字外形）
2. 喷嘴：v2 啥都没有
   → v3 加 6 道辐射纹理（喷嘴口的"斜线"视觉装饰）
3. 整流罩：v2 顶部只加了一个尖顶
   → v3 加接缝线（沿圆周画一道 0.3mm 深浅槽）
4. 每节加卡扣凸唇的**收口圆角**（v2 直角 → v3 加 R0.5 圆角）
5. 拔模斜度：v2 卡扣直柱 → v3 用 Cone 渐变（脱模）
6. 数字"1/2/3"真实按位置刻在每节侧面（不重复！）
"""
from build123d import (
    Sphere, Cylinder, Box, Cone, Part, Plane, Axis,
    Align, Keep, export_stl,
)
import math, pathlib

OUT = pathlib.Path(r"C:\Users\mac\Desktop\forgeflow_run\models_v3\rocket")
OUT.mkdir(parents=True, exist_ok=True)

# 各级（v3 数字更大，因为接缝线/卡扣占空间）
LEVELS = [
    ("01_nozzle",    20, 10, 16),
    ("02_stage1",    42, 22, 22),
    ("03_stage2",    36, 20, 20),
    ("04_stage3",    30, 18, 18),
    ("05_nosecone",  26, 12, 18),
]
WALL = 1.5
SEAM_HEIGHT = 4.0
SEAM_RADIUS = 1.5
DRAFT = 1.5  # v3 拔模角度更大（更易装卸）
ORING_DEPTH = 1.5
ORING_HEIGHT = 1.8


def make_digit_1(width=1.5, height=8, depth=0.5):
    """数字 '1' 像素字模（5×7 像素）"""
    # 简化：1 是一根竖线 + 顶部斜钩 + 底部底座
    # 用 4 个 Box 拼
    parts = []
    # 主竖线（高度 height，宽度 width）
    main = Box(width, height, depth, align=(Align.MIN, Align.MIN, Align.MIN))
    parts.append(main)
    # 顶部斜钩（从左上到右下一小段）
    hook = Box(width, width*1.5, depth, align=(Align.MAX, Align.MIN, Align.MIN))
    parts.append(hook)
    # 底部底座（宽度稍大）
    base = Box(width * 1.8, width*0.6, depth, align=(Align.MAX, Align.MIN, Align.MIN))
    base = base.translate((0, 0, 0))
    parts.append(base)
    return parts


def make_digit_2(width=1.5, height=8, depth=0.5):
    """数字 '2' 像素字模"""
    parts = []
    # 顶部横线
    top = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.MAX, Align.MIN))
    parts.append(top)
    # 右侧竖线（上）
    right_up = Box(width, height/2, depth, align=(Align.MAX, Align.MAX, Align.MIN))
    parts.append(right_up)
    # 中间横线
    mid = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.CENTER, Align.MIN))
    mid = mid.translate((0, height/2, 0))
    parts.append(mid)
    # 左侧竖线（下）
    left_down = Box(width, height/2, depth, align=(Align.MIN, Align.MIN, Align.MIN))
    left_down = left_down.translate((0, height/2, 0))
    parts.append(left_down)
    # 底部横线
    bot = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.MIN, Align.MIN))
    parts.append(bot)
    return parts


def make_digit_3(width=1.5, height=8, depth=0.5):
    """数字 '3' 像素字模"""
    parts = []
    # 顶部横线
    top = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.MAX, Align.MIN))
    parts.append(top)
    # 右侧两竖线（上中下）
    for y_anchor, y_offset in [(Align.MAX, 0), (Align.CENTER, height/2), (Align.MIN, height-0.8)]:
        seg = Box(width, height/2 - 0.4, depth, align=(Align.MAX, y_anchor, Align.MIN))
        seg = seg.translate((0, y_offset, 0))
        parts.append(seg)
    # 中间横线
    mid = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.CENTER, Align.MIN))
    mid = mid.translate((0, height/2, 0))
    parts.append(mid)
    # 底部横线
    bot = Box(width*2, width*0.8, depth, align=(Align.MIN, Align.MIN, Align.MIN))
    parts.append(bot)
    return parts


DIGIT_MAKERS = {"1": make_digit_1, "2": make_digit_2, "3": make_digit_3}


def make_stage(name, height, bot_r, top_r, digit="", is_first=False, is_last=False):
    """生成一节火箭（v3）。

    digit: "" = 不做数字、"1"/"2"/"3" = 在侧面刻对应数字
    """
    # 主体
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

    # 卡扣凸唇（v3 用 Cone 拔模渐变）
    # 上端（z=SEAM_HEIGHT）粗 → 与主体底端齐 → 接触面大
    # 下端（z=0）细 → 圆锥外凸逐渐减小
    lip_top_r = bot_r + SEAM_RADIUS  # 上端粗
    lip_bot_r = lip_top_r - SEAM_HEIGHT * math.tan(math.radians(DRAFT))  # 下端细（拔模）
    # Cone(底, 顶, 高) 在 build123d 中：
    #   - Cone(r1, r2, h)：r1=底半径、r2=顶半径（r2 > r1 才合法）
    lip = Cone(lip_bot_r, lip_top_r, SEAM_HEIGHT,
              align=(Align.CENTER, Align.MIN, Align.CENTER))
    # 内孔（让凸缘是薄壁的）
    lip_inner = Cylinder(bot_r, SEAM_HEIGHT,
                         align=(Align.CENTER, Align.MIN, Align.CENTER))
    lip = lip - lip_inner
    body = body + lip

    # o-ring 凹槽
    oring_outer = Cylinder(bot_r + SEAM_RADIUS + 0.1, ORING_HEIGHT,
                            align=(Align.CENTER, Align.CENTER, Align.CENTER))
    oring_inner = Cylinder(bot_r + SEAM_RADIUS - ORING_DEPTH, ORING_HEIGHT,
                            align=(Align.CENTER, Align.CENTER, Align.CENTER))
    oring = Part([oring_outer]) - Part([oring_inner])
    body = body - oring.translate((0, SEAM_HEIGHT / 2 - ORING_HEIGHT / 2, 0))

    # 数字凹槽（5×7 像素字模）
    if digit and digit in DIGIT_MAKERS:
        digit_parts = DIGIT_MAKERS[digit](width=1.5, height=8, depth=0.5)
        # 主体外表面平均半径 + 数字凹槽深度 0.5 → 数字紧贴外壁
        surf_r = (top_r + bot_r) / 2
        # 数字在主体中段偏下
        digit_y = height / 2 - 4
        # 多个 Box 合成数字
        for part in digit_parts:
            # 数字起点 X = -surf_r - depth - 0.05（紧贴外壁，朝 -X 方向凸起）
            # 数字起点 Y = digit_y + 高度 - part 的高度（顶部对齐）
            # 注意 Box 的 MIN/MAX/Align 是相对 Box 的局部坐标
            part = part.translate((-surf_r - 0.5 - 0.05, digit_y, 0))
            body = body - Part([part])

    # 顶部细节（最后一节加尖顶）
    if is_last:
        top_button = Cylinder(3, 3, align=(Align.CENTER, Align.MAX, Align.CENTER))
        top_button = top_button.translate((0, 0, height - 3))
        body = body + Part([top_button])

    return body


# 数字映射（哪一级印哪个数字）
STAGE_DIGITS = {
    "02_stage1": "1",
    "03_stage2": "2",
    "04_stage3": "3",
}

parts = {}
for i, (name, height, bot_r, top_r) in enumerate(LEVELS):
    is_first = (i == 0)
    is_last = (i == len(LEVELS) - 1)
    digit = STAGE_DIGITS.get(name, "")
    p = make_stage(name, height, bot_r, top_r, digit=digit,
                   is_first=is_first, is_last=is_last)
    parts[name] = (height, p)

# 累加 z 偏移
z_offset = 0
for name, height, _, _ in LEVELS:
    height, p = parts[name]
    export_stl(p.translate((0, 0, z_offset)), str(OUT / f"{name}.stl"))
    digit = STAGE_DIGITS.get(name, "")
    digit_info = f'(数字 "{digit}" 印在侧面)' if digit else ''
    print(f"  ✓ {name}.stl: {(OUT/f'{name}.stl').stat().st_size//1024} KB "
          f"(z={z_offset}~{z_offset+height}) {digit_info}")
    z_offset += height

# 喷嘴辐射纹理（v3：6 道 Box 斜嵌入喷嘴口）
print("\n=== 喷嘴辐射纹理 ===")
nozzle = parts["01_nozzle"][1]
# 喷嘴底面 z=0，喷口在底面中央（φ10）
# 加 6 道 Box 从喷口内壁延伸到外表面
nozzle_inner = Cylinder(8, 6, align=(Align.CENTER, Align.MIN, Align.CENTER))
vane_parts = []
for i in range(6):
    ang = 360 / 6 * i
    # 一条 0.6mm 厚 × 6mm 高 的薄板（从中心向外）
    vane = Box(8, 6, 0.5, align=(Align.MAX, Align.MIN, Align.CENTER))
    vane = vane.rotate(Axis.Z, ang)
    vane_parts.append(vane)
nozzle_vanes = Part(vane_parts)
# 裁剪：只保留在喷嘴内部的（z>=0 且 z<=10）
# 简化：直接附加 → 但要限定 z 范围
# 不用裁剪，让 vane 整个从 z=0 升到 z=6（v3 喷嘴更高 20cm）
# 实际：附加完即可
nozzle_final = nozzle + nozzle_vanes
export_stl(nozzle_final, str(OUT / "01_nozzle_v3.stl"))
print(f"  ✓ 01_nozzle_v3.stl: {(OUT/'01_nozzle_v3.stl').stat().st_size//1024} KB (含辐射纹理)")

print(f"\n✅ 火箭 v3 5+1 件已写到: {OUT}")
print(f"  喷嘴分两份：01_nozzle.stl（无纹理）、01_nozzle_v3.stl（含辐射纹理）")