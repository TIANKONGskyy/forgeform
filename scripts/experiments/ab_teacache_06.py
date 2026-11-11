# -*- coding: utf-8 -*-
"""ab_teacache_06.py - TeaCache 0.5 vs 0.6 对比（1024 标准，固定 seed）"""
import sys, json, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

CFG = "D:/AI-Workshop/config.json"
PROMPT = "a brass pocket watch with visible gears, macro photography"
SEED = 88901

def hamming(a, b):
    if not a or not b:
        return -1
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return -1

def run(thresh):
    c = json.load(open(CFG, encoding="utf-8"))
    c["models"]["image"]["flux"]["teacache_thresh"] = thresh
    json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    pipe = Pipeline(CFG)
    t0 = time.time()
    r = pipe.generate_images(prompt=PROMPT, count=1, model_key="flux", quality_name="标准",
                             resolution="1024×1024 (正方形)", seed=SEED, negative="", skip_cache=True)
    el = time.time() - t0
    if r and r[0].success:
        from PIL import Image
        img = Image.open(r[0].output_path).convert("L").resize((9, 8), Image.LANCZOS)
        px = list(img.getdata())
        bits = []
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits.append("1" if px[base + col] > px[base + col + 1] else "0")
        ph = hex(int("".join(bits), 2))[2:].zfill(16)
        return {"phash": ph, "elapsed": round(el, 2), "path": r[0].output_path}
    return {"phash": "", "elapsed": round(el, 2), "error": str(r[0].error) if r else "no result"}

print("=== TeaCache 0.5 vs 0.6（1024 标准）===")
r05 = run(0.5)
print(f"  0.5: {r05['elapsed']}s")
r06 = run(0.6)
print(f"  0.6: {r06['elapsed']}s")
dist = hamming(r05["phash"], r06["phash"])
print(f"  phash 距离: {dist}（<8 几乎无损，8-15 轻微差异，>15 明显差异）")
print(f"  提速: {(1 - r06['elapsed']/r05['elapsed'])*100:.0f}%")

# 恢复 0.5（当前选定值）
c = json.load(open(CFG, encoding="utf-8"))
c["models"]["image"]["flux"]["teacache_thresh"] = 0.5
json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已恢复 teacache_thresh=0.5")
