# -*- coding: utf-8 -*-
"""bench_mv_turbo.py - mv-Fast(20步) vs mv-Turbo(5步) 速度对比"""
import sys, time, glob, os
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
img = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))[-1]

for mode in ["Hunyuan3D-2mv-Fast", "Hunyuan3D-2mv-Turbo"]:
    # 临时改 config 的 multiview generation_mode
    import json
    p = "D:/AI-Workshop/config.json"
    c = json.load(open(p, encoding="utf-8"))
    c["models"]["3d"]["multiview"]["generation_mode"] = mode
    json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    pipe2 = Pipeline(p)
    t0 = time.time()
    r = pipe2.generate_3d(model_key="multiview", quality_name="高质量", image_path=img)
    el = time.time() - t0
    print(f"{mode}: {el:.0f}s {'OK' if r.success else 'FAIL: '+str(r.error)}")

# 恢复 Fast
c = json.load(open(p, encoding="utf-8"))
c["models"]["3d"]["multiview"]["generation_mode"] = "Hunyuan3D-2mv-Fast"
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已恢复 mv-Fast")
