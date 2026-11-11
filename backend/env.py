# -*- coding: utf-8 -*-
"""运行时路径解析 —— 全仓库唯一允许出现「安装位置」的地方。

铸想早期把 ``D:\\AI-Workshop`` / ``D:\\ComfyUI-New`` 写死在十几个模块里，
换台机器、换个盘符就起不来。现在统一从这里取，分两类：

1. **仓库自带目录**（outputs / temp / projects / logs / models / parts_cad/out）
   跟随本文件所在位置，clone 到任何路径都能跑，不需要配置。

2. **外部依赖**（ComfyUI 本体、它用的解释器、bambu CLI）走环境变量覆盖：

   ================  ==================================  ==========================
   变量              含义                                默认
   ================  ==================================  ==========================
   COMFYUI_DIR       ComfyUI 安装目录                    <仓库同级>/ComfyUI
   COMFY_PYTHON      ComfyUI 使用的 Python 解释器        当前解释器 sys.executable
   CAD_PYTHON        跑 CadQuery 示例（parts_cad）的解释器 不设则跳过相关测试
   BAMBU_EXE         拓竹 bambu CLI 可执行文件            从 PATH 查找
   FOUNDRY_PORT      Web 端口                            7860
   ================  ==================================  ==========================

所有值在导入时求值一次；改了环境变量需要重启进程。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── 仓库根：backend/env.py → backend/ → 仓库根 ──────────────────
ROOT: Path = Path(__file__).resolve().parent.parent

# ── 仓库自带目录 ────────────────────────────────────────────────
OUTPUTS: Path = ROOT / "outputs"
TEMP: Path = ROOT / "temp"
PROJECTS: Path = ROOT / "projects"
LOGS: Path = ROOT / "logs"
MODELS: Path = ROOT / "models"
PARTS_OUT: Path = ROOT / "parts_cad" / "out"
CONFIG_FILE: Path = ROOT / "config.json"
CONFIG_EXAMPLE: Path = ROOT / "config.example.json"

# 首次启动要自动创建的目录
_MANAGED_DIRS = [
    OUTPUTS, OUTPUTS / "images", OUTPUTS / "videos", OUTPUTS / "3d", OUTPUTS / "stl",
    TEMP, PROJECTS, LOGS, MODELS,
]


def _path_env(name: str, default: Path | None) -> Path:
    raw = os.environ.get(name, "").strip()
    if raw:
        return Path(raw).expanduser()
    return default if default is not None else Path("")


# ── 外部依赖（ComfyUI 必须由用户自行安装并指定）──────────────────
COMFYUI_DIR: Path = _path_env("COMFYUI_DIR", ROOT.parent / "ComfyUI")
COMFY_PYTHON: Path = _path_env("COMFY_PYTHON", Path(sys.executable))
# 跑 CadQuery 示例脚本的解释器（可选，没装就跳过相关用例）
CAD_PYTHON: Path = _path_env("CAD_PYTHON", None)

PORT: int = int(os.environ.get("FOUNDRY_PORT", "7860"))


def ensure_dirs() -> list[Path]:
    """创建所有托管目录，返回本次新建的（失败不抛，只警告）。"""
    created: list[Path] = []
    for d in _MANAGED_DIRS:
        try:
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(d)
        except OSError as exc:  # 只读盘 / 权限不足 —— 别让整个 app 起不来
            print(f"[env] 目录创建失败 {d}: {exc}", file=sys.stderr)
    return created


def ensure_config() -> Path:
    """没有 config.json 就拿 config.example.json 复制一份（首次运行友好）。"""
    if not CONFIG_FILE.exists() and CONFIG_EXAMPLE.exists():
        CONFIG_FILE.write_bytes(CONFIG_EXAMPLE.read_bytes())
    return CONFIG_FILE


def normalize_config_paths(cfg: dict) -> dict:
    """把 config.json 的 ``paths.*`` 就地展开成绝对路径。

    config.example.json 里写的是 ``./outputs`` 这类相对值，ComfyUI 目录则可能写
    ``${COMFYUI_DIR}``；两种都在这里统一处理：相对值按仓库根解析，
    ``${...}`` 按环境变量展开，最后给 comfyui / models 补默认值。
    原地修改并返回同一个 dict，方便链式调用。
    """
    paths = cfg.get("paths")
    if not isinstance(paths, dict):
        paths = cfg["paths"] = {}
    for key, raw in list(paths.items()):
        if not raw:
            continue
        val = os.path.expandvars(str(raw))
        if "${" in val:  # 变量没定义，展开失败 —— 别把字面量当路径用
            continue
        p = Path(val).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        paths[key] = str(p)
    paths.setdefault("comfyui", str(COMFYUI_DIR))
    paths.setdefault("models", str(COMFYUI_DIR / "models"))
    for key, default in (("output", OUTPUTS), ("temp", TEMP), ("meshy_api", ROOT / "meshy_api")):
        if not paths.get(key):
            paths[key] = str(default)
    return cfg


def describe() -> str:
    """启动日志用：一眼看清各路径落在哪。"""
    rows = [
        ("仓库根", ROOT),
        ("输出目录", OUTPUTS),
        ("ComfyUI", COMFYUI_DIR),
        ("ComfyUI Python", COMFY_PYTHON),
        ("端口", PORT),
    ]
    return "  ".join(f"{k}={v}" for k, v in rows)
