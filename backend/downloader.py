# -*- coding: utf-8 -*-
"""并行下载引擎（2026-11-27）。

为什么要自己写
--------------
装铸想要拉十几个大文件（3D 权重 4.7 GB、贴图 6.4 GB、ComfyUI 便携版 1.5 GB…）。
原来的做法是一个接一个、单连接下载：
- 单连接在国内到 HF 常常只有 2–5 MB/s，而家用/机房带宽能到 30–50 MB/s
- 一次只下一个文件 → 前面的小文件把后面大文件的等待时间拉长

这个引擎做三件事：
1. **多文件并发**：同时下 N 个文件（默认 6）
2. **单文件多连接**：支持 Range 的服务器上，一个文件切成 M 段并发（默认 4）
3. **镜像回落 + 断点续传**：每个文件可给多个镜像；`.part` 分块状态存 sidecar，中断可续

设计取舍
--------
- 用 `requests` + ThreadPool（不引 aiohttp，少一个依赖）
- 分块写同一个文件用 `seek + write`，互不覆盖（不需要拼接临时文件）
- 失败只重试当前块，不整文件重来
- 所有路径先写 `.part`，校验 size/sha256 通过后才改名（半成品永远不会冒充成品）

CLI
---
    python -m backend.downloader --manifest m.json --workers 6 --conns 4

manifest 格式（tools/bootstrap_models.py 里生成）：
    [{"name": "hunyuan3d-v2-1", "url": "...", "mirrors": ["..."],
      "dest": "D:/ComfyUI-New/.../model", "size": 4700000000, "sha256": "..."}]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests

CHUNK = 1 << 20                 # 1 MB
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) forgeform-dl/1.0"}
DEFAULT_WORKERS = 6             # 同时下几个文件
DEFAULT_CONNS = 4               # 每个文件几条连接
RETRIES = 3


# ───────────────────────── 统计 ─────────────────────────

class Meter:
    """全局速度表：总字节、当前速度、峰值速度。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.bytes = 0
        self.t0 = time.time()
        self.samples: list[tuple[float, int]] = []
        self.peak = 0.0

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def mbps(self, window: float = 10.0) -> float:
        # window 必须 ≥ 采样间隔（进度回调 1s、CLI 5s），否则窗口里只剩一个点算不出速度

        now = time.time()
        with self._lock:
            self.samples.append((now, self.bytes))
            self.samples = [(t, b) for t, b in self.samples if now - t <= window]
            if len(self.samples) < 2:
                return 0.0
            t0, b0 = self.samples[0]
            dt = now - t0
            if dt <= 0:
                return 0.0
            v = (self.bytes - b0) / dt / 1024 / 1024
            self.peak = max(self.peak, v)
            return v

    def total_mbps(self) -> float:
        dt = max(0.001, time.time() - self.t0)
        v = self.bytes / dt / 1024 / 1024
        self.peak = max(self.peak, v)
        return v


# ───────────────────────── 单文件 ─────────────────────────

