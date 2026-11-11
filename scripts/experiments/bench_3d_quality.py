# -*- coding: utf-8 -*-
"""bench_3d_quality.py - V2 各精度档耗时对比"""
import sys, time, glob
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
imgs = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))
img = imgs[-1] if imgs else ""

print("=== V2 Fast 各精度档（octree 256/384/512）===")
for q in ["快速", "标准", "高质量"]:
    t0 = time.time()
    r = pipe.generate_3d(model_key="hunyuan3d", quality_name=q, image_path=img)
    el = time.time() - t0
    ok = r.success
    import os
    size = os.path.getsize(r.output_path) / 1024 if ok and r.output_path and os.path.exists(r.output_path) else 0
    print(f"  [{q}] {el:.1f}s {'OK' if ok else 'FAIL'} GLB={size:.0f}KB")
print("=== 完成 ===")
