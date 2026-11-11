# -*- coding: utf-8 -*-
"""test_enhance_7b.py - 测试 qwen2.5vl:7b 扩写质量（比 14b 小，比 4b 稳）"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.prompt_enhancer import enhance

P = "一条中国龙，鳞片浮雕、龙须、利爪、祥云底座"
for model in ["qwen2.5vl:7b"]:
    t0 = time.time()
    r = enhance(P, model=model, timeout=180)
    el = time.time() - t0
    print(f"[{model}] {el:.1f}s 长度 {len(r)} 含IMAGE={'【IMAGE】' in r} 含3D={'【3D】' in r}")
    print(f"  IMAGE 段前200字:")
    img = r.split("【IMAGE】")[-1].split("【3D】")[0][:200] if "【IMAGE】" in r else r[:200]
    print(f"  {img}")
    print()
