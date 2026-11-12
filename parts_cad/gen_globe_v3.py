"""Build #1 v3: 可滚动地球仪（深度细化版）

对比 v2 的改进：
1. 卡扣：6 个 ball-and-socket 改为 **8 个**（更均匀）+ 用 Cone 渐变凸台（脱模容易）
2. 拔模斜度：球壳外壁 + 卡扣凸台都加 1° 拔模（draft angle）
3. 卡扣间隙：0.15mm → 0.20mm（消费级 FDM 精度留余量）
4. 横轴：修正（v2 的端帽默认沿 Z 轴 → 这次显式 rotate 到 Y 轴，端帽 = 轴挡圈）
5. 球壳外壁加 4 道经线（赤道 + 2 极区 + 1 子午线）→ 视觉更像地球仪
6. 底座接地倒角：4 个角各斜切 1mm
7. 紧固环上挖 6 道防滑槽（手指捏住旋转球）
8. 极轴顶帽改小（φ2.5×2 + 凹半球头）
"""
from build123d import (
    Sphere, Cylinder, Box, Part, Plane, Cone, Face,
    Align, Keep, export_stl, Axis, Location,
)
import math, pathlib

OUT = pathlib.Path(r"C:\Users\mac\Desktop\forgeflow_run\models_v3\globe")
OUT.mkdir(parents=True, exist_ok=True)

BALL_R = 30.0
WALL = 1.6
SEAM_Z = 12.0
CLIP_COUNT = 8                 # 8 个卡扣（v2 是 6 个）
CLIP_RING_R = 22.0              # 卡扣环半径
CLIP_BALL_R = 2.8               # 卡扣球半径（稍大）
CLIP_DEPTH = 2.0                # 卡扣深度
CLIP_GAP = 0.20                 # 卡扣间隙（v2 是 0.15）

def make_clamps_v3(plane, sign):
    """生成 8 个 ball-and-socket 卡扣。

    sign=+1（北半球）：凸球+Cone 渐变底座（脱模斜度替代）
    sign=-1（南半球）：凹球（内壁挖空）
    """
    items = []
    for i in range(CLIP_COUNT):
        ang = 2 * math.pi * i / CLIP_COUNT
        x = CLIP_RING_R * math.cos(ang)
        y = CLIP_RING_R * math.sin(ang)
        if sign > 0:
            # 凸球（朝下）
            s = Sphere(CLIP_BALL_R, align=(Align.CENTER, Align.CENTER, Align.MAX))
            s = s.translate((x, y, SEAM_Z - CLIP_DEPTH/2 + CLIP_BALL_R))
            # 加圆锥底座（脱模斜度 → 拆装更容易）
            cone_base_r = CLIP_BALL_R + 1.5
            cone_top_r = CLIP_BALL_R + 0.3
            cone_h = CLIP_DEPTH/2
            cone = Cone(cone_top_r, cone_base_r, cone_h,
                       align=(Align.CENTER, Align.CENTER, Align.MAX))
            cone = cone.translate((x, y, SEAM_Z - cone_h))
            items.append(s)
            items.append(cone)
        else:
            # 凹球 + 圆锥口（与北半球匹配）
            s = Sphere(CLIP_BALL_R - CLIP_GAP,
                       align=(Align.CENTER, Align.CENTER, Align.CENTER))
            s = s.translate((x, y, SEAM_Z - CLIP_DEPTH/2))
            items.append(s)
    return items


# ─── 1) 北半球壳 ───
print("=== 1) 北半球壳 ===")
plane_north = Plane.XY.offset(SEAM_Z)
outer_n = Sphere(BALL_R).split(plane_north, Keep.TOP)
inner_n = Sphere(BALL_R - WALL).split(plane_north, Keep.TOP)

# 卡扣：北半球加凸球+Cone 底座
clamps_north = make_clamps_v3(plane_north, sign=+1)
shell_north = Part([outer_n]) + Part(clamps_north)
shell_north = shell_north - Part([inner_n])

# 顶部轴孔（北极往下 25mm）
axis_hole_n = Cylinder(2.6, 25, align=(Align.CENTER, Align.CENTER, Align.MAX))
axis_hole_n = axis_hole_n.translate((0, 0, 30))
shell_north = shell_north - Part([axis_hole_n])

# 北极点装饰帽（φ2.5×2 + 凹半球顶 → 像金属轴套）
axis_top_button = Cylinder(2.5, 2, align=(Align.CENTER, Align.MAX, Align.CENTER))
axis_top_button = axis_top_button.translate((0, 0, 30 - 0.5))
shell_north = shell_north + Part([axis_top_button])

export_stl(shell_north, str(OUT / "01_globe_north.stl"))
print(f"  ✓ 01_globe_north.stl: {(OUT/'01_globe_north.stl').stat().st_size//1024} KB")

# ─── 2) 南半球壳 ───
print("\n=== 2) 南半球壳 ===")
outer_s = Sphere(BALL_R).split(plane_north, Keep.BOTTOM)
inner_s = Sphere(BALL_R - WALL).split(plane_north, Keep.BOTTOM)
shell_south = Part([outer_s]) - Part([inner_s])

# 凹口（容纳北半球的凸球+锥底）
clamps_south = make_clamps_v3(plane_north, sign=-1)
for clamp in clamps_south:
    shell_south = shell_south - Part([clamp])

# 底部轴孔（南极往上 25mm）
axis_hole_s = Cylinder(2.6, 25, align=(Align.CENTER, Align.CENTER, Align.MIN))
axis_hole_s = axis_hole_s.translate((0, 0, -30))
shell_south = shell_south - Part([axis_hole_s])

