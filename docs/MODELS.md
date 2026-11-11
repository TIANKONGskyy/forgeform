# 需要哪些模型、从哪下

铸想自己不下载任何权重 —— 它按 `config.json` 里写的**文件名**去 ComfyUI 的模型目录里找。
所以「跑不起来」最常见的原因是：文件名字对不上，或者放错子目录。

下面所有下载链接都是 ComfyUI 官方 blueprint 里写的那一条（我把 `blueprints/*.json` 里的
`"url"` 字段全抽出来了）。国内直连 huggingface.co 不通，把域名换成 `hf-mirror.com` 就行。

---

## 先选档位

| 档位 | 能做什么 | 合计下载 | 显存 |
|---|---|---|---|
| **A. 只出图** | 文生图 / 参考图 i2i | ≈ 20 GB | 12 GB+ |
| **B. 出图 + 建模** | 一句话 → STL 全链路 | ≈ 30 GB | 16 GB+（512 档要 20 GB） |
| **C. 完整** | 再加视频 | ≈ 100 GB | 32 GB |

大多数人只要 B。C 里的视频那条线（MiniMax H3）光文本编码器就 24 GB，显存不够别装。

---

## A. 出图（三选一，不用全装）

### A1. Qwen-Image-2.1 —— 作者在用的主力

| 文件 | 放哪 | 本机大小 |
|---|---|---|
| `qwen_image_2.1_int8_convrot.safetensors` | `models/diffusion_models/` | 6.8 GB |
| `qwen3vl_8b_int8_convrot.safetensors` | `models/text_encoders/` | 8.7 GB |
| `qwen_image_2.1_vae_bf16.safetensors` | `models/vae/` | 0.6 GB |

⚠️ 这三个名字是本机自转的 int8 量化版（`_convrot` 后缀 = 那个转换脚本的产物）。
**官方 Comfy-Org 提供的是 fp8 / bf16 原件，文件名不同**：

```powershell
# 官方原件（fp8，显存占用略高，效果一致）
curl -L -o "$env:COMFYUI_DIR\models\diffusion_models\qwen_image_fp8_e4m3fn.safetensors" `
  "https://hf-mirror.com/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors"

curl -L -o "$env:COMFYUI_DIR\models\text_encoders\qwen3vl_8b_fp8_scaled.safetensors" `
  "https://hf-mirror.com/Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_8b_fp8_scaled.safetensors"

curl -L -o "$env:COMFYUI_DIR\models\vae\qwen_image_vae.safetensors" `
  "https://hf-mirror.com/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors"
```

下完把 `config.json → models.image.qwen21_ref` 里的三个文件名改成你实际的文件名。
**改名不是作弊，是必须的** —— 铸想只认配置里的字。

### A2. FLUX.1-dev —— 最稳的通用档

```powershell
# 主模型（fp8 整包，含 CLIP+T5+VAE 的版本叫 Flux1-dev-fp8）
curl -L -o "$env:COMFYUI_DIR\models\diffusion_models\flux1-dev.safetensors" `
  "https://hf-mirror.com/Comfy-Org/flux1-dev/resolve/main/flux1-dev.safetensors"

# 文本编码器
curl -L -o "$env:COMFYUI_DIR\models\text_encoders\clip_l.safetensors" `
  "https://hf-mirror.com/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\text_encoders\t5xxl_fp8_e4m3fn.safetensors" `
  "https://hf-mirror.com/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors"

# VAE
curl -L -o "$env:COMFYUI_DIR\models\vae\ae.safetensors" `
  "https://hf-mirror.com/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors"
```

### A3. Z-Image Turbo —— 最省显存（8 步出图）

```powershell
curl -L -o "$env:COMFYUI_DIR\models\diffusion_models\z_image_turbo_bf16.safetensors" `
  "https://hf-mirror.com/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\text_encoders\qwen_3_4b.safetensors" `
  "https://hf-mirror.com/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\vae\ae.safetensors" `
  "https://hf-mirror.com/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors"
```

这套合计约 12 GB，8 GB 显存也能跑（见 `docs/CONFIG.md` 的「8 GB」那档）。

---

## B. 3D 建模（在 A 之上，再加约 8 GB）

### B1. Hunyuan3D 2.1 —— 质量最高，作者在用

两个来源，**选一个**：

**① ComfyUI 官方重打包（简单，自带节点）**

```powershell
curl -L -o "$env:COMFYUI_DIR\models\checkpoints\hunyuan_3d_v2.1.safetensors" `
  "https://hf-mirror.com/Comfy-Org/hunyuan3D_2.1_repackaged/resolve/main/hunyuan_3d_v2.1.safetensors"
