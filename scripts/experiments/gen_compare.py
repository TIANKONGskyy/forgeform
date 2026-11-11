# -*- coding: utf-8 -*-
"""gen_compare.py - 质量优先参数重新生成 + 清晰度对比"""
import sys, time, shutil
import numpy as np
from PIL import Image
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
P = "一条中国龙，鳞片浮雕、龙须、利爪、祥云底座"

def sharpness_metrics(path):
    """计算清晰度指标：拉普拉斯方差（清晰度）+ 噪点估计"""
    img = Image.open(path).convert("L")
    arr = np.asarray(img, dtype=np.float32)
    # 拉普拉斯（3x3 卷积核）
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    from scipy import ndimage
    lap = ndimage.convolve(arr, k)
    lap_var = lap.var()
    # 噪点估计：中值滤波残差的标准差
    med = ndimage.median_filter(arr, size=3)
    noise = (arr - med).std()
    return lap_var, noise

# 1. 扩写 + 生成新图（质量优先参数）
print("[1] 扩写...")
s = pipe.enhance_sections(P)
print("[2] 生成图片（dpmpp_2m + karras + TeaCache 0.3 + 30步）...")
t0 = time.time()
r = pipe.generate_images(prompt=s["image"], count=1, model_key="flux",
                         quality_name="高质量", resolution="1024×1024 (正方形)",
                         seed=-1, negative="", skip_cache=True)
el = time.time() - t0
new_img = r[0].output_path if r and r[0].success else ""
print(f"  {el:.0f}s {new_img}")

# 2. 复制到桌面
from pathlib import Path as _P
folder = _P(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙")
if new_img:
    shutil.copy2(new_img, folder / "图片.png")
    (folder / "prompt.txt").write_text(f"原始提示: {P}\n\n【IMAGE】\n{s['image']}\n\n【3D】\n{s['three_d']}", encoding="utf-8")

# 3. 清晰度对比
print("\n=== 清晰度指标对比 ===")
old = r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\海盗船\图片.png"  # 旧参数图（euler/simple/TeaCache 0.5）
for label, path in [("新图(dpmpp_2m)", new_img), ("旧图(euler/TeaCache0.5)", old)]:
    if path:
        try:
            lv, ns = sharpness_metrics(path)
            print(f"  {label}: 拉普拉斯方差={lv:.0f}（越高越清晰） 噪点={ns:.1f}（越低越干净）")
        except Exception as e:
            print(f"  {label}: 计算失败 {e}")
