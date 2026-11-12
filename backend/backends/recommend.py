# -*- coding: utf-8 -*-
"""按这台机器的实际情况，生成可选的"装哪套"方案。

输入：硬件（显存/内存/磁盘）+ 已探测到的框架 + 已有的本地模型
输出：几个方案，每个都写明「装什么、下多少、为什么推荐/不推荐」

设计原则：
  · **先问"能不能零下载"** —— 机器上已经有 ComfyUI + 权重的话，最省的方案是直接用它，
    而不是再下 20 GB 另一套格式。
  · 方案必须标 `fits=False` 而不是默默推荐 —— 8 GB 卡拉 33 GB 模型的事不能让它发生。
  · 每个方案都给出**可执行的下一步**（下载命令 / 配置片段），不做成"你知道该装啥了吧"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

GB = 1024 ** 3


# ────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────
@dataclass
class Hardware:
    vram_gb: float = 0.0
    vram_free_gb: float = 0.0
    ram_gb: float = 0.0
    disk_free_gb: float = 0.0
    gpu_name: str = ""

    @property
    def has_gpu(self) -> bool:
        return self.vram_gb > 0

    def to_dict(self) -> dict:
        return {"vram_gb": round(self.vram_gb, 1), "vram_free_gb": round(self.vram_free_gb, 1),
                "ram_gb": round(self.ram_gb, 1), "disk_free_gb": round(self.disk_free_gb, 1),
                "gpu_name": self.gpu_name, "has_gpu": self.has_gpu}


@dataclass
class Download:
    kind: str                       # llm | image | mesh | framework
    plugin_id: str                  # 装完写进 config 用的 key
    repo: str                       # HF repo / ollama 模型名 / 安装包 URL
    dest: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    size_gb: float = 0.0
    note: str = ""

    def command(self) -> str:
        if self.kind == "llm":
            # 走 Ollama 拉，比自己下 GGUF 省事（自动管量化、模板、上下文）
            return f"ollama pull {self.repo}"
        if self.kind == "framework":
            return self.note
        inc = " ".join(f"--include '{p}'" for p in self.include)
        exc = " ".join(f"--exclude '{p}'" for p in self.exclude)
        return (f"python tools/fetch_model.py --repo {self.repo} --dest {self.dest} "
                f"{inc} {exc} --threads 8").replace("  ", " ").strip()


@dataclass
class Profile:
    key: str
    name: str
    desc: str
    backend_mode: str                       # auto | comfyui | direct
    image_model: str = ""                   # 插件 id / comfyui / ""
    mesh_model: str = ""
    downloads: list[Download] = field(default_factory=list)
    est_gb: float = 0.0
    fits: bool = True
    reason: str = ""
    config_patch: dict = field(default_factory=dict)
    quant_hints: list[dict] = field(default_factory=list)

    def total_gb(self) -> float:
        return round(sum(d.size_gb for d in self.downloads) + self.est_gb * 0, 2)

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "desc": self.desc,
                "backend_mode": self.backend_mode, "image_model": self.image_model,
                "mesh_model": self.mesh_model,
                "download_gb": round(sum(d.size_gb for d in self.downloads), 2),
                "downloads": [{"kind": d.kind, "plugin": d.plugin_id, "repo": d.repo,
                               "gb": d.size_gb, "cmd": d.command()} for d in self.downloads],
                "fits": self.fits, "reason": self.reason,
                "config_patch": self.config_patch, "quant_hints": self.quant_hints}


# ────────────────────────────────────────────────────────────────
# 硬件
# ────────────────────────────────────────────────────────────────
def probe_hardware(disk_path: str = ".") -> Hardware:
    from .quant import disk_free_gb, probe_vram

    total, free, name = probe_vram()
    hw = Hardware(vram_gb=total, vram_free_gb=free, gpu_name=name)
    try:
        import psutil
        hw.ram_gb = psutil.virtual_memory().total / GB
    except Exception:
        if os.name == "nt":
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in ("total", "avail", "page", "availpage", "virtual", "availvirtual", "extended")]
            mem = Memory()
            mem.length = ctypes.sizeof(mem)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                hw.ram_gb = mem.total / GB
    hw.disk_free_gb = disk_free_gb(disk_path)
    return hw


# ────────────────────────────────────────────────────────────────
# 已有资产
# ────────────────────────────────────────────────────────────────
def summarize_assets(frameworks) -> dict:
    """从探测结果里抽出"已经有什么"，只关心跟选型有关的。"""
    def first_alive(prefix: str):
        return next((f for f in frameworks if f.key.startswith(prefix) and f.alive), None)

    comfy = first_alive("comfyui")          # 8188 / 8189 都算
    comfy_ready = bool(comfy and "image" in comfy.caps)
    comfy_3d = bool(comfy and "mesh" in comfy.caps)
    diffusers = next((f for f in frameworks if f.key == "diffusers"), None)
    hy3d = next((f for f in frameworks if f.key == "hy3dgen"), None)
    ollama = next((f for f in frameworks if f.key == "ollama" and f.alive), None)
    return {
        "comfyui": comfy_ready,
        "comfyui_3d": comfy_3d,
        "comfyui_models": (comfy.models if comfy else [])[:12],
        "diffusers": bool(diffusers and diffusers.alive),
        "diffusers_models": (diffusers.models if diffusers else [])[:8],
        "hy3dgen": bool(hy3d and hy3d.alive),
        "ollama": bool(ollama),
        "chat": bool(ollama and ollama.models) or bool(first_alive("llama")) or bool(first_alive("vllm"))
                or bool(first_alive("lmstudio")),
        "chat_endpoints": [f.to_dict() for f in frameworks if f.alive and "text" in f.caps and f.models],
    }


# ────────────────────────────────────────────────────────────────
# 对话模型（LLM）选型
# ────────────────────────────────────────────────────────────────
# 铸想的对话不是聊天，是「把一句话解析成结构化需求」—— 3B 就能做，
# 7B 明显更稳，14B 会主动追问缺失参数。再大对收益很小，显存却翻倍。
_LLM_TIERS = [
    # (最低显存, ollama 模型名, 下载体积 GB, 说明)
    (24, "qwen2.5:14b-instruct-q4_K_M", 9.0, "14B，会主动追问缺失尺寸"),
    (12, "qwen2.5:7b-instruct-q4_K_M", 4.7, "7B，解析稳定，最常用的一档"),
    (7, "qwen2.5:7b-instruct-q4_K_M", 4.7, "7B，8G 卡可跑（上下文收窄）"),
    (5, "qwen2.5:3b-instruct-q4_K_M", 2.0, "3B，够解析需求，显存占用小"),
    (0, "qwen2.5:1.5b-instruct-q4_K_M", 1.1, "1.5B，纯 CPU 也能跑，质量一般"),
]


def pick_llm(vram_gb: float, ram_gb: float) -> tuple[str, float, str]:
    """按显存挑对话模型。没 GPU 时也能退到 1.5B 跑 CPU。"""
    if ram_gb < 20:
        return ("qwen2.5:3b-instruct-q4_K_M", 2.0, "低内存模式：3B Q4，按阶段加载")
    for need, name, gb, why in _LLM_TIERS:
        if vram_gb >= need:
            return name, gb, why
    return _LLM_TIERS[-1][1], _LLM_TIERS[-1][2], _LLM_TIERS[-1][3]


def _llm_download(vram_gb: float, ram_gb: float) -> Download:
    name, gb, why = pick_llm(vram_gb, ram_gb)
    return Download("llm", "chat", name, size_gb=gb,
                    note=f"{why}（{gb} GB，ollama pull 一条命令）")


def _ollama_install_download() -> Download:
    return Download(
        "framework", "ollama", "https://ollama.com/download/OllamaSetup.exe",
        size_gb=0.7,
        note="没装 Ollama 时先装它（0.7 GB，装完 ollama serve 自动起）: "
             "winget install Ollama.Ollama   或   https://ollama.com/download")


# ────────────────────────────────────────────────────────────────
# 方案生成
# ────────────────────────────────────────────────────────────────
def build_profiles(hw: Hardware, assets: dict, model_root: str = "") -> list[Profile]:
    out: list[Profile] = []
    model_root = str(Path(model_root or os.environ.get("FORGEFORM_MODELS") or (Path(__file__).resolve().parents[2] / "models")).resolve())

    # 对话模型：机器上没有可用聊天端点时才需要装。
    # 这一步经常被忽略 —— 用户以为装了出图/建模就完事，结果第一句话就没人解析。
    no_chat = not assets.get("chat")
    has_comfy = bool(assets.get("comfyui"))
    has_diffusers = bool(assets.get("diffusers"))
    has_hy3d = bool(assets.get("hy3dgen"))

    def with_llm(dls: list[Download], need_ollama: bool) -> list[Download]:
        """把对话模型（以及 Ollama 本体）排到下载清单最前面。"""
        if not no_chat:
            return dls
        pre: list[Download] = []
        if need_ollama:
            pre.append(_ollama_install_download())
        pre.append(_llm_download(hw.vram_gb, hw.ram_gb))
        return pre + dls

    # ── 方案 0：零下载（机器上已经有能用的 ComfyUI）──
    if assets.get("comfyui") and assets.get("comfyui_3d"):
        n_img = len(assets.get("comfyui_models") or [])
        out.append(Profile(
            key="reuse",
            name="复用现有 ComfyUI（零下载）",
            desc=f"这台机器上 ComfyUI 已经在跑，有 {n_img} 个出图权重，3D-Pack 也装了。"
                 f"出图/建模都走它，不下载任何东西。",
            backend_mode="auto",
            image_model="comfyui",
            mesh_model="comfyui",
            est_gb=0.0,
            fits=True,
            reason="已有可用的出图 + 3D 能力，重新下载是浪费",
            # 必须把 image / 3d 的默认模型也写进去 —— 只写 backend.mode 的话
            # config 里 stages.3d.default_model 是空的，界面进去点建模会不知道用哪个。
            config_patch={
                "backend": {"mode": "auto"},
                "stages": {"image": {"default_model": "qwen21_ref"},
                           "3d": {"default_model": "hunyuan21"}},
            },
        ))

    # ── 按显存分档的直连方案 ──
    vram = hw.vram_gb
    ram = hw.ram_gb

    def fits_by_ram(need: float) -> bool:
        # 留 6 GB 给系统（Windows 自身就要 4–5 GB）。实测模型 RSS 10.8 GB，
        # 16 GB 机器只剩 0.2 GB 余量 —— 会疯狂换页，不能算"可用"。
        return ram >= need + 6

    # 最小：SD1.5 + 2mini
    # 只在机器上已经有 diffusers / hy3dgen 时才提 —— 否则它和下面的 starter 重复，
    # 只是少了"装 Python 推理库"这一项，会误导用户以为能少下 4 GB。
    if hw.has_gpu and (has_diffusers or has_hy3d):
        sd15 = Download("image", "sd15", "stable-diffusion-v1-5/stable-diffusion-v1-5",
                        "D:/models/sd15", ["model_index.json", "vae/", "unet/", "text_encoder/",
                                           "tokenizer/", "scheduler/"], ["*.ckpt"], 4.2,
                        "SD1.5 全精度，8G 显存够")
        mini = Download("mesh", "hunyuan_mini", "tencent/Hunyuan3D-2mini",
                        "D:/models/hy3dgen/hunyuan3d-2mini",
                        ["^hunyuan3d-dit-v2-mini/", "^hunyuan3d-vae-v2-mini/", "^config\\.json$"],
                        ["\\.ckpt$"], 3.96, "3.96 GB，hy3dgen 认的目录结构")
        out.append(Profile(
            key="minimal",
            name="最小可用（SD1.5 + 3D 2mini）",
            desc="出图用 SD1.5，建模用 Hunyuan3D-2mini。8G 显存能跑，画质一般但链路完整。",
            backend_mode="direct",
            image_model="sd15", mesh_model="hunyuan_mini",
            downloads=with_llm([sd15, mini], need_ollama=not assets.get("ollama")),
            est_gb=8.2,
            fits=vram >= 7 and fits_by_ram(11),
            reason=("显存要 7 GB+、内存要 17 GB+（模型自身占 10.8 GB）"
                    if not (vram >= 7 and fits_by_ram(11)) else "够用"),
            config_patch={
                "backend": {"mode": "direct"},
                "stages": {"image": {"default_model": "sd15"},
                           "3d": {"default_model": "hunyuan_mini"}},
                "models": {"image": {"sd15": {"path": "D:/models/sd15",
                                              "pipeline": "StableDiffusionPipeline",
                                              "steps": 25, "width": 512, "height": 512}},
                           "3d": {"hunyuan_mini": {"path": "D:/models/hy3dgen/hunyuan3d-2mini",
                                                   "octree": 192, "steps": 30}}},
            },
        ))

    # 标准：Z-Image + 2mini
    if hw.has_gpu and vram >= 12:
        zi = Download("image", "z_image", "Tongyi-MAI/Z-Image-Turbo",
                      "D:/models/z-image-turbo-diffusers", ["^model_index\\.json$", "^scheduler/",
                      "^text_encoder/", "^tokenizer/", "^transformer/", "^vae/"],
                      ["assets/"], 30.6, "8 步出图，中文理解好")
        out.append(Profile(
            key="standard",
            name="标准（Z-Image Turbo + 3D 2mini）",
            desc="出图 Z-Image（8 步、1024²、中文提示词准），建模 2mini。"
                 "作者本人日常用的就是这一档。",
            backend_mode="auto",
            image_model="z_image", mesh_model="hunyuan_mini",
            downloads=with_llm([zi], need_ollama=not assets.get("ollama")), est_gb=30.6,
            fits=vram >= 12 and fits_by_ram(11) and hw.disk_free_gb >= 40,
            reason=("显存 12 GB+ / 内存 16 GB+ / 磁盘 40 GB+"
                    if vram >= 12 else f"显存只有 {vram:.0f} GB，不够"),
            config_patch={
                "backend": {"mode": "auto"},
                "stages": {"image": {"default_model": "z_image"},
                           "3d": {"default_model": "hunyuan_mini"}},
                "models": {"image": {"z_image": {"path": "D:/models/z-image-turbo-diffusers"}},
                           "3d": {"hunyuan_mini": {"path": "D:/models/hy3dgen/hunyuan3d-2mini",
                                                   "octree": 256, "steps": 30}}},
            },
        ))

    # 完整：用已有的 ComfyUI 补 3D 大模型
    if assets.get("comfyui") and assets.get("comfyui_3d") and vram >= 16:
        out.append(Profile(
            key="full-comfy",
            name="全量（ComfyUI + Hunyuan3D 2.1）",
            desc="出图用 ComfyUI 的大模型（1024²），建模用 2.1（512 octree，质量天花板）。"
                 "已有 ComfyUI 的话不用下载。",
            backend_mode="comfyui",
            image_model="comfyui", mesh_model="hunyuan21",
            downloads=[], est_gb=0.0,
            fits=True,
            reason="ComfyUI 已有权重，2.1 的权重若缺可用 docs/MODELS.md 里的链接补",
            config_patch={
                "backend": {"mode": "comfyui"},
                "stages": {"image": {"default_model": "qwen21_ref"},
                           "3d": {"default_model": "hunyuan21"}},
            },
        ))

    # ── 从零开始：什么都没有的机器 ──
    # 这是最容易被漏掉的一类用户：新装的 Windows、只有显卡驱动。
    # 四个东西都得装：Ollama（对话）+ LLM + 出图模型 + 建模模型，走直连不装 ComfyUI。
    if not has_comfy and not has_diffusers and not has_hy3d:
        sd15_boot = Download("image", "sd15", "stable-diffusion-v1-5/stable-diffusion-v1-5",
                             "D:/models/sd15",
                             ["model_index.json", "vae/", "unet/", "text_encoder/",
                              "tokenizer/", "scheduler/"], ["*.ckpt"], 4.2,
                             "SD1.5 全精度，8G 显存够")
        mini_boot = Download("mesh", "hunyuan_mini", "tencent/Hunyuan3D-2mini",
                             "D:/models/hy3dgen/hunyuan3d-2mini",
                             ["^hunyuan3d-dit-v2-mini/", "^hunyuan3d-vae-v2-mini/",
                              "^config\\.json$"], ["\\.ckpt$"], 3.96,
                             "3.96 GB，hy3dgen 认的目录结构")
        py_deps = Download("framework", "pydeps", "torch + diffusers + hy3dgen", size_gb=4.0,
                           note="pip install torch diffusers transformers hy3dgen  （约 4 GB，含 CUDA 运行时）")
        dls = with_llm([py_deps, sd15_boot, mini_boot],
                       need_ollama=not assets.get("ollama"))
        total = round(sum(d.size_gb for d in dls), 1)
        out.append(Profile(
            key="starter",
            name="从零开始（LLM + 出图 + 建模全装）",
            desc=f"这台机器上没找到可用的推理框架或模型。需要装 Ollama（对话）、"
                 f"Python 推理库、SD1.5（出图）、Hunyuan3D 2mini（建模），合计约 {total} GB。"
                 f"走直连模式，不需要 ComfyUI。",
            backend_mode="direct",
            image_model="sd15", mesh_model="hunyuan_mini",
            downloads=dls, est_gb=total,
            fits=(hw.vram_gb >= 7 and hw.ram_gb >= 17 and hw.disk_free_gb >= total + 10),
            reason=("显存 ≥ 7 GB / 内存 ≥ 17 GB / 磁盘 ≥ "
                    f"{total + 10:.0f} GB" if not (hw.vram_gb >= 7 and hw.ram_gb >= 17
                                                  and hw.disk_free_gb >= total + 10) else "够用"),
            config_patch={
                "backend": {"mode": "direct"},
                "agent": {"provider": "ollama"},
                "stages": {"image": {"default_model": "sd15"},
                           "3d": {"default_model": "hunyuan_mini"}},
                "models": {
                    "image": {"sd15": {"path": "D:/models/sd15",
                                       "pipeline": "StableDiffusionPipeline",
                                       "steps": 25, "width": 512, "height": 512}},
                    "3d": {"hunyuan_mini": {"path": "D:/models/hy3dgen/hunyuan3d-2mini",
                                            "octree": 192, "steps": 30}},
                },
            },
        ))

    # 只有 CPU 的机器
    if not hw.has_gpu:
        out.append(Profile(
            key="cpu-only",
            name="无 GPU（只能看界面）",
            desc="没探测到 NVIDIA 显卡。铸想可以启动、能翻历史、能下载已有模型，"
                 "但所有生成动作不可用。",
            backend_mode="comfyui", est_gb=0.0, fits=False,
            reason="diffusers / hy3dgen 都要 CUDA",
        ))

    from .quant import recommend_quant
    import json
    for p in out:
        # All installed assets belong to the chosen directory, never a fixed drive.
        p.config_patch = json.loads(json.dumps(p.config_patch).replace("D:/models", model_root.replace("\\", "/")))
        for d in p.downloads:
            d.dest = d.dest.replace("D:/models", model_root.replace("\\", "/"))
        if p.key in ("reuse", "full-comfy") and no_chat:
            p.downloads = with_llm(p.downloads, not assets.get("ollama"))
        if p.key in ("minimal", "standard"):
            if not any(d.plugin_id == "pydeps" for d in p.downloads):
                p.downloads.insert(0, Download("framework", "pydeps", "Python 推理环境", size_gb=4,
                    note="在铸想独立环境安装推理依赖"))
            if not any(d.kind == "mesh" for d in p.downloads):
                p.downloads.append(Download("mesh", "hunyuan_mini", "tencent/Hunyuan3D-2mini",
                    str(Path(model_root) / "hy3dgen" / "hunyuan3d-2mini"),
                    ["^hunyuan3d-dit-v2-mini/", "^hunyuan3d-vae-v2-mini/", "^config\\.json$"], ["\\.ckpt$"], 3.96))
        llm = next((d for d in p.downloads if d.kind == "llm"), None)
        if llm:
            p.config_patch["agent"] = {"enabled": True, "provider": "ollama"}
            p.config_patch.setdefault("models", {})["llm"] = {"default_model": llm.repo,
                "fallback_model": llm.repo, "think": False, "models": {llm.repo: {"num_ctx": 2048 if hw.ram_gb < 20 else 4096}}}
        elif assets.get("chat_endpoints"):
            ep = assets["chat_endpoints"][0]
            p.config_patch["agent"] = {"enabled": True, "provider": "strata", "strata_url": ep["url"].rstrip("/") + ("/v1" if ep["key"] == "ollama" else ""), "strata_model": ep["models"][0]}
        for d in p.downloads:
            if d.kind not in ("image", "mesh", "llm"):
                continue
            size = 3.6 if d.plugin_id == "hunyuan_mini" else d.size_gb
            hint = recommend_quant(size, hw.vram_gb, hw.ram_gb, kind=d.kind)
            p.quant_hints.append({"model": d.plugin_id, "kind": d.kind, **hint})
        if p.key in ("starter", "minimal") and hw.vram_gb >= 7 and hw.ram_gb >= 15.5:
            p.fits = True
            p.reason = "低内存模式：按阶段加载；建模前需至少 11 GB 空闲内存，先关闭其他大型应用。16 GB 实机待验证。" if hw.ram_gb < 20 else "按阶段加载"
            p.config_patch.setdefault("runtime", {})["low_memory"] = hw.ram_gb < 20
            p.config_patch["runtime"]["keep_models"] = False
        total = sum(d.size_gb for d in p.downloads)
        if total and hw.disk_free_gb < total * 1.2 + 5:
            p.fits = False
            p.reason = f"磁盘不足：下载及临时文件预计需 {total * 1.2 + 5:.1f} GB"
        if p.key == "standard" and any(not h["fits"] for h in p.quant_hints):
            p.fits = False
            p.reason = "原精度加载峰值超出内存/显存预算，请选轻量方案或复用已有量化模型"
        for kind in ("image", "3d"):
            for cfg in p.config_patch.get("models", {}).get(kind, {}).values():
                cfg.setdefault("quant", "auto")
    return out


def pick_default(profiles: list[Profile]) -> Profile | None:
    """默认选哪个：优先 fits 的、名字里带"零下载"的，其次 fits 的最后一个（质量最高）。"""
    ok = [p for p in profiles if p.fits]
    if not ok:
        return profiles[0] if profiles else None
    for p in ok:
        if p.key == "reuse":
            return p
    return ok[-1]


def explain(hw: Hardware, assets: dict, profiles: list[Profile]) -> str:
    lines = []
    lines.append(f"硬件：{hw.gpu_name or '无 GPU'} · 显存 {hw.vram_gb:.0f} GB"
                 f"（可用 {hw.vram_free_gb:.0f}） · 内存 {hw.ram_gb:.0f} GB"
                 f" · 磁盘剩 {hw.disk_free_gb:.0f} GB")
    have = []
    if assets.get("comfyui"):
        have.append("ComfyUI" + ("(含3D)" if assets.get("comfyui_3d") else ""))
    if assets.get("diffusers"):
        have.append(f"本地 diffusers 模型 {len(assets.get('diffusers_models') or [])} 个")
    if assets.get("hy3dgen"):
        have.append("hy3dgen")
    if assets.get("chat"):
        have.append("对话服务")
    lines.append("已有：" + ("、".join(have) if have else "什么都没有"))
    return "\n".join(lines)
