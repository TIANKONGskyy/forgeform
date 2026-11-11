# -*- coding: utf-8 -*-
"""test_enhance2.py - 用 Pipeline 实例排查扩写"""
import sys
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline
from backend.prompt_enhancer import enhance as direct_enhance, is_ollama_running

pipe = Pipeline("D:/AI-Workshop/config.json")
P = "海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板"

print("Ollama 运行:", is_ollama_running())
print("--- 直接调 enhance ---")
r1 = direct_enhance(P)
print(f"  长度 {len(r1)}, 前80字: {r1[:80]}")

print("--- 通过 pipe.enhance_prompt ---")
r2 = pipe.enhance_prompt(P)
print(f"  长度 {len(r2)}, 前80字: {r2[:80]}")

print("--- 通过 pipe.enhance_sections ---")
r3 = pipe.enhance_sections(P)
print(f"  image 长度 {len(r3['image'])}, three_d 长度 {len(r3['three_d'])}")
print(f"  image 前80字: {r3['image'][:80]}")
