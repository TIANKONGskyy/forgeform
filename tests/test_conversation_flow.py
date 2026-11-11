"""回归测试：对话状态机 / 意图分类 / 参数落地。

覆盖 2026-09-22 实测抓到的真实缺陷（每条都对应一次线上现象）：
  · 「图片有问题」后说「颜色换成红色」却完全没改图  → 修改必须落到 auto_params + 生成 prompt
  · 「换成红色」被判成新需求，根本进不了改图路径      → 分类器 REFINE 关键字
  · 「用金属质感」被误判成颜色「金色」                → 颜色提取的负向先行断言
  · prompt 里同时出现「蓝色…红色的配色」导致改色含糊  → base_prompt 旧值替换
  · 纯抱怨白烧一次生图                              → 先问清要改什么

全部为纯逻辑 + 桩（不加载 GPU、不调 LLM），秒级可跑：
    python -m unittest discover -s tests -t . -v
"""
import ast
import inspect
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.conversation as C
import backend.describer as de
import backend.intent_classifier as ic
import backend.project_manager as pm

# 本套件验证的是**确定性路径**（关键词兜底/状态机/参数落地），所以默认关掉 agent ——
# 否则每个用例都会真调一次 LLM（实测整套从 5s 变 139s）。TestAgentLayer 会自行打开。
C._AGENT_CFG_CACHE = False

# 套件必须离线可复现：拦掉对本机 LLM 的调用。不加这道闸的代价（实测）：某处真发一次请求
# 就会把 12.9GB 的 gpt-oss 拉回显存并常驻，provider=tuned 时与 11GB 的微调模型一起挤爆 20GB。
import urllib.request as _ur

_real_urlopen = _ur.urlopen


def _no_local_llm(req, *a, **k):
    url = getattr(req, "full_url", str(req))
    if ":11434" in url or ":8082" in url or ":8081" in url:
        raise OSError(f"测试禁止真调本机 LLM（会加载模型抢显存）: {url}")
    return _real_urlopen(req, *a, **k)


_ur.urlopen = _no_local_llm

BASE = "我要一个蓝色手机支架，尺寸 90mm"


def fresh(phase="selecting", **over):
    """一个处于「已出图」状态的项目。"""
    st = {
        "folder": "t", "path": str(ROOT / "projects"),
        "phase": phase, "base_prompt": BASE, "refinements": [],
        "user_prompt": BASE, "understood": BASE,
        "auto_params": {"style": "写实", "size_mm": 90.0, "color": "蓝色",
                        "model": "z_image", "resolution": "1024×1024", "steps": "8步"},
        "conversation": [], "candidates": [], "selected": "", "selected_idx": -1,
        "glb_path": "", "stl_path": "", "stl_size": 90,
    }
    st.update(over)
    return st


class TestApplyRefinement(unittest.TestCase):
    """参数必须真正落到 state 与生成 prompt（原 bug：只在 user_prompt 追加一句无用文字）"""

    def test_color_lands_and_replaces_old_in_base_prompt(self):
        st = fresh()
        changes = C._apply_refinement(st, "颜色换成红色")
        self.assertEqual(st["auto_params"]["color"], "红色")
        # 旧色必须从 base_prompt 消失，否则 prompt 里「蓝色…红色的配色」互相打架
        self.assertNotIn("蓝色", st["base_prompt"])
        self.assertIn("红色", st["user_prompt"])
        self.assertTrue(any("颜色" in c for c in changes))

    def test_absolute_size(self):
        st = fresh()
        C._apply_refinement(st, "尺寸改成 80mm")
        self.assertEqual(st["auto_params"]["size_mm"], 80.0)
        self.assertIn("80mm", st["base_prompt"])
        self.assertEqual(st["stl_size"], 80.0)

    def test_relative_size(self):
        st = fresh()
        C._apply_refinement(st, "大一点")
        self.assertAlmostEqual(st["auto_params"]["size_mm"], 126.0)
        st = fresh()
        C._apply_refinement(st, "小一点")
        self.assertAlmostEqual(st["auto_params"]["size_mm"], 63.0)

    def test_explicit_style(self):
        st = fresh()
        C._apply_refinement(st, "改成卡通风格")
        self.assertEqual(st["auto_params"]["style"], "卡通")

    def test_metal_is_not_a_color(self):
        st = fresh()
        C._apply_refinement(st, "表面用金属质感")
        self.assertEqual(st["auto_params"]["color"], "蓝色")

    def test_pure_complaint_does_not_pollute_prompt(self):
        st = fresh()
        C._apply_refinement(st, "图片有问题")
        self.assertEqual(st["refinements"], [])

    def test_actionable_text_enters_prompt(self):
        st = fresh()
        C._apply_refinement(st, "表面加防滑纹")
        self.assertIn("防滑纹", C._build_prompt(st))


class TestClassifySelecting(unittest.TestCase):
    """选图阶段的意图分流（原 bug：「换成」不在关键字表里 → 被判成新需求）"""

    def test_change_verbs_are_refine(self):
        for text in ["颜色换成红色", "颜色变成绿色", "换个颜色", "改成蓝色",
                     "图片有问题", "不好看", "小一点"]:
            with self.subTest(text=text):
                self.assertIs(ic.classify(text, "selecting")[0], ic.Intent.REFINE)

    def test_new_description_is_not_refine(self):
        for text in ["我要一个熊大", "绘制一个茶杯垫", "做个手机支架", "我想打印一只小猫"]:
            with self.subTest(text=text):
                self.assertIs(ic.classify(text, "selecting")[0], ic.Intent.DESCRIBE_OBJECT)

    def test_explicit_confirm_starts_3d(self):
        for text in ["开始建模", "生成建模", "生成3D", "可以了", "确认"]:
            with self.subTest(text=text):
                self.assertIs(ic.classify(text, "selecting")[0], ic.Intent.START_3D)

    def test_serial_selection_keeps_context(self):
        for text, want in [("选第二张", "1"), ("第2张", "1"), ("2", "1")]:
            with self.subTest(text=text):
                intent, arg = ic.classify(text, "selecting")
                self.assertIs(intent, ic.Intent.SELECT_IMAGE)
                self.assertEqual(arg, want)


class _Stubbed:
    """构造一个 ConversationManager：桩掉生图/落盘，只观察行为。"""

    def __init__(self, phase):
        self.m = C.ConversationManager.__new__(C.ConversationManager)
        self.m.current_project = fresh(phase=phase)
        self.generated, self.appends = [], []
        self.m._append_conversation = lambda st, role, c, reply="": self.appends.append((role, c, reply))
        self.m._do_generate_images = lambda st: (self.generated.append(dict(st["auto_params"])),
                                                {"project": st, "reply": "GEN", "images": []})[1]
        self.m._do_new_project = lambda: {"project": self.m.current_project, "reply": "NEW", "images": []}
        self.m._do_generate_3d = lambda st: {"project": st, "reply": "3D", "images": []}

    def say(self, text):
        return self.m.handle_user_input(text)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self._saved = pm.save_project
        pm.save_project = lambda st: None
        de.generate_confirm_response = lambda **k: "【桩】"

    def tearDown(self):
        pm.save_project = self._saved

    def test_editing_applies_change_before_generating(self):
        """原 bug 现场：editing 阶段说改颜色 → 生图必须拿到新参数"""
        s = _Stubbed("editing")
        s.say("颜色换成红色，大一点")
        self.assertEqual(len(s.generated), 1)
        self.assertEqual(s.generated[0]["color"], "红色")
        self.assertAlmostEqual(s.generated[0]["size_mm"], 126.0)

    def test_editing_reports_applied_changes(self):
        s = _Stubbed("editing")
        s.say("颜色换成红色")
        self.assertTrue(any("已实际应用" in a[2] for a in s.appends))

    def test_editing_pure_complaint_asks_instead_of_burning_generation(self):
        s = _Stubbed("editing")
        s.say("图片有问题")
        self.assertEqual(s.generated, [])
        self.assertTrue(any("说得具体" in a[2] for a in s.appends))

    def test_selecting_actionable_generates(self):
        s = _Stubbed("selecting")
        s.say("颜色换成红色")
        self.assertEqual(len(s.generated), 1)
        self.assertEqual(s.generated[0]["color"], "红色")

    def test_selecting_complaint_enters_editing(self):
        s = _Stubbed("selecting")
        r = s.say("不好看")
        self.assertEqual(s.generated, [])
        self.assertEqual(r["project"]["phase"], "editing")

    def test_refining_updates_params(self):
        """refining 阶段也走「累积 + 总结」：参数更新，回复由 LLM 总结给出"""
        s = _Stubbed("refining")
        r = s.say("颜色改成绿色")
        self.assertEqual(s.m.current_project["auto_params"]["color"], "绿色")
        self.assertEqual(s.m.current_project["phase"], "confirm")
        self.assertEqual(r["reply"], "【桩】")          # 说明走的是 LLM 总结路径


class TestRequirementChat(unittest.TestCase):
    """聊天式定需求：一组对话持续累积上下文，每轮给总结（原实现每轮重置 = 忘掉前面的话）"""

    def setUp(self):
        self._saved = pm.save_project
        pm.save_project = lambda st: None
        self._saved_gen = de.generate_confirm_response
        self.seen = []

        def fake(**k):
            self.seen.append(k.get("auto_params", {}) or {})
            return "【总结】"
        de.generate_confirm_response = fake
        self.m = C.ConversationManager.__new__(C.ConversationManager)
        self.m.current_project = fresh(phase="empty", base_prompt="", user_prompt="",
                                       refinements=[], auto_params={}, stl_size=120)
        self.m._append_conversation = lambda *a, **k: None

    def tearDown(self):
        pm.save_project = self._saved
        de.generate_confirm_response = self._saved_gen

    def say(self, text):
        return self.m.handle_user_input(text)

    def test_multi_turn_accumulates_instead_of_resetting(self):
        self.say("我要一个茶杯垫")
        first_base = self.m.current_project["base_prompt"]
        self.assertTrue(first_base)

        self.say("直径改成 90mm")
        self.say("颜色要绿色")
        st = self.m.current_project

        # ① 首轮需求不能被覆盖
        self.assertEqual(st["base_prompt"], first_base)
        # ② 后续轮次要累积进 refinements（会拼进生成 prompt）
        self.assertIn("直径改成 90mm", st["refinements"])
        self.assertIn("颜色要绿色", st["refinements"])
        # ③ 参数按增量更新
        self.assertEqual(st["auto_params"]["size_mm"], 90.0)
        self.assertEqual(st["auto_params"]["color"], "绿色")
        # ④ 多轮上下文完整保留
        self.assertEqual(len(st["req_notes"]), 3)
        # ⑤ 每轮都在 confirm 阶段（两个按钮：继续细化 / 生成图片 都在）
        self.assertEqual(st["phase"], "confirm")

    def test_relative_size_after_absolute_accumulates(self):
        self.say("我要一个茶杯垫")
        self.say("尺寸改成 80mm")
        self.say("大一点")
        self.assertAlmostEqual(self.m.current_project["auto_params"]["size_mm"], 112.0)

    def test_build_prompt_contains_all_turns(self):
        self.say("我要一个茶杯垫")
        self.say("直径改成 90mm")
        self.say("颜色要绿色")
        prompt = C._build_prompt(self.m.current_project)
        for frag in ["茶杯垫", "90mm", "绿色"]:
            with self.subTest(frag=frag):
                self.assertIn(frag, prompt)

    def test_summary_receives_all_notes(self):
        self.say("我要一个茶杯垫")
        self.say("直径改成 90mm")
        self.say("颜色要绿色")
        notes = self.seen[-1].get("notes") or []
        self.assertEqual(len(notes), 3)
        self.assertIn("直径改成 90mm", notes)

    def test_summary_prompt_includes_history_and_asks(self):
        p = de._summary_prompt(["我要一个茶杯垫", "颜色要绿色"],
                               {"style": "写实", "size_mm": 90, "color": "绿色",
                                "model": "z_image", "requirement": "茶杯垫，颜色要绿色"},
                               "颜色要绿色")
        self.assertIn("我要一个茶杯垫", p)
        self.assertIn("颜色要绿色", p)
        self.assertIn("我目前理解的需求是", p)
        self.assertIn("生成图片", p)          # 告诉用户可以去生成

    def test_followup_detection_does_not_depend_on_phase(self):
        """真 bug 回归：判据若依赖 phase，流式结束后才写 phase 时第二轮需求会被重置"""
        self.assertTrue(C._is_requirement_followup(
            fresh(phase="empty", base_prompt="一个茶杯垫", req_notes=["一个茶杯垫"])))
        self.assertFalse(C._is_requirement_followup(
            fresh(phase="confirm", base_prompt="")))

    def test_two_prepare_calls_accumulate_without_finish_reply(self):
        """不带 finish_reply 连调两次 prepare_describe，也必须累积（不能当首轮重置）"""
        self.m.current_project = fresh(phase="empty", base_prompt="", user_prompt="",
                                       refinements=[], req_notes=[], auto_params={})
        self.m.prepare_describe("我要一个茶杯垫")
        _, _, p2 = self.m.prepare_describe("尺寸改成 80mm")
        self.assertTrue(p2["changes"], "第二轮必须被识别为补充而非首轮")
        self.assertEqual(len(p2["notes"]), 2)
        self.assertEqual(self.m.current_project["auto_params"]["size_mm"], 80.0)

    def test_req_notes_survive_reload(self):
        tmp = Path(tempfile.mkdtemp(prefix="dsh-notes-"))
        saved_dir = pm.PROJECTS_DIR
        # 本测试类 setUp 把 save_project 换成了 no-op，这里必须先还原才能验证真正的落盘
        pm.save_project = self._saved
        try:
            pm.PROJECTS_DIR = tmp
            st = pm.create_project()
            st["req_notes"] = ["我要一个茶杯垫", "颜色要绿色"]
            st["base_prompt"] = "茶杯垫"
            st["auto_params"] = {"color": "绿色", "size_mm": 90}
            pm.save_project(st)
            back = pm.load_project(st["folder"])
            self.assertEqual(back["req_notes"], ["我要一个茶杯垫", "颜色要绿色"])
            self.assertEqual(back["base_prompt"], "茶杯垫")
            self.assertEqual(back["auto_params"]["color"], "绿色")
        finally:
            pm.PROJECTS_DIR = saved_dir
            shutil.rmtree(tmp, ignore_errors=True)


