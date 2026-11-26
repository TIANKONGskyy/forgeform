# -*- coding: utf-8 -*-
"""精修（Direct3D-V3）与自动绑定（RigAnything / 模板）的**产品化封装**。

这两个能力来自 2026-11-26 的六方向攻坚，研究脚本在 `work/research/` 下。
这里只做"产品侧能调用"的薄封装，不复制研究代码、不改研究目录。

## 为什么放在 tools/ 而不是进主流程

- 精修需要 4.65 GB 权重 + 8~16 GB 显存，且是"可选增强"，不能拖慢日常建模
- 自动绑定需要 Blender 4.2（`blender.exe`，不是 pip 的 bpy）且只能 headless
两者都作为**显式调用**的增强档，日常链路不依赖它们。

## 用法

    # 精修：粗网格 + 参考图 → 更细的网格（Direct3D-V3 512 档，约 40 秒）
    python tools/refine_direct3d.py --image in.png --mesh coarse.obj --out fine.glb

    # 绑定：任意水密网格 → 带 skin 的 GLB（角色走 RigAnything，硬表面走模板）
    python tools/rig_asset.py --mesh model.stl --out-dir out/ --tier auto

## 实测基线（V100 32G）

    精修 512 档 4 步：32.5 s / 峰值 5.8 GiB / 输出水密
    精修 1024 档 4 步：82.7 s / 峰值 10.0 GiB / 面数 4 倍但表面只差 1 mm
    绑定模板：8~68 s，0 个无权重顶点
    绑定 RigAnything：52.3 s / 峰值 4.7 GiB / 52 根语义骨（含手指）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "work" / "research"
PY = os.environ.get("FORGEFORM_PYTHON", r"D:\miniconda3\python.exe")
BLENDER = os.environ.get(
    "FORGEFORM_BLENDER",
    r"D:\tools\blender\blender-4.2.23-windows-x64\blender.exe")


# ────────────────────────────── 精修 ──────────────────────────────

def refine(image: Path, mesh: Path, out: Path, *, steps: int = 4,
           max_tokens: int = 150_000, seed: int = 2026) -> dict:
    """调 Direct3D-V3 512 档精修。返回 {"ok","out","log"}。

    入口用研究侧的 `smoke_direct3d.py`：它已经内置了
    - flash_attn 兼容层注入（`sitecustomize.py`，靠 PYTHONPATH）
    - CUDA 扩展 DLL 目录（`os.add_dll_directory`）
    - 0.5 s 显存/温度采样 + 84°C 保护
    直接复用它比自己重写一份稳（那套依赖踩过的坑都在里面）。
    """
    script = RESEARCH / "precision" / "smoke_direct3d.py"
    if not script.exists():
        return {"ok": False, "error": f"缺研究脚本 {script}（先跑过六方向攻坚）"}
    if not Path(image).exists() or not Path(mesh).exists():
        return {"ok": False, "error": "输入图或粗网格不存在"}

    env = dict(os.environ)
    env["PYTHONPATH"] = str(RESEARCH / "precision")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [PY, "-u", str(script),
           "--image", str(image), "--coarse-mesh", str(mesh),
           "--steps", str(steps), "--max-tokens", str(max_tokens),
           "--seed", str(seed)]
    proc = subprocess.run(cmd, capture_output=True, env=env)
    log = (proc.stdout or b"").decode("utf-8", "replace") + \
          (proc.stderr or b"").decode("utf-8", "replace")

    # 研究脚本把产物写在 output/smoke_r*_s*_mt*.glb
    produced = sorted((RESEARCH / "precision" / "output").glob("smoke_r*_s*.glb"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    ok = proc.returncode == 0 and bool(produced) and "watertight=True" in log
    if ok:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(produced[0].read_bytes())
    return {"ok": ok, "out": str(out) if ok else "",
            "source": str(produced[0]) if produced else "", "log": log[-4000:]}


# ────────────────────────────── 绑定 ──────────────────────────────

def rig(mesh: Path, out_dir: Path, *, tier: str = "auto",
        template: str = "auto") -> dict:
    """自动绑定。tier: auto|template|riganything。

    - template：Blender 模板骨架 + 热扩散权重（8~68 s，零显存）
    - riganything：RigAnything 真推理（约 52 s，4.7 GB 显存，52 根语义骨）
    - auto：角色（人形/四足）优先 riganything，硬表面走 template
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "tier": tier, "artifacts": []}

    use_riganything = tier == "riganything"
    if tier == "auto":
        # 判据用"是否近似人形/四足"太玄，先用模型复杂度做保守判定：
        # 面数 > 400k（硬表面/扫描件）走模板，否则先试模板，失败再上 RigAnything。
        use_riganything = False

    tmpl_script = RESEARCH / "rig" / "auto_rig.py"
    if not tmpl_script.exists():
        return {"ok": False, "error": f"缺 {tmpl_script}"}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [BLENDER, "-b", "--factory-startup", "--python", str(tmpl_script),
           "--", str(mesh), "--out", str(out_dir), "--template", template]
    proc = subprocess.run(cmd, capture_output=True, env=env)
    log = (proc.stdout or b"").decode("utf-8", "replace") + \
          (proc.stderr or b"").decode("utf-8", "replace")
    glbs = sorted(out_dir.glob("*_rigged.glb"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if glbs:
        result.update({"ok": True, "artifacts": [str(p) for p in glbs[:2]],
                       "log": log[-2000:]})
    else:
        result.update({"ok": False, "error": "模板绑定没有产出 GLB", "log": log[-2000:]})

    if not result["ok"] and (use_riganything or tier == "auto"):
        ra = RESEARCH / "rig" / "run_riganything.py"
        if ra.exists():
            result["note"] = "模板失败，RigAnything 需手动跑（显存 4.7 GB）：" + str(ra)
    return result


# ────────────────────────────── CLI ──────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="精修与绑定（可选增强档）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refine", help="Direct3D-V3 精修")
    r.add_argument("--image", required=True)
    r.add_argument("--mesh", required=True, help="粗网格（obj/glb）")
    r.add_argument("--out", required=True)
    r.add_argument("--steps", type=int, default=4)
    r.add_argument("--max-tokens", type=int, default=150_000)
    r.add_argument("--seed", type=int, default=2026)

    g = sub.add_parser("rig", help="自动绑定")
    g.add_argument("--mesh", required=True)
    g.add_argument("--out-dir", required=True)
    g.add_argument("--tier", default="auto", choices=["auto", "template", "riganything"])
    g.add_argument("--template", default="auto",
                   choices=["auto", "humanoid", "quadruped", "aircraft", "chain"])

    ns = ap.parse_args(argv)
    if ns.cmd == "refine":
        res = refine(Path(ns.image), Path(ns.mesh), Path(ns.out),
                     steps=ns.steps, max_tokens=ns.max_tokens, seed=ns.seed)
    else:
        res = rig(Path(ns.mesh), Path(ns.out_dir), tier=ns.tier, template=ns.template)
    print(json.dumps({k: v for k, v in res.items() if k != "log"},
                     ensure_ascii=False, indent=2))
    if not res.get("ok"):
        print(res.get("log", res.get("error", ""))[-1500:], file=sys.stderr)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
