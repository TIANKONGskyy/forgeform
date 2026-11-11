# -*- coding: utf-8 -*-
"""verify_relief_depth.py - 验证 STL 浮雕深度（凸起装饰高度是否够 FDM 打印）"""
import sys
import numpy as np
import trimesh

sys.path.insert(0, "D:/AI-Workshop")

def relief_depth(path, label):
    try:
        m = trimesh.load(path, force="mesh")
        # 下采样加速
        if len(m.faces) > 100000:
            m = m.simplify_quadric_decimation(face_count=100000)
        verts = m.vertices.copy()
        # 拉普拉斯平滑得到基准表面
        smooth = trimesh.smoothing.filter_laplacian(m, iterations=20, lamb=0.5)
        base = smooth.vertices
        diff = np.linalg.norm(verts - base, axis=1)
        print(f"  {label}: 浮雕偏移 均值={diff.mean():.3f}mm 中位={np.median(diff):.3f}mm P90={np.percentile(diff,90):.3f}mm 最大={diff.max():.3f}mm")
        print(f"    （FDM 0.4mm 喷嘴：浮雕 ≥0.6mm 才清晰；当前 P90 {'✅够' if np.percentile(diff,90) >= 0.6 else '⚠️偏浅'}）")
        return diff
    except Exception as e:
        print(f"  {label}: 失败 {e}")
        return None

print("=== 浮雕深度验证 ===")
folder = r"C:\Users\mac\Desktop\AI创意工坊_全流程展示"
relief_depth(folder + r"\海盗船\打印模型_多视图.stl", "海盗船 多视图")
relief_depth(folder + r"\哥特式大教堂\打印模型_多视图.stl", "教堂 多视图")
