# -*- coding: utf-8 -*-
"""本地模型的查看 / 安装 / 切换 / 删除（2026-11-28）。

背景
----
之前"装模型"只有一个粗粒度的 `bootstrap_models.py --only <套件>`：
只能整套装，装完也不知道哪个模型真的就位；想删掉某个不用的模型更没接口。

这个模块把模型当成**独立条目**管起来：

    list_models()            每个模型：键、名字、类型、体积、是否就位、权重路径
    install(key) / delete(key)  单个装、单个删
    switch(kind, key)        设置默认用的模型（写 config.models.active）
    usage()                  已占磁盘、可回收空间

约定
----
- 不猜路径：每个模型在 config.models.{image,3d} 里有自己的 `path`/`checkpoint` 字段
- 删除只删"这个模型自己的权重文件"，不碰 ComfyUI 公共 VAE/编码器（那些是共享的）
- 切换只写配置，不动权重
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMFY = Path(os.environ.get("FORGEFORM_COMFY_DIR", r"D:\ComfyUI-New"))
HY3DGEN = Path(os.environ.get("FORGEFORM_HY3DGEN_DIR", r"D:\models\hy3dgen"))

# 每个模型的"重量级权重"在哪（相对 ComfyUI/models 或 hy3dgen 根）
# 只列大文件；共享的 vae/clip 不放进来，避免删一个模型把别的模型搞坏
_PATHS = {
    ("image", "z_image"): ["diffusion_models/z_image_turbo_int8_convrot.safetensors"],
    ("image", "flux"): ["diffusion_models/flux1-dev-fp8.safetensors"],
    ("image", "flux_klein"): ["diffusion_models/flux-2-klein-4b.safetensors"],
    ("image", "flux2_dev"): ["diffusion_models/flux2-dev-Q4_K_M.gguf"],
    ("image", "qwen21_ref"): ["diffusion_models/qwen_image_fp8_hq.safetensors"],
}


def _cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))


def _save(cfg: dict) -> None:
    (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _slot(cfg: dict, kind: str, key: str) -> dict:
    return (((cfg.get("models") or {}).get(kind) or {}).get(key) or {})


def _gb(p: Path) -> float:
    try:
        return round(p.stat().st_size / 1024 ** 3, 2)
    except Exception:
        return 0.0


def _weight_files(cfg: dict, kind: str, key: str) -> list[Path]:
    """这个模型自己占的权重文件。"""
    out: list[Path] = []
    for rel in _PATHS.get((kind, key), []):
        p = COMFY / "models" / rel
        if p.is_file():
            out.append(p)

    # 配置里自带 checkpoint（用户自己加的模型、或换过文件名）也要认，
    # 否则「列表里显示已装、删除却说找不到文件」（2026-11-28 实测）
    slot0 = ((cfg.get("models") or {}).get(kind) or {}).get(key) or {}
    ckpt = str(slot0.get("checkpoint") or "").strip() if isinstance(slot0, dict) else ""
    if ckpt:
        for sub in ("diffusion_models", "unet", "checkpoints"):
            cand = COMFY / "models" / sub / ckpt
            if cand.is_file() and cand not in out:
                out.append(cand)
    if kind == "3d":
        # hy3dgen 直连权重：整个模型目录都算它的
        slot = _slot(cfg, kind, key)
        raw = slot.get("path")
        if raw:
            d = Path(raw)
            # 只认 hy3dgen 根下的目录，避免误删用户自己的模型
            try:
                if d.is_dir() and HY3DGEN in d.parents:
                    out.append(d)
            except Exception:
                pass
        if key == "hunyuan_mini":
            d = HY3DGEN / "hunyuan3d-2mini"
            if d.is_dir() and d not in out:
                out.append(d)
    return out


def _size_of(paths: list[Path]) -> float:
    total = 0
    for p in paths:
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
    return round(total / 1024 ** 3, 2)


def list_models() -> list[dict]:
    cfg = _cfg()
    rows: list[dict] = []
    for kind in ("image", "3d"):
        for key, slot in ((cfg.get("models") or {}).get(kind) or {}).items():
            if not isinstance(slot, dict):
                continue
            files = _weight_files(cfg, kind, key)
            rows.append({
                "key": key,
                "kind": kind,
                "name": slot.get("name") or key,
                "backend": slot.get("backend") or "",
                "installed": bool(files),
                "size_gb": _size_of(files) if files else float(slot.get("size_gb") or 0),
                "paths": [str(p) for p in files],
                "active": (self_active(kind) == key),
            })
    return rows


def active(kind: str) -> str:
    cfg = _cfg()
    return str((((cfg.get("models") or {}).get("active") or {}).get(kind)) or "")


def self_active(kind: str) -> str:
    try:
        return active(kind)
    except Exception:
        return ""


def switch(kind: str, key: str) -> dict:
    """把默认模型切成 key。只写配置，不动权重。"""
    cfg = _cfg()
    if not isinstance((((cfg.get("models") or {}).get(kind)) or {}).get(key), dict):
        raise KeyError(f"没有这个模型：{kind}/{key}")
    models = cfg.setdefault("models", {})
    models.setdefault("active", {})[kind] = key
    _save(cfg)
    return {"ok": True, "kind": kind, "key": key}


def delete(kind: str, key: str) -> dict:
    """删掉这个模型自己的权重（共享 VAE/编码器不动）。"""
    cfg = _cfg()
    files = _weight_files(cfg, kind, key)
    if not files:
        return {"ok": False, "error": "没有找到这个模型的权重文件"}
    freed = _size_of(files)
    removed = []
    for p in files:
        try:
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p, ignore_errors=True)
            removed.append(str(p))
        except Exception as e:
            return {"ok": False, "error": f"删不掉 {p.name}: {e}", "removed": removed}
    # 切到别的可用模型，避免 active 指向已删的
    if active(kind) == key:
        models = cfg.setdefault("models", {})
        remaining = [k for k in ((cfg.get("models") or {}).get(kind) or {})
                     if k != key and _weight_files(cfg, kind, k)]
        if remaining:
            models.setdefault("active", {})[kind] = remaining[0]
        else:
            (models.get("active") or {}).pop(kind, None)
        _save(cfg)
    return {"ok": True, "freed_gb": round(freed, 2), "removed": removed}


def usage() -> dict:
    rows = list_models()
    installed = [r for r in rows if r["installed"]]
    return {
        "installed": len(installed),
        "total": len(rows),
        "disk_gb": round(sum(r["size_gb"] for r in installed), 2),
        "free_gb": round(shutil.disk_usage(str(ROOT)).free / 1024 ** 3, 1),
    }
