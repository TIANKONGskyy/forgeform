# -*- coding: utf-8 -*-
"""ab_teacache.py - TeaCache 阈值 A/B 实测：固定 seed 对比 0.4 vs 0.5 的画质(phash)+耗时"""
import sys, json, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

CFG_PATH = "D:/AI-Workshop/config.json"
PROMPT = "a brass pocket watch with visible gears, macro photography"
SEED = 88888

def hamming(a, b):
    if not a or not b:
        return -1
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return -1

def run_thresh(thresh):
    # 临时改 config 的 teacache_thresh
    c = json.load(open(CFG_PATH, encoding="utf-8"))
    c["models"]["image"]["flux"]["teacache_thresh"] = thresh
    json.dump(c, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 重新实例化 pipeline（读新 config）
    pipe = Pipeline(CFG_PATH)
    t0 = time.time()
    results = pipe.generate_images(
        prompt=PROMPT, count=1, model_key="flux", quality_name="标准",
        resolution="1024×1024 (正方形)", seed=SEED, negative="",
        skip_cache=True,  # 绕过缓存，强制真实生成
    )
    elapsed = time.time() - t0
    if results and results[0].success:
        path = results[0].output_path
        # phash（用 _gen_one 的 dhash）
        from PIL import Image
        img = Image.open(path).convert("L").resize((9, 8), Image.LANCZOS)
        px = list(img.getdata())
        bits = []
        for r in range(8):
            row = r * 9
            for c in range(8):
                bits.append("1" if px[row + c] > px[row + c + 1] else "0")
        ph = hex(int("".join(bits), 2))[2:].zfill(16)
        return {"phash": ph, "elapsed": round(elapsed, 2), "path": path}
    return {"phash": "", "elapsed": round(elapsed, 2), "error": str(results[0].error) if results else "no result"}

print(f"=== TeaCache A/B 实测（1024 标准，seed={SEED}）===")
r04 = run_thresh(0.4)
print(f"  0.4: {r04['elapsed']}s phash={r04['phash']}")
r05 = run_thresh(0.5)
print(f"  0.5: {r05['elapsed']}s phash={r05['phash']}")
dist = hamming(r04["phash"], r05["phash"])
print(f"  phash 汉明距离: {dist} (0=完全相同, 越大差异越大; <8 视为几乎无损)")
dt = r05["elapsed"] - r04["elapsed"]
print(f"  耗时差: {dt:+.2f}s")

# 恢复 0.5（当前选定值）
c = json.load(open(CFG_PATH, encoding="utf-8"))
c["models"]["image"]["flux"]["teacache_thresh"] = 0.5
json.dump(c, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("=== 已恢复 teacache_thresh=0.5 ===")
