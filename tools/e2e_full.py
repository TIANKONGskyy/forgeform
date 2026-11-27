# -*- coding: utf-8 -*-
"""铸想全链路实测：每个模型跑一遍 → 修改产物 → 收集数据（2026-11-28）。

用法：
    python tools/e2e_full.py --out D:\e2e_2026-11-28 [--only image,3d,paint,...]

产物：
    <out>/report.json   每一步的耗时 / 峰值显存 / 产物 / 指标
    <out>/report.md     人能读的表格
    <out>/files/        所有产物（图片 / GLB / STL / 贴图 / 动作）
    <out>/logs/         每一步的原始输出

设计：任何一步失败都继续跑下一步（记 error），最后统一汇总 —— 实测要的是
"哪条路能通、通的时候多快"，不是遇错就停。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(r"D:\ForgeForm-oss")
sys.path.insert(0, str(ROOT))

PROMPT = "一个简约的白色陶瓷马克杯，圆柱杯身，带把手，single object, pure white background"
OUT = Path(r"D:\e2e_2026-11-28")
RESULTS: list[dict] = []
VRAM_PEAK = {"mb": 0}
_STOP = threading.Event()


def _vram_watch() -> None:
    while not _STOP.wait(2):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used,temperature.gpu",
                 "--format=csv,noheader,nounits"], text=True, timeout=8).strip().splitlines()[0]
            mb = int(out.split(",")[0])
            VRAM_PEAK["mb"] = max(VRAM_PEAK["mb"], mb)
        except Exception:
            pass


def rec(step: str, ok: bool, seconds: float, detail: str = "", **extra) -> None:
    row = {"step": step, "ok": bool(ok), "seconds": round(seconds, 1),
           "peak_vram_mb": VRAM_PEAK["mb"], "detail": str(detail)[:400], **extra}
    RESULTS.append(row)
    print(f"  {'✓' if ok else '✗'} {step:<34} {row['seconds']:>7.1f}s  "
          f"vram {row['peak_vram_mb']:>6} MB  {str(detail)[:110]}", flush=True)


def keep(path: str | Path, name: str) -> str:
    """把产物复制进 <out>/files 并返回新路径。"""
    try:
        src = Path(path)
        if not src.is_file():
            return ""
        dst = OUT / "files" / f"{name}{src.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)
    except Exception:
        return ""


def mesh_info(path: str) -> dict:
    try:
        import trimesh
        m = trimesh.load(path, force="mesh")
        return {"faces": int(len(m.faces)), "watertight": bool(m.is_watertight),
                "extents_mm": [round(float(x), 1) for x in m.extents]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ───────────────────────── 1. 出图（每个图像模型）─────────────────────────

def step_images(pipe, image_keys: list[str]) -> dict[str, str]:
    """跑遍所有出图模型，返回 {model_key: 图片路径}。"""
    got: dict[str, str] = {}
    for key in image_keys:
        t0 = time.time()
        try:
            res = pipe.generate_images(PROMPT, count=1, model_key=key,
                                       quality_name="快速", resolution="512×512", seed=42)
            r = res[0] if res else None
            ok = bool(r and getattr(r, "success", False) and r.output_path)
            path = getattr(r, "output_path", "") if ok else ""
            rec(f"出图 · {key}", ok, time.time() - t0,
                Path(path).name if ok else str(getattr(r, "error", ""))[:160],
                file=keep(path, f"img_{key}"))
            if ok:
                got[key] = path
        except Exception as e:
            rec(f"出图 · {key}", False, time.time() - t0, f"{type(e).__name__}: {e}")
    return got


# ───────────────────────── 2. 建模（每个 3D 模型）─────────────────────────

def step_3d(pipe, image_path: str, keys_3d: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in keys_3d:
        if key == "multiview":
            rec("建模 · multiview", False, 0, "需要四视图，单图路径跳过（单独测）")
            continue
        t0 = time.time()
        try:
            r = pipe.generate_3d(image_path, model_key=key, quality_name="快速",
                                 max_facenum=200000, seed=42)
            ok = bool(getattr(r, "success", False) and r.output_path)
            info = mesh_info(r.output_path) if ok else {}
            rec(f"建模 · {key}", ok, time.time() - t0,
                f"{info.get('faces','?')}面 水密={info.get('watertight','?')}" if ok
                else str(getattr(r, "error", ""))[:160],
                file=keep(r.output_path, f"model_{key}") if ok else "", **info)
            if ok:
                out[key] = r.output_path
        except Exception as e:
            rec(f"建模 · {key}", False, time.time() - t0, f"{type(e).__name__}: {e}")
    return out


# ───────────────────────── 3. 后处理（贴图/骨骼/动画/精修）─────────────────

def step_post(model_path: str, image_path: str, mv_path: str) -> None:
    import importlib.util

    from backend import rigging

    # 骨骼（模板档，CPU）
    t0 = time.time()
    try:
        r = rigging.bind_model(model_path, str(OUT / "rig"), tier="template")
        rec("骨骼 · 模板", r.get("ok", False), time.time() - t0,
            f"{r.get('joints')} 关节", file=keep(r["path"], "rigged_template"))
    except Exception as e:
        rec("骨骼 · 模板", False, time.time() - t0, f"{type(e).__name__}: {e}")

    # 动画（BVH 重定向，CPU）
    motion = ROOT / "work" / "research" / "motion"
    if (motion / "人物_BVH步态_重定向.blend").is_file():
        t0 = time.time()
        try:
            inner = OUT / "logs" / "_motion.py"
            inner.parent.mkdir(parents=True, exist_ok=True)
            out_blend = OUT / "files" / "motion_walk.blend"
            inner.write_text(
                f'import sys\nsys.path.insert(0, r"{motion}")\nimport retarget_lib\n'
                f'r = retarget_lib.retarget_bvh(r"{motion / "人物_BVH步态_重定向.blend"}",'
                f' r"{motion / "bvh" / "02_01.bvh"}", r"{out_blend}", n_frames=24,'
                f' mode="cycle", action_name="e2e_walk")\nprint("METRICS", r.get("metrics"))\n',
                encoding="utf-8")
            p = subprocess.run([r"D:\tools\blender\blender-4.2.23-windows-x64\blender.exe",
                                "-b", "--factory-startup", "--python", str(inner)],
                               capture_output=True, timeout=900)
            log = (p.stdout or b"").decode("utf-8", "replace")
            line = next((ln.strip() for ln in log.splitlines() if "penetration=" in ln), "")
            rec("动画 · BVH 走路", p.returncode == 0 and out_blend.is_file(),
                time.time() - t0, line[:120])
        except Exception as e:
            rec("动画 · BVH 走路", False, time.time() - t0, f"{type(e).__name__}: {e}")
    else:
        rec("动画 · BVH 走路", False, 0, "缺基础骨架工程（work/research/motion）")

    # 贴图（fast）
    t0 = time.time()
    try:
        spec = importlib.util.spec_from_file_location("paint_asset", ROOT / "tools" / "paint_asset.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        r = mod.paint(model_path, image_path, OUT / "files" / "painted.glb",
                      texture=1024, view=512, steps=8, fast=True)
        rec("贴图 · 快速档", r.get("ok", False), time.time() - t0,
            f"贴图 {r.get('textures')} 张 峰值 {r.get('peak_vram_mib')} MB",
            file=str(OUT / "files" / "painted.glb") if r.get("ok") else "")
    except Exception as e:
        rec("贴图 · 快速档", False, time.time() - t0, f"{type(e).__name__}: {e}")

    # 精修（Direct3D 4 步）
    t0 = time.time()
    try:
        spec = importlib.util.spec_from_file_location("ar", ROOT / "tools" / "asset_refine_rig.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        r = mod.refine(Path(image_path), Path(model_path),
                       OUT / "files" / "refined.glb", steps=4)
        d = r.get("debris") or {}
        rec("精修 · Direct3D 4 步", r.get("ok", False), time.time() - t0,
            f"碎壳 {d.get('removed_parts','-')}→{d.get('kept_parts','-')} 块", file=r.get("out", ""))
    except Exception as e:
        rec("精修 · Direct3D 4 步", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ───────────────────────── 4. 用模型修改产物 ─────────────────────────

def step_modify(pipe, image_path: str, model_path: str) -> None:
    """修改 = 文字改参考图 → 用改后的图重建一次，对比两次的面数/水密。"""
    from backend import multiview_prompt as mp

    t0 = time.time()
    edited = ""
    try:
        views = {"front": image_path, "back": image_path}
        edited = mp.generate_reference(views, "只把杯子颜色改成深蓝色，形状和把手完全不变", seed=2026)
        ok = bool(edited) and Path(edited).is_file()
        rec("修改 · 文字改参考图", ok, time.time() - t0,
            Path(edited).name if ok else "无产物", file=keep(edited, "edited_front"))
    except Exception as e:
        rec("修改 · 文字改参考图", False, time.time() - t0, f"{type(e).__name__}: {e}")
        return

    t0 = time.time()
    try:
        r = pipe.generate_3d(edited, model_key="hunyuan21", quality_name="快速",
                             max_facenum=200000, seed=43)
        ok = bool(getattr(r, "success", False) and r.output_path)
        info = mesh_info(r.output_path) if ok else {}
        rec("修改 · 重建 3D", ok, time.time() - t0,
            f"{info.get('faces','?')}面 水密={info.get('watertight','?')}" if ok
            else str(getattr(r, "error", ""))[:160],
            file=keep(r.output_path, "model_after_modify") if ok else "", **info)
    except Exception as e:
        rec("修改 · 重建 3D", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ───────────────────────── main ─────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--only", default="image,3d,post,modify")
    ns = ap.parse_args()
    global OUT
    OUT = Path(ns.out)
    for d in ("files", "logs", "rig"):
        (OUT / d).mkdir(parents=True, exist_ok=True)

    threading.Thread(target=_vram_watch, daemon=True).start()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    image_keys = list(((cfg.get("models") or {}).get("image") or {}).keys())
    keys_3d = list(((cfg.get("models") or {}).get("3d") or {}).keys())
    only = {x.strip() for x in ns.only.split(",")}

    t_all = time.time()
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))

    imgs: dict[str, str] = {}
    models: dict[str, str] = {}
    if "image" in only:
        imgs = step_images(pipe, image_keys)
    base_img = imgs.get("qwen21_ref") or next(iter(imgs.values()), "")
    if not base_img and (OUT / "files").is_dir():
        cand = sorted((OUT / "files").glob("img_*"))
        base_img = str(cand[0]) if cand else ""
    if "3d" in only and base_img:
        models = step_3d(pipe, base_img, keys_3d)
    base_model = models.get("hunyuan21") or next(iter(models.values()), "")
    if "post" in only and base_model:
        step_post(base_model, base_img, models.get("multiview", ""))
    if "modify" in only and base_img and base_model:
        step_modify(pipe, base_img, base_model)

    _STOP.set()
    total = time.time() - t_all
    payload = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "total_seconds": round(total, 1),
               "peak_vram_mb": VRAM_PEAK["mb"], "prompt": PROMPT,
               "image_models": image_keys, "models_3d": keys_3d, "results": RESULTS}
    (OUT / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    ok_n = sum(1 for r in RESULTS if r["ok"])
    lines = [f"# 铸想全链路实测（{payload['when']}）", "",
             f"总耗时 **{total/60:.1f} 分钟** · 峰值显存 **{VRAM_PEAK['mb']} MB** · "
             f"成功 {ok_n}/{len(RESULTS)}", "",
             "| 步骤 | 结果 | 耗时(s) | 峰值显存(MB) | 说明 |", "|---|---|---|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['step']} | {'✓' if r['ok'] else '✗'} | {r['seconds']} | "
                     f"{r['peak_vram_mb']} | {r['detail'][:90]} |")
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  汇总 {ok_n}/{len(RESULTS)} · {total/60:.1f} 分钟 · 峰值 {VRAM_PEAK['mb']} MB")
    print(f"  报告：{OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
