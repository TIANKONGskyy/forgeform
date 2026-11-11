# -*- coding: utf-8 -*-
"""smoke_holder.py - 烟盒打火机桌面支架 分步生成（每步展示结果）"""
import sys, time, shutil, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
folder = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\烟盒打火机支架")
folder.mkdir(parents=True, exist_ok=True)

PROMPT = "桌面烟盒打火机收纳支架，一个斜槽放标准烟盒，一个竖槽放打火机，简洁实用，平整底座"

# 第 1 步：扩写
print("=== 第1步：提示词扩写 ===")
sections = pipe.enhance_sections(PROMPT)
(folder / "prompt.txt").write_text(
    f"原始提示: {PROMPT}\n\n【IMAGE】\n{sections['image']}\n\n【3D】\n{sections['three_d']}",
    encoding="utf-8")
print(f"原始: {PROMPT}")
print(f"\n【IMAGE】({len(sections['image'])}字):\n{sections['image']}")
print(f"\n【3D】({len(sections['three_d'])}字):\n{sections['three_d']}")