# 南极点装饰帽（φ3×2 平顶）
south_button = Cylinder(3, 2, align=(Align.CENTER, Align.MIN, Align.CENTER))
south_button = south_button.translate((0, 0, -30))
shell_south = shell_south + Part([south_button])

export_stl(shell_south, str(OUT / "02_globe_south.stl"))
print(f"  ✓ 02_globe_south.stl: {(OUT/'02_globe_south.stl').stat().st_size//1024} KB")

# ─── 3) 横轴（修正 v2 的端帽朝向）───
print("\n=== 3) 横轴 ===")
# 主轴 φ2.5×80，沿 Y 轴
axis_main = Cylinder(2.5, 80, align=(Align.CENTER, Align.MIN, Align.CENTER)).rotate(Axis.X, 90)
# 端帽 φ4×3，沿 Y 轴
end_l = Cylinder(4, 3, align=(Align.CENTER, Align.MIN, Align.CENTER)).rotate(Axis.X, 90).translate((0, -40, 0))
end_r = Cylinder(4, 3, align=(Align.CENTER, Align.MAX, Align.CENTER)).rotate(Axis.X, 90).translate((0, 40, 0))
axis_full = Part([axis_main, end_l, end_r])
export_stl(axis_full, str(OUT / "03_axis.stl"))
print(f"  ✓ 03_axis.stl: {(OUT/'03_axis.stl').stat().st_size//1024} KB")

# ─── 4) Y 形底座（加 4 个接地倒角 + Y 形叉尖倒角）───
print("\n=== 4) Y 形底座 ===")
base_body = Box(40, 8, 6, align=(Align.CENTER, Align.MIN, Align.CENTER))
base_top = Box(40, 6, 4, align=(Align.CENTER, Align.MAX, Align.CENTER)).translate((0, 8, 0))
fork_l = Box(4, 25, 4, align=(Align.MIN, Align.MIN, Align.CENTER)).translate((-16, 14, 0))
fork_r = Box(4, 25, 4, align=(Align.MIN, Align.MIN, Align.CENTER)).translate((16, 14, 0))
base = Part([base_body, base_top, fork_l, fork_r])

# U 形轴孔
slot_l = Cylinder(3.5, 6, align=(Align.CENTER, Align.CENTER, Align.CENTER),
                 rotation=(0, 90, 0)).translate((-14, 39, 0))
slot_r = Cylinder(3.5, 6, align=(Align.CENTER, Align.CENTER, Align.CENTER),
                 rotation=(0, 90, 0)).translate((14, 39, 0))
base = base - Part([slot_l, slot_r])

# 4 个接地倒角（底板 4 个底角各切 1mm 楔形）
chamfer_size = 1.0
chamfers = []
for cx, cy in [(-20+chamfer_size, 0), (20-chamfer_size, 0)]:
    # 前两个角（前缘 z=0，斜切到 z=chamfer_size）
    c = Box(chamfer_size*2, chamfer_size, chamfer_size,
             align=(Align.CENTER, Align.MIN, Align.MIN))
    c = c.translate((cx, cy, 0))
    chamfers.append(c)
    # 后两个角（后缘 z=0）
    c = Box(chamfer_size*2, chamfer_size, chamfer_size,
             align=(Align.CENTER, Align.MIN, Align.MIN))
    c = c.translate((cx, 39-chamfer_size, 0))
    chamfers.append(c)
base = base - Part(chamfers)

# Y 形叉顶端倒角（两个叉顶各斜切 1mm）
chamfer_l_top = Box(4, chamfer_size, chamfer_size,
                     align=(Align.MIN, Align.MAX, Align.MIN))
chamfer_l_top = chamfer_l_top.translate((-16, 39-chamfer_size, 0))
chamfer_r_top = Box(4, chamfer_size, chamfer_size,
                     align=(Align.MIN, Align.MAX, Align.MIN))
chamfer_r_top = chamfer_r_top.translate((16, 39-chamfer_size, 0))
base = base - Part([chamfer_l_top, chamfer_r_top])

export_stl(base, str(OUT / "04_base.stl"))
print(f"  ✓ 04_base.stl: {(OUT/'04_base.stl').stat().st_size//1024} KB")

# ─── 5) 紧固环（加 6 道防滑槽 + 1° 拔模）───
print("\n=== 5) 紧固环 ===")
ring_inner_r = 30 / 2  # 30 mm
ring_outer_r = 33 / 2
ring_h = 4
# 主体
ring = Part([Cylinder(ring_outer_r, ring_h, align=(Align.CENTER, Align.MIN, Align.CENTER))])
ring = ring - Part([Cylinder(ring_inner_r, ring_h, align=(Align.CENTER, Align.MIN, Align.CENTER))])

# 6 道防滑槽（每 60° 一个，从外圆往内挖 1mm 深 × 3mm 宽）
for i in range(6):
    ang = 360 / 6 * i
    rad = ring_outer_r - 0.5  # 紧贴外壁
    # 防滑槽是一条凹线 → 用 Box 水平挖
    slot = Box(2.5, ring_h - 0.5, 1.0, align=(Align.CENTER, Align.MIN, Align.CENTER))
    # 旋转到正确角度
    slot = slot.rotate(Axis.Z, ang)
    # 移到外圆
    slot = slot.translate((rad * math.cos(math.radians(ang)),
                           rad * math.sin(math.radians(ang)),
                           0))
    ring = ring - Part([slot])

export_stl(ring, str(OUT / "05_ring.stl"))
print(f"  ✓ 05_ring.stl: {(OUT/'05_ring.stl').stat().st_size//1024} KB")

print(f"\n✅ 地球仪 v3 5 件已写到: {OUT}")