class TestTwoStageGeneration(unittest.TestCase):
    """两阶段优化（移植自旧版）：512 快速预览 → 3D 前 img2img 高清重绘"""

    def setUp(self):
        self.enterContext(patch('backend.conversation._rewarm_tuned_async'))
        self.enterContext(patch('backend.conversation._warm_image_model_async'))
        self._saved = pm.save_project
        pm.save_project = lambda st: None
        import backend.pipeline as pl
        self.pl, self.calls = pl, []
        self._pl_orig = (pl.Pipeline.generate_images, pl.Pipeline.enhance_image,
                         pl.Pipeline.generate_3d, pl.Pipeline.export_stl)
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-2stage-"))
        (self.tmp / "candidates").mkdir(exist_ok=True)
        (self.tmp / "t").mkdir(exist_ok=True)     # state["folder"]="t"
        self._savedir = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = self.tmp                # 隔离：archive_project 不能碰真实 projects/

    def tearDown(self):
        pm.save_project = self._saved
        pm.PROJECTS_DIR = self._savedir
        (self.pl.Pipeline.generate_images, self.pl.Pipeline.enhance_image,
         self.pl.Pipeline.generate_3d, self.pl.Pipeline.export_stl) = self._pl_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub(self, hd_file=None, gen_file=None):
        calls = self.calls          # 闭包捕获测试实例；方法首位参数是 Pipeline 实例
        gen_file = gen_file or (self.tmp / "gen.png")
        hd_file = hd_file or (self.tmp / "hd.png")
        glb_file = self.tmp / "m.glb"
        stl_file = self.tmp / "m.stl"

        class R:
            success = True
            def __init__(self, p): self.output_path = p

        def fake_gen(_pipe, prompt, count=1, model_key="flux", quality_name="标准",
                     resolution="1024×1024", seed=-1, negative="", progress_cb=None,
                     skip_cache=False, steps_override=0, reference_image=None):
            calls.append(dict(kind="gen", prompt=prompt, resolution=resolution,
                              quality_name=quality_name, skip_cache=skip_cache,
                              model_key=model_key))
            Path(gen_file).write_bytes(b"fake-image")
            return [R(str(gen_file))]

        def fake_hd(_pipe, image_path, prompt, **kw):
            calls.append(dict(kind="hd", image_path=image_path, prompt=prompt, **kw))
            Path(hd_file).write_bytes(b"fake-hd")
            return str(hd_file)

        def fake_g3(_pipe, **kw):
            calls.append(dict(kind="g3", **kw))
            Path(glb_file).write_bytes(b"fake-glb")
            return R(str(glb_file))

        def fake_es(_pipe, glb, **kw):
            calls.append(dict(kind="stl", **kw))
            Path(stl_file).write_bytes(b"fake-stl")
            return R(str(stl_file))

        # 四个都要桩：漏桩 = 真跑（曾因此让套件从 0.2s 变 110s 并真的提交 ComfyUI 任务）
        self.pl.Pipeline.generate_images = fake_gen
        self.pl.Pipeline.enhance_image = fake_hd
        self.pl.Pipeline.generate_3d = fake_g3
        self.pl.Pipeline.export_stl = fake_es

    def _mgr(self, **over):
        m = C.ConversationManager.__new__(C.ConversationManager)
        st = fresh(phase="confirm", base_prompt="一个茶杯垫",
                   auto_params={"style": "写实", "size_mm": 90,
                                "color": "绿色", "model": "z_image"})
        st["path"] = str(self.tmp)
        st.update(over)
        m.current_project = st
        m._append_conversation = lambda *a, **k: None
        return m

    def test_generation_uses_fast_preview_and_skips_cache(self):
        self._stub(gen_file=self.tmp / "gen.png")
        m = self._mgr()
        m._do_generate_images(m.current_project)
        g = [c for c in self.calls if c["kind"] == "gen"][0]
        self.assertEqual(g["resolution"], C._PREVIEW_RES, "预览必须走 512 快速档")
        self.assertEqual(g["quality_name"], "快速")
        self.assertTrue(g["skip_cache"], "必须跳缓存，否则改了参数也可能拿到旧图")
        self.assertEqual(m.current_project["img_stage"], "preview")
        self.assertTrue(m.current_project.get("gen_prompt"), "要记下 prompt 供高清重绘复用")

    def test_enhance_promotes_preview_to_hd(self):
        preview = self.tmp / "preview.png"
        preview.write_bytes(b"p")
        hd = self.tmp / "hd.png"
        hd.write_bytes(b"h")
        m = self._mgr(selected=str(preview), candidates=[str(preview)],
                      img_stage="preview", gen_prompt="一个茶杯垫")
        self._stub(hd_file=str(hd))
        out = m._enhance_selected(m.current_project)
        self.assertNotEqual(out, str(preview), "应返回高清图")
        self.assertEqual(m.current_project["img_stage"], "hd")
        self.assertEqual(len(m.current_project["candidates"]), 2)
        e = [c for c in self.calls if c["kind"] == "hd"][0]
        self.assertEqual(e["resolution"], C._HD_RES, "高清重绘必须走 1024 档")
        self.assertEqual(e["prompt"], "一个茶杯垫", "要复用原 prompt 以保持构图")

    def test_enhance_skipped_when_already_hd(self):
        f = self.tmp / "x.png"
        f.write_bytes(b"x")
        m = self._mgr(selected=str(f), candidates=[str(f)], img_stage="hd")
        self._stub()
        self.assertEqual(m._enhance_selected(m.current_project), str(f))
        self.assertEqual([c for c in self.calls if c["kind"] == "hd"], [])

    def test_resolution_keys_are_valid(self):
        """守卫：这两个常量必须是 get_resolutions() 的真实 key，否则静默回退 1024"""
        from backend.pipeline import Pipeline
        keys = set(Pipeline().get_resolutions().keys())
        for k in (C._PREVIEW_RES, C._HD_RES):
            with self.subTest(key=k):
                self.assertIn(k, keys)

    def test_3d_success_returns_glb_path(self):
        """3D 成功后 state 已切到新对话组 → 返回值必须带上 glb，否则查看器没东西可显示"""
        preview = self.tmp / "p2.png"; preview.write_bytes(b"p")
        m = self._mgr(selected=str(preview), candidates=[str(preview)],
                      img_stage="hd")            # 已是 hd → 不触发重绘
        self._stub(hd_file=None)
        r = m._do_generate_3d(m.current_project)
        self.assertIn("glb", r, "返回值必须带 glb（供 3D 查看器展示）")
        self.assertTrue(r["glb"], f"glb 不能为空：{r.get('glb')!r}")


