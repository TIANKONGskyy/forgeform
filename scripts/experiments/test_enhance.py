# -*- coding: utf-8 -*-
"""test_enhance.py - 排查扩写失败原因"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.prompt_enhancer import enhance, is_ollama_running

print("Ollama 运行:", is_ollama_running())
t0 = time.time()
r = enhance("海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板")
el = time.time() - t0
print(f"耗时: {el:.1f}s")
print(f"返回长度: {len(r)} 字")
print(f"返回内容: {r[:300]}")
