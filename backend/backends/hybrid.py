# -*- coding: utf-8 -*-
"""HybridBackend —— 按阶段自动挑后端。

用户那句话是这么说的：「对面设备有 Ollama / vLLM / ComfyUI 的话，里面有模型就自动复用；
没有就把必须的框架装上，非必要的用现有这套（直连）」。

所以这个后端的职责是**路由**，不是推理：

    出图 → ComfyUI 活着且有出图权重？用它（省得重下）
           否则本地有 diffusers 模型 → 直连
    建模 → ComfyUI + 3D-Pack？用它
           否则 hy3dgen 有权重 → 直连
    对话 → Ollama / llama.cpp / vLLM / LM Studio 谁活着用谁
           都没有 → 关掉（纯规则模式）

它对外暴露和 `ComfyUIClient` 完全一样的方法，`pipeline.py` 一行都不用改。
"""
from __future__ import annotations

import threading

from . import MODE_HYBRID
from . import detect as D

# 探测结果只打印一次 —— 每次 `Pipeline()` 实例化都会新建一个 HybridBackend，
# 一个进程里可能有 3-4 个（app 一个、脚本一个、自检一个），日志会刷好几遍同样五条。
_LOGGED = False


class HybridBackend:
    """按 stage 把请求分派给不同的实现。"""

    name = "hybrid"

    def __init__(self, config: dict):
        self.config = config or {}
        paths = self.config.get("paths") or {}
        comfy_dir = paths.get("comfyui") or ""

        cfg_backend = self.config.get("backend") or {}
        hosts = []
        if isinstance(cfg_backend, dict):
            hosts = cfg_backend.get("probe_hosts") or []
        self.hosts = hosts or ["127.0.0.1"]

        # 探测（很快：只发 GET、2.5 秒超时、绝不拉起服务）
        self.frameworks = D.probe_all(comfy_dir=comfy_dir or None, hosts=self.hosts)
        self.plan = D.recommend(self.frameworks)

        global _LOGGED
        if not _LOGGED:
            _LOGGED = True
            self._log("[hybrid] 探测结果:\n" + D.describe(self.frameworks))
            self._log("[hybrid] 路由: " + ", ".join(f"{k}→{v}" for k, v in self.plan.items()))

        self._comfy = None
        self._direct = None
        self._lock = threading.Lock()
        self._route: dict[str, object] = {}      # job_id → 真正干活的 backend

    # ── 内部 ──────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    def _stage_kind(self, stage: str) -> str:
        return "mesh" if stage in ("3d", "mesh") else stage

    def _get_comfy(self):
        if self._comfy is None:
            from ..comfy_client import ComfyUIClient
            self._comfy = ComfyUIClient(self.config)
        return self._comfy

    def _get_direct(self):
        if self._direct is None:
            from .direct import DirectBackend
            cfg = dict(self.config)
            # 直连模式要自己的输出根，避免和 ComfyUI 模式互相覆盖
            self._direct = DirectBackend(cfg)
        return self._direct

    def pick(self, stage: str):
        """返回该阶段实际要用的后端实例。"""
        kind = self._stage_kind(stage)
        choice = self.plan.get(kind, "")
        if choice == "comfyui":
            return self._get_comfy()
        if choice == "direct":
            return self._get_direct()
        if choice == "comfyui-missing":
            raise RuntimeError(
                "这台机器既没有可用的 ComfyUI，也没有本地 diffusers 模型。\n"
                "两条路选一条：\n"
                "  1) 装 ComfyUI 并把权重放进 models/（见 docs/MODELS.md）\n"
                "  2) 用 python tools/fetch_model.py 拉一个 diffusers 模型（见 docs/BACKENDS.md）")
        # 没探测到：按 config 里的 mode 兜底，再不行报错
        raise RuntimeError(
            f"{kind} 阶段没有可用后端（探测结果: {self.plan}）。"
            f"检查 docs/BACKENDS.md，或显式在 config.json 里指定 backend.mode")

    # ── 与 ComfyUIClient 对齐的接口 ───────────────────────────
    def is_running(self) -> bool:
        try:
            return bool(self.pick("image").is_running())
        except Exception:
            # 出图不行也还有别的能跑，不要因此让整个 app 起不来
            return any(f.alive for f in self.frameworks)

    def interrupt(self) -> bool:
        ok = False
        for b in (self._comfy, self._direct):
            if b is None:
                continue
            try:
                ok = bool(b.interrupt()) or ok
            except Exception:
                pass
        return ok

    def get_gpu_temp(self, gpu_index: int | None = None):
        for b in (self._comfy, self._direct):
            if b is None:
                continue
            try:
                t = b.get_gpu_temp(gpu_index)
                if t:
                    return t
            except Exception:
                pass
        return None

    def upload_image(self, image_path: str, stage: str = "image") -> str:
        return self.pick(stage).upload_image(image_path, stage=stage)

    # ── 兼容层：ComfyUIClient 的私有方法，业务代码里有几处直接调 ──
    def _pick_url(self, stage: str | None = None) -> str:
        """`conversation._warm_image_model_async` 会用它拼 `/queue` 的地址。

        ⚠️ 这个方法**必须存在**。少了它时 AttributeError 会被外面那层
        `except: pass` 吞掉，于是"队列忙就跳过预热"的分支失效 ——
        表现是预热照常提交（干净 clone 里 verify 的 warmup_deduped 就是这么红的，
        开发机上因为 config 走的是 comfyui 模式才碰巧过）。
        """
        try:
            return self._get_comfy()._pick_url(stage)
        except Exception:
            return "http://127.0.0.1:8188"

    def build_workflow(self, stage_name: str, params: dict) -> dict:
        wf = self.pick(stage_name).build_workflow(stage_name, params)
        if isinstance(wf, dict):
            wf.setdefault("__stage__", stage_name)
        return wf

    def submit_workflow(self, workflow_data: dict, model_config: dict | None = None,
                        stage: str | None = None) -> str:
        stage_name = stage or (workflow_data or {}).get("__stage__") or "image"
        backend = self.pick(stage_name)
        pid = backend.submit_workflow(workflow_data, model_config=model_config, stage=stage)
        with self._lock:
            self._route[pid] = backend
        return pid

    def _owner(self, prompt_id: str):
        with self._lock:
            return self._route.get(prompt_id)

    def wait_for_completion(self, prompt_id: str, timeout: int = 600,
                            progress_callback=None, thermal_guard: bool = False) -> dict:
        backend = self._owner(prompt_id)
        if backend is None:
            raise RuntimeError(f"不认识的 job: {prompt_id}（可能来自另一个进程）")
        try:
            return backend.wait_for_completion(prompt_id, timeout=timeout,
                                               progress_callback=progress_callback,
                                               thermal_guard=thermal_guard)
        finally:
            with self._lock:
                self._route.pop(prompt_id, None)

    def get_result_image(self, outputs: dict, stage: str | None = None) -> str:
        for b in (self._direct, self._comfy):
            if b is None:
                continue
            try:
                got = b.get_result_image(outputs, stage=stage)
                if got:
                    return got
            except Exception:
                pass
        return ""

    def get_result_video(self, outputs: dict, stage: str | None = None) -> str:
        for b in (self._comfy, self._direct):
            if b is None:
                continue
            try:
                got = b.get_result_video(outputs, stage=stage)
                if got:
                    return got
            except Exception:
                pass
        return ""

    def get_result_3d(self, outputs: dict) -> str:
        for b in (self._comfy, self._direct):
            if b is None:
                continue
            try:
                got = b.get_result_3d(outputs)
                if got:
                    return got
            except Exception:
                pass
        return ""

    # ── 给 doctor / UI 用 ─────────────────────────────────────
    def report(self) -> list[dict]:
        return [f.to_dict() for f in self.frameworks]

    def close(self) -> None:
        for b in (self._comfy, self._direct):
            if b is not None and hasattr(b, "close"):
                try:
                    b.close()
                except Exception:
                    pass