class TestViewerWiring(unittest.TestCase):
    """3D 查看器接线（旧版 gr.Model3D 在 v3 漏移植 → 建模完看不到模型）

    app.py 导入即会实例化 ConversationManager 并可能写真实 projects/，
    故这里只做源码结构断言（不导入），行为验证放在 ad-hoc 脚本里做。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_model3d_component_exists(self):
        self.assertIn("gr.Model3D(", self.src, "必须有 3D 查看器组件")

    def test_viewer_in_ui_outputs(self):
        outs = self._assign("ui_outputs").value
        names = [t.id for t in outs.elts if isinstance(t, ast.Name)]
        self.assertIn("viewer", names)
        i = names.index("viewer")
        self.assertIn("hero", names)
        self.assertEqual(i, names.index("hero") + 1, "viewer 紧跟 hero（顺序须与 _update_ui 一致）")

    def test_update_ui_arity_matches_ui_outputs(self):
        fn = self._fn("_update_ui")
        cnt = lambda n: len(n.elts) if isinstance(n, (ast.Tuple, ast.List)) else 1
        n_upd = cnt(next(n for n in ast.walk(fn) if isinstance(n, ast.Return)).value)
        n_out = cnt(self._assign("ui_outputs").value)
        self.assertEqual(n_upd, n_out, f"_update_ui 返回 {n_upd} 个，ui_outputs 要 {n_out} 个")

    def test_update_ui_accepts_glb_override(self):
        for name in ("_update_ui", "_update_ui_with_note"):
            fn = self._fn(name)
            args = [a.arg for a in fn.args.args]
            self.assertIn("glb_override", args, f"{name} 必须支持 glb_override")

    def test_3d_entrypoints_pass_glb_override(self):
        """建模完成后 state 已切到新对话组，不带 override 就看不到刚建的模型"""
        for name in ("on_btn_model", "on_submit"):
            body = ast.unparse(self._fn(name)).replace('"', "'")
            self.assertIn("result.get('glb')", body, f"{name} 必须把 result['glb'] 传给 _update_ui")

    def _fn(self, name):
        return next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def _assign(self, name):
        return next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == name for t in n.targets))


class TestPromptEnhanceAndPreview(unittest.TestCase):
    """生图提示词扩写+英译 / 3D 预览图（2026-09-22 两项修复）"""

    def setUp(self):
        self.enterContext(patch('backend.conversation._rewarm_tuned_async'))
        self.enterContext(patch('backend.conversation._warm_image_model_async'))
        self._saved = pm.save_project
        pm.save_project = lambda st: None
        import backend.pipeline as pl
        import backend.describer as de
        import backend.render3d as r3
        self.pl, self.de, self.r3 = pl, de, r3
        self._orig = (pl.Pipeline.generate_images, pl.Pipeline.enhance_image,
                      pl.Pipeline.generate_3d, pl.Pipeline.export_stl,
                      de.enhance_image_prompt, r3.render_mesh_preview)
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-prompt-"))
        (self.tmp / "candidates").mkdir(exist_ok=True)
        (self.tmp / "t").mkdir(exist_ok=True)
        self._savedir = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = self.tmp
        self.calls = []

    def tearDown(self):
        pm.save_project = self._saved
        pm.PROJECTS_DIR = self._savedir
        (self.pl.Pipeline.generate_images, self.pl.Pipeline.enhance_image,
         self.pl.Pipeline.generate_3d, self.pl.Pipeline.export_stl,
         self.de.enhance_image_prompt, self.r3.render_mesh_preview) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub(self, en: str, preview=None):
        calls = self.calls
        glb, stl = self.tmp / "m.glb", self.tmp / "m.stl"
        hd = self.tmp / "hd.png"

        class R:
            success = True
            def __init__(self, p): self.output_path = p

        def gen(_p, prompt, count=1, **kw):
            calls.append(("gen", prompt))
            (self.tmp / "gen.png").write_bytes(b"i")
            return [R(str(self.tmp / "gen.png"))]

        def hd_(_p, image_path, prompt, **kw):
            calls.append(("hd", prompt))
            hd.write_bytes(b"h")
            return str(hd)

        def g3(_p, **kw):
            calls.append(("g3", kw.get("image_path")))
            glb.write_bytes(b"g")
            return R(str(glb))

        def es(_p, glb_, **kw):
            stl.write_bytes(b"s")
            return R(str(stl))

        self.pl.Pipeline.generate_images, self.pl.Pipeline.enhance_image = gen, hd_
        self.pl.Pipeline.generate_3d, self.pl.Pipeline.export_stl = g3, es
        self.de.enhance_image_prompt = lambda zh, **kw: en
        self.r3.render_mesh_preview = lambda p, **kw: (calls.append(("preview", p)) or
                                                       (str(preview) if preview else ""))

    def _mgr(self):
        m = C.ConversationManager.__new__(C.ConversationManager)
        m.current_project = fresh(phase="confirm", base_prompt="我要一个手机支架",
                                  auto_params={"style": "写实", "size_mm": 90,
                                               "color": "蓝色", "model": "z_image"})
        m.current_project["path"] = str(self.tmp)
        m._append_conversation = lambda *a, **k: None
        return m

    def test_english_prompt_is_used_for_generation(self):
        m = self._mgr()
        self._stub(en="a matte blue phone stand, studio lighting, 8k, sharp focus, "
                      "seamless white background, ultra detailed product shot")
        m._do_generate_images(m.current_project)
        gen_prompt = [c[1] for c in self.calls if c[0] == "gen"][0]
        self.assertTrue(gen_prompt.startswith("a matte blue phone stand"))
        self.assertIn("手机支架", m.current_project["gen_prompt_zh"],
                      "中文原句要留存（展示/回溯用）")

    def test_falls_back_to_chinese_when_enhancer_fails(self):
        m = self._mgr()
        self._stub(en="")                      # 扩写失败
        m._do_generate_images(m.current_project)
        gen_prompt = [c[1] for c in self.calls if c[0] == "gen"][0]
        self.assertIn("手机支架", gen_prompt, "扩写失败必须回退中文原句，不能空跑")

    def test_hd_rerender_reuses_english_prompt(self):
        m = self._mgr()
        self._stub(en="a matte blue phone stand, studio lighting, 8k, sharp focus, "
                      "seamless white background, ultra detailed product shot")
        m._do_generate_images(m.current_project)
        m._do_generate_3d(m.current_project)
        hd_prompt = [c[1] for c in self.calls if c[0] == "hd"][0]
        self.assertTrue(hd_prompt.startswith("a matte blue phone stand"),
                        "高清重绘必须复用英文提示词，否则与预览构图不一致")

    def test_3d_stays_in_same_group_and_defers_rotation(self):
        """3D 完成后：**留在本组**（这样用户才看得到刚做出来的模型），
        轮转推迟到下一条消息 —— 用户反馈"建模完没在对话框里显示，却直接新建了对话"。

        旧行为是建模完立刻 new_project() → 本轮返回空白新组 → 查看器对着空组、模型看不见。
        """
        m = self._mgr()
        folder0 = m.current_project["folder"]
        pv = self.tmp / "preview.png"
        self._stub(en="x " * 30, preview=pv)
        m._do_generate_images(m.current_project)
        r = m._do_generate_3d(m.current_project)
        ck_ = self.assertIn
        self.assertIn("preview", r)
        self.assertEqual(Path(r["preview"]).name, "preview.png")
        self.assertTrue(r.get("glb"), "建模结果要带回 glb，供'此刻'展示")
        # 关键：没有换组
        self.assertEqual(r["project"]["folder"], folder0, "建模完不该立刻新建对话组")
        self.assertFalse(r.get("auto_new"), "auto_new 必须为 False（不再立刻建新组）")
        self.assertTrue(r.get("rotate_pending"), "要在 state 上打待轮转标记")
        self.assertTrue(r["project"].get("pending_rotate"))
        # 模型仍在当前组里（查看器/历史都还能看到）
        self.assertTrue(r["project"].get("glb_path"), "模型必须留在本组，别人才看得到")
        self.assertTrue(any(c[0] == "preview" for c in self.calls), "必须真的调用渲染")

    def test_new_project_avoids_name_collision(self):
        """目录名只到「秒」：同一秒连建两个组会撞名，mkdir(exist_ok=True) 会静默合并成
        一个组（表现为"新建了组但内容还在老组"、轮转失效）。撞名必须自动加序号。"""
        a = pm.create_project()
        b = pm.create_project()
        self.assertNotEqual(a["folder"], b["folder"], "同一秒建的两个组不能同名")
        self.assertTrue(Path(b["path"]).exists() and Path(a["path"]).exists())

    def test_rotation_happens_on_next_new_item(self):
        """下一条消息开始做新东西 → 这时才轮转（老组归档、新建组）"""
        m = self._mgr()
        m.current_project["pending_rotate"] = True
        old = m.current_project["folder"]
        n0 = len(pm.list_projects())
        new = m.rotate_if_finished(["set_requirement", "set_param"])
        self.assertNotEqual(new["folder"], old, "开始做新东西时必须轮转")
        self.assertFalse(new.get("pending_rotate"), "新组不该继承待轮转标记")
        self.assertGreaterEqual(len(pm.list_projects()), n0 + 1)
        self.assertTrue(new["folder"].startswith("20"), "新组是时间戳目录")

    def test_continuation_does_not_rotate(self):
        """继续用现有模型（改尺寸/导 STL/送打印/重建模）→ 留在本组，别把人家的模型丢了"""
        for act in ("export_stl", "open_bambu", "generate_3d", "modify_image"):
            m = self._mgr()
            m.current_project["pending_rotate"] = True
            old = m.current_project["folder"]
            got = m.rotate_if_finished([act])
            self.assertEqual(got["folder"], old, f"{act} 不该触发轮转")
            self.assertFalse(got.get("pending_rotate"), "留组后要清标记，否则每条消息都想轮转")

    def test_pure_question_keeps_flag(self):
        """纯问答既不轮转也不清标记：用户只是问了句，不代表这一组结束了"""
        m = self._mgr()
        m.current_project["pending_rotate"] = True
        old = m.current_project["folder"]
        got = m.rotate_if_finished(["answer"])
        self.assertEqual(got["folder"], old, "纯问答不该轮转")
        self.assertTrue(got.get("pending_rotate"), "标记要留着，等下一条真动作再判定")

    def test_execute_plan_rotates_once_not_twice(self):
        """计划里同时有 new_conversation + set_requirement 时，只能建 1 个新组
        （已轮转过就不该再按 new_conversation 建第二个）"""
        m = self._mgr()
        m.current_project["pending_rotate"] = True
        n0 = len(pm.list_projects())
        m._append_conversation = lambda *a, **k: None
        m._do_generate_images = lambda st: {"project": st, "reply": "", "images": []}
        m._apply_param_values = lambda st, a: None
        m._record_requirement = lambda *a, **k: None
        m._execute_plan({"understanding": ["x"], "reply": "r",
                         "actions": [{"name": "new_conversation", "args": {}},
                                     {"name": "set_requirement", "args": {"text": "一个新花瓶"}}]},
                        "换一个东西做，来个花瓶")
        self.assertEqual(len(pm.list_projects()), n0 + 1, "只能建一个新组")

    def test_3d_survives_preview_failure(self):
        m = self._mgr()
        self._stub(en="x " * 30, preview=None)     # 渲染失败返回 ""
        m._do_generate_images(m.current_project)
        r = m._do_generate_3d(m.current_project)
        self.assertTrue(r.get("glb"), "预览渲染失败不能影响建模本身")

    def test_clean_image_prompt_rejects_bad_output(self):
        self.assertEqual(self.de._clean_image_prompt("这是一个蓝色支架，写实风格，很好"), "")
        self.assertEqual(self.de._clean_image_prompt("blue stand"), "")
        self.assertTrue(len(self.de._clean_image_prompt(
            "a detailed matte blue phone stand with soft studio lighting, "
            "seamless white background, ultra detailed, 8k")) > 40)


class TestAgentLayer(unittest.TestCase):
    """专家 agent：LLM 出动作计划 → 白名单校验 → 执行现有执行器（2026-09-22）"""

    def setUp(self):
        self.enterContext(patch('backend.gpu_state.is_generating',return_value=False))
        self.enterContext(patch('backend.agent.is_ready',return_value=True))
        self.enterContext(patch('backend.conversation._rewarm_tuned_async'))
        self.enterContext(patch('backend.conversation._warm_image_model_async'))
        import backend.agent as ag
        import backend.describer as de
        import backend.pipeline as pl
        self.ag, self.pl = ag, pl
        # 先记录原值**再**打桩（反过来会把桩当成原值恢复，污染后续用例）
        self._orig = (pl.Pipeline.generate_images, pl.Pipeline.enhance_image,
                      pl.Pipeline.generate_3d, pl.Pipeline.export_stl,
                      ag.plan, pm.save_project, de.enhance_image_prompt,
                      ag._provider, ag.prompt_style)
        pm.save_project = lambda st: None
        de.enhance_image_prompt = lambda zh, **kw: ""      # 本类不测扩写，别真调 LLM
        # 钉住 provider/prompt 风格：本类用桩替换的是 **Ollama** 路径，
        # 而 config 里 provider 可被切成 tuned（走 HTTP）—— 不钉住的话，
        # 一改配置这些用例就会绕过桩去真调服务，用例变成依赖环境。
        ag._provider = lambda: "ollama"
        ag.prompt_style = lambda: "full"
        self._orig_cache = C._AGENT_CFG_CACHE
        C._AGENT_CFG_CACHE = True            # 这个类专测 agent 路径
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-agent-"))
        (self.tmp / "candidates").mkdir(exist_ok=True)
        (self.tmp / "t").mkdir(exist_ok=True)
        self._savedir = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = self.tmp
        self.calls = []

    def tearDown(self):
        (self.pl.Pipeline.generate_images, self.pl.Pipeline.enhance_image,
         self.pl.Pipeline.generate_3d, self.pl.Pipeline.export_stl,
         self.ag.plan, pm.save_project,
         self._de_module().enhance_image_prompt,
         self.ag._provider, self.ag.prompt_style) = self._orig
        C._AGENT_CFG_CACHE = self._orig_cache
        pm.PROJECTS_DIR = self._savedir
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _de_module():
        import backend.describer as de
        return de

    # ── 解析 ──
    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(self.ag._extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(self.ag._extract_json('好的：{"a": 1} 完毕'), {"a": 1})
        self.assertEqual(self.ag._extract_json('{"a": 1,}'), {"a": 1})   # 尾逗号
        self.assertIsNone(self.ag._extract_json("完全不是 JSON"))
        self.assertIsNone(self.ag._extract_json(""))

    def test_unknown_action_dropped(self):
        self._patch_planner('{"understanding":["x"],"reply":"r","actions":'
                            '[{"name":"delete_everything","args":{}},'
                            '{"name":"answer","args":{}}]}')
        pl = self.ag.plan("随便", {"phase": "confirm"})
        self.assertEqual([a["name"] for a in pl["actions"]], ["answer"],
                         "白名单外的动作必须被丢弃")

    def test_plan_none_on_non_json(self):
        self._patch_planner("抱歉我不会")
        self.assertIsNone(self.ag.plan("随便", {"phase": "confirm"}))

    def test_plan_falls_back_to_answer_when_no_actions(self):
        self._patch_planner('{"understanding":["闲聊"],"reply":"你好","actions":[]}')
        pl = self.ag.plan("你好", {"phase": "confirm"})
        self.assertEqual([a["name"] for a in pl["actions"]], ["answer"])

    def test_plan_retries_once_when_first_output_is_garbage(self):
        """偶发解析失败会静默退回关键词路径（那一轮变"机器味"），必须重试一次"""
        import backend.describer as de
        seq = ["不是 JSON，抱歉",
               '{"understanding":["x"],"reply":"' + "好的。" * 80 +
               '","actions":[{"name":"answer","args":{}}]}']
        calls = []

        def fake(p, timeout=60, **kw):
            calls.append(p)
            return seq[min(len(calls) - 1, 1)]

        orig = de._call_ollama
        de._call_ollama = fake
        self.addCleanup(lambda: setattr(de, "_call_ollama", orig))
        pl = self.ag.plan("你好", {"phase": "confirm", "auto_params": {}})
        self.assertIsNotNone(pl, "重试后应拿到计划")
        self.assertEqual(len(calls), 2, "第一次解析失败必须重试")
        self.assertIn("上一次的输出不是合法 JSON", calls[1], "重试要带上更严厉的约束")

    def test_plan_gives_up_after_two_bad_outputs(self):
        import backend.describer as de
        calls = []
        orig = de._call_ollama
        de._call_ollama = lambda p, timeout=60, **kw: (calls.append(p), "还是不是 JSON")[1]
        self.addCleanup(lambda: setattr(de, "_call_ollama", orig))
        self.assertIsNone(self.ag.plan("你好", {"phase": "confirm"}))
        self.assertEqual(len(calls), 2, "只重试一次就该放弃，不能无限重试")

    # ── 不抢跑规则（用户反馈"不如之前那版"的根因：自作主张烧 GPU）──
    def test_prompt_forbids_running_ahead(self):
        self.assertIn("不要抢跑生成", self.ag._SYS)
        self.assertIn("拿不准要不要生成时", self.ag._SYS)

    def test_prompt_separates_adjust_from_regenerate(self):
        self.assertIn("带方向但**没说要看出结果**", self.ag._SYS)
        self.assertIn("带方向 **且明确要看出结果**", self.ag._SYS)

    def test_example_covers_record_without_generating(self):
        """必须有一条「只记录不生成」的示例（示例比规则管用）"""
        self.assertIn("再加个防滑纹，表面别太亮", self.ag._SYS)
        self.assertIn('"name": "set_requirement", "args": {"text": "表面加防滑纹，哑光不要亮"}',
                      self.ag._SYS)

    # ── 需求记录兜底（实测同一句话有时只回话不记录 → 细节静默丢失）──
    _ANS = [{"name": "answer", "args": {}}]

    def test_guard_records_statement_in_requirement_phase(self):
        out = self.ag.guard_requirement_recorded({"phase": "confirm"}, "再加个防滑纹，表面别太亮",
                                                 list(self._ANS))
        self.assertEqual([a["name"] for a in out], ["set_requirement", "answer"])
        self.assertIn("防滑纹", out[0]["args"]["text"])

    def test_guard_skips_questions(self):
        for q in ("这个材质用 ABS 好还是 PETG？", "能推荐个材质吗", "这样好不好？"):
            with self.subTest(q=q):
                self.assertEqual(self.ag.guard_requirement_recorded({"phase": "confirm"}, q,
                                                                    list(self._ANS)), self._ANS)

    def test_guard_skips_short_and_non_requirement_phases(self):
        self.assertEqual(self.ag.guard_requirement_recorded({"phase": "confirm"}, "嗯不太满意",
                                                            list(self._ANS)), self._ANS)
        self.assertEqual(self.ag.guard_requirement_recorded({"phase": "3d"},
                                                            "再加个防滑纹，表面别太亮",
                                                            list(self._ANS)), self._ANS)

    def test_guard_covers_selecting_but_not_commands(self):
        """出图后（selecting）补细节是最常见的动作，必须记；
        但命令句（出图/建模/打印…）不能当成需求记进去"""
        st = {"phase": "selecting"}
        out = self.ag.guard_requirement_recorded(st, "再加个防滑纹，表面别太亮",
                                                 list(self._ANS))
        self.assertEqual([a["name"] for a in out], ["set_requirement", "answer"])
        for cmd in ("出一版图看看", "开始建模吧", "好了放 Bambu 里打印吧", "重新做一版"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.ag.guard_requirement_recorded(st, cmd, list(self._ANS)),
                                 self._ANS, f"命令句不该被记成需求: {cmd}")

    def test_guard_skips_when_other_actions_exist(self):
        acts = [{"name": "set_requirement", "args": {"text": "x"}}, {"name": "answer", "args": {}}]
        self.assertEqual(self.ag.guard_requirement_recorded({"phase": "confirm"}, "再加防滑纹",
                                                            acts), acts)

    # ── 思考深度：把模型真实推理摊开（用户反馈"思考深度不够"）──
    def test_thinking_gist_takes_tail_and_cleans(self):
        raw = "前面都是复述需求。\n\n" + "决策：选 A 而不是 B，因为更省时间。" * 12
        g = self.ag._thinking_gist(raw, limit=120)
        self.assertLessEqual(len(g), 120)
        self.assertIn("决策", g)
        self.assertNotIn("\n", g)
        self.assertEqual(self.ag._thinking_gist(""), "")

    def test_plan_exposes_thinking_from_ollama(self):
        import backend.describer as de
        orig = de._call_ollama
        orig_thinking = de.LAST_THINKING
        try:
            de.LAST_THINKING = "用户只是想记录需求，不要生成。决定：只 set_requirement。"
            de._call_ollama = lambda p, timeout=60, **kw: (
                '{"understanding":["x"],"reply":"' + "好。" * 90 +
                '","actions":[{"name":"answer","args":{}}]}')
            pl = self.ag.plan("再加个防滑纹", {"phase": "confirm", "auto_params": {}})
        finally:
            de._call_ollama = orig
            de.LAST_THINKING = orig_thinking
        self.assertIn("只 set_requirement", pl["thinking"], "必须把模型的真实推理带出来给 UI 展示")

    def test_prompt_asks_for_tradeoff_reasoning(self):
        """understanding 不能是标签，要写出权衡（示例才是真正的锚）"""
        self.assertIn("权衡", self.ag._SYS)
        self.assertIn("考虑过 X 还是 Y", self.ag._SYS)

    # ── 流式输出（用户要求：现在的输出也搞成流式）──
    def test_partial_reply_extracts_growing_json(self):
        pr = self.ag.partial_reply
        self.assertEqual(pr('{"understanding":["x"],"reply":"你好'), "你好")
        self.assertEqual(pr('{"reply":"第一行\\n第二行'), "第一行\n第二行")
        self.assertEqual(pr('{"reply":"好了。","actions":[]}'), "好了。", "遇到闭合引号应停止")
        self.assertEqual(pr('{"reply":"尾部反斜杠\\'), "尾部反斜杠", "转义没到齐不能吐出反斜杠")
        self.assertEqual(pr('{"understanding":["x"]'), "", "还没写到 reply 时为空")
        self.assertEqual(pr(""), "")

    def test_plan_stream_yields_progress_then_plan(self):
        import backend.describer as de
        orig = de._stream_once
        # 真实 _stream_once 给的是**累积文本**，假流式必须照此契约
        full = ('{"understanding":["x"],"reply":"你好，这是回复。",'
                '"actions":[{"name":"answer","args":{}}]}')
        cut = full.index("你") + 1

        def fake_stream(prompt, model, think, np_, temp, timeout, status, num_ctx=None, json_mode=False):
            for upto in (cut, len(full)):
                status["thinking"] = status.get("thinking", 0) + 5
                # 真实 _stream_once 会把思考原文留在 _thinking_text（收尾即判断依据）
                status["_thinking_text"] = "权衡：只回话，不动工作流。"
                yield full[:upto]

        de._stream_once = fake_stream
        self.addCleanup(lambda: setattr(de, "_stream_once", orig))
        replies, plan = [], None
        for ev in self.ag.plan_stream("你好", {"phase": "confirm", "auto_params": {}}):
            if "plan" in ev:
                plan = ev["plan"]
                break
            replies.append(ev.get("reply", ""))
        self.assertEqual(replies, ["你", "你好，这是回复。"], "进度事件必须逐块给出累积正文")
        self.assertIsNotNone(plan)
        self.assertEqual([a["name"] for a in plan["actions"]], ["answer"])
        self.assertTrue(plan["thinking"], "流式下也要把思考摘要带出来")

    def test_agent_turn_stream_yields_progress_then_result(self):
        orig_plan_stream = self.ag.plan_stream
        self.ag.plan_stream = lambda text, st, **kw: iter([
            {"reply": "正在写", "thinking_chars": 12},
            {"plan": {"understanding": ["u"], "reply": "好。" * 80,
                      "actions": [{"name": "answer", "args": {}}], "thinking": "t"}}])
        self.addCleanup(lambda: setattr(self.ag, "plan_stream", orig_plan_stream))
        m = self._mgr()
        seen, result = [], None
        for ev in m.handle_agent_turn_stream("你好"):
            if "result" in ev:
                result = ev["result"]
                break
            seen.append(ev.get("reply"))
        self.assertEqual(seen, ["正在写"], "必须先给进度")
        self.assertEqual(result["via"], "agent")
        self.assertEqual([a["name"] for a in result["actions"]], ["answer"])

    # ── provider 开关：值来自 config（不假设是哪个，环境里可能切成 tuned）──
    def test_provider_and_style_come_from_config(self):
        import json as _json
        cfg = _json.loads((Path(__file__).resolve().parent.parent / "config.json")
                          .read_text(encoding="utf-8"))["agent"]
        self.assertEqual(self.ag._agent_cfg().get("provider", "ollama"),
                         cfg.get("provider", "ollama"))
        self.assertEqual(self.ag._agent_cfg().get("prompt_style", "full"),
                         cfg.get("prompt_style", "full"))

    def test_call_model_routes_to_tuned_server(self):
        import urllib.request

        class FakeResp:
            def read(self): return b'{"message": {"content": "TUNED"}}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        seen, orig = [], (self.ag._provider, self.ag.tuned_url, urllib.request.urlopen)
        try:
            self.ag._provider = lambda: "tuned"
            self.ag.tuned_url = lambda: "http://127.0.0.1:9999"
            urllib.request.urlopen = lambda req, timeout=None: (seen.append(req.full_url),
                                                                FakeResp())[1]
            out = self.ag._call_model("你好")
        finally:
            self.ag._provider, self.ag.tuned_url, urllib.request.urlopen = orig
        self.assertEqual(out, "TUNED")
        self.assertIn("/api/chat", seen[0])

    def test_stream_model_routes_to_tuned_server(self):
        import urllib.request

        class FakeResp:
            def __iter__(self):
                for t in ("你", "好"):
                    yield ('{"text": "%s", "done": false}\n' % t).encode("utf-8")
                yield b'{"text": "", "done": true}\n'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        orig = (self.ag._provider, self.ag.tuned_url, urllib.request.urlopen)
        try:
            self.ag._provider = lambda: "tuned"
            self.ag.tuned_url = lambda: "http://127.0.0.1:9999"
            urllib.request.urlopen = lambda req, timeout=None: FakeResp()
            chunks = list(self.ag._stream_model("你好"))
        finally:
            self.ag._provider, self.ag.tuned_url, urllib.request.urlopen = orig
        self.assertEqual(chunks, ["你", "你好"], "微调服务也要给累积文本")

    def test_compact_prompt_is_much_smaller(self):
        """微调的意义：规则内化后 prompt 可以大幅缩短（省时间也省上下文）

        ★ 2026-10-18 调整：宇凯要求把站点/铸想的事实喂给 agent，
          而知识库是**全模式共用**的 —— compact 从 ~1.7k 涨到 ~5k。
          这里改成断言"compact 仍明显小于 full"，并单独校验知识库已注入。
          compact 存在的意义（省掉冗长规则）没变，只是多了事实部分。
        """
        st = {"phase": "selecting", "base_prompt": "一个蓝色手机支架", "conversation": [],
              "auto_params": {"style": "写实", "size_mm": 90, "color": "蓝色"},
              "candidates": ["a.png"], "selected": "a.png"}
        compact = self.ag._build_compact_prompt("再加个防滑纹", st)
        full = self.ag._build_plan_prompt("再加个防滑纹", st)   # 默认 full 风格
        # compact 仍是 full 的一半以内（规则部分确实省了）
        self.assertLess(len(compact), len(full) * 0.75,
                        f"compact={len(compact)} 应该明显小于 full={len(full)}")
        # 知识库必须两种模式都有（否则 agent 会失去站点事实）
        self.assertIn("ZYKT studio 是什么", compact)
        self.assertIn("ZYKT studio 是什么", full)
        for mark in ("【工作流状态】", "【最近对话】", "【用户这句话】"):
            self.assertIn(mark, compact)

    # ── 训练锁：训练期间不许把 12GB 对话模型拉回显存（实测拖慢训练 3 倍）──
    def test_describer_guards_respect_training_lock(self):
        import backend.describer as de
        lock = Path(__file__).resolve().parent.parent / "finetune" / "TRAINING.lock"
        made = not lock.exists()          # 训练正在跑时不要动别人的锁
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            if made:
                lock.write_text("unit-test", encoding="utf-8")
            self.assertTrue(de._training_lock_active())
            self.assertFalse(de.warm_up_ollama(), "锁在时预热必须被拒")
            self.assertIn("训练进行中", de._call_ollama("x"), "锁在时不许真发请求")
            self.assertIn("训练进行中", list(de._stream_once("x", de.OLLAMA_MODEL, "low", 16, 0.7,
                                                             5, {}, 4096))[0])
        finally:
            if made:
                lock.unlink(missing_ok=True)

    # ── 校验/夹取 ──
    def test_size_is_clamped(self):
        self.assertEqual(self.ag.validate_args("set_param", {"size_mm": 9999})["size_mm"], 300.0)
        self.assertEqual(self.ag.validate_args("set_param", {"size_mm": 1})["size_mm"], 20.0)

    def test_bad_resolution_and_model_dropped(self):
        out = self.ag.validate_args("set_param", {"resolution": "不存在的档位", "model": "不存在的模型"})
        self.assertNotIn("resolution", out)
        self.assertNotIn("model", out)

    def test_valid_resolution_and_model_kept(self):
        from backend.pipeline import Pipeline
        res = list(Pipeline().get_resolutions().keys())[0]
        img = list(Pipeline().config["models"]["image"].keys())[0]
        out = self.ag.validate_args("set_param", {"resolution": res, "model": img})
        self.assertEqual(out["resolution"], res)
        self.assertEqual(out["model"], img)

    # ── 执行 ──
    def _patch_planner(self, json_text):
        """只桩 LLM 的返回，让真实的 plan() 跑完整 解析→白名单→校验 路径"""
        import backend.describer as de
        orig = de._call_ollama
        de._call_ollama = lambda prompt, timeout=60, **kw: json_text
        self.addCleanup(lambda: setattr(de, "_call_ollama", orig))

    def _stub_pipeline(self):
        calls = self.calls
        hd, glb, stl = self.tmp / "hd.png", self.tmp / "m.glb", self.tmp / "m.stl"

        class R:
            success = True
            def __init__(self, p): self.output_path = p

        def gen(_p, prompt, count=1, **kw):
            calls.append(("gen", prompt))
            (self.tmp / "gen.png").write_bytes(b"i")
            return [R(str(self.tmp / "gen.png"))]

        def hd_(_p, image_path, prompt, **kw):
            calls.append(("hd", prompt)); hd.write_bytes(b"h"); return str(hd)

        def g3(_p, **kw):
            calls.append(("g3",)); glb.write_bytes(b"g"); return R(str(glb))

        def es(_p, glb_, **kw):
            calls.append(("stl",)); stl.write_bytes(b"s"); return R(str(stl))

        self.pl.Pipeline.generate_images, self.pl.Pipeline.enhance_image = gen, hd_
        self.pl.Pipeline.generate_3d, self.pl.Pipeline.export_stl = g3, es

    def _mgr(self, **over):
        m = C.ConversationManager.__new__(C.ConversationManager)
        m.current_project = fresh(phase="confirm", base_prompt="一个蓝色手机支架",
                                  auto_params={"style": "写实", "size_mm": 90,
                                               "color": "蓝色", "model": "z_image"})
        m.current_project["path"] = str(self.tmp)
        m.current_project.update(over)
        m._append_conversation = lambda *a, **k: None
        return m

    def test_agent_turn_applies_params_and_generates(self):
        self._stub_pipeline()
        self._patch_planner(
            '{"understanding":["改颜色和尺寸","要新图"],"reply":"好，我改一下。",'
            '"actions":[{"name":"set_param","args":{"color":"哑光黑","size_mm":120}},'
            '{"name":"generate_image","args":{}}]}')
        m = self._mgr()
        r = m.handle_agent_turn("颜色改成哑光黑，尺寸 120，并生成图片")
        self.assertEqual(m.current_project["auto_params"]["color"], "哑光黑")
        self.assertEqual(m.current_project["auto_params"]["size_mm"], 120)
        self.assertIn("哑光黑", m.current_project["base_prompt"], "base_prompt 旧颜色必须被替换")
        self.assertTrue(any(c[0] == "gen" for c in self.calls), "必须真的出图")
        self.assertIn("好，我改一下。", r["reply"])
        self.assertEqual(r["via"], "agent")
        self.assertEqual([a["name"] for a in r["actions"]], ["set_param", "generate_image"])

    def test_parameter_change_alone_does_not_run_generation(self):
        self._stub_pipeline()
        self._patch_planner('{"reply":"参数已更新。","actions":[{"name":"set_param","args":{"color":"哑光黑"}},{"name":"generate_image","args":{}}]}')
        result=self._mgr().handle_agent_turn('颜色改成哑光黑')
        self.assertFalse(any(call[0]=='gen' for call in self.calls))
        self.assertIn('生成图片',result['reply'])

    def test_generate_3d_implicitly_generates_image_first(self):
        self._stub_pipeline()
        self._patch_planner('{"understanding":["要建模"],"reply":"开始建模",'
                            '"actions":[{"name":"generate_3d","args":{}}]}')
        m = self._mgr(candidates=[], selected="")
        m.handle_agent_turn("开始建模")
        kinds = [c[0] for c in self.calls]
        self.assertIn("gen", kinds, "还没有图时，建模必须先出图")
        self.assertLess(kinds.index("gen"), kinds.index("g3"), "顺序必须是先出图再建模")

    def test_export_stl_without_glb_asks_instead_of_crashing(self):
        self._stub_pipeline()
        self._patch_planner('{"understanding":["要导STL"],"reply":"好的",'
                            '"actions":[{"name":"export_stl","args":{}}]}')
        m = self._mgr(glb_path="")
        r = m.handle_agent_turn("导一下 STL")
        self.assertIn("还没有 3D 模型", r["reply"])
        self.assertEqual([c for c in self.calls if c[0] == "stl"], [])

    def test_agent_turn_none_when_plan_unavailable(self):
        self.ag.plan = lambda text, st, **kw: None
        self.assertIsNone(self._mgr().handle_agent_turn("随便"))

    def test_handle_user_input_falls_back_when_agent_down(self):
        """agent 挂了也必须能工作（关键词路径兜底）"""
        self.ag.plan = lambda text, st, **kw: None
        m = self._mgr()
        r = m.handle_user_input("继续细化要求")
        self.assertIsNotNone(r)
        self.assertNotEqual(r.get("via"), "agent")

    def test_agent_can_be_switched_off(self):
        C._AGENT_CFG_CACHE = False
        self.assertFalse(C.agent_enabled())

    # ── 回复质量与"省钱"规则 ──
    #
    # ★ 2026-10-18 改：原来断言"180-300 字 / 不得少于 150 字"，
    #   但宇凯实测后反馈"新用户只说 8 个字，回 300+ 字太啰嗦，还等 18 秒"。
    #   规则改成**按场景分级**：首次接触短、要展开才长。
    #   这个测试的**意图不变**（回复要有料、不能敷衍），只是断言新的分级要求。
    def test_prompt_demands_rich_replies(self):
        # 必须仍然要求"有料"，只是分段给
        self.assertIn("60-100 字", self.ag._SYS)      # 首次接触
        self.assertIn("100-200 字", self.ag._SYS)     # 纯咨询
        self.assertIn("250 字以上", self.ag._SYS)      # 用户明确要展开时
        # 还要明确禁止"一上来就啰嗦"
        self.assertIn("默认短", self.ag._SYS)

    def test_tail_reminder_present_in_plan_prompt(self):
        """篇幅要求必须在 prompt 最尾部（放中间时模型忽长忽短）"""
        src = inspect.getsource(self.ag._build_plan_prompt)
        self.assertIn("提醒（本轮必须遵守）", src)
        self.assertLess(src.index("提醒（本轮必须遵守）"), src.index("只输出 JSON"))

    def test_prompt_forbids_regenerating_existing_artifacts(self):
        self.assertIn("不要重复生成已有的产物", self.ag._SYS)
        self.assertIn("8c", self.ag._SYS)       # 打印但没 STL → 先建模

    def test_vague_complaint_must_ask_first(self):
        self.assertIn("含糊不满", self.ag._SYS)
        self.assertIn("先问清要改哪里", self.ag._SYS)

    def test_examples_are_long_enough_to_anchor(self):
        """示例才是真正的锚：需求类示例的 reply 必须够长，否则模型照样写短"""
        import re
        replies = re.findall(r'"reply": "(.*?)"', self.ag._SYS, flags=re.S)
        long_ones = [r for r in replies if len(r) >= 120]
        self.assertGreaterEqual(len(long_ones), 3,
                                f"长示例只有 {len(long_ones)} 个：{[len(r) for r in replies]}")

    def test_sys_template_avoids_fstring_brace_escaping(self):
        """JSON 花括号曾因 f-string 转义炸过：改用占位符替换"""
        self.assertNotIn("<<TOOLS>>", self.ag._SYS)          # 已替换
        self.assertIn("<<TOOLS>>", self.ag._SYS_TMPL)
        self.assertIn("set_requirement", self.ag._SYS)       # 工具说明确实注入了

    # ── 回复下限兜底（实测同句模型给 123~426 字，光靠 prompt 不稳）──
    def _st(self, **params):
        p = {"style": "写实", "size_mm": 90, "color": "蓝色"}
        p.update(params)
        return {"auto_params": p, "stl_size": p.get("size_mm")}

    def test_ensure_rich_appends_and_uses_pending_values(self):
        st = self._st()
        acts = [{"name": "set_param", "args": {"color": "哑光黑", "size_mm": 120}}]
        out = self.ag._ensure_rich("收到。", st, acts)
        self.assertGreaterEqual(len(out), self.ag._RICH_MIN)
        self.assertIn("哑光黑", out)
        self.assertIn("120mm", out)
        self.assertNotIn("蓝色", out, "补的参数行必须用本轮即将生效的新值，不能是旧值")

    def test_ensure_rich_keeps_long_reply(self):
        st = self._st()
        long_reply = "很长的回复。" * 40
        self.assertEqual(self.ag._ensure_rich(long_reply, st, [{"name": "answer", "args": {}}]),
                         long_reply)

    def test_ensure_rich_skips_pure_action_turns(self):
        st = self._st()
        for acts in ([{"name": "generate_3d", "args": {}}],
                     [{"name": "open_bambu", "args": {}}],
                     [{"name": "export_stl", "args": {}}]):
            with self.subTest(acts=acts):
                self.assertEqual(self.ag._ensure_rich("好的。", st, acts), "好的。")

    def test_ensure_rich_skips_when_action_also_generates(self):
        """set_param + generate_image：出图执行器自己会补参数与英文提示词，别再叠一段"""
        st = self._st()
        acts = [{"name": "set_param", "args": {"color": "哑光黑"}},
                {"name": "generate_image", "args": {}}]
        self.assertEqual(self.ag._ensure_rich("好。", st, acts), "好。")


class TestNoRegression(unittest.TestCase):
    def test_editing_path_has_no_dead_marker(self):
        """「修改要求：」是原 bug 的残留标记，不得再出现在代码路径里"""
        import tokenize
        src = (ROOT / "backend" / "conversation.py").read_bytes()
        toks = [t.string for t in tokenize.tokenize(iter(src.splitlines(True)).__next__)
                if t.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)]
        self.assertNotIn("修改要求：", " ".join(toks))

    def test_backend_modules_parse(self):
        for name in ["conversation", "intent_classifier", "describer", "project_manager",
                     "pipeline", "mesh_optimizer", "startup"]:
            with self.subTest(module=name):
                p = ROOT / "backend" / f"{name}.py"
                ast.parse(p.read_text(encoding="utf-8"))


class TestTunedServiceWiring(unittest.TestCase):
    """微调 agent 服务的接入（启动检查里自动拉起）。

    以前这服务是人工起的：双击 start-all.ps1 起来的 app 不知道有这回事，
    于是 provider=tuned 但服务不在 → agent 每轮静默回退关键词路径（用户观感=变笨）。
    """

    def setUp(self):
        self.enterContext(patch('backend.gpu_state.is_generating',return_value=False))

    def _checker(self):
        import backend.startup as SU
        return SU

    def test_skips_when_provider_is_ollama(self):
        SU = self._checker()
        orig = SU._read_agent_cfg
        SU._read_agent_cfg = lambda: {"provider": "ollama"}
        try:
            c = SU.StartupChecker()
            c._check_tuned_agent()
            self.assertEqual([r.status for r in c.results], ["ok"])
            self.assertIn("未启用", c.results[0].message)
        finally:
            SU._read_agent_cfg = orig

    def test_reports_online_without_starting(self):
        SU = self._checker()
        orig_cfg, orig_http, orig_start = (SU._read_agent_cfg, SU.StartupChecker._http_ok,
                                           SU.StartupChecker._start_tuned_agent)
        started = []
        SU._read_agent_cfg = lambda: {"provider": "tuned", "tuned_url": "http://127.0.0.1:8082"}
        SU.StartupChecker._http_ok = lambda self, url, timeout=3.0: True
        SU.StartupChecker._start_tuned_agent = lambda self, url: started.append(url) or True
        try:
            c = SU.StartupChecker()
            c._check_tuned_agent()
            self.assertEqual([r.status for r in c.results], ["ok"])
            self.assertFalse(started, "在线时不该重复启动")
        finally:
            (SU._read_agent_cfg, SU.StartupChecker._http_ok,
             SU.StartupChecker._start_tuned_agent) = orig_cfg, orig_http, orig_start

    def test_autostarts_when_offline(self):
        SU = self._checker()
        orig_cfg, orig_http, orig_start = (SU._read_agent_cfg, SU.StartupChecker._http_ok,
                                           SU.StartupChecker._start_tuned_agent)
        started = []
        SU._read_agent_cfg = lambda: {"provider": "tuned", "tuned_url": "http://127.0.0.1:8082"}
        SU.StartupChecker._http_ok = lambda self, url, timeout=3.0: False
        SU.StartupChecker._start_tuned_agent = lambda self, url: started.append(url) or True
        try:
            c = SU.StartupChecker()
            c._check_tuned_agent()
            self.assertIn("fixed", [r.status for r in c.results])
            self.assertEqual(started, ["http://127.0.0.1:8082"])
            self.assertTrue(c.fixed, "启动成功要记进 fixed（启动摘要里会打印）")
        finally:
            (SU._read_agent_cfg, SU.StartupChecker._http_ok,
             SU.StartupChecker._start_tuned_agent) = orig_cfg, orig_http, orig_start

    def test_is_ready_reflects_service_state(self):
        """is_ready 决定是否给用户显示"正在加载"并预热。曾经它漏了 import urllib.request，
        NameError 被 except 静默吞掉 → 永远返回 False（每轮白打一次 /warm + 误报"加载中"）。
        所以这里既验行为，也验"失败不静默"。"""
        import io
        import contextlib
        import urllib.request
        import backend.agent as AG
        orig_url, orig_open, orig_cfg = AG.tuned_url, urllib.request.urlopen, AG._agent_cfg

        class _Resp:                                   # 假响应：loaded=True
            def read(self): return b'{"loaded": true, "ok": true}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        try:
            AG.tuned_url = lambda: "http://127.0.0.1:9/health"   # 死端口
            AG._agent_cfg = lambda: {"provider": "tuned"}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                down = AG.is_ready()
            self.assertFalse(down)
            self.assertIn("探活微调服务失败", buf.getvalue(),
                          "失败必须可见，不能再被 except 静默吞掉")
            urllib.request.urlopen = lambda req, timeout=None: _Resp()
            self.assertTrue(AG.is_ready(), "在线且 loaded 时必须 True（否则每轮都误报加载中）")
        finally:
            AG.tuned_url, urllib.request.urlopen, AG._agent_cfg = orig_url, orig_open, orig_cfg

    def test_export_stl_button_shape_and_reply(self):
        """按钮 handler 的 yield 形状必须是 (state, chat, …) —— state 在最前。

        接的是 outputs=[state] + ui_outputs；若照 on_submit 那种 ("", state, …) 写，
        会把 "" 写进 state 组件。同时必须把 reply 亮给用户（成功说尺寸、失败给原因），
        否则点了按钮没任何反馈 = 这一步等于不存在（用户 2026-09-23 就是这么反馈的）。
        """
        import io
        import tempfile
        from contextlib import redirect_stdout
        import shutil
        import app as A
        tmp = Path(tempfile.mkdtemp(prefix="dsh-stlbtn-"))
        orig = A.conv._do_export_stl
        A.conv._do_export_stl = lambda st: {"project": dict(st, stl_path=str(tmp / "m.stl")),
                                           "reply": "✅ **STL 已导出！** 100mm", "images": []}
        try:
            (tmp / "m.stl").write_bytes(b"solid x")
            st = fresh(phase="stl")
            st["glb_path"] = str(tmp / "m.glb")
            (tmp / "m.glb").write_bytes(b"glTF")
            with redirect_stdout(io.StringIO()):
                outs = list(A.on_btn_export_stl(st))
            self.assertGreaterEqual(len(outs), 2, "应至少给一次进度 + 一次结果")
            first = outs[0]
            self.assertIsInstance(first, tuple)
            self.assertIsInstance(first[0], dict,
                                  "yield 第一位必须是 state（否则会被写进 state 组件）")
            notes = " ".join(str(o) for o in outs)
            self.assertIn("STL 已导出", notes, "结果必须回显（成功/失败都要）")
        finally:
            A.conv._do_export_stl = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_export_stl_button_warns_without_model(self):
        import io
        from contextlib import redirect_stdout
        import app as A
        st = fresh(phase="confirm")
        with redirect_stdout(io.StringIO()):
            outs = list(A.on_btn_export_stl(st))
        self.assertIn("还没有 3D 模型", " ".join(str(o) for o in outs))

    def test_hunyuan_shapegen_removes_background(self):
        """Hunyuan ShapeGen 必须去背景。

        2026-09-23 实测踩到：工作流里写了 remove_background=False，而该节点内部是
        `if remove_background or pil_image.mode == "RGB": 去背景`（传进来前已被转成 RGBA）
        → False 等于完全不去背景 → 白底被当几何体重建成一大块背景板，
        产物变成"小熊 + 一片碎片板"（1 个连通块 / 4 万面，把连通块数那道闸也骗过去了）。
        该节点自己的默认值就是 True。
        """
        import json
        from backend.comfy_client import ComfyUIClient
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        c = ComfyUIClient(cfg)
        wf = c.build_workflow("3d", {"model_config": cfg["models"]["3d"]["hunyuan21"],
                                     "input_image": "x.jpg", "output_path": "o.glb",
                                     "seed": 1, "octree_resolution": 256,
                                     "guidance_scale": 5.0})["prompt"]
        sg = [n["inputs"] for n in wf.values()
              if n["class_type"] == "[Comfy3D] Hunyuan3D 21 ShapeGen"][0]
        foreground=wf[sg['image'][0]]
        self.assertEqual(foreground['class_type'],'JoinImageWithAlpha')
        inverted=wf[foreground['inputs']['alpha'][0]]
        self.assertEqual(inverted['class_type'],'InvertMask')
        self.assertEqual(wf[inverted['inputs']['mask'][0]]['class_type'],'RemoveBackground')
        self.assertIs(sg.get('remove_background'),False,'前景已由必需节点提取，不再调用可能静默失败的内部分割')
        self.assertIs(sg.get("auto_cleanup"), True)

    def test_gate_detects_background_slab(self):
        """质量闸要能识破"物体 + 一片背景板"（只数连通块会被它骗过）。"""
        import tempfile, shutil
        import trimesh
        from backend.pipeline import Pipeline
        tmp = Path(tempfile.mkdtemp(prefix="dsh-slab-"))
        try:
            slab = tmp / "slab.glb"                      # 薄板 + 高面数 = 背景板特征
            trimesh.creation.box(extents=(100, 100, 0.5)).subdivide_to_size(
                max_edge=1.0).export(slab)
            cube = tmp / "cube.glb"
            trimesh.creation.box(extents=(50, 50, 50)).subdivide_to_size(max_edge=1.0).export(cube)
            p = Pipeline()
            flat_slab, msg = p._mesh_is_flat_slab(str(slab))
            flat_cube, _ = p._mesh_is_flat_slab(str(cube))
            self.assertTrue(flat_slab, f"薄板应被判为背景板（{msg}）")
            self.assertFalse(flat_cube, "正常立方体不该被误判")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_triposplat_workflow_matches_official_blueprint(self):
        """TripoSplat 的采样参数必须等于 ComfyUI 官方蓝图的值。

        官方 blueprints/Image to Gaussian Splat (TripoSplat).json：20 步 / cfg 3 / dpmpp_2m。
        我们原来写 30 步 / cfg 5.0 / euler —— 采样器与 cfg 都错。
        另外这条测试还防一个我自己犯过的错：改这里时把 KSampler 写重了（多一个孤儿节点会
        让 ComfyUI 白跑一整遍采样）。
        """
        import json
        from backend.comfy_client import ComfyUIClient
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        c = ComfyUIClient(cfg)
        q = {"steps": 20, "cfg": 3.0, "sampler": "dpmpp_2m",
             "resolution": 384, "smooth": 0, "min_component": 500}
        wf = c.build_workflow("3d", {"model_config": {"name": "TripoSplat"}, "input_image": "x.jpg",
                                     "output_path": "o.glb", "seed": 1, "_tripo_quality": q})["prompt"]
        kss = [n["inputs"] for n in wf.values() if n["class_type"] == "KSampler"]
        self.assertEqual(len(kss), 1, "只允许 1 个 KSampler（多了 = 孤儿节点空跑一遍采样）")
        self.assertEqual(kss[0]["steps"], 20)
        self.assertEqual(float(kss[0]["cfg"]), 3.0)
        self.assertEqual(kss[0]["sampler_name"], "dpmpp_2m")
        stm = [n["inputs"] for n in wf.values() if n["class_type"] == "SplatToMesh"][0]
        self.assertEqual(stm["resolution"], q["resolution"])
        self.assertEqual(stm["min_component"], q["min_component"])

    def test_3d_quality_gate(self):
        """碎块云不许出厂：干净网格放行、碎块云触发 Hunyuan3D 兜底、且不会无限递归。

        背景（2026-09-23 用户报"建模有大问题"）：TripoSplat 输出全是 2000~23000 个连通块的
        碎块云、无一水密，流程却当成功一路放行 → mesh_optimizer 把它糊成石头状块体。
        """
        import tempfile, shutil
        import trimesh
        from backend.pipeline import Pipeline
        tmp = Path(tempfile.mkdtemp(prefix="dsh-gate-"))
        try:
            clean, debris, better = tmp / "c.glb", tmp / "d.glb", tmp / "b.glb"
            trimesh.creation.icosphere(subdivisions=3).export(clean)
            trimesh.util.concatenate(
                [trimesh.creation.box(extents=(.1, .1, .1)) for _ in range(300)]).export(debris)
            trimesh.creation.icosphere(subdivisions=3).export(better)
            self.assertEqual(Pipeline()._mesh_parts(str(clean))[0], 1)

            p = Pipeline()
            calls, orig = [], p.generate_3d
            # ★ 2026-10-18：generate_3d 新增 max_facenum / octree_override 两个直通旋钮
            #   （精度对比/终稿控制用）。桩必须跟着收下，否则兜底透传时会 TypeError ✗
            p.generate_3d = lambda model_key="hunyuan3d", quality_name="标准", \
                image_path=None, _fallback_tried=False, \
                max_facenum=0, octree_override=0: (
                    calls.append((model_key, _fallback_tried)),
                    type("R", (), {"success": True, "output_path": str(better)})())[1]
            try:
                r_clean = p._finalize_3d(str(clean), "triposplat", "标准", "x.jpg", False)
                after_clean = list(calls)
                r_bad = p._finalize_3d(str(debris), "triposplat", "标准", "x.jpg", False)
                after_bad = list(calls)
                r_norec = p._finalize_3d(str(debris), "triposplat", "标准", "x.jpg", True)
                after_norec = list(calls)
            finally:
                p.generate_3d = orig
            self.assertTrue(r_clean.success and not after_clean, "干净网格不该触发兜底")
            self.assertEqual(after_bad, [("hunyuan21", True)],
                             "碎块云要兜底到 hunyuan21，并标记 _fallback_tried=True 防递归")
            self.assertEqual(Path(r_bad.output_path), better, "两个里挑连通块少的（这里取更好那个）")
            self.assertEqual(after_norec, after_bad, "_fallback_tried=True 时不再兜底")
            self.assertTrue(r_bad.success, "合格的兜底产物应放行")
            self.assertFalse(r_norec.success, "已重试仍不合格，不能伪报成功")

            # 夹带背景板的产物：板与物体连成一体 → 也是 1 个连通块，只比数量会误留坏的
            slab = tmp / "slab.glb"
            trimesh.creation.box(extents=(100, 100, 0.5)).subdivide_to_size(
                max_edge=1.0).export(slab)
            self.assertEqual(Pipeline()._mesh_parts(str(slab))[0], 1, "薄板应为 1 个连通块（才骗得过旧判据）")
            self.assertTrue(Pipeline()._mesh_is_flat_slab(str(slab))[0], "薄板要判为夹带背景板")
            calls.clear()
            # ★ 2026-10-18：generate_3d 新增 max_facenum / octree_override 两个直通旋钮
            #   （精度对比/终稿控制用）。桩必须跟着收下，否则兜底透传时会 TypeError ✗
            p.generate_3d = lambda model_key="hunyuan3d", quality_name="标准", \
                image_path=None, _fallback_tried=False, \
                max_facenum=0, octree_override=0: (
                    calls.append((model_key, _fallback_tried)),
                    type("R", (), {"success": True, "output_path": str(better)})())[1]
            try:
                r_slab = p._finalize_3d(str(slab), "hunyuan21", "标准", "x.jpg", False)
            finally:
                p.generate_3d = orig
            self.assertEqual(Path(r_slab.output_path), better,
                             "薄板必须被换成兜底产物（连通块数相同也不能留着坏的）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_warmup_dedup(self):
        """预热不能堆积：GPU 忙时每次预热都会在 ComfyUI 队列排新作业（实测堆过 5 个）。

        桩掉两处，让这条测试**完全不碰网络**：
          · Pipeline.generate_images —— 不真塞队列
          · requests.get —— 预热线程里的"查队列"HTTP 请求（8s 超时）。
            它是这条测试唯一的真实外部依赖：ComfyUI 忙时整个套件里它会假失败
            （2026-09-23 实测：单跑过、全套挂 ✗）。查队列失败本来就被代码当作
            "照常预热"，所以桩掉它不改变被测语义，只是去掉不确定性。
        """
        import time
        import types
        import backend.conversation as C
        from backend.pipeline import Pipeline
        calls = []
        orig = Pipeline.generate_images
        Pipeline.generate_images = lambda self, *a, **k: calls.append(a) or []
        import requests as _rq
        orig_get = _rq.get
        _rq.get = lambda *a, **k: types.SimpleNamespace(
            json=lambda: {"queue_pending": []})       # 队列空 → 照常预热
        C._WARM_INFLIGHT.clear()
        try:
            C._warm_image_model_async("z_image")
            C._warm_image_model_async("z_image")      # 立刻再来一次：必须被拦
            # 有界轮询而不是固定 sleep：预热在线程里跑
            deadline = time.time() + 20
            while C._WARM_INFLIGHT.is_set() and time.time() < deadline:
                time.sleep(0.2)
            self.assertLessEqual(len(calls), 1, f"预热被重复提交 {len(calls)} 次（应 ≤1）")
            self.assertFalse(C._WARM_INFLIGHT.is_set(), "跑完要清标记，否则以后永远不再预热（等满 20s）")
            C._warm_image_model_async("z_image")      # 标记清了 → 允许下一次
            deadline = time.time() + 20
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.2)
            self.assertLessEqual(len(calls), 2)
        finally:
            Pipeline.generate_images = orig
            _rq.get = orig_get
            C._WARM_INFLIGHT.clear()

    def test_mesh_orientation_normalized(self):
        """上轴必须换算对：GLB(Y-up) → Z-up；STL 不重复转；默认不放倒；底面落回 Z=0。

        2026-09-23 用户报"模型躺着"。两条根因：
          a) _scale_to_target 里有无条件绕 X 转 90°（放倒，注释说是为打印精度）
          b) glTF 是 Y-up 而预览/打印是 Z-up
        另外这条测试还夹一条**结构断言**：今天我一度把类结构改坏（方法被吞进别的函数里，
        ast.parse 却照样通过），所以要断言 MeshOptimizer 的方法数没掉。
        """
        import ast
        import tempfile, shutil
        import trimesh
        from backend.mesh_optimizer import MeshOptimizer, y_up_to_z_up

        tree = ast.parse((ROOT / "backend" / "mesh_optimizer.py").read_text(encoding="utf-8"))
        methods = [m.name for m in next(n for n in tree.body
                                       if isinstance(n, ast.ClassDef) and n.name == "MeshOptimizer").body
                   if isinstance(m, ast.FunctionDef)]
        self.assertIn("_load_mesh", methods)
        self.assertIn("_scale_to_target", methods)
        self.assertGreaterEqual(len(methods), 12, f"类方法被吞掉了？只剩 {methods}")

        box = trimesh.creation.box(extents=(1, 5, 1))          # Y 方向最长 = "站着"
        self.assertAlmostEqual(y_up_to_z_up(box).extents[2], 5, places=5, msg="Y 应变成 Z")
        tmp = Path(tempfile.mkdtemp(prefix="dsh-orient-"))
        try:
            g, s = tmp / "a.glb", tmp / "a.stl"
            box.export(g); box.export(s)
            m = MeshOptimizer()
            self.assertAlmostEqual(m._load_mesh(g).extents[2], 5, places=5, msg="GLB 要转")
            self.assertAlmostEqual(m._load_mesh(s).extents[1], 5, places=5, msg="STL 不能转")
            import os
            os.environ.pop("LAY_DOWN_FOR_PRINT", None)
            scaled = m._scale_to_target(m._load_mesh(g), 100.0)
            self.assertGreater(scaled.extents[2], scaled.extents[1], "默认不该放倒")
            self.assertAlmostEqual(scaled.bounds[0][2], 0.0, places=5, msg="底面要在 Z=0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stl_regrounded_after_bottom_slice(self):
        """切掉底部 2mm 后要重新落地，否则切片软件里模型悬空 2mm"""
        import tempfile, shutil
        import trimesh
        from backend.mesh_optimizer import MeshOptimizer, preset_print_ready
        tmp = Path(tempfile.mkdtemp(prefix="dsh-ground-"))
        try:
            src = tmp / "cube.glb"
            trimesh.creation.box(extents=(30, 30, 60)).export(src)
            res = MeshOptimizer().optimize(src, preset_print_ready())
            out = Path(getattr(res, "output_path", "") or "")
            self.assertTrue(out.exists(), "要产出 STL")
            mesh = trimesh.load(str(out), force="mesh")
            self.assertAlmostEqual(mesh.bounds[0][2], 0.0, places=3,
                                   msg=f"底面应落回 Z=0，实际 {mesh.bounds[0][2]:.3f}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_autopilot_arm_cancel_due(self):
        """「自动继续」：布防 → 未到点不动作 → 到点该动作 → 用户介入即取消。

        宇凯 2026-09-23：图片就绪后 N 秒内没提修改需求就自动建模（多视图）。
        这条逻辑最容易出的错是**抢跑**（他没动就烧 GPU）和**误触发**（他已经说话了却还动手），
        所以布防/取消/到期三重判定都要卡死。
        """
        import time as _t
        from backend.conversation import ConversationManager
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-auto-"))
        orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            c = ConversationManager()
            st = pm.create_project()
            cand = pm.save_candidate_image(Path(st["path"]), b"x", 0)
            st.update(phase="selecting", selected=cand, candidates=[cand])
            pm.save_project(st)

            self.assertTrue(c.arm_autopilot(st, "图片已生成"), "配置开启时应布防成功")
            self.assertGreater(st["auto_deadline"], _t.time(), "到期时刻应在未来")
            self.assertEqual(st["auto_model"], c.autopilot_cfg()["model"], "应带上配置里的 3D 模型")
            # 落盘：刷新/重启后布防不能丢（pending_rotate 当初就是这么漏的）
            self.assertGreater(pm.load_project(st["folder"])["auto_deadline"], 0, "布防要落盘")

            ok, why = c.autopilot_due(st, now=st["auto_deadline"] - 1)
            self.assertFalse(ok, f"没到点不许动作：{why}")
            ok, why = c.autopilot_due(st, now=st["auto_deadline"] + 0.1)
            self.assertTrue(ok, f"到点该动作：{why}")

            # 用户介入 → 撤销
            self.assertTrue(c.cancel_autopilot(st, "用户发言"))
            ok, why = c.autopilot_due(st, now=st["auto_deadline"] + 99)
            self.assertFalse(ok, f"已取消就不许动作：{why}")

            # 阶段不对（模型已建完）也不许动作 —— 防止"建完又被自动建一次"
            self.assertTrue(c.arm_autopilot(st, "再布防"))
            st["phase"] = "stl"
            ok, why = c.autopilot_due(st, now=st["auto_deadline"] + 1)
            self.assertFalse(ok, f"已完成阶段不许动作：{why}")
            # 没有图片也不行
            st["phase"] = "selecting"; st["selected"] = ""; st["candidates"] = []
            self.assertTrue(c.arm_autopilot(st, "再布防"))
            ok, why = c.autopilot_due(st, now=st["auto_deadline"] + 1)
            self.assertFalse(ok, f"没图片不许动作：{why}")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_autopilot_fires_with_multiview_and_config_switch(self):
        """到点后要用**多视图**跑，且配置关掉就彻底不布防"""
        import time as _t
        from backend.conversation import ConversationManager
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-auto2-"))
        orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            c = ConversationManager()
            st = pm.create_project()
            cand = pm.save_candidate_image(Path(st["path"]), b"x", 0)
            st.update(phase="selecting", selected=cand, candidates=[cand])
            pm.save_project(st)

            seen = {}
            orig3d = c._do_generate_3d
            c._do_generate_3d = lambda s: (seen.update(model=s.get("auto_params", {}).get("model_3d")),
                                           {"project": s, "reply": "ok", "images": []})[1]
            try:
                c.arm_autopilot(st, "图片已生成")
                st["auto_deadline"] = _t.time() - 1          # 假装已到点
                res = c.autopilot_fire(st)
            finally:
                c._do_generate_3d = orig3d
            self.assertEqual(res.get("autopilot"), "fired")
            # 用哪个模型以配置为准（2026-09-23 实测 multiview 反而更糊 → 默认改成 hunyuan21）
            self.assertEqual(seen.get("model"), c.autopilot_cfg()["model"],
                             "自动继续要用配置里指定的 3D 模型")
            self.assertEqual(c.autopilot_cfg()["model"], "hunyuan21",
                             "默认应是单视图 hunyuan21（多视图实测更差）")
            self.assertEqual(st["auto_deadline"], 0, "触发后要清掉布防（否则每秒都想再跑）")

            # 配置关掉 → 不布防、不动手（这个类没有 self.config，打桩方法本身）
            orig_cfg = c.autopilot_cfg
            c.autopilot_cfg = lambda: {"enabled": False, "delay_sec": 5, "model": "multiview"}
            try:
                self.assertFalse(c.arm_autopilot(st, "x"), "关掉后不该布防")
                st["auto_deadline"] = _t.time() - 1
                ok, why = c.autopilot_due(st)
                self.assertFalse(ok, f"关掉后不该动作：{why}")
            finally:
                c.autopilot_cfg = orig_cfg
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_autopilot_ui_shapes_and_typing_cancel(self):
        """心跳的返回值形状、以及"一开始打字就取消"必须真的生效"""
        import time as _t
        import app as A
        from backend.conversation import ConversationManager
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-auto3-"))
        orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            c = ConversationManager()
            st = pm.create_project()
            cand = pm.save_candidate_image(Path(st["path"]), b"x", 0)
            st.update(phase="selecting", selected=cand, candidates=[cand])
            pm.save_project(st)

            # 未布防：心跳必须把**自己关掉**（active=False）—— 这是"界面一直在弹"的修复点：
            # 常驻 1 秒一跳时 Gradio 每秒显示"处理中"，用户看到的就是一直在弹 ✗
            out = list(A.on_autopilot_tick(dict(st)))
            self.assertEqual(len(out), 1, "未布防时心跳应产出恰好 1 段（用来关自己）")
            self.assertEqual(len(out[0]), 1 + len(A._update_ui(st)) + 1,
                             "心跳输出 = state + 14 UI + timer")
            timer_upd = out[0][-1]
            self.assertFalse(getattr(timer_upd, "active", True), "未布防时心跳必须关掉")
            self.assertEqual(len(A._update_ui(st)), 14, "_update_ui 必须是 14 个（13 UI + STL 文件卡片）")

            # 布防打开 / 取消关闭
            st["auto_deadline"] = 1e12
            self.assertTrue(getattr(A.arm_heartbeat(st), "active", False), "布防后心跳要开")
            st["auto_deadline"] = 0
            self.assertFalse(getattr(A.arm_heartbeat(st), "active", True), "未布防心跳要关")

            # 打字 → 取消（save=False：每个按键不落盘）
            orig_conv = A.conv
            A.conv = c
            try:
                c.arm_autopilot(st, "图片已生成")
                self.assertGreater(st["auto_deadline"], 0)
                A.on_typing(st)
                self.assertEqual(st["auto_deadline"], 0, "打字后布防必须清掉")
                self.assertIn("输入", st.get("auto_reason", ""), "要记下取消原因")
            finally:
                A.conv = orig_conv

            # 已到点时心跳要真的触发（generator：先提示、再出结果）
            A.conv = c
            try:
                c.arm_autopilot(st, "图片已生成")
                st["auto_deadline"] = _t.time() - 1
                fired = {}
                orig_fire = c.autopilot_fire
                c.autopilot_fire = lambda s: (fired.update(ok=True),
                                              {"project": s, "reply": "已自动建模", "images": []})[1]
                try:
                    chunks = list(A.on_autopilot_tick(st))
                finally:
                    c.autopilot_fire = orig_fire
                self.assertTrue(fired.get("ok"), "到点后心跳必须触发自动建模")
                self.assertGreaterEqual(len(chunks), 2, "至少两段：提示 + 结果")
                self.assertTrue(all(len(ch) == 1 + len(A._update_ui(st)) + 1 for ch in chunks),
                                "每段输出 = state + 11 UI + timer，元数必须对齐")
                self.assertTrue(all(not getattr(ch[-1], "active", True) for ch in chunks),
                                "跳完必须把自己关掉（否则又变成周期性事件、界面一直闪）")
            finally:
                A.conv = orig_conv
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_agent_cold_start_warms_before_planning(self):
        """冷启动保护：模型没就绪时，必须**先热身再提问**，否则启动后第一条消息必然超时。

        2026-09-23 端到端演示真实翻车：plan() 默认超时 75s，而微调服务首次调用要把
        14B + LoRA 加载进显存（>150s）→ TimeoutError → 静默回退关键词路径（用户感受＝变笨）。
        """
        from backend import agent as AG
        calls = {}
        o_ready, o_warm, o_call = AG.is_ready, AG.warm_up, AG._call_model
        try:
            AG.is_ready = lambda: False
            AG.warm_up = lambda timeout=300: (calls.update(warm=timeout), True)[1]
            AG._call_model = lambda ask, timeout=None, **kw: (
                calls.update(timeout=timeout),
                '{"understanding": ["测试"], "reply": "好", "actions": [{"name": "answer", "args": {}}]}')[1]
            r = AG.plan("我要一只卡通小熊", {})
            self.assertIsNotNone(r, "热身之后要能正常出计划")
            self.assertEqual(calls.get("warm"), 300, "要真的调 warm_up（上限 300s）")
            self.assertGreaterEqual(calls.get("timeout") or 0, 240,
                                    "冷启动这一轮的超时要放宽到 ≥240s（首轮含 KV 初始化）")

            # 已就绪 → 不该热身、超时也不动
            calls.clear()
            AG.is_ready = lambda: True
            r2 = AG.plan("我要一只卡通小熊", {})
            self.assertIsNotNone(r2)
            self.assertNotIn("warm", calls, "已就绪就不该再热身")
            self.assertEqual(calls.get("timeout"), 75, "就绪时保持默认超时")
        finally:
            AG.is_ready, AG.warm_up, AG._call_model = o_ready, o_warm, o_call

    def test_switch_project_tolerates_none(self):
        """下拉框刷新会传 None 进来，不许炸（2026-09-23 app 日志现场抓到 TypeError）"""
        import tempfile, shutil
        from backend.conversation import ConversationManager
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-sw-")); orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            c = ConversationManager()
            before = c.current_project.get("folder")
            for bad in (None, "", "   "):
                r = c.switch_project(bad)                 # 修复前这里是 TypeError
                self.assertIsInstance(r, dict, f"传 {bad!r} 不该崩")
                self.assertEqual(c.current_project.get("folder"), before,
                                 f"传 {bad!r} 时应保持当前项目不动")
            self.assertIsInstance(pm.load_project(None), dict, "load_project 也要兜住 None")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_autopilot_uses_fast_preset(self):
        """自动继续必须用快速档：写死标准档（octree 512）实测要十几分钟，自动路径等不起"""
        import time as _t
        from backend.conversation import ConversationManager
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-fast-")); orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            c = ConversationManager()
            self.assertEqual(c.autopilot_cfg()["quality"], "快速", "配置里默认应是快速档")
            st = pm.create_project()
            cand = pm.save_candidate_image(Path(st["path"]), b"x", 0)
            st.update(phase="selecting", selected=cand, candidates=[cand])
            pm.save_project(st)
            seen = {}
            orig3d = c._do_generate_3d
            c._do_generate_3d = lambda s: (seen.update(q=(s.get("auto_params") or {}).get("quality_3d")),
                                           {"project": s, "reply": "ok", "images": []})[1]
            try:
                c.arm_autopilot(st, "图片已生成")
                st["auto_deadline"] = _t.time() - 1
                c.autopilot_fire(st)
            finally:
                c._do_generate_3d = orig3d
            self.assertEqual(seen.get("q"), "快速", "自动继续要带快速档给 _do_generate_3d")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_timings_are_measured_and_sane(self):
        """耗时估算必须来自实测表，且自洽（中位 ≤ 上限、样本数 > 0）。

        2026-09-23 用户报"估算不准"：界面上 STL 写着"约 20-40 秒"，实测中位只有 2 秒 ✗
        （n=26）。这次把数值集中到 backend/timings，并在这里卡住"别写离谱数"。
        """
        from backend import timings as T
        for stage in ("image_512", "hd_1024", "3d_fast", "3d_std", "3d_multiview", "stl", "stl_big"):
            med, hi = T.estimate(stage)
            self.assertGreater(med, 0, stage)
            self.assertLessEqual(med, hi, f"{stage}: 中位不该大于上限")
            self.assertGreater(T.samples(stage), 0, f"{stage}: 必须有实测样本数")
            self.assertTrue(T.label(stage), stage)
        self.assertLessEqual(T.estimate("stl")[0], 10, "STL 实测中位就是 2 秒，别再写几十秒")
        self.assertEqual(T.stage_for_3d("multiview", "标准"), "3d_multiview")
        self.assertEqual(T.stage_for_3d("hunyuan21", "快速"), "3d_fast")
        self.assertEqual(T.stage_for_3d("hunyuan21", "标准"), "3d_std")
        self.assertEqual(T.stage_for_stl(94 * 1024 * 1024), "stl_big")
        self.assertEqual(T.stage_for_stl(1 * 1024 * 1024), "stl")
        self.assertEqual(T.human(2), "2 秒")
        self.assertIn("分", T.human(148))

    def test_ring_html_shows_measured_estimate(self):
        """环形进度条：必须带 SVG 环 + 动画，且文案里带上实测样本（不是空话）"""
        import app as A
        html_run = A.ring_html("3d_fast")
        self.assertIn("<svg", html_run)
        self.assertIn("keyframes ringrun", html_run, "要有动画（前端自己走，后端阻塞推不了）")
        self.assertIn("实测通常", html_run)
        self.assertIn("n=2", html_run, "3d_fast 的实测样本数是 2")
        html_done = A.ring_html("stl", 2.3, done=True)
        self.assertIn("实际 2 秒", html_done)
        self.assertNotIn("keyframes ringrun { from", html_done.split("</style>")[0].replace(
            "@keyframes ringrun { from", ""), "完成态不该还在转圈")
        self.assertIn("<div style='height:2px'></div>", A.ring_html(""), "空闲时留白")

    def test_stl_card_and_ring_do_not_leak_across_groups(self):
        """STL 卡片与进度环不能"串"到别的对话组（宇凯 2026-09-23 报"点新建对话会串"）。

        曾经的实现：stl_file 不在 ui_outputs 里 → 只有导出那一次写它 →
        换组/新建后它还挂着上一组的文件 ✗；而且 gr.File 即使 value=None 也会画空框，
        于是"这个框一直在那里" ✗。现在：_update_ui 按**当前组的 state["stl_path"]** 决定，
        没导出过就不画框。
        """
        import tempfile, shutil
        import app as A
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-card-")); orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            empty = pm.create_project()
            vals = A._update_ui(empty)
            self.assertEqual(len(vals), 14, "输出元数 = 14（7 个按钮 + 进度环 + STL 卡片）")
            card = vals[-1]
            # gr.update() 返回的是 **dict**，不是带 .visible 的对象（我在这上面栽过 ✗）
            self.assertFalse(card.get("visible", True), "没 STL 时卡片必须隐藏（不画空框）")
            self.assertIsNone(card.get("value"), "没 STL 时卡片值要清空")

            # 给这组塞一个真 STL 文件 → 卡片应显示并指向它
            stl = Path(empty["path"]) / "model.stl"
            stl.write_bytes(b"solid x / endsolid x")   # 内容不重要，只要文件真实存在
            empty["stl_path"] = str(stl)
            card2 = A._update_ui(empty)[-1]
            self.assertTrue(card2.get("visible", False), "本组有 STL → 卡片显示")
            self.assertEqual(str(card2.get("value")), str(stl))

            # 换到另一组（新建）→ 卡片必须回到隐藏，不能留着上一组的文件
            other = pm.create_project()
            card3 = A._update_ui(other)[-1]
            self.assertFalse(card3.get("visible", True), "新组没有 STL → 卡片隐藏（否则就是串）")
            self.assertIsNone(card3.get("value"), "新组卡片值必须清空")

            # 进度环同理：不传 ring 就是留白，不会显示上一组的"完成"
            self.assertIn("height:2px", A._update_ui(other)[12], "空闲时进度环留白（索引 12：加了 btn_adopt 后挪了一位）")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reference_upload_becomes_modeling_input(self):
        """上传参考图 → 直接成为本组建模输入（宇凯 2026-09-23："我要熊大，但图像只是给了一个卡通小熊"）。

        IP 角色任何纯文字生图模型都只能猜 ✗，可靠路线是"照这张图画"。
        以前界面上**没有上传入口** ✗ —— 这条守的就是这个入口通了：
        上传后 candidates/ 里有它、state["selected"] 指向它、selected.jpg 落盘、
        phase=selecting（按钮出「🧊 生成建模」）。
        """
        import tempfile, shutil
        import app as A
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-ref-")); orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            st = pm.create_project()
            # handler 只校验文件存在（不解析图像内容）→ 写几个字节即可，也别用转义字面量
            # （heredoc 会把转义序列变成真 NUL 字节，直接让整份测试文件语法错误 ✗）
            ref = Path(tmp) / "reference.png"
            ref.write_bytes(b"fake-image-bytes" * 4)
            self.assertTrue(ref.exists() and ref.stat().st_size > 40, "测试图要真存在")

            chunks = list(A.on_reference_uploaded(str(ref), st))
            out = chunks[-1]
            self.assertEqual(len(out), 15, "state + 14 个 UI 输出（state 在最前）")
            new = out[0]
            self.assertEqual(new.get("phase"), "selecting", "上传后应到 selecting（出「生成建模」按钮）")
            self.assertTrue(new.get("selected"), "必须设置 selected（_do_generate_3d 就取这个字段）")
            self.assertTrue(Path(new["selected"]).exists(), "selected 必须是真实文件")
            self.assertIn("candidates", new["selected"].replace("\\", "/"), "参考图要放进 candidates/")
            self.assertTrue((Path(new["path"]) / "selected.jpg").exists(), "selected.jpg 要落盘（刷新不丢）")
            self.assertTrue(any(c == new["selected"] for c in (new.get("candidates") or [])),
                            "candidates 里要有这张参考图")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reference_generation_builds_qwen21_workflow(self):
        """参考图生图必须走 Qwen-Image-2.1 的专用工作流（宇凯 2026-09-23 的"我要熊大"）。

        实测结论：IP 角色纯文字生图只能画属性（Z-Image 6B / FLUX.2 Klein 9B 都出"标准卡通棕熊" ✗），
        只有"照图"才可靠。这条守的就是那条路的结构别被改坏：
          · 节点必须是 TextEncodeQwenImage21（通用 CLIPTextEncode 会出噪声 ✗）
          · images 是 AUTOGROW，API 里必须是 list-of-pairs [[node,0]]（实测一次通过）
          · 编码器走 CLIPLoader(type=qwen_image) + qwen3vl_8b（2.1 的编码器就是 Qwen3-VL-8B）
        """
        import backend.comfy_client as cc
        cfg = {"name": "t", "checkpoint": "qwen_image_2.1_int8_convrot.safetensors",
               "clip_l": "qwen3vl_8b_int8_convrot.safetensors",
               "vae": "qwen_image_2.1_vae_bf16.safetensors", "clip_type": "qwen_image",
               "steps": 20, "cfg": 4.0}
        c = cc.ComfyUIClient.__new__(cc.ComfyUIClient)
        built = c._build_image_workflow({"model_config": cfg, "prompt": "x",
                                         "reference_image": "ref.png", "resolution_px": 768})
        self.assertIn("prompt", built, "构建器必须带外层（submit_workflow 读 data[\"prompt\"]）")
        wf = built["prompt"]
        types = [n["class_type"] for n in wf.values()]
        self.assertIn("TextEncodeQwenImage21", types)
        self.assertNotIn("CLIPTextEncode", types, "2.1 必须用专用编码节点")
        self.assertIn("UNETLoader", types)
        clip = [n for n in wf.values() if n["class_type"] == "CLIPLoader"][0]
        self.assertEqual(clip["inputs"]["type"], "qwen_image")
        self.assertIn("qwen3vl_8b", clip["inputs"]["clip_name"], "编码器必须是 Qwen3-VL-8B")
        enc = [n for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21"][0]
        self.assertEqual(enc["inputs"]["images"], [["4", 0]], "AUTOGROW 要写成 list-of-pairs")
        self.assertEqual(enc["inputs"]["resolution"], 768)
        # 分派条件本身也要守住：没有参考图就**不能**走 2.1 分支
        import inspect
        src = inspect.getsource(cc.ComfyUIClient._build_image_workflow)
        self.assertIn('if params.get("reference_image")', src,
                      "有参考图必须切到 Qwen-Image-2.1 分支（否则又回到「只能猜」的老路）")

    def test_ref_upload_survives_restart(self):
        """ref_upload 必须落盘并读回（否则重启后"照图生成"静默失效 —— AGENTS.md 记过的坑）"""
        import tempfile, shutil
        import backend.project_manager as pm
        tmp = Path(tempfile.mkdtemp(prefix="dsh-refp-")); orig = pm.PROJECTS_DIR
        pm.PROJECTS_DIR = tmp
        try:
            st = pm.create_project()
            ref = Path(tmp) / "r.png"; ref.write_bytes(b"x" * 80)
            st["ref_upload"] = str(ref)
            pm.save_project(st)
            back = pm.load_project(st["folder"])
            self.assertEqual(back.get("ref_upload"), str(ref), "重启后参考图必须还在")
        finally:
            pm.PROJECTS_DIR = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_web_ref_supports_character_image_query_expansion(self):
        """联网找参考图（agent 搜索）必须用多语查询扩写（宇凯 2026-09-24：IP 角色不能只搜中文名）。

        实测：Bing 对"熊大"返回的是腾讯视频截屏（动物"熊"），对"Briar Bear"才命中 IP。
        这是接口层限制，不是 web_ref 的 bug；web_ref 必须做查询扩写来最大化命中概率。
        """
        from backend import web_ref
        # 扩写器要有"熊大" -> ["Briar Bear", "熊大 熊出没 角色"]
        expanded = web_ref._expand_query("熊大")
        self.assertIn("熊大", expanded)
        self.assertIn("Briar Bear", expanded, "Bing 英文名才能命中 IP 角色图")
        self.assertEqual(web_ref._expand_query("海绵宝宝"), ["海绵宝宝", "SpongeBob SquarePants character"])
        # 表里没的角色也得有"角色"后缀启发式
        self.assertEqual(web_ref._expand_query("我的自定义角色"),
                         ["我的自定义角色", "我的自定义角色 角色 设定"])
        # 角色查询扩写表里至少覆盖熊出没主要角色
        for name in ("熊大", "熊二", "光头强"):
            self.assertIn(name, web_ref.CHARACTER_NAME_ALIASES,
                           f"{name} 应该能在英名表里查到（用户大概率会搜）")

    def test_log_summary_surfaces_failures(self):
        """自动拉起失败必须能看见：只打非 ok 行，全 ok 时保持安静"""
        import io
        import contextlib
        import backend.startup as SU
        c = SU.StartupChecker()
        c.results = [SU.CheckResult("微调 agent 服务", "fail", "启动失败 → 回退关键词路径"),
                     SU.CheckResult("目录结构", "ok", "全部就绪"),
                     SU.CheckResult("ComfyUI", "fixed", "已启动 8188")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            c.log_summary()
        out = buf.getvalue()
        self.assertIn("❌ 微调 agent 服务", out)
        self.assertIn("🔧 ComfyUI", out)
        self.assertNotIn("目录结构", out, "ok 行不该刷屏")
        buf2 = io.StringIO()
        c.results = [SU.CheckResult("目录结构", "ok", "全部就绪")]
        with contextlib.redirect_stdout(buf2):
            c.log_summary()
        self.assertEqual(buf2.getvalue(), "", "全 ok 时应静默")

    def test_autostart_env_clears_pythonpath(self):
        """PYTHONPATH 不清，venv 的 python 会去加载 Hermes venv 的 torch → DLL 失败（踩过）"""
        src = (ROOT / "backend" / "startup.py").read_text(encoding="utf-8")
        self.assertIn('env.pop("PYTHONPATH", None)', src)
        self.assertIn("TUNED_VENV_PYTHON", src)
        self.assertIn("serve_agent.py", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
