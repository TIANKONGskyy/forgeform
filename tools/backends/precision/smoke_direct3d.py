"""Minimal Direct3D-V3 inference smoke test on the V100 with flash_attn replaced.

What it proves
--------------
1. `import flash_attn` inside the untouched repo works (module aliased to flash_attn_compat).
2. The real 512-stage denoiser (2.5B params, fp16), DINOv3 ViT-H+ encoder and BiRefNet matte
   all load and run through the patch-free pipeline code path.
3. It produces a mesh, and we record per-step wall time / peak VRAM / triangles / watertightness.

Memory fallback ladder (automatic, no hand-tuned retries):
    A  512 grid, 4 steps, max_tokens 150k    <- the real defaults minus step count
    B  512 grid, 2 steps, max_tokens 60k     <- official lever from the README
    C  512 grid, 1 step,  max_tokens 30k
A 256-voxel grid is NOT a supported stage: the pipeline asserts `resolution in (512, 1024, 1536)`
and each stage config carries its own resolution, so "512 -> 256" cannot be expressed without
patching the model.  max_tokens is the documented VRAM lever and is what we fall back on.

Thermal guard: this V100 has a WHEA-18 history and runs hot when other agents share the box.
The sampling callback checks the GPU temperature between steps and pauses (sleeps) above 84 C.

Usage:
    D:\\miniconda3\\python.exe smoke_direct3d.py [--steps N] [--max-tokens N] [--no-fallback]
"""

from __future__ import annotations

import argparse
import os
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3")
WEIGHTS = Path(os.environ.get("FORGEFORM_D3V3_WEIGHTS")
               or (Path(__file__).resolve().parents[3] / "work" / "research" / "precision" / "weights" / "Direct3D-V3"))

# 权重缺失时给明确提示，不要退化成"把本地路径当 HF repo id"的费解报错
if not WEIGHTS.exists():
    print(f"[refine] 缺权重：{WEIGHTS}")
    print("[refine] 跑 python tools/bootstrap_models.py --only refine 自动下载（约 4.6 GB）", flush=True)
    raise SystemExit(3)
# 2026-11-28：脚本收进 tools/backends 后，权重仍放 work/research/precision/weights；
# 可用环境变量 FORGEFORM_D3V3_WEIGHTS 覆盖。
OUT = ROOT / "output"
for _d in (
    r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin",
    r"D:\miniconda3\Lib\site-packages\torch\lib",
    r"D:\miniconda3\Library\bin",
):
    if os.path.isdir(_d):
        os.add_dll_directory(_d)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO / "third_party" / "voxelization"))
sys.path.insert(0, str(REPO / "third_party" / "fill_holes"))
sys.path.insert(0, str(REPO))

os.environ.setdefault("HF_HOME", r"D:\AI-Workshop\work\feiji_precision_vh\hf_cache_d3v3")
os.environ["HF_HUB_OFFLINE"] = "1"

import torch  # noqa: E402

import flash_attn_compat  # noqa: E402

sys.modules.setdefault("flash_attn", flash_attn_compat)
assert sys.modules["flash_attn"] is flash_attn_compat

from direct3d import Direct3DV3Pipeline  # noqa: E402

ATTEMPTS = [
    {"tag": "A", "resolution": 512, "steps": 4, "max_tokens": 150_000},
    {"tag": "B", "resolution": 512, "steps": 2, "max_tokens": 60_000},
    {"tag": "C", "resolution": 512, "steps": 1, "max_tokens": 30_000},
]
TEMP_LIMIT_C = 84


class ResourceSampler(threading.Thread):
    """nvidia-smi poller at ~0.5 s: peak temperature and peak device memory for the whole run."""

    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            s = gpu_temp()
            if s["temp_c"] is not None:
                self.samples.append(s)
            self._stop_evt.wait(self.interval)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=5)
        if not self.samples:
            return {"peak_temp_c": None, "peak_vram_mib": None, "samples": 0}
        return {
            "peak_temp_c": max(s["temp_c"] for s in self.samples),
            "peak_vram_mib": max(s["vram_mib"] for s in self.samples),
            "min_temp_c": min(s["temp_c"] for s in self.samples),
            "samples": len(self.samples),
        }


def gpu_temp():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        parts = [p.strip() for p in out.stdout.strip().split(",")]
        return {"temp_c": int(parts[0]), "vram_mib": int(parts[1]), "util": int(parts[2])}
    except Exception:  # noqa: BLE001
        return {"temp_c": None, "vram_mib": None, "util": None}


def make_pipeline():
    """Only the 512 stage: loading all three 4.65 GiB denoisers would be 14 GiB of VRAM."""
    pipe = Direct3DV3Pipeline(
        WEIGHTS,
        image_encoder=None,  # model_index.json points image_encoder at the local DINOv3 dir
        torch_dtype=torch.float16,
        revision=None,
        local_files_only=True,
        stages=(512,),
    )
    pipe.to("cuda")
    return pipe


