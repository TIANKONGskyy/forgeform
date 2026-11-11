# -*- coding: utf-8 -*-
"""end_to_end_mv.py - 全链路端到端验证（多视图3D）：扩写→图片→多视图3D→STL"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\海盗船")
folder.mkdir(parents=True, exist_ok=True)
P = "海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板"

# 1. 扩写
print("[1/4] 扩写...")
s = pipe.enhance_sections(P)
(folder / "prompt.txt").write_text(
    f"原始提示: {P}\n\n【IMAGE】\n{s['image']}\n\n【3D】\n{s['three_d']}", encoding="utf-8")
print(f"  IMAGE {len(s['image'])}字 / 3D {len(s['three_d'])}字")

# 2. 图片
print("[2/4] 图片...")
t0 = time.time()
r = pipe.generate_images(prompt=s["image"], count=1, model_key="flux",
                         quality_name="高质量", resolution="1024×1024 (正方形)",
                         seed=-1, negative="", skip_cache=True)
img = r[0].output_path if r and r[0].success else ""
print(f"  {time.time()-t0:.0f}s {'OK' if img else 'FAIL'}")
if img:
    shutil.copy2(img, folder / "图片.png")

# 3. 多视图 3D
print("[3/4] 多视图 3D（Zero123Plus 6视图 → ShapeGen MV）...")
t0 = time.time()
td = pipe.generate_3d(model_key="multiview", quality_name="高质量", image_path=img)
el = time.time() - t0
glb = td.output_path if td.success else ""
print(f"  {el:.0f}s {'OK' if td.success else 'FAIL: '+str(td.error)}")
if glb:
    shutil.copy2(glb, folder / "模型_多视图.glb")

# 4. STL
print("[4/4] STL...")
if glb:
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    print(f"  {time.time()-t0:.0f}s {'OK' if stl.success else 'FAIL'}")
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型_多视图.stl")
        import trimesh
        m = trimesh.load(stl.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        print(f"  STL {len(m.faces):,}面 水密={m.is_watertight} 部件数={len(comps)}")

print("=== 端到端（多视图）完成 ===")
