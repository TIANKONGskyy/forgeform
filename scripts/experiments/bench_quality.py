# -*- coding: utf-8 -*-
"""bench_quality.py - 实测 dev 1024 各精度档速度（零成本提速评估）"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
P = "a brass pocket watch with visible gears, macro photography"

print("=== dev 1024 各精度档速度（固定 seed）===")
for q in ["快速", "标准", "高质量"]:
    t0 = time.time()
    r = pipe.generate_images(prompt=P, count=1, model_key="flux", quality_name=q,
                             resolution="1024×1024 (正方形)", seed=88890, negative="", skip_cache=True)
    el = time.time() - t0
    ok = r[0].success if r else False
    status = "OK" if ok else "FAIL:" + str((r[0].error if r else "?")[:60])
    print(f"  dev 1024 [{q}] {el:.1f}s {status}")
print("=== 完成 ===")
