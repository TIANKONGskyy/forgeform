# -*- coding: utf-8 -*-
"""生成铸想的宣传视频：模型 360° 旋转 + 文字说明。

    python tools/make_promo_video.py

产物：docs/assets/promo/demo.mp4（6 秒，1280×720，H.264）
       docs/assets/promo/demo.gif（同内容，给 README 用）

用 matplotlib 的 CPU 渲染（不占显存，和建模可以同时跑）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import trimesh
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

ROOT = Path(r"D:\ForgeForm-oss")
OUT = ROOT / "docs" / "assets" / "promo"
TMP = OUT / "_frames"
FPS = 24
SECONDS = 6
N = FPS * SECONDS        # 144 帧
W, H = 1280, 720

BG = (12 / 255, 16 / 255, 25 / 255)
MESH = "#b9c2cf"
ACCENT = "#9ce4da"


def load_mesh(path: str, target: int = 9000) -> trimesh.Trimesh:
    m = trimesh.load(path, force="mesh")
    if len(m.faces) > target:
        try:
            import fast_simplification
            v, f = fast_simplification.simplify(
                m.vertices.astype(np.float32), m.faces.astype(np.int32),
                1.0 - target / len(m.faces))
            m = trimesh.Trimesh(v, f, process=False)
        except Exception:
            m = m  # 简化失败就用原网格（慢一点但能跑）
    return m


def render_frames(mesh: trimesh.Trimesh) -> list[Path]:
    TMP.mkdir(parents=True, exist_ok=True)
    for f in TMP.glob("*.png"):
        f.unlink()

    tris = mesh.vertices[mesh.faces]
    center = mesh.bounds.mean(axis=0)
    scale = 1.0 / max(mesh.extents)
    tris = (tris - center) * scale

    paths = []
    for i in range(N):
        t = i / N
        elev = 18 + 8 * np.sin(t * 2 * np.pi * 2)      # 轻微上下
        azim = 360 * t
        fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=BG)
        ax = fig.add_axes([0, 0, 1, 1], projection="3d", facecolor=BG)
        ax.set_axis_off()
        light = np.array([0.4, 0.6, 0.7])
        n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        ln[ln == 0] = 1
        shade = 0.35 + 0.65 * np.abs((n / ln) @ light)
        face = np.clip(np.array([0.73, 0.76, 0.81]) * shade[:, None], 0, 1)
        poly = Poly3DCollection(tris, facecolors=face, edgecolors="none", linewidths=0)
        ax.add_collection3d(poly)
        ax.set_xlim(-0.56, 0.56)
        ax.set_ylim(-0.56, 0.56)
        ax.set_zlim(-0.56, 0.56)
        ax.view_init(elev=elev, azim=azim)
        ax.set_box_aspect((1, 1, 1))
        # 底部文字
        fig.text(0.06, 0.09, "铸想 ForgeForm", color="white", fontsize=17,
                 fontweight="bold", family="Microsoft YaHei")
        fig.text(0.06, 0.05, "一句话 → 出图 → 建模 → 可打印 STL", color=ACCENT,
                 fontsize=12, family="Microsoft YaHei")
        fig.text(0.94, 0.09, "by ZYKT studio", color="#6d7891", fontsize=10,
                 ha="right", family="Microsoft YaHei")
        fig.text(0.94, 0.05, "github.com/TIANKONGskyy/forgeform", color="#6d7891",
                 fontsize=9, ha="right")
        p = TMP / f"f{i:04d}.png"
        fig.savefig(p, facecolor=BG, dpi=100)
        plt.close(fig)
        paths.append(p)
        if (i + 1) % 24 == 0:
            print(f"  帧 {i + 1}/{N}", flush=True)
    return paths


def encode(frames: list[Path]) -> None:
    mp4 = OUT / "demo.mp4"
    gif = OUT / "demo.gif"
    ffmpeg = shutil.which("ffmpeg") or r"D:\ffmpeg\bin\ffmpeg.exe"
    if not Path(ffmpeg).exists() and not shutil.which("ffmpeg"):
        print("  ✗ 没有 ffmpeg，跳过编码")
        return

    cmd = [ffmpeg, "-y", "-framerate", str(FPS), "-i", str(TMP / "f%04d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-vf", f"scale={W}:{H}", str(mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  ✗ mp4 失败:", r.stderr[-400:])
    else:
        print(f"  mp4: {mp4}  {mp4.stat().st_size // 1024} KB")

    cmd2 = [ffmpeg, "-y", "-framerate", str(FPS), "-i", str(TMP / "f%04d.png"),
            "-vf", f"scale=720:-1:flags=lanczos,fps=15", "-loop", "0", str(gif)]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    if r2.returncode == 0 and gif.exists():
        print(f"  gif: {gif}  {gif.stat().st_size // 1024} KB")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    # 用猫头鹰（形态最完整、没有薄片问题）
    src = ROOT / "outputs" / "e2e" / "final_stl.png"
    glb = None
    # trimmed_owl 是 trim_base 切过立板的版本（那个板是**竖直**的，
    # 底部尖峰检测切不到，但 tools/trim_base.py 单独跑过一次把它切掉了）
    for cand in ("outputs/art/view/trimmed_owl.glb",
                 "outputs/3d/model_1794530838.glb",
                 "outputs/art/view/owl.glb"):
        p = ROOT / cand
        if p.exists():
            glb = p
            break
    if not glb:
        print("找不到模型文件")
        return 1

    print(f"源模型: {glb}")
    mesh = load_mesh(str(glb))
    print(f"简化后: {len(mesh.faces):,} 面")
    print(f"渲染 {N} 帧...")
    frames = render_frames(mesh)
    print(f"编码...")
    encode(frames)
    print(f"\n输出: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
