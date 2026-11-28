# 换模型实测（2026-11-28 傍晚）

用户要求："试试别的生图建模和语言模型"。以下全部在本机 V100 32G 真跑。

## 一、生图：两个新模型

| 模型 | 权重 | 档位 | 耗时 | 产物 | 结论 |
|---|---|---|---|---|---|
| **FLUX.1-schnell FP8** | 16.4 GB + 编码器 5 GB | 4 步 1024² | **25 s** | 321 KB | ✅ 可用（现有 flux 工作流直接跑） |
| **SDXL Base 1.0** | 6.6 GB（单 checkpoint） | 25 步 1024² | **10 s** | 774 KB | ✅ 可用（新增 checkpoint 工作流分支） |

对比已有：z_image 18–21 s、klein 28 s、flux2_dev 58 s、flux 61 s、qwen 464 s。

**代码改动**：SDXL 这类"UNet+CLIP+VAE 打包在一个文件"的模型原来跑不了，
新增 `_build_sdxl_workflow`（CheckpointLoaderSimple → CLIPTextEncode×2 →
EmptyLatentImage → KSampler(dpmpp_2m/karras) → VAEDecode），按 `family: sdxl` 派发。

## 二、建模：Hunyuan3D-2.0

| 模型 | 耗时 | 面数 | 水密 | 产物 |
|---|---|---|---|---|
| **Hunyuan3D-2.0（30 步，octree 256）** | 加载 47 s + 推理 34 s | **321,612** | 否 | 5.6 MB GLB |

对比已有：2mini 69 s / 14.8 万面、2.1@256 105 s / 15.1 万面、2.1@512 676 s / 40 万面。
2.0 面数明显更高（32 万），但这一张实测不水密，打印前要走质量闸/修补。

## 三、语言模型：Qwen2.5 两档（做提示词扩写）

| 模型 | 显存 | 加载 | 单次扩写 | 输出质量 |
|---|---|---|---|---|
| Qwen2.5-1.5B-Instruct | ~3 GB | 5 s | **1.4–2.1 s** | ⚠️ 输出中文、IMAGE 段没转英文，"六边形、圆柱体"自相矛盾 |
| **Qwen2.5-7B-Instruct** | ~15 GB | 18 s | **2.1–2.7 s** | ✅ 英文 IMAGE 段 + 中文 3D 段，格式稳定 |

7B 实测输出（输入"做个直径 90mm 的六边形笔筒"）：

```
IMAGE: single object, pure white background, product photo of a 90mm diameter hexagonal pen holder
3D: 单一实体的六边形笔筒模型，要求水密性好，壁厚至少为2mm，并且底面要平整。
```

比规则兜底（`prompt_enhance.py`）自然得多，且 2.7 秒可接受。

## 四、顺手修的 bug

`friendly_errors` 把 `400 Bad Request ... :8188/prompt` 误判成"ComfyUI 没在运行"
（用了裸端口号 8188 做匹配）→ 改成只认 `Max retries / Connection refused / Errno 10061/111`，
并补了回归测试。

## 五、复现

```powershell
python tools/install_comfy.py                 # ComfyUI 环境
python -c "from backend.pipeline import Pipeline; ..."   # 见上表参数
```
