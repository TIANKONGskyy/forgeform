# -*- coding: utf-8 -*-
"""smoke_holder_step2.py - 第2步：生成图像"""
import sys, time, shutil
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\烟盒打火机支架")
folder.mkdir(parents=True, exist_ok=True)

# 读取扩写好的 IMAGE 段
import re
pt = (folder / "prompt.txt").read_text(encoding="utf-8")
m = re.search(r"【IMAGE】\n(.+?)\n\n【3D】", pt, re.S)
img_prompt = m.group(1).strip() if m else "desktop cigarette case and lighter organizer"

print("=== 第2步：生成图像（1024 高质量）===")
t0 = time.time()
results = pipe.generate_images(prompt=img_prompt, count=1, model_key="flux",
                               quality_name="高质量", resolution="1024×1024 (正方形)",
                               seed=-1, negative="", skip_cache=True)
el = time.time() - t0
if results and results[0].success:
    img = results[0].output_path
    shutil.copy2(img, folder / "图片.png")
    print(f"✅ 图像生成完成 ({el:.0f}s): {img}")
    print(f"   已保存到: {folder / '图片.png'}")
else:
    print(f"❌ 图像生成失败: {results[0].error if results else 'no result'}")
