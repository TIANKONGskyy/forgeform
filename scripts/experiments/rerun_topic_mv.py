# -*- coding: utf-8 -*-
"""rerun_topic_mv.py - 指定主题用多视图重跑全流程，验证泛化性"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

TOPICS = {
    "教堂": ("哥特式大教堂", "哥特式大教堂，尖塔、飞扶壁、玫瑰窗、石雕门环、滴水兽"),
    "章鱼": ("蒸汽朋克机械章鱼", "蒸汽朋克机械章鱼，黄铜齿轮、铜管、铆钉、触须吸盘、玻璃穹顶头部"),
}

name = sys.argv[1] if len(sys.argv) > 1 else "教堂"
topic_name, P = TOPICS.get(name, TOPICS["教堂"])
folder = Path(rf"C:\Users\mac\Desktop\AI创意工坊_全流程展示\{topic_name}")
folder.mkdir(parents=True, exist_ok=True)

print(f"=== {topic_name} 多视图重跑 ===")
s = pipe.enhance_sections(P)
print(f"[1] 扩写 IMAGE {len(s['image'])}字 / 3D {len(s['three_d'])}字")

t0 = time.time()
r = pipe.generate_images(prompt=s["image"], count=1, model_key="flux",
                         quality_name="高质量", resolution="1024×1024 (正方形)",
                         seed=-1, negative="", skip_cache=True)
img = r[0].output_path if r and r[0].success else ""
print(f"[2] 图片 {time.time()-t0:.0f}s {'OK' if img else 'FAIL'}")
if img: shutil.copy2(img, folder / "图片.png")

t0 = time.time()
td = pipe.generate_3d(model_key="multiview", quality_name="高质量", image_path=img)
glb = td.output_path if td.success else ""
print(f"[3] 多视图3D {time.time()-t0:.0f}s {'OK' if td.success else 'FAIL: '+str(td.error)}")
if glb: shutil.copy2(glb, folder / "模型_多视图.glb")

if glb:
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    print(f"[4] STL {time.time()-t0:.0f}s {'OK' if stl.success else 'FAIL'}")
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型_多视图.stl")
        import trimesh
        m = trimesh.load(stl.output_path, force="mesh")
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        print(f"    STL {len(m.faces):,}面 水密={m.is_watertight} 部件数={len(comps)}")

print(f"=== {topic_name} 多视图完成 ===")
