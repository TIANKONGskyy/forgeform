# -*- coding: utf-8 -*-
"""compare_mesh.py - 单图 vs 多视图 mesh 客观对比（空间关系/质量指标）"""
import sys
import numpy as np
import trimesh

sys.path.insert(0, "D:/AI-Workshop")

folder = r"C:\Users\mac\Desktop\AI创意工坊_全流程展示\中国龙"
single = folder + r"\模型.glb"
multi = folder + r"\模型_多视图.glb"

def analyze(path, label):
    try:
        m = trimesh.load(path, force="mesh")
        # 基础
        faces = len(m.faces)
        verts = len(m.vertices)
        watertight = m.is_watertight
        # bbox
        bounds = m.bounds
        dims = bounds[1] - bounds[0]
        # 体积
        vol = m.volume if m.is_watertight else 0
        # 连通组件数（部件数量）
        comps = trimesh.graph.connected_components(m.face_adjacency, min_len=1)
        # 对称性：左右镜像的 Hausdorff 距离（越小越对称）
        try:
            mirrored = m.copy()
            mirrored.apply_transform(np.diag([-1, 1, 1, 1]))  # 左右镜像
            dist, _ = trimesh.proximity.closest_point(m, mirrored.vertices)
            hd = dist.max()
        except Exception:
            hd = -1
        print(f"  {label}:")
        print(f"    面数={faces:,} 顶点={verts:,} 水密={watertight}")
        print(f"    尺寸={dims[0]:.1f}×{dims[1]:.1f}×{dims[2]:.1f}mm 体积={vol:.0f}mm³")
        print(f"    部件数={len(comps)} 左右对称Hausdorff={hd:.1f}mm(越小越对称)")
        return {"faces": faces, "watertight": watertight, "comps": len(comps), "symmetry": hd}
    except Exception as e:
        print(f"  {label}: 加载失败 {e}")
        return None

print("=== 单图 vs 多视图 mesh 对比 ===")
r1 = analyze(single, "单图")
r2 = analyze(multi, "多视图")
if r1 and r2:
    print("\n=== 结论 ===")
    print(f"  面数: {r1['faces']:,} → {r2['faces']:,}")
    print(f"  部件数: {r1['comps']} → {r2['comps']}")
    print(f"  对称性: {r1['symmetry']:.1f} → {r2['symmetry']:.1f}mm")
