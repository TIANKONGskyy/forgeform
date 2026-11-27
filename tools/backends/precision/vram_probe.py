"""VRAM / time model for the 512 denoiser as a function of sparse token count.

The 512 smoke run only occupied 11,758 patches, so its 5 GiB peak is not the interesting
number for planning 1024/1536 (more surface patches -> more tokens).  This probe runs one
denoiser forward at a controlled token count and reports wall time + peak allocation, which is
what the 1024/1536 stages would need scaled by their token counts.

Shapes match the real call path: x [N, patch^3 * in_ch] fp16, index [N, 4] int32 (one voxel
thick shell of a 64^3 / 128^3 grid), layout [slice(0, N)], cond [1, 1029, 5120].
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3")
WEIGHTS = ROOT / "weights" / "Direct3D-V3"
for _d in (
    r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin",
    r"D:\miniconda3\Lib\site-packages\torch\lib",
    r"D:\miniconda3\Library\bin",
):
    if os.path.isdir(_d):
        os.add_dll_directory(_d)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))
os.environ["HF_HUB_OFFLINE"] = "1"

import torch  # noqa: E402

import flash_attn_compat  # noqa: E402

sys.modules.setdefault("flash_attn", flash_attn_compat)

from direct3d.models.checkpoint import load_denoiser  # noqa: E402

GRID = 64  # 512 / patch_size 8


def shell_coords(n, grid, device):
    """Roughly surface-like: random positions on the boundary shell of the patch grid."""
    torch.manual_seed(0)
    idx = torch.zeros(n, 4, dtype=torch.int32, device=device)
    idx[:, 0] = 0
    idx[:, 1] = torch.randint(0, grid, (n,), device=device)
    idx[:, 2] = torch.randint(0, grid, (n,), device=device)
    idx[:, 3] = torch.randint(0, grid, (n,), device=device)
    return torch.unique(idx, dim=0)


def main():
    cfg = json.loads((WEIGHTS / "shape_transformers_512" / "config.json").read_text(encoding="utf-8"))
    model = load_denoiser(
        str(WEIGHTS / "shape_transformers_512" / "model.safetensors"), cfg["denoiser"], torch.float16
    ).to("cuda")
    patch = cfg["denoiser"]["patch_size"]
    cond = torch.randn(1, 1029, cfg["denoiser"]["context_dim"], device="cuda", dtype=torch.float16)
    rows = []
    for n in (12000, 50000, 100000, 150000):
        idx = shell_coords(n, GRID, "cuda")
        n = idx.shape[0]
        x = torch.randn(n, patch**3, device="cuda", dtype=torch.float16)
        layout = [slice(0, n)]
        t = torch.tensor([0.5], device="cuda")
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            model(x, idx, layout, t, cond)  # warmup (kernel autotune)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            t0 = time.time()
            model(x, idx, layout, t, cond)
            torch.cuda.synchronize()
        dt = time.time() - t0
        row = {
            "tokens": int(n),
            "forward_seconds": round(dt, 2),
            "step_seconds_cfg2": round(dt * 2, 2),
            "peak_alloc_mib": round(torch.cuda.max_memory_allocated() / 2**20),
            "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
        del x, idx
        torch.cuda.empty_cache()
    (ROOT / "vram_probe.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
