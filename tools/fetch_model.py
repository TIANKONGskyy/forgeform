# -*- coding: utf-8 -*-
"""模型下载器 —— 铸想自己把权重拉下来，不依赖 ComfyUI 的目录约定。

    python tools/fetch_model.py --repo Tongyi-MAI/Z-Image-Turbo \
        --dest D:/models/z-image-turbo-diffusers --exclude "assets/*"

为什么自己写而不直接用 `hf download`：

  · 这台机器上 `huggingface_hub.snapshot_download` 会卡在元数据阶段不动（实测 20 分钟 0 字节）
  · 国内单线程 0.4–0.8 MB/s，多线程分片能到 ~1.5 MB/s
  · 需要断点续传（20 GB 断一次不能从头来）

支持：HF 官方 / hf-mirror / 自建反代；多线程分片；断点续传；落盘前校验大小。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MIRRORS = {
    "hf": "https://huggingface.co",
    "mirror": "https://hf-mirror.com",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) forgeform-fetch/1.0"


# ────────────────────────────────────────────────────────────────
# 元数据
# ────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_files(repo: str, base: str) -> list[tuple[str, int]]:
    """返回 [(rfilename, size)]。

    HF 的 tree API 有的镜像不支持 expand / recursive 参数，会直接 400；
    所以逐个降级，最后回落到 `/api/models/<repo>` 的 siblings（只有名字、没有大小）。
    """
    api = f"{base}/api/models/{repo}/tree/main"
    last = None
    for qs in ("?recursive=true&expand=true", "?recursive=true", ""):
        try:
            data = json.loads(_get(f"{api}{qs}"))
            if not isinstance(data, list):
                raise ValueError("不是文件列表")
            out = []
            for item in data:
                if item.get("type") != "file":
                    continue
                size = item.get("size") or (item.get("lfs") or {}).get("size") or 0
                out.append((item["path"], int(size)))
            if out:
                return out
        except Exception as exc:
            last = exc
            continue

    # 最后一条路：siblings（只有文件名，大小靠下载时 HEAD 补）
    try:
        info = json.loads(_get(f"{base}/api/models/{repo}"))
        sib = info.get("siblings") or []
        out = [(s["rfilename"], 0) for s in sib if s.get("rfilename")]
        if out:
            print(f"  (用 siblings 列表，{len(out)} 个文件，大小未知)")
            return out
    except Exception as exc:
        last = exc

    raise RuntimeError(f"列文件失败: {last}")


def probe_mirrors(repo: str, timeout: int = 8) -> tuple[str, float]:
    """挑一个能通、响应最快的镜像。返回 (base_url, 秒)。

    探的是 `/api/models/<repo>`，不是某个具体文件 —— 仓库里不一定有 model_index.json
    （hy3dgen 那种格式就没有），拿文件探测会把好镜像误判成不可用。
    """
    best, best_t = "", 999.0
    for name, base in MIRRORS.items():
        t0 = time.time()
        try:
            _get(f"{base}/api/models/{repo}", timeout=timeout)
            dt = time.time() - t0
            print(f"  [{name}] {base}  {dt*1000:.0f} ms")
            if dt < best_t:
                best, best_t = base, dt
        except Exception as exc:
            print(f"  [{name}] {base}  不可用 ({type(exc).__name__})")
    return best, best_t


# ────────────────────────────────────────────────────────────────
# 分片下载
# ────────────────────────────────────────────────────────────────
def _content_length(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as r:
        return int(r.headers.get("Content-Length") or 0)


def _fetch_range(url: str, start: int, end: int, part: Path, stop: threading.Event) -> None:
    """下 [start, end] 到 part，支持从 part 已有长度续传。"""
    have = part.stat().st_size if part.exists() else 0
    want = end - start + 1
    if have >= want:
        return
    rng = f"bytes={start + have}-{end}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": rng})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
        while not stop.is_set():
            chunk = r.read(1 << 20)      # 1 MB
            if not chunk:
                break
            f.write(chunk)


def download_file(url: str, dest: Path, size: int, threads: int = 8,
                  retries: int = 3) -> bool:
    """多线程分片下载一个文件。断点续传靠 .part/<index> 目录里的小块。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and size and dest.stat().st_size >= size:
        print(f"    skip  {dest.name} (已完整)")
        return True

    part_dir = dest.parent / (dest.name + ".parts")
    part_dir.mkdir(exist_ok=True)

    if not size:
        size = _content_length(url)
    if size <= 0:
        raise RuntimeError(f"拿不到文件大小: {url}")

    # 小于 16 MB 就单线程，分片反而慢
    threads = 1 if size < 16 << 20 else min(threads, 16)
    chunk = size // threads + 1
    stop = threading.Event()
    procs = []

    def job(i: int):
        s = i * chunk
        e = min(s + chunk - 1, size - 1)
        if s > e:
            return
        for attempt in range(retries):
            try:
                _fetch_range(url, s, e, part_dir / f"{i:03d}", stop)
                return
            except Exception as exc:
                if attempt == retries - 1:
                    print(f"    part {i} 失败: {exc}")
                time.sleep(1.5)

    t0 = time.time()
    ts = [threading.Thread(target=job, args=(i,), daemon=True) for i in range(threads)]
    for t in ts:
        t.start()

    # 进度
    last = 0.0
    while any(t.is_alive() for t in ts):
        time.sleep(0.6)
        done = sum((p.stat().st_size for p in part_dir.iterdir()), 0)
        now = time.time()
        if now - last > 2:
            sp = done / max(now - t0, 0.1) / 1024 / 1024
            pct = done / size * 100 if size else 0
            print(f"    {pct:5.1f}%  {done/1024/1024:8.1f}/{size/1024/1024:.1f} MB  "
                  f"{sp:5.2f} MB/s  ({dest.name})", flush=True)
            last = now

    stop.set()
    for t in ts:
        t.join(timeout=5)

    done = sum((p.stat().st_size for p in part_dir.iterdir()), 0)
    if done < size * 0.999:
        print(f"    ✗ 不完整 {done}/{size} 字节，保留 .parts 下次续传")
        return False

    # 拼起来
    with open(dest, "wb") as out:
        for i in range(threads):
            p = part_dir / f"{i:03d}"
            if p.exists():
                out.write(p.read_bytes())
    for p in part_dir.iterdir():
        p.unlink()
    part_dir.rmdir()

    el = time.time() - t0
    print(f"    ✓ {dest.name}  {size/1024/1024:.1f} MB  {el:.0f}s  "
          f"({size/1024/1024/max(el,0.1):.2f} MB/s)")
    return True


