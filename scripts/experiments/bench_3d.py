# -*- coding: utf-8 -*-
"""bench_3d.py - 实测当前 3D 生成耗时（Hunyuan3D 2.1 ShapeGen）"""
import sys, time, glob
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

# 找一张输入图
imgs = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))
if not imgs:
    print("无输入图"); sys.exit(1)
img = imgs[-1]
print(f"输入图: {img}")

# 实测标准档（octree 384）
t0 = time.time()
r = pipe.generate_3d(model_key="hunyuan3d", quality_name="标准", image_path=img)
el = time.time() - t0
print(f"\n=== 3D 生成耗时（标准 octree 384）===")
print(f"总耗时: {el:.1f}s")
print(f"结果: {'OK' if r.success else 'FAIL: ' + str(r.error)}")
if r.success:
    print(f"输出: {r.output_path}")
print("\n=== 日志（阶段耗时线索）===")
for line in pipe.get_log_text().split("\n"):
    if "3D" in line or "任务" in line or "保存" in line or "VRAM" in line:
        print(" ", line)
