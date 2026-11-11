# -*- coding: utf-8 -*-
"""bench_hypersd.py - Hyper-SD 8步 LoRA 实测（对比 TeaCache 20步）"""
import sys, json, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

CFG = "D:/AI-Workshop/config.json"
PROMPT = "a brass pocket watch with visible gears, macro photography"
SEED = 88931

def run(mode):
    c = json.load(open(CFG, encoding="utf-8"))
    f = c["models"]["image"]["flux"]
    if mode == "baseline":
        f["teacache"] = True
        f["fbcache"] = False
        f["hyper_lora"] = ""
        f["steps"] = 20
    elif mode == "hypersd":
        f["teacache"] = False
        f["fbcache"] = False
        f["hyper_lora"] = "Hyper-FLUX.1-dev-8steps-lora.safetensors"
        f["steps"] = 8
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

print("=== Hyper-SD 8步 vs TeaCache 20步（1024 标准）===")
r_b = run("baseline")
print(f"  TeaCache 20步: {r_b['elapsed']}s {'OK' if r_b['ok'] else 'FAIL: '+str(r_b.get('error'))}")
r_h = run("hypersd")
print(f"  Hyper-SD 8步: {r_h['elapsed']}s {'OK' if r_h['ok'] else 'FAIL: '+str(r_h.get('error'))}")
if r_b["ok"] and r_h["ok"]:
    dist = bin(int(r_b["phash"], 16) ^ int(r_h["phash"], 16)).count("1")
    print(f"  phash 距离: {dist}（<8 几乎无损，8-15 轻微差异）")
    print(f"  提速: {(1 - r_h['elapsed']/r_b['elapsed'])*100:+.0f}%")

# 恢复基线
c = json.load(open(CFG, encoding="utf-8"))
f = c["models"]["image"]["flux"]
f["teacache"] = True
f["fbcache"] = False
f["hyper_lora"] = ""
f["steps"] = 20
json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已恢复基线")
