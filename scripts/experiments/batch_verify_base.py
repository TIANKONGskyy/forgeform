# -*- coding: utf-8 -*-
"""batch_verify_base.py - 批量验证多主题的 STL 无浮空 + 底面平整"""
import sys, time, os
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

targets = [
    ("中国龙", r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙\模型_多视图.glb"),
    ("海盗船", r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\海盗船\模型_多视图.glb"),
    ("教堂", r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\哥特式大教堂\模型_多视图.glb"),
]

print("=== 批量验证 STL（无浮空 + 底面平整）===")
for name, glb in targets:
    if not os.path.exists(glb):
        print(f"  {name}: GLB 不存在，跳过")
        continue
    t0 = time.time()
    r = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    el = time.time() - t0
    if r.success:
        import trimesh
        m = trimesh.load(r.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        zmin = m.bounds[0][2]
        # 底面平整度：最低 1mm 内顶点 Z 的标准差（越小越平）
        low = m.vertices[m.vertices[:, 2] < zmin + 1.0]
        import numpy as np
        zstd = low[:, 2].std() if len(low) > 0 else -1
        print(f"  {name}: {len(m.faces):,}面 水密={m.is_watertight} 部件={len(comps)} 底面Z={zmin:.2f} 底面平整度σ={zstd:.2f}mm {el:.0f}s")
    else:
        print(f"  {name}: FAIL {r.error}")

print("=== 完成 ===")
