# -*- coding: utf-8 -*-
"""铸想全流程端到端测试：LLM 扩写 → 出图 → 建模 → STL。

跑的是真入口（pipeline 的方法），不绕过：
    enhance_prompt / generate_images / generate_3d / export_stl

每步记耗时 + 产物 + 关键指标，最后写出 outputs/e2e/e2e_report.json。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, r"D:\ForgeForm-oss")
ROOT = Path(r"D:\ForgeForm-oss")
OUT = ROOT / "outputs" / "e2e"

PROMPT = "做一个猫头鹰摆件，翅膀收拢，圆胖身体，陶土质感，能直接 3D 打印"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["backend"] = {"mode": "auto"}     # 出图走 ComfyUI，建模再切直连
    cfg["stages"]["image"]["default_model"] = "z_image"
    tmp = Path(r"C:\Users\mac\AppData\Local\Temp\e2e_cfg.json")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    from backend.pipeline import Pipeline

    rep: dict = {"prompt_zh": PROMPT, "steps": [], "ok": False}
    t_all = time.time()

    def step(name: str, ok: bool, sec: float, **extra) -> None:
        rec = {"step": name, "ok": ok, "sec": round(sec, 1), **extra}
        rep["steps"].append(rec)
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] {name:<14} {sec:>7.1f}s  "
              f"{extra.get('detail', '')}", flush=True)

    pipe = Pipeline(str(tmp))
    print("=" * 70)
    print("铸想全流程 · LLM 扩写 → 出图 → 建模 → STL")
    print("=" * 70)
    print(f"输入: {PROMPT}\n")

    # ── 1. LLM 扩写 ──────────────────────────────────────────
    print("[1] LLM 扩写")
    t0 = time.time()
    enhanced = ""
    try:
        enhanced = pipe.enhance_prompt(PROMPT)
        ok = bool(enhanced) and enhanced != PROMPT
        step("llm_enhance", ok, time.time() - t0,
             detail=f"{len(PROMPT)} → {len(enhanced)} 字符",
             output=enhanced[:400] if ok else "")
        print(f"        {enhanced[:160]}")
    except Exception as exc:
        step("llm_enhance", False, time.time() - t0, detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()

    # ── 2. 出图 ──────────────────────────────────────────────
    print("\n[2] 出图（z_image）")
    t0 = time.time()
    img_path = ""
    try:
        imgs = pipe.generate_images(PROMPT, count=1, model_key="z_image",
                                    quality_name="标准", resolution="1024×1024",
                                    skip_cache=True)
        ok = bool(imgs and getattr(imgs[0], "success", False))
        if ok:
            img_path = str(imgs[0].output_path)
            step("image", True, time.time() - t0,
                 detail=f"{Path(img_path).name} "
                        f"{Path(img_path).stat().st_size // 1024} KB",
                 path=img_path)
        else:
            step("image", False, time.time() - t0,
                 detail=str(getattr(imgs[0], "error", "?"))[:120] if imgs else "无返回")
    except Exception as exc:
        step("image", False, time.time() - t0, detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()

    # ── 3. 建模（直连 2mini，带后处理）───────────────────────
    print("\n[3] 建模（2mini 直连 · 512 octree）")
    glb_path = ""
    if img_path:
        cfg["backend"] = {"mode": "direct"}
        tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        pipe2 = Pipeline(str(tmp))
        t0 = time.time()
        try:
            r = pipe2.generate_3d(model_key="hunyuan_mini", quality_name="标准",
                                  image_path=img_path, octree_override=512)
            ok = bool(getattr(r, "success", False))
            if ok:
                glb_path = str(r.output_path)
                import trimesh
                m = trimesh.load(glb_path, force="mesh")
                d = m.bounds[1] - m.bounds[0]
                step("mesh", True, time.time() - t0,
                     detail=f"{len(m.faces):,} 面 · "
                            f"{d[0]:.2f}×{d[1]:.2f}×{d[2]:.2f} · "
                            f"厚度 {d[2] / max(d[0], d[1]) * 100:.0f}% · "
                            f"水密 {bool(m.is_watertight)}",
                     path=glb_path,
                     faces=int(len(m.faces)),
                     thickness_pct=round(float(d[2] / max(d[0], d[1]) * 100), 1),
                     watertight=bool(m.is_watertight))
            else:
                step("mesh", False, time.time() - t0,
                     detail=str(getattr(r, "error", "?"))[:120])
        except Exception as exc:
            step("mesh", False, time.time() - t0, detail=f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
    else:
        step("mesh", False, 0, detail="跳过（没有图）")

    # ── 4. STL ───────────────────────────────────────────────
    print("\n[4] 导出 STL（水密 + 毫米）")
    if glb_path:
        t0 = time.time()
        try:
            s = pipe2.export_stl(glb_path, target_size_mm=80)
            ok = bool(getattr(s, "success", False))
            if ok:
                stl = Path(s.output_path)
                import trimesh
                m = trimesh.load(str(stl), force="mesh")
                step("stl", True, time.time() - t0,
                     detail=f"{stl.name} {stl.stat().st_size // 1024} KB · "
                            f"{len(m.faces):,} 面 · 水密 {bool(m.is_watertight)} · "
                            f"尺寸 {m.extents[0]:.0f}×{m.extents[1]:.0f}×{m.extents[2]:.0f} mm",
                     path=str(stl),
                     watertight=bool(m.is_watertight),
                     extents_mm=[round(float(x), 1) for x in m.extents])
                rep["stl"] = str(stl)
            else:
                step("stl", False, time.time() - t0,
                     detail=str(getattr(s, "error", "?"))[:120])
        except Exception as exc:
            step("stl", False, time.time() - t0, detail=f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
    else:
        step("stl", False, 0, detail="跳过（没有模型）")

    total = time.time() - t_all
    rep["total_sec"] = round(total, 1)
    rep["ok"] = all(s["ok"] for s in rep["steps"])
    (OUT / "e2e_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    okn = sum(1 for s in rep["steps"] if s["ok"])
    print(f"{okn}/{len(rep['steps'])} 步通过 · 总耗时 {total / 60:.1f} 分钟")
    print(f"报告: {OUT / 'e2e_report.json'}")
    print("=" * 70)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
