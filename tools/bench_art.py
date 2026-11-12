# -*- coding: utf-8 -*-
"""艺术品类 2mini 建模测试（两段式）。

出图必须走 ComfyUI（z_image 是 GGUF，直连加载不了）；
建模强制直连（只有直连才吃得到逐层搬显存 + fp16 构建那两个优化）。

    python tools/bench_art.py            # 出图 + 建模
    python tools/bench_art.py --mesh-only  # 只建模（图已经出过）
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\ForgeForm-oss")
ROOT = Path(r"D:\ForgeForm-oss")
OUT = ROOT / "outputs" / "art"

ITEMS = [
    ("deer", "一只卧着的小鹿摆件，鹿角分叉，四肢收在身下，青铜质感"),
    ("owl", "一只猫头鹰摆件，翅膀收拢，圆胖的身体，陶土质感"),
    ("dragon", "一条盘旋的东方龙摆件，身体呈 S 形，鳞片分明，铜质"),
    ("abstract", "一座抽象雕塑，三个扭曲的环互相穿插，哑光白瓷"),
]

MESH_KEY = "hunyuan_mini"
OCTREE = 512
STEPS = 30


def cfg_with(mode: str) -> Path:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["backend"] = {"mode": mode}
    p = Path(rf"C:\Users\mac\AppData\Local\Temp\cfg_art_{mode}.json")
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


def main() -> int:
    mesh_only = "--mesh-only" in sys.argv
    from backend.pipeline import Pipeline

    OUT.mkdir(parents=True, exist_ok=True)
    state_path = OUT / "art_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # ── 1. 出图：走 ComfyUI ──
    if not mesh_only:
        gen = Pipeline(str(cfg_with("auto")))
        for key, prompt in ITEMS:
            print(f"\n[出图] {key}")
            t0 = time.time()
            try:
                imgs = gen.generate_images(prompt, count=1, model_key="z_image",
                                           quality_name="标准", resolution="1024×1024",
                                           skip_cache=True)
                if imgs and getattr(imgs[0], "success", False):
                    p = Path(imgs[0].output_path)
                    state[key] = {"prompt": prompt, "image": str(p),
                                  "image_sec": round(time.time() - t0, 1)}
                    print(f"  ✓ {state[key]['image_sec']}s  {p.name}")
                else:
                    print(f"  ✗ {getattr(imgs[0], 'error', '?') if imgs else '无返回'}")
            except Exception as exc:
                print(f"  ✗ {type(exc).__name__}: {exc}")
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 2. 建模：强制直连 ──
    pipe = Pipeline(str(cfg_with("direct")))
    rows = []
    import trimesh

    for key, prompt in ITEMS:
        st = state.get(key)
        if not st or not st.get("image"):
            print(f"\n[建模] {key} 跳过（没有图）")
            continue
        print(f"\n[建模] {key}  ← {Path(st['image']).name}")
        row = {"key": key, "prompt": prompt, "image": st["image"],
               "image_sec": st.get("image_sec")}
        t1 = time.time()
        try:
            r = pipe.generate_3d(model_key=MESH_KEY, quality_name="标准",
                                 image_path=st["image"],
                                 octree_override=OCTREE)
            if not getattr(r, "success", False):
                row["error"] = str(getattr(r, "error", "?"))[:180]
                print(f"  ✗ {row['error']}")
                rows.append(row)
                continue
            glb = Path(r.output_path)
            row["mesh_sec"] = round(time.time() - t1, 1)
            m = trimesh.load(str(glb), force="mesh")
            bb = m.bounds
            d = bb[1] - bb[0]
            row.update({
                "glb": str(glb),
                "faces": int(len(m.faces)),
                "watertight": bool(m.is_watertight),
                "size": [round(float(x), 3) for x in d],
                "thickness_pct": round(float(d[2] / max(d[0], d[1]) * 100), 1),
                "glb_mb": round(glb.stat().st_size / 1024 / 1024, 2),
            })
            print(f"  ✓ {row['mesh_sec']}s  {row['faces']:,} 面  "
                  f"{d[0]:.2f}×{d[1]:.2f}×{d[2]:.2f}  厚度 {row['thickness_pct']}%  "
                  f"水密 {row['watertight']}")
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:180]
            print(f"  ✗ {row['error']}")
        rows.append(row)

    (OUT / "art_bench.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 70}")
    print(f"{'对象':<10} {'出图':>6} {'建模':>7} {'面数':>10} {'厚度%':>7} {'水密':>5}")
    print("-" * 70)
    for r in rows:
        if "error" in r:
            print(f"{r['key']:<10}  {r['error'][:52]}")
            continue
        print(f"{r['key']:<10} {r.get('image_sec', 0):>5}s {r['mesh_sec']:>6}s "
              f"{r['faces']:>10,} {r['thickness_pct']:>7.1f} "
              f"{'✓' if r['watertight'] else '✗':>5}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
