# -*- coding: utf-8 -*-
"""bench_hypersd2.py - 复核 Hyper-SD 步数 + 稳定耗时"""
import sys, json, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

CFG = "D:/AI-Workshop/config.json"
PROMPT = "a brass pocket watch with visible gears, macro photography"
SEED = 88931

c = json.load(open(CFG, encoding="utf-8"))
f = c["models"]["image"]["flux"]
f["teacache"] = False
f["fbcache"] = False
f["hyper_lora"] = "Hyper-FLUX.1-dev-8steps-lora.safetensors"
f["steps"] = 8
json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

pipe = Pipeline(CFG)
# 预热一张（加载 LoRA/模型）
pipe.generate_images(prompt=PROMPT, count=1, model_key="flux", quality_name="标准",
                     resolution="1024×1024 (正方形)", seed=SEED, negative="", skip_cache=True)
print("预热完成，跑稳定耗时...")
t0 = time.time()
r = pipe.generate_images(prompt=PROMPT, count=1, model_key="flux", quality_name="标准",
                         resolution="1024×1024 (正方形)", seed=SEED, negative="", skip_cache=True)
el = time.time() - t0
print(f"Hyper-SD 8步 稳定: {el:.1f}s {'OK' if r[0].success else 'FAIL'}")
# 打印日志里的步数
for line in pipe.get_log_text().split("\n"):
    if "生成图片" in line:
        print("  ", line)

# 恢复
c = json.load(open(CFG, encoding="utf-8"))
f = c["models"]["image"]["flux"]
f["teacache"] = True
f["fbcache"] = False
f["hyper_lora"] = ""
f["steps"] = 20
json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已恢复基线")
