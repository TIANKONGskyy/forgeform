"""铸想 ForgeForm 的 canonical 验证入口（结构 + 行为 + canonical 套件，约 0.5s）。

为什么要这个文件：验证逻辑原本散在 Temp 的一次性脚本里 —— 路径随机、每次新建
都会给"待验证路径"集合增加一条永远无法收敛的记录（删掉的路径更是无法哈希）。
固化到仓库后路径稳定，任何会话都能一条命令复现。

用法：
    python tools/verify.py          # 全部检查
    python tools/verify.py --fast   # 跳过会跑测试套件的那一项

⚠️ 写验证脚本时的两个坑（都踩过）：
  1. 不要 `import app` —— app.py 导入时会启动后台 bootstrap **预热对话模型**，
     会把 12GB 模型拉回显存；若此时正在微调/生成，会互相抢显存（实测步时 14s→53s）。
     需要看 UI 行为就做源码契约断言，别导入。
  2. 隔离 PROJECTS_DIR 后再导入本仓库任何模块，否则会往真实 projects/ 里写东西
     （app.py 导入时还会实例化 ConversationManager）。
"""


# === 端到端 bug A 守门：ast 扫描确认联网找图不自动设 ref_upload（不算注释）===
def _func_auto_sets_ref_upload(src_app: str, fn_name: str) -> bool:
    tree = ast.parse(src_app)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == fn_name:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if (isinstance(t, ast.Subscript) and
                            isinstance(t.value, ast.Name) and t.value.id == "state" and
                            isinstance(t.slice, ast.Constant) and t.slice.value == "ref_upload"):
                            return True
            return False
    return False


import argparse
import ast
import collections
import inspect
import json
import shutil
import subprocess
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import trimesh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = []


def ck(name: str, cond, detail: str = ""):
    OK.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))


