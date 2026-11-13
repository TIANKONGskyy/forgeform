# -*- coding: utf-8 -*-
"""下载多视图重建需要的权重（Hunyuan3D 2mv + Zero123Plus）。

    python tools/fetch_multiview.py            # 看看要下什么、多大
    python tools/fetch_multiview.py --run      # 真下

## 为什么需要多视图

单图重建时，模型只能"猜"看不见的部分。Meshy / Tripo / 混元都走
「单图 → 4~6 个视角 → 重建」，因为多视图给了模型更多约束，
几何细节和前后关系明显更准。

这台 ComfyUI 的节点已经齐了（`[Comfy3D] Zero123Plus Diffusion Model`、
`[Comfy3D] Hunyuan3D V2 ShapeGen MV`），**只缺权重**。

## 下到哪

ComfyUI-3D-Pack 按 `models/checkpoints/<ModelName>/<subfolder>` 找。
具体映射见 `custom_nodes/ComfyUI-3D-Pack-main/nodes.py` 里的 `MODE_MAP`。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMFY = Path(r"D:\ComfyUI-New")

# (说明, HF repo, 仓库内路径, 本地目标)
PLAN = [
    # ★ 注意：fetch_model.py 会在 --dest 下**按 repo 内路径**建目录。
    #   所以 dest 只能给到 checkpoints，不能预先拼进 hunyuan3d-dit-v2-mv-fast
    #   —— 否则会变成 checkpoints/Hunyuan3D-2mv/hunyuan3d-dit-v2-mv-fast/hunyuan3d-dit-v2-mv-fast/
    #   （实测这么错过一次，下了 4 GB 分片却没合并）。
    ("Hunyuan3D-2mv（多视图重建，Fast 版）", "tencent/Hunyuan3D-2mv",
     "hunyuan3d-dit-v2-mv-fast/model.fp16.safetensors",
     COMFY / "models" / "checkpoints" / "hunyuan3d-dit-v2-mv-fast" / "model.fp16.safetensors"),
    ("Hunyuan3D-2mv 配置", "tencent/Hunyuan3D-2mv",
     "hunyuan3d-dit-v2-mv-fast/config.yaml",
     COMFY / "models" / "checkpoints" / "hunyuan3d-dit-v2-mv-fast" / "config.yaml"),
    ("VAE（多视图共用）", "tencent/Hunyuan3D-2mv",
     "hunyuan3d-vae-v2-mv/model.fp16.safetensors",
     COMFY / "models" / "checkpoints" / "hunyuan3d-vae-v2-mv" / "model.fp16.safetensors"),
    ("Zero123Plus（单图 → 多视图）", "sudo-ai/zero123plus-v1.2",
     "diffusion_pytorch_model.safetensors",
     COMFY / "models" / "checkpoints" / "Zero123Plus" / "diffusion_pytorch_model.safetensors"),
]


def check() -> list[dict]:
    out = []
    for label, repo, src, dst in PLAN:
        exists = dst.exists()
        size = dst.stat().st_size / 1024 ** 3 if exists else 0
        out.append({"label": label, "repo": repo, "path": src,
                    "dst": str(dst), "exists": exists, "gb": round(size, 2)})
    return out


def run(only_missing: bool = True) -> int:
    for item in check():
        if only_missing and item["exists"]:
            print(f"  ✓ 已有 {item['label']}")
            continue
        print(f"\n↓ {item['label']}")
        print(f"  {item['repo']} :: {item['path']}")
        dst = Path(item["dst"])
        # dest 给 checkpoints 根（不是 dst.parent）—— 见上面 PLAN 里的注释
        dest_root = COMFY / "models" / "checkpoints"
        dest_root.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([
            sys.executable, str(ROOT / "tools" / "fetch_model.py"),
            "--repo", item["repo"],
            "--dest", str(dest_root),
            "--include", "^" + item["path"].replace(".", r"\.") + "$",
            "--threads", "8",
        ])
        if r.returncode != 0:
            print(f"  ✗ 失败（{r.returncode}）")
        elif dst.exists():
            print(f"  ✓ {dst.stat().st_size / 1024 ** 3:.2f} GB → {dst}")
        else:
            print("  ⚠ 下完了但目标文件不在，检查文件名")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    items = check()
    total = sum(4.59 if "2mv（" in i["label"] else (1.4 if "VAE" in i["label"] else 3.0)
                for i in items if not i["exists"])
    print("=" * 66)
    print("多视图重建权重")
    print("=" * 66)
    for i in items:
        mark = "✓" if i["exists"] else "·"
        print(f"  {mark} {i['label']:<34} {i['dst']}")
    print(f"\n  预计还需要下载约 {total:.1f} GB")

    if args.run:
        print()
        return run()
    print("\n  加 --run 开始下载")
    return 0


if __name__ == "__main__":
    sys.exit(main())
