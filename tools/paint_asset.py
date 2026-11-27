# -*- coding: utf-8 -*-
"""贴图（PBR 上色）的产品入口 —— 2026-11-27 补齐五项能力里最后缺的那个。

## 为什么需要它

`work/research/texture/tools/run_plane_paint.py` 是研究脚本：输入/输出目录写死在
隔离实例（:8197）的路径上，要手改才能用。这个模块把它收成产品可调用的函数 + CLI。

## 两条路线（2026-11-27 实测，plane_21.stl 406k 面）

| 路线 | 视角/步数/atlas | 耗时 | 峰值显存 | 质量 |
|---|---|---|---|---|
| **完整档**（默认） | 6 视角 / 12 步 / 1024² | **1236 s** | 10.7 GB | 涂装正确、接缝可接受 |
| **快速档** `--fast` | 4 视角 / 8 步 / 1024² | **458 s** | 9.7 GB | 只适合预览：atlas 明显发噪 |

时间大头不是扩散采样，是 **UV 展开 + 烘焙 + 接缝 inpaint**；
所以 `--fast` 省的是视角数和步数，不是那三步本身 —— 想再快只能减 atlas 或面数。

## 依赖（本机已配好，换机器要重做）

主实例 ComfyUI 需要：
1. `ComfyUI-Hunyuan3D-Paint` 节点包（提供 `Hy3D21CameraConfig` / `Hy3DMultiViewsGenerator` 等）
2. `models/diffusers/hunyuan3d-paintpbr` 必须是**完整 diffusers 目录**
   （`model_index.json` + `unet/` `vae/` `text_encoder/` …），平铺的 `unet.bin` 不行
3. `tokenizer/` 里 `vocab.json` 和 `merges.txt` 必须同源（缺 merges 会报
   `vocab and merges must be both from memory or both filenames`）
4. `paint_model` 枚举值按节点包版本写（本机是 `hunyuan3d-paintpbr`）

这四条都是 2026-11-27 验证时踩出来的，`_preflight()` 会先自检再提交。

## 用法

    from tools.paint_asset import paint
    r = paint("model.stl", "ref.png", "out.glb", texture=2048, view=512, steps=20)
    # r = {"ok": True, "out": ..., "seconds": ..., "peak_vram_mib": ..., "textures": 2}

    python tools/paint_asset.py --mesh model.stl --image ref.png --out out.glb --fast
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
COMFY = Path(os.environ.get("FORGEFORM_COMFYUI", r"D:\ComfyUI-New"))
PY = os.environ.get("FORGEFORM_PYTHON", r"D:\miniconda3\python.exe")

# 节点包版本的枚举值：本机 ComfyUI-Hunyuan3D-Paint 只认这个
PAINT_MODEL = os.environ.get("FORGEFORM_PAINT_MODEL", "hunyuan3d-paintpbr")


# ─────────────────────────── 环境自检 ───────────────────────────

def _preflight(port: int) -> dict:
    """提交前把踩过的四个坑一次性查掉，别等 20 分钟后才报错。"""
    problems = []
    url = f"http://127.0.0.1:{port}"
    try:
        info = json.loads(requests.get(f"{url}/object_info", timeout=30).content)
    except Exception as e:
        return {"ok": False, "problems": [f"ComfyUI 不可达（: {port}）：{e}"]}

    need = ["Hy3D21CameraConfig", "Hy3DMultiViewsGenerator", "Hy3DBakeMultiViews", "Hy3DInPaint"]
    for n in need:
        if n not in info:
            problems.append(f"缺节点 {n}（装 ComfyUI-Hunyuan3D-Paint 节点包）")
    if "LocalResearchLoadMesh" not in info:
        problems.append("缺节点 LocalResearchLoadMesh（tools 里的研究节点）")

    weights = COMFY / "models" / "diffusers" / "hunyuan3d-paintpbr"
    if not (weights / "model_index.json").is_file():
        problems.append(f"贴图权重不是完整 diffusers 目录：{weights}"
                        "（需要 model_index.json + unet/ vae/ text_encoder/）")
    tok = weights / "tokenizer"
    if (tok / "vocab.json").is_file() and not (tok / "merges.txt").is_file():
        problems.append(f"{tok} 缺 merges.txt（vocab/merges 必须同源）")
    return {"ok": not problems, "problems": problems}


# ─────────────────────────── 工作流 ───────────────────────────

def build_workflow(mesh: str, image_name: str, out_name: str, *, texture: int = 2048,
                   view: int = 512, steps: int = 20, seed: int = 42,
                   ortho: float = 1.1, fast: bool = False,
                   inpaint_steps: int = 10) -> dict:
    """四视图上色 → 烘焙 → inpaint（接缝修补 + 出网格）。

    ★ 这里没有真正的"跳过 inpaint"档：`Hy3DInPaint` 是**唯一**输出 TRIMESH/GLB 的节点，
      砍掉它就没有产物。所以 fast 档只调三样：
        ① 只拍 4 个水平视角（去掉顶/底两张）
        ② 视图步数降到 8
        ③ atlas 上限 1024
    实测（2026-11-27）：完整档 1236 s（texture 1024 / view 512 / steps 12，6 视角）。
    """
    azimuths = "0,90,180,270" if fast else "0,90,180,270,0,180"
    elevations = "0,0,0,0" if fast else "0,0,0,0,90,-90"
    weights = "1,0.4,0.8,0.4" if fast else "1,0.2,0.5,0.2,0.05,0.05"
    wf = {
        "1": {"class_type": "LocalResearchLoadMesh", "inputs": {"path": mesh}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "3": {"class_type": "Hy3D21CameraConfig",
              "inputs": {"camera_azimuths": azimuths,
                         "camera_elevations": elevations,
                         "view_weights": weights,
                         "ortho_scale": ortho}},
        "4": {"class_type": "Hy3DMultiViewsGenerator",
              "inputs": {"trimesh": ["1", 0], "camera_config": ["3", 0],
                         "paint_model": PAINT_MODEL,
                         "view_size": view, "image": ["2", 0], "steps": steps,
                         "guidance_scale": 3.0, "texture_size": texture,
                         "unwrap_mesh": True, "seed": seed,
                         "diffusion_backend": "pytorch"}},
        "5": {"class_type": "Hy3DBakeMultiViews",
              "inputs": {"pipeline": ["4", 0], "camera_config": ["4", 5],
                         "albedo": ["4", 1], "mr": ["4", 2]}},
        "6": {"class_type": "Hy3DInPaint",
              "inputs": {"pipeline": ["5", 0], "albedo": ["5", 1], "albedo_mask": ["5", 2],
                         "mr": ["5", 3], "mr_mask": ["5", 4],
                         "output_mesh_name": out_name}},
    }
    return {"prompt": wf, "negative_prompt": ""}


_NODE_CACHE: dict = {}


def _has_node(name: str, port: int = 8188) -> bool:
    if port not in _NODE_CACHE:
        try:
            _NODE_CACHE[port] = set(json.loads(
                requests.get(f"http://127.0.0.1:{port}/object_info", timeout=30).content))
        except Exception:
            _NODE_CACHE[port] = set()
    return name in _NODE_CACHE[port]


# ─────────────────────────── 主流程 ───────────────────────────

def paint(mesh: str | Path, image: str | Path, out: str | Path, *,
          texture: int = 2048, view: int = 512, steps: int = 20, seed: int = 42,
          ortho: float = 1.1, fast: bool = False, port: int = 8188,
          timeout: int = 3600, verbose: bool = True) -> dict:
    """给网格上 PBR 贴图，返回 {"ok","out","seconds","peak_vram_mib","textures"}。

    失败时返回 {"ok": False, "error": ...}，不抛（和 pipeline 其他能力一致）。
    """
    mesh_p, img_p, out_p = Path(mesh), Path(image), Path(out)
    if not mesh_p.is_file():
        return {"ok": False, "error": f"网格不存在: {mesh_p}"}
    if not img_p.is_file():
        return {"ok": False, "error": f"参考图不存在: {img_p}"}
    if view < 512:
        return {"ok": False, "error": f"view 最小 512（节点限制），收到 {view}"}

    pre = _preflight(port)
    if not pre["ok"]:
        return {"ok": False, "error": "环境自检未通过", "problems": pre["problems"]}

    url = f"http://127.0.0.1:{port}"
    try:
        q = requests.get(f"{url}/queue", timeout=10).json()
        if q.get("queue_running") or q.get("queue_pending"):
            return {"ok": False, "error": "ComfyUI 队列非空，等它跑完再上色"}
    except Exception as e:
        return {"ok": False, "error": f"ComfyUI 队列查询失败: {e}"}

    # 参考图必须落在 ComfyUI 的 input 目录
    cinput = COMFY / "input"
    cinput.mkdir(parents=True, exist_ok=True)
    image_name = f"paint_{uuid.uuid4().hex[:8]}_{img_p.name}"
    shutil.copy2(img_p, cinput / image_name)

    out_name = f"paint_{uuid.uuid4().hex[:10]}.glb"
    wf = build_workflow(str(mesh_p.resolve()), image_name, out_name,
                        texture=texture, view=view, steps=steps, seed=seed,
                        ortho=ortho, fast=fast)
    t0 = time.time()
    peak = 0
    try:
        r = requests.post(f"{url}/prompt", json=wf, timeout=60)
        if r.status_code != 200:
            return {"ok": False, "error": f"提交失败 {r.status_code}: {r.text[:400]}"}
        pid = r.json().get("prompt_id")
        if verbose:
            print(f"  已提交 {pid[:8]}（texture={texture} view={view} steps={steps} "
                  f"fast={fast}）", flush=True)

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(10)
            try:
                peak = max(peak, int(subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    text=True, timeout=10).strip().splitlines()[0]))
            except Exception:
                pass
            try:
                h = requests.get(f"{url}/history/{pid}", timeout=15).json()
            except Exception:
                continue
            if pid in h:
                st = h[pid].get("status", {})
                if st.get("status_str") == "error":
                    msg = ""
                    for m in (st.get("messages") or []):
                        if m[0] == "execution_error":
                            msg = str(m[1].get("exception_message"))[:300]
                    return {"ok": False, "error": f"上色失败: {msg}", "seconds": round(time.time() - t0, 1)}
                break
        else:
            return {"ok": False, "error": f"超时 {timeout}s"}

        source = COMFY / "output" / out_name
        if not source.is_file():
            cand = sorted((COMFY / "output").glob("*paint*.glb"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            source = cand[0] if cand else None
        if not source or not source.is_file():
            return {"ok": False, "error": "任务完成但没找到 GLB 产物"}
        out_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, out_p)

        info = _inspect_glb(out_p)
        if not info.get("textures"):
            return {"ok": False, "error": "产物没有贴图", "inspect": info}
        return {"ok": True, "out": str(out_p), "seconds": round(time.time() - t0, 1),
                "peak_vram_mib": peak, "textures": info["textures"],
                "images": info["images"], "has_uv": info["has_uv"],
                "pbr": info["pbr"], "bytes": out_p.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            (cinput / image_name).unlink(missing_ok=True)
        except Exception:
            pass


def _inspect_glb(path: Path) -> dict:
    """解 GLB 二进制确认贴图真的在（不信文件大小）。"""
    out = {"textures": 0, "images": 0, "has_uv": False, "pbr": False}
    with path.open("rb") as f:
        if f.read(4) != b"glTF":
            return out
        f.read(8)
        length, ctype = struct.unpack("<II", f.read(8))
        if ctype != 0x4E4F534A or length > 128 * 1024 * 1024:
            return out
        d = json.loads(f.read(length))
    out["textures"] = len(d.get("textures") or [])
    out["images"] = len(d.get("images") or [])
    prims = [p for m in (d.get("meshes") or []) for p in (m.get("primitives") or [])]
    out["has_uv"] = any("TEXCOORD_0" in (p.get("attributes") or {}) for p in prims)
    for m in (d.get("materials") or []):
        pbr = m.get("pbrMetallicRoughness") or {}
        if pbr.get("baseColorTexture") or pbr.get("metallicRoughnessTexture"):
            out["pbr"] = True
    return out


# ─────────────────────────── CLI ───────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给网格上 PBR 贴图（Hunyuan3D Paint PBR）")
    ap.add_argument("--mesh", required=True, help=".stl / .glb / .obj")
    ap.add_argument("--image", required=True, help="参考图（白底原图最好）")
    ap.add_argument("--out", required=True, help="输出 .glb")
    ap.add_argument("--texture", type=int, default=2048, help="atlas 边长，512~4096")
    ap.add_argument("--view", type=int, default=512, help="视图分辨率，最小 512")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ortho", type=float, default=1.1)
    ap.add_argument("--fast", action="store_true", help="跳过接缝 inpaint（更快）")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--timeout", type=int, default=3600)
    ns = ap.parse_args(argv)

    t0 = time.time()
    res = paint(ns.mesh, ns.image, ns.out, texture=ns.texture, view=ns.view,
                steps=ns.steps, seed=ns.seed, ortho=ns.ortho, fast=ns.fast,
                port=ns.port, timeout=ns.timeout)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
