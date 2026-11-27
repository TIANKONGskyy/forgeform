# -*- coding: utf-8 -*-
"""把 ComfyUI 装成"铸想能用"的样子（2026-11-28 实测沉淀）。

为什么单独一个脚本
------------------
`bootstrap_models.py` 负责"下载权重"，但实测发现真正难的是 **ComfyUI 这一侧**：
torch 版本、Python 版本、缺依赖、扩展不重启不生效、`.7z` 解压、模型目录被覆盖……
这些坑散落在四处，用户装到一半就卡住。

这个脚本把整条链固化下来，每一步都可重复执行（已完成的会跳过）：
    1. venv（默认 D:\\comfy_venv312，避免 C 盘写满）
    2. torch 固定版本（默认 2.7.1+cu126；最新 ComfyUI 要 ≥2.7）
    3. ComfyUI 源码（zip 走 gh-proxy，git clone 会 0 字节卡死）
    4. ComfyUI requirements（过滤掉清华镜像缺失的 workflow-templates）
    5. 3D-Pack / GGUF / Paint 节点包（zip）
    6. 已知缺失的依赖（实测清单，一次装齐，避免反复重启）
    7. 启动并验证节点真的加载了（扩展装了不重启不生效，必须验）
    8. 跑 capability_doctor 报告五项能力状态

用法：
    python tools/install_comfy.py                 # 全套（约 10–15 分钟）
    python tools/install_comfy.py --skip-torch    # 已有 torch
    python tools/install_comfy.py --verify-only    # 只检查
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMFY = Path(os.environ.get("FORGEFORM_COMFY_DIR", r"D:\ComfyUI-New"))
VENV = Path(os.environ.get("FORGEFORM_COMFY_ENV", r"D:\comfy_venv312"))
PORT = int(os.environ.get("FORGEFORM_COMFY_PORT", "8188"))

TORCH_VER = os.environ.get("FORGEFORM_TORCH", "2.7.1")
TV_VER = os.environ.get("FORGEFORM_TORCHVISION", "0.22.1")
CUDA_TAG = os.environ.get("FORGEFORM_CUDA", "cu126")
MIRROR = "https://gh-proxy.com/https://codeload.github.com"

# 实测缺过的依赖（3D-Pack + Paint 包 + GGUF 扩展）
EXTRA_DEPS = [
    "gguf", "sentencepiece", "trimesh", "pymeshlab", "pymeshfix", "xatlas", "open3d",
    "einops", "omegaconf", "pyhocon", "diffusers", "transformers", "accelerate",
    "safetensors", "scipy", "scikit-image", "opencv-python", "kornia", "timm",
    "open_clip_torch", "rembg", "onnxruntime", "imageio", "imageio-ffmpeg",
    "matplotlib", "easydict", "iopath", "mmgp", "PyMCubes", "plyfile", "pygltflib",
    "torchmetrics", "pytorch-lightning", "peft", "jaxtyping", "torchtyping",
    "PyOpenGL", "PyOpenGL-accelerate", "fast-simplification",
]

NODE_PACKS = {
    "3dpack": f"{MIRROR}/MrForExample/ComfyUI-3D-Pack/zip/refs/heads/main",
    "gguf": f"{MIRROR}/city96/ComfyUI-GGUF/zip/refs/heads/main",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def _sh(cmd: list[str], timeout: int = 3600, cwd: Path | None = None) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1")
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(cwd) if cwd else None, env=env)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} 退出码 {p.returncode}: {out[-400:]}")
    return out


def _venv_py() -> Path:
    return VENV / "Scripts" / "python.exe"


def step_venv() -> None:
    if _venv_py().is_file():
        log("  ✓ venv 已存在")
        return
    py = shutil.which("python") or sys.executable
    ver = _sh([py, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"]).strip()
    if tuple(int(x) for x in ver.split(".")) < (3, 11):
        raise RuntimeError(f"需要 Python ≥3.11（当前 {ver}）")
    log(f"  建 venv：{VENV}（Python {ver}）")
    _sh([py, "-m", "venv", str(VENV)])


def step_torch() -> None:
    vp = _venv_py()
    try:
        cur = _sh([str(vp), "-c", "import torch;print(torch.__version__)"]).strip()
    except Exception:
        cur = ""
    if cur.startswith(TORCH_VER):
        log(f"  ✓ torch 已是 {cur}")
        return
    log(f"  装 torch {TORCH_VER}+{CUDA_TAG}（约 2.5 GB）")
    _sh([str(vp), "-m", "pip", "install", "-q",
         f"torch=={TORCH_VER}", f"torchvision=={TV_VER}", f"torchaudio=={TORCH_VER}",
         "--index-url", f"https://download.pytorch.org/whl/{CUDA_TAG}"])


def step_comfy_source() -> None:
    if (COMFY / "main.py").is_file():
        log("  ✓ ComfyUI 源码已存在")
        return
    import requests
    log("  下 ComfyUI 源码（zip）")
    r = requests.get(f"{MIRROR}/comfyanonymous/ComfyUI/zip/refs/heads/master",
                     timeout=300, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI zip HTTP {r.status_code}")
    import io
    tmp = Path(os.environ.get("TEMP", ".")) / "ff_comfy_src"
    shutil.rmtree(tmp, ignore_errors=True)
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(tmp)
    src = next(tmp.iterdir())
    COMFY.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = COMFY / item.name
        # 绝不能删 models/（实测因此白下 11 GB 权重）
        if item.is_dir():
            if target.exists():
                continue
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    log(f"  ✓ 源码 → {COMFY}（models/ 未被覆盖）")


def step_requirements() -> None:
    req = COMFY / "requirements.txt"
    if not req.is_file():
        return
    lines = [ln for ln in req.read_text(encoding="utf-8").splitlines()
             if ln.strip() and "comfyui-workflow-templates" not in ln]
    filtered = COMFY / "requirements.ff.txt"
    filtered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("  装 ComfyUI requirements（已过滤模板包）")
    _sh([str(_venv_py()), "-m", "pip", "install", "-q", "-r", str(filtered)])


def step_node_packs() -> None:
    import io
    import requests
    dst = COMFY / "custom_nodes"
    dst.mkdir(parents=True, exist_ok=True)
    for name, url in NODE_PACKS.items():
        target_dir = dst / ("ComfyUI-3D-Pack-main" if name == "3dpack" else "ComfyUI-GGUF-main")
        if target_dir.is_dir() and any(target_dir.iterdir()):
            log(f"  ✓ 节点包已存在：{target_dir.name}")
            continue
        r = requests.get(url, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log(f"  ✗ {name} 下载失败 HTTP {r.status_code}")
            continue
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(dst)
        log(f"  ✓ {name}（{len(r.content)//1024} KB）")


def step_extra_deps() -> None:
    log(f"  装实测缺失依赖（{len(EXTRA_DEPS)} 个）")
    _sh([str(_venv_py()), "-m", "pip", "install", "-q"] + EXTRA_DEPS)


def step_kiui() -> None:
    vp = str(_venv_py())
    try:
        _sh([vp, "-c", "import kiui, utils3d"])
        log("  ✓ kiui/utils3d 已装")
        return
    except Exception:
        pass
    import io
    import requests
    base = Path(os.environ.get("TEMP", ".")) / "ff_pack_deps"
    for name, url in (
        ("kiui", f"{MIRROR}/ashawkey/kiuikit/zip/refs/heads/main"),
        ("utils3d", f"{MIRROR}/EasternJournalist/utils3d/zip/refs/heads/main"),
    ):
        r = requests.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            continue
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(d)
        sub = next((p for p in d.iterdir() if p.is_dir()), None)
        if sub:
            _sh([vp, "-m", "pip", "install", "-q", str(sub)])
            log(f"  ✓ {name}")


def step_start_and_verify() -> None:
    import requests
    # 已在跑就不重复起
    try:
        r = requests.get(f"http://127.0.0.1:{PORT}/system_stats", timeout=3)
        if r.status_code == 200:
            log("  ✓ ComfyUI 已在运行")
            online = True
        else:
            online = False
    except Exception:
        online = False
    if not online:
        log("  启动 ComfyUI …")
        subprocess.Popen(
            [str(_venv_py()), "main.py", "--port", str(PORT),
             "--disable-cuda-malloc", "--use-pytorch-cross-attention"],
            cwd=str(COMFY), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while time.time() - t0 < 180:
            time.sleep(5)
            try:
                if requests.get(f"http://127.0.0.1:{PORT}/system_stats", timeout=3).status_code == 200:
                    online = True
                    break
            except Exception:
                continue
    if not online:
        log("  ✗ ComfyUI 没起来（看 logs 或手动 python main.py）")
        return
    try:
        oi = json.loads(requests.get(f"http://127.0.0.1:{PORT}/object_info", timeout=40).content)
    except Exception as e:
        log(f"  ✗ object_info 读取失败：{e}")
        return
    log(f"  ✓ 节点 {len(oi)} 个")
    checks = {
        "3D-Pack（MV 形状）": "[Comfy3D] Hunyuan3D V2 ShapeGen MV",
        "本地网格节点": "LocalResearchLoadMesh",
        "GGUF 加载": "UnetLoaderGGUF",
    }
    for label, node in checks.items():
        log(f"   {'✓' if node in oi else '✗'} {label}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把 ComfyUI 装成铸想能用的样子")
    ap.add_argument("--skip-torch", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ns = ap.parse_args()

    steps = [
        ("venv", step_venv),
        ("torch", None if ns.skip_torch else step_torch),
        ("ComfyUI 源码", step_comfy_source),
        ("requirements", step_requirements),
        ("节点包", step_node_packs),
        ("额外依赖", step_extra_deps),
        ("kiui/utils3d", step_kiui),
        ("启动与验证", step_start_and_verify),
    ]
    if ns.verify_only:
        steps = [s for s in steps if s[0] == "启动与验证"]

    t0 = time.time()
    for name, fn in steps:
        if fn is None:
            continue
        log(f"[{name}]")
        try:
            fn()
        except Exception as e:
            log(f"  ✗ {name} 失败：{type(e).__name__}: {str(e)[:300]}")
    log(f"\n  用时 {time.time()-t0:.0f}s")

    # 最后跑能力检查
    try:
        log("[能力就绪]")
        _sh([sys.executable, str(ROOT / "tools" / "capability_doctor.py")], timeout=300)
    except Exception as e:
        log(f"  能力检查失败：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
