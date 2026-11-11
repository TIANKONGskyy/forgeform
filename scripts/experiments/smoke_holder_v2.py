# -*- coding: utf-8 -*-
"""smoke_holder_v2.py - 用用户精确提示词重跑（图像→3D→STL），v2 标记保留旧数据"""
import sys, time, shutil
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\烟盒打火机支架")
folder.mkdir(parents=True, exist_ok=True)

PROMPT = ("A minimalist desktop cigarette and lighter holder stand, product design photography. "
          "The stand has two compartments: a rectangular slot sized to fit a standard 20-cigarette pack "
          "(85mm x 55mm x 22mm), and a circular or rectangular groove next to it designed to hold a "
          "standard BIC-style lighter. Made of solid walnut wood with smooth rounded edges and a matte oil "
          "finish, showing natural wood grain. Compact footprint approximately 12cm x 7cm x 3cm. "
          "Placed on a clean dark gray desk surface. Soft studio lighting from the upper left, casting "
          "gentle shadows. Shot from a 3/4 top-down angle to clearly show both compartments. Clean, modern, "
          "Scandinavian-inspired aesthetic. No text, no logos. Product photography, high detail, 4K.")

# 保存提示词
(folder / "prompt_v2.txt").write_text(PROMPT, encoding="utf-8")

# 第 2 步：图像（直接用精确提示词，不扩写）
print("=== 第2步：生成图像（精确提示词，1024 高质量）===")
t0 = time.time()
results = pipe.generate_images(prompt=PROMPT, count=1, model_key="flux",
                               quality_name="高质量", resolution="1024×1024 (正方形)",
                               seed=-1, negative="", skip_cache=True)
el = time.time() - t0
if results and results[0].success:
    img = results[0].output_path
    shutil.copy2(img, folder / "图片_v2.png")
    print(f"✅ 图像 ({el:.0f}s): {folder / '图片_v2.png'}")
else:
    print(f"❌ 图像失败: {results[0].error if results else 'no result'}")
    sys.exit(1)

# 第 3 步：3D 建模（2.1）
print("=== 第3步：3D 建模（2.1，约6分钟）===")
t0 = time.time()
r = pipe.generate_3d(model_key="hunyuan21", quality_name="高质量", image_path=img)
el = time.time() - t0
print(f"3D ({el:.0f}s) {'OK' if r.success else 'FAIL: '+str(r.error)}")
if not r.success:
    sys.exit(1)
glb = r.output_path
shutil.copy2(glb, folder / "模型_v2.glb")

# 第 4 步：STL
print("=== 第4步：STL ===")
t0 = time.time()
stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
el = time.time() - t0
if stl and stl.success:
    shutil.copy2(stl.output_path, folder / "打印模型_v2.stl")
    import trimesh
    m = trimesh.load(stl.output_path, force="mesh")
    comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
    print(f"✅ STL ({el:.0f}s): {len(m.faces):,}面 水密={m.is_watertight} 部件={len(comps)} 底面Z={m.bounds[0][2]:.2f}")
else:
    print(f"❌ STL 失败: {stl.error if stl else '?'}")

print("=== v2 完成，旧数据保留 ===")
