# -*- coding: utf-8 -*-
import json
import pathlib


def test_setup_hint_empty_when_models_present(tmp_path, monkeypatch):
    """模型齐了就不该打扰用户。"""
    from backend import setup_hint

    comfy = tmp_path / "comfy"
    (comfy / "models" / "diffusion_models").mkdir(parents=True)
    (comfy / "models" / "diffusion_models" / "z.safetensors").write_bytes(b"x")
    mesh = tmp_path / "mesh"
    mesh.mkdir()
    (mesh / "config.json").write_text("{}", encoding="utf-8")
    cfg = {
        "paths": {"comfyui": str(comfy)},
        "models": {
            "image": {"z_image": {"checkpoint": "z.safetensors"}},
            "3d": {"hunyuan_mini": {"path": str(mesh)}},
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(setup_hint, "ROOT", tmp_path)

    assert setup_hint.is_ready() is True
    assert setup_hint.banner_html() == ""


def test_setup_hint_lists_missing_and_commands(tmp_path, monkeypatch):
    """缺模型时要说清缺什么、跑哪条命令。"""
    from backend import setup_hint

    (tmp_path / "config.json").write_text(json.dumps({"paths": {}, "models": {}}), encoding="utf-8")
    monkeypatch.setattr(setup_hint, "ROOT", tmp_path)

    assert setup_hint.is_ready() is False
    html = setup_hint.banner_html()
    assert "还差" in html
    assert "bootstrap_models.py --only direct" in html
    assert "bootstrap_models.py --only image" in html
    assert "doctor.py --fix" in html


def test_setup_hint_mesh_only(tmp_path, monkeypatch):
    """只差出图时，不要提建模那条命令。"""
    from backend import setup_hint

    mesh = tmp_path / "mesh"
    mesh.mkdir()
    (mesh / "model.fp16.safetensors").write_bytes(b"x")
    cfg = {"paths": {}, "models": {"3d": {"hunyuan_mini": {"path": str(mesh)}}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(setup_hint, "ROOT", tmp_path)

    html = setup_hint.banner_html()
    assert "--only image" in html
    assert "--only direct" not in html
