# -*- coding: utf-8 -*-
"""rerun_dragon.py - 用 qwen3-14b 重跑中国龙全流程，覆盖桌面"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙")
folder.mkdir(parents=True, exist_ok=True)
P = "一条中国龙，鳞片浮雕、龙须、利爪、祥云底座，东方神话风格"

print("[1/4] 扩写（qwen3-14b）...")
s = pipe.enhance_sections(P)
(folder / "prompt.txt").write_text(
    f"原始提示: {P}\n\n【IMAGE】\n{s['image']}\n\n【3D】\n{s['three_d']}", encoding="utf-8")
print(f"  IMAGE {len(s['image'])}字 / 3D {len(s['three_d'])}字")

print("[2/4] 图片...")
t0 = time.time()
r = pipe.generate_images(prompt=s["image"], count=1, model_key="flux",
                         quality_name="高质量", resolution="1024×1024 (正方形)",
                         seed=-1, negative="", skip_cache=True)
print(f"  {time.time()-t0:.0f}s")
img = r[0].output_path if r and r[0].success else ""
if img:
    shutil.copy2(img, folder / "图片.png")

print("[3/4] 3D...")
t0 = time.time()
td = pipe.generate_3d(model_key="hunyuan3d", quality_name="高质量", image_path=img)
print(f"  {time.time()-t0:.0f}s {'OK' if td.success else 'FAIL'}")
glb = td.output_path if td.success else ""
if glb:
    shutil.copy2(glb, folder / "模型.glb")

print("[4/4] STL...")
t0 = time.time()
stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
print(f"  {time.time()-t0:.0f}s {'OK' if stl.success else 'FAIL'}")
if stl and stl.success:
    shutil.copy2(stl.output_path, folder / "打印模型.stl")
    import trimesh
    m = trimesh.load(stl.output_path, force="mesh")
    print(f"  STL {len(m.faces)} 面，水密={m.is_watertight}")

print("=== 中国龙重跑完成 ===")
