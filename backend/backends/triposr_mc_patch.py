# -*- coding: utf-8 -*-
"""给 TripoSR 补一个 torchmcubes 的替代品 —— 它没有 Windows 的 wheel，装不上。

`tsr/models/isosurface.py` 里是 `from torchmcubes import marching_cubes`，
接口是 `(verts, faces) = marching_cubes(volume, level)`。
这里用 skimage 实现同样的签名，导入时替换掉那个模块。

用法：在 import tsr 之前 `import tsr_mc_patch`。
"""
import types

import numpy as np
import torch


def marching_cubes(level: torch.Tensor, iso: float = 0.0):
    """和 torchmcubes.marching_cubes 同样的签名。

    level: (R, R, R) 的标量场
    返回: (verts (N,3) float, faces (M,3) long)
    """
    from skimage import measure

    vol = level.detach().float().cpu().numpy()
    # skimage 的坐标顺序是 (z, y, x)；torchmcubes 是 (x, y, z)。
    # 调用方后面会做 v_pos[..., [2,1,0]] 转置，这里保持一致即可。
    verts, faces, _normals, _values = measure.marching_cubes(vol, level=iso)
    return (torch.from_numpy(verts.copy()).float(),
            torch.from_numpy(faces.copy()).long())


def install() -> None:
    """把假的 torchmcubes 塞进 sys.modules。"""
    import sys

    mod = types.ModuleType("torchmcubes")
    mod.marching_cubes = marching_cubes
    mod.__version__ = "0.0.0-skimage-shim"
    sys.modules["torchmcubes"] = mod


install()
