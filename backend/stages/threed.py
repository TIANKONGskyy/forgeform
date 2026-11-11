"""3D generation stage — Hunyuan3D V2 models via Comfy3D."""
from pathlib import Path

from backend.stages.base import Stage, StageResult


class ThreeDStage(Stage):

    @property
    def name(self) -> str:
        return "threed"

    @property
    def display_name(self) -> str:
        return "3D 生成"

    @property
    def icon(self) -> str:
        return "🧊"

    def build_workflow(self, model_key: str, model_cfg: dict,
                       quality: dict, **kwargs) -> dict:
        image_path = kwargs.get("image_path", "")
        server_img = self.client.upload_image(image_path)

        # Steps + octree
        steps_override = int(quality.get("td_steps_override", 0))
        octree_override = int(quality.get("octree_override", 0))
        steps = steps_override if steps_override > 0 else model_cfg.get("default_steps", 20)
        octree = octree_override if octree_override > 0 else model_cfg.get("default_octree", 256)

        gen_mode = model_cfg["generation_mode"]
        bg_removal = model_cfg.get("bg_removal", "birefnet.safetensors")
        guidance = model_cfg.get("guidance_scale", 5.5)
        seed = int(kwargs.get("seed", 2026))

        # Unique save path for file recovery
        ts = int(__import__("time").time())
        save_subfolder = "aiw_3d"
        save_filename = f"model_{ts}.glb"

        wf = {
            "4": {"class_type": "LoadImage", "inputs": {"image": server_img}},
            "20": {"class_type": "LoadBackgroundRemovalModel", "inputs": {
                "bg_removal_name": bg_removal}},
            "21": {"class_type": "RemoveBackground", "inputs": {
                "bg_removal_model": ["20", 0], "image": ["4", 0]}},
            "50": {"class_type": "[Comfy3D] Load Hunyuan3D V2 ShapeGen Pipeline", "inputs": {
                "generation_mode": gen_mode,
                "weights_format": "safetensors", "flash_vdm": True}},
            "51": {"class_type": "[Comfy3D] Hunyuan3D V2 DiT Flow Matching Model", "inputs": {
                "hunyuan3d_v2_i23d_pipe": ["50", 0],
                "reference_image": ["4", 0],
                "reference_mask": ["21", 0],
                "seed": seed, "guidance_scale": guidance,
                "num_inference_steps": steps,
                "octree_resolution": octree}},
            "70": {"class_type": "[Comfy3D] Save 3D Mesh", "inputs": {
                "mesh": ["51", 0],
                "save_path": f"{save_subfolder}/{save_filename}"}},
        }
        return wf

    def process_output(self, outputs: dict, model_key: str,
                       model_cfg: dict, **kwargs) -> StageResult:
        # Save_3D_Mesh has RETURN_TYPES=() → outputs dict is empty.
        # Must find file on disk.
        try:
            src = self.client.find_on_disk("aiw_3d", "glb")
            final = self.client.copy_to_outputs(src, "3d", prefix="model_")
            return StageResult(True, output_path=str(final))
        except Exception as e:
            return StageResult(False, error=str(e))
