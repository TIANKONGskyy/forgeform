# -*- coding: utf-8 -*-
import json
import pathlib

import pytest


def _mk(tmp_path, monkeypatch):
    from backend import assets as A
    from backend import asset_ops as O

    root = tmp_path
    (root / "config.json").write_text(json.dumps({"paths": {}, "models": {}}), encoding="utf-8")
    monkeypatch.setattr(A, "ASSETS", root / "assets")
    monkeypatch.setattr(A, "INDEX", root / "assets" / "index.json")
    (root / "assets").mkdir(parents=True)
    monkeypatch.setattr(O, "ROOT", root)
    return A, O, root


def test_describe_and_delete(tmp_path, monkeypatch):
    A, O, root = _mk(tmp_path, monkeypatch)
    f = root / "m.glb"
    f.write_bytes(b"x" * 2048)
    row = A.register(f, "mesh", title="杯子", metrics={"faces": 1000, "watertight": True})
    d = O.describe(row["id"])
    assert d["ok"] and "面" in d["summary"] and d["title"] == "杯子"
    stored = A.absolute(row)          # 资产库里那份副本
    assert stored.exists()
    ok, msg = O.delete(row["id"])
    assert ok
    assert A.get(row["id"]) is None
    assert not stored.exists()        # 库里的副本被删
    assert f.exists()                 # 用户原始文件不动（只删库）


def test_delete_missing_is_friendly(tmp_path, monkeypatch):
    A, O, root = _mk(tmp_path, monkeypatch)
    ok, msg = O.delete("不存在")
    assert not ok and "不存在" in msg


def test_reference_rejects_non_mesh(tmp_path, monkeypatch):
    A, O, root = _mk(tmp_path, monkeypatch)
    ok, msg = O.reference("x")
    assert not ok


def test_revise_requires_instruction(tmp_path, monkeypatch):
    A, O, root = _mk(tmp_path, monkeypatch)
    f = root / "m.glb"
    f.write_bytes(b"x")
    row = A.register(f, "mesh", title="杯子")
    ok, msg = O.revise(row["id"], "   ")
    assert not ok and "改什么" in msg
