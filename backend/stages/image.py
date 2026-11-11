"""Image Generation Stage - FLUX 图片生成"""
import shutil
from pathlib import Path
from typing import Dict
from .base import Stage, StageResult


class ImageStage(Stage):
    @property
    def name(self):
        return "image"
    
    @property
    def display_name(self):
        return "图片生成"
    
    @property
    def icon(self):
        return "🎨"
    
    def build_workflow(self, params: Dict) -> Dict:
        """构建 FLUX 图像生成工作流"""
        return self.client.build_workflow("image", params)
    
    def process_output(self, outputs: Dict, params: Dict) -> StageResult:
        """从 ComfyUI 输出提取图片"""
        image_url = self.client.get_result_image(outputs)
        if image_url:
            # 下载到本地
            output_dir = Path(self.config["paths"]["output"]) / "images"
            output_dir.mkdir(parents=True, exist_ok=True)
            
            import requests
            response = requests.get(image_url, timeout=60)
            if response.status_code == 200:
                output_path = output_dir / f"img_{hash(image_url) % 100000:05d}.png"
                with open(output_path, "wb") as f:
                    f.write(response.content)
                return StageResult(True, str(output_path))
        
        return StageResult(False, error="未找到输出图像")
