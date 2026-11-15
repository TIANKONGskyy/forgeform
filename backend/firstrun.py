# -*- coding: utf-8 -*-
"""下载完就能用：**挑这台机器最快的可行路径，直接应用，不问**。

## 为什么要"挑"而不是"问"

用户下载完打开，面对四个选项（复用已有 / 云端 / 下载全量 / 只装最小）根本
没法判断哪个能成。而机器自己知道：

    ComfyUI 已装  → 0 GB，立刻可用（这是绝大多数已装过 AI 工具的人）
    云端 key 已填 → 0 GB，立刻可用（新用户最省事）
    有显卡但啥都没有 → 最小方案 ~10 GB（SD1.5 + 2mini + 2B 对话）
    没显卡 → 只开界面 + 云端（本地生成跑不动，别让人白等）

`auto()` 按这个顺序挑第一个**真能跑通**的，写好配置，返回一句话说明。
失败不抛，退回"界面可用"状态。

## 三种就绪度要分开看

出图 / 建模 / 对话是**三条独立的链**，各自可能就绪或缺失。UI 要分别显示，
否则一个缺失就让人以为"整个不能用"。

    status()["stages"] = {
      "chat":  {"ready": True,  "how": "Strata 125B @ 8091"},
      "image": {"ready": True,  "how": "复用 ComfyUI（z_image）"},
      "mesh":  {"ready": False, "why": "没有可用的 3D 模型，需下载 4 GB 或接云端"},
    }
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _has_keys() -> dict:
    """云端三条链各自有没有 key。"""
    try:
        from backend import cloud
        return {k: cloud.available(k) for k in ("enhancer", "image", "mesh")}
    except Exception:
        return {"enhancer": False, "image": False, "mesh": False}


def _probe() -> dict:
    """摸一遍这台机器手上有什么。"""
    p = {"comfyui": "", "comfy3d": False, "hy3dgen": False, "diffusers": 0,
         "ollama": False, "strata": False, "gpu": "", "vram_gb": 0.0,
         "models": {"image": [], "mesh": []}, "cloud": _has_keys()}
    try:
        from backend.backends.detect import guess_comfy_dir
        p["comfyui"] = guess_comfy_dir() or ""
    except Exception:
        pass
    if p["comfyui"]:
        cd = Path(p["comfyui"])
        # 目录名实测是 `ComfyUI-3D-Pack-main`（zip 解压带 -main 后缀），
        # 精确匹配会误报"没装 3D-Pack"，进而把"出图+建模都能用"降级成"只能出图"。
        nodes = cd / "custom_nodes"
        p["comfy3d"] = any(
            "3d" in d.name.lower() and "pack" in d.name.lower()
            for d in nodes.iterdir() if d.is_dir()) if nodes.is_dir() else False
        md = cd / "models"
        for kind, patterns in (
            ("image", ("checkpoints/*.safetensors", "checkpoints/*.ckpt",
                       "checkpoints/*.gguf", "diffusion_models/*.safetensors")),
            ("mesh", ("checkpoints/tencent/**/*.ckpt", "diffusion_models/*hunyuan*")),
        ):
            found = []
            for pat in patterns:
                found += [str(x.relative_to(md)) for x in md.glob(pat)]
            p["models"][kind] = sorted(set(found))[:8]
    try:
        import importlib.util
        p["hy3dgen"] = importlib.util.find_spec("hy3dgen") is not None
    except Exception:
        pass
    for probe_dir in (ROOT / "models", Path("D:/models"), Path("C:/models")):
        try:
            if probe_dir.is_dir():
                p["diffusers"] += len([d for d in probe_dir.iterdir()
                                       if d.is_dir() and (d / "model_index.json").exists()])
        except Exception:
            pass
    for name, port in (("ollama", 11434), ("strata", 8091)):
        try:
            import socket
            with socket.socket() as s:
                s.settimeout(0.25)
                p[name] = s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            pass
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (out.stdout or "").strip().splitlines()
        if line:
            parts = [x.strip() for x in line[0].split(",")]
            p["gpu"] = parts[0]
            if len(parts) > 1:
                p["vram_gb"] = round(float(parts[1].split()[0]) / 1024, 1)
    except Exception:
        pass
    return p


# ══════════════════════════════════════════════════════════════════
# 候选路径
# ══════════════════════════════════════════════════════════════════
def plan() -> list[dict]:
    """按"最快能跑起来"排序的候选。"""
    p = _probe()
    out = []

    # ① 复用已有 ComfyUI —— 0 GB
    if p["comfyui"]:
        stages = ["image"] + (["mesh"] if p["comfy3d"] else [])
        out.append({
            "key": "reuse-comfyui", "name": "复用现有 ComfyUI",
            "gb": 0.0, "ok": True, "stages": stages,
            "how": f'{p["comfyui"]}',
            "note": ("出图 + 建模都能用（3D-Pack 已装）" if p["comfy3d"]
                     else "只能出图；建模要 3D-Pack 或另装模型"),
        })

    # ② 云端 —— 0 GB
    ck = p["cloud"]
    if any(ck.values()):
        stages = [k for k, v in (("image", ck["image"]), ("mesh", ck["mesh"])) if v]
        out.append({
            "key": "cloud", "name": "走云端 API",
            "gb": 0.0, "ok": bool(stages), "stages": stages,
            "how": "已填的 key",
            "note": "不占显存、不下权重；按量计费由云厂商收",
        })

    # ③ 本地直连（hy3dgen / diffusers 已有权重）—— 0 GB
    if p["hy3dgen"] or p["diffusers"] or p["models"]["mesh"]:
        out.append({
            "key": "reuse-direct", "name": "复用本地直连模型",
            "gb": 0.0, "ok": True,
            "stages": (["mesh"] if (p["hy3dgen"] or p["models"]["mesh"]) else [])
                      + (["image"] if p["diffusers"] else []),
            "how": f'hy3dgen={p["hy3dgen"]} · diffusers={p["diffusers"]} 个',
            "note": "直连模式，不需要 ComfyUI",
        })

    # ④ 最小下载 —— 约 10 GB
    if p["gpu"]:
        out.append({
            "key": "minimal", "name": "下载最小方案",
            "gb": 10.3, "ok": p["vram_gb"] >= 5.5, "stages": ["image", "mesh"],
            "how": f'{p["gpu"]} {p["vram_gb"]}G',
            "note": ("SD1.5 出图 + 2mini 建模 + 2B 对话，约 10 GB；"
                     "下载期间界面已经能用"),
        })

    # ⑤ 兜底：只开界面
    out.append({
        "key": "ui-only", "name": "只开界面",
        "gb": 0.0, "ok": True, "stages": [],
        "how": "不需要任何模型",
        "note": "能看界面、能聊（如果有对话服务），不能出图/建模",
    })
    return out


def _patch_for(key: str) -> dict:
    """每个候选对应的 config 补丁。"""
    p = _probe()
    if key in ("reuse-comfyui", "reuse-direct"):
        mode = "comfyui" if key == "reuse-comfyui" else "direct"
        patch = {"backend": {"mode": mode}}
        if p["comfyui"]:
            patch["paths"] = {"comfyui": p["comfyui"],
                              "models": str(Path(p["comfyui"]) / "models")}
        return patch
    if key == "cloud":
        return {"backend": {"mode": "cloud"}}
    if key == "minimal":
        return {"backend": {"mode": "direct"}}
    return {}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def apply(key: str) -> dict:
    """把某个候选写进 config.json。**不下载任何东西**（下载是另一条流程）。"""
    cands = {c["key"]: c for c in plan()}
    c = cands.get(key)
    if not c:
        return {"ok": False, "error": f"没有这个路径: {key}"}
    if not c["ok"]:
        return {"ok": False, "error": f'这条路径在你这台机器上跑不动：{c["note"]}'}
    if key == "minimal":
        # 下载不在这里做（要几分钟到几十分钟），只把"要下什么"记下来，
        # 由 Studio 的安装流程去执行；这里保证界面先能用。
        return {"ok": True, "key": key, "name": c["name"],
                "need_download": True, "gb": c["gb"], "note": c["note"],
                "stages": c["stages"]}
    healed = heal_defaults()
    cfg = _cfg()
    new = _merge(cfg, _patch_for(key))
    try:
        (ROOT / "config.json").write_text(
            json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"写配置失败: {exc}"}
    return {"ok": True, "key": key, "name": c["name"], "gb": c["gb"],
            "need_download": False, "note": c["note"], "stages": c["stages"],
            "healed": healed}


def auto() -> dict:
    """挑第一个真能用的应用。永远返回 ok=True（最差也能开界面）。"""
    cands = plan()
    tried = []
    for c in cands:
        if not c["ok"]:
            tried.append({"key": c["key"], "why": "跑不动"})
            continue
        if c["stages"] and c["key"] != "ui-only":
            r = apply(c["key"])
            if r.get("ok"):
                r["tried"] = tried
                return r
            tried.append({"key": c["key"], "why": r.get("error", "")})
    # 全都不行 → 界面模式
    return {"ok": True, "key": "ui-only", "name": "只开界面", "gb": 0.0,
            "need_download": False, "stages": [], "tried": tried,
            "note": "没有可用的生成后端，界面仍可用"}


# ══════════════════════════════════════════════════════════════════
# 三条链各自的就绪度
# ══════════════════════════════════════════════════════════════════
def _comfy_models_dir() -> Path | None:
    try:
        c = _probe()["comfyui"]
        return (Path(c) / "models") if c else None
    except Exception:
        return None


def model_ready(kind: str, key: str) -> tuple[bool, str]:
    """某个模型现在到底能不能跑。按 backend 分别判，不搞"一刀切"。"""
    cfg = _cfg()
    cfg_key = "3d" if kind == "mesh" else kind
    mc = (((cfg.get("models") or {}).get(cfg_key) or {}).get(key) or {})
    if not mc:
        return False, "配置里没有这个模型"
    p = _probe()

    if mc.get("cloud"):
        ok = p["cloud"].get(kind if kind != "mesh" else "mesh") is True
        return ok, ("云端已配 key" if ok else "云端没填 key")

    backend = str(mc.get("backend") or "").lower()
    path = str(mc.get("path") or "")
    ckpt = str(mc.get("checkpoint") or mc.get("model") or "")

    # ① 直连 diffusers / hy3dgen：看 path 在不在
    if path:
        if not Path(path).exists():
            return False, f"路径不存在: {path}"
        if backend == "hy3dgen" or kind == "mesh":
            return (p["hy3dgen"], "hy3dgen 未安装" if not p["hy3dgen"] else f"直连 {path}")
        if kind == "image":
            return True, f"diffusers {path}"
        return True, path

    # ② 交给 ComfyUI 的（按文件名）：要求 ComfyUI 在 + 3D-Pack（建模时）+ 文件真在
    if ckpt:
        if not p["comfyui"]:
            return False, "没装 ComfyUI"
        if kind == "mesh" and not p["comfy3d"]:
            return False, "ComfyUI 没装 3D-Pack"
        md = _comfy_models_dir()
        if md and md.is_dir():
            hits = list(md.rglob(ckpt))
            if hits:
                return True, f"ComfyUI {hits[0].name}"
            # 2.1 的权重可能被硬链到子目录，名字带 fp16 等后缀
            stem = Path(ckpt).stem.split("_")[0].lower()
            loose = [x for x in md.rglob("*") if x.is_file() and stem and stem in x.name.lower()]
            if loose:
                return True, f"ComfyUI {loose[0].name}"
            return False, f"权重不在 ComfyUI models 里: {ckpt}"
        return True, f"ComfyUI {ckpt}（目录读不到，按已装算）"

    return False, "没写 path 也没写 checkpoint"


def usable_models(kind: str) -> list[str]:
    """这一阶段所有现在就能跑的模型 key。"""
    cfg = _cfg()
    cfg_key = "3d" if kind == "mesh" else kind
    out = []
    for key in ((cfg.get("models") or {}).get(cfg_key) or {}):
        ok, _how = model_ready(kind, key)
        if ok:
            out.append(key)
    return out


def heal_defaults() -> list[dict]:
    """把默认模型换成**这台机器上真能用**的那个。

    这是"下载完就能用"的关键一步：出厂配置里 `stages.3d.default_model`
    指向质量最好的（hunyuan21，要 7 GB 权重）；如果那台机器没下这个权重，
    用户看到的就是"建模永远失败"，而实际上 hy3dgen 的 2mini 就在本地。
    与其让人排查，不如直接把默认指到能跑的那个。
    """
    cfg = _cfg()
    changes = []
    stages = cfg.setdefault("stages", {})
    for kind, cfg_key in (("image", "image"), ("mesh", "3d")):
        cur = str((stages.get(cfg_key) or {}).get("default_model") or "")
        ok, _how = model_ready(kind, cur) if cur else (False, "未配置")
        if ok:
            continue
        usable = usable_models(kind)
        if not usable:
            continue
        # 优先挑"名字里带 mini / 小"的（省显存），否则第一个
        pick = next((k for k in usable if "mini" in k.lower()), usable[0])
        stages.setdefault(cfg_key, {})["default_model"] = pick
        changes.append({"stage": kind, "from": cur or "(空)", "to": pick,
                        "why": f"{cur or '默认'} 不可用，{pick} 可用"})
    if changes:
        try:
            (ROOT / "config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return []
    return changes


def status() -> dict:
    p = _probe()
    cfg = _cfg()
    stages_cfg = cfg.get("stages") or {}
    models = cfg.get("models") or {}

    def _model_ready(kind: str) -> tuple[bool, str]:
        # config 里建模叫 "3d"，出图叫 "image"。对外说 mesh 更直白，
        # 但读配置必须用 "3d" —— 这里曾经读 stages["mesh"] 导致永远报"没就绪"。
        cfg_key = "3d" if kind == "mesh" else kind
        key = ((stages_cfg.get(cfg_key) or {}).get("default_model") or "")
        mc = ((models.get(cfg_key) or {}).get(key) or {})
        if mc.get("cloud"):
            return (p["cloud"].get(kind) is True,
                    f"云端 {key}" if p["cloud"].get(kind) else f"云端 {key}（没 key）")
        if mc.get("path") and Path(str(mc["path"])).exists():
            return True, f"本地 {key}"
        if mc.get("checkpoint") and p["comfyui"] and p["comfy3d"]:
            return True, f"ComfyUI 3D-Pack · {key}"
        if mc.get("checkpoint") and p["comfyui"]:
            return True, f"ComfyUI {key}"
        if kind == "mesh" and p["hy3dgen"] and mc.get("path"):
            return True, f"hy3dgen {key}"
        if kind == "image" and p["diffusers"]:
            return True, f"diffusers {key}"
        return False, (f"{key or '未配置'} 没就绪")

    _img_key = ((stages_cfg.get("image") or {}).get("default_model") or "")
    _mesh_key = ((stages_cfg.get("3d") or {}).get("default_model") or "")
    img_ok, img_how = model_ready("image", _img_key)
    mesh_ok, mesh_how = model_ready("mesh", _mesh_key)
    chat_how = ("Strata 125B @ 8091" if p["strata"] else
                ("Ollama @ 11434" if p["ollama"] else "没有对话服务"))
    return {
        "machine": {"gpu": p["gpu"], "vram_gb": p["vram_gb"],
                    "comfyui": p["comfyui"], "comfy3d": p["comfy3d"],
                    "hy3dgen": p["hy3dgen"], "diffusers": p["diffusers"],
                    "ollama": p["ollama"], "strata": p["strata"],
                    "cloud": p["cloud"]},
        "stages": {
            "chat": {"ready": p["strata"] or p["ollama"], "how": chat_how},
            "image": {"ready": img_ok, "how": img_how},
            "mesh": {"ready": mesh_ok, "how": mesh_how},
        },
        "candidates": plan(),
        "mode": (cfg.get("backend") or {}).get("mode", ""),
    }