def run_attempt(pipe, attempt, args):
    steps_seen = []
    step_times = []
    thermal = []
    last = {"t": None}

    def on_step(stage, done, total):
        now = time.time()
        if last["t"] is not None and done > 0:
            step_times.append(round(now - last["t"], 2))
        last["t"] = now
        steps_seen.append((stage, done, total))
        if done < total:  # only between steps; a pause cannot interrupt a running kernel
            t = gpu_temp()
            if t["temp_c"] and t["temp_c"] >= TEMP_LIMIT_C:
                thermal.append(t)
                print(f"  [thermal] {t['temp_c']}C >= {TEMP_LIMIT_C}C, pausing 30s", flush=True)
                time.sleep(30)

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    sampler = ResourceSampler()
    sampler.start()
    t0 = time.time()
    try:
        mesh = pipe(
            args.image,
            args.coarse_mesh,
            resolution=attempt["resolution"],
            num_inference_steps={attempt["resolution"]: attempt["steps"]},
            guidance_scale={attempt["resolution"]: 4.0},
            seed=args.seed,
            max_tokens=attempt["max_tokens"],
            target_faces=args.target_faces or None,
            on_sampling_step=on_step,
        )
    finally:
        wall = time.time() - t0
        gpu_samples = sampler.stop()
    return mesh, {
        "wall_seconds": round(wall, 1),
        "step_seconds": step_times,
        "per_step_seconds": round(sum(step_times) / len(step_times), 2) if step_times else None,
        "peak_alloc_mib": round(torch.cuda.max_memory_allocated() / 2**20),
        "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20),
        "peak_temp_c": gpu_samples["peak_temp_c"],
        "peak_device_vram_mib": gpu_samples["peak_vram_mib"],
        "gpu_samples": gpu_samples["samples"],
        "thermal_pauses": thermal,
        "triangles": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "bbox_mm": [round(float(v) * 1000, 1) for v in mesh.extents],
        "watertight": bool(mesh.is_watertight),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=None, help="override attempt A step count")
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--image", default=str(REPO / "assets" / "input_imgs" / "6.png"))
    parser.add_argument("--coarse-mesh", default=str(REPO / "assets" / "coarse_meshes" / "6.obj"))
    parser.add_argument("--target-faces", type=int, default=0, help="0 = keep every triangle")
    parser.add_argument("--no-fallback", action="store_true")
    args = parser.parse_args()

    attempts = [dict(a) for a in ATTEMPTS]
    if args.steps is not None:
        attempts[0]["steps"] = args.steps
    if args.max_tokens is not None:
        attempts[0]["max_tokens"] = args.max_tokens
    limit = 1 if args.no_fallback else len(attempts)

    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "torch": torch.__version__,
        "gpu": torch.cuda.get_device_name(0),
        "gpu_capability": list(torch.cuda.get_device_capability(0)),
        "sdpa_backend": "EFFICIENT_ATTENTION (flash/cuDNN kernels do not exist for sm_70)",
        "image": args.image,
        "coarse_mesh": args.coarse_mesh,
        "attempts": [],
    }
    print(json.dumps({k: report[k] for k in ("gpu", "gpu_capability")}), flush=True)
    print("[gpu before]", gpu_temp(), flush=True)

    load_start = time.time()
    try:
        pipe = make_pipeline()
    except Exception as exc:  # noqa: BLE001
        report["load_error"] = f"{type(exc).__name__}: {exc}"
        report["load_traceback"] = traceback.format_exc()[-4000:]
        print("[FAIL load]", report["load_error"], flush=True)
        (OUT / "smoke_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1
    report["load_seconds"] = round(time.time() - load_start, 1)
    report["vram_after_load_mib"] = round(torch.cuda.memory_allocated() / 2**20)
    print(f"[load] {report['load_seconds']}s, {report['vram_after_load_mib']} MiB allocated", flush=True)

    ok = False
    for attempt in attempts[:limit]:
        print(f"[attempt {attempt['tag']}] {attempt}", flush=True)
        try:
            mesh, stats = run_attempt(pipe, attempt, args)
            dest = OUT / f"smoke_r{attempt['resolution']}_s{attempt['steps']}_mt{attempt['max_tokens']}.glb"
            mesh.export(dest)
            stats = {
                **stats,
                **attempt,
                "output": str(dest),
                "output_mib": round(dest.stat().st_size / 2**20, 2),
                "ok": True,
            }
            report["attempts"].append(stats)
            print(
                f"[ok {attempt['tag']}] {stats['wall_seconds']}s, "
                f"{stats['per_step_seconds']}s/step, peak {stats['peak_alloc_mib']} MiB alloc / "
                f"{stats['peak_reserved_mib']} MiB reserved, {stats['triangles']:,} triangles, "
                f"watertight={stats['watertight']} -> {dest.name}",
                flush=True,
            )
            ok = True
            break
        except (torch.OutOfMemoryError, RuntimeError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            oom = isinstance(exc, torch.OutOfMemoryError) or bool(re.search(r"out of memory|CUDA error", msg, re.I))
            report["attempts"].append({"tag": attempt["tag"], **attempt, "ok": False, "error": msg, "oom": oom})
            print(f"[fail {attempt['tag']}] {msg}", flush=True)
            torch.cuda.empty_cache()
            if not oom:
                break

    report["ok"] = ok
    report["gpu_after"] = gpu_temp()
    tag = f"_{attempts[0]['steps']}steps" if args.steps is not None else ""
    for name in (OUT / f"smoke_report{tag}.json", OUT / "smoke_report.json"):
        name.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("[report]", OUT / f"smoke_report{tag}.json", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
