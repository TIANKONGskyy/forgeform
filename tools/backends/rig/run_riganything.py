#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_riganything.py —— RigAnything 端到端推理跑一版（本机 V100 适配）

为什么不直接用官方 scripts/inference.sh：
  1. 它只认 .obj / .glb，喂 .stl 会把输入文件本身覆盖掉（`file_path.replace(".obj", ...)` 没替换到）；
  2. 它的 mesh_simplify.py 里有一句 `bpy.context.scene.cycles.device = "GPU"`，
     而 pip 版 bpy 4.2.0 不带 Cycles（engine 枚举只有 BLENDER_EEVEE_NEXT），这一步会炸。
所以这里用 trimesh 自己复刻它的「预处理」：Z-up→Y-up 旋转 + 移到原点 + 归一化到单位球，
导出 `<name>_simplified.glb`，然后**直接调官方 inference.py / vis_skel.py**（这两步才是模型本体）。

用法:
  venv_riganything\\Scripts\\python.exe run_riganything.py --mesh <任意网格> --out <输出目录> [--name xxx]
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, 'third_party', 'RigAnything')
CKPT = os.path.join(HERE, 'models', 'riganything_ckpt.pt')
CFG = os.path.join(HERE, 'models', 'config.yaml')


def log(msg, t0=[time.time()]):
    print('[%7.1fs] %s' % (time.time() - t0[0], msg), flush=True)


def prep(mesh_path, outdir, name, simplify=0):
    """复刻 mesh_simplify.py 的预处理：可选简化 + Z-up→Y-up、居中、归一化到单位球、导出 GLB。

    simplify: 目标面数（0=不简化）。官方 pipeline 第 1 步就是干这个的，
    实测不简化喂 203k 顶点的飞机 → 蒙皮阶段 OOM（PyTorch 吃到 27.5 GB / 32 GB）。
    """
    m = trimesh.load(mesh_path, force='mesh')
    n_in, n_f_in = len(m.vertices), len(m.faces)
    n_simplified = None
    if simplify and int(simplify) > 0 and len(m.faces) > int(simplify):
        import pymeshlab
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(vertex_matrix=np.asarray(m.vertices, dtype=np.float64),
                                   face_matrix=np.asarray(m.faces, dtype=np.int32)))
        target = min(int(simplify), len(m.faces))
        ms.meshing_decimation_quadric_edge_collapse(
            targetfacenum=target, qualitythr=0.8, preservetopology=True,
            preserveboundary=True, boundaryweight=1.0, optimalplacement=True,
            preservenormal=True, autoclean=True)
        m = trimesh.Trimesh(vertices=ms.current_mesh().vertex_matrix(),
                            faces=ms.current_mesh().face_matrix(), process=False)
        n_simplified = {'faces_before': n_f_in, 'faces_after': len(m.faces),
                        'verts_after': len(m.vertices), 'target': target}

    v = np.asarray(m.vertices, dtype=np.float64)
    # Z-up → Y-up 的正确转换：(x, y, z) → (x, z, -y)，高度落到 +Y。
    # 坑：官方 mesh_simplify.py 写的是 np.dot(verts, [[1,0,0],[0,0,1],[0,-1,0]])，
    # 那是「行向量 × 矩阵」= 上面矩阵的转置，等价于把高度映射到 **-Y**。
    # 直接照抄会让模型看到倒立的网格（实测人形头朝下、腿朝上），必须用转置后的方向。
    x, y, z = v[:, 0].copy(), v[:, 1].copy(), v[:, 2].copy()
    v = np.stack([x, z, -y], axis=1)
    v = v - v.mean(axis=0)
    scale = float(np.max(np.linalg.norm(v, axis=1)))
    v = v / max(scale, 1e-12)
    out = trimesh.Trimesh(vertices=v, faces=np.asarray(m.faces), process=False)
    os.makedirs(outdir, exist_ok=True)
    glb = os.path.join(outdir, '%s_simplified.glb' % name)
    out.export(glb)
    return {'input': mesh_path, 'glb': glb, 'verts_in': n_in, 'faces_in': n_f_in,
            'verts_out': int(len(v)), 'norm_scale': round(scale, 6),
            'simplified': n_simplified,
            'bbox_extent_unit': [round(float(x), 4) for x in out.extents],
            'watertight': bool(out.is_watertight)}