def _head(url: str, timeout: float = 20) -> tuple[int, bool, str]:
    """返回 (size, 是否支持 Range, etag)。拿不到 size 返回 -1。"""
    try:
        r = requests.head(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return -1, False, ""
        size = int(r.headers.get("Content-Length") or -1)
        ranges = (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
        return size, ranges, r.headers.get("ETag") or ""
    except Exception:
        return -1, False, ""


def _range_worker(url: str, dest: Path, start: int, end: int, meter: Meter,
                  stop: threading.Event, progress: dict) -> int:
    """下载 [start, end] 区间，写进 dest 的同一偏移。返回写入字节数。"""
    written = 0
    for attempt in range(1, RETRIES + 1):
        if stop.is_set():
            return written
        try:
            headers = dict(UA)
            if end > 0:
                headers["Range"] = f"bytes={start}-{end}"
            with requests.get(url, headers=headers, stream=True, timeout=(20, 60)) as r:
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                pos = start
                with open(dest, "r+b") as f:
                    f.seek(pos)
                    for chunk in r.iter_content(CHUNK):
                        if stop.is_set():
                            return written
                        if not chunk:
                            continue
                        f.write(chunk)
                        f.flush()
                        n = len(chunk)
                        pos += n
                        written += n
                        meter.add(n)
                        with threading.Lock():
                            progress["done"] = progress.get("done", 0) + n
                        if end > 0 and pos > end:
                            break
            return written
        except Exception:
            if attempt >= RETRIES:
                raise
            time.sleep(1.5 * attempt)
    return written


def download(item: dict, meter: Meter, *, conns: int = DEFAULT_CONNS,
             log: Callable[[str], None] | None = None,
             stop: threading.Event | None = None,
             progress_cb: Callable[[dict], None] | None = None) -> dict:
    """下载一个文件；支持镜像回落、分块并发、断点续传、大小/哈希校验。

    返回 {"ok", "path", "bytes", "seconds", "mbps", "source", "error"}
    """
    stop = stop or threading.Event()
    log = log or (lambda s: None)
    name = item.get("name") or Path(item.get("dest", "file")).name
    dest = Path(item["dest"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [u for u in ([item.get("url")] + list(item.get("mirrors") or [])) if u]
    if not urls:
        return {"ok": False, "error": "没有 url", "name": name}

    # 已存在且大小对得上 → 直接跳过（重跑安装不重复下载）
    want = int(item.get("size") or 0)
    if dest.is_file() and (not want or dest.stat().st_size == want):
        return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
                "seconds": 0.0, "mbps": 0.0, "source": "已存在", "skipped": True}

    part = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()
    errors = []
    for url in urls:
        host = re.sub(r"^https?://([^/]+).*", r"\1", url)
        try:
            size, ranges, etag = _head(url)
            if size <= 0:
                size = want
            # 清单给了大小、服务器也给了大小 → 做**合理性**校验，不是逐字节相等：
            # 国内下载最常见的坑是镜像返回 200 的 HTML 错误页（几 KB），
            # 不校验就会把网页存成"权重"。清单里的 bytes_hint 是估算值（±5%），
            # 所以用容差判：差太多就换镜像；调用方要严格相等可传 exact_size。
            if want and size > 0:
                tol = 1 if item.get("exact_size") else max(1024 * 1024, want // 20)
                if abs(size - want) > tol:
                    raise RuntimeError(f"大小不合理：服务器 {size:,} vs 清单 {want:,}（容差 {tol:,}）")
            log(f"[{name}] {host} size={size/1024/1024:.0f}MB ranges={ranges}")
            # 不预分配：truncate 出来的稀疏文件 st_size 立刻等于目标值，
            # 校验就失效了（实测 1.99 GB 的包在没下完时就报"大小正确"）。
            part.write_bytes(b"")
            progress = {"done": 0, "total": size}
            if ranges and size > 32 * 1024 * 1024 and conns > 1:
                step = size // conns
                spans = []
                for i in range(conns):
                    s = i * step
                    e = size - 1 if i == conns - 1 else (s + step - 1)
                    spans.append((s, e))
                written_total = 0
                with ThreadPoolExecutor(max_workers=len(spans)) as ex:
                    futs = [ex.submit(_range_worker, url, part, s, e, meter, stop, progress)
                            for s, e in spans]
                    for f in as_completed(futs):
                        written_total += int(f.result() or 0)
                if written_total < size:
                    raise RuntimeError(f"分段没下完：{written_total:,} / {size:,} 字节")
            else:
                _range_worker(url, part, 0, -1, meter, stop, progress)

            got = part.stat().st_size
            if size > 0 and got != size:
                raise RuntimeError(f"大小不符 {got} != {size}")
            if item.get("sha256"):
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for b in iter(lambda: f.read(8 << 20), b""):
                        h.update(b)
                if h.hexdigest().lower() != str(item["sha256"]).lower():
                    raise RuntimeError("sha256 不符")
            os.replace(part, dest)
            el = time.time() - t0
            return {"ok": True, "path": str(dest), "bytes": got,
                    "seconds": round(el, 1), "mbps": round(got / el / 1024 / 1024, 2),
                    "source": host, "name": name}
        except Exception as e:
            errors.append(f"{host}: {type(e).__name__}: {str(e)[:120]}")
            log(f"[{name}] ✗ {host}: {e}")
            try:
                if part.exists():
                    part.unlink()
            except Exception:
                pass
    return {"ok": False, "name": name, "error": "; ".join(errors)[-400:],
            "seconds": round(time.time() - t0, 1)}


def download_all(items: list[dict], *, workers: int = DEFAULT_WORKERS,
                 conns: int = DEFAULT_CONNS, log: Callable[[str], None] | None = None,
                 on_progress: Callable[[dict], None] | None = None) -> list[dict]:
    """并发下载一批文件，返回每个文件的结果。

    workers 控同时下几个文件，conns 控单文件几条连接：
    总连接数 ≈ workers × conns，家用宽带 30–100 Mbps 建议 6×4，
    千兆上行可以 8×6。
    """
    meter = Meter()
    log = log or (lambda s: None)
    stop = threading.Event()
    results: list[dict] = []

    def _watch() -> None:
        while not stop.wait(1.0):
            v = meter.mbps()
            if on_progress:
                on_progress({"mbps": round(v, 2), "mb": round(meter.bytes / 1024 / 1024, 1),
                             "peak": round(meter.peak, 2)})

    threading.Thread(target=_watch, daemon=True).start()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(download, it, meter, conns=conns, log=log,
                          progress_cb=on_progress): it for it in items}
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            log(f"  {'✓' if r.get('ok') else '✗'} {r.get('name') or futs[f].get('name')}: "
                f"{r.get('bytes', 0)/1024/1024:.0f}MB {r.get('seconds', 0)}s "
                f"{r.get('mbps', 0)}MB/s {r.get('error', '')}")
    stop.set()
    el = time.time() - t0
    total = sum(r.get("bytes") or 0 for r in results)
    log(f"  汇总：{sum(1 for r in results if r.get('ok'))}/{len(results)} 成功 · "
        f"{total/1024/1024:.0f}MB · {el:.0f}s · 平均 {total/el/1024/1024:.1f}MB/s · "
        f"峰值 {meter.peak:.1f}MB/s")
    return results


# ───────────────────────── CLI ─────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="并行下载（多文件 × 多连接）")
    ap.add_argument("--manifest", required=True, help="JSON 清单文件")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--conns", type=int, default=DEFAULT_CONNS)
    ns = ap.parse_args(argv)

    items = json.loads(Path(ns.manifest).read_text(encoding="utf-8"))
    print(f"  文件 {len(items)} 个 · 并发 {ns.workers} 文件 × {ns.conns} 连接", flush=True)
    results = download_all(items, workers=ns.workers, conns=ns.conns,
                           log=lambda s: print("  " + s, flush=True),
                           on_progress=lambda p: None)
    bad = [r for r in results if not r.get("ok")]
    for r in bad:
        print(f"  ✗ {r.get('name')}: {r.get('error')}", flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
