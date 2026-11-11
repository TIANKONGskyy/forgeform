# -*- coding: utf-8 -*-
"""run_hy21.py - Hunyuan3D 2.1（最强质量）全流程实测"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙")
img = str(folder / "图片.png")
if not os.path.exists(img):
    print("无输入图（先有图片.png）"); sys.exit(1)

print("=== Hunyuan3D 2.1（最强质量）实测 ===")
t0 = time.time()
r = pipe.generate_3d(model_key="hunyuan21", quality_name="高质量", image_path=img)
el = time.time() - t0
print(f"2.1 建模: {el:.0f}s {'OK' if r.success else 'FAIL: '+str(r.error)}")
glb = r.output_path if r.success else ""

if glb:
    shutil.copy2(glb, folder / "模型_2.1最强.glb")
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    print(f"STL: {time.time()-t0:.0f}s {'OK' if stl.success else 'FAIL'}")
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型_2.1最强.stl")
        import trimesh
        m = trimesh.load(stl.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        print(f"  STL {len(m.faces):,}面 水密={m.is_watertight} 部件数={len(comps)}")

print("=== 2.1 实测完成 ===")
