# -*- coding: utf-8 -*-
"""能力后端脚本的位置解析（2026-11-28）。

背景
----
骨骼 / 动画 / 精修 / 贴图 这四条能力的执行脚本原先放在 `work/research/`，
而 `work/` 被 .gitignore 排除 —— 干净安装的用户机器上根本没有这些文件，
`tools/asset_refine_rig.py` 会直接报「缺研究脚本」。

修法
----
把这些脚本的**源码**收进 `tools/backends/`（权重仍然不进仓库），
执行时优先用仓库内的副本，找不到再回退到老的 `work/research/`（开发者本机）。

用法：
    from tools.backends import resolve
    p = resolve("rig/auto_rig.py")        # 返回 Path 或 None
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LEGACY = ROOT / "work" / "research"


def resolve(rel: str) -> Path | None:
    """rel 形如 "rig/auto_rig.py"。仓库副本优先，其次 work/research 旧路径。"""
    cand = HERE / rel
    if cand.is_file():
        return cand
    # 旧布局：rig/auto_rig.py → work/research/rig/auto_rig.py
    old = LEGACY / rel
    if old.is_file():
        return old
    # 贴图脚本在旧布局里多一层 tools/
    old2 = LEGACY / rel.replace("texture/", "texture/tools/")
    if old2.is_file():
        return old2
    return None


def weights_dir() -> Path:
    """研究权重目录（Direct3D-V3 / RigAnything），仍放 work/research 下。"""
    return LEGACY
