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

    # ── 出图 ──────────────────────────────────────────────────
    def _pick(self, stage: str, params: dict) -> tuple[str, dict]:
        """按 config 找回这一轮该用哪个模型。"""
        stages = self.config.get("stages") or {}
        default_key = (stages.get(stage) or {}).get("default_model", "")
        key = params.get("model_key") or default_key
        table = (self.config.get("models") or {}).get(stage) or {}
        if key and key in table:
            return key, table[key]
        if default_key and default_key in table:
            return default_key, table[default_key]
        if table:
            k = next(iter(table))
            return k, table[k]
        raise RuntimeError(f"config.models.{stage} 是空的，没法选模型")

    def _run_image(self, params: dict) -> list[Path]:
        import torch
        key, cfg = self._pick("image", params)
        model_ref = cfg.get("path") or cfg.get("repo") or cfg.get("model_id")
        if not model_ref:
            raise RuntimeError(
                f"models.image.{key} 缺 path：DirectBackend 需要本地 diffusers 目录或 HF repo id")

        kind = (cfg.get("backend") or "flux").lower()
        steps = int(params.get("steps") or cfg.get("steps") or 20)
        w = int(params.get("width") or 1024)
        h = int(params.get("height") or 1024)
        prompt = params.get("prompt") or ""
        negative = params.get("negative") or ""
        seed = int(params.get("seed") or -1)

        def build():
            return self._load_image_pipe(kind, model_ref, cfg)

        pipe = self._keeper.get(f"image:{kind}:{model_ref}", build)
        gen = None
        if seed >= 0:
            gen = torch.Generator(device="cuda").manual_seed(seed)

        kwargs = dict(prompt=prompt, num_inference_steps=steps, width=w, height=h)
        if gen is not None:
            kwargs["generator"] = gen
        # turbo 系（z_image / klein）guidance 要低
        if kind in ("z_image", "flux2klein", "flux2_klein"):
            kwargs["guidance_scale"] = float(cfg.get("guidance") or 1.0)
        elif negative:
            kwargs["negative_prompt"] = negative

        self._log(f"[direct] 出图 {key} {w}x{h} {steps}步 …")
        t0 = time.time()
        out = pipe(**kwargs)
        img = out.images[0]
        name = f"direct_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        path = self.out_root / "images" / name
        img.save(path)
        self._log(f"[direct] 出图完成 {round(time.time()-t0,1)}s → {name}")
        return [path]

    def _load_image_pipe(self, kind: str, ref: str, cfg: dict):
        import torch
        dtype = torch.bfloat16
        common = dict(torch_dtype=dtype, use_safetensors=True)
        if kind == "z_image":
            from diffusers import ZImagePipeline as P
        elif kind in ("qwen_image", "qwen"):
            from diffusers import QwenImagePipeline as P
        elif kind in ("flux2klein", "flux2_klein"):
            from diffusers import Flux2KleinPipeline as P
        elif kind in ("flux2",):
            from diffusers import Flux2Pipeline as P
        else:
            from diffusers import FluxPipeline as P
        pipe = P.from_pretrained(ref, **common)
        pipe.to("cuda")
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass
        return pipe

    # ── 3D ────────────────────────────────────────────────────
    def _run_3d(self, params: dict) -> list[Path]:
        from PIL import Image

        key, cfg = self._pick("3d", params)
        ref = cfg.get("path") or cfg.get("repo") or cfg.get("model_id")
        if not ref:
            raise RuntimeError(
                f"models.3d.{key} 缺 path：DirectBackend 需要 hy3dgen 能认的目录或 repo id")

        img_path = params.get("image_path") or ""
        if not img_path or not Path(img_path).exists():
            raise FileNotFoundError(f"输入图片不存在: {img_path}")
        image = Image.open(img_path).convert("RGBA")

        steps = int(params.get("steps") or cfg.get("steps") or 30)
        octree = int(params.get("octree_resolution") or cfg.get("octree") or 256)
        guidance = float(cfg.get("guidance_scale") or 6.0)

        def build():
            from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline as P
            self._log(f"[direct] 加载 hy3dgen {ref}")
            return P.from_pretrained(ref, device="cuda", dtype=__import__("torch").float16)

        pipe = self._keeper.get(f"3d:{ref}", build)
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
