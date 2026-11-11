"""Model Manager — track loaded models, preload/unload for efficiency."""
import time
import urllib.request
import json
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ModelStage(Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    LOADED = "loaded"
    ACTIVE = "active"     # currently computing
    UNLOADING = "unloading"


@dataclass
class ModelInfo:
    """Track a model's lifecycle."""
    name: str
    stage: str  # "text_to_image", "image_to_video", "image_to_3d"
    checkpoint: str
    lora: str = ""
    state: ModelStage = ModelStage.UNLOADED
    vram_mb: int = 0       # estimated VRAM usage
    load_time_s: float = 0
    last_used: float = 0
    use_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "stage": self.stage,
            "checkpoint": self.checkpoint,
            "lora": self.lora,
            "state": self.state.value,
            "vram_mb": self.vram_mb,
            "load_time_s": self.load_time_s,
            "last_used": self.last_used,
            "use_count": self.use_count,
        }


class ModelManager:
    """Manages model lifecycle for efficient VRAM usage.

    Strategy:
    - Track which models are loaded in ComfyUI
    - When switching stages, signal the old model to unload
    - Preload the next stage's model while current is finishing
    - ComfyUI handles actual VRAM; we track state for UI display
    """

    # Model registry with VRAM estimates
    REGISTRY = {
        "flux_fp8": ModelInfo(
            name="FLUX Dev FP8", stage="text_to_image",
            checkpoint="flux1-dev-fp8.safetensors",
            lora="Hyper-FLUX.1-dev-8steps-V1-lora.safetensors",
            vram_mb=8500,
        ),
        "flux_fp16": ModelInfo(
            name="FLUX Dev FP16", stage="text_to_image",
            checkpoint="flux1-dev.safetensors",
            lora="Hyper-FLUX.1-dev-8steps-V1-lora.safetensors",
            vram_mb=14000,
        ),
        "h3_turbo": ModelInfo(
            name="MiniMax H3 Turbo", stage="image_to_video",
            checkpoint="minimax_h3_fp8.safetensors",
            lora="minimax_h3_turbo_v4.safetensors",
            vram_mb=12000,
        ),
        "hy3d_turbo": ModelInfo(
            name="Hunyuan3D v2 Turbo", stage="image_to_3d",
            checkpoint="hunyuan3d-dit-v2-turbo",
            vram_mb=6000,
        ),
        "hy3d_full": ModelInfo(
            name="Hunyuan3D v2 Full", stage="image_to_3d",
            checkpoint="hunyuan3d-dit-v2",
            vram_mb=8000,
        ),
    }

    def __init__(self):
        self.models: dict[str, ModelInfo] = {}
        self.active_model: Optional[str] = None
        self._preload_queue: list[str] = []
        # Initialize from registry
        for k, v in self.REGISTRY.items():
            self.models[k] = ModelInfo(
                name=v.name, stage=v.stage,
                checkpoint=v.checkpoint, lora=v.lora,
                vram_mb=v.vram_mb,
            )

    def mark_loading(self, model_key: str):
        if model_key in self.models:
            self.models[model_key].state = ModelStage.LOADING

    def mark_loaded(self, model_key: str, load_time: float = 0):
        if model_key in self.models:
            m = self.models[model_key]
            m.state = ModelStage.LOADED
            m.load_time_s = load_time
            m.last_used = time.time()

    def mark_active(self, model_key: str):
        # Unload previous
        if self.active_model and self.active_model != model_key:
            self.mark_unloaded(self.active_model)
        if model_key in self.models:
            self.models[model_key].state = ModelStage.ACTIVE
            self.models[model_key].use_count += 1
            self.models[model_key].last_used = time.time()
            self.active_model = model_key

    def mark_unloaded(self, model_key: str):
        if model_key in self.models:
            self.models[model_key].state = ModelStage.UNLOADED

    def queue_preload(self, model_key: str):
        """Queue a model for preloading after current finishes."""
        if model_key not in self._preload_queue:
            self._preload_queue.append(model_key)

    def get_preload_next(self) -> Optional[str]:
        if self._preload_queue:
            return self._preload_queue.pop(0)
        return None

    def get_status_html(self) -> str:
        """Render model status as HTML cards."""
        stage_groups = {}
        for k, m in self.models.items():
            stage_groups.setdefault(m.stage, []).append((k, m))

        stage_labels = {
            "text_to_image": "🎨 文字→图片",
            "image_to_video": "🎬 图片→视频",
            "image_to_3d": "🧊 图片→3D",
        }

        html_parts = []
        for stage_key in ["text_to_image", "image_to_video", "image_to_3d"]:
            items = stage_groups.get(stage_key, [])
            html_parts.append(f'<div class="mm-stage-label">{stage_labels.get(stage_key, stage_key)}</div>')
            for k, m in items:
                state_cls = f"mm-{m.state.value}"
                state_icon = {
                    "unloaded": "○",
                    "loading": "◌",
                    "loaded": "●",
                    "active": "◉",
                    "unloading": "◐",
                }.get(m.state.value, "·")
                state_text = {
                    "unloaded": "未加载",
                    "loading": "加载中...",
                    "loaded": "就绪",
                    "active": "运算中",
                    "unloading": "卸载中...",
                }.get(m.state.value, m.state.value)
                vram = f"{m.vram_mb / 1024:.1f}GB" if m.vram_mb else "-"
                uses = f"×{m.use_count}" if m.use_count else ""
                html_parts.append(
                    f'<div class="mm-card {state_cls}">'
                    f'<span class="mm-dot">{state_icon}</span>'
                    f'<span class="mm-name">{m.name}</span>'
                    f'<span class="mm-vram">{vram}</span>'
                    f'<span class="mm-uses">{uses}</span>'
                    f'<span class="mm-state">{state_text}</span>'
                    f'</div>'
                )
        return "".join(html_parts)

    def get_checkpoint(self, model_key: str) -> tuple[str, str]:
        """Return (checkpoint, lora) for a model key."""
        m = self.models.get(model_key)
        if m:
            return m.checkpoint, m.lora
        return "", ""


# Global singleton
_manager = ModelManager()


def get_manager() -> ModelManager:
    return _manager
