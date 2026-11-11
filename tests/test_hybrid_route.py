# -*- coding: utf-8 -*-
"""自动适配：探测 → 路由。不依赖任何真实服务（全部用假的 Framework 样本）。"""
import unittest

from backend.backends import MODE_COMFYUI, MODE_DIRECT, MODE_HYBRID, backend_mode, make_backend
from backend.backends.detect import Framework, recommend


def fw(key, alive=True, caps=(), url="http://127.0.0.1:1", models=()):
    return Framework(key=key, label=key, alive=alive, url=url,
                     caps=set(caps), models=list(models))


class RecommendTest(unittest.TestCase):
    def test_comfyui_wins_for_image_and_mesh(self):
        plan = recommend([
            fw("comfyui", caps=("image", "mesh")),
            fw("diffusers", caps=("image",)),
            fw("hy3dgen", caps=("mesh",)),
            fw("ollama", caps=("text",)),
        ])
        self.assertEqual(plan["image"], "comfyui")
        self.assertEqual(plan["mesh"], "comfyui")
        self.assertEqual(plan["text"], "ollama")

    def test_falls_back_to_direct_when_no_comfyui(self):
        plan = recommend([
            fw("comfyui", alive=False, caps=("image", "mesh")),
            fw("diffusers", caps=("image",)),
            fw("hy3dgen", caps=("mesh",)),
        ])
        self.assertEqual(plan["image"], "direct")
        self.assertEqual(plan["mesh"], "direct")

    def test_comfyui_alive_but_no_image_weights(self):
        """ComfyUI 活着但没出图权重 → 出图该走直连，而不是硬塞给 ComfyUI。"""
        plan = recommend([
            fw("comfyui", caps=("mesh",)),          # 只有 3D
            fw("diffusers", caps=("image",)),
        ])
        self.assertEqual(plan["image"], "direct")
        self.assertEqual(plan["mesh"], "comfyui")

    def test_chat_prefers_ollama_then_openai_compat(self):
        self.assertEqual(recommend([fw("ollama", caps=("text",))])["text"], "ollama")
        self.assertEqual(recommend([fw("llama-127.0.0.1", caps=("text",))])["text"], "openai")
        self.assertEqual(recommend([fw("vllm-127.0.0.1", caps=("text",))])["text"], "openai")
        self.assertEqual(recommend([fw("lmstudio-127.0.0.1", caps=("text",))])["text"], "openai")

    def test_nothing_alive(self):
        plan = recommend([fw("comfyui", alive=False), fw("ollama", alive=False)])
        self.assertEqual(plan["text"], "off")
        self.assertNotIn("image", plan)      # 上游会给出明确的"没有后端"错误


class ModeTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(backend_mode({"backend": {"mode": "auto"}}), MODE_HYBRID)
        self.assertEqual(backend_mode({"backend": {"mode": "hybrid"}}), MODE_HYBRID)
        self.assertEqual(backend_mode({"backend": {"mode": "builtin"}}), MODE_DIRECT)
        self.assertEqual(backend_mode({"backend": "native"}), MODE_DIRECT)
        self.assertEqual(backend_mode({"backend": {"mode": "comfy"}}), MODE_COMFYUI)
        self.assertEqual(backend_mode({}), MODE_COMFYUI)      # 默认不变
        self.assertEqual(backend_mode({"backend": {"mode": "瞎写"}}), MODE_COMFYUI)

    def test_make_backend_types(self):
        from backend.backends.direct import DirectBackend
        from backend.backends.hybrid import HybridBackend
        from backend.comfy_client import ComfyUIClient

        base = {"paths": {"comfyui": ".", "output": "."}}
        self.assertIsInstance(make_backend({**base, "backend": {"mode": "comfyui"}}), ComfyUIClient)
        self.assertIsInstance(make_backend({**base, "backend": {"mode": "direct"}}), DirectBackend)


class HybridRoutingTest(unittest.TestCase):
    """HybridBackend 的路由表：job 提交给谁，回收时就还给谁。"""

    def setUp(self):
        import tempfile
        from backend.backends.hybrid import HybridBackend

        self.tmp = tempfile.TemporaryDirectory()
        cfg = {"backend": {"mode": "auto", "probe_hosts": ["127.0.0.1"]},
               "paths": {"comfyui": ".", "output": str(self.tmp.name)}}
        self.b = HybridBackend(cfg)
        # 探测结果按测试需要覆盖，别依赖机器上真跑着什么
        self.b.plan = {"image": "direct", "mesh": "direct", "text": "off"}

    def tearDown(self):
        try:
            self.b.close()
        finally:
            self.tmp.cleanup()

    def test_routes_by_stage(self):
        called = {}

        class FakeBackend:
            def __init__(self, tag):
                self.tag = tag

            def build_workflow(self, stage, params):
                return {"stage": stage, "params": params, "__stage__": stage, "tag": self.tag}

            def submit_workflow(self, wf, model_config=None, stage=None):
                called["submit"] = self.tag
                return f"{self.tag}-{stage}"

            def wait_for_completion(self, pid, timeout=10, progress_callback=None, thermal_guard=False):
                return {"outputs": {"__files__": [f"/tmp/{pid}"]}, "who": self.tag}

            def get_result_image(self, outputs, stage=None):
                return "http://x"

            def get_result_video(self, outputs, stage=None):
                return ""

            def get_result_3d(self, outputs):
                return ""

            def interrupt(self):
                return True

            def is_running(self):
                return True

            def get_gpu_temp(self, idx=None):
                return 60

            def upload_image(self, p, stage="image"):
                return p

        d = FakeBackend("direct")
        self.b._direct = d

        wf = self.b.build_workflow("image", {"prompt": "x"})
        pid = self.b.submit_workflow(wf, stage="image")
        self.assertEqual(called["submit"], "direct")
        res = self.b.wait_for_completion(pid)
        self.assertEqual(res["who"], "direct")
        # 完成后路由表要清掉，避免长期占内存
        self.assertNotIn(pid, self.b._route)

    def test_unknown_job_raises(self):
        with self.assertRaises(RuntimeError):
            self.b.wait_for_completion("不存在的pid")

    def test_report_shape(self):
        rep = self.b.report()
        self.assertIsInstance(rep, list)
        for item in rep:
            self.assertIn("key", item)
            self.assertIn("alive", item)
            self.assertIn("caps", item)


if __name__ == "__main__":
    unittest.main()
