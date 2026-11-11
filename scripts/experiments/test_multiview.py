# -*- coding: utf-8 -*-
"""test_multiview.py - 多视图 3D 实测（Zero123Plus → ShapeGen MV）"""
import sys, time, glob, os
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
imgs = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))
if not imgs:
    print("无输入图"); sys.exit(1)
img = imgs[-1]
print(f"输入图: {os.path.basename(img)}")

print("多视图 3D 生成中（首次会下载 Zero123Plus 权重 ~3GB）...")
t0 = time.time()
r = pipe.generate_3d(model_key="multiview", quality_name="高质量", image_path=img)
el = time.time() - t0
print(f"耗时: {el:.0f}s")
print(f"结果: {'OK' if r.success else 'FAIL: ' + str(r.error)}")
if r.success:
    print(f"输出: {r.output_path}")
    if os.path.exists(r.output_path):
        print(f"GLB 大小: {os.path.getsize(r.output_path)/1024:.0f}KB")
