# -*- coding: utf-8 -*-
"""同一个物件，两种建模后端并排比：ComfyUI 的 Hunyuan3D 2.1 vs 直连的 2mini。

    python tools/bench_mesh.py --image D:/forgeform/outputs/images/xxx.png

产出：
  · 两次的耗时、面数、水密、包围盒
  · 两份 STL，以及一张四视角对比图
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(ROOT))
    ap.add_argument("--image", default="", help="输入图；空则用 outputs/images 里最新的一张")
    ap.add_argument("--quality", default="快速")
    ap.add_argument("--octree", type=int, default=256, help="直连 2mini 的 octree")
    ap.add_argument("--size", type=int, default=80)
    ap.add_argument("--models", default="hunyuan21,mini", help="要跑的：hunyuan21 / mini")
    args = ap.parse_args()

    home = Path(args.home).resolve()
    sys.path.insert(0, str(home))
    os.chdir(home)

    cfg_path = home / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # 输入图
    if args.image:
        img = Path(args.image)
    else:
        cands = sorted((home / "outputs" / "images").glob("*.png"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("outputs/images 里没有图，先用 generate_images 出一张")
            return 1
        img = cands[-1]
    print(f"输入图: {img}  ({img.stat().st_size//1024} KB)")

    want = [m.strip() for m in args.models.split(",") if m.strip()]
    results = {}

    # ── ComfyUI 的 Hunyuan3D 2.1 ─────────────────────────
    if "hunyuan21" in want:
        print("\n[1] ComfyUI · Hunyuan3D 2.1", flush=True)
        from backend.pipeline import Pipeline
        pipe = Pipeline(str(cfg_path))
        t0 = time.time()
        try:
            r = pipe.generate_3d(model_key="hunyuan21", quality_name=args.quality,
                                 image_path=str(img))
            dt = time.time() - t0
            if getattr(r, "success", False):
                glb = Path(r.output_path)
                stl = _to_stl(pipe, glb, args.size)
                info = _info(stl or glb)
                print(f"  ✓ {dt:.1f}s  {glb.name}  {info}")
                results["hunyuan21"] = {"ok": True, "sec": round(dt, 1),
                                        "glb": str(glb), "stl": str(stl or ""), **info}
            else:
                print(f"  ✗ {dt:.1f}s  {getattr(r, 'error', '未知')}")
                results["hunyuan21"] = {"ok": False, "sec": round(dt, 1),
                                        "error": str(getattr(r, "error", ""))[:160]}
        except Exception as exc:
            print(f"  ✗ {time.time()-t0:.1f}s  {type(exc).__name__}: {exc}")
            results["hunyuan21"] = {"ok": False, "sec": round(time.time() - t0, 1),
                                    "error": f"{type(exc).__name__}: {exc}"[:160]}

    # ── 直连的 hy3dgen + 2mini ───────────────────────────
    if "mini" in want:
        print(f"\n[2] 直连 · hy3dgen + Hunyuan3D 2mini (octree {args.octree})", flush=True)
        model_root = Path(r"D:/models/hy3dgen")
        os.environ["HY3DGEN_MODELS"] = str(model_root)
        t0 = time.time()
        try:
            import torch
            import trimesh
            from PIL import Image
            from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
            tload = time.time()
            hp = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
                "hunyuan3d-2mini", subfolder="hunyuan3d-dit-v2-mini",
                device="cuda", dtype=torch.float16)
            load_s = time.time() - tload
            print(f"  加载 {load_s:.1f}s", flush=True)
            tinf = time.time()
            meshes = hp(image=Image.open(img).convert("RGBA"), num_inference_steps=30,
                        guidance_scale=6.0, octree_resolution=args.octree,
                        num_chunks=8000, output_type="trimesh")
            infer_s = time.time() - tinf
            mesh = meshes[0] if isinstance(meshes, (list, tuple)) else meshes
            out_dir = home / "outputs" / "3d"
            out_dir.mkdir(parents=True, exist_ok=True)
            glb = out_dir / f"mesh_mini_{int(time.time())}.glb"
            mesh.export(str(glb))
            info = {"faces": len(mesh.faces), "vertices": len(mesh.vertices),
                    "watertight": bool(mesh.is_watertight),
                    "bbox": [round(float(x), 2) for x in mesh.bounding_box.extents]}
            stl = _to_stl_direct(glb, home, args.size)
            print(f"  ✓ 加载 {load_s:.1f}s + 推理 {infer_s:.1f}s = {time.time()-t0:.1f}s  "
                  f"{glb.name}  {info}", flush=True)
            results["mini"] = {"ok": True, "sec": round(time.time() - t0, 1),
                               "load_sec": round(load_s, 1), "infer_sec": round(infer_s, 1),
                               "glb": str(glb), "stl": str(stl or ""), **info}
        except Exception as exc:
            print(f"  ✗ {time.time()-t0:.1f}s  {type(exc).__name__}: {exc}")
            results["mini"] = {"ok": False, "sec": round(time.time() - t0, 1),
                               "error": f"{type(exc).__name__}: {exc}"[:160]}

    # ── 汇总 + 渲染对比 ─────────────────────────────────
    print("\n" + "=" * 74)
    print(f"{'后端':<14} {'耗时':>9} {'面数':>10} {'水密':>7} {'厚度/尺寸(mm)':>20}")
    print("-" * 74)
    for k, label in (("hunyuan21", "ComfyUI 2.1"), ("mini", "直连 2mini")):
        r = results.get(k)
        if not r:
            continue
        if r.get("ok"):
            bb = r.get("bbox") or []
            bbs = "×".join(str(x) for x in bb) if bb else "-"
            print(f"{label:<14} {r['sec']:>8.1f}s {r.get('faces', '-'):>10} "
                  f"{str(r.get('watertight', '-')):>7} {bbs:>20}")
        else:
            print(f"{label:<14} {r['sec']:>8.1f}s   失败: {r.get('error','')[:34]}")
    print("=" * 74)

    out = home / "outputs" / "bench_mesh.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据: {out}")

    stls = [Path(r["stl"]) for r in results.values() if r.get("ok") and r.get("stl")]
    if len(stls) >= 2:
        _render_pair(stls, home / "outputs" / "bench_mesh.png", list(results.keys()))
    return 0 if all(r.get("ok") for r in results.values()) else 1


def _to_stl(pipe, glb: Path, size: int):
    try:
        s = pipe.export_stl(str(glb), target_size_mm=size)
        return Path(s.output_path) if getattr(s, "success", False) else None
    except Exception:
        return None


def _to_stl_direct(glb: Path, home: Path, size: int):
    """直连模式的 STL 导出走同一套 mesh_optimizer（和 app 里点"导出 STL"一致）。"""
    try:
        from backend.mesh_optimizer import MeshOptimizer, preset_print_ready
        mo = MeshOptimizer(output_dir=home / "outputs" / "stl")
        res = mo.optimize(glb, preset_print_ready())
        p = getattr(res, "output_path", None) or getattr(res, "stl_path", None)
        return Path(p) if p and Path(p).exists() else None
    except Exception as exc:
        print(f"  STL 导出跳过: {type(exc).__name__}: {exc}")
        return None


def _info(path: Path) -> dict:
    try:
        import trimesh
        m = trimesh.load(str(path), force="mesh")
        return {"faces": len(m.faces), "vertices": len(m.vertices),
                "watertight": bool(m.is_watertight),
                "bbox": [round(float(x), 2) for x in m.bounding_box.extents]}
    except Exception:
        return {}


def _render_pair(stls: list[Path], out: Path, labels: list[str]) -> None:
    try:
        import numpy as np
        import trimesh
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except Exception:
        font = ImageFont.load_default()

    cell = 320
    pad = 12
    lab = 28
    W = len(stls) * cell + (len(stls) + 1) * pad
    H = 2 * (cell + lab) + 3 * pad
    canvas = Image.new("RGB", (W, H), (8, 12, 20))
    draw = ImageDraw.Draw(canvas)

    for idx, stl in enumerate(stls):
        m = trimesh.load(str(stl), force="mesh")
        views = [("front", (0, 0, 1)), ("iso", (1, -1, 1))]
        for row, (name, vec) in enumerate(views):
            pts = m.vertices
            v = np.array(vec, dtype=float)
            v /= np.linalg.norm(v)
            up = np.array([0, 0, 1.0]) if abs(v[2]) < 0.9 else np.array([0, 1.0, 0])
            right = np.cross(up, v)
            right /= np.linalg.norm(right) or 1
            up = np.cross(v, right)
            px = pts @ right
            py = pts @ up
            w = px.max() - px.min() or 1
            h = py.max() - py.min() or 1
            scale = (cell * 0.86) / max(w, h)
            fx = ((px - px.min()) * scale + (cell - w * scale) / 2).astype(int)
            fy = (cell - ((py - py.min()) * scale + (cell - h * scale) / 2)).astype(int)
            z = pts @ v
            order = np.argsort(z)
            img = Image.new("RGB", (cell, cell), (10, 15, 26))
            d2 = ImageDraw.Draw(img)
            faces = m.faces
            for f in faces[np.argsort(z[faces].mean(axis=1))]:
                x = fx[f]; y = fy[f]
                zz = z[f].mean()
                t = (zz - z.min()) / (z.max() - z.min() or 1)
                c = (int(150 + 90 * t), int(165 + 80 * t), int(200 + 50 * t))
                d2.polygon(list(zip(x, y)), fill=c, outline=(40, 55, 80))
            cx = pad + idx * (cell + pad)
            cy = pad + row * (cell + lab)
            canvas.paste(img, (cx, cy))
            if row == 0:
                draw.text((cx + 4, cy + cell + 4), labels[idx], fill=(150, 200, 255), font=font)
    canvas.save(out)
    print(f"对比图: {out}")


if __name__ == "__main__":
    sys.exit(main())
