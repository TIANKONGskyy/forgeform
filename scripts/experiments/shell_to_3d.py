# -*- coding: utf-8 -*-
"""shell_to_3d.py - 贝壳高清图 → 2.1 建模 → STL"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
# 最近的高清图（贝壳）
img = r"D:\AI-Workshop\outputs\images\hd_1786712365.png"
if not os.path.exists(img):
    print("高清图不存在，尝试用编辑结果"); img = r"D:\AI-Workshop\outputs\images\edit_1786712432.png"
if not os.path.exists(img):
    print("都找不到"); sys.exit(1)

folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\贝壳")
folder.mkdir(parents=True, exist_ok=True)
shutil.copy2(img, folder / "图片.png")
print(f"输入图: {img}")

print("=== 贝壳 3D 建模（2.1 最强质量，约6分钟）===")
t0 = time.time()
r = pipe.generate_3d(model_key="hunyuan21", quality_name="高质量", image_path=img)
el = time.time() - t0
print(f"建模: {el:.0f}s {'OK' if r.success else 'FAIL: '+str(r.error)}")
glb = r.output_path if r.success else ""

if glb:
    shutil.copy2(glb, folder / "模型.glb")
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    print(f"STL: {time.time()-t0:.0f}s {'OK' if stl.success else 'FAIL'}")
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型.stl")
        import trimesh
        m = trimesh.load(stl.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        print(f"  STL {len(m.faces):,}面 水密={m.is_watertight} 部件数={len(comps)} 底面Z={m.bounds[0][2]:.2f}")

print("=== 贝壳完成 ===")
