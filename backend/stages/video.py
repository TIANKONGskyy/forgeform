"""Video generation stage — MiniMax H3 Ref2VA models."""
from pathlib import Path
from PIL import Image

from backend.stages.base import Stage, StageResult


class VideoStage(Stage):

    @property
    def name(self) -> str:
        return "video"

    @property
    def display_name(self) -> str:
        return "视频生成"

    @property
    def icon(self) -> str:
        return "🎬"

    def build_workflow(self, model_key: str, model_cfg: dict,
                       quality: dict, **kwargs) -> dict:
        image_path = kwargs.get("image_path", "")
        prompt = kwargs.get("prompt", "")

        # Upload first frame + horizontal flip (last frame)
        server_first = self.client.upload_image(image_path)
        flipped = Path(image_path).parent / f"_flip_tmp_{Path(image_path).stem}.png"
        Image.open(image_path).transpose(Image.FLIP_LEFT_RIGHT).save(flipped)
        server_last = self.client.upload_image(str(flipped))
        flipped.unlink(missing_ok=True)

        # Steps
        steps_override = int(quality.get("vid_steps_override", 0))
        steps = steps_override if steps_override > 0 else model_cfg.get("default_steps", 8)
        length = model_cfg.get("default_length", 192)

        is_gguf = model_cfg.get("is_gguf", False)
        unet_name = model_cfg["unet"]
        clip_name = model_cfg["clip"]
        clip_type = model_cfg.get("clip_type", "minimax")
        vae_name = model_cfg["vae"]
        lora_name = model_cfg.get("lora")
        scheduler = model_cfg.get("scheduler", "beta")
        sampler = model_cfg.get("sampler", "euler")

        # UNET loader
        if is_gguf:
            unet_node = {"class_type": "UnetLoaderGGUF",
                         "inputs": {"unet_name": unet_name}}
        else:
            unet_node = {"class_type": "UNETLoader",
                         "inputs": {"unet_name": unet_name, "weight_dtype": "default"}}

        # Model chain
        if lora_name:
            model_node = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["1", 0], "lora_name": lora_name, "strength_model": 1.0}}
            model_ref = ["6", 0]
        else:
            model_node = None
            model_ref = ["1", 0]

        wf = {
            "1": unet_node,
            "2": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": clip_name, "type": clip_type}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
            "4": {"class_type": "LoadImage", "inputs": {"image": server_first}},
            "5": {"class_type": "LoadImage", "inputs": {"image": server_last}},
        }

        if lora_name:
            wf["6"] = model_node

        wf.update({
            "7": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
                "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                "width": 1344, "height": 768, "length": length,
                "first_frame": ["4", 0], "last_frame": ["5", 0]}},
            "8": {"class_type": "BasicGuider", "inputs": {
                "model": model_ref, "conditioning": ["7", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
            "10": {"class_type": "KSamplerSelect", "inputs": {
                "sampler_name": sampler}},
            "11": {"class_type": "BasicScheduler", "inputs": {
                "model": model_ref, "scheduler": scheduler,
                "steps": steps, "denoise": 1.0}},
            "12": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0],
                "sigmas": ["11", 0], "latent_image": ["7", 1]}},
            "13": {"class_type": "VAEDecode", "inputs": {
                "samples": ["12", 0], "vae": ["3", 0]}},
            "14": {"class_type": "CreateVideo", "inputs": {
                "images": ["13", 0], "fps": 24}},
            "15": {"class_type": "SaveVideo", "inputs": {
                "video": ["14", 0], "filename_prefix": "aiw/video_",
                "format": "mp4", "codec": "h264"}},
        })
        return wf

    def process_output(self, outputs: dict, model_key: str,
                       model_cfg: dict, **kwargs) -> StageResult:
        dest = self.client.output_root / "videos"
        try:
            path = self.client.download(outputs, dest, prefix="video_", ext="mp4")
            return StageResult(True, output_path=str(path))
        except Exception as e:
            return StageResult(False, error=str(e))
