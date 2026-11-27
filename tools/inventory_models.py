# -*- coding: utf-8 -*-
"""盘点本机所有"铸想会用到的本地模型"，输出清单（2026-11-27）。

只读：不删任何东西，只统计。
用法：python tools/inventory_models.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(r"D:\ForgeForm-oss")
CKPT_EXT = {".safetensors", ".ckpt", ".bin", ".pt", ".pth", ".gguf", ".onnx", ".npz"}
SKIP_DIRS = {"venv", ".venv", "venv_riganything", "node_modules", ".git", "__pycache__"}

# (分区名, 路径, 说明)
TARGETS = [
    ("ComfyUI 本体与权重", r"D:\ComfyUI-New", "出图 / 建模 / 贴图 / 视频的后端"),
    ("ComfyUI Python 环境", r"D:\comfy312", "ComfyUI 专用 venv（uv 建）"),
    ("铸想模型目录", str(ROOT / "models"), "config.json 里登记的本地 3D 权重"),
    ("铸想研究权重", str(ROOT / "work" / "research"), "Direct3D-V3 / RigAnything / Paint 等"),
    ("Hy3Dgen 权重缓存", os.path.expandvars(r"%USERPROFILE%\.cache\hy3dgen"), "hy3dgen 自动下载"),
    ("HuggingFace 缓存", os.path.expandvars(r"%USERPROFILE%\.cache\huggingface"), "transformers 等"),
    ("Ollama 模型库", os.path.expandvars(r"%USERPROFILE%\.ollama\models"), "本地对话 / 向量模型"),
    ("Ollama 程序目录", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama"), "ollama.exe"),
    ("Strata 服务", r"D:\tools\strata", "Qwen3.8 125B（旧对话模型）"),
    ("铸想输出", str(ROOT / "outputs"), "产物（不是模型，但占地方）"),
]


def dir_size(p: Path) -> tuple[int, int]:
    """返回 (总字节, 文件数)。"""
    total = files = 0
    for dirpath, dirnames, filenames in os.walk(p, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
                files += 1
            except OSError:
                pass
    return total, files


def big_files(p: Path, min_mb: int = 200, limit: int = 40) -> list[dict]:
    out = []
    for dirpath, dirnames, filenames in os.walk(p, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if Path(f).suffix.lower() not in CKPT_EXT:
                continue
            fp = Path(dirpath) / f
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            if sz >= min_mb * 1024 * 1024:
                out.append({"path": str(fp), "mb": round(sz / 1024 / 1024)})
    out.sort(key=lambda r: -r["mb"])
    return out[:limit]


def ollama_models() -> list[dict]:
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=8).read())
        return [{"name": m["name"], "size_gb": round(m["size"] / 1024 ** 3, 2),
                 "params": (m.get("details") or {}).get("parameter_size", "?")}
                for m in d.get("models", [])]
    except Exception:
        return []


def human(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ns = ap.parse_args()

    rows, total = [], 0
    for name, path, desc in TARGETS:
        p = Path(path)
        if not p.exists():
            rows.append({"name": name, "path": path, "exists": False, "desc": desc})
            continue
        size, files = dir_size(p)
        total += size
        rows.append({"name": name, "path": path, "exists": True, "desc": desc,
                     "bytes": size, "files": files,
                     "checkpoints": big_files(p)})

    data = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "total_bytes": total,
            "total_human": human(total), "targets": rows,
            "ollama_models": ollama_models(),
            "disk": {}}
    for d in ("C:", "D:"):
        try:
            u = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-PSDrive {d[0]}).Free"], text=True, timeout=20).strip()
            data["disk"][d] = human(int(u))
        except Exception:
            pass

    print(f"  盘点时间 {data['time']}")
    print(f"  合计 {data['total_human']}")
    for r in rows:
        if not r["exists"]:
            print(f"  -  {r['name']:<16} 不存在 ({r['path']})")
            continue
        print(f"  ✓  {r['name']:<16} {human(r['bytes']):>9}  {r['files']:>5} 文件  {r['path']}")
        for c in (r.get("checkpoints") or [])[:6]:
            print(f"        · {c['mb']:>6} MB  {c['path']}")
    if data["ollama_models"]:
        print("  Ollama 模型：")
        for m in data["ollama_models"]:
            print(f"        · {m['name']:<28} {m['params']:>8}  {m['size_gb']} GB")

    out = Path(ns.json) if ns.json else ROOT / "docs" / "LOCAL-MODELS-INVENTORY.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  清单已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
