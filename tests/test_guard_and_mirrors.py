# -*- coding: utf-8 -*-
"""并发防护与内置 GitHub 加速的测试（2026-11-28）。"""
from __future__ import annotations

import json
import pathlib


# ── gpu_guard ───────────────────────────────────────────────

def test_guard_serializes_heavy_jobs():
    """第二个重活进来时，第一个必须让出（注册的 releaser 被调用）。"""
    from backend import gpu_guard

    freed = []

    def releaser():
        freed.append(gpu_guard.status()["holder"])

    # 清空历史注册，避免污染其它测试
    gpu_guard._RELEASERS.clear()
    gpu_guard.register_releaser("fake_comfy", releaser)

    with gpu_guard.hold("image"):
        assert gpu_guard.status()["holder"] == "image"
        with gpu_guard.hold("mesh"):
            assert gpu_guard.status()["holder"] == "mesh"
            assert freed, "第二个重活前应调用 releaser 释放显存"
        assert gpu_guard.status()["holder"] is None
    assert gpu_guard.status()["holder"] is None


def test_guard_status_and_busy_message():
    from backend import gpu_guard

    gpu_guard._RELEASERS.clear()
    assert gpu_guard.busy_message() == ""
    with gpu_guard.hold("paint"):
        msg = gpu_guard.busy_message()
        assert "paint" in msg and "占着" in msg


def test_pipeline_methods_are_guarded():
    """出图/建模/多视图必须被包上（否则并发时仍会 OOM）。"""
    from backend.pipeline import Pipeline

    for name in ("generate_images", "generate_3d", "generate_3d_multiview"):
        assert getattr(getattr(Pipeline, name), "_ff_guarded", False), name


def test_capability_functions_are_guarded():
    from backend import capability_ui

    for name in ("do_rig", "do_paint", "do_refine", "do_multiview", "do_motion"):
        fn = getattr(capability_ui, name)
        assert getattr(fn, "_ff_guarded", False), name


# ── mirrors（内置 GitHub 加速）────────────────────────────────

def test_mirrors_prefix_only_github():
    from backend import mirrors

    out = mirrors.gh_url("https://github.com/a/b.zip")
    assert out != "https://github.com/a/b.zip"
    assert out.endswith("https://github.com/a/b.zip")
    # 非 github 不改
    assert mirrors.gh_url("https://hf-mirror.com/x/y") == "https://hf-mirror.com/x/y"


def test_mirrors_order_and_direct_fallback():
    from backend import mirrors

    urls = mirrors.gh_urls("https://raw.githubusercontent.com/a/b/c.txt")
    assert urls[-1].startswith("https://raw.githubusercontent.com/")
    assert len(urls) >= 2
    assert len(set(urls)) == len(urls)


def test_mirrors_config_override(tmp_path, monkeypatch):
    from backend import mirrors

    (tmp_path / "config.json").write_text(
        json.dumps({"network": {"github_mirrors": ["https://example-mirror/"]}}), encoding="utf-8")
    monkeypatch.setattr(mirrors, "ROOT", tmp_path)
    assert mirrors.mirrors() == ["https://example-mirror/"]
    assert mirrors.gh_url("https://github.com/a").startswith("https://example-mirror/")


def test_mirrors_env_override(monkeypatch):
    from backend import mirrors

    monkeypatch.setenv("FORGEFORM_GH_MIRROR", "https://env-mirror")
    assert mirrors.mirrors() == ["https://env-mirror/"]


def test_pip_args_shape():
    from backend import mirrors

    a = mirrors.pip_args()
    assert a[0] == "-i" and a[1].startswith("https://")
    assert "--trusted-host" in a

# ── 提示词扩写兜底 ────────────────────────────────────────

def test_extract_size_mm():
    from backend.prompt_enhance import extract_size_mm

    assert extract_size_mm("做个 90mm 的杯子") == 90.0
    assert extract_size_mm("9 厘米高") == 90.0
    assert extract_size_mm("1.2cm 的扣") == 12.0
    assert extract_size_mm("随便做个东西") is None


def test_offline_enhance_sections():
    from backend.prompt_enhance import offline_enhance

    s = offline_enhance("做个马克杯 90mm")
    assert "single object" in s["image"]
    assert "90mm" in s["image"]
    assert "水密" in s["three_d"] and "壁厚" in s["three_d"]
    assert offline_enhance("")["image"] == ""


def test_pipeline_enhance_never_silent(monkeypatch):
    """没有 LLM 时必须走规则兜底，不能静默返回原文。"""
    import backend.pipeline as pl

    monkeypatch.setattr(pl, "llm_enhance", lambda p: p)  # 模拟没有 LLM
    pipe = pl.Pipeline(r"D:\ForgeForm-oss\config.json")
    out = pipe.enhance_prompt("做个笔筒")
    assert out != "做个笔筒"
    assert "single object" in out
