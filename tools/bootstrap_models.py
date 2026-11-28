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
# 3D-Pack / Paint 节点包需要的**纯 Python** 依赖（编译型 CUDA 包另列，Windows 上难装）
PACK_PIP = [
    "pyhocon", "trimesh", "einops", "omegaconf", "diffusers", "transformers",
    "accelerate", "safetensors", "scipy", "opencv-python", "kornia",
    "open_clip_torch", "xatlas", "pymeshlab", "pymeshfix", "pygltflib", "timm",
    "jaxtyping", "torchtyping", "rembg", "onnxruntime", "imageio",
    "imageio-ffmpeg", "matplotlib", "easydict", "iopath", "mmgp", "PyMCubes",
    "plyfile", "gguf", "sentencepiece", "pytorch_msssim", "lightning",
    "torchmetrics", "open3d", "scikit-image", "fast-simplification",
]

COMFY_ENV = Path(os.environ.get("FORGEFORM_COMFY_ENV", r"D:\comfy312"))


def _comfy_python() -> str:
    """装 ComfyUI 侧依赖该用哪个解释器。

    2026-11-28 实测踩到：bootstrap 用 sys.executable 装 3D-Pack 依赖，
    但 ComfyUI 实际跑在 `tools/install_comfy.py` 建的 venv 里 →
    "依赖装好了，ComfyUI 还是 import 失败"。有 venv 就必须装进 venv。
    """
    for cand in (COMFY_ENV / "Scripts" / "python.exe", COMFY / "python_embeded" / "python.exe"):
        if cand.is_file():
            return str(cand)
    return sys.executable

CKPT = COMFY / "custom_nodes" / "ComfyUI-3D-Pack-main" / "Checkpoints" / "Diffusers" / "tencent" / "Hunyuan3D-2_1"

# HF 直连在国内基本不通；镜像优先
HF = "https://hf-mirror.com"
MS = "https://modelscope.cn/models"

SUITES = {
    "core": "ComfyUI + 3D-Pack + Hunyuan3D 2.1（能单图建模）",
    "direct": "hy3dgen 直连权重（2mini，约 12 GB）——不依赖 ComfyUI 节点，本机实测最稳",
    "image": "出图最小集：Z-Image Turbo int8 + Qwen3-4B 编码器 + VAE（约 11 GB，实测 21 s/张）",
    "paint": "贴图权重（PBR 2.1）",
    "mv": "多视图权重（Hunyuan3D 2mv）",
    "refine": "精修权重（Direct3D-V3 512 + DINOv3）",
    "rig": "RigAnything 语义骨骼",
    "all": "全部",
}


def _ensure_comfy_env(log) -> str:
    """确保 ComfyUI + 它的 venv 就绪（出图必须有能跑的 ComfyUI）。"""
    venv_py = COMFY_ENV / "Scripts" / "python.exe"
    if (COMFY / "main.py").is_file() and venv_py.is_file():
        return "已存在"
    log("    调 tools/install_comfy.py 装 ComfyUI 运行环境（约 8 分钟）…")
    _sh([sys.executable, str(ROOT / "tools" / "install_comfy.py")], timeout=3600)
    return "ok"


def _image_items() -> list[dict]:
    """出图最小集：Z-Image Turbo（int8）+ Qwen3-4B 编码器 + VAE。

    2026-11-28 实测：这套 11 GB 在本机 4 步 1024² 出图 21 秒，
    是"能真的用起来"的最小图像栈；FLUX/Qwen-Image 都要 20–50 GB。
    """
    hf = "https://hf-mirror.com/Comfy-Org/z_image_turbo/resolve/main"
    dm = COMFY / "models" / "diffusion_models"
    te = COMFY / "models" / "text_encoders"
    va = COMFY / "models" / "vae"
    return [
        {"name": "z_image_int8", "url": f"{hf}/split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors",
         "dest": str(dm / "z_image_turbo_int8_convrot.safetensors"),
         "bytes_hint": 6_201_000_000, "suite": "image"},
        {"name": "qwen3_4b_fp8", "url": f"{hf}/split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors",
         "dest": str(te / "qwen_3_4b_fp8_mixed.safetensors"),
         "bytes_hint": 5_632_000_000, "suite": "image"},
        {"name": "z_image_vae", "url": f"{hf}/split_files/vae/ae.safetensors",
         "dest": str(va / "ae.safetensors"), "bytes_hint": 336_000_000, "suite": "image"},
    ]


