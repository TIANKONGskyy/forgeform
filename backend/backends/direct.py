# -*- coding: utf-8 -*-
"""DirectBackend —— 不用 ComfyUI，直接从 Python 跑推理。

设计约束（很重要）：

1. **对 `pipeline.py` 零侵入**。它只会调 9 个方法，而且习惯「提交 → 轮询 → 拿 URL 下载」
   这套 ComfyUI 协议。所以这里伪造同一套协议：产物落盘后起一个本地 HTTP 服务，
   返回和 ComfyUI `/view?filename=...` 一样的 URL。
2. **默认不启用**。`config.json → backend.mode` 不写就是 comfyui，行为与以前完全一致。
3. **能力分级**：出图 / 3D 能跑；视频明确不支持（没有可用的独立实现）。

`config.json` 里怎么配：

    "backend": { "mode": "direct" },
    "models": {
      "image": {
        "z_image": { "backend": "z_image", "path": "D:/models/z-image-turbo-diffusers" }
      },
      "3d": {
        "hunyuanmini": { "backend": "hy3dgen", "path": "D:/models/hunyuan3d-2mini",
                         "octree": 256, "steps": 30 }
      }
    },
    "stages": { "image": {"default_model": "z_image"}, "3d": {"default_model": "hunyuanmini"} }

`path` 可以是本地 diffusers 目录，也可以是 HF repo id（那样会走 huggingface_hub 缓存）。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from . import MODE_DIRECT


# backend 字段 → diffusers 类名（插件没写 pipeline 时的兜底）
_PIPELINE_BY_BACKEND = {
    "flux": "FluxPipeline",
    "flux2": "Flux2Pipeline",
    "flux2klein": "Flux2KleinPipeline",
    "flux2_klein": "Flux2KleinPipeline",
    "qwen_image": "QwenImagePipeline",
    "qwen": "QwenImagePipeline",
    "z_image": "ZImagePipeline",
    "sdxl": "StableDiffusionXLPipeline",
    "sd": "StableDiffusionPipeline",
    "sd_turbo": "StableDiffusionPipeline",
}


# ────────────────────────────────────────────────────────────────
# 本地产物 HTTP 服务：伪造 ComfyUI 的 /view?filename=
# ────────────────────────────────────────────────────────────────
class _ArtifactServer:
    """把产物目录暴露成 http://127.0.0.1:{port}/view?filename=... 。

    为什么不用 file:// —— pipeline 里是 `requests.get(url)` 下载，requests 不认 file://。
    为什么不用改 pipeline —— 返回值形状和 ComfyUI 一致，业务层就完全不用知道后端换了。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        root = self.root

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(root), **kw)

            def do_GET(self):  # noqa: N802
                # /view?filename=images%2Fx.png → 在 root 下按相对路径找
                if self.path.startswith("/view"):
                    qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                    name = ""
                    for part in qs.split("&"):
                        if part.startswith("filename="):
                            name = part[len("filename="):]
                    from urllib.parse import unquote
                    self.path = "/" + unquote(name)
                return super().do_GET()

            def log_message(self, *_args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def url_for(self, path: Path) -> str:
        self.start()
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            rel = p.name
        return f"http://127.0.0.1:{self.port}/view?filename={quote(rel, safe='/')}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None


# ────────────────────────────────────────────────────────────────
# 模型加载器：同一时刻只留一个，切换先卸载
# ────────────────────────────────────────────────────────────────
class _ModelKeeper:
    """显存是分时的。这里保证任何时刻只有一个 diffusers / hy3dgen 模型在显存里。"""

    def __init__(self, log=print):
        self._obj = None
        self._key = ""
        self.log = log

    @property
    def key(self) -> str:
        return self._key

    def get(self, key: str, builder):
        if self._obj is not None and self._key == key:
            return self._obj
        self.unload()
        self.log(f"[direct] 加载 {key}")
        self._obj = builder()
        self._key = key
        return self._obj

    def unload(self) -> None:
        if self._obj is None:
            return
        key = self._key
        try:
            del self._obj
        finally:
            self._obj = None
            self._key = ""
        try:
            import gc
            import torch
            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass
        self.log(f"[direct] 已卸载 {key}")


# ────────────────────────────────────────────────────────────────
# 主类
# ────────────────────────────────────────────────────────────────
class DirectBackend:
    """不依赖 ComfyUI 的后端。接口与 ComfyUIClient 等价。"""

    name = "direct"

    def __init__(self, config: dict):
        self.config = config or {}
        self._paths = self.config.get("paths") or {}
        out = Path(self._paths.get("output") or "outputs")
        self.out_root = out
        for sub in ("images", "videos", "3d"):
            (out / sub).mkdir(parents=True, exist_ok=True)

        self._server = _ArtifactServer(out)
        self._keeper = _ModelKeeper(log=self._log)
        self._jobs: dict[str, list[Path]] = {}
        self._lock = threading.Lock()
        self._current: dict | None = None   # 正在执行的 spec（interrupt 用）
        self._interrupted = False

    # ── 基础 ──────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    def is_running(self) -> bool:
        # 没有外部服务要探活；能 import 出推理库就算可用
        try:
            import torch  # noqa: F401
            return True
        except Exception:
            return False

    def interrupt(self) -> bool:
        self._interrupted = True
        return True

    def get_gpu_temp(self, gpu_index: int | None = None):
        try:
            import subprocess
            q = "temperature.gpu"
            if gpu_index is not None:
                q = f"temperature.gpu,index"
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=6,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            vals = [int(x) for x in out.stdout.strip().splitlines() if x.strip().isdigit()]
            if gpu_index is not None and gpu_index < len(vals):
                return vals[gpu_index]
            return max(vals) if vals else None
        except Exception:
            return None

    def upload_image(self, image_path: str, stage: str = "image") -> str:
        """ComfyUI 那边要上传到 input 目录；这边不需要，原样返回路径。"""
        p = Path(image_path)
        if not p.exists():
            raise FileNotFoundError(f"图片不存在: {image_path}")
        return str(p)

    # ── 伪造的作业协议 ────────────────────────────────────────
    def build_workflow(self, stage_name: str, params: dict) -> dict:
        return {"__direct__": True, "stage": stage_name, "params": dict(params or {})}

    def submit_workflow(self, workflow_data: dict, model_config: dict | None = None,
                        stage: str | None = None) -> str:
        """同步执行推理，产物落盘，返回一个 job id。"""
        stage_name = workflow_data.get("stage") or stage or "image"
        params = workflow_data.get("params") or {}
        self._interrupted = False
        self._current = {"stage": stage_name, "params": params}

        job_id = uuid.uuid4().hex
        try:
            if stage_name == "image":
                produced = self._run_image(params)
            elif stage_name == "3d":
                produced = self._run_3d(params)
            elif stage_name == "video":
                raise NotImplementedError(
                    "DirectBackend 不支持视频：MiniMax H3 目前只有 ComfyUI 节点实现。"
                    "用 config.json → backend.mode = \"comfyui\" 跑视频，或关掉视频阶段。")
            else:
                raise ValueError(f"未知阶段: {stage_name}")
        finally:
            self._current = None

        with self._lock:
            self._jobs[job_id] = produced
        return job_id

    def wait_for_completion(self, prompt_id: str, timeout: int = 600,
                            progress_callback=None, thermal_guard: bool = False) -> dict:
        # 推理在 submit 阶段已经同步做完了，这里只把产物吐出去
        files = self._jobs.get(prompt_id) or []
        return {"outputs": {"__files__": [str(p) for p in files]},
                "status": {"status_str": "success" if files else "error"}}

    def _first(self, outputs: dict) -> str:
        files = (outputs or {}).get("__files__") or []
        if not files:
            return ""
        p = Path(files[0])
        if not p.exists():
            return ""
        return self._server.url_for(p)

    def get_result_image(self, outputs: dict, stage: str | None = None) -> str:
        return self._first(outputs)

    def get_result_video(self, outputs: dict, stage: str | None = None) -> str:
        return self._first(outputs)

    def get_result_3d(self, outputs: dict) -> str:
        return self._first(outputs)

    # ── 模型解析：config 优先，插件补默认 ────────────────────
    def _resolve(self, stage: str, params: dict) -> tuple[str, dict, object]:
        """返回 (key, 合并后的参数, 插件或 None)。

        优先级：`config.json → models.<stage>.<key>` 显式字段 > 插件 YAML 的 defaults。
        所以 config 里只写 `{"backend": "z_image", "path": "..."}` 就够，
        步数/guidance/分辨率这些从插件继承 —— 换模型时不用把参数抄一遍。
        """
        from ..plugins.spec import resolve as find_plugin

        kind = "mesh" if stage == "3d" else stage
        stages = self.config.get("stages") or {}
        key = params.get("model_key") or (stages.get(stage) or {}).get("default_model") or ""
        table = (self.config.get("models") or {}).get(stage) or {}

        cfg = dict(table.get(key) or {}) if key in table else {}
        plugin = find_plugin(kind, key) if key else None

        if plugin is None and not cfg and table:
            key = next(iter(table))
            cfg = dict(table[key])
            plugin = find_plugin(kind, key)

        if not key:
            known = ", ".join(p.id for p in __import__("backend.plugins.spec",
                                                       fromlist=["registry"]).registry().of_kind(kind))
            raise RuntimeError(
                f"没有可用的 {stage} 模型：config.models.{stage} 是空的，"
                f"内置插件也没有匹配项（已知: {known or '无'}）")

        merged = dict(plugin.params()) if plugin else {}
        for k, v in cfg.items():
            if v not in (None, "", 0):
                merged[k] = v
        return key, merged, plugin

    def _pick(self, stage: str, params: dict) -> tuple[str, dict]:
        key, merged, _plugin = self._resolve(stage, params)
        return key, merged

    def _run_image(self, params: dict) -> list[Path]:
        import torch

        key, opts, plugin = self._resolve("image", params)
        cfg = (self.config.get("models") or {}).get("image", {}).get(key, {}) or {}
        ref = cfg.get("path") or cfg.get("repo") or (plugin.target if plugin else "")
        if not ref:
            raise RuntimeError(
                f"模型 {key} 没给 path/repo：config.models.image.{key}.path 或插件的 repo 至少要有一个")

        steps = int(params.get("steps") or opts.get("steps") or 20)
        w = int(opts.get("width") or 1024)
        h = int(opts.get("height") or 1024)
        guidance = opts.get("guidance")
        prompt = params.get("prompt") or ""
        negative = params.get("negative") or ""
        seed = int(params.get("seed") or -1)

        pipe_cls_name = (cfg.get("pipeline") or (plugin.pipeline if plugin else "")
                         or _PIPELINE_BY_BACKEND.get((cfg.get("backend") or "").lower(), "FluxPipeline"))

        def build():
            return self._load_pipe(pipe_cls_name, ref, opts.get("dtype") or "bfloat16")

        pipe = self._keeper.get(f"image:{pipe_cls_name}:{ref}", build)
        gen = None
        if seed >= 0:
            gen = torch.Generator(device="cuda").manual_seed(seed)

        kwargs = dict(prompt=prompt, num_inference_steps=steps, width=w, height=h)
        if gen is not None:
            kwargs["generator"] = gen
        if params.get("guidance_scale") is not None:
            kwargs["guidance_scale"] = float(params["guidance_scale"])
        elif guidance is not None:
            kwargs["guidance_scale"] = float(guidance)
        if negative and "guidance_scale" in kwargs and float(kwargs["guidance_scale"]) > 1.0:
            kwargs["negative_prompt"] = negative

        self._log(f"[direct] 出图 {key} ({pipe_cls_name}) {w}x{h} {steps}步 …")
        t0 = time.time()
        out = pipe(**kwargs)
        img = out.images[0]
        name = f"direct_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        path = self.out_root / "images" / name
        img.save(path)
        self._log(f"[direct] 出图完成 {round(time.time()-t0,1)}s → {name}")
        return [path]

    def _load_pipe(self, cls_name: str, ref: str, dtype_name: str = "bfloat16"):
        """按插件声明的类名从 diffusers 里拿 pipeline。适配器留给不兼容的模型。"""
        import diffusers
        import torch

        dtype = getattr(torch, dtype_name, torch.bfloat16)
        cls = getattr(diffusers, cls_name, None)
        if cls is None:
            raise RuntimeError(f"diffusers 里没有 {cls_name}（版本 {diffusers.__version__}）"
                               f"—— 检查插件 YAML 的 pipeline 字段，或升级 diffusers")
        self._log(f"[direct] 加载 {cls_name} ← {ref}")
        try:
            pipe = cls.from_pretrained(ref, torch_dtype=dtype, use_safetensors=True)
        except Exception:
            # 有些社区模型只有 pytorch_model.bin（没转 safetensors）
            pipe = cls.from_pretrained(ref, torch_dtype=dtype, use_safetensors=False)
        pipe.to("cuda")
        for opt in ("enable_attention_slicing", "enable_vae_slicing"):
            try:
                getattr(pipe, opt)()
            except Exception:
                pass
        return pipe

    # ── 3D ────────────────────────────────────────────────────
    def _run_3d(self, params: dict) -> list[Path]:
        import torch
        from PIL import Image

        key, opts, plugin = self._resolve("3d", params)
        cfg = (self.config.get("models") or {}).get("3d", {}).get(key, {}) or {}
        ref = cfg.get("path") or cfg.get("repo") or (plugin.target if plugin else "")
        if not ref:
            raise RuntimeError(
                f"模型 {key} 没给 path/repo：config.models.3d.{key}.path 或插件的 repo 至少要有一个")

        loader = str(cfg.get("loader") or (plugin.loader if plugin else "") or
                     "hy3dgen.shapegen.Hunyuan3DDiTFlowMatchingPipeline")

        img_path = params.get("image_path") or ""
        if not img_path or not Path(img_path).exists():
            raise FileNotFoundError(f"输入图片不存在: {img_path}")
        image = Image.open(img_path).convert("RGBA")

        steps = int(params.get("steps") or opts.get("steps") or 30)
        octree = int(params.get("octree_resolution") or opts.get("octree") or 256)
        guidance = float(opts.get("guidance") or 6.0)
        dtype = getattr(torch, str(opts.get("dtype") or "float16"), torch.float16)

        def build():
            mod, _, cls = loader.rpartition(".")
            if not mod:
                raise RuntimeError(f"loader 要写完整路径，例如 hy3dgen.shapegen.XXX: {loader}")
            self._log(f"[direct] 加载 {cls} ← {ref}")
            M = __import__(mod, fromlist=[cls])
            return getattr(M, cls).from_pretrained(ref, device="cuda", dtype=dtype)

        pipe = self._keeper.get(f"3d:{loader}:{ref}", build)
        self._log(f"[direct] 3D 生成 {key} steps={steps} octree={octree} …")
        t0 = time.time()
        meshes = pipe(image=image, num_inference_steps=steps,
                      guidance_scale=guidance, octree_resolution=octree,
                      num_chunks=8000, output_type="trimesh")
        mesh = meshes[0] if isinstance(meshes, (list, tuple)) else meshes
        name = f"direct3d_{int(time.time())}_{uuid.uuid4().hex[:6]}.glb"
        path = self.out_root / "3d" / name
        mesh.export(str(path))
        self._log(f"[direct] 3D 完成 {round(time.time()-t0,1)}s "
                  f"faces={len(mesh.faces)} → {name}")
        return [path]

    # ── 生命周期 ──────────────────────────────────────────────
    def close(self) -> None:
        self._keeper.unload()
        self._server.stop()
