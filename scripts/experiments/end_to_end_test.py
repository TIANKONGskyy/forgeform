# -*- coding: utf-8 -*-
"""end_to_end_test.py - 全链路细节保真实测：扩写→图片→3D→STL（海盗船）"""
import sys, time, os
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

# ── 1. 扩写 ──
print("=== 1. 扩写提示词 ===")
sections = pipe.enhance_sections("海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板")
img_prompt = sections["image"]
td_prompt = sections["three_d"]
print(f"  IMAGE段({len(img_prompt)}字): {img_prompt[:150]}...")
print(f"  3D段({len(td_prompt)}字): {td_prompt[:150]}...")

# ── 2. 图片生成（IMAGE 段，高质量）──
print("\n=== 2. 图片生成（1024 高质量）===")
t0 = time.time()
results = pipe.generate_images(prompt=img_prompt, count=1, model_key="flux",
                               quality_name="高质量", resolution="1024×1024 (正方形)",
                               seed=89100, negative="", skip_cache=True)
el = time.time() - t0
img = results[0].output_path if results and results[0].success else ""
print(f"  图片: {el:.1f}s {'OK' if img else 'FAIL'} {img}")

if img:
    # ── 3. 3D 建模（高质量 octree 512）──
    print("\n=== 3. 3D 建模（高质量 octree 512）===")
    t0 = time.time()
    td = pipe.generate_3d(model_key="hunyuan3d", quality_name="高质量", image_path=img)
    el = time.time() - t0
    glb = td.output_path if td.success else ""
    if glb and os.path.exists(glb):
        gsize = os.path.getsize(glb) / 1024
    else:
        gsize = 0
    print(f"  3D: {el:.1f}s {'OK' if td.success else 'FAIL'} GLB={gsize:.0f}KB {glb}")

    if td.success:
        # ── 4. STL 转换（high_quality 预设）──
        print("\n=== 4. STL 转换（high_quality 预设）===")
        t0 = time.time()
        stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
        el = time.time() - t0
        if stl and stl.success:
            import trimesh
            m = trimesh.load(stl.output_path, force="mesh")
            print(f"  STL: {el:.1f}s OK 面数={len(m.faces)} 顶点={len(m.vertices)} 水密={m.is_watertight}")
            print(f"  输出: {stl.output_path}")
        else:
            print(f"  STL FAIL: {stl.error if stl else '?'}")

print("\n=== 全链路完成 ===")
