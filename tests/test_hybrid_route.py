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

    def test_selected_model_controls_upload_build_and_submit(self):
        from unittest.mock import Mock
        direct, comfy = Mock(), Mock()
        direct.build_workflow.return_value = {"params": {}}
        comfy.build_workflow.return_value = {"prompt": {}}
        direct.submit_workflow.return_value = "direct-job"
        comfy.submit_workflow.return_value = "comfy-job"
        direct.upload_image.return_value = "local.png"
        comfy.upload_image.return_value = "uploaded.png"
        self.b._direct, self.b._comfy = direct, comfy
        self.b.plan = {"image": "direct", "mesh": "direct"}
        selected = {"checkpoint": "model.safetensors"}
        self.assertEqual(self.b.upload_image("local.png", "image", selected), "uploaded.png")
        wf = self.b.build_workflow("image", {"model_config": selected, "model_key": "chosen"})
        self.assertEqual(self.b.submit_workflow(wf, stage="image"), "comfy-job")
        comfy.build_workflow.assert_called_once()
        comfy.submit_workflow.assert_called_once()
        direct.submit_workflow.assert_not_called()

        wf = self.b.build_workflow("3d", {"model_config": {"path": "model"}, "model_key": "mini"})
        self.assertEqual(self.b.submit_workflow(wf, stage="3d"), "direct-job")
        self.assertEqual(self.b.pick("3d", {"backend": "hy21"}), comfy)

    def test_report_shape(self):
        rep = self.b.report()
        self.assertIsInstance(rep, list)
        for item in rep:
            self.assertIn("key", item)
            self.assertIn("alive", item)
            self.assertIn("caps", item)


class SharedQueueSafetyTest(unittest.TestCase):
    def test_release_only_when_queue_is_confirmed_idle(self):
        from unittest.mock import Mock, patch
        from backend.pipeline import Pipeline
        pipe = Pipeline.__new__(Pipeline)
        pipe.config = {"comfyui_ports": {"image": 8188, "media": 8188}}
        pipe._log = Mock()
        response = Mock()
        with patch('backend.pipeline.requests.get', return_value=response), \
             patch('backend.pipeline.requests.post') as post, patch('backend.pipeline.time.sleep'):
            response.json.return_value = {'queue_running': [[1]], 'queue_pending': []}
            pipe._free_vram()
            post.assert_not_called()
            response.json.return_value = {'queue_running': [], 'queue_pending': []}
            pipe._free_vram()
            post.assert_called_once()


class DiffusersDetectionTest(unittest.TestCase):
    def test_hunyuan_checkpoints_are_3d_not_image_weights(self):
        import tempfile
        from pathlib import Path
        from backend.backends.detect import _scan_model_dir, _SCAN_CACHE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'custom_nodes/ComfyUI-3D-Pack-main').mkdir(parents=True)
            shape = root / 'models/checkpoints/tencent/Hunyuan3D-2_1'
            for name in ('hunyuan3d-dit-v2-1', 'hunyuan3d-vae-v2-1'):
                part = shape / name; part.mkdir(parents=True)
                (part / 'model.fp16.ckpt').write_bytes(b'weights')
            result = _scan_model_dir(root)
            self.assertIn('Hunyuan3D-2.1', result['mesh'])
            self.assertEqual(result['image'], [])
            _SCAN_CACHE.pop(str(root.resolve()).lower(), None)

    def test_missing_component_and_shard_are_not_usable(self):
        import json
        import tempfile
        from pathlib import Path
        from backend.backends.detect import _diffusers_complete
        index = {'_class_name': 'ExamplePipeline', 'unet': ['diffusers', 'UNet'],
                 'vae': ['diffusers', 'Autoencoder'], 'tokenizer': ['transformers', 'Tokenizer']}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('unet', 'vae', 'tokenizer'):
                (root / name).mkdir()
            (root / 'tokenizer' / 'tokenizer.json').write_text('{}')
            (root / 'vae' / 'model.safetensors').write_bytes(b'v')
            (root / 'unet' / 'model-00001-of-00002.safetensors').write_bytes(b'a')
            (root / 'unet' / 'model.safetensors.index.json').write_text(json.dumps(
                {'weight_map': {'a': 'model-00001-of-00002.safetensors', 'b': 'model-00002-of-00002.safetensors'}}))
            self.assertFalse(_diffusers_complete(root, index)[0])
            (root / 'unet' / 'model-00002-of-00002.safetensors').write_bytes(b'b')
            self.assertTrue(_diffusers_complete(root, index)[0])
            (root / 'vae' / 'model.safetensors').unlink()
            self.assertFalse(_diffusers_complete(root, index)[0])


if __name__ == "__main__":
    unittest.main()
