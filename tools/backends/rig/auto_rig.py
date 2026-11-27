#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ForgeForm 骨骼线 / Auto-Rigging —— 模板骨架 + 自动权重 可复用管线
=================================================================
输入：任意水密网格（.stl/.obj/.glb/.gltf/.ply/.fbx）
输出：带 armature 的 .glb + 骨骼线渲染 PNG + 权重质量 report.json

两种跑法：
  1) 直接跑（脚本自己找 blender.exe 重启到 Blender 里）：
       D:\\miniconda3\\python.exe auto_rig.py <mesh> --out <dir> [--template auto|humanoid|quadruped|aircraft]
  2) 在 Blender 里跑：
       blender.exe -b --factory-startup -P auto_rig.py -- <mesh> --out <dir>

设计要点（都是为了"任意网格能跑通"）：
  - 模板骨架用「bbox 归一化坐标」定义：x,y ∈ [-0.5,0.5]，z ∈ [0,1]，
    按网格 bbox 映射，所以骨架永远铺满 bbox。
  - 接头点若在网格外部（薄壳/异形），沿最近表面法线往里推，并记录推了多少。
  - 权重先试 Blender 骨骼热扩散（ARMATURE_AUTO，质量最好），
    失败/有孤点则用「最近骨段反距离」补齐 → 无权重顶点数逼到 0。
  - 每顶点最多留 4 根骨影响（glTF 规范），重归一化。
  - 撕裂检测：拿 rest / posed 两态的实际边长比，算 max / p99 / >1.5x 的边数。
  - 渲染：Cycles CPU（本机 V100 是 TCC 无 OpenGL，不能用 EEVEE）+ 合成器辉光。
