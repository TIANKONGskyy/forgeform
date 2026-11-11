# -*- coding: utf-8 -*-
"""bench_klein.py - 实测 Klein Q5 vs dev 的 1024 出图速度（固定 seed）"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
PROMPT = "a brass pocket watch with visible gears, macro photography"

def run(model_key, quality):
    t0 = time.time()
    results = pipe.generate_images(
        prompt=PROMPT, count=1, model_key=model_key, quality_name=quality,
        resolution="1024×1024 (正方形)", seed=88889, negative="", skip_cache=True,
    )
    el = time.time() - t0
    ok = results[0].success if results else False
    err = (results[0].error[:80] if results and not results[0].success else "")
    return el, ok, err

print("=== Klein Q5 vs dev 实测（1024 标准，固定 seed）===")
for model, label in [("flux", "dev fp8 (主力)"), ("flux_klein", "Klein Q5 (快车道候选)")]:
    el, ok, err = run(model, "标准")
    status = "✅" if ok else f"❌ {err}"
    print(f"  {label:22s} {el:5.2f}s {status}")
print("=== 完成 ===")
