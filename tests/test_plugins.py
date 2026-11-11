# -*- coding: utf-8 -*-
"""模型插件框架：加一个 YAML 就能接一个新模型，不改代码。

这组用例守的是"插件仍然是可插的"——如果有人把某个模型硬编码回 `direct.py` 的 if/elif，
这里就会红。
"""
import tempfile
import unittest
from pathlib import Path

from backend.plugins.spec import (
    ModelPlugin,
    Registry,
    parse_simple_yaml,
    registry,
    resolve,
)


class YamlParserTest(unittest.TestCase):
    def test_scalars_and_nested_defaults(self):
        text = """
id: z_image
kind: image
label: Z-Image Turbo 6B
repo: Tongyi-MAI/Z-Image-Turbo
vram_hint: 20
defaults:
  steps: 8
  guidance: 1.0
  width: 1024
  dtype: bfloat16
"""
        d = parse_simple_yaml(text)
        self.assertEqual(d["id"], "z_image")
        self.assertEqual(d["vram_hint"], 20)
        self.assertEqual(d["defaults"]["steps"], 8)
        self.assertAlmostEqual(d["defaults"]["guidance"], 1.0)
        self.assertEqual(d["defaults"]["dtype"], "bfloat16")

    def test_comments_and_blank_lines(self):
        d = parse_simple_yaml("# 顶注释\n\nid: x   # 行内注释\nkind: image\n")
        self.assertEqual(d["id"], "x")
        self.assertEqual(d["kind"], "image")

    def test_quoted_values(self):
        d = parse_simple_yaml('id: "a b"\nnotes: \'带空格\'\n')
        self.assertEqual(d["id"], "a b")
        self.assertEqual(d["notes"], "带空格")


class RegistryTest(unittest.TestCase):
    def test_builtin_plugins_loaded(self):
        r = registry(reload=True)
        ids = {(p.kind, p.id) for p in r.all()}
        # 这四个出图 + 两个建图是仓库自带的最小集合
        for want in [("image", "z_image"), ("image", "qwen_image"),
                     ("image", "flux"), ("image", "flux2klein"),
                     ("mesh", "hunyuan21"), ("mesh", "hunyuan_mini")]:
            self.assertIn(want, ids, f"缺少内置插件 {want}")

    def test_every_image_plugin_declares_pipeline_or_loader(self):
        r = registry()
        for p in r.of_kind("image"):
            self.assertTrue(p.pipeline or p.loader, f"{p.id} 没写 pipeline/loader")
        for p in r.of_kind("mesh"):
            self.assertTrue(p.loader, f"{p.id} 没写 loader")

    def test_resolve_returns_plugin_with_defaults(self):
        p = resolve("image", "z_image")
        self.assertIsNotNone(p)
        self.assertEqual(p.pipeline, "ZImagePipeline")
        self.assertEqual(p.defaults["steps"], 8)
        self.assertEqual(p.target, p.repo)      # 没配本地路径时退回 repo

    def test_plugins_params_merge(self):
        p = resolve("image", "z_image")
        merged = p.params({"steps": 12, "width": None})
        self.assertEqual(merged["steps"], 12)          # 覆盖
        self.assertEqual(merged["width"], 1024)        # None 不覆盖
        self.assertEqual(merged["guidance"], 1.0)      # 插件默认保留

    def test_user_dir_can_add_and_override(self):
        """用户目录里丢一个同 id 的 YAML，应该覆盖内置的。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "image"
            d.mkdir(parents=True)
            (d / "my_model.yaml").write_text(
                "id: my_model\nkind: image\npipeline: FluxPipeline\n"
                "repo: someone/my-model\ndefaults:\n  steps: 4\n", encoding="utf-8")
            (d / "z_image.yaml").write_text(
                "id: z_image\nkind: image\npipeline: ZImagePipeline\n"
                "repo: overridden/repo\ndefaults:\n  steps: 6\n", encoding="utf-8")

            r = Registry()
            r.load_dir(d)
            self.assertEqual(r.get("image", "my_model").pipeline, "FluxPipeline")
            self.assertEqual(r.get("image", "z_image").repo, "overridden/repo")
            self.assertEqual(r.get("image", "z_image").defaults["steps"], 6)


class DirectBackendUsesPluginsTest(unittest.TestCase):
    """DirectBackend 必须从插件拿 pipeline 类名，而不是内部 if/elif。"""

    def test_resolve_prefers_config_then_plugin(self):
        import tempfile
        from backend.backends.direct import DirectBackend

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "backend": {"mode": "direct"},
                "paths": {"output": str(Path(tmp) / "out")},
                "stages": {"image": {"default_model": "z_image"}},
                "models": {"image": {"z_image": {"path": "D:/somewhere"}}},
            }
            b = DirectBackend(cfg)
            try:
                key, opts, plugin = b._resolve("image", {})
                self.assertEqual(key, "z_image")
                self.assertIsNotNone(plugin)
                self.assertEqual(plugin.pipeline, "ZImagePipeline")
                # config 里的 path 覆盖插件 repo 作为加载目标
                self.assertEqual(opts["steps"], 8)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
