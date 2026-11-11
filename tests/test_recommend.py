# -*- coding: utf-8 -*-
"""配置向导的推荐逻辑：给定硬件和已有资产，推荐得对不对。

不探测真机，全部用构造的 Hardware / assets —— 这样 8G 卡、16G 内存的机器也能在测试里跑。
"""
import unittest

from backend.backends.recommend import Hardware, build_profiles, pick_default, summarize_assets


def hw(vram, ram=32, disk=200):
    return Hardware(vram_gb=vram, vram_free_gb=vram * 0.9, ram_gb=ram,
                    disk_free_gb=disk, gpu_name=f"fake-{vram}G")


def assets(comfy=False, comfy3d=False, diffusers=False, hy3d=False, chat=False):
    return {"comfyui": comfy, "comfyui_3d": comfy3d, "comfyui_models": ["a"] * 12 if comfy else [],
            "diffusers": diffusers, "diffusers_models": ["m"] if diffusers else [],
            "hy3dgen": hy3d, "chat": chat}


class ProfileTest(unittest.TestCase):
    def test_full_comfy_machine_prefers_zero_download(self):
        """已经有 ComfyUI + 3D-Pack 的机器，第一推荐应该是"零下载"。"""
        ps = build_profiles(hw(32), assets(comfy=True, comfy3d=True, chat=True))
        self.assertEqual(ps[0].key, "reuse")
        self.assertEqual(ps[0].downloads, [])
        self.assertIs(pick_default(ps), ps[0])

    def test_8g_laptop(self):
        """8G 显存 + 16G 内存：minimal 能装（紧），standard 装不下。"""
        ps = build_profiles(hw(8, ram=16), assets())
        keys = {p.key: p for p in ps}
        self.assertIn("minimal", keys)
        # 内存 16G，模型要 10.8G，留 5G 给系统 → 判定卡边缘、不该标 fits
        self.assertFalse(keys["minimal"].fits, "16G 内存不该被判为舒适可用")
        self.assertIn("内存", keys["minimal"].reason)
        # 8G 显存连 standard 都不进列表（阈值 12G）
        self.assertNotIn("standard", keys)

    def test_8g_with_32g_ram_is_ok(self):
        ps = build_profiles(hw(8, ram=32), assets())
        keys = {p.key: p for p in ps}
        self.assertTrue(keys["minimal"].fits, "8G 显存 + 32G 内存应该可用")

    def test_12g_gets_standard(self):
        ps = build_profiles(hw(12, ram=32), assets())
        keys = {p.key: p for p in ps}
        self.assertIn("standard", keys)
        self.assertTrue(keys["standard"].fits)

    def test_24g_gets_comfy_full(self):
        ps = build_profiles(hw(24, ram=64), assets(comfy=True, comfy3d=True))
        keys = {p.key: p for p in ps}
        self.assertIn("full-comfy", keys)

    def test_no_gpu(self):
        ps = build_profiles(hw(0, ram=16), assets())
        keys = {p.key: p for p in ps}
        self.assertIn("cpu-only", keys)
        self.assertFalse(keys["cpu-only"].fits)

    def test_disk_too_small_blocks_standard(self):
        ps = build_profiles(hw(24, ram=64, disk=20), assets())
        std = next((p for p in ps if p.key == "standard"), None)
        self.assertIsNotNone(std)
        self.assertFalse(std.fits, "磁盘只有 20G，30.6G 的模型不该判可用")

    def test_profiles_have_config_patch(self):
        """每个方案都得给出可执行的 config 片段，不能只告诉用户"你该装啥"。"""
        for p in build_profiles(hw(32, ram=64), assets(comfy=True, comfy3d=True)):
            self.assertTrue(p.config_patch, f"{p.key} 缺 config_patch")
            self.assertIn("backend", p.config_patch)

    def test_download_commands_are_runnable(self):
        ps = build_profiles(hw(32, ram=64), assets())
        for p in ps:
            for d in p.downloads:
                cmd = d.command()
                self.assertIn("tools/fetch_model.py", cmd)
                self.assertIn("--repo", cmd)
                self.assertIn("--dest", cmd)

    def test_summarize_assets_ignores_dead_comfyui(self):
        """8189 没起、8188 起了 —— 不能因为死实例把能力判没了。"""
        from backend.backends.detect import Framework
        fws = [
            Framework("comfyui", "ComfyUI", alive=True, caps={"image", "mesh"}, models=["x"]),
            Framework("comfyui-8189", "ComfyUI (8189)", alive=False, caps=set()),
        ]
        a = summarize_assets(fws)
        self.assertTrue(a["comfyui"])
        self.assertTrue(a["comfyui_3d"])


if __name__ == "__main__":
    unittest.main()