"""

import os
import sys
import json
import math
import time
import argparse
import subprocess

# ---------------------------------------------------------------- bootstrap


def _in_blender():
    """判断当前是否已经跑在 Blender 进程里。

    注意：miniconda 的 site-packages 里也装了一个 bpy 4.2 pip wheel，
    能 import 但【不带 Cycles 渲染器】。所以不能拿 import bpy 当判据，
    只看进程名 / 环境变量哨兵。
    """
    if os.environ.get('AUTORIG_IN_BLENDER') == '1':
        return True
    base = os.path.basename(sys.argv[0] or '').lower()
    return base.startswith('blender') or base.startswith('bpy')


def _reexec_in_blender():
    """不在 Blender 里跑时，自动找到 blender.exe 重新执行自己。"""
    if _in_blender():
        return False

    argv = sys.argv[1:]
    exe = os.environ.get('BLENDER_EXE') or r'D:\tools\blender\blender-4.2.23-windows-x64\blender.exe'
    if not os.path.exists(exe):
        cand = [
            r'D:\tools\blender\blender-4.2.23-windows-x64\blender.exe',
            r'C:\Program Files\Blender Foundation\Blender 4.2\blender.exe',
        ]
        exe = next((c for c in cand if os.path.exists(c)), None)
    if not exe:
        sys.exit('[auto_rig] 找不到 blender.exe，请设置环境变量 BLENDER_EXE 或用 blender -b -P 方式跑')
    cmd = [exe, '-b', '--factory-startup', '-P', os.path.abspath(__file__), '--'] + argv
    print('[auto_rig] 启动 Blender:', exe, flush=True)
    sys.stderr.flush()
    env = dict(os.environ)
    env['AUTORIG_IN_BLENDER'] = '1'
    rc = subprocess.call(cmd, env=env)
    sys.exit(rc)


_reexec_in_blender()

# ---------------------------------------------------------------- imports (bpy)
import bpy
import bmesh
from mathutils import Vector, Matrix

T0 = time.time()
LOG_LINES = []


def log(msg):
    s = '[%7.1fs] %s' % (time.time() - T0, msg)
    print(s, flush=True)
    LOG_LINES.append(s)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- templates
# 坐标：x 右, y 前后, z 上；x,y ∈ [-0.5,0.5]，z ∈ [0,1]（相对 bbox）
# 每条：(name, parent, head, tail, connected)
# 命名带上 arm/leg/wing/head/tail 关键词，pose 阶段按关键词自动摆姿势。

TEMPLATES = {}

def humanoid_bones(arm_mode='t'):
    """人形模板：用「身高比例」定义（x/y/z 都是 身高H 的倍数）。

    为什么不用 bbox 归一化：人的肩宽/髋宽是身高的固定比例，和网格 bbox 宽度无关。
    实测手臂下垂的角色（bbox 宽/高 = 0.22）用 T-pose 模板 → 13/21 根骨被拉到体外。
    arm_mode: t=T字, a=A字(约40°下垂), down=自然下垂(约75°)
    """
    ang = {'t': 0.0, 'a': 42.0, 'down': 74.0}.get(arm_mode, 0.0)
    ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))

    def arm(side):
        s = 1.0 if side == 'L' else -1.0
        sh = (s * 0.100, 0.0, 0.815)          # 肩关节
        segs = [('upper_arm', 0.165), ('forearm', 0.145), ('hand', 0.085)]
        pts = [sh]
        for _nm, L in segs:
            x, y, z = pts[-1]
            pts.append((x + s * L * ca, y, z - L * sa))
        out = [('clavicle.' + side, 'chest', (s * 0.020, 0.0, 0.805), sh, False)]
        for i, (nm, _L) in enumerate(segs):
            out.append(('%s.%s' % (nm, side), ('clavicle.' + side) if i == 0 else '%s.%s' % (segs[i - 1][0], side),
                        pts[i], pts[i + 1], i > 0))
        return out

    def leg(side):
        s = 1.0 if side == 'L' else -1.0
        hip = (s * 0.072, 0.0, 0.505)
        knee = (s * 0.070, 0.005, 0.275)
        ankle = (s * 0.068, 0.010, 0.048)
        toe = (s * 0.068, -0.075, 0.012)
        return [
            ('thigh.' + side, 'root', hip, knee, False),
            ('shin.' + side, 'thigh.' + side, knee, ankle, True),
            ('foot.' + side, 'shin.' + side, ankle, toe, True),
        ]

    b = [
        ('root',     None,       (0.0, 0.0, 0.505), (0.0, 0.0, 0.560), False),
        ('spine_01', 'root',     (0.0, 0.0, 0.560), (0.0, 0.0, 0.635), True),
        ('spine_02', 'spine_01', (0.0, 0.0, 0.635), (0.0, 0.0, 0.710), True),
        ('chest',    'spine_02', (0.0, 0.0, 0.710), (0.0, 0.0, 0.800), True),
        ('neck',     'chest',    (0.0, 0.0, 0.800), (0.0, 0.0, 0.865), True),
        ('head',     'neck',     (0.0, 0.0, 0.865), (0.0, 0.0, 0.945), True),
        ('head_top', 'head',     (0.0, 0.0, 0.945), (0.0, 0.0, 1.000), True),
    ]
    b += arm('L') + arm('R') + leg('L') + leg('R')
    return b


def build_template(name, ext):
    """返回 (bones, space)。space: 'bbox' = x,y 归一化到 bbox；'height' = 全部按身高比例。"""
    if name == 'humanoid':
        w_over_h = float(ext.x) / max(float(ext.z), 1e-9)
        if w_over_h > 0.72:
            mode = 't'
        elif w_over_h > 0.45:
            mode = 'a'
        else:
            mode = 'down'
        return humanoid_bones(mode), 'height', 'arms=%s (bbox 宽/高=%.2f)' % (mode, w_over_h)
    return TEMPLATES[name], 'bbox', 'fixed bbox template'


TEMPLATES['quadruped'] = [
    ('root',       None,        (0.000, 0.350, 0.560), (0.000, 0.180, 0.600), False),
    ('spine_01',   'root',      (0.000, 0.180, 0.600), (0.000, 0.000, 0.620), True),
    ('spine_02',   'spine_01',  (0.000, 0.000, 0.620), (0.000, -0.180, 0.600), True),
    ('neck',       'spine_02',  (0.000, -0.180, 0.600), (0.000, -0.360, 0.720), True),
    ('head',       'neck',      (0.000, -0.360, 0.720), (0.000, -0.500, 0.800), True),
    ('tail_01',    'root',      (0.000, 0.350, 0.560), (0.000, 0.450, 0.640), False),
    ('tail_02',    'tail_01',   (0.000, 0.450, 0.640), (0.000, 0.500, 0.760), True),

    # 前腿（y 负方向）
    ('upper_front.L', 'spine_02', (0.180, -0.180, 0.560), (0.180, -0.190, 0.320), False),
    ('lower_front.L', 'upper_front.L', (0.180, -0.190, 0.320), (0.180, -0.180, 0.110), True),
    ('foot_front.L',  'lower_front.L', (0.180, -0.180, 0.110), (0.180, -0.270, 0.030), True),
    ('upper_front.R', 'spine_02', (-0.180, -0.180, 0.560), (-0.180, -0.190, 0.320), False),
    ('lower_front.R', 'upper_front.R', (-0.180, -0.190, 0.320), (-0.180, -0.180, 0.110), True),
    ('foot_front.R',  'lower_front.R', (-0.180, -0.180, 0.110), (-0.180, -0.270, 0.030), True),

    # 后腿（y 正方向）
    ('upper_hind.L', 'root',     (0.180, 0.250, 0.560), (0.180, 0.230, 0.320), False),
    ('lower_hind.L', 'upper_hind.L', (0.180, 0.230, 0.320), (0.180, 0.250, 0.110), True),
    ('foot_hind.L',  'lower_hind.L', (0.180, 0.250, 0.110), (0.180, 0.340, 0.030), True),
    ('upper_hind.R', 'root',     (-0.180, 0.250, 0.560), (-0.180, 0.230, 0.320), False),
    ('lower_hind.R', 'upper_hind.R', (-0.180, 0.230, 0.320), (-0.180, 0.250, 0.110), True),
    ('foot_hind.R',  'lower_hind.R', (-0.180, 0.250, 0.110), (-0.180, 0.340, 0.030), True),
]

TEMPLATES['aircraft'] = [
    ('fuselage_01', None,        (0.000, 0.500, 0.500), (0.000, 0.250, 0.540), False),
    ('fuselage_02', 'fuselage_01', (0.000, 0.250, 0.540), (0.000, 0.000, 0.540), True),
    ('fuselage_03', 'fuselage_02', (0.000, 0.000, 0.540), (0.000, -0.250, 0.520), True),
    ('tail',       'fuselage_03', (0.000, -0.250, 0.520), (0.000, -0.500, 0.560), True),

    ('wing_root.L', 'fuselage_02', (0.050, 0.050, 0.520), (0.220, 0.010, 0.520), False),
    ('wing_mid.L',  'wing_root.L', (0.220, 0.010, 0.520), (0.370, 0.000, 0.520), True),
    ('wing_tip.L',  'wing_mid.L',  (0.370, 0.000, 0.520), (0.500, 0.000, 0.520), True),
    ('wing_root.R', 'fuselage_02', (-0.050, 0.050, 0.520), (-0.220, 0.010, 0.520), False),
    ('wing_mid.R',  'wing_root.R', (-0.220, 0.010, 0.520), (-0.370, 0.000, 0.520), True),
    ('wing_tip.R',  'wing_mid.R',  (-0.370, 0.000, 0.520), (-0.500, 0.000, 0.520), True),

    ('tail_fin',    'tail',      (0.000, -0.360, 0.540), (0.000, -0.470, 0.960), False),
    ('tailplane.L', 'tail',      (0.020, -0.400, 0.540), (0.190, -0.430, 0.540), False),
    ('tailplane.R', 'tail',      (-0.020, -0.400, 0.540), (-0.190, -0.430, 0.540), False),
    ('nose',        'fuselage_01', (0.000, 0.500, 0.500), (0.000, 0.560, 0.500), True),
]


def pick_template(extents):
    """按 bbox 长宽高猜模板。返回 (name, reason)"""
    w, d, h = float(extents[0]), float(extents[1]), float(extents[2])
    flat = h / max(w, d)
    upright = h / max(w, d)
    if flat < 0.55:
        return 'aircraft', 'flat (h/max(x,y)=%.2f < 0.55) → aircraft/broad' % flat
    if upright >= 0.85:
        return 'humanoid', 'upright (h/max(x,y)=%.2f >= 0.85) → humanoid/biped' % upright
    return 'quadruped', 'elongated (0.55 <= h/max(x,y)=%.2f < 0.85) → quadruped' % upright


# ---------------------------------------------------------------- mesh io


def import_any(path):
    ext = os.path.splitext(path)[1].lower()
    log('import %s (%s)' % (path, ext))
    if ext == '.stl':
        if hasattr(bpy.ops.wm, 'stl_import'):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif ext == '.obj':
        if hasattr(bpy.ops.wm, 'obj_import'):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in ('.glb', '.gltf'):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == '.ply':
        if hasattr(bpy.ops.wm, 'ply_import'):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    elif ext == '.fbx':
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        raise SystemExit('[auto_rig] 不支持的格式: %s' % ext)

    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        raise SystemExit('[auto_rig] 导入后没有 mesh 对象')
    bpy.ops.object.select_all(action='DESELECT')
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = 'MESH_body'
    obj.data.name = 'MESH_body_mesh'
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def normalize_placement(obj):
    """XY 居中、Z 落地（底面 z=0），保持原始单位不缩放。"""
    me = obj.data
    xs = [v.co.x for v in me.vertices]
    ys = [v.co.y for v in me.vertices]
    zs = [v.co.z for v in me.vertices]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    zmin = min(zs)
    off = Vector((-cx, -cy, -zmin))
    for v in me.vertices:
        v.co += off
    me.update()
    bpy.context.view_layer.update()
    xs = [v.co.x for v in me.vertices]
    ys = [v.co.y for v in me.vertices]
    zs = [v.co.z for v in me.vertices]
    bb_min = Vector((min(xs), min(ys), min(zs)))
    bb_max = Vector((max(xs), max(ys), max(zs)))
    return bb_min, bb_max


def orient_horizontal(obj, mode='auto'):
    """把「水平方向最长的那根轴」转到 Y 上。

    模板骨架假设：四足/飞机 机体长轴沿 Y，人形竖直沿 Z。
    实测 rabbit 模型的前向是 X（侧对 -Y），不转的话四足脊柱会横穿身体宽度，
    导致 12/19 根骨被判定在体外、大幅位移 → 权重错、拉伸爆表。
    """
    if mode == 'none':
        return False, 'skipped (--orient none)'
    me = obj.data
    xs = [v.co.x for v in me.vertices]
    ys = [v.co.y for v in me.vertices]
    ext_x = max(xs) - min(xs)
    ext_y = max(ys) - min(ys)
    if ext_x <= ext_y * 1.15:
        return False, 'X(%.2f) <= Y(%.2f)*1.15, no rotation' % (ext_x, ext_y)
    # 绕 Z +90°: (x,y) -> (-y, x)
    for v in me.vertices:
        x, y = v.co.x, v.co.y
        v.co.x = -y
        v.co.y = x
    me.update()
    bpy.context.view_layer.update()
    return True, 'rotated +90deg about Z (X %.2f > Y %.2f)' % (ext_x, ext_y)


# ---------------------------------------------------------------- containment


def make_bvh(obj):
    from mathutils.bvhtree import BVHTree
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    tree = BVHTree.FromBMesh(bm)
    bm.free()
    return tree


def inside_test(tree, p):
    """水密网格：最近点法线和 (p-closest) 反向 => 内部"""
    res = tree.find_nearest(p)
    if res is None:
        return True, None, None
    loc, nor, idx, dist = res
    if nor is None or nor.length < 1e-9:
        return True, loc, nor
    return ((p - loc).dot(nor) < 0.0), loc, nor


def build_chain_template(obj, bb_min, bb_max, n_bones=10):
    """通用兜底模板：沿最长轴切 N 条带，取每条带的顶点重心连成一条骨链。

    为什么需要它：模板骨架的致命弱点是「姿势」。坐着的兔子、迈步的人形，
    任何对称模板都会把骨头放到空气里（实测人形 15/21 根骨在体外）。
    重心链永远在体内（近似中轴），所以对任意水密网格都不会撕裂 ——
    代价是没有语义（不是「左腿/右臂」，只是 chain_01..chain_10），
    适合「把雕塑掰一下」这种需求。
    """
    me = obj.data
    nv = len(me.vertices)
    co = [0.0] * (nv * 3)
    me.vertices.foreach_get('co', co)
    import numpy as np
    P = np.array(co, dtype=np.float64).reshape(nv, 3)

    ext = np.array([bb_max[i] - bb_min[i] for i in range(3)], dtype=np.float64)
    axis = int(np.argmax(ext))
    lo = float(np.array(bb_min)[axis])
    hi = float(np.array(bb_max)[axis])
    span = max(hi - lo, 1e-9)

    pts = []
    for i in range(n_bones + 1):
        a = lo + span * (i / float(n_bones))
        b = lo + span * ((i + 1) / float(n_bones))
        m = (P[:, axis] >= a) & (P[:, axis] <= b)
        if not m.any():
            # 空带：用整条轴上的最近点兜底
            pts.append(pts[-1] if pts else np.array([0.0, 0.0, lo + span * 0.5]))
            continue
        pts.append(P[m].mean(axis=0))

    bones = []
    for i in range(n_bones):
        nm = 'chain_%02d' % (i + 1)
        parent = None if i == 0 else 'chain_%02d' % i
        bones.append((nm, parent, tuple(pts[i]), tuple(pts[i + 1]), i > 0))
    return bones, 'world', 'slice-centroid chain along %s, %d bones' % ('xyz'[axis], n_bones)


def mesh_points_np(obj):
    import numpy as np
    me = obj.data
    nv = len(me.vertices)
    co = [0.0] * (nv * 3)
    me.vertices.foreach_get('co', co)
    return np.array(co, dtype=np.float64).reshape(nv, 3)


def snap_legs(joints, obj, bb_min, bb_max):
    """人形模板的腿链按「z 分带 + 左右半空间重心」吸附到真实腿骨位置。

    解决迈步/蹲姿：对称模板的腿落在两腿之间的空气里（实测 human 15/21 根骨在体外、
    撕裂 12.2%）。Mesh 是迈步姿势时，左腿仍然在 +x 半空间、右腿在 -x 半空间，
    所以按「高度带 × x 正负」取重心就能拿到每条腿真实的中轴。
    返回每侧腿的吸附情况，失败（带内没点）就保留模板原值。
    """
    import numpy as np
    P = mesh_points_np(obj)
    H = float(bb_max.z - bb_min.z)
    z = P[:, 2]
    x = P[:, 0]
    info = {}
    # (z 带中心, 半宽) —— 髋、膝、踝
    bands = {'hip': (0.475, 0.035), 'knee': (0.270, 0.035), 'ankle': (0.065, 0.035)}
    for side, sgn in (('L', 1.0), ('R', -1.0)):
        cents = {}
        for key, (zc, hw) in bands.items():
            m = (np.abs(z - zc * H) <= hw * H) & ((x * sgn) > 0.0)
            if m.sum() < 20:
                cents[key] = None
                continue
            cents[key] = Vector((float(P[m, 0].mean()), float(P[m, 1].mean()),
                                 float(P[m, 2].mean())))
        info[side] = {k: (None if v is None else [round(v.x, 4), round(v.y, 4), round(v.z, 4)])
                      for k, v in cents.items()}
        if cents['hip'] is None or cents['knee'] is None or cents['ankle'] is None:
            info[side]['applied'] = False
            continue
        th, sh, ft = 'thigh.' + side, 'shin.' + side, 'foot.' + side
        if th in joints and sh in joints and ft in joints:
            joints[th]['head'] = cents['hip']
            joints[th]['tail'] = cents['knee']
            joints[sh]['head'] = cents['knee']
            joints[sh]['tail'] = cents['ankle']
            joints[ft]['head'] = cents['ankle']
            joints[ft]['tail'] = cents['ankle'] + Vector((0.0, -0.075 * H, -0.036 * H))
            info[side]['applied'] = True
        else:
            info[side]['applied'] = False
    return info


def fit_joints_one(tree, p, height, core_x=0.0, core_y=0.0):
    """把单个接头点拉回体内，返回 (newp, moved_dist)。p 已在体内则原样返回。

    为什么不能「投影到最近表面」：异形网格上模板的肢端常在空气中，
    最近表面可能是耳朵/翅膀尖 —— 投影过去会让手臂骨抓住耳朵，
    拉伸时耳朵跟着手臂飞（实测 max 拉伸 26x）。改成「朝身体中轴收」：
    沿 (p → 同高度竖直中轴) 方向二分找第一个体内点，再往里推一半。
    """
    eps = 0.01 * height
    if inside_test(tree, p)[0]:
        return p, 0.0
    q = Vector((core_x, core_y, p.z))
    if inside_test(tree, q)[0]:
        lo, hi = 0.0, 1.0
        t_in = None
        for _ in range(16):
            mid = 0.5 * (lo + hi)
            if inside_test(tree, p.lerp(q, mid))[0]:
                t_in = mid
                hi = mid
            else:
                lo = mid
        if t_in is not None:
            t_final = min(1.0, 0.5 * t_in + 0.5)
            np_ = p.lerp(q, t_final)
            return np_, (np_ - p).length
    res = tree.find_nearest(p)
    if res is not None and res[0] is not None and res[1] is not None and res[1].length > 1e-9:
        np_ = res[0] - res[1].normalized() * eps
        return np_, (np_ - p).length
    return p, 0.0


def fit_joints(template, bb_min, bb_max, tree, height, space='bbox'):
    """把模板映射到网格，并把落在网格外的接头拉回体内。

    为什么不能「投影到最近表面」：异形网格上模板的肢端常在空气中，
    最近表面可能是耳朵/翅膀尖 —— 投影过去会让手臂骨抓住耳朵，
    拉伸时耳朵跟着手臂飞（实测 max 拉伸 26x）。改成「朝身体中轴收」：
    沿 (p → 竖直中轴) 方向二分找第一个体内点，再往里推一半。
    """
    ext = bb_max - bb_min
    eps = 0.01 * height

    def to_world(n):
        if space == 'world':
            return Vector((float(n[0]), float(n[1]), float(n[2])))
        if space == 'height':
            # 全部按身高比例（人形）
            H = float(ext.z)
            return Vector((n[0] * H, n[1] * H, bb_min.z + n[2] * H))
        return Vector((
            bb_min.x + (n[0] + 0.5) * ext.x,
            bb_min.y + (n[1] + 0.5) * ext.y,
            bb_min.z + n[2] * ext.z,
        ))

    def pull_inside(p):
        return fit_joints_one(tree, p, height)

    joints = {}
    for name, parent, h, t, conn in template:
        joints[name] = {'parent': parent,
                        'head': to_world(h),
                        'tail': to_world(t),
                        'connected': conn,
                        'moved_head': 0.0, 'moved_tail': 0.0,
                        'outside_head': False, 'outside_tail': False}
    bpy.context.view_layer.update()

    for _ in range(3):
        changed = 0
        for name, j in joints.items():
            for key in ('head', 'tail'):
                p = j[key]
                ok = inside_test(tree, p)[0]
                if not ok:
                    j['outside_' + key] = True
                    np_, dist = pull_inside(p)
                    j[key] = np_
                    j['moved_' + key] += dist
                    changed += 1
        if changed == 0:
            break
        bpy.context.view_layer.update()

    for name, j in joints.items():
        d = j['tail'] - j['head']
        if d.length < 1e-6:
            j['tail'] = j['head'] + Vector((0, 0, eps))
    return joints


def bone_inside_ratio(joints, template, tree, samples=24):
    """每根骨沿长度采样，统计多少比例的采样点在网格内部（骨架质量指标）。"""
    out = {}
    for name, parent, h, t, conn in template:
        j = joints[name]
        a, b = j['head'], j['tail']
        n_in = 0
        for i in range(samples):
            p = a.lerp(b, (i + 0.5) / samples)
            if inside_test(tree, p)[0]:
                n_in += 1
        out[name] = round(n_in / float(samples), 3)
    return out


# ---------------------------------------------------------------- armature


def build_armature(joints, template, name='RIG'):
    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    ebs = {}
    for bname, parent, h, t, conn in template:
        j = joints[bname]
        eb = arm_data.edit_bones.new(bname)
        eb.head = j['head']
        eb.tail = j['tail']
        eb.roll = 0.0
        ebs[bname] = eb
    for bname, parent, h, t, conn in template:
        if parent:
            ebs[bname].parent = ebs[parent]
            # 只对模板显式标注 connect 且父 tail ≈ 子 head 时才连通
            if conn:
                ebs[bname].use_connect = (ebs[bname].head - ebs[parent].tail).length < 1e-4
    bpy.ops.object.mode_set(mode='OBJECT')
    log('armature: %d bones' % len(arm_data.bones))
    return arm_obj


# ---------------------------------------------------------------- weighting


def calc_weights_heat(mesh_obj, arm_obj):
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    err = None
    try:
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')
    except Exception as e:
        err = '%s: %s' % (type(e).__name__, e)
        log('!! ARMATURE_AUTO 失败: %s' % err)
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = mesh_obj
    return err


def segment_distances(verts_np, heads, tails):
    """verts_np (N,3), heads/tails (B,3) -> (N,B) 点到骨段距离"""
    import numpy as np
    N = verts_np.shape[0]
    B = heads.shape[0]
    out = np.empty((N, B), dtype=np.float32)
    for b in range(B):
        a = heads[b]
        bvec = tails[b] - a
        L2 = float(np.dot(bvec, bvec))
        if L2 < 1e-12:
            d = verts_np - a
            out[:, b] = np.sqrt((d * d).sum(axis=1))
            continue
        ap = verts_np - a
        t = (ap @ bvec) / L2
        t = np.clip(t, 0.0, 1.0)
        proj = a + t[:, None] * bvec
        d = verts_np - proj
        out[:, b] = np.sqrt((d * d).sum(axis=1))
    return out


def calc_weights_nearest(mesh_obj, arm_obj, top_k=3, power=4.0):
    """最近骨段反距离权重（兜底/补漏）。返回 (N,) 每顶点是否被赋值"""
    import numpy as np
    me = mesh_obj.data
    verts = np.empty((len(me.vertices), 3), dtype=np.float32)
    me.vertices.foreach_get('co', verts.ravel())

    bones = [b for b in arm_obj.data.bones]
    heads = np.array([tuple(b.head_local) for b in bones], dtype=np.float32)
    tails = np.array([tuple(b.tail_local) for b in bones], dtype=np.float32)
    D = segment_distances(verts, heads, tails)

    k = min(top_k, D.shape[1])
    order = np.argsort(D, axis=1)[:, :k]
    # 反距离幂权重，距离加小量防 0
    dsel = np.take_along_axis(D, order, axis=1)
    w = 1.0 / np.power(dsel + 1e-6, power)
    w = w / w.sum(axis=1, keepdims=True)

    for b in bones:
        if b.name not in mesh_obj.vertex_groups:
            mesh_obj.vertex_groups.new(name=b.name)
    # 清空后重写
    for b in bones:
        vg = mesh_obj.vertex_groups[b.name]
        vg.remove(range(len(me.vertices)))
    for j in range(k):
        vgj = [mesh_obj.vertex_groups[bones[int(order[i, j])].name] for i in range(len(me.vertices))]
        # 逐顶点写太慢，按骨分桶
        buckets = {}
        for i in range(len(me.vertices)):
            nm = bones[int(order[i, j])].name
            buckets.setdefault(nm, []).append((i, float(w[i, j])))
        for nm, items in buckets.items():
            vg = mesh_obj.vertex_groups[nm]
            for i, ww in items:
                vg.add([i], ww, 'REPLACE')
    return True


def fill_zero_weights(mesh_obj, arm_obj, power=4.0, top_k=2, exclude=None):
    """只给零权重顶点补最近骨权重，返回补了多少个。exclude=不许参与分配的骨名集合"""
    import numpy as np
    me = mesh_obj.data
    excl = set(exclude or ())
    zero_idx = []
    for v in me.vertices:
        if not any(g.weight > 1e-4 for g in v.groups):
            zero_idx.append(v.index)
    if not zero_idx:
        return 0

    bones = [b for b in arm_obj.data.bones if b.name not in excl]
    if not bones:
        return 0
    heads = np.array([tuple(b.head_local) for b in bones], dtype=np.float32)
    tails = np.array([tuple(b.tail_local) for b in bones], dtype=np.float32)

    verts = np.empty((len(me.vertices), 3), dtype=np.float32)
    me.vertices.foreach_get('co', verts.ravel())
    sub = verts[zero_idx]
    D = segment_distances(sub, heads, tails)
    k = min(top_k, D.shape[1])
    order = np.argsort(D, axis=1)[:, :k]
    dsel = np.take_along_axis(D, order, axis=1)
    w = 1.0 / np.power(dsel + 1e-6, power)
    w = w / w.sum(axis=1, keepdims=True)

    for b in bones:
        if b.name not in mesh_obj.vertex_groups:
            mesh_obj.vertex_groups.new(name=b.name)
    for j in range(k):
        buckets = {}
        for r, vi in enumerate(zero_idx):
            nm = bones[int(order[r, j])].name
            ww = float(w[r, j])
            if ww < 1e-4:
                continue
            buckets.setdefault(nm, []).append((vi, ww))
        for nm, items in buckets.items():
            vg = mesh_obj.vertex_groups[nm]
            for vi, ww in items:
                vg.add([vi], ww, 'REPLACE')
    return len(zero_idx)


def smooth_weights(mesh_obj, repeat=2, factor=0.5):
    """权重平滑：热扩散出来的权重在骨边界会有硬跳变（拉伸比值爆表的元凶）。
    vertex_group_smooth 做几轮邻域平均能显著压平断层。"""
    if repeat <= 0:
        return 0
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    if mesh_obj.vertex_groups:
        mesh_obj.vertex_groups.active_index = 0
    try:
        # 必须进 WEIGHT_PAINT 才满足 poll()
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        bpy.ops.object.vertex_group_smooth(group_select_mode='ALL', factor=factor,
                                          repeat=repeat, expand=0.0)
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception as e:
        log('!! 权重平滑失败: %s: %s' % (type(e).__name__, e))
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
        return 0
    return repeat


def snapshot_weights(mesh_obj):
    """把当前权重存成 [(gname, w), ...] 每顶点一份，用于伪骨清理失败时回滚。"""
    gname = {g.index: g.name for g in mesh_obj.vertex_groups}
    snap = []
    for v in mesh_obj.data.vertices:
        snap.append([(gname[g.group], g.weight) for g in v.groups if g.weight > 0.0])
    return snap


def restore_weights(mesh_obj, snap):
    n = len(mesh_obj.data.vertices)
    for vg in mesh_obj.vertex_groups:
        vg.remove(range(n))
    for i, items in enumerate(snap):
        for nm, w in items:
            mesh_obj.vertex_groups[nm].add([i], w, 'REPLACE')


def measure_tearing(moderate_scale, obj, arm, rest_np, height):
    """摆一次温和姿势量撕裂，量完复位。用于「先试后定」。"""
    reset_pose(arm)
    apply_test_pose(arm, scale=moderate_scale)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    posed = deformed_verts(obj, dg)
    reset_pose(arm)
    bpy.context.view_layer.update()
    st = edge_stretch_stats(obj, rest_np, posed, height)
    return {'stretch_p99': st['stretch_p99'],
            'edges_over_1_5x_pct': st['edges_over_1_5x_pct'],
            'elong_max_frac_height': st['elong_max_frac_height']}


def is_worse(new, old, rel=0.10, abs_pct=0.05):
    """new 比 old 明显更差？主看 >1.5x 边占比，其次 p99。"""
    if not old or not new:
        return False
    e0, e1 = old.get('edges_over_1_5x_pct') or 0.0, new.get('edges_over_1_5x_pct') or 0.0
    if e1 > max(e0 * (1.0 + rel), e0 + abs_pct):
        return True
    p0, p1 = old.get('stretch_p99') or 0.0, new.get('stretch_p99') or 0.0
    if p1 > p0 * (1.0 + rel):
        return True
    return False


def prune_spurious_bones(mesh_obj, arm_obj, joints, share_thr=0.25, reloc_thr=0.5):
    """把「模板里有、网格里没有」的伪骨权重清掉，权重按最近**有效**骨重算。

    判定必须用【合取】，单看任一条都会误杀：
      - 只看 share（影响顶点占比）：chain_07..11 占 30~52%（链式骨架本来就把身体切成几大块），
        飞机的 fuselage_03 34%、wing_root.L 35% —— 全是**真实**骨，会被误删。
      - 只看 relocated（接头被拉回体内的距离）：飞机的 tail_fin 位移 28 单位 / 骨长 21.7 = 129%，
        foot.L 151% —— 但它们 share 很小，不是元凶。
    真伪骨的特征是【**又大又飘**】：抢的顶点多（> share_thr），而且整根骨本来就不在体内
    （head+tail 位移 > reloc_thr × 骨长）。human 的 upper_arm.L/R、forearm.R 正好符合
    （share 37/45/28%，位移 97%/128%/193%），而 chain 骨位移恒为 0、飞机翅膀位移只占 11~20%。

    处理方式**不是**并给父骨（父骨会瞬间变成 74%，制造一个假 rig），而是：
      清掉伪骨权重 → 剩余权重重归一 → 一个都不剩的顶点，按「最近有效骨段反距离」重算。
    """
    me = mesh_obj.data
    nv = len(me.vertices)
    gname = {g.index: g.name for g in mesh_obj.vertex_groups}

    # 每骨影响顶点数
    per = {}
    for v in me.vertices:
        for g in v.groups:
            if g.weight > 1e-4:
                nm = gname.get(g.group)
                if nm:
                    per[nm] = per.get(nm, 0) + 1

    flagged = {}
    for b in arm_obj.data.bones:
        nm = b.name
        cnt = per.get(nm, 0)
        if cnt == 0:
            continue
        blen = (Vector(b.tail_local) - Vector(b.head_local)).length
        if blen < 1e-9:
            continue
        j = joints.get(nm)
        moved = (j['moved_head'] + j['moved_tail']) if j else 0.0
        share = cnt / float(nv)
        reloc = moved / blen
        if share > share_thr and reloc > reloc_thr:
            flagged[nm] = {'share': round(share, 4), 'reloc_frac': round(reloc, 4),
                           'verts': cnt}

    if not flagged:
        return {'flagged': {}, 'verts_pruned': 0, 'verts_reassigned': 0}

    # 沿骨链下传：伪骨的子骨（前臂/手）同样不存在，否则它们还会拖着躯干拉丝
    # （human 上只删 upper_arm 而留着 hand.R，撕裂卡在 3.5% 不掉）
    changed = True
    while changed:
        changed = False
        for b in arm_obj.data.bones:
            if b.name in flagged or b.parent is None:
                continue
            if b.parent.name in flagged:
                cnt = per.get(b.name, 0)
                blen = (Vector(b.tail_local) - Vector(b.head_local)).length
                j = joints.get(b.name)
                moved = (j['moved_head'] + j['moved_tail']) if j else 0.0
                flagged[b.name] = {
                    'share': round(cnt / float(nv), 4),
                    'reloc_frac': round(moved / blen, 4) if blen > 1e-9 else 0.0,
                    'verts': cnt,
                    'via_parent': b.parent.name,
                }
                changed = True

    # 清权重
    fset = set(flagged.keys())
    need_reassign = []
    n_pruned = 0
    for v in me.vertices:
        gs = [(g.group, g.weight) for g in v.groups if g.weight > 1e-6]
        if not gs:
            continue
        keep = [(gid, w) for gid, w in gs if gname.get(gid) not in fset]
        if len(keep) == len(gs):
            continue
        n_pruned += 1
        s = sum(w for _g, w in keep)
        for gid, w in gs:
            if gname.get(gid) in fset:
                mesh_obj.vertex_groups[gname[gid]].remove([v.index])
        if s > 1e-6:
            for gid, w in keep:
                mesh_obj.vertex_groups[gname[gid]].add([v.index], w / s, 'REPLACE')
        else:
            need_reassign.append(v.index)

    # 被清空的顶点：只在「有效骨」里找最近骨段
    n_re = 0
    if need_reassign:
        import numpy as np
        bones = [b for b in arm_obj.data.bones if b.name not in fset]
        if bones:
            heads = np.array([tuple(b.head_local) for b in bones], dtype=np.float32)
            tails = np.array([tuple(b.tail_local) for b in bones], dtype=np.float32)
            co = [0.0] * (nv * 3)
            me.vertices.foreach_get('co', co)
            P = np.array(co, dtype=np.float64).reshape(nv, 3)
            sub = P[need_reassign]
            D = segment_distances(sub, heads, tails)
            k = min(2, D.shape[1])
            order = np.argsort(D, axis=1)[:, :k]
            dsel = np.take_along_axis(D, order, axis=1)
            w = 1.0 / np.power(dsel + 1e-6, 4.0)
            w = w / w.sum(axis=1, keepdims=True)
            for jj in range(k):
                buckets = {}
                for r, vi in enumerate(need_reassign):
                    nm = bones[int(order[r, jj])].name
                    buckets.setdefault(nm, []).append((vi, float(w[r, jj])))
                for nm, items in buckets.items():
                    vg = mesh_obj.vertex_groups[nm]
                    for vi, ww in items:
                        vg.add([vi], ww, 'REPLACE')
            n_re = len(need_reassign)

    return {'flagged': flagged, 'verts_pruned': n_pruned, 'verts_reassigned': n_re,
            'share_thr': share_thr, 'reloc_thr': reloc_thr}


def limit_influences(mesh_obj, max_inf=4):
    """每顶点最多 max_inf 根骨，并重归一化（glTF 规范）"""
    me = mesh_obj.data
    groups = list(mesh_obj.vertex_groups)
    gname = {g.index: g.name for g in groups}
    trimmed = 0
    for v in me.vertices:
        gs = sorted([(g.group, g.weight) for g in v.groups if g.weight > 1e-5],
                    key=lambda x: -x[1])
        if len(gs) > max_inf:
            trimmed += 1
            for gid, _w in gs[max_inf:]:
                mesh_obj.vertex_groups[gname[gid]].remove([v.index])
            gs = gs[:max_inf]
        s = sum(w for _g, w in gs)
        if s > 0 and abs(s - 1.0) > 1e-4:
            for gid, w in gs:
                mesh_obj.vertex_groups[gname[gid]].add([v.index], w / s, 'REPLACE')
    return trimmed


# ---------------------------------------------------------------- metrics


def weight_metrics(mesh_obj, arm_obj):
    me = mesh_obj.data
    nv = len(me.vertices)
    gname = {g.index: g.name for g in mesh_obj.vertex_groups}
    per_bone = {b.name: 0 for b in arm_obj.data.bones}
    zero = 0
    maxinf = 0
    suminf = 0
    for v in me.vertices:
        gs = [g for g in v.groups if g.weight > 1e-4]
        if not gs:
            zero += 1
            continue
        if len(gs) > maxinf:
            maxinf = len(gs)
        suminf += len(gs)
        for g in gs:
            nm = gname.get(g.group)
            if nm in per_bone:
                per_bone[nm] += 1
    avg = (suminf / float(nv - zero)) if nv > zero else 0.0
    unused = [k for k, v in per_bone.items() if v == 0]
    return {
        'verts': nv,
        'verts_zero_weight': zero,
        'zero_weight_pct': round(100.0 * zero / max(nv, 1), 4),
        'max_influences': maxinf,
        'avg_influences': round(avg, 3),
        'per_bone_vert_count': per_bone,
        'bones_without_influence': unused,
    }


def deformed_verts(obj, depsgraph):
    import numpy as np
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    n = len(me.vertices)
    arr = np.empty((n, 3), dtype=np.float64)
    me.vertices.foreach_get('co', arr.ravel())
    ev.to_mesh_clear()
    return arr


def edge_stretch_stats(mesh_obj, rest_np, posed_np, height):
    """撕裂/拉伸检测。

    关键坑：STL 重建网格常有大量极短边（退化边），短边稍微一动边长比就爆表，
    会把 max/p99 完全污染。所以分两套统计：
      - all：全部边
      - valid：rest 边长 >= 1e-3 * 高度 的边（有意义的那批）
    另外给「绝对伸长量 / 模型高度」，这个才是肉眼看到撕裂的程度。
    """
    import numpy as np
    me = mesh_obj.data
    ne = len(me.edges)
    e = np.empty((ne, 2), dtype=np.int32)
    me.edges.foreach_get('vertices', e.ravel())
    l0 = np.linalg.norm(rest_np[e[:, 0]] - rest_np[e[:, 1]], axis=1)
    l1 = np.linalg.norm(posed_np[e[:, 0]] - posed_np[e[:, 1]], axis=1)

    ok = l0 > 1e-9
    ratio_all = np.where(ok, l1 / np.maximum(l0, 1e-12), 1.0)
    elong_all = l1 - l0

    tiny_thr = max(1e-3 * height, 1e-9)
    valid = l0 >= tiny_thr
    ratio_v = ratio_all[valid]
    elong_v = elong_all[valid]

    def pct(a, q):
        if a.size == 0:
            return None
        return round(float(np.percentile(a, q)), 4)

    # 最夸张的 10 条边，带位置（归一化到 bbox）
    worst = np.argsort(-ratio_all)[:10]
    rest_mid = 0.5 * (rest_np[e[worst, 0]] + rest_np[e[worst, 1]])
    worst_list = []
    for k, ei in enumerate(worst):
        worst_list.append({
            'ratio': round(float(ratio_all[ei]), 3),
            'rest_len_frac_height': round(float(l0[ei]) / max(height, 1e-9), 6),
            'elong_frac_height': round(float(elong_all[ei]) / max(height, 1e-9), 6),
            'pos_z_frac': round(float(rest_mid[k][2]) / max(height, 1e-9), 4),
            'pos_x_frac': round(float(rest_mid[k][0]) / max(height, 1e-9), 4),
        })

    return {
        'edges_total': int(ne),
        'edges_degenerate_lt_1e-3h': int((~valid).sum()),
        'edges_valid': int(valid.sum()),
        # 全部边
        'all_stretch_max': round(float(ratio_all.max()), 4) if ne else None,
        'all_edges_over_2x': int((ratio_all > 2.0).sum()),
        # 有效边
        'stretch_max': round(float(ratio_v.max()), 4) if ratio_v.size else None,
        'stretch_p99': pct(ratio_v, 99),
        'stretch_p999': pct(ratio_v, 99.9),
        'stretch_mean': round(float(ratio_v.mean()), 4) if ratio_v.size else None,
        'edges_over_1_5x': int((ratio_v > 1.5).sum()),
        'edges_over_2_0x': int((ratio_v > 2.0).sum()),
        'edges_over_3_0x': int((ratio_v > 3.0).sum()),
        'edges_over_1_5x_pct': round(100.0 * float((ratio_v > 1.5).sum()) / max(ratio_v.size, 1), 4),
        # 绝对伸长（占模型高度比例），肉眼撕裂程度
        'elong_max_frac_height': round(float(elong_v.max()) / max(height, 1e-9), 5) if elong_v.size else None,
        'elong_p999_frac_height': round(float(np.percentile(elong_v, 99.9)) / max(height, 1e-9), 5) if elong_v.size else None,
        'edges_elong_over_1pct_height': int((elong_v > 0.01 * height).sum()),
        'worst_edges': worst_list,
    }


# ---------------------------------------------------------------- posing

POSE_RULES = [
    # (关键词, 轴, 角度deg)
    ('upper_arm', 0, -62.0),
    ('forearm', 1, 55.0),
    ('hand', 1, 18.0),
    ('clavicle', 1, -12.0),
    ('thigh', 0, 42.0),
    ('shin', 0, -68.0),
    ('foot', 0, 22.0),
    ('upper_front', 0, 38.0),
    ('lower_front', 0, -55.0),
    ('foot_front', 0, 20.0),
    ('upper_hind', 0, -34.0),
    ('lower_hind', 0, 52.0),
    ('foot_hind', 0, -18.0),
    ('wing_mid', 1, 26.0),
    ('wing_tip', 1, 34.0),
    ('wing_root', 1, -18.0),
    ('tail_fin', 0, 22.0),
    ('tailplane', 0, 30.0),
    ('neck', 0, 14.0),
    ('head', 2, 26.0),
    ('spine_01', 1, 8.0),
    ('spine_02', 1, 10.0),
    ('fuselage_02', 0, 14.0),
]


def apply_test_pose(arm_obj, scale=1.0):
    """按关键词给骨骼加旋转，返回改了哪些骨。左右不对称（L 正 / R 负）更利于看撕裂。"""
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='POSE')
    touched = []
    for pb in arm_obj.pose.bones:
        nm = pb.name
        sign = 1.0
        if nm.endswith('.R'):
            sign = -1.0
        best = None
        for kw, axis, ang in POSE_RULES:
            if kw in nm:
                best = (kw, axis, ang)
                break
        if best is None and nm.startswith('chain_'):
            idx = int(nm.split('_')[-1])
            best = ('chain', 0, 16.0 if idx % 2 else -9.0)
            sign = 1.0
        if best is None:
            pb.rotation_mode = 'QUATERNION'
            continue
        kw, axis, ang = best
        ang = ang * scale * (sign if axis == 0 else 1.0)
        pb.rotation_mode = 'XYZ'
        eul = [0.0, 0.0, 0.0]
        eul[axis] = math.radians(ang)
        pb.rotation_euler = eul
        touched.append((nm, kw, axis, round(ang, 1)))
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.update()
    return touched


def reset_pose(arm_obj):
    for b in arm_obj.pose.bones:
        b.rotation_mode = 'QUATERNION'
        b.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        b.location = (0.0, 0.0, 0.0)
        b.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()


# ---------------------------------------------------------------- render


def octa_bone_geometry(pairs, radius_scale=0.055, ring_at=0.10, min_r=None):
    """pairs: [(name,(head,tail))] -> (verts, faces) 八面体骨骼几何

    参考图（outputs/ref/character/rig/*_bone_*.png）就是 Blender 八面体骨骼，
    形状 = 头顶尖点 + 靠近 head 的一圈方环 + 尾部尖点。
    radius_scale 太大/自发光太强会糊成白楔子，实测 0.055 + strength 1.8 才像参考图。
    """
    verts = []
    faces = []
    for _nm, (h, t) in pairs:
        h = Vector(h)
        t = Vector(t)
        d = t - h
        L = d.length
        if L < 1e-6:
            continue
        ax = d.normalized()
        up = Vector((0.0, 0.0, 1.0))
        if abs(ax.dot(up)) > 0.95:
            up = Vector((0.0, 1.0, 0.0))
        e1 = ax.cross(up).normalized()
        e2 = ax.cross(e1).normalized()
        r = radius_scale * L
        if min_r:
            r = max(r, min_r)
        ring = []
        for k in range(4):
            ang = math.pi * 0.5 * k + math.pi * 0.25
            ring.append(h + ax * (ring_at * L) + e1 * (r * math.cos(ang)) + e2 * (r * math.sin(ang)))
        base = len(verts)
        verts.append(tuple(h))
        for p in ring:
            verts.append(tuple(p))
        verts.append(tuple(t))
        for k in range(4):
            a = 1 + k
            b = 1 + (k + 1) % 4
            faces.append((base + 0, base + b, base + a))
            faces.append((base + 5, base + a, base + b))
    return verts, faces


def make_emission_mat(name, color, strength):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    em = nt.nodes.new('ShaderNodeEmission')
    em.inputs['Color'].default_value = (color[0], color[1], color[2], 1.0)
    em.inputs['Strength'].default_value = strength
    nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
    return mat


def make_xray_mat(name, color, alpha):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.inputs['Base Color'].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs['Alpha'].default_value = alpha
    bsdf.inputs['Roughness'].default_value = 0.35
    if 'Specular IOR Level' in bsdf.inputs:
        bsdf.inputs['Specular IOR Level'].default_value = 0.35
    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


def make_solid_mat(name, color, rough=0.55):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.inputs['Base Color'].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs['Roughness'].default_value = rough
    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


def view_spans(bb_min, bb_max, az_deg, el_deg):
    """把 bbox 8 个角投影到相机 right/up 轴，返回 (横向跨度, 纵向跨度)。"""
    center = (bb_min + bb_max) * 0.5
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    dirv = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    right = Vector((math.cos(az), math.sin(az), 0.0))
    up = right.cross(dirv).normalized()
    span_r = 0.0
    span_u = 0.0
    for sx in (bb_min.x, bb_max.x):
        for sy in (bb_min.y, bb_max.y):
            for sz in (bb_min.z, bb_max.z):
                p = Vector((sx, sy, sz)) - center
                span_r = max(span_r, abs(p.dot(right)))
                span_u = max(span_u, abs(p.dot(up)))
    return span_r * 2.0, span_u * 2.0


def aim_camera(cam, bb_min, bb_max, az_deg=0.0, el_deg=0.0, res=(1000, 1400), margin=1.10):
    """正交相机按 bbox 精确取景（把 8 个角投影到相机 right/up 轴上取跨度）。"""
    ext = bb_max - bb_min
    center = (bb_min + bb_max) * 0.5
    diag = max(float(ext.length), 1e-6)
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    dirv = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    cam.location = center + dirv * (diag * 3.0)
    cam.rotation_euler = (math.pi * 0.5 - el, 0.0, az)

    span_r, span_u = view_spans(bb_min, bb_max, az_deg, el_deg)
    rx, ry = res
    need = max(span_u, span_r * float(ry) / float(rx))
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = max(need * margin, 1e-6)


def setup_scene(bb_min, bb_max, res=(1000, 1400), samples=48, bg=(0.010, 0.014, 0.026)):
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    sc.cycles.device = 'CPU'
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 4
    sc.cycles.transparent_max_bounces = 8
    sc.render.resolution_x, sc.render.resolution_y = res
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = False
    try:
        sc.view_settings.view_transform = 'Filmic'
    except Exception:
        sc.view_settings.view_transform = 'Standard'
    sc.view_settings.look = 'None'

    world = bpy.data.worlds.new('W') if not sc.world else sc.world
    sc.world = world
    world.use_nodes = True
    bgn = world.node_tree.nodes.get('Background')
    if bgn:
        bgn.inputs['Color'].default_value = (bg[0], bg[1], bg[2], 1.0)
        bgn.inputs['Strength'].default_value = 1.0

    ext = bb_max - bb_min
    center = (bb_min + bb_max) * 0.5
    cam_data = bpy.data.cameras.new('CAM')
    cam = bpy.data.objects.new('CAM', cam_data)
    bpy.context.collection.objects.link(cam)
    sc.camera = cam
    aim_camera(cam, bb_min, bb_max, 0.0, 0.0, res)

    # 柔光（只为让半透/实心网格有体积感，功率按场景尺寸缩放）
    light_data = bpy.data.lights.new('KEY', type='AREA')
    L = max(float(ext.length), 1e-3)
    light_data.energy = 8.0 * L * L
    light_data.size = max(ext.x, ext.z) * 1.5
    light = bpy.data.objects.new('KEY', light_data)
    bpy.context.collection.objects.link(light)
    light.location = (center.x - ext.x * 1.2, bb_min.y - ext.y * 1.6, center.z + ext.z * 1.2)
    light.rotation_euler = (math.radians(58.0), 0.0, math.radians(-32.0))

    # 合成器辉光
    sc.use_nodes = True
    nt = sc.node_tree
    nt.nodes.clear()
    rl = nt.nodes.new('CompositorNodeRLayers')
    gl = nt.nodes.new('CompositorNodeGlare')
    gl.glare_type = 'FOG_GLOW'
    gl.quality = 'HIGH'
    gl.threshold = 1.0
    gl.size = 6
    comp = nt.nodes.new('CompositorNodeComposite')
    nt.links.new(rl.outputs['Image'], gl.inputs['Image'])
    nt.links.new(gl.outputs['Image'], comp.inputs['Image'])
    return cam


def build_bone_object(pairs, mat, name='BONES', min_r=None):
    v, f = octa_bone_geometry(pairs, min_r=min_r)
    me = bpy.data.meshes.new(name + '_mesh')
    me.from_pydata(v, [], f)
    me.validate()
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(mat)
    return ob


def render_to(path, samples=None):
    sc = bpy.context.scene
    if samples:
        sc.cycles.samples = samples
    sc.render.filepath = path
    sc.render.image_settings.file_format = 'PNG'
    t = time.time()
    bpy.ops.render.render(write_still=True)
    log('render %.1fs -> %s' % (time.time() - t, path))


# ---------------------------------------------------------------- main


def parse_args():
    argv = sys.argv
    if '--' in argv:
        argv = argv[argv.index('--') + 1:]
    else:
        argv = []
    ap = argparse.ArgumentParser(prog='auto_rig.py')
    ap.add_argument('mesh')
    ap.add_argument('--out', default=None)
    ap.add_argument('--template', default='auto',
                    choices=['auto', 'humanoid', 'quadruped', 'aircraft', 'chain'])
    ap.add_argument('--chain-bones', type=int, default=10)
    ap.add_argument('--snap-limbs', default='auto', choices=['auto', 'legs', 'none'],
                    help='auto=人形模板才做腿链切片吸附')
    ap.add_argument('--prune-spurious', default='auto', choices=['auto', 'always', 'none'],
                    help='auto=非 chain 模板才清理「又大又飘」的伪骨')
    ap.add_argument('--prune-share', type=float, default=0.25,
                    help='伪骨判据之一：影响顶点数占全网格比例 > 该值')
    ap.add_argument('--prune-reloc', type=float, default=0.5,
                    help='伪骨判据之二：整骨被拉回体内的距离 > 该值×骨长')
    ap.add_argument('--resmooth', type=int, default=2,
                    help='伪骨清理后重平滑轮数，0=关')
    ap.add_argument('--weight', default='auto', choices=['auto', 'heat', 'nearest'])
    ap.add_argument('--orient', default='auto', choices=['auto', 'none'])
    ap.add_argument('--smooth', type=int, default=2, help='权重平滑轮数，0=关')
    ap.add_argument('--samples', type=int, default=48)
    ap.add_argument('--res', default='1000x1400')
    ap.add_argument('--no-render', action='store_true')
    ap.add_argument('--tag', default=None)
    return ap.parse_args(argv)


def main():
    args = parse_args()
    mesh_path = os.path.abspath(args.mesh)
    if not os.path.exists(mesh_path):
        raise SystemExit('[auto_rig] 输入不存在: %s' % mesh_path)
    base = os.path.splitext(os.path.basename(mesh_path))[0]
    tag = args.tag or base
    outdir = os.path.abspath(args.out or os.path.join(os.path.dirname(mesh_path), 'rig_' + base))
    os.makedirs(outdir, exist_ok=True)

    report = {'input': mesh_path, 'outdir': outdir, 'tag': tag, 'blender': bpy.app.version_string}
    ph = {}

    # --- 1 scene
    bpy.ops.wm.read_factory_settings(use_empty=True)
    log('scene reset')

    # --- 2 import
    t = time.time()
    obj = import_any(mesh_path)
    rotated, rot_reason = orient_horizontal(obj, args.orient)
    bb_min, bb_max = normalize_placement(obj)
    ext = bb_max - bb_min
    report['mesh'] = {
        'verts': len(obj.data.vertices),
        'faces': len(obj.data.polygons),
        'extent': [round(float(x), 4) for x in ext],
        'bbox_min': [round(float(x), 4) for x in bb_min],
        'bbox_max': [round(float(x), 4) for x in bb_max],
        'reoriented': rotated,
        'orient_reason': rot_reason,
    }
    ph['import_s'] = round(time.time() - t, 2)
    log('mesh %d verts / %d faces, extent=%s' % (len(obj.data.vertices), len(obj.data.polygons),
                                                 [round(float(x), 3) for x in ext]))
    log('orient: %s' % rot_reason)

    # --- 3 template
    t = time.time()
    tpl_name = args.template
    if tpl_name == 'auto':
        tpl_name, reason = pick_template(ext)
    else:
        reason = 'user specified'
    if tpl_name == 'chain':
        template, tpl_space, tpl_detail = build_chain_template(
            obj, bb_min, bb_max, n_bones=args.chain_bones)
    else:
        template, tpl_space, tpl_detail = build_template(tpl_name, ext)
    report['template'] = {'name': tpl_name, 'reason': reason,
                          'detail': tpl_detail, 'space': tpl_space,
                          'bones': len(template)}
    log('template=%s (%s) %s, %d bones, space=%s' %
        (tpl_name, reason, tpl_detail, len(template), tpl_space))

    tree = make_bvh(obj)
    joints = fit_joints(template, bb_min, bb_max, tree, float(ext.z), space=tpl_space)

    # 人形模板：腿链按 z 分带重心吸附（治迈步/蹲姿，模板腿落在两腿中间空气里的问题）
    limb_snap = None
    do_snap = (args.snap_limbs == 'legs') or (args.snap_limbs == 'auto' and tpl_name == 'humanoid')
    if do_snap:
        limb_snap = snap_legs(joints, obj, bb_min, bb_max)
        applied = [s for s, v in limb_snap.items() if v.get('applied')]
        log('腿链切片吸附: %s' % (applied or 'none (band 内点数不足，保留模板值)'))
        # 吸附后重新做一次体内校正
        for bname, j in joints.items():
            for key in ('head', 'tail'):
                if not inside_test(tree, j[key])[0]:
                    np_, dist = fit_joints_one(tree, j[key], height=float(ext.z))
                    j['moved_' + key] += dist
                    j[key] = np_
    moved = {n: {'head': round(j['moved_head'], 4), 'tail': round(j['moved_tail'], 4),
                 'outside_head': j['outside_head'], 'outside_tail': j['outside_tail']}
             for n, j in joints.items()
             if j['moved_head'] > 1e-6 or j['moved_tail'] > 1e-6}
    inr = bone_inside_ratio(joints, template, tree)
    bad_bones = {k: v for k, v in inr.items() if v < 0.5}
    report['joint_fit'] = {
        'bones_total': len(joints),
        'joints_relocated': len(moved),
        'relocated_pct': round(100.0 * len(moved) / max(len(joints), 1), 2),
        'detail': moved,
        'bone_inside_ratio': inr,
        'bones_mostly_outside': bad_bones,
        'mean_bone_inside_ratio': round(sum(inr.values()) / max(len(inr), 1), 3),
        'limb_snap': limb_snap,
    }
    log('joint fit: %d/%d bones relocated; mean bone-inside=%.2f; mostly-outside=%s' %
        (len(moved), len(joints),
         report['joint_fit']['mean_bone_inside_ratio'],
         list(bad_bones.keys()) or 'none'))

    # --- 4 armature
    arm = build_armature(joints, template)
    ph['template_s'] = round(time.time() - t, 2)

    # --- 5 weights
    t = time.time()
    heat_err = None
    if args.weight in ('auto', 'heat'):
        heat_err = calc_weights_heat(obj, arm)
        if heat_err and args.weight == 'heat':
            log('heat 失败但用户强制 heat，继续用已生成的部分')

    m_before = weight_metrics(obj, arm)
    log('after heat: zero=%d (%.2f%%) maxinf=%d' %
        (m_before['verts_zero_weight'], m_before['zero_weight_pct'], m_before['max_influences']))

    if args.weight == 'nearest' or (args.weight == 'auto' and
                                    m_before['zero_weight_pct'] > 20.0):
        log('改用 nearest 距离权重（heat 质量不达标）')
        calc_weights_nearest(obj, arm)
    else:
        n_fill = fill_zero_weights(obj, arm)
        log('zero-weight 补齐: %d 个顶点' % n_fill)
        report['zero_filled'] = n_fill

    if args.smooth > 0:
        rep = smooth_weights(obj, repeat=args.smooth, factor=0.5)
        log('权重平滑: %d 轮' % rep)
        report['weight_smooth_rounds'] = rep
        # 平滑可能把近零权重摊出来，再补一次零权重
        n_fill2 = fill_zero_weights(obj, arm)
        if n_fill2:
            log('平滑后 zero-weight 补齐: %d' % n_fill2)

    # 伪骨清理：模板里有、网格里没有的骨（又大又飘：share>25% 且 整骨位移>0.5×骨长）
    pruned = {'flagged': {}, 'verts_pruned': 0, 'verts_reassigned': 0}
    do_prune = args.prune_spurious == 'always' or (
        args.prune_spurious == 'auto' and tpl_name != 'chain')
    if do_prune:
        # 先量一次「清理前」的温和姿势撕裂，清完再量一次；只有确实变好才留下。
        # （实测：human 4.38%→0.59% 该留；rabbit 四足 0.54%→1.88% 是误伤真头/颈骨，必须回滚）
        rest_for_measure = deformed_verts(obj, bpy.context.evaluated_depsgraph_get())
        before = measure_tearing(0.4, obj, arm, rest_for_measure, float(ext.z))
        snap = snapshot_weights(obj)
        pruned = prune_spurious_bones(obj, arm, joints,
                                      share_thr=args.prune_share,
                                      reloc_thr=args.prune_reloc)
        if pruned['flagged']:
            log('伪骨清理候选: %s' % ', '.join('%s(share %.0f%%, reloc %.0f%%)' % (
                k, v['share'] * 100, v['reloc_frac'] * 100) for k, v in pruned['flagged'].items()))
            n_fill3 = fill_zero_weights(obj, arm, exclude=pruned['flagged'].keys())
            if n_fill3:
                log('  zero-weight 补齐: %d' % n_fill3)
            # 重算出来的顶点是「最近骨 Voronoi」硬边界，必须再平滑几次把它们糊开，
            # 否则相邻顶点挂到不同骨上 → 撕裂反而更严重（实测 elong 7.8%→27.6%H）
            if args.resmooth > 0:
                rep2 = smooth_weights(obj, repeat=args.resmooth, factor=0.5)
                log('  重平滑: %d 轮' % rep2)
                n_fill4 = fill_zero_weights(obj, arm, exclude=pruned['flagged'].keys())
                if n_fill4:
                    log('  重平滑后 zero-weight 补齐: %d' % n_fill4)
                pruned['resmooth_rounds'] = rep2

            after = measure_tearing(0.4, obj, arm, rest_for_measure, float(ext.z))
            pruned['tearing_before'] = before
            pruned['tearing_after'] = after
            if is_worse(after, before):
                log('  ✗ 清理后更差（>1.5x %.2f%%→%.2f%%，p99 %.2f→%.2f）→ 回滚，保留原始热扩散权重'
                    % (before['edges_over_1_5x_pct'], after['edges_over_1_5x_pct'],
                       before['stretch_p99'], after['stretch_p99']))
                restore_weights(obj, snap)
                pruned['reverted'] = True
                pruned['revert_reason'] = 'tearing worse: >1.5x %.2f%%->%.2f%%, p99 %.2f->%.2f' % (
                    before['edges_over_1_5x_pct'], after['edges_over_1_5x_pct'],
                    before['stretch_p99'], after['stretch_p99'])
            else:
                log('  ✓ 清理有效（>1.5x %.2f%%→%.2f%%，p99 %.2f→%.2f）→ 保留'
                    % (before['edges_over_1_5x_pct'], after['edges_over_1_5x_pct'],
                       before['stretch_p99'], after['stretch_p99']))
                pruned['reverted'] = False
        else:
            log('伪骨清理: 未发现（没有既占 >%.0f%% 顶点又把整根骨放在体外的骨）'
                % (args.prune_share * 100))
    report['spurious_prune'] = pruned

    n_trim = limit_influences(obj, 4)
    log('influence 裁剪到 <=4: %d 个顶点被裁' % n_trim)
    report['influences_trimmed'] = n_trim
    m_after = weight_metrics(obj, arm)
    report['weights'] = m_after
    report['weight_method'] = args.weight
    report['heat_error'] = heat_err
    ph['weights_s'] = round(time.time() - t, 2)
    log('final weights: zero=%d (%.3f%%) maxinf=%d avg=%.2f unused_bones=%s' %
        (m_after['verts_zero_weight'], m_after['zero_weight_pct'],
         m_after['max_influences'], m_after['avg_influences'],
         m_after['bones_without_influence']))

    # --- 6 rest snapshot + pose + stretch
    t = time.time()
    reset_pose(arm)
    dg = bpy.context.evaluated_depsgraph_get()
    rest_np = deformed_verts(obj, dg)
    import numpy as np

    # 温和姿势（0.4 倍角度）：日常动画实际会遇到的幅度
    touched_m = apply_test_pose(arm, scale=0.4)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    posed_m = deformed_verts(obj, dg)
    st_mod = edge_stretch_stats(obj, rest_np, posed_m, float(ext.z))

    # 压力姿势（1.0 倍角度）：极限拉伸，最容易暴露权重断层
    reset_pose(arm)
    touched = apply_test_pose(arm, scale=1.0)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    posed_np = deformed_verts(obj, dg)
    st = edge_stretch_stats(obj, rest_np, posed_np, float(ext.z))
    dmove = np.linalg.norm(posed_np - rest_np, axis=1)
    st['verts_moved_max'] = round(float(dmove.max()), 4)
    st['verts_moved_mean'] = round(float(dmove.mean()), 4)
    st['verts_moved_gt_1pct_height'] = int((dmove > 0.01 * float(ext.z)).sum())

    report['pose'] = {'bones_posed': len(touched), 'detail': touched[:40],
                      'moderate_scale': 0.4, 'stress_scale': 1.0}
    report['tearing_moderate'] = st_mod
    report['tearing_stress'] = st
    ph['pose_s'] = round(time.time() - t, 2)
    log('pose(moderate 0.4x): stretch max=%.2f p99=%.2f over1.5x=%d (%.2f%%)' %
        (st_mod['stretch_max'] or 0, st_mod['stretch_p99'] or 0,
         st_mod['edges_over_1_5x'], st_mod['edges_over_1_5x_pct']))
    log('pose(stress  1.0x): stretch max=%.2f p99=%.2f over1.5x=%d (%.2f%%) elong_max=%.3f*h' %
        (st['stretch_max'] or 0, st['stretch_p99'] or 0,
         st['edges_over_1_5x'], st['edges_over_1_5x_pct'],
         st['elong_max_frac_height'] or 0))

    # --- 7 export glb (静置姿态)
    t = time.time()
    bpy.ops.object.select_all(action='DESELECT')
    reset_pose(arm)
    glb_path = os.path.join(outdir, tag + '_rigged.glb')
    obj.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format='GLB',
                              use_selection=True, export_skins=True,
                              export_apply=False, export_yup=True)
    report['glb'] = {'path': glb_path, 'bytes': os.path.getsize(glb_path)}
    ph['export_s'] = round(time.time() - t, 2)
    log('export %s (%.1f MB)' % (glb_path, os.path.getsize(glb_path) / 1048576.0))

    # 存 blend
    blend_path = os.path.join(outdir, tag + '_rigged.blend')
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    report['blend'] = blend_path

    # --- 8 render
    if not args.no_render:
        t = time.time()
        rx, ry = [int(x) for x in args.res.lower().split('x')]
        res = (rx, ry)
        cam = setup_scene(bb_min, bb_max, res=res, samples=args.samples)
        bone_mat = make_emission_mat('BONE_EM', (1.0, 0.93, 0.70), 1.8)
        xray_mat = make_xray_mat('MESH_XRAY', (0.30, 0.48, 0.85), 0.115)
        solid_mat = make_solid_mat('MESH_SOLID', (0.42, 0.47, 0.58))
        min_r = 0.0035 * float(ext.z)

        rest_pairs = [(b.name, (tuple(b.head_local), tuple(b.tail_local)))
                      for b in arm.data.bones]
        bones_rest = build_bone_object(rest_pairs, bone_mat, 'BONES_REST', min_r=min_r)

        imgs = []
        sc = bpy.context.scene

        def shot(name, az, el):
            # 宽扁物体（飞机）自动切横图，瘦高物体（人/兔）竖图
            sr, su = view_spans(bb_min, bb_max, az, el)
            r = res
            if sr > su * 1.12:
                r = (max(res), min(res))
            sc.render.resolution_x, sc.render.resolution_y = r
            aim_camera(cam, bb_min, bb_max, az, el, r)
            p = os.path.join(outdir, name)
            render_to(p)
            imgs.append(name)

        # 1) 纯骨骼 正面（参考图风格）
        obj.hide_render = True
        shot('01_bones_rest_front.png', 0.0, 0.0)
        # 2) 纯骨骼 3/4（脊柱/肢体都能看清）
        shot('02_bones_rest_34.png', 38.0, 10.0)

        # 3) 骨骼 + 半透网格（rest）
        obj.hide_render = False
        obj.data.materials.clear()
        obj.data.materials.append(xray_mat)
        shot('03_xray_rest_34.png', 38.0, 10.0)

        # 4) 同一机位 posed —— 和 03 两帧对比看撕裂
        reset_pose(arm)
        apply_test_pose(arm, scale=1.0)
        bpy.context.view_layer.update()
        bones_rest.hide_render = True
        pose_pairs = [(pb.name, (tuple(pb.head), tuple(pb.tail)))
                      for pb in arm.pose.bones]
        bones_pose = build_bone_object(pose_pairs, bone_mat, 'BONES_POSE', min_r=min_r)
        shot('04_xray_pose_34.png', 38.0, 10.0)

        # 5) 实心网格（posed）—— 表面拉伸/撕裂最直观
        bones_pose.hide_render = True
        obj.data.materials.clear()
        obj.data.materials.append(solid_mat)
        shot('05_solid_pose_34.png', 38.0, 10.0)

        # 6) 实心网格（rest）对照
        reset_pose(arm)
        bpy.context.view_layer.update()
        shot('06_solid_rest_34.png', 38.0, 10.0)

        ph['render_s'] = round(time.time() - t, 2)
        report['render'] = {
            'res': [rx, ry], 'samples': args.samples, 'device': 'CPU (Cycles)',
            'images': imgs,
        }

    ph['total_s'] = round(time.time() - T0, 2)
    report['timing'] = ph

    rep_path = os.path.join(outdir, 'report_%s.json' % tag)
    with open(rep_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, 'run_%s.log' % tag), 'w', encoding='utf-8') as f:
        f.write('\n'.join(LOG_LINES) + '\n')
    log('DONE -> %s' % outdir)
    print('REPORT_JSON=' + rep_path, flush=True)
    print('SUMMARY=' + json.dumps({
        'tag': tag,
        'verts': report['mesh']['verts'],
        'faces': report['mesh']['faces'],
        'template': report['template']['name'],
        'bones': report['template']['bones'],
        'joints_relocated': report['joint_fit']['joints_relocated'],
        'verts_zero_weight': m_after['verts_zero_weight'],
        'max_influences': m_after['max_influences'],
        'avg_influences': m_after['avg_influences'],
        'bones_without_influence': m_after['bones_without_influence'],
        'stretch_max': st.get('stretch_max'),
        'stretch_p99': st.get('stretch_p99'),
        'edges_over_1_5x': st.get('edges_over_1_5x'),
        'total_s': ph['total_s'],
    }, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
