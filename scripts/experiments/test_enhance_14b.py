# -*- coding: utf-8 -*-
"""test_enhance_14b.py - 测试 qwen3-14b 扩写质量"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.prompt_enhancer import enhance, is_ollama_running

print("Ollama:", is_ollama_running())
P = "一条中国龙，鳞片浮雕、龙须、利爪、祥云底座，东方神话风格"
t0 = time.time()
r = enhance(P, model="qwen3-14b:latest")
el = time.time() - t0
print(f"耗时 {el:.1f}s, 返回长度 {len(r)}")
print(f"含【IMAGE】: {'【IMAGE】' in r}, 含【3D】: {'【3D】' in r}")
print("前300字:")
print(r[:300])
