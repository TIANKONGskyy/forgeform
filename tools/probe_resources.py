# -*- coding: utf-8 -*-
"""跑一次直连 3D，同时采样显存与内存峰值 —— 用来回答「8G 显存 / 16G 内存能不能跑」。

    python tools/probe_resources.py --model-dir D:/models/hy3dgen

输出峰值显存、峰值内存、峰值 GPU 利用率、耗时。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PEAK = {"vram_used": 0.0, "vram_total": 0.0, "ram_used": 0.0, "ram_total": 0.0,
        "proc_rss": 0.0, "gpu_util": 0.0, "temp": 0.0}
_STOP = threading.Event()


def _sample() -> None:
    import os
    import psutil
    me = psutil.Process(os.getpid())
    vm = psutil.virtual_memory()
    PEAK["ram_total"] = vm.total / 1024 ** 3
    PEAK["ram_used"] = vm.used / 1024 ** 3
    PEAK["proc_rss"] = me.memory_info().rss / 1024 ** 3
    while not _STOP.is_set():
        try:
            vm = psutil.virtual_memory()
            PEAK["ram_used"] = max(PEAK["ram_used"], vm.used / 1024 ** 3)
            # 当前进程的 RSS 才是"这个模型真正吃多少内存"，系统 used 含别的进程
            PEAK["proc_rss"] = max(PEAK["proc_rss"], me.memory_info().rss / 1024 ** 3)
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=6,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line = (out.stdout or "").strip().splitlines()
            if line:
                p = [x.strip() for x in line[0].split(",")]
                PEAK["vram_used"] = max(PEAK["vram_used"], float(p[0]) / 1024)
                PEAK["vram_total"] = float(p[1]) / 1024
                PEAK["gpu_util"] = max(PEAK["gpu_util"], float(p[2]))
                PEAK["temp"] = max(PEAK["temp"], float(p[3]))
        except Exception:
            pass
        time.sleep(0.4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=r"D:/models/hy3dgen")
    ap.add_argument("--repo", default="hunyuan3d-2mini")
    ap.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini")
    ap.add_argument("--image", default=str(ROOT / "docs/showcase/coupling/image.png"))
    ap.add_argument("--octree", type=int, default=256)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--num-chunks", type=int, default=8000,
                    help="越小越省内存，越大越快；低内存机器用 2000")
    args = ap.parse_args()

    os.environ["HY3DGEN_MODELS"] = args.model_dir

    t = threading.Thread(target=_sample, daemon=True)
    t.start()

    import torch
    from PIL import Image
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    base_ram = PEAK["ram_used"]
    base_vram = PEAK["vram_used"]
    base_rss = PEAK["proc_rss"]
    print(f"基线: 系统内存 {base_ram:.1f}G / 本进程 RSS {base_rss:.1f}G / 显存 {base_vram:.1f}G", flush=True)

    t0 = time.time()
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        args.repo, subfolder=args.subfolder, device="cuda", dtype=torch.float16)
    load_s = time.time() - t0
    print(f"[加载] {load_s:.1f}s  此时显存 {PEAK['vram_used']:.1f}G 内存 {PEAK['ram_used']:.1f}G", flush=True)

    image = Image.open(args.image).convert("RGBA")
    t1 = time.time()
    meshes = pipe(image=image, num_inference_steps=args.steps, guidance_scale=6.0,
                  octree_resolution=args.octree, num_chunks=args.num_chunks,
                  output_type="trimesh")
    infer_s = time.time() - t1
    mesh = meshes[0] if isinstance(meshes, (list, tuple)) else meshes

    time.sleep(1.5)
    _STOP.set()
    t.join(timeout=2)

    print(f"[推理] {infer_s:.1f}s  faces={len(mesh.faces)} watertight={bool(mesh.is_watertight)}")
    print()
    print("=" * 56)
    print(f"峰值显存       {PEAK['vram_used']:.1f} GB / {PEAK['vram_total']:.0f} GB"
          f"   （扣掉基线约 {PEAK['vram_used']-base_vram:.1f} GB）")
    print(f"本进程 RSS     {PEAK['proc_rss']:.1f} GB  （模型自身占用约 "
          f"{PEAK['proc_rss']-base_rss:.1f} GB）")
    print(f"系统内存       {PEAK['ram_used']:.1f} GB / {PEAK['ram_total']:.0f} GB")
    print(f"峰值 GPU 利用率 {PEAK['gpu_util']:.0f}%   峰值温度 {PEAK['temp']:.0f}°C")
    print(f"耗时           加载 {load_s:.1f}s + 推理 {infer_s:.1f}s")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
