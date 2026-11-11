# -*- coding: utf-8 -*-
"""用下载好的 Hunyuan3D-2mini 跑一次真实 3D，验证 direct 模式的 3D 链路。

    python tools/bench_hy3d.py --model-dir D:/models/hy3dgen \
        --repo hunyuan3d-2mini --subfolder hunyuan3d-dit-v2-mini \
        --image D:/ForgeForm-oss/docs/showcase/coupling/image.png

hy3dgen 的目录约定（`smart_load_model` 里写死的）：

    $HY3DGEN_MODELS/<repo>/<subfolder>/config.yaml
    $HY3DGEN_MODELS/<repo>/<subfolder>/model.fp16.safetensors
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=r"D:/models/hy3dgen",
                    help="HY3DGEN_MODELS 的根目录")
    ap.add_argument("--repo", default="hunyuan3d-2mini")
    ap.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini")
    ap.add_argument("--image", default=str(ROOT / "docs/showcase/coupling/image.png"))
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--octree", type=int, default=256)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    img = Path(args.image)
    if not img.exists():
        print(f"输入图不存在: {img}")
        return 1

    os.environ["HY3DGEN_MODELS"] = args.model_dir
    model_root = Path(args.model_dir) / args.repo / args.subfolder
    print(f"模型目录: {model_root}")
    for f in ("config.yaml", "model.fp16.safetensors"):
        p = model_root / f
        print(f"  {'✓' if p.exists() else '✗'} {f}"
              + (f"  {p.stat().st_size/1024**3:.2f} GB" if p.exists() else ""))

    import torch
    from PIL import Image
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    print(f"\n[1] 加载 {args.repo}/{args.subfolder}")
    t0 = time.time()
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        args.repo, subfolder=args.subfolder, device="cuda", dtype=torch.float16)
    print(f"    加载 {time.time()-t0:.1f}s")

    image = Image.open(img).convert("RGBA")
    print(f"\n[2] 推理 steps={args.steps} octree={args.octree}")
    t1 = time.time()
    meshes = pipe(image=image, num_inference_steps=args.steps,
                  guidance_scale=6.0, octree_resolution=args.octree,
                  num_chunks=8000, output_type="trimesh")
    infer = time.time() - t1
    mesh = meshes[0] if isinstance(meshes, (list, tuple)) else meshes
    print(f"    推理 {infer:.1f}s  faces={len(mesh.faces)} vertices={len(mesh.vertices)}")
    print(f"    watertight={bool(mesh.is_watertight)}  "
          f"bbox={[round(float(x),3) for x in mesh.bounding_box.extents]}")

    out = Path(args.out) if args.out else Path("direct-bench") / "bench_hy3d.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out))
    print(f"\n[3] 产物: {out}  ({out.stat().st_size/1024**2:.1f} MB)")
    print(f"\n✅ 直连 3D 通过  加载 {time.time()-t0-infer:.1f}s + 推理 {infer:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
