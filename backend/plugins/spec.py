# -*- coding: utf-8 -*-
"""模型插件：一个 YAML 描述一个模型，加文件即接入，不改代码。

为什么是插件而不是 `if kind == "z_image": ... else: ...`：

    铸想的定位是"一句话建模"，模型本身会不断换代。把模型写死在代码里的后果是
    每来一个新模型就要改 `direct.py`、跑一遍测试、发一次版。插件化之后，
    换模型 = 丢一个 YAML 进 `plugins/`，或者改 `config.json` 里的 `default_model`。

一个插件长这样（`plugins/image/z_image.yaml`）：

    id: z_image
    kind: image
    label: Z-Image Turbo 6B
    pipeline: ZImagePipeline        # diffusers 类名
    repo: Tongyi-MAI/Z-Image-Turbo  # 或本地 path
    defaults:
      steps: 8
      guidance: 1.0
      width: 1024
      height: 1024

`pipeline` 写类名就能用，是因为 diffusers 的 pipeline 都接受
`from_pretrained(path, torch_dtype=...)` + 统一的 `__call__(prompt=..., num_inference_steps=...)`。
真遇到不兼容的模型，再加一个 `adapter: my_adapter.py` 做转接。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent          # backend/plugins/
REPO_PLUGINS = PLUGIN_ROOT.parent.parent / "plugins"    # <repo>/plugins/
USER_PLUGINS = Path(os.environ.get("FORGEFORM_PLUGINS") or (Path.home() / ".forgeform" / "plugins"))


# ────────────────────────────────────────────────────────────────
# 极简 YAML 读取：只支持插件用到的两种结构
#   key: value
#   key:
#     sub: value
# 不引第三方依赖（pyyaml 不是必需依赖）。
# ────────────────────────────────────────────────────────────────
def _coerce(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    if s[0] in "\"'" and s[-1] == s[0] and len(s) > 1:
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    # 列表 [a, b]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_coerce(x) for x in inner.split(",")] if inner else []
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def parse_simple_yaml(text: str) -> dict:
    out: dict = {}
    cur: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        key = key.strip()
        if indent == 0:
            if val.strip():
                out[key] = _coerce(val)
                cur = None
            else:
                out[key] = {}
                cur = out[key]
        elif cur is not None:
            cur[key] = _coerce(val)
    return out


# ────────────────────────────────────────────────────────────────
# 插件对象
# ────────────────────────────────────────────────────────────────
@dataclass
class ModelPlugin:
    id: str
    kind: str                      # image | mesh
    label: str = ""
    pipeline: str = ""             # diffusers 类名（image）
    loader: str = ""               # 完整 import 路径（mesh，或特殊 image）
    repo: str = ""                 # HF repo id，下载用
    path: str = ""                 # 本地目录
    adapter: str = ""              # 可选：同目录下的 .py，做参数转接
    defaults: dict = field(default_factory=dict)
    vram_hint: float = 0.0
    unavailable_reason: str = ""
    notes: str = ""
    source: str = ""               # 来自哪个文件

    # ── 便利属性 ──
    @property
    def target(self) -> str:
        """加载时用的引用：本地目录优先，其次 repo。"""
        return self.path or self.repo

    def params(self, override: dict | None = None) -> dict:
        out = dict(self.defaults)
        out.update({k: v for k, v in (override or {}).items() if v is not None})
        return out

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("id", "kind", "label", "pipeline", "loader", "repo", "path",
              "defaults", "vram_hint", "notes", "source")}
        return d


# ────────────────────────────────────────────────────────────────
# 注册表
# ────────────────────────────────────────────────────────────────
class Registry:
    def __init__(self):
        self._items: dict[str, ModelPlugin] = {}
        self._errors: list[str] = []

    def add(self, plugin: ModelPlugin) -> None:
        self._items[f"{plugin.kind}:{plugin.id}"] = plugin

    def get(self, kind: str, pid: str) -> ModelPlugin | None:
        return self._items.get(f"{kind}:{pid}")

    def of_kind(self, kind: str) -> list[ModelPlugin]:
        return [p for k, p in self._items.items() if k.startswith(f"{kind}:")]

    def all(self) -> list[ModelPlugin]:
        return list(self._items.values())

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def load_dir(self, root: Path) -> int:
        if not root.exists():
            return 0
        n = 0
        for f in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
            try:
                data = parse_simple_yaml(f.read_text(encoding="utf-8"))
                pid = str(data.get("id") or f.stem)
                kind = str(data.get("kind") or f.parent.name).rstrip("s")
                if kind not in ("image", "mesh", "video"):
                    kind = "image"
                plugin = ModelPlugin(
                    id=pid, kind=kind,
                    label=str(data.get("label") or pid),
                    pipeline=str(data.get("pipeline") or ""),
                    loader=str(data.get("loader") or ""),
                    repo=str(data.get("repo") or ""),
                    path=str(data.get("path") or ""),
                    adapter=str(data.get("adapter") or ""),
                    defaults=dict(data.get("defaults") or {}),
                    vram_hint=float(data.get("vram_hint") or 0),
                    notes=str(data.get("notes") or ""),
                    unavailable_reason=str(data.get("unavailable_reason") or ""),
                    source=str(f),
                )
                if plugin.adapter:
                    plugin.adapter = str(f.parent / plugin.adapter)
                self.add(plugin)
                n += 1
            except Exception as exc:
                self._errors.append(f"{f}: {type(exc).__name__}: {exc}")
        return n


_REGISTRY: Registry | None = None


def registry(reload: bool = False) -> Registry:
    """全进程一个注册表。先读仓库内置插件，再读用户目录（用户可以覆盖内置的）。"""
    global _REGISTRY
    if _REGISTRY is not None and not reload:
        return _REGISTRY
    r = Registry()
    r.load_dir(REPO_PLUGINS)
    r.load_dir(USER_PLUGINS)
    _REGISTRY = r
    return r


def resolve(kind: str, pid: str) -> ModelPlugin | None:
    return registry().get(kind, pid)
