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
    out = Path(out)                     # 允许调用方传 str（CLI 与脚本都会这么传）
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
        # ★ 2026-11-27：精修产物天然带一堆碎壳（实测 plane_21: 713 个连通块，
        #   712 个是 <100 面的碎块）。不过滤直接进切片器就是一堆浮尘，
        #   所以这里自动过一遍小壳过滤 —— 与 Pipeline._drop_debris 同一套判据。
        debris = {}
        try:
            sys.path.insert(0, str(ROOT))
            from backend.pipeline import Pipeline

            class _Bare(Pipeline):        # 只要 _drop_debris，不建配置/不连服务
                def __init__(self):
                    pass

                def _log(self, *a, **k):
                    pass

            debris = _Bare()._drop_debris(str(out))
        except Exception as e:
            debris = {"error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "out": str(out), "debris": debris,
                "source": str(produced[0]) if produced else "", "log": log[-2000:]}
    return {"ok": ok, "out": str(out) if ok else "",
            "source": str(produced[0]) if produced else "", "log": log[-4000:]}


# ────────────────────────────── 绑定 ──────────────────────────────

def _run_riganything(mesh: Path, out_dir: Path, result: dict) -> dict:
    """跑 RigAnything 真推理（研究侧脚本），产出 52 根语义骨的 GLB。

    实测（V100 32G，human_body.stl 50k 顶点）：52 骨 / 52.3 s / 峰值 5,669 MiB / 撕裂 0.061%。
    顶点数 > 8k 面时必须简化，否则 PyTorch 吃到 27.5 GB 会 OOM —— 这里按面数自动加 --simplify。
    """
    script = RESEARCH / "rig" / "run_riganything.py"
    if not script.exists():
        return {**result, "error": f"缺 {script}（RigAnything 研究环境未部署）"}
    import trimesh
    faces = 0
    try:
        faces = int(len(trimesh.load(str(mesh), force="mesh").faces))
    except Exception:
        pass
    env = dict(os.environ)
    env.update(PYTHONPATH="", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    # ★ 2026-11-27 修：RigAnything 依赖 easydict / open3d，装在研究侧独立 venv 里；
    #   用 miniconda 的解释器跑会 `ModuleNotFoundError: No module named 'easydict'`。
    ra_py = RESEARCH / "rig" / "venv_riganything" / "Scripts" / "python.exe"
    interp = str(ra_py) if ra_py.is_file() else PY
    cmd = [interp, str(script), "--mesh", str(mesh), "--out", str(out_dir)]
    if faces > 20_000:
        cmd += ["--simplify", "8192"]
    proc = subprocess.run(cmd, capture_output=True, env=env, cwd=str(RESEARCH / "rig"),
                          timeout=1800)
    log = (proc.stdout or b"").decode("utf-8", "replace") + \
          (proc.stderr or b"").decode("utf-8", "replace")
    glbs = sorted(out_dir.rglob("*rig*.glb"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not glbs:
        return {**result, "error": "RigAnything 没有产出 GLB", "log": log[-2000:]}
    return {**result, "ok": True, "artifacts": [str(glbs[0])],
            "faces_in": faces, "log": log[-1000:]}


def rig(mesh: Path, out_dir: Path, *, tier: str = "auto",
        template: str = "auto") -> dict:
    """自动绑定。tier: auto|template|riganything。

    - template：Blender 模板骨架 + 热扩散权重（8~68 s，零显存）
    - riganything：RigAnything 真推理（约 52 s，4.7 GB 显存，52 根语义骨）
    - auto：角色（人形/四足）优先 riganything，硬表面走 template
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "tier": tier, "artifacts": []}

    # ★ 2026-11-27 修：`--tier riganything` 原来只跑模板、然后在 note 里说"需手动跑"，
    #   等于这个档位是假的（实测报 joints=21，而 RigAnything 应该给 52 根语义骨）。
    #   现在真的分派：riganything 直接跑研究侧的推理脚本；auto = 先模板，闸门不过再上它。
    if tier == "riganything":
        return _run_riganything(mesh, out_dir, result)

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
