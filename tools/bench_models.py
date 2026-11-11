# -*- coding: utf-8 -*-
"""同一句提示词，轮流用不同出图模型跑一遍，留下实测数据。

    python tools/bench_models.py --models qwen21_ref,z_image,flux_klein --quality 标准

输出：每个模型的耗时、产物体积、图片路径；最后打一张对比表 + 拼图。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "一个六角法兰联轴器，中心有轴孔，六个螺栓孔，金属质感，白色背景，单件"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(ROOT))
    ap.add_argument("--models", default="qwen21_ref,z_image,flux_klein,flux")
    ap.add_argument("--quality", default="标准")
    ap.add_argument("--resolution", default="1024×1024")
    args = ap.parse_args()

    home = Path(args.home).resolve()
    sys.path.insert(0, str(home))
    os.chdir(home)
    cfg_path = home / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    known = list((cfg.get("models", {}).get("image") or {}).keys())

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    models = [m for m in models if m in known]
    if not models:
        print(f"没有可用模型；config 里有: {known}")
        return 1

    # 先检查权重在不在 —— 缺文件时 ComfyUI 只会回一个 400，白等 35 秒才看到错误。
    import glob
    comfy = os.environ.get("COMFYUI_DIR") or (cfg.get("paths") or {}).get("comfyui") or ""
    comfy = os.path.expandvars(comfy)
    skip = {}
    if comfy and Path(comfy).exists():
        for mk in models:
            entry = cfg["models"]["image"][mk]
            need = [entry.get(k) for k in ("checkpoint", "unet", "vae", "clip_l", "clip_t5")
                    if entry.get(k)]
            missing = []
            for f in need:
                hit = glob.glob(str(Path(comfy) / "models" / "**" / f), recursive=True)
                if not hit:
                    missing.append(f)
            if missing:
                skip[mk] = missing

    from backend.pipeline import Pipeline
    pipe = Pipeline(str(cfg_path))

    print("=" * 72)
    print(f"模型对比   提示词: {PROMPT}")
    print("=" * 72)

    rows = []
    for i, mk in enumerate(models, 1):
        label = (cfg["models"]["image"][mk].get("label")
                 or cfg["models"]["image"][mk].get("checkpoint") or mk)
        print(f"\n[{i}/{len(models)}] {mk}  ({label})", flush=True)
        if mk in skip:
            miss = ", ".join(skip[mk])
            print(f"  - 跳过：缺权重 {miss}", flush=True)
            rows.append({"model": mk, "label": label, "ok": None,
                         "sec": 0.0, "error": f"缺权重: {miss}"})
            continue
        t0 = time.time()
        try:
            res = pipe.generate_images(PROMPT, count=1, model_key=mk,
                                       quality_name=args.quality,
                                       resolution=args.resolution, skip_cache=True)
            dt = time.time() - t0
            if res and getattr(res[0], "success", False):
                p = Path(res[0].output_path)
                kb = p.stat().st_size // 1024
                print(f"  ✓ {dt:.1f}s  {p.name}  {kb} KB", flush=True)
                rows.append({"model": mk, "label": label, "ok": True,
                             "sec": round(dt, 1), "kb": kb, "file": str(p)})
            else:
                err = getattr(res[0], "error", "无返回") if res else "无返回"
                print(f"  ✗ {dt:.1f}s  {err}", flush=True)
                rows.append({"model": mk, "label": label, "ok": False,
                             "sec": round(dt, 1), "error": str(err)[:160]})
        except Exception as exc:
            dt = time.time() - t0
            print(f"  ✗ {dt:.1f}s  {type(exc).__name__}: {exc}", flush=True)
            rows.append({"model": mk, "label": label, "ok": False,
                         "sec": round(dt, 1), "error": str(exc)[:160]})

    print("\n" + "=" * 72)
    print(f"{'模型':<16} {'权重':<34} {'耗时':>8} {'体积':>9}")
    print("-" * 72)
    for r in rows:
        if r.get("ok") is True:
            size = f"{r['kb']} KB"
        elif r.get("ok") is None:
            size = "跳过"
        else:
            size = "失败"
        print(f"{r['model']:<16} {r['label'][:34]:<34} {r['sec']:>7.1f}s {size:>9}")
    print("-" * 72)
    ok = [r for r in rows if r.get("ok")]
    if ok:
        fastest = min(ok, key=lambda r: r["sec"])
        biggest = max(ok, key=lambda r: r["kb"])
        print(f"最快 {fastest['model']} {fastest['sec']}s · 体积最大 {biggest['model']} {biggest['kb']} KB")
    print("=" * 72)

    out = home / "outputs" / "bench_models.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"prompt": PROMPT, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"数据: {out}")

    # 拼一张对比图（2 列）
    try:
        from PIL import Image, ImageDraw, ImageFont
        files = [Path(r["file"]) for r in ok]
        if files:
            cell, pad, labelh = 512, 10, 34
            cols = 2
            rows_n = (len(files) + cols - 1) // cols
            W = cols * cell + (cols + 1) * pad
            H = rows_n * (cell + labelh) + (rows_n + 1) * pad
            canvas = Image.new("RGB", (W, H), (8, 12, 20))
            draw = ImageDraw.Draw(canvas)
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
            except Exception:
                font = ImageFont.load_default()
            for idx, r in enumerate(ok):
                im = Image.open(r["file"]).convert("RGB").resize((cell, cell))
                cx = pad + (idx % cols) * (cell + pad)
                cy = pad + (idx // cols) * (cell + labelh + pad)
                canvas.paste(im, (cx, cy))
                draw.text((cx + 4, cy + cell + 6),
                          f"{r['model']}  {r['sec']}s  {r['kb']} KB",
                          fill=(150, 200, 255), font=font)
            sheet = home / "outputs" / "bench_models.png"
            canvas.save(sheet)
            print(f"对比图: {sheet}")
    except Exception as exc:
        print(f"拼图跳过: {type(exc).__name__}: {exc}")

    # ok=None 是"跳过"（缺权重），不算失败
    return 0 if all(r.get("ok") is not False for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
