"""Build #1: 可滚动地球仪（球 + Y 形底座 + 轴）"""
from build123d import (
    Sphere, Cylinder, Box, Part, Plane, Solid,
    Align, Keep, export_stl,
)
import pathlib

OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

BALL_R = 30.0
WALL = 1.6
SEAM_Z = 12.0

clip_radius = 4.0
clip_high_h = 3.5
clip_low_h = 2.0
clip_gap = 0.1
positions = [(-12, 0, 0), (6, -10.4, 0), (6, 10.4, 0)]

# 切面
plane = Plane.XY.offset(SEAM_Z)

# ─── 1) 北半球壳 ───
north_outer = Sphere(BALL_R).split(plane, Keep.TOP)         # z=12~30
inner_top   = Sphere(BALL_R - WALL).split(plane, Keep.TOP)  # z=12~28.4
north_shell = Part([north_outer]) - Part([inner_top])

# 卡扣头（朝下）
clip_clips = []
for x, y, z in positions:
    c = Cylinder(clip_radius, clip_high_h,
                 align=(Align.CENTER, Align.CENTER, Align.MIN))
    c = c.translate((x, y, SEAM_Z - clip_high_h))  # 顶部齐 z=12
    clip_clips.append(c)
north_shell = north_shell + Part(clip_clips)

# 顶部轴孔（北极往赤道）
axis_hole_n = Cylinder(2.6, 25, align=(Align.CENTER, Align.CENTER, Align.MAX))
axis_hole_n = axis_hole_n.translate((0, 0, 30))  # 顶部齐 z=30
north_shell = north_shell - Part([axis_hole_n])

export_stl(north_shell, str(OUT / "01_globe_north.stl"))
print(f"  ✓ 01_globe_north.stl: {(OUT/'01_globe_north.stl').stat().st_size//1024} KB")

# ─── 2) 南半球壳 ───
south_outer = Sphere(BALL_R).split(plane, Keep.BOTTOM)      # z=-30~12 ✓
inner_bot    = Sphere(BALL_R - WALL).split(plane, Keep.BOTTOM) # z=-28.4~12
south_shell  = Part([south_outer]) - Part([inner_bot])

# 卡扣凹口（朝下）
clip_holes = []
for x, y, z in positions:
    h = Cylinder(clip_radius - clip_gap, clip_low_h,
                 align=(Align.CENTER, Align.CENTER, Align.MAX))
    h = h.translate((x, y, SEAM_Z - clip_low_h - clip_gap))  # 底部齐 z=12-clip_low_h-0.1
    clip_holes.append(h)
south_shell = south_shell - Part(clip_holes)

# 底部轴孔（南极往赤道）
axis_hole_s = Cylinder(2.6, 25, align=(Align.CENTER, Align.CENTER, Align.MIN))
axis_hole_s = axis_hole_s.translate((0, 0, -30))  # 底部齐 z=-30
south_shell = south_shell - Part([axis_hole_s])

export_stl(south_shell, str(OUT / "02_globe_south.stl"))
print(f"  ✓ 02_globe_south.stl: {(OUT/'02_globe_south.stl').stat().st_size//1024} KB")

# ─── 3) 横轴 ───
axis = Part([Cylinder(2.5, 80, align=(Align.CENTER, Align.CENTER, Align.CENTER))])
export_stl(axis, str(OUT / "03_axis.stl"))
print(f"  ✓ 03_axis.stl: {(OUT/'03_axis.stl').stat().st_size//1024} KB")

# ─── 4) Y 形底座 ───
b1 = Box(40, 8, 6, align=(Align.CENTER, Align.MIN, Align.CENTER))
b2 = Box(40, 6, 4, align=(Align.CENTER, Align.MAX, Align.CENTER)).translate((0, 8, 0))
f1 = Box(4, 25, 4, align=(Align.MIN, Align.MIN, Align.CENTER)).translate((-16, 14, 0))
f2 = Box(4, 25, 4, align=(Align.MIN, Align.MIN, Align.CENTER)).translate((16, 14, 0))
base = Part([b1, b2, f1, f2])

slot_l = Cylinder(3.5, 6, align=(Align.CENTER, Align.CENTER, Align.CENTER),
                 rotation=(0, 90, 0)).translate((-14, 39, 0))
slot_r = Cylinder(3.5, 6, align=(Align.CENTER, Align.CENTER, Align.CENTER),
                 rotation=(0, 90, 0)).translate((14, 39, 0))
base = base - Part([slot_l, slot_r])

export_stl(base, str(OUT / "04_base.stl"))
print(f"  ✓ 04_base.stl: {(OUT/'04_base.stl').stat().st_size//1024} KB")

print(f"\n✅ 地球仪 4 件已写到: {OUT}")
print("  装配：03_axis 插 04_base → 02_south 套轴 → 01_north 扣上 → 球能在轴上滚")