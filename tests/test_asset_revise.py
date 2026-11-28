# -*- coding: utf-8 -*-
"""「改一版」必须真的改：先按新要求出图，再建模（2026-11-28 实测）。"""
from __future__ import annotations

import json
import pathlib


def _mk(tmp_path, monkeypatch):
    from backend import assets as A
    from backend import asset_ops as O

    (tmp_path / "assets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(A, "ASSETS", tmp_path / "assets")
    monkeypatch.setattr(A, "INDEX", tmp_path / "assets" / "index.json")
    monkeypatch.setattr(O, "ROOT", tmp_path)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png")
    mesh = tmp_path / "m.glb"
    mesh.write_bytes(b"x")
    row = A.register(mesh, "mesh", title="杯子",
                     params={"source_image": str(src), "model": "hunyuan_mini"})
    return A, O, row, mesh


def test_revise_regenerates_image_then_mesh(tmp_path, monkeypatch):
    A, O, row, mesh = _mk(tmp_path, monkeypatch)

    calls = {"images": 0, "mesh": 0, "prompt": ""}

    class FakeResult:
        def __init__(self, path):
            self.success = True
            self.output_path = path

    class FakePipeline:
        def __init__(self, *a, **k):
            pass

        def generate_images(self, prompt, **k):
            calls["images"] += 1
            calls["prompt"] = prompt
            out = tmp_path / "new.png"
            out.write_bytes(b"png")
            return [FakeResult(str(out))]

        def generate_3d(self, model, quality, image, **k):
            calls["mesh"] += 1
            assert str(image).endswith("new.png"), "应当用新图建模，而不是旧图"
            out = tmp_path / "new.glb"
            out.write_bytes(b"glb")
            return FakeResult(str(out))

    import backend.pipeline as pl
    monkeypatch.setattr(pl, "Pipeline", FakePipeline)

    ok, msg = O.revise(row["id"], "把手加粗")
    assert ok is True
    assert calls["images"] == 1 and calls["mesh"] == 1
    assert "把手加粗" in calls["prompt"]


def test_revise_without_source_image_is_friendly(tmp_path, monkeypatch):
    from backend import assets as A
    from backend import asset_ops as O

    (tmp_path / "assets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(A, "ASSETS", tmp_path / "assets")
    monkeypatch.setattr(A, "INDEX", tmp_path / "assets" / "index.json")
    monkeypatch.setattr(O, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    mesh = tmp_path / "x.glb"
    mesh.write_bytes(b"x")
    row = A.register(mesh, "mesh", title="无来源")
    ok, msg = O.revise(row["id"], "改一下")
    assert not ok and ("来源图" in msg or "引用" in msg)
