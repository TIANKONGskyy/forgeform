# -*- coding: utf-8 -*-
"""把导出的 STL 渲成四个视角的预览图，用来证明"真的建出了三维模型"。

    python render_stl_preview.py helmet coupling planter dragon

只用 CPU（matplotlib Agg），不占显存 —— 可以和建模任务同时跑。
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import trimesh
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

HERE = Path(r"D:\ForgeForm-oss\docs\showcase")
VIEWS = [("front", (0, -1, 0)), ("side", (1, 0, 0)),
         ("top", (0, 0, 1)), ("iso", (1, -1, 0.8))]


def shade(mesh: trimesh.Trimesh, elev_dir) -> np.ndarray:
    """按面法线做一遍简单 lambert，给多边形上色。"""
    d = np.array(elev_dir, dtype=float)
    d /= np.linalg.norm(d)
    n = mesh.face_normals
    lam = np.clip(n @ d, 0, 1)
    base = np.array([0.72, 0.80, 0.92])          # 冷白
    dark = np.array([0.16, 0.20, 0.28])
    rgb = dark + (base - dark) * lam[:, None] ** 1.4
    return np.clip(rgb, 0, 1)


def render(stl: Path, out: Path) -> bool:
    m = trimesh.load(str(stl), force="mesh")
    if len(m.faces) == 0:
        print(f"  ! 空网格 {stl}")
        return False
    # 面数太多才抽稀 —— 而且必须是**保拓扑的**减面，不能用等距抽样：
    # 抽样会切断面片连接，画出来支离破碎，看着像模型本身坏了（踩过）。
    faces = m.faces
    verts = m.vertices
    if len(faces) > 120000:
        try:
            m = m.simplify_quadric_decimation(120000)
            faces, verts = m.faces, m.vertices
        except Exception:
            pass
    tris = verts[faces]

    fig = plt.figure(figsize=(8, 8), dpi=140, facecolor="#0a0c11")
    for i, (name, direction) in enumerate(VIEWS, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d", facecolor="#0a0c11")
        colors = shade(m, direction)
        pc = Poly3DCollection(tris, facecolors=colors, edgecolors="none", linewidths=0)
        ax.add_collection3d(pc)
        lo, hi = m.bounds
        ctr = (lo + hi) / 2
        r = max(hi - lo) / 2 * 1.02
        ax.set_xlim(ctr[0] - r, ctr[0] + r)
        ax.set_ylim(ctr[1] - r, ctr[1] + r)
        ax.set_zlim(ctr[2] - r, ctr[2] + r)
        az, el = {"front": (-90, 0), "side": (0, 0), "top": (0, 89.9), "iso": (-55, 22)}[name]
        ax.view_init(elev=el, azim=az)
        ax.set_box_aspect([1, 1, 1])
        ax.set_axis_off()
        ax.text2D(0.05, 0.94, name, transform=ax.transAxes,
                  color="#63708a", fontsize=9, family="monospace")
    fig.subplots_adjust(0, 0, 1, 1, 0.01, 0.01)
    fig.savefig(out, facecolor="#0a0c11", dpi=140)
    plt.close(fig)
    return True


def main() -> int:
    slugs = sys.argv[1:] or [d.name for d in HERE.iterdir()
                             if d.is_dir() and (d / "model.stl").exists()]
    done = 0
    for slug in slugs:
        stl = HERE / slug / "model.stl"
        if not stl.exists():
            print(f"[skip] {slug}: 没有 model.stl")
            continue
        out = HERE / slug / "model.png"
        try:
            if render(stl, out):
                print(f"[ok] {slug} -> {out.name} ({out.stat().st_size // 1024} KB)")
                done += 1
        except Exception as exc:
            print(f"[fail] {slug}: {exc}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
