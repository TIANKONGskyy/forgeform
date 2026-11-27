# -*- coding: utf-8 -*-
"""五项能力详细测试驱动（2026-11-27）。

每项都走产品入口，记录 ok/耗时/关键指标，最后打印 JSON 汇总。
跑之前确保 ComfyUI(:8188) 在线、GPU 空闲。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\ForgeForm-oss")
sys.path.insert(0, str(ROOT))
OUT = Path(r"D:\tmp\verify_all")
OUT.mkdir(parents=True, exist_ok=True)
RESULTS = []


def record(area, name, ok, seconds, detail="", **extra):
    row = {"area": area, "name": name, "ok": bool(ok), "seconds": round(seconds, 1),
           "detail": str(detail)[:300], **extra}
    RESULTS.append(row)
    print(f"  {'✓' if ok else '✗'} [{area}] {name:<28} {row['seconds']:>7.1f}s  {row['detail'][:110]}",
          flush=True)


def gpu():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"], text=True, timeout=10).strip()
        return [int(x.strip()) for x in out.split(",")[:3]]
    except Exception:
        return [-1, -1, -1]


# ───────────────────── ① 骨骼线 ─────────────────────
def test_rig():
    from backend import rigging

    cases = [
        ("模板·飞机", ROOT / "outputs/art/casts/rabbit/model.stl", "template", "chain"),
        ("RigAnything·人形", ROOT / "work/research/rig/assets/human_body.stl",
         "riganything", "auto"),
    ]
    for name, mesh, tier, tmpl in cases:
        if not mesh.is_file():
            record("骨骼", name, False, 0, f"缺素材 {mesh}")
            continue
        t0 = time.time()
        try:
            r = rigging.bind_model(str(mesh), str(OUT / f"rig_{tier}"),
                                   tier=tier, template=tmpl)
            record("骨骼", name, r.get("ok", False), time.time() - t0,
                   f"joints={r.get('joints')} skins={r.get('skins')}", joints=r.get("joints"))
        except Exception as e:
            record("骨骼", name, False, time.time() - t0, f"{type(e).__name__}: {e}")


# ───────────────────── ② 动画 ─────────────────────
def test_motion():
    root = ROOT / "work/research/motion"
    blender = r"D:\tools\blender\blender-4.2.23-windows-x64\blender.exe"
    blend = root / "人物_BVH步态_重定向.blend"
    for bvh in ("02_01.bvh", "09_01_run.bvh", "08_01.bvh"):
        src = root / "bvh" / bvh
        if not src.is_file():
            record("动画", bvh, False, 0, "缺 BVH")
            continue
        inner = OUT / f"_motion_{bvh.replace('.', '_')}.py"
        inner.write_text(
            f'import sys, json\n'
            f'sys.path.insert(0, r"{root}")\n'
            f'import retarget_lib\n'
            f'r = retarget_lib.retarget_bvh(r"{blend}", r"{src}",\n'
            f'    r"{OUT / ("motion_" + bvh.replace(".", "_") + ".blend")}",\n'
            f'    n_frames=24, mode="cycle", action_name="t_{bvh[:5]}")\n'
            f'print("METRICS", json.dumps(r.get("metrics") if isinstance(r, dict) else {{}}))\n',
            encoding="utf-8")
        t0 = time.time()
        p = subprocess.run([blender, "-b", "--factory-startup", "--python", str(inner)],
                           capture_output=True, timeout=900)
        log = (p.stdout or b"").decode("utf-8", "replace") + \
              (p.stderr or b"").decode("utf-8", "replace")
        mline = next((ln for ln in log.splitlines() if "METRICS" in ln), "")
        pen = slip = ""
        for ln in log.splitlines():
            if "penetration=" in ln:
                pen = ln.strip()[:140]
        ok = p.returncode == 0 and bool(mline)
        record("动画", f"BVH {bvh}", ok, time.time() - t0, pen or log[-100:])


# ───────────────────── ③ 精度提升 ─────────────────────
def test_refine():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ar", ROOT / "tools" / "asset_refine_rig.py")
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)
    base = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3\assets")
    img, mesh = base / "input_imgs" / "6.png", base / "coarse_meshes" / "6.obj"
    for steps in (4, 15):
        t0 = time.time()
        r = ar.refine(img, mesh, OUT / f"refine_s{steps}.glb", steps=steps)
        d = r.get("debris") or {}
        record("精度", f"Direct3D-V3 {steps} 步", r.get("ok", False), time.time() - t0,
               f"debris={d.get('removed_parts', '-')} 块 → {d.get('kept_parts', '-')} 块 / "
               f"{d.get('faces', '-')} 面", faces=d.get("faces"))


# ───────────────────── ④ 多视图 ─────────────────────
def test_multiview():
    from backend.pipeline import Pipeline
    import trimesh
    pipe = Pipeline(str(ROOT / "config.json"))
    views = {k: str(Path(r"D:\tmp\mv_real2") / f"mv_{k}.png")
             for k in ("front", "back", "left", "right")}
    if not all(Path(v).is_file() for v in views.values()):
        record("多视图", "真实照片 4 视图", False, 0, "缺输入图")
        return
    t0 = time.time()
    r = pipe.generate_3d_multiview(views, quality_name="高质量", seed=42,
                                   octree_override=512, max_facenum=500000)
    secs = time.time() - t0
    if getattr(r, "success", False) and r.output_path:
        m = trimesh.load(str(r.output_path), force="mesh")
        record("多视图", "真实照片 4 视图", True, secs,
               f"{len(m.faces):,} 面 水密={m.is_watertight}", faces=len(m.faces))
    else:
        record("多视图", "真实照片 4 视图", False, secs,
               str(getattr(r, "error", ""))[:200])


# ───────────────────── ⑤ 修改 ─────────────────────
def test_edit():
    from backend import multiview_prompt as mp
    views = {"front": r"D:\tmp\mv_real2\mv_front.png",
             "back": r"D:\tmp\mv_real2\mv_back.png"}
    cases = [
        ("模糊指令（应暴露风险）", "把机身改成红色涂装，其余保持不变"),
        ("具体指令（应保住主体）", "只在机身加一条红色条纹，飞机外形/发动机/螺旋桨/浮筒全部保持完全一致"),
    ]
    for name, prompt in cases:
        t0 = time.time()
        try:
            out = mp.generate_reference(views, prompt, seed=20261127)
            p = Path(out)
            record("修改", name, p.is_file() and p.stat().st_size > 1000,
                   time.time() - t0, f"{p.name} {p.stat().st_size//1024 if p.is_file() else 0} KB")
        except Exception as e:
            record("修改", name, False, time.time() - t0, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    only = (sys.argv[1] if len(sys.argv) > 1 else "all")
    mem, temp, util = gpu()
    print(f"  起始 GPU: {mem} MiB / {temp}°C / util {util}%", flush=True)
    if only in ("all", "rig"):
        test_rig()
    if only in ("all", "motion"):
        test_motion()
    if only in ("all", "refine"):
        test_refine()
    if only in ("all", "multiview"):
        test_multiview()
    if only in ("all", "edit"):
        test_edit()
    Path(OUT / "results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n  汇总: {passed}/{len(RESULTS)} 通过", flush=True)
