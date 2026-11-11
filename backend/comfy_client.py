"""ComfyUI API Client - 通过 API 提交工作流"""
import json
import time
import requests
import uuid
from pathlib import Path
from backend.env import COMFYUI_DIR
from typing import Dict, Optional
from .logger import get_logger

logger = get_logger("comfy_client")


class ComfyUIClient:
    def __init__(self, config):
        self.config = config
        # 双实例架构（2026-08-20）：image→8188(GPU0)，video/3d→8189(GPU1)，双卡并行
        ports = config.get("comfyui_ports", {})
        self.port_image = int(ports.get("image", 8188))
        self.port_media = int(ports.get("media", 8189))
        self.comfyui_url = f"http://127.0.0.1:{self.port_image}"   # 图片实例（GPU0）
        self.media_url = f"http://127.0.0.1:{self.port_media}"     # 视频/3D 实例（GPU1）
        self.stage_url = {
            "image": self.comfyui_url,
            "video": self.media_url,
            "3d": self.media_url,
        }
        self._prompt_url: dict = {}   # prompt_id → 提交的实例 URL
        self.comfyui_dir = config.get("paths", {}).get("comfyui") or str(COMFYUI_DIR)
        self.client_id = str(uuid.uuid4())
        self.ws = None

    def _pick_url(self, stage: Optional[str] = None) -> str:
        """按阶段选实例 URL（image→8188, video/3d→8189）"""
        if stage and stage in self.stage_url:
            return self.stage_url[stage]
        return self.comfyui_url

    def is_running(self) -> bool:
        """双实例都在线才算 UP（2026-08-20 双卡架构）"""
        for url in (self.comfyui_url, self.media_url):
            try:
                r = requests.get(f"{url}/system_stats", timeout=5)
                if r.status_code != 200:
                    return False
            except Exception:
                return False
        return True

    def interrupt(self) -> bool:
        """中断两个实例的当前任务"""
        ok = True
        for url in (self.comfyui_url, self.media_url):
            try:
                r = requests.post(f"{url}/interrupt", timeout=5)
                if r.status_code != 200:
                    ok = False
            except Exception:
                ok = False
        return ok

    def get_gpu_temp(self, gpu_index: Optional[int] = None) -> Optional[int]:
        """读取 GPU 温度（摄氏度），失败返回 None
        2026-08-13：温度看门狗用，轻量 nvidia-smi 单查询
        2026-08-20：gpu_index 指定查哪张卡（image 实例=GPU0，media 实例=GPU1）
        """
        import subprocess
        try:
            cmd = ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
            if gpu_index is not None:
                cmd = ["nvidia-smi", "-i", str(gpu_index)] + cmd[1:]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
            return int(out.split("\n")[0].strip())
        except Exception:
            return None
    
    def build_workflow(self, stage_name: str, params: Dict) -> Dict:
        """根据 stage 和参数构建 ComfyUI 工作流 JSON"""
        if stage_name == "image":
            return self._build_image_workflow(params)
        elif stage_name == "3d":
            return self._build_3d_workflow(params)
        elif stage_name == "video":
            return self._build_video_workflow(params)
        else:
            raise ValueError(f"Unknown stage: {stage_name}")
    
    # ═══════════════════════════════════════════════════════════
    # IMAGE WORKFLOW (FLUX)
    # ═══════════════════════════════════════════════════════════
    
    def _build_image_workflow(self, params: Dict) -> Dict:
        """构建图像生成工作流。

        2026-09-23：带参考图请求（params["reference_image"]）时走 Qwen-Image-2.1 的参考图分支 ——
        这是"照图生成指定角色"的唯一可靠路径（纯文字模型只能画属性，实测两次都不对 ✗）。
        """
        if params.get("reference_image"):
            return self._build_qwen21_ref_workflow(params)
        model = params.get("model_config", {})
        prompt_text = params.get("prompt", "")
        negative = params.get("negative_prompt", "")
        width = params.get("width", 1024)
        height = params.get("height", 1024)
        steps = params.get("steps", model.get("steps", 20))
        # FLUX guidance-distilled（2026-08-14 调研修正）：
        # guidance 是模型嵌入条件（由 CLIPTextEncodeFlux 注入），KSampler cfg 必须 1.0；
        # cfg>1 会触发 cond+uncond 双前向（慢一倍）+ 经典 CFG 伪影（过饱和）
        cfg = 1.0
        seed = params.get("seed", -1)
        
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        checkpoint = model.get("checkpoint", "")
        # Z-Image 分支（2026-08-17）：阿里 6B，AuraFlow 采样 + lumina2 clip + SD3 latent
        if "z-image" in checkpoint.lower() or "z_image" in checkpoint.lower() or "zimage" in checkpoint.lower():
            return self._build_zimage_workflow(params)
        # FLUX.2 Klein 分支（2026-08-14）：架构与 FLUX.1 dev 完全不同
        # （Flux2Scheduler + SamplerCustomAdvanced + CFGGuider + EmptyFlux2LatentImage）
        if "klein" in checkpoint.lower() or "flux-2" in checkpoint.lower() or "flux2" in checkpoint.lower():
            return self._build_flux2_workflow(params)
        is_gguf = checkpoint.endswith(".gguf")
        
        workflow = {}
        nid = 1
        
        # 1. Load checkpoint
        if is_gguf:
            workflow[str(nid)] = {
                "class_type": "UnetLoaderGGUF",
                "inputs": {"unet_name": checkpoint}
            }
        else:
            workflow[str(nid)] = {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": checkpoint,
                    "weight_dtype": "fp8_e4m3fn" if "fp8" in checkpoint else "default"
                }
            }
        model_node = str(nid); nid += 1
        
        # 1.4 Hyper-SD 8步 LoRA（可选，2026-08-14）：挂到 model 上，8 步出图约 2-3x 加速
        lora_name = model.get("hyper_lora", "")
        if lora_name:
            workflow[str(nid)] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": [model_node, 0], "lora_name": lora_name, "strength_model": 1.0}
            }
            model_node = str(nid); nid += 1
        
        # 1.5 加速缓存：FBCache（WaveSpeed）优先，TeaCache 备选（二选一，2026-08-14）
        use_fbcache = bool(model.get("fbcache", False)) and not is_gguf
        use_teacache = bool(model.get("teacache", False)) and not is_gguf and not use_fbcache
        if use_fbcache:
            workflow[str(nid)] = {
                "class_type": "ApplyFBCacheOnModel",
                "inputs": {
                    "model": [model_node, 0],
                    "object_to_patch": "diffusion_model",
                    "residual_diff_threshold": model.get("fbcache_thresh", 0.12),
                    "start": 0.0,
                    "end": 1.0,
                    "max_consecutive_cache_hits": -1,
                }
            }
            model_node = str(nid); nid += 1
        elif use_teacache:
            workflow[str(nid)] = {
                "class_type": "TeaCache",
                "inputs": {
                    "model": [model_node, 0],
                    "model_type": model.get("teacache_type", "flux"),
                    "rel_l1_thresh": model.get("teacache_thresh", 0.4),
                    "start_percent": 0.0,
                    "end_percent": 1.0,
                    "cache_device": "cuda",
                }
            }
            model_node = str(nid); nid += 1
        
        # 2. Load CLIP
        clip_l = model.get("clip_l", "clip_l.safetensors")
        clip_t5 = model.get("clip_t5", "")
        clip_type = model.get("clip_type", "flux")
        
        if clip_t5:
            workflow[str(nid)] = {
                "class_type": "DualCLIPLoader",
                "inputs": {"clip_name1": clip_l, "clip_name2": clip_t5, "type": clip_type}
            }
        else:
            workflow[str(nid)] = {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": clip_l, "type": clip_type}
            }
        clip_node = str(nid); nid += 1
        
        # 3. Load VAE
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": model.get("vae", "ae.safetensors")}
        }
        vae_node = str(nid); nid += 1
        
        # 4. CLIP Text Encode (positive)
        # 2026-08-14：FLUX.1 dev（DualCLIPLoader 分支）改用 CLIPTextEncodeFlux 接管 guidance；
        # FLUX.2 Klein（单 encoder）保持标准 CLIPTextEncode（config 已 cfg=1.0）
        if clip_t5:
            guidance = params.get("guidance", model.get("guidance", 3.5))
            workflow[str(nid)] = {
                "class_type": "CLIPTextEncodeFlux",
                "inputs": {
                    "clip": [clip_node, 0],
                    "clip_l": prompt_text,
                    "t5xxl": prompt_text,
                    "guidance": guidance,
                }
            }
        else:
            workflow[str(nid)] = {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt_text, "clip": [clip_node, 0]}
            }
        pos_node = str(nid); nid += 1
        
        # 5. CLIP Text Encode (negative)
        workflow[str(nid)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": [clip_node, 0]}
        }
        neg_node = str(nid); nid += 1
        
        # 6. Latent: EmptyLatentImage (text2img) / LoadImage+Scale+VAEEncode (img2img) /
        #    LoadImage+Scale+mask+InpaintModelConditioning (inpaint)
        init_image = params.get("init_image", "")
        mask_file = params.get("mask_file", "")
        denoise = params.get("denoise", 1.0)
        load_img_node = None
        scale_node = None
        if init_image:
            workflow[str(nid)] = {
                "class_type": "LoadImage",
                "inputs": {"image": init_image}
            }
            load_img_node = str(nid); nid += 1
            workflow[str(nid)] = {
                "class_type": "ImageScale",
                "inputs": {
                    "image": [load_img_node, 0],
                    "upscale_method": "lanczos",
                    "width": width, "height": height,
                    "crop": "disabled"
                }
            }
            scale_node = str(nid); nid += 1

        if mask_file:
            # inpaint：原图 + 遮罩 → InpaintModelConditioning
            if load_img_node is None:
                raise ValueError("inpaint 需要 init_image")
            workflow[str(nid)] = {
                "class_type": "LoadImage",
                "inputs": {"image": mask_file}
            }
            load_mask_node = str(nid); nid += 1
            workflow[str(nid)] = {
                "class_type": "ImageToMask",
                "inputs": {"image": [load_mask_node, 0], "channel": "red"}
            }
            mask_node = str(nid); nid += 1
            workflow[str(nid)] = {
                "class_type": "InpaintModelConditioning",
                "inputs": {
                    "positive": [pos_node, 0],
                    "negative": [neg_node, 0],
                    "vae": [vae_node, 0],
                    "pixels": [scale_node, 0],
                    "mask": [mask_node, 0],
                    "noise_mask": True,
                }
            }
            inpaint_node = str(nid); nid += 1
            # KSampler 用 inpaint 的输出（positive/negative/latent）
            ks_pos, ks_neg, ks_lat = [inpaint_node, 0], [inpaint_node, 1], [inpaint_node, 2]
        elif init_image:
            # img2img: 缩放后编码到 latent
            workflow[str(nid)] = {
                "class_type": "VAEEncode",
                "inputs": {"pixels": [scale_node, 0], "vae": [vae_node, 0]}
            }
            latent_node = str(nid); nid += 1
        else:
            workflow[str(nid)] = {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1}
            }
            latent_node = str(nid); nid += 1
        
        # 7. KSampler
        if not mask_file:
            # 非 inpaint：positive/negative 用文本编码，latent 用对应生成节点
            ks_pos, ks_neg, ks_lat = [pos_node, 0], [neg_node, 0], [latent_node, 0]
        # inpaint 时 ks_pos/ks_neg/ks_lat 已在 InpaintModelConditioning 分支定义
        workflow[str(nid)] = {
            "class_type": "KSampler",
            "inputs": {
                "model": [model_node, 0],
                "positive": ks_pos,
                "negative": ks_neg,
                "latent_image": ks_lat,
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": model.get("sampler", "euler"),
                "scheduler": model.get("scheduler", "simple"),
                "denoise": denoise
            }
        }
        sampler_node = str(nid); nid += 1
        
        # 8. VAE Decode
        workflow[str(nid)] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [sampler_node, 0], "vae": [vae_node, 0]}
        }
        decode_node = str(nid); nid += 1
        
        # 9. Save Image
        workflow[str(nid)] = {
            "class_type": "SaveImage",
            "inputs": {"images": [decode_node, 0], "filename_prefix": "ai_workshop"}
        }
        
        return {"prompt": workflow, "negative_prompt": negative}
    
    # ═══════════════════════════════════════════════════════════
    # FLUX.2 WORKFLOW (Klein 4B/9B)
    # ═══════════════════════════════════════════════════════════
    
    def _build_flux2_workflow(self, params: Dict) -> Dict:
        """FLUX.2 Klein 文生图工作流（2026-08-14 新增，基于官方 blueprint）
        
        节点链：UNETLoader → CFGGuider → SamplerCustomAdvanced → VAEDecode
               + CLIPLoaderGGUF(flux2) + Flux2Scheduler + EmptyFlux2LatentImage + RandomNoise
        关键参数（官方示例）：cfg=5（CFGGuider）、sampler=euler、weight_dtype=default
        """
        model = params.get("model_config", {})
        prompt_text = params.get("prompt", "")
        negative = params.get("negative_prompt", "")
        width = params.get("width", 1024)
        height = params.get("height", 1024)
        steps = params.get("steps", model.get("steps", 20))
        cfg = params.get("cfg", model.get("cfg", 5.0))
        seed = params.get("seed", -1)
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        checkpoint = model.get("checkpoint", "")
        is_gguf = checkpoint.endswith(".gguf")
        clip_name = model.get("clip_l", "")
        clip_is_gguf = clip_name.endswith(".gguf")
        
        workflow = {}
        nid = 1
        
        # 1. Load checkpoint（fp8 用 weight_dtype=default，官方示例确认）
        if is_gguf:
            workflow[str(nid)] = {
                "class_type": "UnetLoaderGGUF",
                "inputs": {"unet_name": checkpoint}
            }
        else:
            workflow[str(nid)] = {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": checkpoint, "weight_dtype": "default"}
            }
        model_node = str(nid); nid += 1
        
        # 1.4 微调 LoRA（2026-08-18）：挂到 model 上（如 flux_klein 的 3D 打印微调）
        lora_name = model.get("lora", "")
        if lora_name:
            workflow[str(nid)] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": [model_node, 0], "lora_name": lora_name, "strength_model": 1.0}
            }
            model_node = str(nid); nid += 1
        
        # 2. Load CLIP（encoder；gguf 用 CLIPLoaderGGUF，type=flux2）
        if clip_is_gguf:
            workflow[str(nid)] = {
                "class_type": "CLIPLoaderGGUF",
                "inputs": {"clip_name": clip_name, "type": "flux2"}
            }
        else:
            workflow[str(nid)] = {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": clip_name, "type": "flux2", "device": "default"}
            }
        clip_node = str(nid); nid += 1
        
        # 3. Load VAE
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": model.get("vae", "flux2-vae.safetensors")}
        }
        vae_node = str(nid); nid += 1
        
        # 4. CLIP Text Encode (positive)
        workflow[str(nid)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt_text, "clip": [clip_node, 0]}
        }
        pos_node = str(nid); nid += 1
        
        # 5. CLIP Text Encode (negative)
        workflow[str(nid)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": [clip_node, 0]}
        }
        neg_node = str(nid); nid += 1
        
        # 6. CFGGuider（FLUX.2 的 cfg 走这里，默认 5）
        workflow[str(nid)] = {
            "class_type": "CFGGuider",
            "inputs": {
                "model": [model_node, 0],
                "positive": [pos_node, 0],
                "negative": [neg_node, 0],
                "cfg": cfg,
            }
        }
        guider_node = str(nid); nid += 1
        
        # 7. KSamplerSelect（euler）
        workflow[str(nid)] = {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"}
        }
        sampler_node = str(nid); nid += 1
        
        # 8. Flux2Scheduler
        workflow[str(nid)] = {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": steps, "width": width, "height": height}
        }
        sigmas_node = str(nid); nid += 1
        
        # 9. RandomNoise
        workflow[str(nid)] = {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed}
        }
        noise_node = str(nid); nid += 1
        
        # 10. EmptyFlux2LatentImage
        workflow[str(nid)] = {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1}
        }
        latent_node = str(nid); nid += 1
        
        # 11. SamplerCustomAdvanced
        workflow[str(nid)] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": [noise_node, 0],
                "guider": [guider_node, 0],
                "sampler": [sampler_node, 0],
                "sigmas": [sigmas_node, 0],
                "latent_image": [latent_node, 0],
            }
        }
        sample_node = str(nid); nid += 1
        
        # 12. VAEDecode
        workflow[str(nid)] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [sample_node, 0], "vae": [vae_node, 0]}
        }
        decode_node = str(nid); nid += 1
        
        # 13. SaveImage
        workflow[str(nid)] = {
            "class_type": "SaveImage",
            "inputs": {"images": [decode_node, 0], "filename_prefix": "ai_workshop_klein"}
        }
        
        return {"prompt": workflow, "negative_prompt": negative}
    
    # ═══════════════════════════════════════════════════════════
    # Z-IMAGE WORKFLOW (阿里 6B，AuraFlow 采样 + lumina2 clip + SD3 latent)
    # ═══════════════════════════════════════════════════════════
    
    def _build_qwen21_ref_workflow(self, params: Dict) -> Dict:
        """Qwen-Image-2.1 **参考图**工作流（2026-09-23 新增）—— 宇凯要的"照图生成指定角色"。

        为什么必须有这条（而不是继续调提示词）：熊大是 IP 角色，纯文字生图**只能画到属性**
        （实测：Z-Image 6B 与 FLUX.2 Klein 9B 都出一只"标准卡通棕熊"，不是那个角色 ✗）。
        要精确还原只有"照图" —— 本机 ComfyUI v0.37.2 自带 2.1 支持，实测 42s 出图且角色保真 ✓。

        节点链（与 2026-09-23 在 http://127.0.0.1:8188 实测跑通的图一致）：
          UNETLoader(qwen_image_2.1_int8_convrot)            → MODEL
          CLIPLoader(qwen3vl_8b_int8_convrot, qwen_image)    → CLIP  ← 2.1 编码器就是 **Qwen3-VL-8B**
          VAELoader(qwen_image_2.1_vae_bf16)                 → VAE
          LoadImage(参考图，已上传到 ComfyUI 的 input)          → IMAGE
          TextEncodeQwenImage21(clip, prompt, negative_prompt, resolution, images=[[img,0]], vae)
              → 输出 0=正向 1=负向 2=LATENT   ← 2.1 专用节点；图直接喂进去，由 DiT 做 image slots splicing
          KSampler(euler/simple, 20 步, cfg 4.0) → VAEDecode → SaveImage

        坑（都是我踩过的）：
          · 编码器别下错：`..._pe_t2i/pe_i2i` 是提示词增强器 ✗，主编码器是 `qwen3vl_8b_int8_convrot` ✓
          · 别用通用 CLIPTextEncode ✗ —— 2.1 的条件要靠 TextEncodeQwenImage21（它会吐 LATENT）
          · `images` 是 AUTOGROW 输入：API 里要写成 **list-of-pairs** `[[node_id, 0], ...]` ✓
            （实测提交一次即通过；写成单 pair 或 dict 会被校验拒）
        """
        model = params.get("model_config", {})
        prompt_text = params.get("prompt", "")
        # 2026-09-24: 之前 negative 是 "" 时 TextEncodeQwenImage21 报 "missing negative_prompt"
        # → 必须给非空字符串（用 2.1 专用的强负向词）
        negative = (params.get("negative_prompt", "") or
                    "low quality, blurry, distorted, deformed, ugly, bad anatomy, watermark, "
                    "text, logo, rough, coarse, low detail, pixelated, grainy, oversimplified, "
                    "low poly, plastic, unfinished, painted pattern, flat decal, flat texture, "
                    "2d pattern, fake texture")
        res = int(params.get("resolution_px") or params.get("width") or 768)
        steps = int(params.get("steps", model.get("steps", 20)))
        cfg = float(params.get("cfg", model.get("cfg", 4.0)))
        ref_name = params.get("reference_image") or ""
        seed = params.get("seed", -1)
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)

        wf, nid = {}, 1

        def add(class_type, inputs):
            nonlocal nid
            wf[str(nid)] = {"class_type": class_type, "inputs": inputs}
            nid += 1
            return str(nid - 1)

        unet = add("UNETLoader", {"unet_name": model.get("checkpoint", ""), "weight_dtype": "default"})
        clip = add("CLIPLoader", {"clip_name": model.get("clip_l", ""),
                                  "type": model.get("clip_type", "qwen_image"), "device": "default"})
        vae = add("VAELoader", {"vae_name": model.get("vae", "")})
        img = add("LoadImage", {"image": ref_name, "upload": "image"})
        cond = add("TextEncodeQwenImage21", {
            "clip": [clip, 0], "prompt": prompt_text, "negative_prompt": negative,
            "resolution": res, "images": [[img, 0]], "vae": [vae, 0]})
        ks = add("KSampler", {"model": [unet, 0], "positive": [cond, 0], "negative": [cond, 1],
                              "latent_image": [cond, 2], "seed": seed, "steps": steps, "cfg": cfg,
                              "sampler_name": model.get("sampler", "euler"),
                              "scheduler": model.get("scheduler", "simple"), "denoise": 1.0})
        dec = add("VAEDecode", {"samples": [ks, 0], "vae": [vae, 0]})
        add("SaveImage", {"images": [dec, 0], "filename_prefix": "qwen21_ref"})
        # 必须和其它构建器一样带外层（submit_workflow 读 workflow_data["prompt"]，
        # 返回裸图会 KeyError: 'prompt' —— E2E 跑出来的 ✗）
        return {"prompt": wf, "negative_prompt": negative}

    def _build_zimage_workflow(self, params: Dict) -> Dict:
        """Z-Image 文生图工作流（2026-08-17 新增）
        节点链：UnetLoaderGGUF → CLIPLoaderGGUF(lumina2) → VAELoader
               + ModelSamplingAuraFlow(shift=3) + EmptySD3LatentImage
               + KSampler(euler/simple, 8步, cfg=1)
        """
        model = params.get("model_config", {})
        prompt_text = params.get("prompt", "")
        negative = params.get("negative_prompt", "")
        width = params.get("width", 1024)
        height = params.get("height", 1024)
        steps = params.get("steps", model.get("steps", 8))
        seed = params.get("seed", -1)
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        checkpoint = model.get("checkpoint", "")
        clip_name = model.get("clip_l", "")
        vae_name = model.get("vae", "")
        shift = model.get("aura_shift", 3)
        
        workflow = {}
        nid = 1
        
        # 1. UnetLoaderGGUF
        workflow[str(nid)] = {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": checkpoint}
        }
        model_node = str(nid); nid += 1
        
        # 2. CLIPLoaderGGUF (type=lumina2)
        workflow[str(nid)] = {
            "class_type": "CLIPLoaderGGUF",
            "inputs": {"clip_name": clip_name, "type": "lumina2"}
        }
        clip_node = str(nid); nid += 1
        
        # 3. VAELoader
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name}
        }
        vae_node = str(nid); nid += 1
        
        # 4. ModelSamplingAuraFlow (shift 采样)
        workflow[str(nid)] = {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": [model_node, 0], "shift": shift}
        }
        model_node = str(nid); nid += 1
        
        # 5. CLIPTextEncode (positive)
        workflow[str(nid)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt_text, "clip": [clip_node, 0]}
        }
        pos_node = str(nid); nid += 1
        
        # 6. CLIPTextEncode (negative)
        workflow[str(nid)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": [clip_node, 0]}
        }
        neg_node = str(nid); nid += 1
        
        # 7. EmptySD3LatentImage
        workflow[str(nid)] = {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1}
        }
        latent_node = str(nid); nid += 1
        
        # 8. KSampler
        workflow[str(nid)] = {
            "class_type": "KSampler",
            "inputs": {
                "model": [model_node, 0],
                "positive": [pos_node, 0],
                "negative": [neg_node, 0],
                "latent_image": [latent_node, 0],
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            }
        }
        sampler_node = str(nid); nid += 1
        
        # 9. VAEDecode
        workflow[str(nid)] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [sampler_node, 0], "vae": [vae_node, 0]}
        }
        decode_node = str(nid); nid += 1
        
        # 10. SaveImage
        workflow[str(nid)] = {
            "class_type": "SaveImage",
            "inputs": {"images": [decode_node, 0], "filename_prefix": "ai_workshop_zimage"}
        }
        
        return {"prompt": workflow, "negative_prompt": negative}
    
    # ═══════════════════════════════════════════════════════════
    # 3D WORKFLOW
    # ═══════════════════════════════════════════════════════════
    
    def _build_3d_workflow(self, params: Dict) -> Dict:
        """构建 3D 生成工作流"""
        model = params.get("model_config", {})
        model_name = model.get("name", "")
        
        if "Hunyuan3D" in model_name:
            # 多视图模式（2026-08-14）：model 带 multiview 标记 → 多视图重建（空间关系准确）
            if model.get("backend") == "multiview":
                return self._build_multiview_workflow(params)
            # V2 管线：model 带 generation_mode 时走 V2（Turbo/Fast + FlashVDM）
            if model.get("generation_mode"):
                return self._build_hunyuan3d_v2_workflow(params)
            return self._build_hunyuan3d_workflow(params)
        elif "TripoSplat" in model_name:
            return self._build_triposplat_workflow(params)
        else:
            raise ValueError(f"Unknown 3D model: {model_name}")
    
    def _build_hunyuan3d_workflow(self, params: Dict) -> Dict:
        """构建 Hunyuan3D v2.1 工作流
        
        使用 Comfy3D 管线节点：
        Load ShapeGen Pipeline → Load TexGen Pipeline → ShapeGen → TexGen → Save Mesh
        """
        model = params.get("model_config", {})
        input_image = params.get("input_image", "")
        seed = params.get("seed", 1234)
        texgen = params.get("texgen", True)  # False = ShapeGen-only (无 custom_rasterizer 编译时降级)
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        # 2026-08-14 调研：params 覆盖支持 quality 接线；guidance 5.0 基线、octree 日常 384/终稿 512
        steps = params.get("steps", model.get("steps", 30))
        guidance = params.get("guidance_scale", model.get("guidance_scale", 5.0))
        octree_res = params.get("octree_resolution", model.get("octree_resolution", 384))
        
        workflow = {}
        nid = 1
        
        # 1. Load ShapeGen Pipeline (from HuggingFace: tencent/Hunyuan3D-2.1)
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Load Hunyuan3D 21 ShapeGen Pipeline",
            "inputs": {
                "subfolder": "hunyuan3d-dit-v2-1"
            }
        }
        shapegen_pipe_node = str(nid); nid += 1
        
        texgen_pipe_node = None
        # 2. Load TexGen Pipeline (仅 texgen=True)
        if texgen:
            workflow[str(nid)] = {
                "class_type": "[Comfy3D] Load Hunyuan3D 21 TexGen Pipeline",
                "inputs": {
                    "max_num_view": 8,
                    "resolution": 768,
                    "enable_mmgp": True
                }
            }
            texgen_pipe_node = str(nid); nid += 1
        
        # 3. Load input image
        workflow[str(nid)] = {
            "class_type": "LoadImage",
            "inputs": {"image": input_image}
        }
        img_node = str(nid); nid += 1
        
        # Foreground extraction is an explicit required stage. The Comfy3D
        # helper silently keeps the opaque image if rembg initialization fails,
        # which reconstructs a background slab as part of the object.
        workflow[str(nid)]={'class_type':'LoadBackgroundRemovalModel','inputs':{'bg_removal_name':model.get('bg_removal','birefnet.safetensors')}}
        bg_model_node=str(nid);nid+=1
        workflow[str(nid)]={'class_type':'RemoveBackground','inputs':{'bg_removal_model':[bg_model_node,0],'image':[img_node,0]}}
        mask_node=str(nid);nid+=1
        # Foreground=1 from segmentation; JoinImageWithAlpha uses 1-mask.
        workflow[str(nid)]={'class_type':'InvertMask','inputs':{'mask':[mask_node,0]}}
        inverse_mask_node=str(nid);nid+=1
        workflow[str(nid)]={'class_type':'JoinImageWithAlpha','inputs':{'image':[img_node,0],'alpha':[inverse_mask_node,0]}}
        foreground_node=str(nid);nid+=1

        # 4. ShapeGen: image → mesh
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Hunyuan3D 21 ShapeGen",
            "inputs": {
                "shapegen_pipe": [shapegen_pipe_node, 0],
                "image": [foreground_node, 0],
                "seed": seed,
                "steps": steps,
                "guidance_scale": guidance,
                "octree_resolution": octree_res,
                # Alpha is already supplied by the required segmentation nodes.
                # Avoid the node's optional rembg wrapper: initialization errors
                # there silently retain the opaque background.
                "remove_background": False,  # explicit foreground alpha above
                "auto_cleanup": True,
                # ★ 2026-10-18：面数上限可由 params 显式指定
                #   （0/不给 = 让节点按 octree 自动推导，保持原行为）
                "max_facenum": int(params.get("max_facenum") or 0),
            }
        }
        shapegen_node = str(nid); nid += 1
        
        output_path = params.get("output_path", f"Hunyuan3D_{int(time.time())}.glb")
        
        if not texgen:
            # ShapeGen-only: 直接保存 mesh
            workflow[str(nid)] = {
                "class_type": "[Comfy3D] Save 3D Mesh",
                "inputs": {
                    "mesh": [shapegen_node, 0],
                    "save_path": Path(output_path).name
                }
            }
            return {"prompt": workflow, "negative_prompt": ""}
        
        # 5. Save intermediate mesh (MESH → STRING path for TexGen)
        temp_mesh_name = f"h3_shape_{int(time.time())}.glb"
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Save 3D Mesh",
            "inputs": {
                "mesh": [shapegen_node, 0],
                "save_path": temp_mesh_name
            }
        }
        save_shape_node = str(nid); nid += 1
        
        # 6. TexGen: mesh_path + image → textured_mesh
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Hunyuan3D 21 TexGen",
            "inputs": {
                "texgen_pipe": [texgen_pipe_node, 0],
                "mesh_path": [save_shape_node, 0],  # save_path STRING from Save node
                "image": [img_node, 0],
                "create_pbr": True,
                "use_remesh": False
            }
        }
        texgen_node = str(nid); nid += 1
        
        # 7. Save final textured mesh as GLB
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Save 3D Mesh",
            "inputs": {
                "mesh": [texgen_node, 0],
                "save_path": Path(output_path).name
            }
        }
        
        return {"prompt": workflow, "negative_prompt": ""}
    
    def _build_hunyuan3d_v2_workflow(self, params: Dict) -> Dict:
        """Hunyuan3D V2 ShapeGen 工作流（2026-08-14）：Turbo/Fast 模式 + FlashVDM + 自动去背景
        
        相比 2.1 管线：
        - Fast 20 步 / Turbo 5 步（vs 2.1 的 30 步）
        - FlashVDM：VAE 解码快 3-5 倍（仅 turbo/fast 生效）
        - 内置 FloaterRemover/DegenerateFaceRemover/FaceReducer 网格后处理（质量提升）
        - 自动 rembg 去背景（reference_mask）
        """
        model = params.get("model_config", {})
        input_image = params.get("input_image", "")
        seed = params.get("seed", 1234)
        octree_res = params.get("octree_resolution", 384)
        guidance = params.get("guidance_scale", 5.0)
        steps = params.get("steps", model.get("steps", 20))
        generation_mode = model.get("generation_mode", "Hunyuan3D-2-Fast")
        output_path = params.get("output_path", "")
        
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        workflow = {}
        nid = 1
        
        # 1. LoadBackgroundRemovalModel（birefnet 去背景）
        workflow[str(nid)] = {
            "class_type": "LoadBackgroundRemovalModel",
            "inputs": {"bg_removal_name": "birefnet.safetensors"}
        }
        bg_model = str(nid); nid += 1
        
        # 2. LoadImage
        workflow[str(nid)] = {
            "class_type": "LoadImage",
            "inputs": {"image": input_image}
        }
        img_node = str(nid); nid += 1
        
        # 3. RemoveBackground → mask
        workflow[str(nid)] = {
            "class_type": "RemoveBackground",
            "inputs": {"bg_removal_model": [bg_model, 0], "image": [img_node, 0]}
        }
        mask_node = str(nid); nid += 1
        
        # 4. Load V2 ShapeGen Pipeline（FlashVDM 开）
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Load Hunyuan3D V2 ShapeGen Pipeline",
            "inputs": {
                "generation_mode": generation_mode,
                "weights_format": "safetensors",
                "flash_vdm": True,
            }
        }
        pipe_node = str(nid); nid += 1
        
        # 5. V2 DiT Flow Matching Model → mesh
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Hunyuan3D V2 DiT Flow Matching Model",
            "inputs": {
                "hunyuan3d_v2_i23d_pipe": [pipe_node, 0],
                "reference_image": [img_node, 0],
                "reference_mask": [mask_node, 0],
                "seed": seed,
                "guidance_scale": guidance,
                "num_inference_steps": steps,
                "octree_resolution": octree_res,
            }
        }
        mesh_node = str(nid); nid += 1
        
        # 6. Save 3D Mesh
        workflow[str(nid)] = {
            "class_type": "[Comfy3D] Save 3D Mesh",
            "inputs": {
                "mesh": [mesh_node, 0],
                "save_path": Path(output_path).name if output_path else f"mesh_{int(time.time())}.glb",
            }
        }
        
        return {"prompt": workflow, "negative_prompt": ""}
    
    def _build_multiview_workflow(self, params: Dict) -> Dict:
        """多视图 3D 工作流（2026-08-14）：解决单图 3D 空间关系错乱
        
        链路：单图 → Zero123Plus 生成 6 视图 → 提取 front/right/back/left → 
              Hunyuan3D V2 ShapeGen MV 多视图重建（2mv 管线必须 4 视图 dict）
        """
        model = params.get("model_config", {})
        input_image = params.get("input_image", "")
        seed = params.get("seed", 1234)
        octree_res = params.get("octree_resolution", 384)
        guidance = params.get("guidance_scale", 5.0)
        steps = params.get("steps", 20)
        output_path = params.get("output_path", "")
        
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        workflow = {}
        
        # 1. LoadBackgroundRemovalModel
        workflow["1"] = {"class_type": "LoadBackgroundRemovalModel",
                         "inputs": {"bg_removal_name": "birefnet.safetensors"}}
        # 2. LoadImage
        workflow["2"] = {"class_type": "LoadImage", "inputs": {"image": input_image}}
        # 3. RemoveBackground → mask
        workflow["3"] = {"class_type": "RemoveBackground",
                         "inputs": {"bg_removal_model": ["1", 0], "image": ["2", 0]}}
        # 4. Load Diffusers Pipeline (Zero123Plus)
        workflow["4"] = {"class_type": "[Comfy3D] Load Diffusers Pipeline",
                         "inputs": {"diffusers_pipeline_name": "Zero123PlusPipeline",
                                    "repo_id": "sudo-ai/zero123plus-v1.2",
                                    "custom_pipeline": "", "force_download": False,
                                    "force_disable_xformers": True}}
        # 5. Zero123Plus → 6 视图（azimuth: 30/90/150/-150/-90/-30）
        workflow["5"] = {"class_type": "[Comfy3D] Zero123Plus Diffusion Model",
                         "inputs": {"zero123plus_pipe": ["4", 0],
                                    "reference_image": ["2", 0], "reference_mask": ["3", 0],
                                    "seed": seed, "guidance_scale": 4.0, "num_inference_steps": 28}}
        # 6-9. 提取 front(0)/right(1)/back(2)/left(4)
        for i, idx in enumerate([0, 1, 2, 4]):
            workflow[str(6 + i)] = {"class_type": "GetImagesFromBatchIndexed",
                                    "inputs": {"images": ["5", 0], "indexes": str(idx)}}
        # 10. Multi Background Remover → LIST（front/back/left/right）
        workflow["10"] = {"class_type": "[Comfy3D] Multi Background Remover",
                          "inputs": {"image_front": ["6", 0], "image_back": ["8", 0],
                                     "image_left": ["9", 0], "image_right": ["7", 0]}}
        # 11. Load Hunyuan3D V2 ShapeGen Pipeline (mv)
        workflow["11"] = {"class_type": "[Comfy3D] Load Hunyuan3D V2 ShapeGen Pipeline",
                          "inputs": {"generation_mode": model.get("generation_mode", "Hunyuan3D-2mv-Fast"),
                                     "weights_format": "safetensors", "flash_vdm": True}}
        # 12. ShapeGen MV → mesh
        workflow["12"] = {"class_type": "[Comfy3D] Hunyuan3D V2 ShapeGen MV",
                          "inputs": {"shapegen_pipe": ["11", 0], "images": ["10", 0],
                                     "seed": seed, "guidance_scale": guidance,
                                     "num_inference_steps": steps, "octree_resolution": octree_res}}
        # 13. Save 3D Mesh
        workflow["13"] = {"class_type": "[Comfy3D] Save 3D Mesh",
                          "inputs": {"mesh": ["12", 0],
                                     "save_path": Path(output_path).name if output_path else f"mesh_{int(time.time())}.glb"}}
        
        return {"prompt": workflow, "negative_prompt": ""}
    
    def _build_triposplat_workflow(self, params: Dict) -> Dict:
        """构建 TripoSplat 工作流
        
        使用原生 ComfyUI 节点：
        UNETLoader → TripoSplatConditioning → KSampler → VAEDecodeTripoSplat → SplatToMesh
        """
        model = params.get("model_config", {})
        input_image = params.get("input_image", "")
        seed = params.get("seed", -1)
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        workflow = {}
        nid = 1
        
        # 1. Load TripoSplat model via UNETLoader
        workflow[str(nid)] = {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": model.get("model", "triposplat_fp16.safetensors"),
                "weight_dtype": "default"
            }
        }
        model_node = str(nid); nid += 1
        
        # 2. Load TripoSplat VAE encoder —— 注意：TripoSplatConditioning 实际用 Flux2 VAE
        #    （nodes_triposplat.py 源码确认：'Flux2 VAE'；triposplat_vae_encoder 不是标准 VAE 格式）
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "flux2-vae.safetensors"}
        }
        vae_enc_node = str(nid); nid += 1
        
        # 3. Load TripoSplat VAE decoder
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": model.get("vae_decoder", "triposplat_vae_decoder_fp16.safetensors")}
        }
        vae_dec_node = str(nid); nid += 1
        
        # 4. Load CLIP Vision —— TripoSplat 用 DINOv3 ViT-H/16（nodes_triposplat.py 确认）
        workflow[str(nid)] = {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": "dino_v3_vit_h.safetensors"}
        }
        vision_node = str(nid); nid += 1
        
        # 5. Load & preprocess input image
        workflow[str(nid)] = {
            "class_type": "LoadImage",
            "inputs": {"image": input_image}
        }
        img_node = str(nid); nid += 1

        # 5.5 背景移除（birefnet）→ 前景 mask（TripoSplat 必需）
        workflow[str(nid)] = {
            "class_type": "LoadBackgroundRemovalModel",
            "inputs": {"bg_removal_name": "birefnet.safetensors"}
        }
        bg_model_node = str(nid); nid += 1

        workflow[str(nid)] = {
            "class_type": "RemoveBackground",
            "inputs": {
                "bg_removal_model": [bg_model_node, 0],
                "image": [img_node, 0],
            }
        }
        bg_mask_node = str(nid); nid += 1

        # 6. TripoSplat Preprocess Image
        workflow[str(nid)] = {
            "class_type": "TripoSplatPreprocessImage",
            "inputs": {
                "image": [img_node, 0],
                "mask": [bg_mask_node, 0],
                "erode_radius": 1,
                "size": 1024
            }
        }
        preproc_node = str(nid); nid += 1
        
        # 7. TripoSplat Conditioning
        workflow[str(nid)] = {
            "class_type": "TripoSplatConditioning",
            "inputs": {
                "clip_vision": [vision_node, 0],
                "vae": [vae_enc_node, 0],
                "image": [preproc_node, 0]
            }
        }
        cond_node = str(nid); nid += 1
        
        # 9. KSampler
        # 【2026-09-23 修】参数照官方蓝图 blueprints/Image to Gaussian Splat (TripoSplat).json：
        #   KSampler = 20 步 / cfg 3 / dpmpp_2m / simple
        # 之前写的是 30 步 / cfg 5.0 / euler —— **采样器和 cfg 都不对**。splat 模型用错采样器
        # 出来的点云是噪的，随后 SplatToMesh 抽出的网格必然碎：实测 8 组产物全是 2000~15500
        # 个连通块、无一水密，原始 GLB 渲染出来是漂浮碎块而不是模型。
        tp_q = params.get("_tripo_quality") or {}
        workflow[str(nid)] = {
            "class_type": "KSampler",
            "inputs": {
                "model": [model_node, 0],
                "positive": [cond_node, 0],
                "negative": [cond_node, 1],
                "latent_image": [cond_node, 2],
                "seed": seed,
                "steps": int(tp_q.get("steps", 20)),
                "cfg": float(tp_q.get("cfg", 3.0)),
                "sampler_name": tp_q.get("sampler", "dpmpp_2m"),
                "scheduler": "simple",
                "denoise": 1.0
            }
        }
        sampler_node = str(nid); nid += 1
        
        # 9.5 VAEDecodeTripoSplat
        workflow[str(nid)] = {
            "class_type": "VAEDecodeTripoSplat",
            "inputs": {
                "samples": [sampler_node, 0],
                "vae": [vae_dec_node, 0],
                "num_gaussians": int(tp_q.get("num_gaussians", 262144)),
                "seed": seed
            }
        }
        decode_node = str(nid); nid += 1
        
        # 10. Convert Splat to Mesh
        # 参数语义见 comfy_extras/nodes_gaussian_splat.py::SplatToMesh 的 tooltip：
        #   resolution = 密度网格分辨率（越高越细，显存/时间按 ^3 涨）
        #   smooth     = Taubin 平滑迭代（保体积，0=原始面）
        #   min_component = 丢弃顶点数少于此的连通块 → 专门用来剔漂浮碎块（碎片多时调大）
        workflow[str(nid)] = {
            "class_type": "SplatToMesh",
            "inputs": {
                "splat": [decode_node, 0],
                "resolution": int(tp_q.get("resolution", 384)),
                "kernel": int(tp_q.get("kernel", 5)),
                "smooth": int(tp_q.get("smooth", 0)),
                "level": float(tp_q.get("level", 0.4)),
                "min_component": int(tp_q.get("min_component", 500)),
                "min_opacity": 0.02,
                "color_sharpen": 2.0,
            }
        }
        mesh_node = str(nid); nid += 1
        
        # 11. Save as GLB（原生 SaveGLB 接受 SplatToMesh 的 MESH 类型）
        output_path = params.get("output_path", f"TripoSplat_{int(time.time())}.glb")
        workflow[str(nid)] = {
            "class_type": "SaveGLB",
            "inputs": {
                "mesh": [mesh_node, 0],
                "filename_prefix": "3d/" + Path(output_path).stem,
            }
        }
        
        return {"prompt": workflow, "negative_prompt": ""}
    
    # ═══════════════════════════════════════════════════════════
    # VIDEO WORKFLOW (MiniMax H3)
    # ═══════════════════════════════════════════════════════════
    
    def _build_video_workflow(self, params: Dict) -> Dict:
        """构建 MiniMax H3 视频生成工作流
        
        使用 ComfyUI 官方 H3 节点：
        UNETLoader → MiniMaxH3SigmaShift → KSampler
        CLIPLoader + VAELoader → MiniMaxH3ImageToVideo → positive + latent → KSampler
        KSampler → VAEDecode(视频) + VAEDecodeAudio(音频) → VHS_VideoCombine
        """
        model = params.get("model_config", {})
        input_image = params.get("input_image", "")
        prompt_text = params.get("prompt", "")
        width = params.get("width", model.get("resolution", [1344, 768])[0])
        height = params.get("height", model.get("resolution", [1344, 768])[1])
        fps = model.get("fps", 24)
        duration = model.get("duration", 5)
        # 2026-08-14：steps 支持 params 覆盖（quality 接线）；turbo 强度官方调教点 1.0
        steps = params.get("steps", model.get("steps", 8))
        seed = params.get("seed", -1)
        
        # turbo lora 加速（2026-08-13）：8步→4步，约 2x；quality 可显式关 turbo
        turbo_enabled = params.get("turbo_enabled", None)
        if turbo_enabled is None:
            turbo_enabled = bool(model.get("turbo_enabled", False))
        turbo_lora = model.get("turbo_lora", "")
        turbo_strength = model.get("turbo_lora_strength", 1.0)
        if turbo_enabled and turbo_lora and "steps" not in params:
            steps = model.get("turbo_steps", 4)
        
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)
        
        # Calculate length from duration (at 24fps, snapped to H3 grid)
        length = max(5, int(duration * fps))  # minimum 5 frames
        
        workflow = {}
        nid = 1
        
        # 1. UNETLoader - H3 model
        workflow[str(nid)] = {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": model.get("model", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
                "weight_dtype": "default"
            }
        }
        model_node = str(nid); nid += 1
        
        # 1.5 Turbo LoRA（可选）：LoraLoaderModelOnly 把加速 lora 挂到 model 上
        if turbo_enabled and turbo_lora:
            workflow[str(nid)] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": [model_node, 0],
                    "lora_name": turbo_lora,
                    "strength_model": turbo_strength,
                }
            }
            model_node = str(nid); nid += 1
        
        # 2. MiniMaxH3SigmaShift - 官方 sigma 校正
        workflow[str(nid)] = {
            "class_type": "MiniMaxH3SigmaShift",
            "inputs": {
                "model": [model_node, 0],
                "shift_video": 12.0,
                "shift_audio": 3.0
            }
        }
        sigma_node = str(nid); nid += 1
        
        # 3. CLIPLoader - MiniMax CLIP
        workflow[str(nid)] = {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": model.get("clip", "qwen3vl_32b_minimax_h3_int4_convrot.safetensors"),
                "type": model.get("clip_type", "minimax"),
                "device": "default"
            }
        }
        clip_node = str(nid); nid += 1
        
        # 4. VAELoader - video VAE
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": model.get("video_vae", "minimax_h3_video_vae_fp16.safetensors")}
        }
        video_vae_node = str(nid); nid += 1
        
        # 5. VAELoader - audio VAE
        workflow[str(nid)] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": model.get("audio_vae", "minimax_h3_audio_vae_fp32.safetensors")}
        }
        audio_vae_node = str(nid); nid += 1
        
        # 6. MiniMaxH3ImageToVideo (官方节点)
        i2v_inputs = {
            "clip": [clip_node, 0],
            "vae": [video_vae_node, 0],
            "prompt": prompt_text,
            "width": width,
            "height": height,
            "length": length,
        }
        workflow[str(nid)] = {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": i2v_inputs
        }
        i2v_node = str(nid); nid += 1
        
        # 7. LoadImage (if I2VA mode - has input image)
        if input_image:
            workflow[str(nid)] = {
                "class_type": "LoadImage",
                "inputs": {"image": input_image}
            }
            img_node = str(nid)
            workflow[i2v_node]["inputs"]["first_frame"] = [img_node, 0]
            nid += 1
        
        # 8. KSampler
        # 2026-08-14 调研：turbo 官方推荐 scheduler=simple（beta 仅 R2V 多参考场景更优）
        scheduler_name = "simple"
        workflow[str(nid)] = {
            "class_type": "KSampler",
            "inputs": {
                "model": [sigma_node, 0],
                "positive": [i2v_node, 0],
                "negative": [i2v_node, 0],
                "latent_image": [i2v_node, 1],
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": scheduler_name,
                "denoise": 1.0
            }
        }
        sampler_node = str(nid); nid += 1
        
        # 9. VAEDecode (视频帧)
        workflow[str(nid)] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [sampler_node, 0], "vae": [video_vae_node, 0]}
        }
        decode_node = str(nid); nid += 1
        
        # 10. VAEDecodeAudio (音频)
        workflow[str(nid)] = {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": [sampler_node, 0], "vae": [audio_vae_node, 0]}
        }
        audio_decode_node = str(nid); nid += 1
        
        # 11. VHS_VideoCombine
        workflow[str(nid)] = {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": [decode_node, 0],
                "audio": [audio_decode_node, 0],
                "frame_rate": float(fps),
                "loop_count": 0,
                "filename_prefix": "ai_workshop_video",
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True
            }
        }
        
        return {"prompt": workflow, "negative_prompt": ""}
    
    # ═══════════════════════════════════════════════════════════
    # SUBMISSION & POLLING
    # ═══════════════════════════════════════════════════════════
    
    def submit_workflow(self, workflow_data: Dict, model_config: Optional[Dict] = None,
                        stage: Optional[str] = None) -> str:
        """提交工作流到 ComfyUI（stage 决定实例：image→8188, video/3d→8189）"""
        if "prompt" not in workflow_data and "workflow" in workflow_data:
            workflow_data = {"prompt": workflow_data["workflow"]}
        
        payload = {
            "prompt": workflow_data["prompt"],
            "client_id": self.client_id
        }
        
        url = self._pick_url(stage)
        response = requests.post(
            f"{url}/prompt",
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        result = response.json()
        prompt_id = result.get("prompt_id", "unknown")
        self._prompt_url[prompt_id] = url  # 记录任务所属实例，wait 时用
        
        # Check for validation errors
        if "node_errors" in result:
            errors = result["node_errors"]
            if errors:
                logger.error(f"Workflow validation errors: {json.dumps(errors, indent=2)}")
        
        logger.info(f"ComfyUI workflow submitted ({'media' if url == self.media_url else 'image'}): {prompt_id}")
        return prompt_id
    
    def wait_for_completion(self, prompt_id: str, timeout: int = 600, 
                          progress_callback=None, thermal_guard: bool = False) -> Dict:
        """轮询等待任务完成

        掉线处理（2026-08-11 优化）：
        - 连接失败时静默重试（不刷日志），连续失败超阈值才记一次 warning
        - 每次失败后探测 ComfyUI 在线状态：
          * 若已离线 → 继续等待（自愈会自动拉起）
          * 若重新上线但 history 里已无此任务 → 任务已丢失，立即返回错误

        温度看门狗（2026-08-13 新增，thermal_guard=True 时启用）：
        - 每 ~9s 读一次 GPU 温度（重负载任务，保护 83°C 崩溃红线）
        - ≥80°C 记 warning；≥85°C 立即 interrupt 并返回错误
        """
        start_time = time.time()
        fail_streak = 0
        logged_offline = False
        last_temp_check = 0.0
        temp_warned = False
        TEMP_WARN = 83   # 警告阈值（宇凯确认 83-85°C 内可接受）
        TEMP_CRIT = 88   # 强制中断阈值（2026-08-14 从 85 调到 88，2.1 高质量档负载高会到 85°C）
        url = self._prompt_url.get(prompt_id, self.comfyui_url)  # 任务所属实例
        gpu_idx = 1 if url == self.media_url else 0              # 温度看门狗查对应卡

        while time.time() - start_time < timeout:
            try:
                # ── 温度看门狗（每 ~9s 检查一次，查任务所在 GPU）──
                if thermal_guard and time.time() - last_temp_check >= 9.0:
                    last_temp_check = time.time()
                    t = self.get_gpu_temp(gpu_idx)
                    if t is not None:
                        if t >= TEMP_CRIT:
                            logger.error(f"🌡️ GPU{gpu_idx} {t}°C 达危险阈值，中断任务")
                            self.interrupt()
                            return {"status": "error",
                                    "error": f"GPU 温度过高({t}°C)，已自动中断任务保护硬件，请降功耗墙后重试",
                                    "outputs": {}}
                        elif t >= TEMP_WARN and not temp_warned:
                            temp_warned = True
                            logger.warning(f"🌡️ GPU{gpu_idx} {t}°C 偏高（≥{TEMP_WARN}°C），注意监控")

                response = requests.get(
                    f"{url}/history/{prompt_id}",
                    timeout=8
                )
                fail_streak = 0
                logged_offline = False

                if response.status_code == 200:
                    history = response.json()
                    if prompt_id in history:
                        entry = history[prompt_id]
                        # Check for errors
                        status_info = entry.get("status", {})
                        if status_info.get("status_str") == "error":
                            error_msg = status_info.get("messages", [])
                            return {"status": "error", "error": str(error_msg), "outputs": {}}
                        
                        outputs = entry.get("outputs", {})
                        return {"status": "completed", "outputs": outputs}
                
                if progress_callback:
                    elapsed = int(time.time() - start_time)
                    progress_callback(f"ComfyUI 处理中... ({elapsed}s)")
                
                time.sleep(3)
            except Exception as e:
                fail_streak += 1
                # 连续失败才记日志（避免刷屏）；每 12 次（约 1 分钟）重报一次
                if fail_streak == 1 or fail_streak % 12 == 0:
                    logger.warning(f"Polling error (x{fail_streak}): {e}")

                # ComfyUI 掉线时：探测是否已恢复，恢复后检查任务是否丢失
                if fail_streak >= 2:
                    if self.is_running():
                        # ComfyUI 在线但本任务不在 history → 任务已随重启丢失
                        if not logged_offline:
                            logger.warning(f"ComfyUI 已重启，任务 {prompt_id[:8]} 已丢失")
                            logged_offline = True
                        return {
                            "status": "error",
                            "error": "ComfyUI 中途重启，任务已丢失，请重试",
                            "outputs": {},
                        }

                time.sleep(5)
        
        raise TimeoutError(f"ComfyUI task timeout after {timeout}s")
    
    def get_result_image(self, outputs: Dict, stage: Optional[str] = None) -> Optional[str]:
        """从输出中提取图像路径（stage 决定用哪个实例的 /view）"""
        for node_id, output in outputs.items():
            if "images" in output:
                images = output["images"]
                if images:
                    img = images[0]
                    return f"{self._pick_url(stage)}/view?filename={img['filename']}&type={img.get('type', 'output')}"
        return None
    
    def get_result_video(self, outputs: Dict, stage: Optional[str] = None) -> Optional[str]:
        """从输出中提取视频路径（stage 决定用哪个实例的 /view）"""
        for node_id, output in outputs.items():
            if "gifs" in output:
                videos = output["gifs"]
                if videos:
                    vid = videos[0]
                    return f"{self._pick_url(stage)}/view?filename={vid['filename']}&type={vid.get('type', 'output')}"
        return None
    
    def get_result_3d(self, outputs: Dict) -> Optional[str]:
        """从输出中提取 3D 模型完整路径（处理 subfolder + type）"""
        for node_id, output in outputs.items():
            # Check various output keys
            for key in ["mesh", "3d", "model_file", "GLB", "save_path"]:
                if key in output:
                    val = output[key]
                    if isinstance(val, list) and val:
                        item = val[0]
                        if isinstance(item, dict):
                            # SaveGLB 等原生节点: {filename, subfolder, type}
                            filename = item.get("filename", "")
                            if filename:
                                sub = item.get("subfolder", "") or ""
                                ftype = item.get("type", "output")
                                base = Path(self.comfyui_dir) if getattr(self, "comfyui_dir", None) else COMFYUI_DIR
                                full = Path(base) / "output"
                                if sub:
                                    full = full / sub
                                candidate = full / filename
                                if candidate.exists():
                                    return str(candidate)
                                # fallback: 相对当前工作目录
                                if Path(filename).exists():
                                    return filename
                        else:
                            path = str(item)
                            if path and Path(path).exists():
                                return path
        return None
    
    def upload_image(self, image_path: str, stage: str = "image") -> str:
        """上传图片到对应实例（image→8188，video/3d→8189）"""
        import mimetypes
        content_type = mimetypes.guess_type(image_path)[0] or "image/png"
        
        with open(image_path, "rb") as f:
            files = {"image": (Path(image_path).name, f, content_type)}
            response = requests.post(
                f"{self._pick_url(stage)}/upload/image",
                files=files,
                timeout=30
            )
        
        if response.status_code == 200:
            result = response.json()
            return result.get("name", Path(image_path).name)
        raise Exception(f"Upload failed: {response.text}")
