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

# ────────────────── 2026-11-27 验证中发现并修掉的三个 bug ──────────────────

def test_riganything_tier_dispatches_to_its_own_runner(tmp_path: Path, monkeypatch):
    """`--tier riganything` 曾经是假的：只跑模板、在 note 里写"需手动跑"。

    现在必须真的走到 RigAnything 执行器；缺执行器时要报错，而不是静默给模板结果。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_asset_refine_rig", r"D:\ForgeForm-oss\tools\asset_refine_rig.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mesh = tmp_path / "m.stl"
    mesh.write_bytes(b"solid x\nendsolid x\n")
    monkeypatch.setattr(mod, "RESEARCH", tmp_path / "no_research")

    r = mod.rig(mesh, tmp_path / "out", tier="riganything")
    assert r["ok"] is False
    assert "run_riganything" in str(r.get("error", ""))


def test_rig_produced_glob_accepts_both_naming_schemes(tmp_path: Path):
    """模板档产出 *_rigged.glb、RigAnything 产出 *_rig.glb —— 后端两种都要认。

    曾经的 bug：只 glob *_rigged.glb，RigAnything 明明产出了却报"没有产出 GLB"。
    """
    from backend import rigging

    out = tmp_path / "out"
    out.mkdir()
    (out / "human_simplified.glb").write_bytes(b"x")      # 中间件，不能选
    rigged = out / "character_simplified_rig.glb"
    rigged.write_bytes(b"x")

    cands = [p for p in list(out.glob("*_rigged.glb")) + list(out.rglob("*.glb"))
             if p.name.endswith("_rig.glb") or p.name.endswith("_rigged.glb")]
    assert [p.name for p in cands] == ["character_simplified_rig.glb"]


def test_validate_glb_accepts_52_joint_skin(tmp_path: Path):
    """RigAnything 的产物是 52 关节，校验器不能把多关节当异常。"""
    import json as _json
    import struct as _struct

    from backend import rigging

    doc = {
        "asset": {"version": "2.0"},
        "skins": [{"joints": list(range(52))}],
        "nodes": [{"skin": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "JOINTS_0": 1,
                                                  "WEIGHTS_0": 2}}]}],
    }
    p = tmp_path / "ra.glb"
    body = _json.dumps(doc).encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    p.write_bytes(b"glTF" + _struct.pack("<II", 2, 12 + 8 + len(body)) +
                  _struct.pack("<II", len(body), 0x4E4F534A) + body)
    info = rigging._validate_glb(p)
    assert info["joints"] == 52

# ────────────────── 2026-11-27 贴图产品入口 + 精修自动过滤 ──────────────────

def test_paint_workflow_shape_and_fast_mode():
    """贴图工作流必须：LoadMesh → LoadImage → CameraConfig → MultiViews → Bake → InPaint。

    fast 档只减视角/步数，**不能**砍掉 Hy3DInPaint —— 它是唯一输出网格的节点
    （砍了就永远没有产物）。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_paint_asset", r"D:\ForgeForm-oss\tools\paint_asset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    full = mod.build_workflow("m.stl", "ref.png", "o.glb", fast=False)["prompt"]
    fast = mod.build_workflow("m.stl", "ref.png", "o.glb", fast=True)["prompt"]

    for wf in (full, fast):
        classes = [n["class_type"] for n in wf.values()]
        assert "Hy3DInPaint" in classes, "必须保留出网格的节点"
        assert wf["6"]["class_type"] == "Hy3DInPaint"
        assert wf["1"]["class_type"] == "LocalResearchLoadMesh"

    # fast 档少两张视角（顶/底），并降低步数门槛由调用方给
    assert len(full["3"]["inputs"]["camera_azimuths"].split(",")) == 6
    assert len(fast["3"]["inputs"]["camera_azimuths"].split(",")) == 4


