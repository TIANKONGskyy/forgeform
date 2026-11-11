"""Stage base class - Pipeline 中的一个环节"""
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Dict

from backend.logger import get_logger


class StageResult:
    """Stage 执行结果"""
    def __init__(self, success: bool, output_path: str = "",
                 metadata: dict = None, error: str = "",
                 duration_s: float = 0):
        self.success = success
        self.output_path = output_path
        self.metadata = metadata or {}
        self.error = error
        self.duration_s = duration_s

    def __repr__(self):
        status = "✅" if self.success else "❌"
        return f"StageResult({status} {self.output_path or self.error[:60]})"


class Stage(ABC):
    """Pipeline Stage 基类"""
    
    def __init__(self, config: dict, client):
        """
        Args:
            config: 完整 config.json dict
            client: ComfyUIClient 实例
        """
        self.config = config
        self.client = client
        self.log = get_logger()
    
    @property
    @abstractmethod
    def name(self) -> str:
        """唯一标识：'image', 'video', 'threed'"""
        ...
    
    @property
    @abstractmethod
    def display_name(self) -> str:
        """UI 显示名：'图片生成'"""
        ...
    
    def get_model_config(self, model_key: str = None) -> dict:
        """获取模型配置"""
        models = self.config.get("models", {}).get(self.name, {})
        if model_key and model_key in models:
            return models[model_key]
        # 返回默认（第一个）
        #
        # ★ 2026-10-15 修「一个"默认模型"有两份配置在打架」。
        #
        #   原来这里只读 `generation.default_<stage>_model`，而对话那条路
        #   （conversation.py / describer.py）读的是 `stages.<stage>.default_model`。
        #   于是同一件事有两个答案：
        #       generation.default_image_model = "z_image"
        #       stages.image.default_model     = "qwen21_ref"
        #   不传 model_key 的调用拿到 z_image，传了的拿到 qwen21_ref —— 出图模型
        #   取决于走哪条路，纯看运气。3d 也一样（triposplat vs hunyuan21），
        #   实测就是它让 auto_params 记成 triposplat、实际跑的却是 hunyuan21。
        #
        #   `stages.*.default_model` 是页面和对话都在用的那个，以它为准；
        #   `generation.default_*` 保留为**遗留配置的兜底**（老 config 没有 stages 键时用）。
        default_key = (
            self.config.get("stages", {}).get(self.name, {}).get("default_model")
            or self.config.get("generation", {}).get(f"default_{self.name}_model")
        )
        if default_key and default_key in models:
            return models[default_key]
        if models:
            return list(models.values())[0]
        raise RuntimeError(f"No models configured for stage '{self.name}'")
    
    def list_models(self) -> list:
        """返回 [(key, display_name)]"""
        models = self.config.get("models", {}).get(self.name, {})
        result = []
        for key, m in models.items():
            result.append((key, m.get("name", key)))
        return result
    
    def execute(self, model_key: str = None, progress_callback=None,
                **kwargs) -> StageResult:
        """执行 Stage，支持自动 fallback"""
        t0 = time.time()
        
        try:
            if progress_callback:
                progress_callback(f"[{self.display_name}] 开始执行...")
            
            # 构建参数
            params = {**kwargs}
            params["model_config"] = self.get_model_config(model_key)
            params["model_key"] = model_key
            
            # 构建并提交工作流
            workflow_data = self.build_workflow(params)
            
            if progress_callback:
                progress_callback(f"[{self.display_name}] 提交到 ComfyUI...")
            
            prompt_id = self.client.submit_workflow(workflow_data)
            
            if progress_callback:
                progress_callback(f"[{self.display_name}] 等待完成...")
            
            result = self.client.wait_for_completion(
                prompt_id,
                timeout=600,
                progress_callback=progress_callback
            )
            
            # 处理输出
            output = self.process_output(result.get("outputs", {}), params)
            
            dur = time.time() - t0
            if output.success:
                self.log.log(self.name, "complete", "completed", 
                           duration_s=dur, output=output.output_path)
            else:
                self.log.log(self.name, "complete", "failed", 
                           duration_s=dur, error=output.error)
            
            return output
            
        except Exception as e:
            dur = time.time() - t0
            self.log.log(self.name, "execute", "failed", 
                        error=str(e), duration_s=dur)
            return StageResult(False, error=str(e), duration_s=dur)
    
    @abstractmethod
    def build_workflow(self, params: Dict) -> Dict:
        """构建 ComfyUI 工作流
        
        Args:
            params: 包含 model_config, prompt, 等参数
            
        Returns:
            {"prompt": {...}, "negative_prompt": "..."}
        """
        ...
    
    @abstractmethod
    def process_output(self, outputs: Dict, params: Dict) -> StageResult:
        """从 ComfyUI 输出中提取结果"""
        ...