# ────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────
def _match(path: str, includes: list[str], excludes: list[str]) -> bool:
    if includes and not any(re.search(p, path) for p in includes):
        return False
    if any(re.search(p, path) for p in excludes):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想模型下载器")
    ap.add_argument("--repo", required=True, help="例如 Tongyi-MAI/Z-Image-Turbo")
    ap.add_argument("--dest", required=True, help="本地目录")
    ap.add_argument("--include", action="append", default=[], help="只下匹配这个正则的文件")
    ap.add_argument("--exclude", action="append", default=[], help="排除匹配这个正则的文件")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--mirror", default="auto", choices=["auto", "hf", "mirror"],
                    help="auto=自动挑最快的镜像")
    ap.add_argument("--dry-run", action="store_true", help="只列文件，不下")
    args = ap.parse_args()

    dest = Path(args.dest)
    print(f"仓库: {args.repo}\n目标: {dest}\n")

    if args.mirror == "auto":
        print("挑镜像:")
        base, _t = probe_mirrors(args.repo)
        if not base:
            print("所有镜像都不可用")
            return 1
    else:
        base = MIRRORS[args.mirror]
    print(f"使用: {base}\n")

    try:
        files = list_files(args.repo, base)
    except Exception as exc:
        print(f"列文件失败: {exc}")
        return 1

    picked = [(p, s) for p, s in files if _match(p, args.include, args.exclude)]
    total = sum(s for _, s in picked)
    print(f"待下载 {len(picked)} 个文件，合计 {total/1024**3:.2f} GB\n")

    # 磁盘空间预检
    free = 0
    try:
        import shutil
        probe = dest if dest.exists() else dest.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(str(probe)).free
    except Exception:
        pass
    if free and total and free < total * 1.1:
        print(f"✗ 磁盘不够：需要 {total/1024**3:.1f} GB，可用 {free/1024**3:.1f} GB")
        return 1
    if free:
        print(f"磁盘可用 {free/1024**3:.1f} GB（够）\n")

    if args.dry_run:
        for p, s in picked:
            print(f"  {s/1024**2:9.1f} MB  {p}")
        return 0

    t0 = time.time()
    ok = 0
    for i, (path, size) in enumerate(picked, 1):
        url = f"{base}/{args.repo}/resolve/main/{path}"
        print(f"[{i}/{len(picked)}] {path}  ({size/1024**2:.1f} MB)")
        try:
            if download_file(url, dest / path, size, threads=args.threads):
                ok += 1
        except Exception as exc:
            print(f"    ✗ {type(exc).__name__}: {exc}")

    el = time.time() - t0
    got = sum((p.stat().st_size for p in dest.rglob("*") if p.is_file()), 0)
    print(f"\n完成 {ok}/{len(picked)} 个文件，{got/1024**3:.2f} GB，耗时 {el/60:.1f} 分钟")
    return 0 if ok == len(picked) else 1


if __name__ == "__main__":
    sys.exit(main())