def check_app_source() -> None:
    """结构断言：不导入 app.py，避免副作用（曾因模糊补丁吃掉 def build(): 并产生重复函数）"""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    names = [n.name for n in ast.parse(src).body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    ck("app_no_duplicate_defs", not [k for k, v in collections.Counter(names).items() if v > 1])
    ck("app_has_build", "build" in names)
    ck("app_has_understanding_steps", "⏳ 正在理解你的需求…" in src)
    ck("app_sidebar_params_block", 'sb-label">当前参数' in src)
    ck("app_has_model3d_viewer", "gr.Model3D(" in src)

    # 建模后的轮转时机（用户反馈：建模完看不到模型，却直接新建了对话）
    ck("app_hands_glb_to_viewer_in_agent_path",
       '_update_ui(res["project"], res.get("glb"))' in src,
       "agent 路径（现在的主路径）不传 glb，建模完 3D 查看器就对着空组")
    src_c0 = (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8")
    ck("modeling_defers_rotation",
       "def rotate_if_finished" in src_c0 and "pending_rotate" in src_c0
       and '"auto_new": False' in src_c0,
       "3D 完立刻 new_project 会让本轮返回空白新组 → 模型看不见；改为打标记、下条消息再轮转")
    src_pm = (ROOT / "backend" / "project_manager.py").read_text(encoding="utf-8")
    ck("pending_rotate_persisted",
       '"pending_rotate": bool(meta.get("pending_rotate"))' in src_pm
       and 'meta["pending_rotate"] = bool(state.get("pending_rotate"))' in src_pm,
       "不落盘的话重启后这一组永远不轮转（模型一直挂在同组）")
    ck("new_project_avoids_name_collision",
       "while folder.exists()" in src_pm,
       "目录名只到秒，同一秒连建两组会撞名并静默合并 → 必须加序号")
    ck("rotation_respects_continuation",
       '"export_stl", "open_bambu", "generate_3d", "modify_image"' in src_c0,
       "继续改这个模型（导 STL/打印/重建模）时要留在本组，别把人家的模型丢了")


def check_viewer_behavior(tmp: Path) -> None:
    """行为断言：需要导入 app → 先隔离 PROJECTS_DIR"""
    import backend.project_manager as pm
    keep = pm.PROJECTS_DIR
    pm.PROJECTS_DIR = tmp
    try:
        import app as A
        (tmp / "candidates").mkdir(exist_ok=True)
        glb = tmp / "model.glb"
        glb.write_bytes(b"glTF-fake")
        base = {"folder": "f", "path": str(tmp), "phase": "stl", "conversation": [],
                "glb_path": str(glb), "stl_path": "", "auto_params": {}}
        get = lambda u, k, d=None: u.get(k, d) if hasattr(u, "get") else d

        vals = A._update_ui(base)
        # 11 = chat / hero / viewer / refine / gen / edit / model / new / **stl** / sidebar / history
        # （2026-09-23 加了「📦 导出 STL」按钮 → 10 → 11；数量必须与 ui_outputs 对齐）
        # 13 = chat, hero, viewer, 6 按钮, sidebar, history, 环形进度条, STL 文件卡片
        # （这条守卫的价值就是"元数一漂移就报错"——今天从 11→12→13 都是它提醒的）
        ck("ui_returns_14_values", len(vals) == 14, str(len(vals)))
        ck("viewer_visible_with_glb", get(vals[2], "visible") is True)
        ck("viewer_value_is_glb", str(get(vals[2], "value", "")) == str(glb))
        ck("viewer_hidden_without_glb",
           get(A._update_ui({**base, "glb_path": ""})[2], "visible") is False)
        # 建模成功会切到新对话组 → 新 state 没有 glb，必须靠 override 把模型留住
        ck("glb_override_survives_auto_new",
           get(A._update_ui({**base, "glb_path": ""}, glb_override=str(glb))[2], "visible") is True)
        # _update_ui_with_note 必须追加气泡，不能替换整个对话
        cs = {**base, "phase": "confirm", "glb_path": "",
              "conversation": [{"role": "user", "content": "茶杯垫"},
                               {"role": "assistant", "content": "好的"}]}
        w = A._update_ui_with_note(cs, "⏳ 正在理解你的需求…")
        ck("with_note_appends_not_replaces",
           len(w[0]) == len(A._chat_value(cs["conversation"])) + 1)
        ck("with_note_keeps_history", "茶杯垫" in str(w[0]))
    finally:
        pm.PROJECTS_DIR = keep


def check_pipeline_stages(tmp: Path) -> None:
    """两阶段生图 + 3D 链路：四个 Pipeline 方法全打桩（漏桩 = 真调 ComfyUI）"""
    import backend.conversation as C
    import backend.pipeline as pl
    import backend.project_manager as pm

    keys = pl.Pipeline().get_resolutions()
    ck("preview_res_key_valid", C._PREVIEW_RES in keys, C._PREVIEW_RES)
    ck("hd_res_key_valid", C._HD_RES in keys, C._HD_RES)
    ck("followup_keyed_on_base_prompt_only",
       C._is_requirement_followup({"phase": "empty", "base_prompt": "x"}) is True
       and C._is_requirement_followup({"phase": "confirm", "base_prompt": ""}) is False)
    ck("3d_result_carries_glb",
       '"glb": glb_path' in inspect.getsource(C.ConversationManager._do_generate_3d))

    # 隔离 PROJECTS_DIR：3D 成功后调 archive_project / new_project，会用模块级路径
    keep_dir = pm.PROJECTS_DIR
    pm.PROJECTS_DIR = tmp

    orig = (pl.Pipeline.generate_images, pl.Pipeline.enhance_image,
            pl.Pipeline.generate_3d, pl.Pipeline.export_stl)
    warm_orig=C._rewarm_tuned_async,C._warm_image_model_async
    C._rewarm_tuned_async=lambda: None
    C._warm_image_model_async=lambda *a,**k: None
    save = pm.save_project
    calls = []

    def r(p):
        p = Path(p); p.write_bytes(b"d")
        return SimpleNamespace(success=True, output_path=str(p), metadata={})

    def gen(_p, prompt, count=1, model_key="", quality_name="", resolution="", seed=-1,
            negative="", progress_cb=None, skip_cache=False, steps_override=0,
            reference_image=None):
        calls.append(("gen", resolution, quality_name, skip_cache, reference_image))
        return [r(tmp / "g.png")]

    def hd(_p, image_path, prompt, **kw):
        calls.append(("hd", kw.get("resolution")))
        (tmp / "hd.png").write_bytes(b"hd")      # 必须真建文件，否则被判为重绘失败
        return str(tmp / "hd.png")

    def g3(_p, **kw):
        calls.append(("g3", kw.get("model_key"))); return r(tmp / "m.glb")

    def es(_p, glb, **kw):
        calls.append(("stl", kw.get("optimize_preset"))); return r(tmp / "m.stl")

    pl.Pipeline.generate_images, pl.Pipeline.enhance_image = gen, hd
    pl.Pipeline.generate_3d, pl.Pipeline.export_stl = g3, es
    pm.save_project = lambda st: None
    try:
        (tmp / "t").mkdir(exist_ok=True)
        m = C.ConversationManager.__new__(C.ConversationManager)
        m.current_project = {
            "folder": "t", "path": str(tmp), "phase": "confirm", "base_prompt": "一个茶杯垫",
            "user_prompt": "一个茶杯垫", "refinements": [], "req_notes": ["一个茶杯垫"],
            "auto_params": {"style": "写实", "size_mm": 90, "color": "绿色", "model": "z_image"},
            "stl_size": 90, "conversation": [], "candidates": [], "selected": "",
            "selected_idx": -1, "glb_path": "", "stl_path": ""}
        m._append_conversation = lambda *a, **k: None
        m._do_generate_images(m.current_project)
        g = [c for c in calls if c[0] == "gen"][0]
        ck("preview_is_512_fast_skipcache",
           g[1] == C._PREVIEW_RES and g[2] == "快速" and g[3] is True)
        ck("img_stage_is_preview", m.current_project["img_stage"] == "preview")

        calls.clear()
        (tmp / "prev.png").write_bytes(b"p")
        m.current_project.update(selected=str(tmp / "prev.png"),
                                 candidates=[str(tmp / "prev.png")],
                                 img_stage="preview", gen_prompt="一个茶杯垫")
        res = m._do_generate_3d(m.current_project)
        order = [c[0] for c in calls]
        ck("hd_runs_before_3d",
           "hd" in order and "g3" in order and order.index("hd") < order.index("g3"), str(order))
        # 默认 3D 模型 = config 里的 stages.3d.default_model。
        # 2026-09-23 宇凯要求默认切 Hunyuan3D 2.1：TripoSplat 本机产出碎块云（实测 8 组
        # 全是 2000~23000 个连通块），已不再作默认。
        ck("3d_uses_config_default_model",
           [c for c in calls if c[0] == "g3"][0][1] == "hunyuan21",
           f"实际 {[c for c in calls if c[0] == 'g3'][0][1]}")
        ck("stl_uses_print_ready", [c for c in calls if c[0] == "stl"][0][1] == "print_ready")
        ck("result_carries_glb", bool(res.get("glb")))
        # 用户反馈修正：建模完**不再立刻新建对话组**（那会让本轮返回空白新组、模型看不见），
        # 改为留在本组并把轮转推迟到下一条消息（见 rotate_if_finished）
        ck("rotation_deferred_after_3d",
           res.get("auto_new") is False and res.get("rotate_pending") is True
           and res["project"]["phase"] != "empty"
           and bool(res["project"].get("glb_path")))
        ck("reply_mentions_hd_rerender", "高清重绘" in res["reply"])
    finally:
        (pl.Pipeline.generate_images, pl.Pipeline.enhance_image,
         pl.Pipeline.generate_3d, pl.Pipeline.export_stl) = orig
        C._rewarm_tuned_async,C._warm_image_model_async=warm_orig
        pm.save_project = save
        pm.PROJECTS_DIR = keep_dir


def check_launcher() -> None:
    """启动链路：只保留 UTF-8 根因修复，**流程结构保持原样**

    2026-09-22 真事故：Start-Process -RedirectStandardOutput 打开的是 GBK 文件流，
    未设 PYTHONIOENCODING 时 ComfyUI 的 app/logger.py 写 emoji 抛 UnicodeEncodeError，
    异常从 print() 冒出 → 绑定端口前进程即死 → 8188 永不监听 → 就绪等待必然超时
    （用户看到"卡在开启 ComfyUI"）。修复只加两行环境变量，不改脚本流程。
    """
    from backend import startup
    for k, v in (("PYTHONIOENCODING", "utf-8"), ("PYTHONUTF8", "1")):
        ck(f"comfy_env_{k.lower()}", startup.COMFY_ENV.get(k) == v,
           str(startup.COMFY_ENV.get(k)))

    ps1 = (ROOT / "start-all.ps1").read_text(encoding="utf-8")
    ck("launcher_sets_pythonioencoding", "$env:PYTHONIOENCODING = 'utf-8'" in ps1)
    ck("launcher_sets_pythonutf8", "$env:PYTHONUTF8 = '1'" in ps1)
    # 结构必须保持原版（用户要求"改回去"）：原有串行就绪等待仍在
    ck("launcher_keeps_original_serial_wait", "while (-not (Test-Port 8188)" in ps1,
       "已是原版流程")
    # ★ 2026-10-19：旧断言写的是 RTX 3080 时代的 "(single-GPU --lowvram) starting"。
    #   换到 V100 后 --lowvram 会在采样阶段报 CUDA error（见 AGENTS/startup.py 实测），
    #   启动脚本已改为不带 --lowvram 的 single-GPU 分支 —— 断言跟着改，但"流程标记仍在"的意图不变。
    ck("launcher_keeps_original_flow_markers",
       "(3/4) ComfyUI 8188 (single-GPU) starting" in ps1 and "(4/4) App starting" in ps1)
    # 只查可执行代码，注释里解释 --lowvram 为什么不用的文字不算
    _ps1_code = [ln for ln in ps1.splitlines() if not ln.strip().startswith("#")]
    ck("launcher_never_uses_lowvram",
       all("--lowvram" not in ln for ln in _ps1_code),
       "V100 上 --lowvram = 采样阶段 CUDA error（实测），启动链路里绝不能出现")
    # 我上轮重排引入的标记必须已被撤掉
    ck("launcher_revert_no_passthru", "-PassThru" not in ps1)
    ck("startup_redirects_comfy_logs",
       "comfyui-8188-stdout.log" in (ROOT / "backend" / "startup.py").read_text(encoding="utf-8"))


def check_prompt_and_preview() -> None:
    """生图提示词扩写+英译 / 3D 预览图（2026-09-22 两项修复）"""
    import backend.describer as D
    import backend.prompt_enhancer as PE
    import backend.render3d as R3

    # ── 提示词：清理函数的边界（这是"草率"回归的第一道闸）──
    # 2026-11-13：`_clean_image_prompt` 出口会补干净背景（isolated on white…），
    # 所以这里只验"前缀被去掉"，不要求整串完全相等
    ck("clean_strips_prompt_prefix",
       D._clean_image_prompt('Prompt: a matte blue phone stand, studio light, 8k')
       .startswith("a matte blue phone stand, studio light, 8k"))
    ck("clean_adds_white_background",
       "white background" in D._clean_image_prompt(
           'Prompt: a matte blue phone stand, studio light, 8k'))
    ck("clean_rejects_chinese", D._clean_image_prompt("这是一个蓝色手机支架，写实风格，非常好") == "")
    ck("clean_rejects_too_short", D._clean_image_prompt("blue stand") == "")
    ck("clean_strips_markdown_and_newlines",
       "\n" not in D._clean_image_prompt(
           "```\nA detailed matte black bracket with soft studio lighting, 8k, sharp\n```"))
    long_ok = D._clean_image_prompt(
        "a matte blue phone stand, single cylindrical base, soft studio lighting, "
        "seamless white background, 45 degree view, ultra detailed, 8k, sharp focus")
    ck("clean_accepts_good_prompt", len(long_ok) > 40)

    src_conv = (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8")

    # ── 扩写函数接线 ──
    ck("enhance_image_prompt_exists", callable(getattr(D, "enhance_image_prompt", None)))
    ck("image_gen_calls_enhancer", "describer.enhance_image_prompt(" in src_conv)
    ck("reply_shows_english_prompt", "实际喂给模型的英文提示词" in src_conv)

    # ── 翻译模型不能再写死那个没装的 tag ──
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    want = cfg.get("models", {}).get("llm", {}).get("default_model")
    ck("translate_model_is_installed_one", PE.TRANSLATE_MODEL == want,
       f"{PE.TRANSLATE_MODEL} (config={want})")
    ck("translate_model_not_dead_tag", PE.TRANSLATE_MODEL != "qwen3:0.6b")

    # ── 3D 预览图 ──
    ck("render_mesh_preview_exists", callable(getattr(R3, "render_mesh_preview", None)))
    box = trimesh.creation.box(extents=(20, 30, 40))
    thinned = R3._decimate(box, target=4)
    ck("decimate_reduces_faces", len(thinned.faces) <= len(box.faces))
    bc = R3._base_color(box)
    ck("base_color_in_range", all(0.0 <= c_ <= 1.0 for c_ in bc), str(bc))
    # 空/损坏资产必须安静放弃（否则 numpy 空切片异常会冒到日志里）
    with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as f:
        f.write(b"glTF-fake")
        bogus = f.name
    try:
        ck("render_returns_empty_on_bad_asset", R3.render_mesh_preview(bogus) == "")
    finally:
        Path(bogus).unlink(missing_ok=True)
    ck("3d_appends_preview_to_chat",
       "render_mesh_preview(src_mesh)" in src_conv and "conv_images" in src_conv)
    ck("preview_prefers_stl_over_glb",
       'src_mesh = state.get("stl_path") or glb_path' in src_conv,
       "STL 是优化过的可打印网格；GLB 含飞面碎片，预览会像破的")
    ck("3d_returns_preview_key", '"preview": preview_png' in src_conv)

    # ── 查看器只在「这一组真的产出了模型」时展示 ──
    # 2026-09-22 修：曾用 last_glb 回退，把上一组的模型带进新组 →
    # 用户一发消息（新组还没建模）就弹出展示框。这条守住回归。
    src_app = (ROOT / "app.py").read_text(encoding="utf-8")
    ck("viewer_does_not_fall_back_to_last_glb",
       'state.get("last_glb")' not in src_app and 'state["last_glb"]' not in src_app)
    ck("viewer_uses_override_then_own_glb",
       'glb = glb_override or state.get("glb_path") or ""' in src_app)
    src_pm = (ROOT / "backend" / "project_manager.py").read_text(encoding="utf-8")
    ck("last_glb_no_longer_persisted",
       "last_glb" not in src_pm and "last_glb" not in src_conv,
       "新组必须保持空白，否则展示框会提前弹出")

    # ── Gradio tuples 只允许一条消息一个文件（踩过的 ValidationError）──
    ck("chat_value_single_file_form", "bot_msg = (files[0], ai_reply) if files else ai_reply" in src_app)


def check_agent_layer() -> None:
    """专家 agent：LLM 读对话决定工作流动作（2026-09-22 架构改造）"""
    import backend.agent as AG
    import backend.describer as D

    need = {"set_requirement", "set_param", "generate_image", "enhance_image",
            "modify_image", "generate_3d", "export_stl", "new_conversation",
            "switch_conversation", "open_bambu", "answer"}
    ck("agent_tools_cover_workflow", need <= set(AG.TOOLS),
       f"缺 {need - set(AG.TOOLS)}")
    ck("agent_spec_lists_every_tool", all(n in AG._SYS for n in AG.TOOLS))
    ck("agent_prompt_forbids_new_actions", "只能从中选" in AG._SYS)

    ck("agent_extract_json_robust",
       AG._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
       and AG._extract_json('x {"a": 1} y') == {"a": 1}
       and AG._extract_json("不是 JSON") is None)
    ck("agent_clamps_size", AG.validate_args("set_param", {"size_mm": 9999})["size_mm"] == 300.0
       and AG.validate_args("set_param", {"size_mm": 3})["size_mm"] == 20.0)
    ck("agent_drops_bad_resolution",
       "resolution" not in AG.validate_args("set_param", {"resolution": "不存在的档"}))
    ck("agent_drops_unknown_model",
       "model" not in AG.validate_args("set_param", {"model": "不存在的模型"}))

    # 用假 LLM 输出跑真实 plan()：未知动作必须被丢、合法动作要保留
    # ⚠️ 先钉住 provider=ollama —— 下面桩的是 Ollama 路径；config 切成 tuned 时
    #    会走 HTTP 绕过桩，检查就变成依赖环境了（踩过）
    _prov, _style = AG._provider, AG.prompt_style
    AG._provider, AG.prompt_style = (lambda: "ollama"), (lambda: "full")
    orig = D._call_ollama
    try:
        D._call_ollama = lambda prompt, timeout=60, **kw: (
            '{"understanding":["x"],"reply":"r","actions":'
            '[{"name":"rm_rf","args":{}},{"name":"answer","args":{}}]}')
        with patch('backend.gpu_state.is_generating',return_value=False),patch.object(AG,'is_ready',return_value=True):
            pl = AG.plan("随便", {"phase": "confirm"})
        ck("agent_whitelist_drops_unknown", [a["name"] for a in pl["actions"]] == ["answer"])
        D._call_ollama = lambda prompt, timeout=60, **kw: "我不会 JSON"
        with patch('backend.gpu_state.is_generating',return_value=False),patch.object(AG,'is_ready',return_value=True):
            ck("agent_plan_none_on_garbage", AG.plan("随便", {"phase": "confirm"}) is None)
    finally:
        D._call_ollama = orig
        AG._provider, AG.prompt_style = _prov, _style

    # 接线：agent 优先 + 兜底 + 可关
    src_c = (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8")
    ck("conversation_tries_agent_first",
       "if agent_enabled():" in src_c and "handle_agent_turn(text)" in src_c)
    ck("conversation_falls_back_to_keywords", "回退关键词路径" in src_c)
    ck("agent_turn_returns_none_when_no_plan",
       "def handle_agent_turn" in src_c and "if not pl:" in src_c)
    ck("agent_reply_reports_understanding",
       '"understanding": pl["understanding"]' in src_c)
    src_a = (ROOT / "app.py").read_text(encoding="utf-8")
    ck("app_routes_through_agent",
       "conv.handle_agent_turn_stream(user_input)" in src_a and "正在分析你的意图" in src_a)
    # agent_enabled 是模块级函数，写成 conv.agent_enabled() 会让提交直接抛 AttributeError
    ck("app_uses_module_level_agent_enabled",
       "conv.agent_enabled()" not in src_a and "agent_enabled()" in src_a)
    ck("app_shows_decision_trace", "执行：" in src_a)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ck("agent_toggle_in_config", "enabled" in cfg.get("agent", {}),
       str(cfg.get("agent")))
    from backend import conversation as CV
    ck("agent_enabled_reads_config", CV.agent_enabled() == bool(cfg["agent"]["enabled"]))

    # ── 上下文窗口：Agent 的长 prompt 会撞 Ollama 默认窗口，输出被从中间截断 → JSON 解析失败 ──
    import backend.describer as DD
    prof = DD._profile()
    ck("llm_num_ctx_widened", int(prof.get("num_ctx") or 0) >= 8192,
       f"num_ctx={prof.get('num_ctx')}")
    src_d = (ROOT / "backend" / "describer.py").read_text(encoding="utf-8")
    ck("num_ctx_passed_to_ollama", 'opts["num_ctx"]' in src_d)
    ck("top_level_think_used_as_default", 'LLM_CFG.get("think", False)' in src_d,
       "顶层 models.llm.think 曾被忽略，改它没反应")

    # ── 思考深度可见：接住 Ollama 的 thinking 字段并带到 UI ──
    ck("thinking_field_captured", "LAST_THINKING" in src_d and 'd.get("thinking")' in src_d)
    src_ag = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
    ck("plan_exposes_thinking", '"thinking": _thinking_gist' in src_ag
       and "def _thinking_gist" in src_ag)
    ck("prompt_asks_for_tradeoffs", "权衡" in AG._SYS and "考虑过 X 还是 Y" in AG._SYS)
    ck("app_shows_thinking_line", "🧠 思考" in src_a and "🎯 判断" in src_a)
    # 用户 2026-09-23 反馈：缺少 GLB→STL 这一步（以前只有自动导一次 / agent 判断两个入口）
    ck("stl_export_button_wired",
       # 按钮文案会变（emoji / 措辞），这里只认"声明存在"，不绑死具体字符串
       re.search(r'btn_stl\s*=\s*gr\.Button\(', src_a) is not None
       and "btn_stl.click(fn=on_btn_export_stl" in src_a
       and "btn_stl" in src_a.split("ui_outputs = [")[1].split("]")[0]   # 必须在 ui_outputs 里
       and "def on_btn_export_stl" in src_a,
       "声明 / 事件 / ui_outputs 三处缺一不可（漏 ui_outputs 会显示但不刷新）")
    ck('stl_button_yield_state_first',
       # 断言"第一位是 state"本身（形态可能变，但这一条不能变）
       ('yield (prj, *_update_ui_with_note(prj' in src_a or 'yield prj, *_update_ui' in src_a)
       and 'yield state, *_update_ui_with_note(state, ' in src_a,
       '按钮类 handler 第一位必须是 state（写成空串在最前会把 "" 写进 state 组件）')
    ck('stl_button_surfaces_reply',
       'reply = (result.get("reply")' in src_a
       and '_update_ui_with_note(prj, reply' in src_a
       # 就地读，别用后面才定义的 src_conv（我第一版就踩了 UnboundLocalError ✗）
       and '文件：`{stl_path}`' in (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8"),
       '成功要报路径/尺寸、失败要给原因，否则点了像没反应')
    # 用户 2026-09-23 报「建模有大问题」：TripoSplat 输出全是碎块云却一路当成功放行
    src_cc = (ROOT / "backend" / "comfy_client.py").read_text(encoding="utf-8")
    src_pl = (ROOT / "backend" / "pipeline.py").read_text(encoding="utf-8")
    ck("triposplat_official_sampler",
       '"steps": int(tp_q.get("steps", 20))' in src_cc
       and 'tp_q.get("sampler", "dpmpp_2m")' in src_cc
       and 'float(tp_q.get("cfg", 3.0))' in src_cc,
       "官方蓝图是 20 步/cfg 3/dpmpp_2m；原来是 30/cfg5/euler")
    ck("triposplat_ladder_reaches_workflow",
       'params["_tripo_quality"]' in src_pl and 'is_tripo' in src_pl,
       "原来只传 octree/guidance，TripoSplat 根本不读 → 精度档是摆设")
    ck("splat_to_mesh_reads_ladder",
       'tp_q.get("min_component", 500)' in src_cc and 'tp_q.get("resolution", 384)' in src_cc)
    # 用户 2026-09-23 报"模型躺着"：GLB 是 Y-up、预览/打印是 Z-up；且曾有句无条件放倒
    src_mo = (ROOT / "backend" / "mesh_optimizer.py").read_text(encoding="utf-8")
    # 2026-09-23 真身发现：4 视角预览依赖 matplotlib，而它只装在 Hermes venv 里，
    # 生产用 miniconda（start-all.ps1 还会清空 PYTHONPATH）→ 用户启动时预览静默失败，
    # 表现为建模完看不到模型。凡是只在某个会话环境里能跑通的依赖，都要用
    # **清空 PYTHONPATH + 生产解释器**验一次。
    import subprocess as _sp
    probe = _sp.run(
        [sys.executable, "-c",
         "import matplotlib, backend.render3d as r; print('ok', matplotlib.__version__)"],
        cwd=str(ROOT), env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True, text=True, timeout=180)
    # 自动继续（宇凯 2026-09-23）：图片就绪后 N 秒无修改需求就自动建模
    src_conv = (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8")
    src_app = (ROOT / "app.py").read_text(encoding="utf-8")
    src_pm = (ROOT / "backend" / "project_manager.py").read_text(encoding="utf-8")
    src_ag = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
    # 耗时估算必须来自实测表（用户报"估算不准"：界面写 STL 20-40 秒，实测中位 2 秒 ✗）
    try:
        from backend import timings as _T
        _stl_med = _T.estimate("stl")[0]
        ck("timings_measured_stl_fast", _stl_med <= 10,
           f"STL 实测中位 {_stl_med:.0f}s（n={_T.samples('stl')}）")
        ck("timings_have_samples",
           all(_T.samples(k) > 0 for k in ("image_512", "hd_1024", "3d_fast", "3d_std", "stl")),
           "每个阶段都要有实测样本数，不能凭空写")
    except Exception as e:
        ck("timings_measured_stl_fast", False, repr(e)[:60])
    ck("ui_has_ring_and_stl_file", "def ring_html" in src_app
       and "keyframes ringrun" in src_app
       and "stl_file = gr.File" in src_app,
       "环形进度条 + STL 下载卡片都要在界面上")
    ck("stl_export_reports_path", "文件：`{stl_path}`" in src_conv,
       "导出后必须给出文件路径，否则用户拿不到东西（点了像没反应）")
    # 启动顺序（宇凯 2026-09-23："对话加载太慢，最好一开就常驻在内存"）：
    # bootstrap 必须先预热对话模型，再做重量级自检 —— 否则启动后马上说话会撞 89s 冷加载
    _bs = src_app.split("def _background_bootstrap", 1)[1].split("def ", 1)[0]
    bootstrap=next(n for n in ast.parse(src_app).body if isinstance(n,ast.FunctionDef) and n.name=='_background_bootstrap')
    ck("bootstrap_keeps_models_lazy",
       not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='warm_up' for n in ast.walk(bootstrap)),
       "按现行需求：界面常驻，模型按需加载，不在启动时抢占显存")
    ck("bootstrap_reports_lazy_policy",'模型按需加载' in _bs,
       "启动日志应说明模型按需加载")
    # 上传参考图入口（宇凯 2026-09-23："我要熊大，但图像只是给了一个卡通小熊"）
    # IP 角色纯文字模型只能猜，必须能"照图" —— 这条守的就是入口存在且接线正确
    ck("reference_upload_entry_exists",
       "ref_up = gr.UploadButton" in src_app
       and "on_reference_uploaded" in src_app
       and "ref_up.upload(fn=on_reference_uploaded" in src_app,
       "上传参考图的组件/处理器/事件三处缺一不可")
    ck("reference_upload_becomes_selected",
       'state["selected"] = str(dst)' in src_app
       and 'state["phase"] = "selecting"' in src_app
       and "pm.save_selected_image" in src_app,
       "上传后要进 selected（建模就取这个字段）+ selecting 阶段 + 落盘")
    # ── Qwen-Image-2.1 参考图链路（宇凯 2026-09-23："我要熊大，但图像只是给了一个卡通小熊"）──
    src_cc2 = (ROOT / "backend" / "comfy_client.py").read_text(encoding="utf-8")
    src_pl2 = (ROOT / "backend" / "pipeline.py").read_text(encoding="utf-8")
    src_pm2 = (ROOT / "backend" / "project_manager.py").read_text(encoding="utf-8")
    ck("qwen21_workflow_uses_own_node",
       "TextEncodeQwenImage21" in src_cc2 and '\"images\": [[img, 0]]' in src_cc2,
       "2.1 必须用专用编码节点；images 要写成 AUTOGROW 的 list-of-pairs（实测唯一通过的形式）")
    ck("reference_image_switches_branch",
       'if params.get("reference_image"):' in src_cc2 and "reference_image=_ref or None" in (
           ROOT / "backend" / "conversation.py").read_text(encoding="utf-8"),
       "有参考图必须切到 2.1 分支（否则 IP 角色又只能靠猜）")
    ck("ref_upload_persisted", '"ref_upload"' in src_pm2,
       "ref_upload 必须落盘（漏了它重启后『照图生成』静默失效）")
    try:
        _cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        _q = _cfg["models"]["image"]["qwen21_ref"]
        ck("qwen21_encoder_is_qwen3vl_8b",
           "qwen3vl_8b" in _q.get("clip_l", "") and "pe_t2i" not in _q.get("clip_l", "")
           and _q.get("clip_type") == "qwen_image",
           "2.1 的文本编码器是 Qwen3-VL-8B；写成 ..._pe_t2i/pe_i2i（提示词增强器）会出纯噪声 ✗")
    except Exception as e:
        ck("qwen21_encoder_is_qwen3vl_8b", False, repr(e)[:60])
    # ── 联网找参考图（2026-09-24 加：用户上传 + agent 搜索都要有）──
    src_wr = (ROOT / "backend" / "web_ref.py").read_text(encoding="utf-8")
    ck("agent_character_search_exists",
       "def find_character_image" in src_wr
       and "CHARACTER_NAME_ALIASES" in src_wr
       and "Briar Bear" in src_wr,
       'agent 搜索：web_ref.find_character_image + 中英名映射表（Bing 对熊大返动物，要查 Briar Bear）')

    # ── 端到端 bug A 修：联网找图必须用户确认才能做参考图（2026-09-24）──
    src_app2 = (ROOT / "app.py").read_text(encoding="utf-8")
    src_pm2 = (ROOT / "backend" / "project_manager.py").read_text(encoding="utf-8")
    ck("web_ref_no_auto_set_ref_upload",
       not _func_auto_sets_ref_upload(src_app2, "on_reference_search"),
       "on_reference_search 不能自动设 ref_upload（ast 检查：注释里不算），否则端到端 bug A 回归")
    ck("adopt_handler_does_set_ref_upload",
       _func_auto_sets_ref_upload(src_app2, "on_adopt_as_reference"),
       "on_adopt_as_reference 必须设 ref_upload（用户点了 → 才标参考图）")
    ck("adopt_button_wired_in_ui",
       'gr.Button("🎯 采用为参考图"' in src_app2
       and "btn_adopt.click(fn=on_adopt_as_reference" in src_app2,
       "🎯 采用按钮 + 接线")
    ck("ref_upload_pending_persisted",
       'ref_upload_pending' in src_pm2,
       "ref_upload_pending 必须落盘（刷新就丢，按钮可见性算不准）")

    # ── UI 响应回归保护（2026-09-25）：同步函数 + outputs=[state] 在 Gradio 5 里被吞掉
    # （导致点按钮完全没反应）。所有 on_btn_* 必须 generator（yield）。 ──
    for fn_name in ("on_btn_new", "on_btn_refine", "on_btn_edit"):
        node = None
        for n in ast.walk(ast.parse(src_app2)):
            if isinstance(n, ast.FunctionDef) and n.name == fn_name:
                node = n; break
        body = ast.unparse(node.body) if node else ""
        is_gen = (node is not None) and bool(getattr(node, "body", None)) and isinstance(node.body[0], (ast.Expr, ast.Yield)) if False else False
        # 简单点：靠 ast.unparse 里有 "yield"
        ck(f"{fn_name}_is_generator", "yield" in body,
           f"{fn_name} 必须 generator（yield 至少一次），否则 Gradio 5 吞掉 inputs/outputs → 按钮不响应")

    ck("heartbeat_dormant_when_idle", "def arm_heartbeat" in src_app
       and 'gr.Timer(value=float(conv.autopilot_cfg()["delay_sec"]), active=False)' in src_app
       and 'gr.Timer(active=bool((state or {}).get("auto_deadline")))' in src_app,
       "心跳必须按需开关 —— 常驻 1 秒一跳会让界面一直显示处理中（宇凯报的'一直在弹'）")
    ck("model_library_routes_wired",
       "def register_model_library" in src_app
       and "register_model_library(_sa)" in src_app
       and "Under construction" not in src_app,
       "模型库不能只留占位 —— backend/model_library.py 要真挂到 FastAPI")
    ck("typing_event_debounced", 'trigger_mode="always_last"' in src_app,
       "输入框每次按键都发事件会一直闪 → 用 always_last 去抖")
    ck("none_folder_name_tolerated",
       "if not folder_name or not str(folder_name).strip()" in src_pm
       and "if not folder_name or not str(folder_name).strip()" in src_conv,
       "下拉框刷新传 None 会 TypeError（app 日志现场抓到）→ 两层都要兜")
    _auto_cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("autopilot", {})
    ck("autopilot_uses_fast_preset",
       'quality_3d"] = _cfg["quality"]' in src_conv
       and 'quality_3d") or "高质量"' in src_conv
       and _auto_cfg.get("quality") == "快速",
       "自动继续走快速档（octree 256）/ 手动点建模默认高质量（512）")
    ck("cold_start_warms_before_planning",
       "def _cold_start_timeout" in src_ag
       and src_ag.count("_cold_start_timeout(timeout)") >= 2,
       "plan 与 plan_stream 都要走冷启动保护（否则启动后第一条消息必超时回退）")
    ck("autopilot_config_present", "autopilot" in json.loads(
        (ROOT / "config.json").read_text(encoding="utf-8")), "config 要有 autopilot 段")
    ck("autopilot_not_a_runaway", "def cancel_autopilot" in src_conv
       and "self.cancel_autopilot(st0, " in src_conv,
       "用户一发言就要取消布防（不能抢跑烧 GPU）")
    ck("autopilot_single_fire", "conv.cancel_autopilot(state, \"用户手动点了生成建模\")" in src_app,
       "用户手动点建模要先撤布防，否则心跳到点会双跑")
    ck("autopilot_typing_cancels", "msg.input(fn=on_typing" in src_app,
       "打字（未提交）也算提要求，要取消")
    ck("autopilot_persisted", "auto_deadline" in src_pm and src_pm.count("auto_deadline") >= 2,
       "布防要写进 meta 并读回（pending_rotate 当初就是这么漏的）")
    ck("autopilot_honours_enabled", "自动继续已关闭" in src_conv,
       "到点也要再查 enabled —— 关掉之后不许再动手")
    ck("production_interpreter_can_render",
       probe.returncode == 0 and "ok" in probe.stdout,
       (probe.stdout.strip() or probe.stderr.strip())[:90] or "探测失败")
    src_r3 = (ROOT / "backend" / "render3d.py").read_text(encoding="utf-8")
    ck("stdlib_only_at_module_level",
       "import matplotlib" not in src_r3.split("def _pyplot", 1)[0],
       "模块顶层禁止 import 重依赖（缺它时整个模块 import 会失败）")
    ck("up_axis_normalized", "def y_up_to_z_up" in src_mo
       and 'mesh = y_up_to_z_up(mesh)' in src_mo,
       "GLB 必须换算成 Z-up（只对 glb/gltf，STL 不重复转）")
    ck("no_unconditional_lay_down", "if lay_down:" in src_mo
       and "LAY_DOWN_FOR_PRINT" in src_mo,
       "原来无条件绕 X 转 90° 把角色模型放倒（切底部还会切到背）")
    ck("stl_regrounded_after_slice", "Slice+Reground" in src_mo,
       "切掉底部 2mm 后要落回 Z=0，否则切片软件里悬空")
    ck("glb_preview_rotated", "y_up_to_z_up" in (ROOT / "backend" / "render3d.py").read_text(encoding="utf-8"),
       "GLB 预览也要转，否则 4 视角里躺着")
    from backend.comfy_client import ComfyUIClient
    graph=ComfyUIClient({})._build_hunyuan3d_workflow({'input_image':'reference.png','texgen':False,'seed':1})['prompt']
    shape=next(n for n in graph.values() if n['class_type']=='[Comfy3D] Hunyuan3D 21 ShapeGen')
    foreground=graph[shape['inputs']['image'][0]]
    ck("hunyuan_foreground_is_required",
       foreground['class_type']=='JoinImageWithAlpha' and graph[foreground['inputs']['alpha'][0]]['class_type']=='InvertMask'
       and graph[graph[foreground['inputs']['alpha'][0]]['inputs']['mask'][0]]['class_type']=='RemoveBackground',
       "必须先接前景遮罩，再建模；分割失败不能静默使用白底")
    ck("gate_detects_background_slab",
       "def _mesh_is_flat_slab" in src_pl and "or flat" in src_pl,
       "只数连通块会被'物体+背景板'骗过")
    ck("3d_quality_gate_wired",
       "def _finalize_3d" in src_pl and src_pl.count("self._finalize_3d(") == 3,
       "3 条成功返回都要过闸（碎块云不许出厂）")
    ck("3d_gate_has_fallback_guard",
       "_fallback_tried" in src_pl and 'model_key="hunyuan21"' in src_pl,
       "兜底到 Hunyuan3D 2.1，并用 _fallback_tried 防两模型互相递归")
    import time
    from backend import conversation as C

    # 前面的检查会真的跑 pipeline 流程，可能留下 autopilot 的 5 秒 Timer 或后台预热线程。
    # 不清干净的话，_WARM_INFLIGHT 会被那些遗留任务占着，这一项就必然红
    # （实测：干净 clone 里就是因此 150/151，开发机上碰巧时序对得上才过）。
    try:
        C._WARM_INFLIGHT.clear()
        for fn in ("cancel_autopilot", "_cancel_autopilot", "cancel_current"):
            f = getattr(C, fn, None)
            if callable(f):
                try: f()
                except Exception: pass
    except Exception:
        pass
    time.sleep(0.2)

    with patch('requests.get', return_value=SimpleNamespace(json=lambda: {'queue_running':[[1]],'queue_pending':[]})), patch('backend.pipeline.Pipeline.generate_images') as generate:
        C._warm_image_model_async('qwen21_ref')
        deadline=time.monotonic()+15          # 冷启动时 Pipeline() 初始化 + 线程调度要几秒，3 秒太紧
        while C._WARM_INFLIGHT.is_set() and time.monotonic()<deadline:
            time.sleep(0.02)
        ok = not C._WARM_INFLIGHT.is_set() and not generate.called
        if not ok:
            print(f"[warmup] inflight={C._WARM_INFLIGHT.is_set()} gen_called={generate.called}",
                  flush=True)
        C._WARM_INFLIGHT.clear()              # 别把状态留给后面的检查
        ck("warmup_deduped", ok, "生成队列忙时不提交预热作业")
    ck("stl_button_needs_model",
       'has_model = bool((state or {}).get("glb_path"))' in src_a,
       "按有没有模型显示，而不是按阶段（改尺寸重导时也要能点到）")
    ck("app_warms_agent_model_not_ollama",
       "agent.is_ready()" in src_a and "agent.warm_up()" in src_a,
       "agent 分支预热的是 agent 实际要用的模型（provider=tuned 时预热 Ollama 会白占"
       "12.9GB 把 14B 挤出 20GB 显存 —— 实测被换到系统内存）")
    ck("app_imports_agent_module", "from backend import agent" in src_a,
       "on_submit 用了 agent.is_ready/warm_up，不导入会 NameError 点发送即崩")

    # ── 流式输出（用户要求：现在的输出也搞成流式）──
    ck("agent_streaming_implemented",
       "def plan_stream" in src_ag and "def partial_reply" in src_ag)
    ck("partial_reply_functional",
       AG.partial_reply('{"understanding":["x"],"reply":"你好') == "你好"
       and AG.partial_reply('{"reply":"好了。","actions":[]}') == "好了。"
       and AG.partial_reply("没有 reply") == "")
    ck("stream_plan_prompt_shared",
       "def _build_plan_prompt" in src_ag and "_finalize_plan" in src_ag,
       "非流式与流式必须共用同一套 prompt/校验，避免两条路行为不一致")
    ck("stream_carries_thinking_text", 'status["_thinking_text"]' in src_d)
    ck("stream_passes_num_ctx",
       src_d.count("p.get(\"num_ctx\")") >= 2, "流式与非流式都要传 num_ctx")
    src_c2 = (ROOT / "backend" / "conversation.py").read_text(encoding="utf-8")
    ck("conversation_stream_entry", "def handle_agent_turn_stream" in src_c2)
    ck("app_consumes_stream",
       "conv.handle_agent_turn_stream(user_input)" in src_a and "正在推理" in src_a)
    ck("app_throttles_stream", "if now - last_emit < 0.25" in src_a)

    # ── 微调线（照 v7 的方式：Unsloth LoRA + Flask/PEFT 服务）──
    # urllib 漏导入会被 except 静默吞成 NameError → 函数永远返回 False（is_ready 就这么中招过）
    head = src_ag.split("\ndef ", 1)[0]
    ck("agent_urllib_imported_at_module_level",
       "import urllib.request" in head,
       "局部漏 import 时被 except 吞掉，表现是'永远未就绪'——很难查")
    ck("agent_is_ready_logs_failure",
       "探活微调服务失败" in src_ag, "探活失败不能静默")
    ck("agent_provider_switch",
       "def _provider" in src_ag and "def _call_model" in src_ag and "def _stream_model" in src_ag
       and "def unload_tuned" in src_ag)
    ck("plan_uses_provider", "_call_model(ask, timeout=timeout)" in src_ag
       and "_stream_model(prompt, timeout=timeout, status=status)" in src_ag)
    ck("compact_prompt_available",
       "def _build_compact_prompt" in src_ag and "_COMPACT_SYS" in src_ag
       and "def prompt_style" in src_ag)
    ck("tuned_unload_before_generation", "agent.unload_tuned()" in src_c2,
       "生成前让出显存，否则 14B 与 ComfyUI 抢 20GB")
    FT = ROOT / "finetune"
    # 判存在要看具体脚本，不能只看目录：app 初始化时会 mkdir finetune/（放训练锁与 venv），
    # 空目录不代表微调线真的在。
    if (FT / "serve_agent.py").exists():
        ck("finetune_scripts_present",
           all((FT / f).exists() for f in
               ("gen_agent_dataset.py", "train_v8.py", "serve_agent.py", "verify_v8.py")),
           "造数据 / 训练 / 推理服务 三个脚本")
        ck("serve_agent_has_stream_and_unload",
           "/api/stream" in (FT / "serve_agent.py").read_text(encoding="utf-8")
           and "/unload" in (FT / "serve_agent.py").read_text(encoding="utf-8"))
        src_train = (FT / "train_v8.py").read_text(encoding="utf-8")
        ck("train_v8_seq_len_adequate",
           'DSH_TRAIN_MAXSEQ' in src_train and '"2048"' in src_train,
           "v7 的 1024 会截断我们的 JSON 样本；默认 2048 且可用环境变量下调（显存紧张时）")
        ck("training_lock_skips_warmup",
           "def _gpu_busy_lock" in src_a and "TRAINING.lock" in src_a
           and "跳过预热" in src_a and "TRAINING.lock" in src_train,
           "训练期间任何 import app 的脚本都不该把 12GB 对话模型拉回显存"
           "（实测把训练拖慢 3 倍：步时 14s→53s）")
        ck("describer_guards_respect_lock",
           "def _training_lock_active" in src_d and src_d.count("_training_lock_active()") >= 4,
           "咽喉点守卫：预热 / 非流式 / 流式 三处都要认锁（否则任何一次真实调用都会拉回模型）")

        # ── gpt-oss 那条线（v9）：硬约束是"不能再量化一次"+"必须用 harmony 模板" ──
        v9 = (FT / "train_gptoss_v9.py")
        ck("train_v9_present", v9.exists())
        if v9.exists():
            src_v9 = v9.read_text(encoding="utf-8")
            ck("train_v9_uses_fastmodel", "from unsloth import FastModel" in src_v9,
               "gpt-oss 权重原生 MXFP4，FastLanguageModel 不支持")
            ck("train_v9_no_double_quant",
               "load_in_4bit=False" in src_v9 and "dtype=None" in src_v9,
               "已经是 4-bit 了再量化一次既无意义也伤精度；转 bf16 又会翻倍到 40GB")
            ck("train_v9_harmony_template", "apply_chat_template" in src_v9
               and "add_generation_prompt=False" in src_v9)
        src_srv = (FT / "serve_agent.py").read_text(encoding="utf-8")
        ck("serve_agent_dual_kind",
           'DSH_AGENT_KIND' in src_srv and "FastModel" in src_srv and "FastLanguageModel" in src_srv,
           "同一服务要能起 Qwen(dense,4bit) 与 gpt-oss(MXFP4,harmony) 两种")
        ck("serve_agent_strips_reasoning_channel",
           "_ANALYSIS" in src_srv and "def _clean" in src_srv,
           "gpt-oss 的 analysis 频道内容不该混进最终答复")
        ck("serve_agent_no_reasoning_effort_kwarg",
           "reasoning_effort=os.environ" not in src_srv,
           "模板里别再传 reasoning_effort —— 它会往 system 插推理档位说明，"
           "训练数据里没有这段，格式不一致就答非所问")
        ck("serve_maxnew_default_is_kind_aware",
           '_DEFAULT_NEW = "2048" if KIND != "gptoss" else "700"' in src_srv,
           "v8 的 JSON 要 ~1.5k 预算；gpt-oss 给大会空转到十几分钟（实测 312s）")
        # 微调服务的「接入」：启动检查里自动拉起（以前靠人工起，双击启动的 app 不知道有这回事）
        src_su = (ROOT / "backend" / "startup.py").read_text(encoding="utf-8")
        ck("startup_wires_tuned_service",
           "def _check_tuned_agent" in src_su and "self._check_tuned_agent()" in src_su
           and "TUNED_VENV_PYTHON" in src_su and "serve_agent.py" in src_su,
           "provider=tuned 但服务不在时会静默回退关键词路径（观感=变笨），必须自动拉起")
        ck("tuned_autostart_clears_pythonpath",
           'env.pop("PYTHONPATH", None)' in src_su,
           "不清 PYTHONPATH，venv 的 python 会加载 Hermes venv 的 torch → DLL 失败")
        ck("bootstrap_logs_check_summary",
           "checker.log_summary()" in src_a and "def log_summary" in src_su,
           "自动拉起失败必须进日志，否则用户只感到'agent 变笨了'，看不到真因")
        ck("tuned_autostart_gated_by_provider",
           '!= "tuned"' in src_su, "provider=ollama 时不该去起微调服务")
        src_en = (FT / "enable_tuned.py").read_text(encoding="utf-8")
        ck("enable_tuned_supports_gptoss",
           "ON_GPT" in src_en and ":8083" in src_en and 'arg in ("on", "gpt")' in src_en)
    else:
        print("SKIP finetune_*  [本仓库不含 finetune/ 微调线，相关断言跳过]\n")


def check_canonical_suite() -> None:
    log_path = ROOT / 'logs' / 'verification-canonical.log'
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open('w', encoding='utf-8') as log:
        res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                             cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True, timeout=300)
    tail = log_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()
    ck("canonical_suite_passes", res.returncode == 0 and any(line.startswith('Ran ') for line in tail),
       " | ".join(tail[-3:]) or f'无测试输出，exit={res.returncode}，日志={log_path}')


def _block_local_llm() -> None:
    """验证必须离线可复现：拦掉对本机 LLM 的调用。

    不加这道闸的代价（实测）：某处真发一次请求就会把 12.9GB 的 gpt-oss 拉回显存
    （keep_alive=-1 常驻），于是 provider=tuned 时 11GB + 12.9GB 一起挤爆 20GB；
    同时检查也变成"依赖当时环境"而不是可复现。
    """
    import urllib.request as ur
    real = ur.urlopen

    def guard(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if ":11434" in url or ":8082" in url or ":8081" in url:
            raise OSError(f"verify 期间禁止真调本机 LLM（会加载模型抢显存）: {url}")
        return real(req, *a, **k)

    ur.urlopen = guard


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过内联 unittest")
    args = ap.parse_args()
    _block_local_llm()

    tmp = Path(tempfile.mkdtemp(prefix="dsh-verify-"))
    try:
        check_app_source()
        check_viewer_behavior(tmp)
        check_pipeline_stages(tmp)
        check_launcher()
        check_prompt_and_preview()
        check_agent_layer()
        if not args.fast:
            check_canonical_suite()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(OK)}/{len(OK)} passed")
    return 0 if all(OK) else 1


if __name__ == "__main__":
    sys.exit(main())


# === 端到端 bug A 守门：ast 扫描确认联网找图不自动设 ref_upload（不算注释）===
def _func_auto_sets_ref_upload(src_app: str, fn_name: str) -> bool:
    tree = _ast.parse(src_app)
    for n in _ast.walk(tree):
        if isinstance(n, _ast.FunctionDef) and n.name == fn_name:
            for sub in _ast.walk(n):
                if isinstance(sub, _ast.Assign):
                    for t in sub.targets:
                        if (isinstance(t, _ast.Subscript) and
                            isinstance(t.value, _ast.Name) and t.value.id == "state" and
                            isinstance(t.slice, _ast.Constant) and t.slice.value == "ref_upload"):
                            return True
            return False
    return False
    sys.exit(main())
