# -*- coding: utf-8 -*-
"""跑一组生成任务，同时采样显存峰值（2026-11-28 补齐报告里的"显存"数据）。

做法：后台线程每 1.5 秒读一次 nvidia-smi，记录峰值与基线；
任务结束打印 峰值/基线/增量，方便写进实测报告。

用法：
    python tools/measure_vram.py image z_image
    python tools/measure_vram.py 3d 2mini
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _gpu_mib() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=5).strip().splitlines()
        return int(out[0].strip())
    except Exception:
        return -1


class Sampler:
    def __init__(self, interval: float = 1.5):
        self.interval = interval
        self.base = _gpu_mib()
        self.peak = self.base
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _loop(self):
        while not self._stop.is_set():
            v = _gpu_mib()
            if v > self.peak:
                self.peak = v
            self._stop.wait(self.interval)

    def __enter__(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)

    def report(self, label: str, seconds: float) -> str:
        delta = max(0, self.peak - self.base)
        return (f"  {label}: {seconds:.0f}s · 基线 {self.base} MiB · "
                f"峰值 {self.peak} MiB · 增量 {delta} MiB ({delta/1024:.1f} GB)")


def run_image(model_key: str) -> int:
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))
    with Sampler() as s:
        t0 = time.time()
        res = pipe.generate_images(
            "a white ceramic mug, single object, pure white background",
            count=1, model_key=model_key, quality_name="快速",
            resolution="1024×1024", seed=7)
        secs = time.time() - t0
    r = res[0] if res else None
    print(s.report(f"出图 {model_key}", secs))
    print(f"    success={getattr(r, 'success', None)} out={getattr(r, 'output_path', '')}")
    return 0 if getattr(r, "success", False) else 1


def run_3d(model_key: str, quality: str = "快速") -> int:
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))
    img = ROOT / "outputs" / "images"
    cands = sorted(img.glob("*.png")) if img.is_dir() else []
    if not cands:
        print("  没有参考图，先用 measure_vram.py image z_image 生成一张")
        return 2
    with Sampler() as s:
        t0 = time.time()
        r = pipe.generate_3d(model_key, quality, str(cands[-1]), max_facenum=120000, seed=9)
        secs = time.time() - t0
    print(s.report(f"建模 {model_key}/{quality}", secs))
    print(f"    success={getattr(r, 'success', None)} out={getattr(r, 'output_path', '')}")
    return 0 if getattr(r, "success", False) else 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    kind, key = sys.argv[1], sys.argv[2]
    if kind == "image":
        return run_image(key)
    if kind == "3d":
        return run_3d(key, sys.argv[3] if len(sys.argv) > 3 else "快速")
    print("kind 只能是 image / 3d")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
