# -*- coding: utf-8 -*-
"""DirectBackend 端到端冒烟：真模型 → diffusers 推理 → 本地 HTTP 交付 → requests 拿回。

用法：
    python tools/bench_direct.py --model tiny_sd --path D:/models/tiny-sd \
        --pipeline StableDiffusionPipeline --steps 4 --size 512

这是"不用 ComfyUI 也能出图"的权威验证。跑通说明整条链路成立：
    插件/配置 → DirectBackend → diffusers → 产物落盘 → /view 伪协议 → pipeline 能下载
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="tiny_sd", help="模型 key（随便起，只用于日志）")
    ap.add_argument("--path", required=True, help="本地 diffusers 目录")
    ap.add_argument("--pipeline", default="StableDiffusionPipeline")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--guidance", type=float, default=0.0)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--prompt", default="a red apple on a white background, single object")
    ap.add_argument("--offload", default="", choices=["", "model", "sequential"],
                    help="显存不够时用 CPU offload：model=按子模块换入换出")
    ap.add_argument("--quant", default="auto", choices=["auto", "none", "fp8", "bnb8", "bnb4", "gguf"],
                    help="量化档；auto=按显存自动选")
    ap.add_argument("--keep", action="store_true", help="保留产物目录")
    args = ap.parse_args()

    out = Path(tempfile.mkdtemp(prefix="direct-bench-")) if not args.keep else Path("direct-bench")
    print(f"产物目录: {out}")

    from backend.backends.direct import DirectBackend

    cfg = {
        "backend": {"mode": "direct"},
        "paths": {"output": str(out)},
        "stages": {"image": {"default_model": args.model}},
        "models": {"image": {args.model: {
            "path": args.path,
            "pipeline": args.pipeline,
            "steps": args.steps,
            "guidance": args.guidance,
            "width": args.size,
            "height": args.size,
            "offload": args.offload,
            "quant": args.quant,
        }}},
    }

    t0 = time.time()
    b = DirectBackend(cfg)
    try:
        print("\n[1] build_workflow")
        wf = b.build_workflow("image", {"prompt": args.prompt, "steps": args.steps})
        print("   ", {k: v for k, v in wf.items() if k != "params"})

        print("\n[2] submit_workflow（同步跑推理）")
        t = time.time()
        pid = b.submit_workflow(wf, stage="image")
        infer_s = time.time() - t
        print(f"    job={pid[:8]}  推理 {infer_s:.1f}s")

        print("\n[3] wait_for_completion")
        res = b.wait_for_completion(pid, timeout=600)
        files = (res.get("outputs") or {}).get("__files__") or []
        print("   ", files)

        print("\n[4] get_result_image → 伪 /view URL")
        url = b.get_result_image(res.get("outputs", {}), stage="image")
        print("   ", url)

        print("\n[5] 用 requests 把它拿回来（和 pipeline 里一模一样）")
        import requests
        r = requests.get(url, timeout=30)
        print(f"    HTTP {r.status_code}  {len(r.content)} 字节")
        assert r.status_code == 200 and len(r.content) > 1000

        if files:
            print(f"\n产物: {files[0]}  ({Path(files[0]).stat().st_size//1024} KB)")
        print(f"\n✅ 端到端通过，总耗时 {time.time()-t0:.1f}s（推理 {infer_s:.1f}s）")
        return 0
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
