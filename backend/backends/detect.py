# -*- coding: utf-8 -*-
"""探测这台机器上有什么推理框架，以及它们能干什么。

铸想不该假定"用户一定装了 ComfyUI"或"一定没装 Ollama"。启动时先看一眼：

    Ollama 活着  → 对话可以直接用它，不用装任何东西
    ComfyUI 活着 → 出图/3D 走它（能用它的自定义节点和现成权重）
    都没有       → 自己装 diffusers / hy3dgen，用直连模式

探测必须是**快且无副作用**的：只发 GET、超时 2 秒、绝不拉起任何服务。
结果给 `backend/backends/hybrid.py` 做路由决策，也给 `tools/doctor.py` 展示。
"""
from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 2.5
UA = "forgeform-detect/1.0"


@dataclass
class Framework:
    key: str                      # ollama / comfyui / llama / vllm / lmstudio / diffusers / hy3dgen
    label: str
    alive: bool = False
    url: str = ""
    models: list[str] = field(default_factory=list)
    caps: set[str] = field(default_factory=set)      # text / image / mesh / video
    detail: str = ""
    note: str = ""

    def has(self, cap: str) -> bool:
        return self.alive and cap in self.caps

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "alive": self.alive, "url": self.url,
                "models": self.models[:12], "caps": sorted(self.caps),
                "detail": self.detail, "note": self.note}


# ────────────────────────────────────────────────────────────────
# 基础
# ────────────────────────────────────────────────────────────────
def _get_json(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _scan_model_dir(comfy: Path) -> dict[str, list[str]]:
    """扫 ComfyUI 的 models 目录，判断它到底有没有能用的权重。"""
    out = {"image": [], "mesh": [], "vae": []}
    subs = {
        "image": ("checkpoints", "diffusion_models", "unet"),
        "vae": ("vae",),
        "mesh": ("3d",),
    }
    for kind, names in subs.items():
        for n in names:
            d = comfy / "models" / n
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".safetensors", ".ckpt", ".gguf", ".bin"):
                    out[kind].append(f.name)
    # 3D-Pack 的权重常放在自己的目录里
    pack = comfy / "custom_nodes" / "ComfyUI-3D-Pack-main"
    if pack.exists():
        for f in pack.rglob("*.ckpt"):
            if f.is_file():
                out["mesh"].append(f.name)
    return out


# ────────────────────────────────────────────────────────────────
# 各家探测
# ────────────────────────────────────────────────────────────────
def probe_ollama(host: str = "127.0.0.1", port: int = 11434) -> Framework:
    fw = Framework("ollama", "Ollama", url=f"http://{host}:{port}")
    if not _port_open(host, port):
        return fw
    try:
        data = _get_json(f"{fw.url}/api/tags")
        fw.models = [m.get("name", "") for m in (data.get("models") or [])]
        fw.alive = True
        fw.caps = {"text"}
        fw.detail = f"{len(fw.models)} 个模型"
    except Exception as exc:
        fw.note = f"端口通但读不到模型列表: {type(exc).__name__}"
    return fw


def probe_openai_compat(key: str, label: str, host: str, port: int) -> Framework:
    """llama.cpp / vLLM / LM Studio / 任何 OpenAI 兼容端点。"""
    fw = Framework(key, label, url=f"http://{host}:{port}/v1")
    if not _port_open(host, port):
        return fw
    try:
        data = _get_json(f"{fw.url}/models")
        fw.models = [m.get("id", "") for m in (data.get("data") or [])]
        fw.alive = True
        fw.caps = {"text"}
        fw.detail = f"{len(fw.models)} 个模型"
    except Exception as exc:
        fw.note = f"端口通但 /v1/models 不可用: {type(exc).__name__}"
    return fw


def probe_comfyui(host: str = "127.0.0.1", port: int = 8188,
                  comfy_dir: str | Path | None = None) -> Framework:
    fw = Framework("comfyui", "ComfyUI", url=f"http://{host}:{port}")
    if not _port_open(host, port):
        return fw
    try:
        stats = _get_json(f"{fw.url}/system_stats")
        fw.alive = True
        devs = stats.get("devices") or []
        if devs:
            d = devs[0]
            vram = (d.get("vram_total") or 0) / 1024 ** 3
            fw.detail = f"{d.get('name', '?')}" + (f"，{vram:.0f}G" if vram else "")
    except Exception as exc:
        fw.note = f"端口通但 /system_stats 失败: {type(exc).__name__}"
        return fw

    if comfy_dir:
        comfy = Path(comfy_dir)
        found = _scan_model_dir(comfy)
        if found["image"]:
            fw.caps.add("image")
        if found["mesh"] or (comfy / "custom_nodes" / "ComfyUI-3D-Pack-main").exists():
            fw.caps.add("mesh")
        n3d = comfy / "custom_nodes" / "ComfyUI-3D-Pack-main"
        bits = [f"出图权重 {len(found['image'])}", f"VAE {len(found['vae'])}"]
        if n3d.exists():
            bits.append("3D-Pack 已装")
        fw.models = found["image"][:12]
        fw.detail = (fw.detail + " · " if fw.detail else "") + "，".join(bits)
    else:
        # 不知道目录就按"它能出图"乐观处理（ComfyUI 活着本身就有价值）
        fw.caps = {"image", "mesh"}
        fw.note = "未提供 ComfyUI 目录，按通用能力处理"
    return fw


