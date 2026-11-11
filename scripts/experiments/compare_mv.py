# -*- coding: utf-8 -*-
"""compare_mv.py - 单图 vs 多视图 3D 对比，产物放桌面"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙")

# 用中国龙的图片（euler+simple 生成的最新版）
img = str(folder / "图片.png")
if not os.path.exists(img):
    print("无输入图"); sys.exit(1)

# 多视图版本（空间关系准确）
print("多视图 3D 生成中（Zero123Plus 6视图 → ShapeGen MV，约 100s）...")
t0 = time.time()
r = pipe.generate_3d(model_key="multiview", quality_name="高质量", image_path=img)
el = time.time() - t0
if r.success:
    shutil.copy2(r.output_path, folder / "模型_多视图.glb")
    print(f"多视图: {el:.0f}s OK -> 模型_多视图.glb")
else:
    print(f"多视图 FAIL: {r.error}")

# 说明文件
(folder / "对比说明.txt").write_text(
    "单图 vs 多视图 3D 对比\n\n"
    "- 模型.glb：单图 3D（Hunyuan3D 只看到正面，背面靠脑补，空间关系可能错乱）\n"
    "- 模型_多视图.glb：多视图 3D（Zero123Plus 生成前/右/后/左 4 视角，重建空间关系准确）\n\n"
    "请用 Windows 3D 查看器或 Bambu Studio 旋转对比，重点看：\n"
    "1. 龙爪/龙身/祥云底座的位置关系是否更合理\n"
    "2. 背面结构是否完整（单图版背面常是糊的）\n"
    "3. 浮雕装饰（鳞片）是否更清晰\n",
    encoding="utf-8")
print("对比产物已放桌面")