def patch_image_config(log) -> str:
    """把 z_image 指向实际下到的 safetensors（默认配置里是 GGUF 文件名）。"""
    p = ROOT / "config.json"
    if not p.is_file():
        return "没有 config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    img = (cfg.get("models") or {}).get("image") or {}
    z = img.get("z_image")
    if not isinstance(z, dict):
        return "配置里没有 z_image"
    z["checkpoint"] = "z_image_turbo_int8_convrot.safetensors"
    z["clip_l"] = "qwen_3_4b_fp8_mixed.safetensors"
    z["vae"] = "ae.safetensors"
    z["clip_type"] = "qwen_image"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return "z_image → safetensors"


def _comfy_portable() -> dict:
    """ComfyUI 便携版：直接用官方 release 的 zip（不用 7z，Windows 自带解压）。"""
    return {
        "name": "ComfyUI-portable",
        # 实测（2026-11-28）：github.com 直连超时，gh-proxy 能到 1.99 GB → 镜像放首位
        "url": "https://gh-proxy.com/https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
        "mirrors": [
            "https://ghfast.top/https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
            "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z",
        ],
        "dest": str(Path(os.environ.get("TEMP", ".")) / "ComfyUI_windows_portable.7z"),
        "bytes_hint": 2_085_000_000,
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
    """装 ComfyUI-3D-Pack：用 zip（实测 git clone 走 gh-proxy 会卡在 0 字节）。"""
    import io
    import zipfile

    import requests

    target = COMFY / "custom_nodes" / "ComfyUI-3D-Pack-main"
    # 判据必须看真实内容：只认 __init__.py + nodes 目录
    if (target / "__init__.py").is_file() and (target / "nodes.py").is_file():
        return "已存在"

    dst = COMFY / "custom_nodes"
    dst.mkdir(parents=True, exist_ok=True)
    keep = target / "Checkpoints"
    tmp_keep = Path(os.environ.get("TEMP", ".")) / "ff_ckpt_keep"
    if keep.is_dir():
        if tmp_keep.exists():
            shutil.rmtree(tmp_keep, ignore_errors=True)
        shutil.move(str(keep), str(tmp_keep))

    urls = [
        "https://gh-proxy.com/https://codeload.github.com/MrForExample/ComfyUI-3D-Pack/zip/refs/heads/main",
        "https://ghfast.top/https://codeload.github.com/MrForExample/ComfyUI-3D-Pack/zip/refs/heads/main",
    ]
    err = ""
    for u in urls:
        try:
            log(f"    zip {u.split('/')[2]} ...")
            r = requests.get(u, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            z = zipfile.ZipFile(io.BytesIO(r.content))
            top = z.namelist()[0].split("/")[0]
            z.extractall(dst)
            ext = dst / top
            if ext != target:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                ext.rename(target)
            if tmp_keep.exists():
                dest = target / "Checkpoints"
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.move(str(tmp_keep), str(dest))
            return f"zip {len(r.content)//1024//1024} MB"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log(f"    ✗ {err}")
    raise RuntimeError(err)


def _pip_comfy(log) -> str:
    if not (COMFY_ENV / "Scripts" / "python.exe").is_file():
        raise RuntimeError(f"ComfyUI 环境不存在：{COMFY_ENV}（先建 venv）")
    py = COMFY_ENV / "Scripts" / "python.exe"
    req = COMFY / "requirements.txt"
    if req.is_file():
        _sh([str(py), "-m", "pip", "install", "-q", "-r", str(req)], timeout=3600)
    return "ok"



def _install_comfy_source(log) -> str:
    """源码装 ComfyUI：**zip 下载** + pip 依赖（不要 git clone）。

    ★ 2026-11-28 实测两件事：
      1. 便携版 1.99 GB，走镜像只有 0.3–1.3 MB/s（实测 0.95 MB/s，要 35 分钟以上）
      2. `git clone` 走 gh-proxy 会卡在 0 字节（10 分钟无进展），zip 只要 2 秒（13 MB）
    所以这里一律用 codeload zip。
    """
    import io
    import zipfile

    import requests

    if (COMFY / "main.py").is_file():
        return "已存在"

    urls = [
        "https://gh-proxy.com/https://codeload.github.com/comfyanonymous/ComfyUI/zip/refs/heads/master",
        "https://ghfast.top/https://codeload.github.com/comfyanonymous/ComfyUI/zip/refs/heads/master",
    ]
    body = None
    err = ""
    for u in urls:
        try:
            log(f"    下载 ComfyUI 源码 zip（{u.split('/')[2]}）...")
            r = requests.get(u, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and len(r.content) > 1024 * 1024:
                body = r.content
                break
            err = f"HTTP {r.status_code} / {len(r.content)} B"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if body is None:
        raise RuntimeError(f"ComfyUI 源码下载失败：{err}")

    COMFY.mkdir(parents=True, exist_ok=True)
    tmp = Path(os.environ.get("TEMP", ".")) / "ff_comfy_src_zip"
    shutil.rmtree(tmp, ignore_errors=True)
    zipfile.ZipFile(io.BytesIO(body)).extractall(tmp)
    src = next(tmp.iterdir())
    n = 0
    for item in src.iterdir():
        target = COMFY / item.name
        # 绝不覆盖 models/：实测覆盖会清空已下载的权重
        if item.is_dir():
            if target.exists():
                continue
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
        n += 1
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"    ✓ 源码解压 {n} 项（models/ 未覆盖）")
    return "zip"


def _install_pack_deps(log) -> str:
    """装 3D-Pack 依赖 + 两个 git 依赖（kiui/utils3d，用 zip 装）。

    2026-11-28 实测：3D-Pack 的 requirements 里有 spconv-cu126 / cumm / slangtorch /
    nerfacc 这些需要现场编译的 CUDA 包，Windows + torch2.6 基本编不过；
    而 Hunyuan3D 图生 3D 只用得到纯 Python 那部分，所以这里只装纯 Python 依赖，
    并把缺的编译包列出来提示用户。
    """
    import importlib

    from backend import downloader as dl

    el = os.environ.get("PIP_INDEX_URL") or "https://pypi.tuna.tsinghua.edu.cn/simple"
    _sh([_comfy_python(), "-m", "pip", "install", "-q", "-i", el] + PACK_PIP, timeout=3600)

    # kiui / utils3d 只有 GitHub，走 zip
    deps = Path(os.environ.get("TEMP", ".")) / "ff_pack_deps"
    deps.mkdir(parents=True, exist_ok=True)
    for name, url in (
        ("kiui", "https://gh-proxy.com/https://codeload.github.com/ashawkey/kiuikit/zip/refs/heads/main"),
        ("utils3d", "https://gh-proxy.com/https://codeload.github.com/EasternJournalist/utils3d/zip/refs/heads/main"),
    ):
        try:
            importlib.import_module(name)
            continue
        except Exception:
            pass
        zpath = deps / f"{name}.zip"
        res = dl.download({"name": name, "url": url, "dest": str(zpath)}, dl.Meter(), conns=2)
        if not res.get("ok"):
            log(f"    ✗ {name} 下载失败")
            continue
        import zipfile
        with zipfile.ZipFile(zpath) as z:
            z.extractall(deps / name)
        sub = next((p for p in (deps / name).iterdir() if p.is_dir()), None)
        if sub:
            _sh([sys.executable, "-m", "pip", "install", "-q", str(sub)], timeout=1800)

    missing = []
    for m in ("spconv", "cumm", "slangtorch", "nerfacc", "torch_scatter"):
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    if missing:
        log(f"    ⚠ 缺编译型 CUDA 包：{', '.join(missing)}（Windows 上难编；缺它们只影响部分节点）")
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
    if Path(src).suffix.lower() == ".7z":
        if Path(seven).is_file():
            _sh([seven, "x", src, f"-o{target.parent}", "-y"], timeout=1800)
            return "7z"
        # 本机实测很多 Windows 没装 7-Zip（2026-11-28）→ 用纯 Python 的 py7zr 兜底
        try:
            import py7zr  # noqa: F401
        except Exception:
            log("  · 没有 7-Zip，尝试装 py7zr")
            _sh([sys.executable, "-m", "pip", "install", "-q", "py7zr"], timeout=600)
        import py7zr
        with py7zr.SevenZipFile(src, mode="r") as z:
            z.extractall(path=target.parent)
        return "py7zr"
    import zipfile
    with zipfile.ZipFile(src) as z:
        z.extractall(target.parent)
    return "zip"


def _install_hy3dgen_direct(log) -> str:
    """下 hy3dgen 直连权重（tencent/Hunyuan3D-2mini）并写进 config。

    为什么单独一条：本机实测 ComfyUI+3D-Pack 被 pytorch3d 卡死，
    而 hy3dgen 直连（backend.mode=direct）能稳定出模。
    """
    target = Path(os.environ.get("FORGEFORM_HY3DGEN_DIR", r"D:\models\hy3dgen")) / "hunyuan3d-2mini"
    if (target / "model.fp16.safetensors").is_file() or (target / "config.json").is_file():
        return "已存在"

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from huggingface_hub import snapshot_download

    log(f"    下 hy3dgen 2mini → {target}（约 12 GB）")
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        "tencent/Hunyuan3D-2mini",
        local_dir=str(target),
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.yaml"],
        max_workers=8,
    )
    return "ok"


def patch_hy3dgen_config(log) -> str:
    """把直连权重写进 config（models.3d.hunyuan_mini.path）。"""
    p = ROOT / "config.json"
    if not p.is_file():
        return "没有 config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    path = Path(os.environ.get("FORGEFORM_HY3DGEN_DIR", r"D:\models\hy3dgen")) / "hunyuan3d-2mini"
    slot = ((cfg.get("models") or {}).get("3d") or {}).get("hunyuan_mini")
    if isinstance(slot, dict):
        slot["path"] = str(path)
        slot["backend"] = "hy3dgen"
    else:
        cfg.setdefault("models", {}).setdefault("3d", {})["hunyuan_mini"] = {
            "path": str(path), "backend": "hy3dgen"}
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def patch_config(comfy_dir: Path, log) -> str:
    p = ROOT / "config.json"
    if not p.is_file():
        example = ROOT / "config.example.json"
        if example.is_file():
            shutil.copy2(example, p)
            log("    （干净安装：先由 config.example.json 生成 config.json）")
        else:
            raise FileNotFoundError("既没有 config.json 也没有 config.example.json")
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
    ap.add_argument("--comfy", default="source", choices=["source", "portable"],
                    help="ComfyUI 安装方式：source=clone+pip（快，默认）；portable=便携版 1.99GB")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)

    def log(s):
        print(s, flush=True)

    items = weight_items(ns.only)
    if ns.only in ("image", "all"):
        items = items + _image_items()
    # ★ 2026-11-28 实测：ComfyUI 便携版之前只登记了 URL，压根没进下载队列，
    #   结果"解压 ComfyUI"必然失败。这里把它并进同一批（同样多线程下）。
    if ns.comfy == "portable":
        comfy_item = _comfy_portable()
        comfy_item.pop("kind", None)
        items = items + [comfy_item]
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
        import time as _t

        def _tick(p):
            log(f"    … {p['mb']:7.0f} MB  当前 {p['mbps']:5.1f} MB/s  峰值 {p['peak']:5.1f} MB/s")

        res = downloader.download_all(items, workers=ns.workers, conns=ns.conns,
                                      log=log, on_progress=_tick)
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
        _sh([_comfy_python(), "-m", "pip", "install", "-q", "-r", str(req)], timeout=3600)
        return "ok"

    # 三条互不依赖的线同时推进：下载（内部再并行）、解压、clone；
    # pip 装铸想依赖与它们并行；ComfyUI 的 pip 要等解压+clone；配置与体检最后。
    comfy_job = (("安装 ComfyUI(源码)", lambda: _install_comfy_source(log), [])
                 if ns.comfy == "source"
                 else ("解压 ComfyUI", lambda: _extract_archive(log), []))
    if ns.only == "direct":
        items = []

    jobs = []
    if items:
        jobs.append(("下载权重", download_task, []))
    jobs += [
        comfy_job,
        ("3D-Pack 仓库", lambda: _clone_pack(log), ["安装 ComfyUI(源码)"]),
        ("3D-Pack 依赖", lambda: _install_pack_deps(log), ["3D-Pack 仓库"]),
        ("铸想依赖(pip)", _pip_main, []),
    ]
    if ns.only in ("direct", "core", "all"):
        jobs.append(("hy3dgen 直连权重", lambda: _install_hy3dgen_direct(log), []))
    if ns.only in ("image", "all"):
        jobs.append(("ComfyUI 环境", lambda: _ensure_comfy_env(log), []))
    out = runner.run(jobs)
    # 依赖阶段：pip 要在解压 + clone 之后
    if "解压 ComfyUI" in out["done"] and "3D-Pack 仓库" in out["done"]:
        try:
            log("  → 装 ComfyUI 依赖（pip）")
            log("    " + _pip_comfy(log))
        except Exception as e:
            log(f"  ✗ pip 失败：{e}")
    try:
        if ns.only in ("direct", "core", "all"):
            log("  → 直连权重写进配置：" + patch_hy3dgen_config(log))
        if ns.only in ("image", "all"):
            log("  → 图像模型写进配置：" + patch_image_config(log))
        log("  → 写配置：" + patch_config(COMFY, log))
    except Exception as e:
        log(f"  ✗ 写配置失败：{e}")

    try:
        log("  → 体检（--fix 会补建 outputs/logs/projects/temp 与 config.json）")
        _sh([sys.executable, str(ROOT / "tools" / "doctor.py"), "--fix"], timeout=600)
        log("    doctor 通过")
    except Exception as e:
        log(f"    doctor 有问题：{str(e)[:200]}")

    ok = not out["failed"]
    log(f"\n  {'完成' if ok else '部分失败'} · 用时 {time.time() - runner.t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