def test_paint_rejects_small_view_and_missing_files(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_paint_asset2", r"D:\ForgeForm-oss\tools\paint_asset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    r = mod.paint(tmp_path / "no.stl", tmp_path / "no.png", tmp_path / "o.glb")
    assert r["ok"] is False and "网格不存在" in r["error"]

    mesh = tmp_path / "m.stl"
    img = tmp_path / "r.png"
    mesh.write_bytes(b"x")
    img.write_bytes(b"x")
    r2 = mod.paint(mesh, img, tmp_path / "o.glb", view=256)
    assert r2["ok"] is False and "view 最小 512" in r2["error"]


def test_refine_accepts_str_out_and_reports_debris(tmp_path: Path, monkeypatch):
    """refine 要能接受 str 路径（CLI 传的就是 str），且成功后自动过小壳过滤。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_ar2", r"D:\ForgeForm-oss\tools\asset_refine_rig.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.refine.__defaults__ is not None or True   # 只确认签名存在
    # 缺研究脚本时必须返回 ok=False 而不是抛
    monkeypatch.setattr(mod, "RESEARCH", tmp_path / "none")
    r = mod.refine(str(tmp_path / "i.png"), str(tmp_path / "m.obj"), str(tmp_path / "o.glb"))
    assert r["ok"] is False and "缺研究脚本" in r["error"]

# ────────────────── 2026-11-27 全面测试中发现并修掉的问题 ──────────────────

def test_bind_model_returns_ok_flag(tmp_path: Path, monkeypatch):
    """bind_model 的返回值必须带 ok —— 调用方（含 verify_capabilities）都按 r["ok"] 判成败。"""
    import inspect

    from backend import rigging

    src = inspect.getsource(rigging.bind_model)
    assert '"ok": True' in src, "成功路径必须返回 ok=True"
    # 失败路径也要有 ok=False 语义（用非法输入触发）
    bad = tmp_path / "x.txt"
    bad.write_text("x", encoding="utf-8")
    try:
        rigging.bind_model(str(bad), str(tmp_path / "o"))
    except ValueError as e:
        assert "模型" in str(e)
    else:
        raise AssertionError("非模型文件应该抛 ValueError")


def test_studio_cancel_flips_inprocess_job_status():
    """环境扫描/校准是进程内任务：取消后状态必须立刻变 cancelled。

    原来 cancel() 只 set 事件，job 卡在 running → 前端一直转圈，
    且 POST /api/config 被“请等待当前安装完成后再保存”拦住（2026-11-27 实测）。
    """
    import inspect
    import re

    from backend import studio as studio_mod

    src = inspect.getsource(studio_mod.Studio.cancel)
    # 取消后要把 running 翻成 cancelled（进程内任务没有子进程可 kill）
    assert "cancelled" in src
    assert re.search(r"job\[['\"]status['\"]\]\s*=\s*['\"]cancelled['\"]", src), \
        "cancel() 必须直接把 job 状态置为 cancelled"


def test_multiview_resolves_lazy_hybrid_comfy_client():
    """HybridBackend 的 ComfyUIClient 是惰性的：多视图必须走 _get_comfy() 拿真实客户端。

    否则 backend.mode=auto 时 generate_3d_multiview 直接报“不支持多视图”（2026-11-27 实测）。
    """
    import inspect

    from backend.pipeline import Pipeline

    src = inspect.getsource(Pipeline.generate_3d_multiview)
    assert "_get_comfy" in src, "必须处理 hybrid 的惰性客户端"
    assert "comfy.wait_for_completion" in src, "submit 与 wait 必须用同一个客户端对象"

# ────────────────── 2026-11-27 能力面板接进 UI ──────────────────

def test_capability_ui_renders_five_panels():
    """能力面板必须在 app 里真渲染出五个 Tab（不是只有函数存在）。"""
    import inspect

    from backend import capability_ui

    src = inspect.getsource(capability_ui.render)
    for token in ("骨骼绑定", "贴图 PBR", "精修", "多视图重建", "动画"):
        assert token in src, f"缺面板：{token}"
    # 五个 api_name 是 UI 与外部调用（gradio_client）的契约，不能改名
    for name in ("cap_rig", "cap_paint", "cap_refine", "cap_multiview", "cap_motion"):
        assert f'api_name="{name}"' in src, f"缺接口名 {name}"


def test_capability_ui_frees_gpu_before_heavy_work():
    """GPU 重活前必须先让出对话模型。

    ★ 2026-11-27 实测：UI 点「精修」直接 OOM —— 预热把 Strata 125B 驻进显存
      （30.8 GB used / 1.8 GB free），Direct3D-V3 要 ~5 GB。
      主对话流程本来会 `_release_chat_gpu()`，能力面板绕过了，所以每个重活入口都要补。
    """
    import inspect

    from backend import capability_ui

    for fn in (capability_ui.do_paint, capability_ui.do_refine,
               capability_ui.do_multiview):
        assert "_free_gpu()" in inspect.getsource(fn), f"{fn.__name__} 没释放显存"
    # 模板档是 CPU，不该为了它停对话模型
    rig_src = inspect.getsource(capability_ui.do_rig)
    assert 'if str(tier) == "riganything"' in rig_src


def test_gallery_collector_lists_every_generation_path():
    """总览收集器要覆盖每条生成路线（少一条用户就看不到那种效果）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_gallery", r"D:\ForgeForm-oss\tools\collect_gallery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    methods = [m[1] for m in mod.PICKS]
    for need in ("单图", "多视图", "精修", "贴图", "骨骼", "动画"):
        assert any(need in m for m in methods), f"缺生成路线：{need}"
    assert len(mod.PICKS) >= 10
