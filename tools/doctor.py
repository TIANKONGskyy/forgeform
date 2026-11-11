# -*- coding: utf-8 -*-
"""装完先跑这个：告诉你缺什么、能不能开工。

    python tools/doctor.py

不加载模型、不启动服务、不写仓库（除了首次补一份 config.json）。
每项检查独立，任何一项失败都只是 ✗，不会中断后续检查。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, BAD, WARN = "✓", "✗", "!"

_results: list[tuple[str, str, str, str]] = []   # (级别, 项目, 值, 建议)


def add(level: str, item: str, value: str = "", hint: str = "") -> None:
    _results.append((level, item, value, hint))


# ── 1. Python ──────────────────────────────────────────────
def check_python() -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        add(OK, "Python", f"{ver} ({platform.system()})")
    else:
        add(BAD, "Python", ver, "需要 3.11 或更高")


# ── 2. 依赖 ────────────────────────────────────────────────
PKGS = [
    # (import 名, pip 名, 是否必需, 缺了会怎样)
    ("gradio", "gradio", True, "无界面"),
    ("fastapi", "fastapi", True, "无自定义路由"),
    ("trimesh", "trimesh", True, "无法导出 STL"),
    ("numpy", "numpy", True, "无法处理网格"),
    ("requests", "requests", True, "连不上 ComfyUI"),
    ("psutil", "psutil", True, "无硬件监控"),
    ("PIL", "Pillow", True, "无法处理图片"),
    ("websocket", "websocket-client", True, "无实时进度"),
    ("pymeshlab", "pymeshlab", False, "补洞降级"),
    ("pymeshfix", "pymeshfix", False, "水密率下降"),
    ("fast_simplification", "fast-simplification", False, "减面变慢"),
    ("rtree", "rtree", False, "网格查询变慢"),
]


def check_deps() -> None:
    missing_required, missing_optional = [], []
    versions = []
    for mod, pipname, required, effect in PKGS:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            versions.append(f"{pipname}{(' ' + ver) if ver else ''}")
        except Exception:
            (missing_required if required else missing_optional).append((pipname, effect))

    if missing_required:
        for name, effect in missing_required:
            add(BAD, f"依赖 {name}", "缺失", f"{effect} → pip install {name}")
    else:
        add(OK, "依赖（必需）", "全部就绪")

    if missing_optional:
        for name, effect in missing_optional:
            add(WARN, f"依赖 {name}", "缺失", f"{effect}（可选，装上更好）")
    else:
        add(OK, "依赖（可选）", "全部就绪")

    if versions:
        add(OK, "版本", " · ".join(versions[:6]) + (" …" if len(versions) > 6 else ""))


# ── 3. config ──────────────────────────────────────────────
def check_config() -> dict:
    cfg_path = ROOT / "config.json"
    example = ROOT / "config.example.json"
    if not cfg_path.exists():
        if example.exists():
            shutil.copyfile(example, cfg_path)
            add(WARN, "config.json", "不存在 → 已从 example 复制", "把 models 里的文件名改成你本地的")
        else:
            add(BAD, "config.json", "不存在且没有 example", "仓库不完整")
            return {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        add(OK, "config.json", "可解析")
        return cfg
    except Exception as exc:
        add(BAD, "config.json", f"解析失败: {exc}", "用 config.example.json 覆盖回去")
        return {}


# ── 4. 路径 ────────────────────────────────────────────────
def resolve_paths(cfg: dict) -> dict:
    try:
        from backend.env import normalize_config_paths, COMFYUI_DIR
    except Exception:
        return cfg.get("paths", {}) or {}
    try:
        normalize_config_paths(cfg)
    except Exception:
        pass
    return cfg.get("paths", {}) or {}


def check_paths(paths: dict) -> None:
    comfy = paths.get("comfyui", "")
    if comfy and Path(comfy).exists():
        add(OK, "ComfyUI 目录", comfy)
    else:
        add(BAD, "ComfyUI 目录", comfy or "(未设置)",
            '设环境变量 COMFYUI_DIR，或在 config.json 的 paths.comfyui 里写绝对路径')

    for key in ("output", "temp"):
        p = paths.get(key, "")
        if not p:
            continue
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
            add(OK, f"目录 {key}", p)
        except OSError as exc:
            add(BAD, f"目录 {key}", f"{p} ({exc})", "检查磁盘权限/空间")


# ── 5. ComfyUI 服务 ────────────────────────────────────────
def check_comfy_service() -> None:
    url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        devs = data.get("devices") or []
        name = devs[0].get("name", "?") if devs else "?"
        vram = devs[0].get("vram_total", 0) / 1024**3 if devs else 0
        add(OK, "ComfyUI 服务", f"{url}  ({name}{f', {vram:.0f}G' if vram else ''})")
    except Exception:
        add(BAD, "ComfyUI 服务", f"{url} 无响应",
            "先起 ComfyUI；Windows 上记得 PYTHONIOENCODING=utf-8，V100 别加 --lowvram")


# ── 6. 模型文件 ────────────────────────────────────────────
def check_models(cfg: dict, paths: dict) -> None:
    comfy = Path(paths.get("comfyui") or "")
    if not comfy.exists():
        add(WARN, "模型文件", "跳过", "ComfyUI 目录不可用")
        return

    # ComfyUI 的模型可能落在这些目录（不同节点读不同位置）
    search_dirs = ["diffusion_models", "checkpoints", "unet", "vae", "text_encoders",
                   "clip", "loras", "3d"]
    index = {}
    for sub in search_dirs:
        d = comfy / "models" / sub
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    index[f.name] = sub

    models = cfg.get("models") or {}
    for stage in ("image", "video", "3d"):
        table = models.get(stage) or {}
        for key, mcfg in table.items():
            if not isinstance(mcfg, dict):
                continue
            want = [v for k, v in mcfg.items()
                    if k in ("checkpoint", "vae", "clip_l", "clip_t5", "clip", "lora",
                             "turbo_lora", "video_vae", "audio_vae", "model", "clip_vision")
                    and isinstance(v, str) and v.strip()]
            if not want:
                continue
            hit = [w for w in want if Path(w).name in index]
            miss = [w for w in want if Path(w).name not in index]
            label = f"{stage}.{key}"
            if miss:
                add(WARN, label, f"{len(hit)}/{len(want)} 找到",
                    "缺: " + ", ".join(Path(x).name for x in miss[:3])
                    + (" …" if len(miss) > 3 else ""))
            else:
                add(OK, label, f"{len(hit)}/{len(want)} 找到")


# ── 7. GPU ─────────────────────────────────────────────────
def check_gpu() -> None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (out.stdout or "").strip().splitlines()
        if line:
            add(OK, "GPU", line[0])
        else:
            add(WARN, "GPU", "nvidia-smi 无输出", "无 GPU 时只能看界面，生成不可用")
    except Exception:
        add(WARN, "GPU", "未检测到 nvidia-smi", "无 GPU 时只能看界面，生成不可用")


# ── 报告 ───────────────────────────────────────────────────
def report() -> int:
    icon = {OK: "[OK]  ", BAD: "[FAIL]", WARN: "[WARN]"}
    width = max(len(i) for _, i, _, _ in _results) if _results else 10
    print("\n铸想 ForgeForm 环境自检")
    print("=" * 78)
    for level, item, value, hint in _results:
        print(f"{icon[level]} {item.ljust(width)}  {value}")
        if hint:
            print(f"          {' ' * width}  → {hint}")
    print("=" * 78)

    bad = [r for r in _results if r[0] == BAD]
    warn = [r for r in _results if r[0] == WARN]
    if bad:
        print(f"结论：还不能跑。{len(bad)} 项阻塞，按上面的 → 处理后重跑本脚本。")
        return 1
    if warn:
        print(f"结论：能跑。{len(warn)} 项提醒，不影响开工。")
        return 0
    print("结论：全部就绪。python app.py 然后打开 http://127.0.0.1:7860")
    return 0


def main() -> int:
    check_python()
    check_deps()
    cfg = check_config()
    paths = resolve_paths(cfg) if cfg else {}
    check_paths(paths)
    check_comfy_service()
    if cfg:
        check_models(cfg, paths)
    check_gpu()
    return report()


if __name__ == "__main__":
    sys.exit(main())