```

6.86 GB，放进 `models/checkpoints/`。需要 ComfyUI 里能跑 Hunyuan3D 的原生节点。
对应 `config.json` 里 `backend: "hy21"` 那条路。

**② ComfyUI-3D-Pack（作者实际用的，质量最好但也最折腾）**

```powershell
# 放 models/diffusion_models/
hunyuan3d-dit-v2-1_fp16.ckpt         6.9 GB
hunyuan3d-vae-v2-1_fp16.ckpt         0.6 GB
```

这两个来自 3D-Pack 自己的安装脚本（`custom_nodes/ComfyUI-3D-Pack-main/install.py` 会拉）。
3D-Pack 需要**编译 CUDA 扩展**（`Gen_3D_Modules`），是这个项目里最容易卡住的一步：
sm_70（V100）上要改几个 `int64` 的宏，见 `docs/teacache-compat-patch.diff` 的同类做法。

### B2. TripoSplat —— 轻量备选，约 4 GB

```powershell
curl -L -o "$env:COMFYUI_DIR\models\diffusion_models\triposplat_fp16.safetensors" `
  "https://hf-mirror.com/VAST-AI/TripoSplat/resolve/main/diffusion_models/triposplat_fp16.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\vae\triposplat_vae_decoder_fp16.safetensors" `
  "https://hf-mirror.com/VAST-AI/TripoSplat/resolve/main/vae/triposplat_vae_decoder_fp16.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\vae\triposplat_vae_encoder_fp16.safetensors" `
  "https://hf-mirror.com/VAST-AI/TripoSplat/resolve/main/vae/triposplat_vae_encoder_fp16.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\vae\flux2-vae.safetensors" `
  "https://hf-mirror.com/Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\clip_vision\dino_v3_vit_h.safetensors" `
  "https://hf-mirror.com/VAST-AI/TripoSplat/resolve/main/clip_vision/dino_v3_vit_h.safetensors"
```

TripoSplat 只需要 ComfyUI 原生节点（`backend: "native_comfyui"`），不用编译 C++，
所以**第一次装建议先跑通它**，再上 Hunyuan3D。

---

## C. 视频（可选，约 55 GB）

```powershell
# 主模型
curl -L -o "$env:COMFYUI_DIR\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors" `
  "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"

# 文本编码器（24 GB，显存杀手）
curl -L -o "$env:COMFYUI_DIR\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" `
  "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"

# VAE ×2
curl -L -o "$env:COMFYUI_DIR\models\vae\minimax_h3_video_vae_fp16.safetensors" `
  "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors"
curl -L -o "$env:COMFYUI_DIR\models\vae\minimax_h3_audio_vae_fp32.safetensors" `
  "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors"

# Turbo LoRA（4 步，不装就要 30+ 步）
curl -L -o "$env:COMFYUI_DIR\models\loras\minimax_h3_turbo_4step_pruned_comfyui.safetensors" `
  "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main/loras/minimax_h3_turbo_4step_pruned_comfyui.safetensors"
```

---

## 下完怎么验

不要靠"我觉得下对了"。跑：

```powershell
python tools/doctor.py
```

它会逐个检查 `config.json` 里点名的文件在不在 ComfyUI 目录里：

```
[OK]   image.qwen21_ref  3/3 找到
[WARN] 3d.triposplat     1/2 找到  → 缺: dino_v3_vit_h.safetensors
```

比这更硬的验证：打开 ComfyUI，在对应节点的下拉框里找你的文件名。**下拉框里有 = 位置对了**。
那里没有，改铸想的配置也没用。

---

## 几个坑

- **文件名必须和 `config.json` 一字不差**（含大小写）。铸想不做模糊匹配。
- **`text_encoders` 和 `clip` 是两个目录**。新版节点读 `text_encoders/`，老节点读 `clip/`。
  放错了表现是 "value not in list"。
- **GGUF 文件需要 ComfyUI-GGUF 节点**。`flux-2-klein-9b-Q5_K_M.gguf`、
  `z_image_turbo-Q8_0.gguf` 这类都是 GGUF，没装 `ComfyUI-GGUF` 会加载失败。
- **14B 以上的文本编码器不要把 `--lowvram` 当救命药**。V100/sm_70 上它会让采样阶段崩
  （`CUDA error: operation not supported`）。宁可换小一点的量化版。
- **磁盘**：完整档 100 GB 只是权重，加上 ComfyUI 输出和 `projects/`，留 200 GB 才舒服。
