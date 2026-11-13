# -*- coding: utf-8 -*-
"""切掉 2mini 重建出来的"底部地毯"。

## 问题从哪来

参考图是产品摄影风格：灰色背景 + 地面接触阴影。2mini 会把背景和阴影
当成几何一起重建，结果模型下面多一块带纹理的平板。实测四件艺术品全中，
输入图见 `outputs/art/art_inputs.png`。

## 怎么切

两步，都是几何判断，不看颜色：

1. **保留最大连通块** —— 主体几乎总是体积最大的那块；碎片和地毯是零散小块。
2. **切底部薄板** —— 从最低点往上扫，如果某一层的水平截面面积
   远大于它上面几层（像一个"盘子"），说明这是底座，把下面整段裁掉。

只切 Z 轴（模型的上方向），不动 XY —— 底座再大也是平的。

用法：
    python tools/trim_base.py in.glb out.glb [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import trimesh


def largest_component(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """保留**面数最多**的连通块。

    不用 `mesh.split()`：2mini 的产物常有退化面，split 会把整块主体拆成
    一堆只有 2 个面的碎片，按体积挑就会选中一个 2 面的垃圾（实测踩过）。
    改成按面邻接算连通分量，再按面数选最大的。
    """
    import numpy as np
    import trimesh.graph as tg

    n = len(mesh.faces)
    if n == 0:
        return mesh
    try:
        comps = tg.connected_components(mesh.face_adjacency,
                                        nodes=np.arange(n), min_len=1)
    except Exception:
        return mesh
    if len(comps) <= 1:
        return mesh
    biggest = max(comps, key=len)
    if len(biggest) >= n * 0.98:
        return mesh
    return mesh.submesh([biggest], append=True)


def find_base_cut(mesh: trimesh.Trimesh, spike_ratio: float = 0.12,
                  max_cut_ratio: float = 0.20) -> float | None:
    """找"底部地毯"的裁切高度。找不到返回 None。

    判据来自实测的 Z 轴三角形分布（`outputs/art` 那批）：
    地毯是一大片平面，会在**最底层**堆出一个尖峰 —— 猫头鹰那件
    底层一格有 105 万个三角形，占总体 33%，上面一格骤降到 2.5 万。
    而形态正常的模型（鹿）底层只占 0.2%，分布平滑。

    所以：只切"最底层三角形占比 > spike_ratio"的情况。
    和主体融合在一起的地毯（鹿那种）这个方法识别不出来 ——
    那种得靠出图阶段去掉背景，见 docs/MODEL-BENCH.md。
    """
    import numpy as np

    n = len(mesh.faces)
    if n == 0:
        return None
    z = mesh.triangles_center[:, 2]
    lo, hi = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    height = hi - lo
    if height <= 1e-6:
        return None

    bins = 40
    hist, edges = np.histogram(z, bins=bins, range=(lo, hi))
    bottom = int(hist[0])
    if bottom / n < spike_ratio:
        return None

    # 尖峰所在层的上边界就是切点，但不超过整体高度的 max_cut_ratio
    cut = float(edges[1])
    if cut > lo + height * max_cut_ratio:
        cut = lo + height * max_cut_ratio
    print(f"  底部尖峰: 最底层 {bottom:,} 面 / 总 {n:,} = {bottom / n * 100:.1f}%")
    print(f"  上层降到 {int(hist[1]):,} 面 → 在 z={cut:.4f} 切")
    return cut


def trim(path_in: str, path_out: str, dry: bool = False) -> dict:
    mesh = trimesh.load(path_in, force="mesh")
    info = {"faces_in": len(mesh.faces), "components_in": 0,
            "cut_z": None, "faces_out": 0, "removed_pct": 0.0}

    parts = mesh.split(only_watertight=False)
    info["components_in"] = len(parts)
    print(f"  读入 {path_in}")
    print(f"  面数 {len(mesh.faces):,}  连通块 {len(parts)}")

    main = largest_component(mesh)
    if main is not mesh:
        print(f"  保留最大连通块: {len(main.faces):,} 面")

    cut = find_base_cut(main)
    if cut is None:
        print("  没检测到底座平面，只做连通块清理")
        out = main
        info["cut_z"] = None
    else:
        # 沿 Z 平面切（trimesh 5.x 没有 Trimesh.slice，用 intersections 里的函数）
        try:
            import trimesh.intersections as ti
            out = ti.slice_mesh_plane(main, plane_normal=[0, 0, 1],
                                      plane_origin=[0, 0, cut], cap=True)
            if out is None or len(out.faces) == 0:
                print("  切完是空的，放弃裁切")
                out = main
                info["cut_z"] = None
            else:
                info["cut_z"] = round(cut, 4)
                print(f"  在 z={cut:.4f} 处切开，保留上半部分")
                # 切完再清一次连通块：底座常常是**独立的一块**，
                # 跟主体不连通，切开之后要再筛一遍才能去掉
                before = len(out.faces)
                out = largest_component(out)
                if len(out.faces) < before:
                    print(f"  切后再清理: {before:,} → {len(out.faces):,} 面")
        except Exception as exc:
            print(f"  裁切失败（{exc}），只做连通块清理")
            out = main
            info["cut_z"] = None

    info["faces_out"] = len(out.faces)
    info["removed_pct"] = round((1 - len(out.faces) / max(1, len(mesh.faces))) * 100, 1)

    if not dry:
        out.export(path_out)
        p = Path(path_out)
        print(f"  写出 {path_out}  {p.stat().st_size / 1024 / 1024:.1f} MB")
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="处理 outputs/art/view 下所有 glb")
    args = ap.parse_args()

    if args.all:
        d = Path(args.src)
        for f in sorted(d.glob("*.glb")):
            if f.name.startswith("trimmed_"):
                continue
            print(f"\n[{f.name}]")
            trim(str(f), str(d / f"trimmed_{f.name}"), dry=args.dry_run)
        return 0

    dst = args.dst or str(Path(args.src).with_name("trimmed_" + Path(args.src).name))
    print(f"\n[{Path(args.src).name}]")
    trim(args.src, dst, dry=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
