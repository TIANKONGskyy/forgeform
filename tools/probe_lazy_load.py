# -*- coding: utf-8 -*-
"""排查"优先往显存载入"为什么会崩（0xC0000005）。

分四种组合跑，每次都是独立进程（由外部脚本调起），
这样才能分清是"显存限制"、"内存限制"、还是"两者叠加"导致的。

    python tools/probe_lazy_load.py --vram 0 --ram 0      # 都不限
    python tools/probe_lazy_load.py --vram 6 --ram 0      # 只限显存
    python tools/probe_lazy_load.py --vram 0 --ram 10     # 只限内存
    python tools/probe_lazy_load.py --vram 6 --ram 10     # 都限

--vram 0 / --ram 0 表示不设限制。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vram", type=float, default=0.0, help="0 = 不限制")
    ap.add_argument("--ram", type=float, default=0.0, help="0 = 不限制")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu", "cpu_then_cuda"])
    ap.add_argument("--layers", type=int, default=0, help="只加载前 N 层（0 = 全部）")
    ap.add_argument("--keep", action="store_true", help="不释放张量，全部保留")
    args = ap.parse_args()

    print(f"[probe] vram={args.vram or '不限'} ram={args.ram or '不限'} device={args.device}")

    import torch

    if args.vram > 0:
        from backend.backends.quant import probe_vram

        total = probe_vram()[0] or 32.0
        torch.cuda.set_per_process_memory_fraction(args.vram / total, 0)
        print(f"[probe] 显存限制 {args.vram}/{total} GB")

    if args.ram > 0:
        from tools.bench_lowend import limit_ram

        print(f"[probe] 内存限制 {args.ram} GB -> {limit_ram(args.ram)}")

    import psutil

    me = psutil.Process(os.getpid())
    rss = lambda: me.memory_info().rss / 1024 ** 3  # noqa: E731

    path = r"D:\models\hy3dgen\hunyuan3d-2mini\hunyuan3d-dit-v2-mini\model.fp16.safetensors"
    if not os.path.exists(path):
        print("[probe] ✗ 权重不存在")
        return 2

    from safetensors import safe_open

    print(f"[probe] 基线 RSS {rss():.2f} GB  显存 "
          f"{torch.cuda.memory_allocated() / 1024 ** 3:.2f} GB")

    t0 = time.time()
    n_loaded = 0
    total_mb = 0.0
    kept: list = []
    try:
        f = safe_open(path, framework="pt", device="cpu")
        keys = list(f.keys())
        print(f"[probe] 权重共 {len(keys)} 个张量")
        if args.layers > 0:
            keys = keys[: args.layers]

        for i, k in enumerate(keys):
            t = f.get_tensor(k)
            if args.device == "cuda":
                t = t.to("cuda", non_blocking=True)
                t = t.to(torch.float16)
            elif args.device == "cpu_then_cuda":
                t = t.to(torch.float16)
                t = t.to("cuda", non_blocking=True)
            else:
                t = t.to(torch.float16)
            total_mb += t.numel() * t.element_size() / 1024 ** 2
            n_loaded += 1
            if (i + 1) % 50 == 0 or i + 1 == len(keys):
                print(f"  {i + 1}/{len(keys)}  {total_mb:.0f} MB  "
                      f"RSS {rss():.2f} GB  "
                      f"显存 {torch.cuda.memory_allocated() / 1024 ** 3:.2f} GB", flush=True)
            if args.keep:
                kept.append(t)
            else:
                del t

        print(f"\n[probe] ✓ 跑完 {n_loaded} 层 / {total_mb:.0f} MB / "
              f"{time.time() - t0:.1f}s")
        print(f"[probe] 显存峰值 {torch.cuda.max_memory_allocated() / 1024 ** 3:.2f} GB  "
              f"内存峰值 {rss():.2f} GB")
        return 0
    except RuntimeError as exc:
        # 预期的 OOM 会走这里（而不是段错误）
        print(f"\n[probe] ✗ RuntimeError（优雅失败）{time.time() - t0:.1f}s: {str(exc)[:200]}")
        print(f"[probe] 已加载 {n_loaded} 层 / {total_mb:.0f} MB")
        return 1


if __name__ == "__main__":
    sys.exit(main())
