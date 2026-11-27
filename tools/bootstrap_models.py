# -*- coding: utf-8 -*-
"""一键装齐铸想需要的东西（并行）——2026-11-27。

和 `install.ps1` 的分工
-----------------------
`install.ps1` 管"拿到代码 + pip 依赖"；这个脚本管**模型与 ComfyUI**，
并且尽可能并行：下载池、git clone、pip 装 ComfyUI 依赖、写配置同时推进。

任务图（→ 表示依赖）
--------------------
    [目录] ─┐
    [ComfyUI 便携版] ─┬→ [pip: ComfyUI 依赖] ─┐
    [3D-Pack 仓库] ───┘                       ├→ [写 config] → [体检]
    [3D 权重 × N] ───────────────────────────┘
    [贴图权重] ─┘

用法
----
    python tools/bootstrap_models.py                     # 全套（约 60 GB）
    python tools/bootstrap_models.py --only core         # 只装能跑单图建模的最小集
    python tools/bootstrap_models.py --only weights      # 只补权重
    python tools/bootstrap_models.py --dry-run           # 只打印计划
    python tools/bootstrap_models.py --workers 8 --conns 6

`core` = ComfyUI + 3D-Pack + Hunyuan3D 2.1（约 12 GB），装完就能"一句话→STL"。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMFY = Path(os.environ.get("FORGEFORM_COMFY_DIR", r"D:\ComfyUI-New"))
COMFY_ENV = Path(os.environ.get("FORGEFORM_COMFY_ENV", r"D:\comfy312"))
CKPT = COMFY / "custom_nodes" / "ComfyUI-3D-Pack-main" / "Checkpoints" / "Diffusers" / "tencent" / "Hunyuan3D-2_1"

# HF 直连在国内基本不通；镜像优先
HF = "https://hf-mirror.com"
MS = "https://modelscope.cn/models"

SUITES = {
    "core": "ComfyUI + 3D-Pack + Hunyuan3D 2.1（能单图建模）",
    "paint": "贴图权重（PBR 2.1）",
    "mv": "多视图权重（Hunyuan3D 2mv）",
    "refine": "精修权重（Direct3D-V3 512 + DINOv3）",
    "rig": "RigAnything 语义骨骼",
    "all": "全部",
}


def _comfy_portable() -> dict:
    """ComfyUI 便携版：直接用官方 release 的 zip（不用 7z，Windows 自带解压）。"""
    return {
        "name": "ComfyUI-portable",
        "url": "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
        "mirrors": [
            "https://gh-proxy.com/https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
            "https://ghfast.top/https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
        ],
        "dest": str(Path(os.environ.get("TEMP", ".")) / "ComfyUI_windows_portable.7z"),
        "bytes_hint": 1_600_000_000,
        "kind": "archive",
    }


def weight_items(suite: str) -> list[dict]:
    """按套件给权重清单。路径遵循 3D-Pack 的目录约定。"""
    items: list[dict] = []
    if suite in ("core", "all"):
        items += [
            {"name": "hunyuan3d-v2-1-unet",
             # 实测（2026-11-27，本机）：ModelScope 4 段 4.2 MB/s，hf-mirror 同条件 1.2 MB/s
             # → 国内把 ModelScope 放首位，hf-mirror 当回退
             "url": f"{MS}/AI-ModelScope/Hunyuan3D-2/resolve/master/hunyuan3d-dit-v2-0/model.fp16.safetensors",
             "mirrors": [f"{HF}/tencent/Hunyuan3D-2/resolve/main/hunyuan3d-dit-v2-0/model.fp16.safetensors"],
             "dest": str(CKPT / "hunyuan3d-v2-1" / "unet" / "diffusion_pytorch_model.bin"),
             "bytes_hint": 4_926_000_000, "suite": "core"},
        ]
    if suite in ("paint", "all"):
        items += [
            {"name": "paintpbr-unet",
             "url": f"{HF}/tencent/Hunyuan3D-2.1/resolve/main/hunyuan3d-paintpbr-v2-1/unet/diffusion_pytorch_model.bin",
             "mirrors": [f"{MS}/AI-ModelScope/Hunyuan3D-2.1/resolve/master/hunyuan3d-paintpbr-v2-1/unet/diffusion_pytorch_model.bin"],
             "dest": str(CKPT / "hunyuan3d-paintpbr-v2-1" / "unet" / "diffusion_pytorch_model.bin"),
             "bytes_hint": 3_925_000_000, "suite": "paint"},
            {"name": "paintpbr-textencoder",
             "url": f"{HF}/tencent/Hunyuan3D-2.1/resolve/main/hunyuan3d-paintpbr-v2-1/text_encoder/pytorch_model.bin",
             "mirrors": [f"{MS}/AI-ModelScope/Hunyuan3D-2.1/resolve/master/hunyuan3d-paintpbr-v2-1/text_encoder/pytorch_model.bin"],
             "dest": str(CKPT / "hunyuan3d-paintpbr-v2-1" / "text_encoder" / "pytorch_model.bin"),
             "bytes_hint": 1_362_000_000, "suite": "paint"},
        ]
    if suite in ("refine", "all"):
        items += [
            {"name": "direct3d-512",
             "url": f"{HF}/wushuang98/Direct3D-V3/resolve/main/shape_transformers_512/model.safetensors",
             "mirrors": [f"{HF}/smthem/Direct3D-V3-Comfy/resolve/main/shape_transformers_512/model.safetensors"],
             "dest": str(ROOT / "work" / "research" / "precision" / "weights" / "Direct3D-V3" / "shape_transformers_512" / "model.safetensors"),
             "bytes_hint": 4_988_000_000, "suite": "refine"},
        ]
    if suite in ("rig", "all"):
        items += [
            {"name": "riganything",
             "url": f"{HF}/Isabellaliu/RigAnything/resolve/main/riganything_ckpt.pt",
             "dest": str(ROOT / "work" / "research" / "rig" / "models" / "riganything_ckpt.pt"),
             "bytes_hint": 2_344_000_000, "suite": "rig"},
        ]
    return items


# ───────────────────────── 并行任务 ─────────────────────────

class Runner:
    """小型任务调度：无依赖的任务并行跑，有依赖的等前置完成。"""

    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.t0 = time.time()

    def run(self, jobs: list[tuple[str, callable, list[str]]]) -> dict:
        """jobs = [(名字, 可调用, 依赖名列表)]；依赖完成后才启动。"""
        done: set[str] = set()
        failed: set[str] = set()
        results: dict[str, object] = {}
        pending = list(jobs)
        running: dict = {}

        with ThreadPoolExecutor(max_workers=8) as ex:
            while pending or running:
                for name, fn, deps in list(pending):
                    if all(d in done for d in deps):
                        pending.remove((name, fn, deps))
                        if any(d in failed for d in deps):
                            failed.add(name)
                            self.log(f"  ⊘ 跳过 {name}（前置失败）")
                            continue
                        running[ex.submit(fn)] = name
                if not running:
                    time.sleep(0.2)
                    continue
                for fut in as_completed(list(running), timeout=None):
                    name = running.pop(fut)
                    try:
                        results[name] = fut.result()
                        done.add(name)
                        self.log(f"  ✓ {name} 完成（{time.time() - self.t0:.0f}s）")
                    except Exception as e:
                        failed.add(name)
                        self.log(f"  ✗ {name} 失败：{type(e).__name__}: {str(e)[:160]}")
                    break          # 一次只处理一个完成事件，重新评估可启动任务
        return {"done": done, "failed": failed, "results": results}


def _sh(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                       timeout=timeout, env=env)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 退出码 {p.returncode}: {out[-300:]}")
    return out


def _clone_pack(log) -> str:
    target = COMFY / "custom_nodes" / "ComfyUI-3D-Pack"
    if target.is_dir():
        return "已存在"
    urls = ["https://gh-proxy.com/https://github.com/MrForExample/ComfyUI-3D-Pack.git",
            "https://ghfast.top/https://github.com/MrForExample/ComfyUI-3D-Pack.git",
            "https://github.com/MrForExample/ComfyUI-3D-Pack.git"]
    err = ""
    for u in urls:
        try:
            _sh(["git", "clone", "--depth", "1", u, str(target)], timeout=1800)
            return u
        except Exception as e:
            err = str(e)
            log(f"  clone 失败（{u.split('/')[2]}）：{str(e)[:100]}")
    raise RuntimeError(err)


def _pip_comfy(log) -> str:
    if not (COMFY_ENV / "Scripts" / "python.exe").is_file():
        raise RuntimeError(f"ComfyUI 环境不存在：{COMFY_ENV}（先建 venv）")
    py = COMFY_ENV / "Scripts" / "python.exe"
    req = COMFY / "requirements.txt"
    if req.is_file():
        _sh([str(py), "-m", "pip", "install", "-q", "-r", str(req)], timeout=3600)
    return "ok"


def _extract_archive(log) -> str:
    """解压 ComfyUI 便携版（7z 用 7z.exe，zip 用 Python）。"""
    src = _comfy_portable()["dest"]
    if not Path(src).is_file():
        return "没有下载到压缩包"
    target = COMFY
    if target.exists() and any(target.iterdir()):
        return "已存在"
    seven = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
    if Path(src).suffix.lower() == ".7z" and Path(seven).is_file():
        _sh([seven, "x", src, f"-o{target.parent}", "-y"], timeout=1800)
        return "7z"
    import zipfile
    with zipfile.ZipFile(src) as z:
        z.extractall(target.parent)
    return "zip"


def patch_config(comfy_dir: Path, log) -> str:
    p = ROOT / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg.setdefault("paths", {})["comfyui"] = str(comfy_dir)
    cfg.setdefault("backend", {})["mode"] = "auto"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="并行装齐铸想需要的 ComfyUI + 权重")
    ap.add_argument("--only", default="core", choices=list(SUITES))
    ap.add_argument("--workers", type=int, default=8, help="同时下几个文件")
    ap.add_argument("--conns", type=int, default=8, help="单文件几条连接（镜像限速时越多越快）")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)

    def log(s):
        print(s, flush=True)

    items = weight_items(ns.only)
    total_hint = sum(int(i.get("bytes_hint") or 0) for i in items)
    log(f"  套件 {ns.only}：{SUITES[ns.only]}")
    log(f"  权重 {len(items)} 个，约 {total_hint/1024**3:.1f} GB；"
        f"并发 {ns.workers} 文件 × {ns.conns} 连接")

    if ns.dry_run:
        for i in items:
            log(f"    · {i['name']:<28} {int(i.get('bytes_hint') or 0)/1024**3:.1f} GB → {i['dest']}")
        log("  （dry-run，没有真的下载）")
        return 0

    from backend import downloader

    runner = Runner(log)
    meter_box: dict = {}

    def download_task():
        res = downloader.download_all(items, workers=ns.workers, conns=ns.conns, log=log)
        meter_box["results"] = res
        bad = [r for r in res if not r.get("ok")]
        if bad:
            raise RuntimeError(f"{len(bad)} 个下载失败：{bad[0].get('error')}")
        return len(res)

    def _pip_main() -> str:
        """铸想自身依赖：和下载同时跑（互不干扰，纯并行收益）。"""
        req = ROOT / "requirements.txt"
        if not req.is_file():
            return "没有 requirements.txt"
        _sh([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], timeout=3600)
        return "ok"

    # 三条互不依赖的线同时推进：下载（内部再并行）、解压、clone；
    # pip 装铸想依赖与它们并行；ComfyUI 的 pip 要等解压+clone；配置与体检最后。
    jobs = [
        ("下载权重", download_task, []),
        ("解压 ComfyUI", _extract_archive, []),
        ("3D-Pack 仓库", lambda: _clone_pack(log), []),
        ("铸想依赖(pip)", _pip_main, []),
    ]
    out = runner.run(jobs)
    # 依赖阶段：pip 要在解压 + clone 之后
    if "解压 ComfyUI" in out["done"] and "3D-Pack 仓库" in out["done"]:
        try:
            log("  → 装 ComfyUI 依赖（pip）")
            log("    " + _pip_comfy(log))
        except Exception as e:
            log(f"  ✗ pip 失败：{e}")
    try:
        log("  → 写配置：" + patch_config(COMFY, log))
    except Exception as e:
        log(f"  ✗ 写配置失败：{e}")

    try:
        log("  → 体检")
        _sh([sys.executable, str(ROOT / "tools" / "doctor.py")], timeout=600)
        log("    doctor 通过")
    except Exception as e:
        log(f"    doctor 有问题：{str(e)[:200]}")

    ok = not out["failed"]
    log(f"\n  {'完成' if ok else '部分失败'} · 用时 {time.time() - runner.t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
