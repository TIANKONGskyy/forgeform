# -*- coding: utf-8 -*-
"""smoke_holder_step3.py - 第3步：3D 建模 + STL"""
import sys, time, shutil
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\烟盒打火机支架")
img = str(folder / "图片.png")

print("=== 第3步：3D 建模（2.1 最强质量，约6分钟）===")
t0 = time.time()
r = pipe.generate_3d(model_key="hunyuan21", quality_name="高质量", image_path=img)
el = time.time() - t0
print(f"3D 建模: {el:.0f}s {'OK' if r.success else 'FAIL: '+str(r.error)}")
glb = r.output_path if r.success else ""

if glb:
    shutil.copy2(glb, folder / "模型.glb")
    print(f"✅ 3D 模型保存: {folder / '模型.glb'}")

    print("=== 第4步：STL 导出 ===")
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    el = time.time() - t0
    print(f"STL 导出: {el:.0f}s {'OK' if stl.success else 'FAIL'}")
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型.stl")
        import trimesh
        m = trimesh.load(stl.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        print(f"✅ STL 保存: {folder / '打印模型.stl'}")
        print(f"   {len(m.faces):,}面 水密={m.is_watertight} 部件数={len(comps)} 底面Z={m.bounds[0][2]:.2f}")
print("=== 完成 ===")
