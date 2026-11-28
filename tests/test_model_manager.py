# -*- coding: utf-8 -*-
import json
import pathlib

import pytest


def _setup(tmp_path, monkeypatch):
    from backend import model_manager as mm

    comfy = tmp_path / "comfy"
    (comfy / "models" / "diffusion_models").mkdir(parents=True)
    (comfy / "models" / "diffusion_models" / "z_image_turbo_int8_convrot.safetensors").write_bytes(b"x" * 1024)
    hy = tmp_path / "hy3dgen"
    (hy / "hunyuan3d-2mini").mkdir(parents=True)
    (hy / "hunyuan3d-2mini" / "config.json").write_text("{}", encoding="utf-8")
    cfg = {
        "paths": {"comfyui": str(comfy)},
        "models": {
            "image": {"z_image": {"name": "Z", "checkpoint": "z_image_turbo_int8_convrot.safetensors"},
                       "flux": {"name": "F"}},
            "3d": {"hunyuan_mini": {"name": "Mini", "path": str(hy / "hunyuan3d-2mini")}},
            "active": {},
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mm, "ROOT", tmp_path)
    monkeypatch.setattr(mm, "COMFY", comfy)
    monkeypatch.setattr(mm, "HY3DGEN", hy)
    return mm


def test_list_models_marks_installed(tmp_path, monkeypatch):
    mm = _setup(tmp_path, monkeypatch)
    rows = {r["key"]: r for r in mm.list_models()}
    assert rows["z_image"]["installed"] is True
    assert rows["flux"]["installed"] is False
    assert rows["hunyuan_mini"]["installed"] is True


def test_switch_writes_config(tmp_path, monkeypatch):
    mm = _setup(tmp_path, monkeypatch)
    mm.switch("image", "z_image")
    assert mm.active("image") == "z_image"
    with pytest.raises(KeyError):
        mm.switch("image", "不存在")


def test_delete_removes_only_its_files(tmp_path, monkeypatch):
    mm = _setup(tmp_path, monkeypatch)
    cfg_before = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    r = mm.delete("image", "z_image")
    assert r["ok"] is True and r["freed_gb"] >= 0
    rows = {x["key"]: x for x in mm.list_models()}
    assert rows["z_image"]["installed"] is False
    # 配置里的其它模型没被动
    cfg_after = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert set(cfg_after["models"]["image"]) == set(cfg_before["models"]["image"])


def test_delete_3d_removes_dir(tmp_path, monkeypatch):
    mm = _setup(tmp_path, monkeypatch)
    r = mm.delete("3d", "hunyuan_mini")
    assert r["ok"] is True
    assert not (tmp_path / "hy3dgen" / "hunyuan3d-2mini").exists()


def test_usage_reports(tmp_path, monkeypatch):
    mm = _setup(tmp_path, monkeypatch)
    u = mm.usage()
    assert u["total"] >= 2 and u["installed"] >= 1 and u["free_gb"] > 0

def test_weight_files_uses_checkpoint_field(tmp_path, monkeypatch):
    """配置里自带 checkpoint（用户自己的模型）也要能被列出/删除。"""
    from backend import model_manager as mm
    import json

    comfy = tmp_path / "comfy"
    (comfy / "models" / "diffusion_models").mkdir(parents=True)
    f = comfy / "models" / "diffusion_models" / "my_own.safetensors"
    f.write_bytes(b"x" * 512)
    cfg = {"paths": {"comfyui": str(comfy)},
           "models": {"image": {"mine": {"name": "我的模型", "checkpoint": "my_own.safetensors"}}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mm, "ROOT", tmp_path)
    monkeypatch.setattr(mm, "COMFY", comfy)

    rows = {r["key"]: r for r in mm.list_models()}
    assert rows["mine"]["installed"] is True
    r = mm.delete("image", "mine")
    assert r["ok"] is True
    assert not f.exists()


def test_partial_files_not_counted_as_installed(tmp_path, monkeypatch):
    """只有 .part / .incomplete 时不能算已装（子 agent 实测：半成品被当成品）。"""
    from backend import model_manager as mm
    import json

    comfy = tmp_path / "comfy"
    (comfy / "models" / "diffusion_models").mkdir(parents=True)
    (comfy / "models" / "diffusion_models" / "z.safetensors.part").write_bytes(b"x" * 1024)
    cfg = {"paths": {"comfyui": str(comfy)},
           "models": {"image": {"z_image": {"name": "Z", "checkpoint": "z.safetensors"}}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mm, "ROOT", tmp_path)
    monkeypatch.setattr(mm, "COMFY", comfy)

    rows = {r["key"]: r for r in mm.list_models()}
    assert rows["z_image"]["installed"] is False
    # 切到没装好的模型要报错，而不是静默写配置
    try:
        mm.switch("image", "z_image")
        raise AssertionError("未装好的模型不应允许切换")
    except RuntimeError:
        pass
