# -*- coding: utf-8 -*-
"""把所有生成路径的成品模型收进一个文件夹，方便一次性对比（2026-11-27）。

用法：
    python tools/collect_gallery.py [--out D:\\铸想模型总览]

做的事：
1. 从各个产物目录挑出**当前能代表每条生成路线**的模型（不是全部 238 个）
2. 复制成统一命名：`<编号>_<方法>_<来源>.glb`
3. 每个都做一次二进制体检（面数 / 水密 / 贴图 / 关节 / 动画）
4. 生成 `总览.md` + `体检.json` + 三视角预览图

**只看清单不改原件** —— 源目录里的文件不会被删改。
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\ForgeForm-oss")
PY = r"D:\miniconda3\python.exe"
DEFAULT_OUT = Path(r"D:\铸想模型总览")

# (编号, 方法名, 说明, 源文件)
PICKS = [
    ("01", "单图-2mini512", "Hunyuan3D 2mini · 512 octree · 单张侧视图",
     r"D:\ForgeForm-oss\outputs\art\casts\whale\model.glb"),
    ("02", "单图-2.1-512", "Hunyuan3D 2.1 · 512 octree（当前主力）",
     r"C:\Users\mac\Desktop\feiji\plane_21.stl"),
    ("03", "单图-2.1-768", "Hunyuan3D 2.1 · 768 octree（极限档）",
     r"C:\Users\mac\Desktop\feiji\plane_21_768.stl"),
    ("04", "多视图-2mv-512", "四张真实照片 → 2mv · 512 octree",
     r"D:\ForgeForm-oss\outputs\3d\model_mv_1795771969.glb"),
    ("05", "精修-Direct3D-4步", "Direct3D-V3 · 512 档 4 步 + 小壳过滤",
     r"D:\tmp\verify_all\refine_s4.glb"),
    ("06", "精修-Direct3D-15步", "Direct3D-V3 · 512 档 15 步 + 小壳过滤",
     r"D:\tmp\verify_all\refine_s15.glb"),
    ("07", "贴图-PBR完整档", "Hunyuan3D Paint PBR · 6 视角 / 12 步 / 1024²",
     r"D:\ComfyUI-New\output\verify_paint_00001_.glb"),
    ("08", "贴图-PBR快速档", "Hunyuan3D Paint PBR · 4 视角 / 8 步 / 1024²（预览用）",
     r"D:\tmp\verify_paint_tool\plane_pbr.glb"),
    ("09", "骨骼-模板档", "Blender 模板骨架 + 热扩散权重（21 关节）",
     r"D:\tmp\verify_rig\rigged_4d013528439d.glb"),
    ("10", "骨骼-RigAnything", "RigAnything · 语义骨架（40+ 关节，含手指）",
     r"D:\tmp\verify_rig_ra6\rigged_9491a2340ae6.glb"),
    ("11", "动画-四种mocap", "BVH 重定向 · 走 / 挥 / 鞠 / 待机（4 条具名动画）",
     r"D:\ForgeForm-oss\work\research\motion\人物_BVH四种动作_mocap.glb"),
    ("12", "动画-跑步", "BVH 重定向 · 跑步循环",
     r"D:\tmp\verify_all\motion_09_01_run_bvh.glb"),
]


def glb_info(path: Path) -> dict:
    """解 GLB 二进制拿真信息（不信扩展名/大小）。STL 走 trimesh。"""
    info = {"bytes": path.stat().st_size, "faces": None, "watertight": None,
            "textures": 0, "joints": 0, "animations": 0, "format": path.suffix.lower()}
    if path.suffix.lower() == ".glb":
        raw = path.read_bytes()
        off = 12
        clen, ctype = struct.unpack("<II", raw[off:off + 8])
        d = json.loads(raw[off + 8:off + 8 + clen].decode("utf-8"))
        info["textures"] = len(d.get("textures") or [])
        info["animations"] = len(d.get("animations") or [])
        info["joints"] = sum(len(s.get("joints") or []) for s in (d.get("skins") or []))
        prims = [p for m in (d.get("meshes") or []) for p in (m.get("primitives") or [])]
        info["primitives"] = len(prims)
        ax = d.get("accessors") or []
        if prims:
            idx = prims[0].get("indices")
            if isinstance(idx, int) and idx < len(ax):
                info["faces"] = int(ax[idx].get("count", 0) // 3)
    try:
        import trimesh
        m = trimesh.load(str(path), force="mesh")
        info["faces"] = int(len(m.faces))
        info["watertight"] = bool(m.is_watertight)
        info["extents_mm"] = [round(float(x), 1) for x in m.extents]
    except Exception as e:
        info["trimesh_error"] = f"{type(e).__name__}: {e}"
    return info


def render_preview(paths: list[Path], out_png: Path) -> None:
    """把前若干件渲染成一张对比图（CPU，不占 GPU 显存）。"""
    script = out_png.parent / "_render.py"
    script.write_text(f'''
import sys
sys.path.insert(0, r"{ROOT}")
import numpy as np, trimesh, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from pathlib import Path

items = {json.dumps([str(p) for p in paths], ensure_ascii=False)}
n = len(items)
cols = 4
rows = (n + cols - 1) // cols
fig = plt.figure(figsize=(cols * 3.4, rows * 3.2), dpi=110)
for i, p in enumerate(items):
    ax = fig.add_subplot(rows, cols, i + 1, projection="3d")
    try:
        m = trimesh.load(p, force="mesh")
        if len(m.faces) > 120000:
            m = m.simplify_quadric_decimation(face_count=120000)
        tri = m.triangles
        nrm = m.face_normals
        light = np.array([0.35, -0.45, 0.82]); light /= np.linalg.norm(light)
        ln = np.linalg.norm(nrm, axis=1); ln[ln == 0] = 1
        shade = 0.3 + 0.7 * np.abs((nrm / ln[:, None]) @ light)
        fc = np.clip(np.array([0.72, 0.76, 0.84]) * shade[:, None], 0, 1)
        # 有贴图就取贴图平均色当 tint
        try:
            mat = m.visual.material
            img = getattr(mat, "baseColorTexture", None)
            if img is not None:
                a = np.asarray(img.convert("RGB")).reshape(-1, 3).mean(axis=0) / 255.0
                fc = np.clip(fc * (0.5 + a * 0.9), 0, 1)
        except Exception:
            pass
        ax.add_collection3d(Poly3DCollection(tri, facecolors=fc, edgecolors="none"))
        c = m.bounds.mean(axis=0); r = float(m.extents.max()) / 2
        ax.set_xlim(c[0]-r, c[0]+r); ax.set_ylim(c[1]-r, c[1]+r); ax.set_zlim(c[2]-r, c[2]+r)
        ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=16, azim=-58); ax.set_axis_off()
        ax.set_title(Path(p).stem.split("_")[0], fontsize=9)
    except Exception as e:
        ax.text(0.5, 0.5, "渲染失败", ha="center")
        ax.set_axis_off()
fig.savefig(r"{out_png}", bbox_inches="tight", facecolor="#f5f7fa")
print("ok")
''', encoding="utf-8")
    subprocess.run([PY, str(script)], capture_output=True, timeout=1800)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ns = ap.parse_args()
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)

    manifest, copied = [], []
    for num, method, desc, src in PICKS:
        s = Path(src)
        if not s.is_file():
            manifest.append({"num": num, "method": method, "desc": desc,
                             "ok": False, "error": f"源文件不存在: {s}"})
            print(f"  ✗ {num} {method}: 缺 {s.name}", flush=True)
            continue
        dst = out / f"{num}_{method}{s.suffix.lower()}"
        shutil.copy2(s, dst)
        info = glb_info(dst)
        copied.append(dst)
        manifest.append({"num": num, "method": method, "desc": desc, "ok": True,
                         "file": dst.name, **info})
        print(f"  ✓ {num} {method:<22} {info['bytes']/1024/1024:>6.1f} MB  "
              f"面 {info.get('faces')}  水密 {info.get('watertight')}  "
              f"贴图 {info['textures']}  关节 {info['joints']}  动画 {info['animations']}",
              flush=True)

    # 预览图
    if copied:
        try:
            render_preview(copied, out / "总览预览.png")
            print("  ✓ 预览图 总览预览.png", flush=True)
        except Exception as e:
            print(f"  ✗ 预览图失败: {e}", flush=True)

    (out / "体检.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    lines = ["# 铸想各生成路线成品总览（2026-11-27）", "",
             "同一份代码、同一台机器（V100 32G）跑出来的 12 件代表产物。", "",
             "| # | 方法 | 面数 | 水密 | 贴图 | 关节 | 动画 | 大小 | 说明 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m in manifest:
        if not m.get("ok"):
            lines.append(f"| {m['num']} | {m['method']} | - | - | - | - | - | - | 缺文件 |")
            continue
        lines.append(f"| {m['num']} | {m['method']} | {m.get('faces')} | "
                     f"{'✓' if m.get('watertight') else '✗'} | {m['textures']} | "
                     f"{m['joints']} | {m['animations']} | "
                     f"{m['bytes']/1024/1024:.1f} MB | {m['desc']} |")
    lines += ["", "## 怎么看", "",
              "- 直接用 [Blender](https://www.blender.org/) 或在线查看器打开 `.glb`",
              "- 带贴图的（07/08）要看颜色，白模看不出差别",
              "- 带骨骼的（09/10）在 Blender 里按 `N` 面板看 Armature，或直接播放动画",
              "- 带动画的（11/12）在 Blender 的 Action 列表里能看到多条具名动作",
              "- `总览预览.png` 是 12 件的缩略对比图"]
    (out / "总览.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n  输出目录: {out}")
    print(f"  成功 {sum(1 for m in manifest if m.get('ok'))}/{len(PICKS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
