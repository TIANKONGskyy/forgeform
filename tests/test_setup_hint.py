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
    assert "first_run.py" in html
    assert "--with-image" in html
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
    assert "first_run.py" in html
    assert "--with-image" in html

def test_first_run_dry_run_is_safe(tmp_path, monkeypatch):
    """--dry-run 只体检，不下载、不改配置。"""
    import subprocess
    import sys

    from backend import setup_hint

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(setup_hint, "ROOT", tmp_path)

    root = pathlib.Path(__file__).resolve().parent.parent
    p = subprocess.run([sys.executable, str(root / "tools" / "first_run.py"), "--dry-run"],
                       cwd=str(root), capture_output=True, timeout=300)
    out = (p.stdout or b"").decode("utf-8", "replace")
    assert p.returncode == 0
    assert "dry-run" in out
    assert "bootstrap" in out or "direct" in out
