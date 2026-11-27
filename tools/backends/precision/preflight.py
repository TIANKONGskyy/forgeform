"""CPU pre-flight: load every model the pipeline needs before touching the GPU.

Checks, in order (skips what is not downloaded yet):
  D1  DINOv3 ViT-H+ loads from the local mirror dir and emits [1, tokens, 5120] features
  D2  BiRefNet loads through AutoModelForImageSegmentation(trust_remote_code=True)
  D3  the 512-stage denoiser loads from safetensors into the architecture and reports params

This is what turns "OOM at minute 20" into "fails immediately, for this reason".
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3")
WEIGHTS = ROOT / "weights"
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
os.environ["HF_HOME"] = r"D:\AI-Workshop\work\feiji_precision_vh\hf_cache_d3v3"

import torch  # noqa: E402

import flash_attn_compat  # noqa: E402

sys.modules.setdefault("flash_attn", flash_attn_compat)

report = {}


def step(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        report[name] = {"ok": True, "seconds": round(time.time() - t0, 1), "detail": detail}
        print(f"[PASS] {name} ({report[name]['seconds']}s): {detail}", flush=True)
    except Exception as exc:  # noqa: BLE001
        report[name] = {
            "ok": False,
            "seconds": round(time.time() - t0, 1),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2500:],
        }
        print(f"[FAIL] {name}: {report[name]['error']}", flush=True)
        print(report[name]["traceback"], flush=True)


def dinov3():
    from transformers import DINOv3ViTModel

    path = WEIGHTS / "dinov3-vith16plus"
    if not (path / "model.safetensors").is_file():
        raise FileNotFoundError(path)
    model = DINOv3ViTModel.from_pretrained(path, dtype=torch.float16, local_files_only=True).eval()
    n = sum(p.numel() for p in model.parameters())
    with torch.inference_mode():
        feats = model(torch.randn(1, 3, 512, 512, dtype=torch.float16)).last_hidden_state
    return f"{n/1e6:.0f}M params, hidden={feats.shape[-1]}, tokens={feats.shape[1]}"


def dinov3_extractor():
    from direct3d.models.image_encoder import DINOv3FeatureExtractor

    path = WEIGHTS / "dinov3-vith16plus"
    if not (path / "model.safetensors").is_file():
        raise FileNotFoundError(path)
    enc = DINOv3FeatureExtractor(str(path), dtype=torch.float16, local_files_only=True).eval()
    with torch.inference_mode():
        out = enc(torch.randn(1, 3, 512, 512), 512)
    del enc
    return f"4-layer concat -> {tuple(out.shape)} (expect [1, tokens, 5120])"


def birefnet():
    from transformers import AutoModelForImageSegmentation

    model = AutoModelForImageSegmentation.from_pretrained(
        "ZhengPeng7/BiRefNet", trust_remote_code=True, local_files_only=True
    ).eval()
    n = sum(p.numel() for p in model.parameters())
    return f"{n/1e6:.0f}M params"


def denoiser():
    import json

    from direct3d.models.checkpoint import load_denoiser

    cfg_path = WEIGHTS / "Direct3D-V3" / "shape_transformers_512" / "config.json"
    weights_path = WEIGHTS / "Direct3D-V3" / "shape_transformers_512" / "model.safetensors"
    if not weights_path.is_file():
        raise FileNotFoundError(weights_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    t0 = time.time()
    model = load_denoiser(str(weights_path), cfg["denoiser"], torch.float16)
    load_s = time.time() - t0
    n = sum(p.numel() for p in model.parameters())
    return (
        f"{n/1e9:.2f}B params, {n*2/2**30:.2f} GiB fp16, loaded in {load_s:.1f}s, "
        f"window={cfg['denoiser']['window_size']}, in_ch={model.in_channels}, patch={model.patch_size}"
    )


if __name__ == "__main__":
    print(f"torch {torch.__version__}", flush=True)
    step("D1 dinov3_vit_h_plus", dinov3)
    step("D2 direct3d_encoder(4-layer concat)", dinov3_extractor)
    step("D3 birefnet", birefnet)
    step("D4 denoiser_512", denoiser)
    ok = all(v["ok"] for v in report.values())
    import json

    (ROOT / "preflight_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("ALL OK" if ok else "SOME STEPS FAILED", flush=True)
    sys.exit(0 if ok else 1)
