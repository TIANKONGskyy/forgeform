# -*- coding: utf-8 -*-
"""DirectBackend（不用 ComfyUI 的后端）的协议契约。

不加载任何真模型：把 `_run_image` / `_run_3d` 换成写个占位文件的假实现，
验证的是**协议形状**——

    build_workflow → submit_workflow → wait_for_completion → get_result_* → URL 可下载

这四条对上了，`pipeline.py` 就完全不知道后端换了（它只会 requests.get 那个 URL）。
"""
import tempfile
import unittest
from pathlib import Path

from backend.backends import MODE_DIRECT, backend_mode, make_backend
from backend.backends.direct import DirectBackend

CFG = {
    "backend": {"mode": "direct"},
    "paths": {"output": ""},          # 测试里替换成临时目录
    "stages": {"image": {"default_model": "z_image"},
               "3d": {"default_model": "mini"}},
    "models": {
        "image": {"z_image": {"backend": "z_image", "path": "dummy-ref"}},
        "3d": {"mini": {"backend": "hy3dgen", "path": "dummy-3d"}},
    },
}


class BackendSelectionTest(unittest.TestCase):
    def test_default_is_comfyui(self):
        self.assertEqual(backend_mode({}), "comfyui")
        self.assertEqual(backend_mode({"backend": {}}), "comfyui")

    def test_explicit_direct(self):
        self.assertEqual(backend_mode({"backend": {"mode": MODE_DIRECT}}), MODE_DIRECT)
        # 也接受直接把 backend 写成字符串
        self.assertEqual(backend_mode({"backend": "direct"}), MODE_DIRECT)

    def test_make_backend_returns_comfy_by_default(self):
        from backend.comfy_client import ComfyUIClient
        b = make_backend({"paths": {"comfyui": ".", "output": "."}})
        self.assertIsInstance(b, ComfyUIClient)


class DirectBackendProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = dict(CFG)
        cfg["paths"] = {"output": str(root / "outputs")}
        self.b = DirectBackend(cfg)
        self.root = root

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def _fake_image(self, params):
        p = self.b.out_root / "images" / "fake.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        return [p]

    def test_image_flow_end_to_end(self):
        """整条 ComfyUI 协议要能无感地被伪造。"""
        self.b._run_image = self._fake_image      # 不加载真模型

        wf = self.b.build_workflow("image", {"prompt": "x", "steps": 8})
        self.assertTrue(wf.get("__direct__"))
        self.assertEqual(wf["stage"], "image")

        pid = self.b.submit_workflow(wf, stage="image")
        self.assertIsInstance(pid, str)
        self.assertGreater(len(pid), 8)

        res = self.b.wait_for_completion(pid, timeout=10)
        self.assertIn("outputs", res)

        url = self.b.get_result_image(res.get("outputs", {}), stage="image")
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        self.assertIn("/view?filename=", url)

        # pipeline 就是这样把结果拿回去的
        import requests
        r = requests.get(url, timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 8)

    def test_upload_image_passthrough(self):
        p = self.root / "in.png"
        p.write_bytes(b"x")
        self.assertEqual(self.b.upload_image(str(p), stage="image"), str(p))

    def test_missing_image_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.b.upload_image(str(self.root / "nope.png"))

    def test_video_is_explicitly_unsupported(self):
        """视频不能静默降级成空结果 —— 必须报清楚原因。"""
        wf = self.b.build_workflow("video", {"prompt": "x"})
        with self.assertRaises(NotImplementedError) as cm:
            self.b.submit_workflow(wf, stage="video")
        self.assertIn("ComfyUI", str(cm.exception))

    def test_is_running_true(self):
        """is_running() 的语义就是"torch 能否 import"。

        CI 不装 torch（它有几个 GB，测试用不到），所以这里断言的是
        "结果与实际环境一致"，而不是硬要求 True —— 硬要求会让 CI 永远红。
        """
        try:
            import torch  # noqa: F401
            has_torch = True
        except Exception:
            has_torch = False
        self.assertEqual(self.b.is_running(), has_torch)

    def test_model_pick_falls_back_to_first(self):
        key, cfg = self.b._pick("image", {"model_key": "不存在的key"})
        self.assertEqual(key, "z_image")
        self.assertEqual(cfg["backend"], "z_image")


if __name__ == "__main__":
    unittest.main()
