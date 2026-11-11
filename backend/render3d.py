"""把 GLB 渲染成预览图，贴进对话里（2026-09-22）

为什么需要它：
  建模完成后只有一个 gr.Model3D 组件，而 triposplat 产出的 GLB 有 40~80MB，
  浏览器加载慢/容易空白；更关键的是建模成功会自动切到新对话组 → 组件被隐藏，
  用户主观感受就是"建模生成出来以后没有展示"。
  渲染成 PNG 后作为图片贴进对话气泡：历史滚动随时可见，与其它图片一致。

实现取舍：
  本机没有 pyrender，trimesh 的离屏渲染依赖 OpenGL/GL 上下文（这台机器上不稳），
  故用 matplotlib 3D 多边形渲染：纯 CPU、无 GL、结果稳定可复现。
  面数过多时先简化（fast_simplification，与 mesh_optimizer 同源），否则绘图很慢。
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import trimesh


def _pyplot():
    """matplotlib 只在真正渲染时才导入。

    2026-09-23：这个模块原来在**模块级** import matplotlib，而 matplotlib 只装在
    Hermes 的 venv 里、生产用的 miniconda 里没有；start-all.ps1 又会清空 PYTHONPATH
    → 用户正常启动时，建模完成后渲染 4 视角会 ModuleNotFoundError，被 _do_generate_3d
    的 try 吞掉 → **静默没有预览**（就是"建模完看不到模型"的真身）。
    改成懒加载：缺依赖只降级为"没有预览"，不再牵连建模流程；并把缺什么写清楚。
    """
    import matplotlib
    matplotlib.use("Agg")          # 必须：无显示环境
    import matplotlib.pyplot as plt
    return plt


def _load_mesh(asset_path: str) -> "trimesh.Trimesh":
    """读取 3D 资产（GLB/GLTF 走 scene，STL/OBJ 走 mesh），合并成一个 Trimesh

    2026-09-22：原实现写死 file_type="glb"，无法渲染 STL。而 STL 才是
    mesh_optimizer 处理过的可打印网格（水密单体、面少），比原始 GLB 更适合做预览。

    【2026-09-23 修】GLB 是 Y-up，而我们用 matplotlib 3D 渲染（Z-up）→ 不换算会
    "躺着"。这里只对 GLB/GLTF 转一次；STL 是我们产出的 Z-up 资产，不能转。
    """
    p = Path(asset_path)
    suffix = p.suffix.lower().lstrip(".")
    if suffix in ("glb", "gltf"):
        scene = trimesh.load(str(p), force="scene", file_type=suffix)
        from .mesh_optimizer import y_up_to_z_up
        if isinstance(scene, trimesh.Trimesh):
            return y_up_to_z_up(scene)
        # GLB 通常加载成 Scene，不是 Trimesh —— 之前只在 Trimesh 分支转轴，
        # Scene 的几何原样拼接，预览里花瓶是躺着的。这里逐个几何转。
        geoms = [y_up_to_z_up(g) for g in getattr(scene, "geometry", {}).values()
                 if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError(f"资产里没有可用网格: {asset_path}")
        return trimesh.util.concatenate(geoms) if len(geoms) > 1 else geoms[0]
    else:
        scene = trimesh.load(str(p), force="mesh")
    if isinstance(scene, trimesh.Trimesh):
        return scene
    geoms = [g for g in getattr(scene, "geometry", {}).values()
             if isinstance(g, trimesh.Trimesh)]
    if not geoms:
        raise ValueError(f"资产里没有可用网格: {asset_path}")
    if len(geoms) == 1:
        return geoms[0]
    return trimesh.util.concatenate(geoms)


def _base_color(mesh: "trimesh.Trimesh") -> tuple:
    """取纹理平均色作为预览底色（略提亮，matplotlib 的 shade 会压暗）；取不到用中性灰蓝"""
    try:
        vis = getattr(mesh, "visual", None)
        mat = getattr(vis, "material", None)
        tex = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
        if tex is not None:
            img = tex.convert("RGB") if hasattr(tex, "convert") else tex
            arr = np.asarray(img.resize((32, 32)), dtype=float) / 255.0
            base = arr.reshape(-1, 3).mean(axis=0)
            return tuple(np.clip(base * 1.25 + 0.10, 0.0, 1.0))
    except Exception:
        pass
    return (0.72, 0.76, 0.82)


def _decimate(mesh: "trimesh.Trimesh", target: int = 12000) -> "trimesh.Trimesh":
    """面数过多先简化（matplotlib 的 plot_trisurf 是 O(面数)，不减会画几十秒甚至几分钟）"""
    if len(mesh.faces) <= target:
        return mesh
    try:
        import fast_simplification
        v, f = fast_simplification.simplify(
            np.asarray(mesh.vertices, dtype=np.float32),
            np.asarray(mesh.faces, dtype=np.int32),
            1.0 - target / len(mesh.faces),
        )
        out = trimesh.Trimesh(vertices=v, faces=f, process=False)
        if len(out.faces) > 0:
            return out
    except Exception:
        # 退化方案：随机抽样面片（仅用于展示，允许少量破面）
        rng = random.Random(0)
        keep = rng.sample(range(len(mesh.faces)), min(target, len(mesh.faces)))
        return trimesh.Trimesh(vertices=mesh.vertices,
                               faces=np.asarray(mesh.faces)[keep], process=False)
    return mesh


def render_mesh_preview(asset_path: str, out_png: str = None,
                        px: int = 480, views=(45, 135, 225, 315)) -> str:
    """把 3D 资产（GLB 或 STL）渲染成 4 视角预览图，返回 PNG 路径；失败返回空串"""
    try:
        mesh = _decimate(_load_mesh(asset_path))
    except Exception as e:
        print(f"[render3d] 读取失败: {e!r}")
        return ""
    if mesh is None or len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        # 空/损坏网格（例如只有 GLB 头没有几何体）→ 直接放弃，别抛 numpy 的空切片异常
        print("[render3d] 网格为空，跳过预览")
        return ""

    if out_png is None:
        out_png = str(Path(asset_path).with_name("preview.png"))
    base = _base_color(mesh)
    v, f = np.asarray(mesh.vertices, dtype=float), np.asarray(mesh.faces)
    tri = v[f]                                    # (F,3,3)
    ctr = tri.reshape(-1, 3).mean(axis=0)
    rad = float(np.abs(tri.reshape(-1, 3) - ctr).max()) or 1.0
    try:
        plt = _pyplot()
    except Exception as e:
        print(f"[render3d] 缺少 matplotlib，跳过预览（建模不受影响）: {e!r}")
        return ""

    n = len(views)
    cols = 2 if n > 1 else 1
    rows = (n + cols - 1) // cols
    fig = plt.figure(figsize=(px / 120 * cols, px / 120 * rows), dpi=120)
    # plot_trisurf triangulates in XY and can flip the face winding of closed
    # surfaces. Preserve the original 3D triangles for consistent shading.
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    normals = np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    light = np.array([0.4, -0.5, 0.75]); light /= np.linalg.norm(light)
    colors = np.clip(np.asarray(base)[None, :] * (0.45 + 0.55*np.maximum(normals@light, 0))[:, None], 0, 1)

    for i, az in enumerate(views, 1):
        ax = fig.add_subplot(rows, cols, i, projection="3d")
        ax.add_collection3d(Poly3DCollection(tri, facecolors=colors, edgecolors='none',
                                           linewidths=0, antialiased=False))
        ax.view_init(elev=22, azim=az)
        ax.set_box_aspect((1, 1, 1))
        ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
        ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
        ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
        ax.set_axis_off()
        ax.set_title(f"{az}°", fontsize=8, color="#888")

    fig.subplots_adjust(left=0, right=1, top=0.96, bottom=0, wspace=0, hspace=0.02)
    try:
        fig.savefig(out_png, facecolor="white")
    finally:
        plt.close(fig)
    print(f"[render3d] 预览已生成: {Path(out_png).name} ({len(f)} 面)")
    return out_png
