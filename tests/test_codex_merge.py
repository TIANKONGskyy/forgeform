# -*- coding: utf-8 -*-
"""Codex 合并进来的三个模块的回归测试（2026-11-26）。

覆盖：
- `view_classifier.assign_views` 的三条硬规则（去重 / 低置信 / 视图空洞）
- `multiview_prompt.build_real_workflow` 的 2mv 强制修正与 alpha 重接
- `rigging.bind_model` 的守门（不接受非模型、缺执行器时明确报错）
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# ───────────────────────── 视图归类 ─────────────────────────

def _fake_classifier(table):
    def _f(path):
        view, conf = table.get(Path(path).stem, ("unknown", 0.0))
        return {"view": view, "confidence": conf}
    return _f


def test_assign_views_complete_four_ok():
    from backend import view_classifier as vc

    table = {"a": ("front", 0.95), "b": ("back", 0.95),
             "c": ("left", 0.95), "d": ("right", 0.95)}
    got, unresolved, results = vc.assign_views(["a", "b", "c", "d"],
                                               classifier=_fake_classifier(table))
    assert set(got) == {"front", "back", "left", "right"}
    assert unresolved == []
    assert len(results) == 4


def test_assign_views_rejects_duplicate_direction():
    """两张图都判成 front：两张都不能用（宁可让用户重选，也不能张冠李戴）。"""
    from backend import view_classifier as vc

    table = {"a": ("front", 0.95), "b": ("back", 0.95), "c": ("left", 0.95),
             "e": ("front", 0.95)}
    got, unresolved, _ = vc.assign_views(["a", "b", "c", "e"],
                                         classifier=_fake_classifier(table))
    assert got == {}
    assert set(unresolved) == {"a", "e"}


def test_assign_views_rejects_low_confidence():
    from backend import view_classifier as vc

    table = {"a": ("front", 0.95), "b": ("back", 0.95),
             "c": ("left", 0.95), "g": ("right", 0.3)}
    got, unresolved, _ = vc.assign_views(["a", "b", "c", "g"],
                                         classifier=_fake_classifier(table))
    assert got == {}
    assert unresolved == ["g"]


def test_assign_views_reports_hole_instead_of_silence():
    """缺 back、却有 left/right 时，必须给出 unresolved（原来返回空 + 空，界面像卡住）。"""
    from backend import view_classifier as vc

    table = {"a": ("front", 0.95), "c": ("left", 0.95), "d": ("right", 0.95)}
    got, unresolved, _ = vc.assign_views(["a", "c", "d"],
                                         classifier=_fake_classifier(table))
    assert got == {}
    assert unresolved, "视图有空洞时必须告诉调用方"
    assert set(unresolved) <= {"c", "d"}


def test_assign_views_single_image_becomes_front():
    from backend import view_classifier as vc

    got, unresolved, _ = vc.assign_views(["only.png"], classifier=_fake_classifier({}))
    assert got == {"front": "only.png"}
    assert unresolved == []


# ───────────────────────── 多视图工作流 ─────────────────────────

def test_multiview_workflow_forces_2mv_mode():
    """config 里写单视图模式时，必须纠正成 2mv，否则 MV 节点必崩。"""
    from backend import multiview_prompt as mp

    wf = mp.build_real_workflow({"front": "f.png", "back": "b.png", "left": "l.png",
                                 "right": "r.png"},
                                {"generation_mode": "Hunyuan3D-2-Fast"}, "o.glb")
    assert wf["prompt"]["6"]["inputs"]["generation_mode"] == "Hunyuan3D-2mv-Fast"


def test_multiview_workflow_keeps_existing_mv_mode():
    from backend import multiview_prompt as mp

    wf = mp.build_real_workflow({"front": "f.png", "back": "b.png"},
                                {"generation_mode": "Hunyuan3D-2mv-Turbo"}, "o.glb")
    assert wf["prompt"]["6"]["inputs"]["generation_mode"] == "Hunyuan3D-2mv-Turbo"


def test_multiview_workflow_rejoins_alpha_when_present():
    """已抠好的图必须把 alpha 接回 RGBA，否则会被再抠一遍、丢掉薄结构。"""
    from backend import multiview_prompt as mp

    wf = mp.build_real_workflow({"front": "f.png", "back": "b.png", "left": "l.png",
                                 "right": "r.png"},
                                {"generation_mode": "Hunyuan3D-2mv-Fast"}, "o.glb",
                                alpha_views={"front", "left"})
    classes = [n["class_type"] for n in wf["prompt"].values()]
    assert classes.count("JoinImageWithAlpha") == 2
    # 重接后的节点必须真的接到 Multi Background Remover 的对应输入上
    mbr = next(n for n in wf["prompt"].values()
               if n["class_type"] == "[Comfy3D] Multi Background Remover")
    assert len(mbr["inputs"]["image_front"]) == 2      # [node_id, slot]
    assert len(mbr["inputs"]["image_back"]) == 2       # 未接 alpha 的仍是 LoadImage


def test_multiview_workflow_rejects_incomplete_prefix():
    """只给 front+left（缺 back）要被拒，不能把 left 当 back 用。"""
    from backend import multiview_prompt as mp

    with pytest.raises(ValueError):
        mp.build_real_workflow({"front": "f.png", "left": "l.png"},
                               {"generation_mode": "Hunyuan3D-2mv-Fast"}, "o.glb")


# ───────────────────────── 绑定守门 ─────────────────────────

def test_bind_model_rejects_non_mesh(tmp_path: Path):
    from backend import rigging

    bad = tmp_path / "note.txt"
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        rigging.bind_model(str(bad), str(tmp_path / "out"))


def test_bind_model_reports_missing_executor(tmp_path: Path, monkeypatch):
    from backend import rigging

    mesh = tmp_path / "m.stl"
    mesh.write_bytes(b"solid x\nendsolid x\n")
    monkeypatch.setattr(rigging, "TOOL", tmp_path / "no_such_tool.py")
    with pytest.raises(RuntimeError) as ei:
        rigging.bind_model(str(mesh), str(tmp_path / "out"))
    assert "执行器" in str(ei.value)


def test_validate_glb_rejects_mesh_without_skin(tmp_path: Path):
    """GLB 里没有 skins 时必须报错 —— 子进程退出码 0 不算证据。"""
    import json as _json
    import struct as _struct

    from backend import rigging

    p = tmp_path / "noskin.glb"
    doc = _json.dumps({"asset": {"version": "2.0"},
                       "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]})
    body = doc.encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    p.write_bytes(b"glTF" + _struct.pack("<II", 2, 12 + 8 + len(body)) +
                  _struct.pack("<II", len(body), 0x4E4F534A) + body)
    with pytest.raises(RuntimeError) as ei:
        rigging._validate_glb(p)
    assert "蒙皮" in str(ei.value)