def run_step(tag, cmd, logf):
    log('exec: %s' % ' '.join('"%s"' % c if ' ' in c else c for c in cmd[1:]))
    t = time.time()
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    dt = time.time() - t
    logf.write('===== %s (%.1fs, rc=%d) =====\n' % (tag, dt, r.returncode))
    logf.write('CMD: %s\n' % ' '.join(cmd))
    logf.write('--- stdout ---\n%s\n' % (r.stdout or ''))
    logf.write('--- stderr ---\n%s\n' % (r.stderr or ''))
    logf.flush()
    log('%s rc=%d  %.1fs' % (tag, r.returncode, dt))
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '').strip().splitlines()[-6:]
        for line in tail:
            log('   ! ' + line)
    return r.returncode, dt, (r.stdout or ''), (r.stderr or '')


class VramSampler(threading.Thread):
    """每 interval 秒采一次 nvidia-smi，记录峰值。"""

    def __init__(self, interval=10.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                r = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,utilization.gpu',
                                    '--format=csv,noheader,nounits'],
                                   capture_output=True, text=True, timeout=10)
                line = (r.stdout or '').strip().splitlines()[0]
                mem, util = [int(x.strip()) for x in line.split(',')[:2]]
                self.samples.append((round(time.time(), 1), mem, util))
            except Exception:
                pass
            for _ in range(int(self.interval * 10)):
                if self._stop:
                    return
                time.sleep(0.1)

    def stop(self):
        self._stop = True

    def summary(self):
        if not self.samples:
            return {}
        mems = [s[1] for s in self.samples]
        utils = [s[2] for s in self.samples]
        return {'samples': len(self.samples),
                'mem_peak_mb': max(mems), 'mem_min_mb': min(mems),
                'mem_first_mb': mems[0], 'util_peak_pct': max(utils),
                'interval_s': self.interval,
                'raw': self.samples}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mesh', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--name', default=None)
    ap.add_argument('--vram-interval', type=float, default=10.0)
    ap.add_argument('--skip-vis', action='store_true')
    ap.add_argument('--simplify', type=int, default=0,
                    help='目标面数（0=不简化）。面数 >15 万时建议 8192，否则蒙皮阶段会 OOM')
    args = ap.parse_args()

    name = args.name or os.path.splitext(os.path.basename(args.mesh))[0]
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    logf = open(os.path.join(outdir, 'run_%s.log' % name), 'w', encoding='utf-8')
    report = {'mesh': args.mesh, 'name': name, 'outdir': outdir}

    log('python: %s' % sys.executable)
    report['prep'] = prep(args.mesh, outdir, name, simplify=args.simplify)
    log('prep: %(verts_in)d -> %(verts_out)d verts, unit-bbox %(bbox_extent_unit)s' % report['prep'])
    if report['prep'].get('simplified'):
        log('simplified: %s' % report['prep']['simplified'])
    glb = report['prep']['glb']
    npz = os.path.join(outdir, '%s_simplified.npz' % name)

    py = sys.executable
    t_all = time.time()
    sampler = VramSampler(args.vram_interval)
    sampler.start()

    rc1, dt1, out1, err1 = run_step('inference', [
        py, 'inference.py', '--config', CFG, '--load', CKPT,
        '-s', 'inference', 'true', '-s', 'inference_out_dir', outdir,
        '--mesh_path', glb], logf)
    report['inference'] = {'rc': rc1, 'seconds': round(dt1, 2)}

    rig_glb = None
    if rc1 == 0 and not args.skip_vis:
        rc2, dt2, out2, err2 = run_step('vis_skel', [
            py, os.path.join('inference_utils', 'vis_skel.py'),
            '--data_path', npz, '--save_path', outdir, '--mesh_path', glb], logf)
        report['vis'] = {'rc': rc2, 'seconds': round(dt2, 2)}
        cand = os.path.join(outdir, '%s_simplified_rig.glb' % name)
        if os.path.exists(cand):
            rig_glb = cand
    elif rc1 != 0:
        report['vis'] = {'skipped': 'inference failed'}

    sampler.stop()
    time.sleep(0.2)
    report['vram'] = sampler.summary()
    report['total_seconds'] = round(time.time() - t_all, 2)
    report['npz'] = npz if os.path.exists(npz) else None
    report['rig_glb'] = rig_glb
    if rig_glb:
        report['rig_glb_bytes'] = os.path.getsize(rig_glb)
    if report['vram']:
        log('VRAM: peak=%s MB  first=%s MB  util_peak=%s%%' % (
            report['vram']['mem_peak_mb'], report['vram']['mem_first_mb'],
            report['vram']['util_peak_pct']))

    with open(os.path.join(outdir, 'report_%s.json' % name), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logf.close()
    log('DONE rc_inference=%d  rig_glb=%s' % (rc1, rig_glb))
    print('SUMMARY=' + json.dumps({k: v for k, v in report.items()
                                   if k not in ('vram',)}, ensure_ascii=False), flush=True)
    return 0 if rc1 == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
