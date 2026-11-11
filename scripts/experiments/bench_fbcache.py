# -*- coding: utf-8 -*-
"""bench_fbcache.py - FBCache(WaveSpeed) vs TeaCache 实测（1024 标准，固定 seed）"""
import sys, json, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

CFG = "D:/AI-Workshop/config.json"
PROMPT = "a brass pocket watch with visible gears, macro photography"
SEED = 88921

def run(mode):
    c = json.load(open(CFG, encoding="utf-8"))
    f = c["models"]["image"]["flux"]
    if mode == "teacache":
        f["teacache"] = True
        f["fbcache"] = False
    elif mode == "fbcache":
        f["teacache"] = False
        f["fbcache"] = True
        f["fbcache_thresh"] = 0.12
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
        return {"phash": ph, "elapsed": round(el, 2), "path": r[0].output_path, "ok": True}
    return {"elapsed": round(el, 2), "error": str(r[0].error) if r else "no result", "ok": False}

print("=== FBCache vs TeaCache（1024 标准，固定 seed）===")
r_t = run("teacache")
print(f"  TeaCache 0.5: {r_t['elapsed']}s {'OK' if r_t['ok'] else 'FAIL: '+str(r_t.get('error'))}")
r_f = run("fbcache")
print(f"  FBCache 0.12: {r_f['elapsed']}s {'OK' if r_f['ok'] else 'FAIL: '+str(r_f.get('error'))}")
if r_t["ok"] and r_f["ok"]:
    dist = bin(int(r_t["phash"], 16) ^ int(r_f["phash"], 16)).count("1")
    print(f"  phash 距离: {dist}（<8 几乎无损）")
    print(f"  提速: {(1 - r_f['elapsed']/r_t['elapsed'])*100:+.0f}%")

# 恢复 TeaCache（当前选定值）
c = json.load(open(CFG, encoding="utf-8"))
f = c["models"]["image"]["flux"]
f["teacache"] = True
f["fbcache"] = False
json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已恢复 TeaCache")
