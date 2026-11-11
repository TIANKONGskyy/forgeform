# -*- coding: utf-8 -*-
"""端到端基础能力验证：对话 → 出图 → 建模 → STL。

在**模拟安装的环境**（D:\\ff_simulate，从 GitHub clone 的）里跑，
证明"新装的人拿到的这份是能真干活的"。

    python tools/selftest_e2e.py --home D:/ff_simulate
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMPT = "做一个六角法兰联轴器，中心有轴孔和六个螺栓孔，金属质感，单件"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(ROOT), help="仓库根（跑哪个副本）")
    ap.add_argument("--quality", default="快速", choices=["快速", "标准", "高质量"])
    ap.add_argument("--size", type=int, default=80)
    args = ap.parse_args()

    home = Path(args.home).resolve()
    sys.path.insert(0, str(home))
    import os
    os.chdir(home)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    cfg_path = home / "config.json"
    if not cfg_path.exists():
        print(f"✗ {cfg_path} 不存在（先启动一次 app.py 让它生成）")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    print(f"仓库: {home}")
    print(f"配置: backend={((cfg.get('backend') or {}).get('mode'))}  "
          f"image={(cfg.get('stages') or {}).get('image', {}).get('default_model')}  "
          f"3d={(cfg.get('stages') or {}).get('3d', {}).get('default_model')}")

    results = {}
    t_all = time.time()

    # ── 1. 对话 / agent 解析 ──────────────────────────────
    print("\n[1/4] 对话：让 agent 解析这句话", flush=True)
    from backend import agent
    t0 = time.time()
    try:
        if not agent.is_ready():
            agent.warm_up()
        state = {
            "phase": "describing", "requirement": "", "auto_params": {},
            "conversation": [], "candidates": [], "selected": "",
            "glb_path": "", "stl_path": "",
        }
        plan = agent.plan(PROMPT, state, timeout=90)
        reply = (plan or {}).get("reply", "")
        actions = [a.get("name") for a in (plan or {}).get("actions", [])]
        dt = time.time() - t0
        print(f"    {dt:.1f}s  动作={actions}")
        print(f"    回复（前 160 字）: {reply[:160]}")
        results["agent"] = {"ok": bool(plan), "s": round(dt, 1), "actions": actions}
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {exc}")
        results["agent"] = {"ok": False, "error": str(exc)[:200]}

    # ── 2. 出图 ──────────────────────────────────────────
    print("\n[2/4] 出图", flush=True)
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(cfg_path))
    t0 = time.time()
    img = None
    try:
        res = pipe.generate_images(PROMPT, count=1, model_key="qwen21_ref",
                                   quality_name="标准", resolution="1024×1024",
                                   skip_cache=True)
        dt = time.time() - t0
        if res and getattr(res[0], "success", False):
            img = Path(res[0].output_path)
            kb = img.stat().st_size // 1024
            print(f"    {dt:.1f}s  → {img.name}  {kb} KB")
            results["image"] = {"ok": True, "s": round(dt, 1), "file": str(img), "kb": kb}
        else:
            err = getattr(res[0], "error", "未知") if res else "无返回"
            print(f"    ✗ 失败: {err}")
            results["image"] = {"ok": False, "s": round(dt, 1), "error": str(err)[:200]}
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {exc}")
        results["image"] = {"ok": False, "error": str(exc)[:200]}

    if not img or not img.exists():
        print("\n出图没成功，后面的无法继续")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1

    # ── 3. 建模 ──────────────────────────────────────────
    print(f"\n[3/4] 建模（{args.quality}）", flush=True)
    t0 = time.time()
    glb = None
    try:
        r = pipe.generate_3d(model_key="hunyuan21", quality_name=args.quality,
                             image_path=str(img))
        dt = time.time() - t0
        if getattr(r, "success", False):
            glb = Path(r.output_path)
            kb = glb.stat().st_size // 1024
            print(f"    {dt:.1f}s  → {glb.name}  {kb} KB")
            results["mesh"] = {"ok": True, "s": round(dt, 1), "file": str(glb), "kb": kb}
        else:
            err = getattr(r, "error", "未知")
            print(f"    ✗ 失败: {err}")
            results["mesh"] = {"ok": False, "s": round(dt, 1), "error": str(err)[:200]}
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {exc}")
        results["mesh"] = {"ok": False, "error": str(exc)[:200]}

    if not glb or not glb.exists():
        print("\n建模没成功，后面的无法继续")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1

    # ── 4. 几何 + STL ────────────────────────────────────
    print("\n[4/4] 几何检查 + 导出 STL", flush=True)
    try:
        import trimesh
        m = trimesh.load(str(glb), force="mesh")
        faces, verts = len(m.faces), len(m.vertices)
        wt = bool(m.is_watertight)
        bbox = [round(float(x), 2) for x in m.bounding_box.extents]
        print(f"    faces={faces}  vertices={verts}  watertight={wt}  bbox={bbox}")

        t0 = time.time()
        s = pipe.export_stl(str(glb), target_size_mm=args.size)
        dt = time.time() - t0
        if getattr(s, "success", False):
            stl = Path(s.output_path)
            m2 = trimesh.load(str(stl), force="mesh")
            print(f"    STL {dt:.1f}s → {stl.name}  {stl.stat().st_size//1024} KB  "
                  f"watertight={bool(m2.is_watertight)}  "
                  f"bbox={[round(float(x),1) for x in m2.bounding_box.extents]}")
            results["stl"] = {"ok": True, "s": round(dt, 1), "file": str(stl),
                              "watertight": bool(m2.is_watertight)}
        else:
            print(f"    ✗ STL 失败: {getattr(s, 'error', '未知')}")
            results["stl"] = {"ok": False, "error": str(getattr(s, 'error', ''))[:200]}
        results["mesh"]["faces"] = faces
        results["mesh"]["watertight"] = wt
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {exc}")
        results["stl"] = {"ok": False, "error": str(exc)[:200]}

    total = time.time() - t_all
    print(f"\n{'='*58}")
    for k, label in (("agent", "对话"), ("image", "出图"), ("mesh", "建模"), ("stl", "STL")):
        r = results.get(k) or {}
        print(f"  {label:4} {'✓' if r.get('ok') else '✗'}  {r.get('s', '-')}s")
    print(f"  总计 {total/60:.1f} 分钟")
    print(f"{'='*58}")
    out = home / "outputs" / "selftest_e2e.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all((results.get(k) or {}).get("ok") for k in ("agent", "image", "mesh", "stl")) else 1


if __name__ == "__main__":
    sys.exit(main())
