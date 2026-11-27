# 本机「铸想相关」本地模型清单（2026-11-27 删除前存档）

> 这条清单是在**删除前**生成的，用于日后重装对照。
> 机器：Gooxi SR201-D12RE-V2.0 · V100 32G · Windows · 2026-11-27

## 合计与去向

| 分区 | 大小 | 处理 |
|---|---|---|
| `D:\ComfyUI-New`（本体 + 全部权重） | **348.2 GB** | 删除 |
| `D:\comfy312`（ComfyUI 的 venv） | 15.0 GB | 删除 |
| `D:\ForgeForm-oss\models`（铸想登记的本地 3D/图像权重） | 23.9 GB | 删除 |
| `D:\ForgeForm-oss\work\research`（Direct3D-V3 / RigAnything / Paint 研究权重） | 16.4 GB | 删除 |
| `C:\Users\mac\.cache\huggingface` | 4.8 GB | 删除 |
| `D:\models`（hy3dgen / triposr / z-image 等） | 31.2 GB | 删除 |
| `D:\ollama-models`（Ollama 模型实体） | 34.9 GB | 删除 |
| `C:\Users\mac\AppData\Local\Programs\Ollama` + `C:\Users\mac\ollama` | 2.7 GB | 删除 |
| **合计** | **≈ 477 GB** | |

保留：`D:\ForgeForm-oss` 源码、`D:\forgeform-v3.8.4.zip` 安装包、`D:\铸想模型总览`（12 件成品）、`outputs/`（小）。

## 一、ComfyUI 里的模型（348.2 GB，按大小前 10）

| 大小 | 文件 |
|---|---|
| 25.1 GB | `models\text_encoders\qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors` |
| 20.0 GB | `models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| 20.0 GB | `models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| 19.1 GB | `models\diffusion_models\flux2-dev-q4_k_m.gguf` |
| 13.7 GB | `models\text_encoders\Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf` |
| 9.7 GB | `models\unet\Wan2.2-I2V-A14B-HighNoise-Q5_K_S.gguf` |
| 6.4 GB | `custom_nodes\ComfyUI-3D-Pack-main\Checkpoints\...\hunyuan3d-paintpbr-v2-1`（贴图） |
| 4.7 GB | 3D-Pack `hunyuan3d-v2-1` 形状权重 |
| 2.9 GB | `models\diffusers\...`（flux2klein、sd15 等） |
| 其余 | 视频/3D/控制网等约 200 GB |

**铸想实际用到的只有三组**（其余是别的项目的视频/图像模型）：
1. **Hunyuan3D 2.1 / 2mini / 2mv**（建模）—— 单图 512 档一次约 8–10 分钟
2. **Hunyuan3D Paint PBR 2.1**（贴图）—— 完整档约 20 分钟
3. **Qwen-Image / FLUX 等出图模型**（出图）—— 单张约 56 秒

## 二、铸想 `models/` 目录（23.9 GB）

| 大小 | 路径 | 用途 |
|---|---|---|
| 7.4 GB | `flux2klein4b\transformer\...safetensors` | 出图（本地 diffusers） |
| 2.9 GB | `flux2klein4b\text_encoder\model-00002-of-00002.safetensors` | 出图 |
| 1.6 GB ×2 | `sd15\unet\diffusion_pytorch_model.fp16.{bin,safetensors}` | 快速出图 |
| 0.47 GB ×2 | `sd15\text_encoder\...` | 出图 |

## 三、研究权重 `work/research/`（16.4 GB）

| 大小 | 路径 | 用途 |
|---|---|---|
| 4.8 GB | `precision\weights\Direct3D-V3\shape_transformers_1024\model.safetensors` | 精修 1024 档 |
| 4.8 GB | `precision\weights\Direct3D-V3\shape_transformers_512\model.safetensors` | 精修 512 档 |
| 3.2 GB | `precision\weights\dinov3-vith16plus\model.safetensors` | 精修图像编码 |
| 2.2 GB | `rig\models\riganything_ckpt.pt` | RigAnything 骨骼（52 关节） |

## 四、`D:\models`（31.2 GB）

| 大小 | 目录 | 用途 |
|---|---|---|
| 14.8 GB | `hy3dgen` | hy3dgen（Hunyuan3D 直连路线）权重 |
| 7.0 GB | `ms` | ModelScope 缓存 |
| 3.6 GB | `z-image-turbo-diffusers` | 出图（Z-Image） |
| 1.6 GB | `triposr` | 备选图生 3D |
| 1.2 GB | `hf` | HF 缓存 |
| 1.0 GB | `tiny-sd` | 冒烟测试用 |
| 0.9 GB | `scout-base` | 视觉/科研 scout |
| 0.8 GB | `hunyuan3d-2mini` | 2mini（快速建模） |
| 0.3 GB | `dino-vitb16` | 图像编码 |

## 五、Ollama（34.9 GB 模型 + 2.7 GB 程序）

| 模型 | 参数 | 大小 | 实测冷启动 | 用途 |
|---|---|---|---|---|
| `qwen2.5:3b` | 3.1B | 1.8 GB | **12.2 s** | 最快的本地对话选项 |
| `qwen3.8:latest` | 27.3B | 16.5 GB | 67.0 s | 之前铸想的对话大脑 |
| `gpt-oss:20b` | 20.9B | 12.9 GB | 66.5 s | 备选（本地实测回空，不推荐） |
| `zykt-scout-v2/v3(-q4)` | 494M | 0.4–0.9 GB | — | ZYKT 站点用 |
| `zykt-small` | 630M | 0.5 GB | — | ZYKT 站点用 |
| `zykt-scratch-*` | 29M | 0.06 GB | — | 占位 |
| `embeddinggemma` | 308M | 0.6 GB | — | 向量化 |

**实测结论：本地最快的是 `qwen2.5:3b`（冷启 12.2 s / 常驻 6.5 s）；27B 级都要 60 s 以上，不适合"打开就能聊"。**

## 六、删除后的影响

- 铸想：出图 / 建模 / 贴图 / 精修 / 骨骼（RigAnything 档）都不可用；
  **仍可用**：对话（接远程模型时）、多视图以外的几何功能（导入 GLB 转 STL）、模板骨骼（纯 Blender）、BVH 动画、能力工作台里不依赖权重的部分
- Minecraft 机器人、ZYKT 站点图片理解：依赖 Ollama，会一起失效（用户已知并同意）

## 七、重装顺序建议（按需，不必全装）

1. `pip install -r requirements.txt`（不含模型）
2. `python tools/doctor.py` 看缺什么
3. 只想要 3D：装 ComfyUI + Hunyuan3D 2.1（约 9 GB），其余跳过
4. 想要贴图：再加 Paint PBR 2.1（6.4 GB）
5. 想要精修：Direct3D-V3 512 档（4.8 GB + DINOv3 3.2 GB）
6. 想要骨骼：模板档零下载；RigAnything 2.2 GB
7. 想要快速对话：`qwen2.5:3b`（1.8 GB）；或直接接远程 API，不装本地
