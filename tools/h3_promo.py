# -*- coding: utf-8 -*-
"""用 MiniMax H3 生成铸想的宣传片素材。

    python tools/h3_promo.py --dry-run     # 只打印工作流，不提交
    python tools/h3_promo.py               # 提交（约 45-60 分钟出一段）

## 为什么用纯 T2V

本机 ComfyUI 的 MiniMax 云端 API key 已失效（重启即丢，且账号欠费）。
`MiniMaxH3ImageToVideo` **不传 first_frame** 就是纯文生视频，完全本地推理，
不依赖任何云端节点 —— 这是这台机器上唯一能跑通的路径。

## 配置（按 skill 里的实测结论）

- 主模型 **fl2va**（T2V/I2V 主模型，不是 ref2va）
- LoRA **1.9 GB 完整版** turbo 8step（592M 剪枝版丢参数，别用）
- 采样器 **res_multistep / simple**
- **不要 TeaCache**（会明显劣化细节）
- 6 秒 → snap 到 17 的倍数
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid

API = "http://127.0.0.1:8188"
# 本机只有 ref2va（参考图模型），没有 fl2va（T2V/I2V 主模型）。
# Ref2VA 不传 ref_images 也能出片，只是没有参考图约束。
# 要真正的 T2V 得下 21 GB 的 fl2va：tencent/Hunyuan3D-2 同仓不同文件。
UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
VAE = "minimax_h3_video_vae_fp16.safetensors"
CLIP = "qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors"
LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
FPS = 24


def snap(seconds: float) -> int:
    """帧数要 snap 到 17 的倍数（H3 的潜空间对齐要求）。"""
    frames = int(seconds * FPS)
    return frames + (17 - frames % 17) % 17


# 铸想宣传：一个白底展台，3D 扫描/线框逐段变成实体模型。
# 刻意不提"studio backdrop / platform / cinematic camera"——skill 里实测这些词
# 会让 H3 把背景补成暗色影棚，或者在末段自己推镜头。
PROMPT = (
    "A clean product shot on a pure white background, camera COMPLETELY LOCKED OFF "
    "and STATIC: no camera movement, no zoom, no push-in, no pan, no orbit, no shake. "
    "In the center of the frame floats a small abstract 3D object shaped like a simple "
    "rounded figurine, rendered as a matte light-grey surface. "
    "The object starts as a faint blue wireframe of triangulated polygons, then the "
    "triangles progressively fill in from bottom to top with solid matte grey material, "
    "until the whole object becomes a clean solid sculpture. "
    "The transformation is smooth and continuous across the whole shot. "
    "Soft even shadowless lighting, no harsh shadows. "
    "BACKGROUND STABILITY - CRITICAL: the background is PURE WHITE and BRIGHT for the "
    "ENTIRE video and never changes. It NEVER darkens, NEVER turns grey, NEVER turns "
    "black. There is NO black backdrop, NO dark studio, NO shadow wall, NO fog, NO "
    "colour shift, NO lighting change, NO exposure change. "
    "The white space around the object stays white from the first frame to the last frame. "
    "NO new objects appearing, NO props, NO text, NO logos, NO hands, NO people."
)


def workflow(prompt: str, seconds: float = 6, seed: int = 20261113) -> dict:
    length = snap(seconds)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "minimax"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": ["1", 0], "lora_name": LORA, "strength_model": 1.0}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo",
              "inputs": {"clip": ["2", 0], "prompt": prompt,
                         "width": 1344, "height": 768, "length": length,
                         "ref_image_size": "match"}},
        "6": {"class_type": "BasicGuider",
              "inputs": {"model": ["4", 0], "conditioning": ["5", 0]}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler",
              "inputs": {"model": ["4", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0}},
        "10": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["7", 0], "guider": ["6", 0], "sampler": ["8", 0],
                          "sigmas": ["9", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode",
               "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VHS_VideoCombine",
               "inputs": {"images": ["11", 0], "filename_prefix": "forgeform_promo",
                          "frame_rate": 24, "loop_count": 0, "format": "video/h264-mp4",
                          "pingpong": False, "save_output": True}},
    }


def submit(wf: dict) -> dict:
    payload = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(f"{API}/prompt", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seconds", type=float, default=6)
    ap.add_argument("--seed", type=int, default=20261113)
    args = ap.parse_args()

    wf = workflow(PROMPT, args.seconds, args.seed)
    frames = wf["5"]["inputs"]["length"]
    print("=" * 66)
    print("铸想宣传片 · MiniMax H3（纯 T2V）")
    print("=" * 66)
    print(f"  分辨率 1344x768   帧数 {frames}（{frames / FPS:.1f} 秒）")
    print(f"  模型   {UNET}")
    print(f"  LoRA   {LORA}")
    print(f"  采样   res_multistep / simple / 8 步（无 TeaCache）")
    print(f"\n  prompt: {PROMPT[:120]}…")

    if args.dry_run:
        print("\n  --dry-run：不提交")
        return 0

    t0 = time.time()
    try:
        r = submit(wf)
        print(f"\n  ✓ 已提交 {r.get('prompt_id')}  ({time.time() - t0:.1f}s)")
        print("     预计 45-60 分钟。进度看 ComfyUI stderr 的 s/it，")
        print("     产物在 D:\\ComfyUI-New\\output\\forgeform_promo_*.mp4")
        print("     ⚠️ 中途会出现 48 字节的 mp4，涨过 500KB 才算完成")
        return 0
    except Exception as exc:
        print(f"\n  ✗ 提交失败: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
