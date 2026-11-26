# -*- coding: utf-8 -*-
"""多视图入口与小壳过滤的单测（2026-11-26 接入）。

这两个功能都是本轮六个方向攻坚后落地的：
- 多视图：**白底原图直传**，不抠图、不归一化（实测 0.4872 vs 预遮罩 0.3112）
- 小壳过滤：Direct3D-V3 那类「1 大块 + 上千碎壳」的产物能过质量闸
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("trimesh")
import trimesh


# ─────────────────────────── 小壳过滤 ───────────────────────────

def _make_fragmented(n_shells: int = 300, shell_faces: int = 80):
    """造一个「大块 + 一堆 80 面碎壳」的网格，形态对齐 Direct3D-V3 输出。"""
    main = trimesh.creation.icosphere(subdivisions=5)          # ~20k 面
    parts = [main]
    for i in range(n_shells):
        s = trimesh.creation.icosphere(subdivisions=1)         # 80 面
        s.apply_scale(0.002)
        s.apply_translation([2.5 + i * 0.05, 0, 0])
        parts.append(s)
    return trimesh.util.concatenate(parts)


def test_drop_debris_removes_shells(tmp_path: Path):
    from backend.pipeline import Pipeline

    class _P(Pipeline):
        def __init__(self):          # 不建配置、不连 ComfyUI
            pass

        def _log(self, *a, **k):
            pass

    src = tmp_path / "frag.glb"
    _make_fragmented(300).export(str(src))
    before = len(trimesh.load(str(src), force="mesh").faces)

    info = _P()._drop_debris(str(src))

    assert info["removed"] is True
    assert info["removed_parts"] == 300
    assert info["kept_parts"] == 1
    after = len(trimesh.load(str(src), force="mesh").faces)
    assert after < before
    # 主体必须还在（20k 面量级），不能把网格删空
    assert after > 10_000


def test_drop_debris_keeps_meaningful_parts(tmp_path: Path):
    """有意义的分离件（面数够多）不能被当碎屑删掉 —— 这是它和「只留最大块」的区别。"""
    from backend.pipeline import Pipeline

    class _P(Pipeline):
        def __init__(self):
            pass

        def _log(self, *a, **k):
            pass

    main = trimesh.creation.icosphere(subdivisions=5)      # ~20k
    second = trimesh.creation.icosphere(subdivisions=4)    # ~5k，占 20%
    second.apply_translation([5.0, 0, 0])
    src = tmp_path / "two_parts.glb"
    trimesh.util.concatenate([main, second]).export(str(src))

    info = _P()._drop_debris(str(src))

    assert info["removed"] is False          # 两块都够大，不该动
    assert info["kept_parts"] == 2


def test_drop_debris_idempotent(tmp_path: Path):
    from backend.pipeline import Pipeline

    class _P(Pipeline):
        def __init__(self):
            pass

        def _log(self, *a, **k):
            pass

    src = tmp_path / "frag2.glb"
    _make_fragmented(120).export(str(src))
    p = _P()
    first = p._drop_debris(str(src))
    second = p._drop_debris(str(src))
    assert first["removed"] is True
    assert second["removed"] is False        # 第二次没有碎屑可删


def test_mesh_optimizer_debris_config_exists():
    """OptConfig 必须带这两个旋钮，否则 mesh_optimizer 里 _remove_debris 会 AttributeError。"""
    from backend.mesh_optimizer import OptConfig

    cfg = OptConfig()
    assert hasattr(cfg, "debris_min_faces")
    assert hasattr(cfg, "debris_min_ratio")
    assert cfg.debris_min_faces >= 0
    assert 0 <= cfg.debris_min_ratio < 1


# ─────────────────────────── 多视图入口 ───────────────────────────

def test_generate_3d_multiview_signature_and_validation():
    """缺图要立刻报错，不能把不存在的路径传下去。"""
    from backend.pipeline import Pipeline

    class _P(Pipeline):
        def __init__(self):
            self.comfy = object()          # 有对象即可，验证阶段不会用到

        def _log(self, *a, **k):
            pass

    r = _P().generate_3d_multiview({"front": "nope.png"})
    assert getattr(r, "success", True) is False
    assert "缺图" in str(getattr(r, "error", ""))


def test_generate_3d_multiview_requires_backend_support(tmp_path: Path):
    """后端没有 _build_multiview_real_workflow 时要给明确错误，而不是 AttributeError。"""
    from backend.pipeline import Pipeline

    img = tmp_path / "a.png"
    img.write_bytes(b"not-an-image")

    class _P(Pipeline):
        def __init__(self):
            self.comfy = object()

        def _log(self, *a, **k):
            pass

    r = _P().generate_3d_multiview({"front": str(img), "back": str(img),
                                    "left": str(img), "right": str(img)})
    assert getattr(r, "success", True) is False
    assert "多视图" in str(getattr(r, "error", ""))


def test_multiview_forces_mv_generation_mode():
    """回归：config 里 3D 模型的 generation_mode 是单视图的（Hunyuan3D-2-Fast），
    多视图必须强制改成 2mv，否则 MV 节点加载单视图管线 → cv2.resize(None) 必崩。
    （2026-11-26 实测：60 秒内失败，报 src is not a numpy array）
    """
    from backend.pipeline import Pipeline

    captured = {}

    class _Comfy:
        def _build_multiview_real_workflow(self, params):
            captured.update(params)
            raise RuntimeError("stop-here")      # 建完工作流就停，不提交

        def submit_workflow(self, workflow, stage="3d"):   # 占位，防止属性不存在先抛
            raise RuntimeError("should-not-reach")

        def upload_image(self, path, stage="image", **kw):
            return Path(path).name

    class _P(Pipeline):
        def __init__(self):
            self.comfy = _Comfy()
            self.backend_mode = "comfyui"
            self.config = {"models": {"3d": {"hunyuan21": {
                "name": "Hunyuan3D 2.1", "generation_mode": "Hunyuan3D-2-Fast"}}},
                "paths": {"output": str(Path(tempfile.gettempdir()) / "ff_mv_test"),
                          "comfyui": str(Path(tempfile.gettempdir()) / "ff_mv_test")},
                "stages": {}}
            self.state = type("S", (), {"current_glb_path": None,
                                        "steps_done": []})()

        def _log(self, *a, **k):
            pass

    tmp = Path(tempfile.mkdtemp())
    imgs = {}
    for k in ("front", "back", "left", "right"):
        p = tmp / f"{k}.png"
        p.write_bytes(b"x")
        imgs[k] = str(p)

    _P().generate_3d_multiview(imgs, model_key="hunyuan21")
    assert captured, "没有走到工作流构建"
    assert captured["model_config"]["generation_mode"] == "Hunyuan3D-2mv-Fast"
    assert captured["guidance_scale"] == 5.0

    """关键回归：四张图必须**原样**进 Multi Background Remover，中间不能插抠图节点。

    这是本轮最重要的实测结论（预遮罩 0.3112 → 白底原图 0.4872）。
    """
    from backend.comfy_client import ComfyUIClient

    c = ComfyUIClient.__new__(ComfyUIClient)     # 不建连接
    params = {
        "model_config": {"generation_mode": "Hunyuan3D-2mv-Fast"},
        "real_views": {"front": "f.png", "back": "b.png",
                       "left": "l.png", "right": "r.png"},
        "seed": 42, "octree_resolution": 512,
        "guidance_scale": 6.0, "steps": 30,
        "output_path": "mv.glb",
    }
    wf = c._build_multiview_real_workflow(params)["prompt"]

    classes = {n["class_type"] for n in wf.values()}
    # 必须只有 LoadImage → Multi Background Remover → 2mv，不应出现 Zero123Plus
    assert "LoadImage" in classes
    assert "[Comfy3D] Multi Background Remover" in classes
    assert "[Comfy3D] Hunyuan3D V2 ShapeGen MV" in classes
    assert not any("Zero123Plus" in x for x in classes), "多视图真实照片路径不该走 Zero123Plus"

    # 四张图的输入节点必须直连 load image（load image 的 image 字段 = 原文件名）
    loads = [n["inputs"]["image"] for n in wf.values() if n["class_type"] == "LoadImage"]
    assert loads == ["f.png", "b.png", "l.png", "r.png"]
