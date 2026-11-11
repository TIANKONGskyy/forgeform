# -*- coding: utf-8 -*-
"""verify_sharpness.py - 验证 euler+simple 图片清晰度（拉普拉斯方差 + 噪点）"""
import numpy as np
from PIL import Image
from scipy import ndimage

def metrics(path):
    img = Image.open(path).convert("L")
    arr = np.asarray(img, dtype=np.float32)
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    lap = ndimage.convolve(arr, k)
    lap_var = lap.var()
    med = ndimage.median_filter(arr, size=3)
    noise = (arr - med).std()
    return lap_var, noise

imgs = {
    "中国龙": r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙\图片.png",
    "海盗船": r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\海盗船\图片.png",
}
print("=== euler+simple 图片清晰度指标 ===")
for name, p in imgs.items():
    try:
        lv, ns = metrics(p)
        print(f"  {name}: 拉普拉斯方差={lv:.0f}（越高越清晰） 噪点={ns:.1f}（越低越干净）")
    except Exception as e:
        print(f"  {name}: 失败 {e}")
print("\n参考值（之前 dpmpp_2m+karras 的图）：拉普拉斯 1697-3980，噪点 7.3-13.3")
