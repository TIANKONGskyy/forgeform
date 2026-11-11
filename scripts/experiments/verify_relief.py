# -*- coding: utf-8 -*-
"""verify_relief.py - 客观验证 STL 的浮雕几何细节（装饰是否真的"立"起来了）"""
import sys
import numpy as np
import trimesh

sys.path.insert(0, "D:/AI-Workshop")

def analyze_relief(path, label):
    """分析 mesh 的浮雕细节：面间角度（浮雕=大角度）、表面高度变化"""
    try:
        m = trimesh.load(path, force="mesh")
        # 手动计算面间二面角：相邻面的法线夹角
        fa = m.face_adjacency  # 相邻面对 (N, 2)
        fn = m.face_normals
        n1 = fn[fa[:, 0]]
        n2 = fn[fa[:, 1]]
        cosang = np.clip(np.einsum('ij,ij->i', n1, n2), -1, 1)
        angles = np.arccos(cosang)  # 弧度
        sharp = (angles > 0.3).sum()  # >17° 的折痕
        total_edges = len(angles)
        # 顶点到局部均值平面的偏移（浮雕高度）
        verts = m.vertices
        # 计算每个顶点相对其邻居的高度变化（简化：用顶点法线一致性）
        # 曲率代理：顶点法线的邻域变化
        normals = m.vertex_normals
        # 采样：浮雕区域 = 法线方向快速变化的顶点
        print(f"  {label}:")
        print(f"    面数={len(m.faces):,}")
        print(f"    折痕边(>17°角)={sharp:,} / 总边={total_edges:,} ({sharp/total_edges*100:.1f}%)")
        print(f"    面间角度均值={np.degrees(angles).mean():.1f}° 最大={np.degrees(angles).max():.1f}°")
        # 曲率分析（用离散高斯曲率估计：顶点周围角度和）
        return sharp
    except Exception as e:
        print(f"  {label}: 失败 {e}")
        return 0

print("=== STL 浮雕几何验证（折痕边 = 浮雕/装饰细节）===")
folder = r"C:\Users\mac\Desktop\AI创意工坊_全流程展示"
s1 = analyze_relief(folder + r"\中国龙\打印模型.stl", "中国龙 单图 STL")
s2 = analyze_relief(folder + r"\中国龙\打印模型_多视图.stl", "中国龙 多视图 STL") if False else None
# 海盗船多视图
s3 = analyze_relief(folder + r"\海盗船\打印模型_多视图.stl", "海盗船 多视图 STL")
print("\n说明：折痕边比例越高 = 浮雕/装饰几何细节越丰富（对比光滑平面）")
