# -*- coding: utf-8 -*-
"""为编译型 CUDA 包生成"占位包"，让上层能 import（2026-11-28 实测用）。

背景
----
ComfyUI-3D-Pack 在 import 阶段就 `from pytorch3d...` / `import nvdiffrast...`，
而这两个包在 Windows + torch 2.7 下都需要现场编译 CUDA 扩展 —— 装不上，
于是**整个 3D-Pack 的所有节点**（包括我们只需要的 Hunyuan3D 2mv 形状生成）一起消失。

占位包只让 import 通过；真正调用这些库的节点会在运行时抛错（可以接受：
MVDream / TriplaneGaussian 那几条支路我们不用）。

用法（装到指定解释器的 site-packages）：
    python tools/make_cuda_stubs.py --python D:\\comfy_venv312\\Scripts\\python.exe
"""
from __future__ import annotations

import argparse
import subprocess
import sys

STUB_BODY = '''"""Auto-generated stub (local test only). Import passes; real use raises."""
import typing as _t


class _Stub:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Stub()

    def __getattr__(self, name):
        return _Stub()

    def __getitem__(self, k):
        return _Stub()

    def __iter__(self):
        return iter(())


def __getattr__(name):
    return _Stub()
'''

PACKAGES = {
    "pytorch3d": [
        "renderer", "renderer.cameras", "renderer.lighting", "renderer.materials",
        "renderer.mesh", "renderer.points", "renderer.utils", "renderer.renderer",
        "renderer.compositing", "renderer.blending", "structures", "ops", "io",
        "vis", "vis.texture_vis", "common", "common.datatypes", "utils",
        "utils.camera_conversions", "utils.build", "transforms", "loss", "datasets",
        "ops.sample_farthest_points", "ops.interpolate_face_attributes", "io.obj_io",
    ],
    "nvdiffrast": [
        "torch", "torch.ops", "torch.utils", "common",
    ],
}

MAKER = '''
import pathlib, sys
target = pathlib.Path({target!r})
body = {body!r}
mods = {mods!r}
target.mkdir(parents=True, exist_ok=True)
(target / "__init__.py").write_text(body, encoding="utf-8")
for m in mods:
    # 每个层级都建成包（带 __init__.py），这样 a.b.c 不会被子模块 a.b 的 .py 挡住
    parts = m.split(".")
    d = target
    for part in parts:
        d = d / part
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            init.write_text(body, encoding="utf-8")
print(f"  ✓ stub {{target.name}}（{{len(mods)+1}} 个文件）")
'''


def install_via(python: str, package: str, mods: list[str]) -> None:
    site = subprocess.check_output(
        [python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        text=True).strip()
    target = f"{site}/{package}"
    code = MAKER.format(target=target.replace("\\", "/"), body=STUB_BODY, mods=mods)
    subprocess.run([python, "-c", code], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="目标解释器")
    ns = ap.parse_args()
    for pkg, mods in PACKAGES.items():
        try:
            install_via(ns.python, pkg, mods)
        except Exception as e:
            print(f"  ✗ {pkg}: {type(e).__name__}: {e}")
    # 验证
    for pkg in PACKAGES:
        r = subprocess.run([ns.python, "-c", f"import {pkg};print('ok')"], capture_output=True)
        print(f"  {'✓' if r.returncode == 0 else '✗'} import {pkg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
