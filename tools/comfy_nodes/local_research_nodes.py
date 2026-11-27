# -*- coding: utf-8 -*-
"""ComfyUI 自定义节点：本地网格加载器（2026-11-28）。

背景
----
`tools/paint_asset.py` 的工作流用了一个叫 `LocalResearchLoadMesh` 的节点，
它原本在 `ComfyUI-3D-Pack` 里。干净安装的机器没有 3D-Pack（而且要 pytorch3d
才能 import），贴图就直接跑不起来。

这个节点只做一件事：给一个绝对路径，把网格读成 ComfyUI 的 TRIMESH 类型。
不依赖 3D-Pack、不依赖 pytorch3d。

安装：放到 `ComfyUI/custom_nodes/local_research_nodes.py`，重启 ComfyUI。
"""
from __future__ import annotations

import os

import numpy as np
import trimesh


class LocalResearchLoadMesh:
    """从绝对路径加载网格（stl/glb/obj/ply），输出 TRIMESH。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("TRIMESH",)
    RETURN_NAMES = ("trimesh",)
    FUNCTION = "load"
    CATEGORY = "ForgeForm"

    def load(self, path: str):
        p = os.path.expanduser(str(path or "").strip().strip('"'))
        if not os.path.isfile(p):
            raise FileNotFoundError(f"网格不存在：{p}")
        mesh = trimesh.load(p, force="mesh")
        if mesh.is_empty:
            raise ValueError(f"网格为空：{p}")
        # ComfyUI 的 TRIMESH 约定是 (vertices, faces) 元组
        verts = np.asarray(mesh.vertices, dtype=np.float32)
        faces = np.asarray(mesh.faces, dtype=np.int64)
        print(f"[ForgeForm] 加载网格 {os.path.basename(p)}: "
              f"{len(verts)} 顶点 / {len(faces)} 面", flush=True)
        return ((verts, faces),)


NODE_CLASS_MAPPINGS = {"LocalResearchLoadMesh": LocalResearchLoadMesh}
NODE_DISPLAY_NAME_MAPPINGS = {"LocalResearchLoadMesh": "本地网格加载（ForgeForm）"}