def probe_local_diffusers(extra_roots: list[str | Path] | None = None) -> Framework:
    """扫本地有哪些 diffusers 格式模型（有 model_index.json 的目录）。"""
    fw = Framework("diffusers", "本地 diffusers 模型")
    roots = [Path.home() / ".cache" / "huggingface", Path("D:/models"), Path("D:/hf-cache")]
    for r in extra_roots or []:
        roots.insert(0, Path(r))
    found = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for f in root.rglob("model_index.json"):
                if len(f.parts) - len(root.parts) > 5:
                    continue
                try:
                    idx = json.loads(f.read_text(encoding="utf-8"))
                    found.append((f.parent, idx.get("_class_name", "?")))
                except Exception:
                    continue
        except Exception:
            continue
    fw.models = [f"{cls} @ {p}" for p, cls in found[:12]]
    fw.alive = bool(found)
    if found:
        fw.caps = {"image"}
        fw.detail = f"{len(found)} 个"
    return fw


def probe_hy3dgen() -> Framework:
    fw = Framework("hy3dgen", "hy3dgen（Hunyuan3D Python 包）")
    try:
        import hy3dgen  # noqa: F401
        fw.alive = True
        fw.caps = {"mesh"}
        fw.detail = "已安装"
        mods = []
        try:
            from hy3dgen.shapegen import (Hunyuan3DDiTPipeline,  # noqa: F401
                                          Hunyuan3DDiTFlowMatchingPipeline)  # noqa: F401
            mods.append("shapegen")
        except Exception:
            pass
        try:
            from hy3dgen.texgen import Hunyuan3DPaintPipeline  # noqa: F401
            mods.append("texgen")
        except Exception:
            pass
        if mods:
            fw.detail += "（" + " + ".join(mods) + "）"
    except Exception:
        pass
    return fw


def probe_all(comfy_dir: str | Path | None = None,
              hosts: list[str] | None = None) -> list[Framework]:
    """把能探的都探一遍。返回按"能干活"排序的清单。"""
    hosts = hosts or ["127.0.0.1"]
    out: list[Framework] = []

    for h in hosts:
        if h == "127.0.0.1":
            out.append(probe_ollama(h, 11434))
            out.append(probe_comfyui(h, 8188, comfy_dir))
            out.append(probe_comfyui(h, 8189, comfy_dir))     # 双卡时的第二个实例
        out.append(probe_openai_compat(f"llama-{h}", "llama.cpp", h, 8080))
        out.append(probe_openai_compat(f"llama8091-{h}", "llama.cpp (8091)", h, 8091))
        out.append(probe_openai_compat(f"vllm-{h}", "vLLM", h, 8000))
        out.append(probe_openai_compat(f"lmstudio-{h}", "LM Studio", h, 1234))

    out.append(probe_local_diffusers([comfy_dir] if comfy_dir else None))
    out.append(probe_hy3dgen())
    return out


# ────────────────────────────────────────────────────────────────
# 路由建议
# ────────────────────────────────────────────────────────────────
CAP_NAMES = {"text": "对话", "image": "出图", "mesh": "建模", "video": "视频"}


def recommend(frameworks: list[Framework]) -> dict[str, str]:
    """每个阶段该用哪个后端。返回 {"image": "comfyui", "mesh": "direct", "text": "ollama"}。

    优先级（每种能力）：
      出图/建模：ComfyUI 优先（有现成权重、节点生态）→ 直连
      对话：Ollama → llama.cpp/vLLM/LM Studio → 关掉（纯规则）
    """
    pick: dict[str, str] = {}

    # 出图 / 建模
    comfy = next((f for f in frameworks if f.key == "comfyui" and f.alive), None)
    if comfy and "image" in comfy.caps:
        pick["image"] = "comfyui"
    elif next((f for f in frameworks if f.key == "diffusers" and f.alive), None):
        pick["image"] = "direct"

    if comfy and "mesh" in comfy.caps:
        pick["mesh"] = "comfyui"
    elif next((f for f in frameworks if f.key == "hy3dgen" and f.alive), None):
        pick["mesh"] = "direct"

    # 对话
    if next((f for f in frameworks if f.key == "ollama" and f.alive), None):
        pick["text"] = "ollama"
    elif next((f for f in frameworks if f.key.startswith("llama") and f.alive), None):
        pick["text"] = "openai"
    elif next((f for f in frameworks if f.key.startswith(("vllm", "lmstudio")) and f.alive), None):
        pick["text"] = "openai"
    else:
        pick["text"] = "off"
    return pick


def describe(frameworks: list[Framework]) -> str:
    lines = []
    for f in frameworks:
        if not f.alive:
            continue
        caps = "、".join(CAP_NAMES.get(c, c) for c in sorted(f.caps)) or "?"
        lines.append(f"  ✓ {f.label:<22} {f.url:<26} 能: {caps}"
                     + (f"  （{f.detail}）" if f.detail else ""))
    # 端口不通但对用户有意义的（比如本机没装 hy3dgen）
    for f in frameworks:
        if not f.alive and f.key in ("hy3dgen", "diffusers"):
            lines.append(f"  · {f.label:<22} 未安装/未发现")
    return "\n".join(lines) if lines else "  （没探测到任何推理框架）"
