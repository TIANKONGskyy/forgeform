# -*- coding: utf-8 -*-
"""五项能力的工作台 UI（2026-11-27 接入）。

沿 `backend/cultural_ui.py` 的扩展点写法：app.py 在 `Blocks` 里调用 `render()`，
这里自己管控件和事件，不动主对话流程。

五个能力：
1. **骨骼绑定** — 模板档（CPU，8–68 s）/ RigAnything 档（GPU，~50 s，40+ 语义骨）
2. **贴图 PBR** — 完整档（~20 min）/ 快速档（~8 min，预览用）
3. **精修** — Direct3D-V3 512 档，4 步（~45 s）/ 15 步（~70 s），自动过小壳过滤
4. **多视图** — 四张白底原图 → 2mv，~85 s
5. **动画** — BVH 重定向，纯 CPU，~10 s/条

所有按钮都调用 `backend.*` 或 `tools/` 里的**同一套产品入口**，和 CLI 完全一致，
所以这里跑通就等于产品可用。
"""
from __future__ import annotations

import json
import shutil
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _register_asset(path: str, kind: str, params: dict | None = None, metrics: dict | None = None) -> dict:
    """产物登记进资产库（失败不影响主流程）。"""
    try:
        from . import assets as asset_store
        return asset_store.register(path, kind, params=params or {},
                                    source="能力工作台", metrics=metrics or {})
    except Exception:
        return {}


def _glb_metrics(path: str) -> dict:
    out = {}
    try:
        import json as _json
        import struct as _struct
        raw = Path(path).read_bytes()
        clen, _ = _struct.unpack("<II", raw[12:20])
        doc = _json.loads(raw[20:20 + clen].decode("utf-8"))
        out["textures"] = len(doc.get("textures") or [])
        out["joints"] = sum(len(s.get("joints") or []) for s in (doc.get("skins") or []))
        out["animations"] = len(doc.get("animations") or [])
    except Exception:
        pass
    try:
        import trimesh
        m = trimesh.load(str(path), force="mesh")
        out["faces"] = int(len(m.faces))
        out["watertight"] = bool(m.is_watertight)
    except Exception:
        pass
    return out


def _out_dir(sub: str) -> Path:
    d = ROOT / "outputs" / "capabilities" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _free_gpu() -> str:
    """跑 GPU 重活前先让出对话模型（Strata 125B 常驻约 28 GB，不让就必 OOM）。

    ★ 2026-11-27 实测：UI 里点「精修」直接 OOM —— 因为 app 预热把 125B 驻进了显存
      （30.8 GB used / 1.8 GB free），Direct3D-V3 需要 ~5 GB。
      主对话流程本来就会在生成前调用 `_release_chat_gpu()`，能力面板绕过了那条路，
      所以这里补一刀。释放是幂等的：没加载就什么也不做。
    """
    notes = []
    try:
        from .pipeline import _release_chat_gpu
        _release_chat_gpu()
        notes.append("chat释放")
    except Exception:
        pass
    try:
        from . import describer
        if hasattr(describer, "strata_stop"):
            if describer.strata_stop(timeout=20):
                notes.append("strata停止")
    except Exception:
        pass
    try:
        from . import agent as _agent
        if hasattr(_agent, "unload_tuned") and _agent.unload_tuned():
            notes.append("tuned卸载")
    except Exception:
        pass
    return "/".join(notes) or "无占用"



def _asset_html() -> str:
    """资产面板内容：与左列产物库同一套视觉（2026-11-27 统一装饰）。"""
    try:
        from . import asset_ui
        return asset_ui.page_html()
    except Exception as e:
        return f'<div class="ast-page-empty">资产索引读取失败：{type(e).__name__}</div>'

def _fmt(r: dict, keys) -> str:
    parts = []
    for k in keys:
        if k in r and r[k] is not None:
            parts.append(f"{k}={r[k]}")
    return " · ".join(parts)


# ───────────────────────── 各能力的处理函数 ─────────────────────────

