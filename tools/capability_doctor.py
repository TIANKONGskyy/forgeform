# -*- coding: utf-8 -*-
"""能力就绪检查：一条命令告诉你"五项能力现在哪几项能用、缺什么"（2026-11-28）。

为什么需要
----------
端到端实测发现：用户点「绑骨骼 / 上色 / 精修 / 动画 / 多视图」失败时，
报错各不相同（缺脚本/缺权重/缺 CUDA 扩展/缺基础工程），日志里散落在不同位置。
这个脚本把每项能力的前置条件**逐条列出来**，缺哪条、怎么补，一眼能看到。

用法：
    python tools/capability_doctor.py
    python tools/capability_doctor.py --json

判定原则
--------
- 只看"能不能跑起来"的必要条件，不猜质量
- 每项给出：状态（ready / partial / blocked）、缺什么、补的命令
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMFY = Path(os.environ.get("FORGEFORM_COMFY_DIR", r"D:\ComfyUI-New"))
COMFY_VENV = Path(os.environ.get("FORGEFORM_COMFY_ENV", r"D:\comfy_venv312"))
RESEARCH = ROOT / "work" / "research"


def _exists(p: Path | str) -> bool:
    return Path(p).is_file()


def _comfy_url() -> str:
    raw = ""
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        raw = ((cfg.get("paths") or {}).get("comfyui") or "").strip()
    except Exception:
        pass
    if raw.startswith("http"):
        return raw.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('FORGEFORM_COMFY_PORT', '8188')}"


def _comfy_online() -> tuple[bool, str]:
    try:
        import requests
        r = requests.get(f"{_comfy_url()}/system_stats", timeout=5)
        return r.status_code == 200, _comfy_url()
    except Exception as e:
        return False, f"{_comfy_url()}（{type(e).__name__}）"


def _nodes(names: list[str]) -> tuple[bool, list[str]]:
    try:
        import requests
        oi = json.loads(requests.get(f"{_comfy_url()}/object_info", timeout=20).content)
        missing = [n for n in names if n not in oi]
        return not missing, missing
    except Exception:
        return False, names


def check_motion() -> dict:
    from tools.backends import resolve

    lib = resolve("motion/retarget_lib.py")
    baked_dir = RESEARCH / "motion" / "actions"
    baked = sorted(baked_dir.glob("*.blend")) if baked_dir.is_dir() else []
    blend = RESEARCH / "motion" / "人物_BVH步态_重定向.blend"
    if baked:
        return {"state": "ready", "detail": f"{len(baked)} 个预烘焙动作（52 骨）",
                "fix": ""}
    if lib and blend.is_file():
        return {"state": "ready", "detail": "现场重定向（基础骨架工程 + BVH）", "fix": ""}
    return {"state": "blocked", "detail": "既无预烘焙动作包，也无基础骨架工程",
            "fix": "放 5 个动作 .blend 到 work/research/motion/actions/，"
                   "或提供 人物_BVH步态_重定向.blend"}


def check_rig() -> dict:
    from tools.backends import resolve

    tmpl = resolve("rig/auto_rig.py")
    blender = Path(r"D:\tools\blender\blender-4.2.23-windows-x64\blender.exe")
    if tmpl and blender.is_file():
        return {"state": "ready", "detail": "模板骨架（纯 CPU，实测 53 s / 14 关节）", "fix": ""}
    bad = []
    if not tmpl:
        bad.append("缺 tools/backends/rig/auto_rig.py")
    if not blender.is_file():
        bad.append(f"缺 Blender：{blender}")
    return {"state": "blocked", "detail": "；".join(bad),
            "fix": "装 Blender 4.2（改 FORGEFORM_BLENDER 指向 blender.exe）"}


def check_paint() -> dict:
    online, url = _comfy_online()
    if not online:
        return {"state": "blocked", "detail": f"ComfyUI 离线：{url}",
                "fix": "先启动 ComfyUI"}
    ok, missing = _nodes(["LocalResearchLoadMesh", "Hy3D21CameraConfig",
                          "Hy3DMultiViewsGenerator", "Hy3DInPaint"])
    weights = COMFY / "models" / "diffusers" / "hunyuan3d-paintpbr"
    has_w = (weights / "model_index.json").is_file()
    if ok and has_w:
        return {"state": "partial", "detail": "节点与权重齐，但光栅化内核需匹配 torch 版本",
                "fix": "若报 custom_rasterizer_kernel DLL 错误，需要用与 torch 同版本的 CUDA 重编"}
    bad = []
    if not ok:
        bad.append("缺节点：" + ", ".join(missing))
    if not has_w:
        bad.append("缺权重：models/diffusers/hunyuan3d-paintpbr（约 6.4 GB）")
    return {"state": "blocked", "detail": "；".join(bad),
            "fix": "装 ComfyUI-Hunyuan3D-Paint 节点包 + 权重"}


def check_refine() -> dict:
    from tools.backends import resolve

    script = resolve("precision/smoke_direct3d.py")
    weights = RESEARCH / "precision" / "weights" / "Direct3D-V3"
    has_w = (weights / "shape_transformers_512" / "model.safetensors").is_file()
    has_src = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3").is_dir()
    if script and has_w and has_src:
        return {"state": "ready", "detail": "脚本 + 权重 + Direct3D 源码齐", "fix": ""}
    bad = []
    if not script:
        bad.append("缺 tools/backends/precision/smoke_direct3d.py")
    if not has_w:
        bad.append("缺权重 shape_transformers_512（4.6 GB）")
    if not has_src:
        bad.append("缺 Direct3D 源码与 third_party CUDA 扩展")
    return {"state": "blocked", "detail": "；".join(bad),
            "fix": "python tools/bootstrap_models.py --only refine（权重）+ 放 Direct3D 源码"}


def check_multiview() -> dict:
    online, url = _comfy_online()
    if not online:
        return {"state": "blocked", "detail": f"ComfyUI 离线：{url}", "fix": "先启动 ComfyUI"}
    ok, missing = _nodes(["[Comfy3D] Hunyuan3D V2 ShapeGen MV"])
    mv_weights = COMFY / "custom_nodes" / "ComfyUI-3D-Pack-main" / "Checkpoints" / "Diffusers" / "tencent" / "Hunyuan3D-2mv"
    if ok:
        return {"state": "ready" if mv_weights.is_dir() else "partial",
                "detail": "2mv 节点在" + ("" if mv_weights.is_dir() else "，但缺 2mv 权重"),
                "fix": "" if mv_weights.is_dir() else "下载 Hunyuan3D-2mv 权重（约 4.7 GB）"}
    return {"state": "blocked", "detail": "缺 2mv 节点（3D-Pack）", "fix": "装 ComfyUI-3D-Pack"}


CHECKS = {
    "骨骼绑定": check_rig,
    "动画": check_motion,
    "贴图 PBR": check_paint,
    "精修": check_refine,
    "多视图": check_multiview,
}

ICON = {"ready": "✓", "partial": "◐", "blocked": "✗"}


def main() -> int:
    as_json = "--json" in sys.argv
    rows = {}
    for name, fn in CHECKS.items():
        try:
            rows[name] = fn()
        except Exception as e:
            rows[name] = {"state": "blocked", "detail": f"检查本身出错：{type(e).__name__}: {e}", "fix": ""}
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    ready = sum(1 for r in rows.values() if r["state"] == "ready")
    for name, r in rows.items():
        print(f"  {ICON.get(r['state'], '?')} {name:<8} {r['detail']}")
        if r["state"] != "ready" and r["fix"]:
            print(f"      → {r['fix']}")
    print(f"\n  {ready}/{len(rows)} 完全就绪")
    return 0 if ready == len(rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