def do_rig(mesh_path, tier, template, progress=None):
    """骨骼绑定：模板档或 RigAnything 档。"""
    from backend import rigging
    if str(tier) == "riganything":     # 模板档是 CPU，不用动对话模型
        _free_gpu()

    if not mesh_path:
        return None, "先上传一个网格（.stl/.glb/.obj）"
    out = _out_dir("rig")
    t0 = time.time()
    try:
        r = rigging.bind_model(str(mesh_path), str(out), tier=str(tier),
                               template=str(template))
        msg = (f"✓ 绑定完成 {time.time()-t0:.0f}s · 关节 {r['joints']} · "
               f"{Path(r['path']).name}")
        return r["path"], msg
    except Exception as e:
        return None, f"✗ 绑定失败：{type(e).__name__}: {e}"


def do_paint(mesh_path, image_path, quality, progress=None):
    """贴图：完整档 / 快速档。"""
    _free_gpu()   # 让出对话模型，避免 OOM（Strata 125B 常驻约 28 GB）
    import importlib.util

    spec = importlib.util.spec_from_file_location("paint_asset", ROOT / "tools" / "paint_asset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not mesh_path or not image_path:
        return None, "需要同时给网格和参考图"
    fast = quality == "快速档（预览，约 8 分钟）"
    out = _out_dir("paint") / f"painted_{int(time.time())}.glb"
    t0 = time.time()
    r = mod.paint(mesh_path, image_path, out, texture=1024 if fast else 2048,
                  view=512, steps=8 if fast else 20, fast=fast)
    if r.get("ok"):
        return r["out"], (f"✓ 上色完成 {r['seconds']:.0f}s · 贴图 {r['textures']} 张 · "
                          f"峰值 {r['peak_vram_mib']} MiB")
    return None, f"✗ 上色失败：{r.get('error') or r.get('problems')}"


def do_refine(image_path, mesh_path, steps, progress=None):
    """精修：Direct3D-V3 512 档 + 自动小壳过滤。"""
    _free_gpu()   # 让出对话模型，避免 OOM（Strata 125B 常驻约 28 GB）
    import importlib.util

    spec = importlib.util.spec_from_file_location("ar", ROOT / "tools" / "asset_refine_rig.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not image_path or not mesh_path:
        return None, "精修需要：一张参考图 + 一个粗网格（.obj/.glb）"
    out = _out_dir("refine") / f"refined_{int(steps)}steps_{int(time.time())}.glb"
    r = mod.refine(Path(image_path), Path(mesh_path), out, steps=int(steps))
    if r.get("ok"):
        d = r.get("debris") or {}
        return r["out"], (f"✓ 精修完成 · 小壳 {d.get('removed_parts','-')} 块 → "
                          f"{d.get('kept_parts','-')} 块 · {d.get('faces','-')} 面")
    return None, f"✗ 精修失败：{r.get('error') or (r.get('log') or '')[-300:]}"


def do_multiview(front, back, left, right, quality, progress=None):
    """多视图：四张**白底原图**直传 2mv（不预遮罩、不归一化）。"""
    _free_gpu()   # 让出对话模型，避免 OOM（Strata 125B 常驻约 28 GB）
    from backend.pipeline import Pipeline

    views = {"front": front, "back": back, "left": left, "right": right}
    miss = [k for k, v in views.items() if not v]
    if miss:
        return None, f"缺视图：{miss}（顺序必须 正→背→左→右）"
    pipe = Pipeline(str(ROOT / "config.json"))
    t0 = time.time()
    r = pipe.generate_3d_multiview(views, quality_name=str(quality), seed=42,
                                   octree_override=512, max_facenum=500000)
    if getattr(r, "success", False) and r.output_path:
        try:
            import trimesh
            m = trimesh.load(str(r.output_path), force="mesh")
            detail = f"{len(m.faces):,} 面 · 水密={m.is_watertight}"
        except Exception:
            detail = ""
        out = _out_dir("multiview") / Path(r.output_path).name
        shutil.copy2(r.output_path, out)
        return str(out), f"✓ 多视图完成 {time.time()-t0:.0f}s · {detail}"
    return None, f"✗ 多视图失败：{getattr(r, 'error', '')}"


def do_motion(bvh_name, advance, progress=None):
    """动画：把 BVH 重定向到内置骨架上（纯 CPU）。"""
    import subprocess

    # 2026-11-28：脚本收进 tools/backends。动画有两条路：
    #   ① 预烘焙动作包（work/research/motion/actions/<key>.blend，52 骨 + 具名 Action）——直接交付
    #   ② 现场重定向（需要基础骨架工程 + BVH）——用户自己换动作时用
    from tools.backends import resolve as _resolve
    motion_dir = ROOT / "work" / "research" / "motion"
    lib = _resolve("motion/retarget_lib.py")
    key = str(bvh_name).split("_")[0].lower()
    baked = {"walk": "walk", "02": "walk", "08": "walk", "run": "run", "09": "run",
             "bow": "bow", "111": "bow", "wave": "wave", "141": "wave",
             "idle": "idle", "77": "idle"}.get(key)
    baked_path = motion_dir / "actions" / f"{baked}.blend" if baked else None
    if baked_path and baked_path.is_file():
        out = _out_dir("motion") / f"motion_{baked}_{int(time.time())}.blend"
        shutil.copy2(baked_path, out)
        return str(out), f"✓ 交付预烘焙动作「{baked}」（52 骨 + 具名 Action，{baked_path.stat().st_size/1024/1024:.0f} MB）"
    blend = motion_dir / "人物_BVH步态_重定向.blend"
    bvh = motion_dir / "bvh" / str(bvh_name)
    if lib is None:
        return None, "缺动画后端脚本（tools/backends/motion/retarget_lib.py）"
    if not blend.is_file():
        return None, "既没有预烘焙动作包（work/research/motion/actions/），也没有基础骨架工程；" \
                     "先跑 python tools/bootstrap_models.py --only motion 或提供 .blend"
    if not bvh.is_file():
        return None, f"缺 BVH：{bvh_name}"
    out_blend = _out_dir("motion") / f"motion_{bvh.stem}_{int(time.time())}.blend"
    inner = _out_dir("motion") / f"_run_{bvh.stem}.py"
    inner.write_text(
        f'import sys, json\n'
        f'sys.path.insert(0, r"{motion_dir}")\n'
        f'import retarget_lib\n'
        f'r = retarget_lib.retarget_bvh(r"{blend}", r"{bvh}", r"{out_blend}",\n'
        f'    n_frames=24, mode="cycle", action_name="ui_{bvh.stem[:6]}")\n'
        f'print("METRICS", json.dumps(r.get("metrics") if isinstance(r, dict) else {{}}))\n',
        encoding="utf-8")
    blender = r"D:\tools\blender\blender-4.2.23-windows-x64\blender.exe"
    t0 = time.time()
    p = subprocess.run([blender, "-b", "--factory-startup", "--python", str(inner)],
                       capture_output=True, timeout=900)
    log = (p.stdout or b"").decode("utf-8", "replace") + \
          (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0 or not out_blend.is_file():
        return None, f"✗ 重定向失败：{log[-300:]}"
    line = next((ln.strip() for ln in log.splitlines() if "penetration=" in ln), "")
    return str(out_blend), f"✓ 重定向完成 {time.time()-t0:.0f}s · {line[:120]}"


# ───────────────────────── UI ─────────────────────────

def render():
    """在 app 的 Blocks 里渲染五个能力面板。"""
    import gradio as gr

    bvh_choices = ["02_01.bvh", "08_01.bvh", "09_01_run.bvh", "111_37_wave.bvh",
                   "113_02_bow.bvh", "77_02_idle.bvh"]
    try:
        from backend import env
        d = ROOT / "work" / "research" / "motion" / "bvh"
        if d.is_dir():
            found = sorted(p.name for p in d.glob("*.bvh"))
            bvh_choices = found or bvh_choices
    except Exception:
        pass

    with gr.Accordion("🛠 能力工作台 · 骨骼 / 贴图 / 精修 / 多视图 / 动画", open=False,
                      elem_id="capability_lab"):
        gr.Markdown("这些是**产品入口**：和命令行 `tools/asset_refine_rig.py`、"
                    "`tools/paint_asset.py` 走同一套代码。长任务（贴图）请耐心等，"
                    "日志会显示耗时与显存。")

        with gr.Tabs():
            # ① 骨骼
            with gr.Tab("① 骨骼绑定"):
                with gr.Row():
                    rig_mesh = gr.File(label="网格（.stl / .glb / .obj）", file_types=[".stl", ".glb", ".obj"])
                    with gr.Column():
                        rig_tier = gr.Radio(
                            ["template", "riganything"],
                            value="template",
                            label="档位（template=CPU 快；riganything=角色语义骨，要 5 GB 显存）")
                        rig_tmpl = gr.Dropdown(
                            ["auto", "humanoid", "quadruped", "aircraft", "chain"],
                            value="auto", label="模板（仅 template 档有效）")
                        rig_btn = gr.Button("开始绑定", variant="primary")
                rig_out = gr.Model3D(label="绑定结果（拖动旋转）", height=300)
                rig_dl = gr.File(label="下载 GLB")
                rig_log = gr.Markdown("等待开始")

                def _rig(mesh, tier, tmpl):
                    path, msg = do_rig(getattr(mesh, "name", mesh), tier, tmpl)
                    if path:
                        _register_asset(path, "rigged", {"tier": tier}, _glb_metrics(path))
                    return path, path, msg

                rig_btn.click(_rig, inputs=[rig_mesh, rig_tier, rig_tmpl],
                              outputs=[rig_out, rig_dl, rig_log], api_name="cap_rig")

            # ② 贴图
            with gr.Tab("② 贴图 PBR"):
                with gr.Row():
                    paint_mesh = gr.File(label="网格", file_types=[".stl", ".glb", ".obj"])
                    paint_img = gr.Image(label="参考图（白底原图最好）", type="filepath")
                paint_quality = gr.Radio(
                    ["完整档（6 视角 · 约 20 分钟）", "快速档（预览，约 8 分钟）"],
                    value="快速档（预览，约 8 分钟）", label="质量档")
                paint_btn = gr.Button("开始上色", variant="primary")
                paint_out = gr.Model3D(label="上色结果", height=300)
                paint_dl = gr.File(label="下载 GLB（含 PBR 贴图）")
                paint_log = gr.Markdown("等待开始")

                def _paint(mesh, img, quality):
                    path, msg = do_paint(getattr(mesh, "name", mesh), img, quality)
                    if path:
                        _register_asset(path, "painted", {"quality": quality}, _glb_metrics(path))
                    return path, path, msg

                paint_btn.click(_paint, inputs=[paint_mesh, paint_img, paint_quality],
                                outputs=[paint_out, paint_dl, paint_log], api_name="cap_paint")

            # ③ 精修
            with gr.Tab("③ 精修（Direct3D-V3）"):
                gr.Markdown("需要两张输入：**参考图** + **粗网格**（一般是刚生成的 GLB/OBJ）。")
                with gr.Row():
                    ref_img = gr.Image(label="参考图", type="filepath")
                    ref_mesh = gr.File(label="粗网格（.obj / .glb）", file_types=[".obj", ".glb"])
                ref_steps = gr.Radio([4, 15], value=4, label="采样步数（4 快，15 细）")
                ref_btn = gr.Button("开始精修", variant="primary")
                ref_out = gr.Model3D(label="精修结果（已过小壳过滤）", height=300)
                ref_dl = gr.File(label="下载 GLB")
                ref_log = gr.Markdown("等待开始")

                def _refine(img, mesh, steps):
                    path, msg = do_refine(img, getattr(mesh, "name", mesh), steps)
                    if path:
                        _register_asset(path, "refined", {"steps": steps}, _glb_metrics(path))
                    return path, path, msg

                ref_btn.click(_refine, inputs=[ref_img, ref_mesh, ref_steps],
                              outputs=[ref_out, ref_dl, ref_log], api_name="cap_refine")

            # ④ 多视图
            with gr.Tab("④ 多视图重建"):
                gr.Markdown("**四张白底原图**，顺序 正 → 背 → 左 → 右，同仰角、方位严格 90°。"
                            "不要预先抠图（会被再抠一遍，实测更差）。")
                with gr.Row():
                    mv_front = gr.Image(label="正面", type="filepath")
                    mv_back = gr.Image(label="背面", type="filepath")
                    mv_left = gr.Image(label="左侧", type="filepath")
                    mv_right = gr.Image(label="右侧", type="filepath")
                mv_q = gr.Radio(["标准", "高质量"], value="高质量", label="精度档")
                mv_btn = gr.Button("开始重建", variant="primary")
                mv_out = gr.Model3D(label="重建结果", height=300)
                mv_dl = gr.File(label="下载 GLB")
                mv_log = gr.Markdown("等待开始")

                def _mv(a, b, c, d, q):
                    path, msg = do_multiview(a, b, c, d, q)
                    if path:
                        _register_asset(path, "multiview", {"quality": q}, _glb_metrics(path))
                    return path, path, msg

                mv_btn.click(_mv, inputs=[mv_front, mv_back, mv_left, mv_right, mv_q],
                             outputs=[mv_out, mv_dl, mv_log], api_name="cap_multiview")

            # ⑤ 动画
            with gr.Tab("⑤ 动画（BVH 重定向）"):
                gr.Markdown("把公开 mocap 动作重定向到内置 52 骨人形骨架。**纯 CPU**，约 10 秒。")
                with gr.Row():
                    mo_bvh = gr.Dropdown(bvh_choices, value=bvh_choices[0] if bvh_choices else None,
                                         label="动作（BVH）")
                    mo_adv = gr.Checkbox(value=False, label="高级：保留更多帧（默认 24 帧循环）")
                    mo_btn = gr.Button("开始重定向", variant="primary")
                mo_dl = gr.File(label="动画工程（.blend，含具名 Action）")
                mo_log = gr.Markdown("等待开始")

                def _mo(bvh, adv):
                    path, msg = do_motion(bvh, adv)
                    if path:
                        _register_asset(path, "motion", {"bvh": bvh}, {})
                    return path, msg

                mo_btn.click(_mo, inputs=[mo_bvh, mo_adv],
                             outputs=[mo_dl, mo_log], api_name="cap_motion")

            # ⑥ 我的资产
            with gr.Tab("⑥ 我的资产"):
                gr.Markdown("所有能力的产物都在这里。按类型 / 时间排列，"
                            "超容量会自动淘汰最旧的（默认 8 GB，"
                            "可用环境变量 `FORGEFORM_ASSET_MAX_BYTES` 改）。")
                asset_html = gr.HTML(_asset_html())
                with gr.Row():
                    asset_refresh = gr.Button("🔄 刷新", size="sm")
                    report_btn = gr.Button("🩺 遇到问题？生成诊断包", size="sm")
                report_file = gr.File(label="诊断包（已脱敏，可直接贴 issue）", visible=False)
                report_note = gr.Markdown("")

                def _refresh():
                    return _asset_html()

                def _report():
                    try:
                        from tools.doctor import build_report
                        p = build_report()
                        return gr.update(value=str(p), visible=True), \
                            f"✓ 诊断包已生成：`{p}`（key/token/密码已打码，发之前可先自己看一遍）"
                    except Exception as e:
                        return gr.update(visible=False), f"✗ 生成失败：{type(e).__name__}: {e}"

                asset_refresh.click(_refresh, outputs=[asset_html], api_name="cap_assets")
                report_btn.click(_report, outputs=[report_file, report_note], api_name="cap_report")


# ── 并发防护（2026-11-28）──
def _ff_cap_guard():
    from . import gpu_guard
    for name, kind in (("do_rig", "rig"), ("do_paint", "paint"), ("do_refine", "refine"),
                       ("do_multiview", "multiview"), ("do_motion", "motion")):
        fn = globals().get(name)
        if fn and not getattr(fn, "_ff_guarded", False):
            globals()[name] = gpu_guard.guarded(kind)(fn)


_ff_cap_guard()
