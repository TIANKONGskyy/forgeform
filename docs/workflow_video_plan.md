# AI 创意工坊 全链路工作流梳理

> 本文是给「工作流介绍视频」写的脚本草稿，模型档位停留在 2026-09 那一版
> （FLUX 出图 / TripoSplat 建模）。当前默认档位见 README「实测耗时」与 `config.example.json`。

> 目标：为「介绍工作流」的视频提供逐步骤脚本 + 每步模型的精简介绍 + 真实遥测数据（内存增长曲线 / CPU-GPU 占用与功耗曲线）。

---

## 链路总览

```
文字(短提示词)
   │  ① 提示词扩写        Qwen3-Coder-30B-A3B (llama-server :8080)
   ▼                      + qwen3:0.6b 翻译 (Ollama :11434)
【IMAGE】+【3D】双段提示词
   │  ② 图片生成          FLUX.1-dev FP8 (ComfyUI :8188, 28步)
   ▼
产品白底图 (1024×1024 PNG)
   │  ③ 视频生成(可选)    MiniMax H3 (ref2va int8 21GB + qwen3vl CLIP)
   ▼
展示动画 (mp4)
   │  ④ 3D 生成           TripoSplat (默认) / Hunyuan3D 2.1 (fallback)
   ▼
三角网格 (GLB)
   │  ⑤ STL 优化          trimesh + pymeshlab + fast_simplification (纯 CPU)
   ▼
可打印 STL (≤5万面, 水密, 平底)
   │  ⑥ 拓竹直发          bambu-cli → A1 / A1 mini
   ▼
实物打印
```

---

## 步骤 ① 提示词扩写（LLM）

| 项 | 值 |
|---|---|
| 模型 | **Qwen3-Coder-30B-A3B**（MoE 30B，Q4_K_M GGUF 量化） |
| 服务 | llama-server `:8080`（常驻别名 `qwen3-coder-30b`） |
| 副模型 | qwen3:0.6b（中英翻译，Ollama `:11434`，用完即卸载） |
| 作用 | 短提示词 → 【IMAGE】艺术段（FLUX 用，120-180词）+【3D】技术段（打印用，80-120词） |
| 权重 | `D:\ollama-models\_download\Qwen3-30B-A3B-Q4_K_M.gguf` |

**一句话**：30B 的 MoE 专家模型把「手机支架」扩写成两段结构化的专业提示词，并保证支架是空的、纯白底、可打印。

---

## 步骤 ② 图片生成（文生图）

| 项 | 值 |
|---|---|
| 默认模型 | **FLUX.1-dev FP8**（28 步，euler/simple，guidance 3.5） |
| 权重 | `flux1-dev-fp8.safetensors`（17.2GB） |
| VAE | `ae.safetensors` |
| CLIP | `clip_l.safetensors` + `t5xxl_fp8_e4m3fn.safetensors` |
| 备选 | FLUX.2 Klein 9B Q5（8步）/ FLUX.2 Dev 32B Q4 / Z-Image Turbo 6B |
| 输出 | 1024×1024 纯白底产品图 |

**一句话**：FLUX.1-dev 用 28 步把扩写后的英文提示词渲染成对称、正对、纯白底的产品图——这是后面 3D 重建的「输入基准」。

---

## 步骤 ③ 视频生成（可选，图生视频）

| 项 | 值 |
|---|---|
| 模型 | **MiniMax H3**（33B 全模态，ref2va int8 量化 21GB） |
| CLIP | qwen3vl_32b（CPU 编码） |
| VAE | video_vae fp16 + audio_vae fp32 |
| 加速 | ref2v turbo LoRA（8 步） |
| 输出 | 1344×768 / 5s / 24fps 展示动画 |

**一句话**：MiniMax H3 把产品首帧变成部件升起、展示、归位的 15 秒组装动画（视频/音频全模态）。

---

## 步骤 ④ 3D 生成（图生 3D）

| 项 | 值 |
|---|---|
| 默认模型 | **TripoSplat**（triposplat_fp16 7.4GB + dino_v3_vit_h 视觉编码 + flux2-vae） |
| fallback | Hunyuan3D 2.1（7GB 权重，ShapeGen，约 3 分钟/个） |
| 输出 | GLB 三角网格 |

**一句话**：TripoSplat 把一张白底产品图重建成水密三角网格（default 首选），失败时 Hunyuan3D 2.1 兜底。

---

## 步骤 ⑤ STL 优化（纯 CPU）

| 项 | 值 |
|---|---|
| 工具 | trimesh + pymeshlab + pymeshfix + fast_simplification |
| 动作 | 加 3mm 平底座 → 布尔合并 → 补洞 → 去浮空 → 简化到 ≤5 万面 → 水密检查 |
| 输出 | 可打印 STL（120mm 最长轴） |

**一句话**：纯 CPU 的网格工程——补洞、加平底、精简到 5 万面，保证 FDM 直接能打。

---

## 步骤 ⑥ 拓竹直发（打印）

| 项 | 值 |
|---|---|
| 工具 | bambu-cli（bambu.exe） |
| 目标 | 拓竹 A1（192.168.1.136）/ A1 mini（192.168.1.106） |
| 动作 | SSDP 发现 → 上传 STL → 切片 → 打印 |

**一句话**：bambu-cli 通过局域网把 STL 直接推给拓竹打印机开始打印。

---

## 遥测采集计划（真实数据）

采集工具：`telemetry_sampler.py`（每秒采样 nvidia-smi + psutil）。

对每个模型采集两段曲线：
1. **载入曲线**：RAM used / VRAM used 随时间增长（模型 load 阶段）
2. **计算曲线**：CPU% / GPU util% / GPU 功耗 W 随时间（推理阶段）

待采集模型清单：
| # | 模型 | 采集方式 |
|---|---|---|
| 1 | Qwen3-Coder-30B-A3B | llama-server 启动 + 一次扩写 |
| 2 | FLUX.1-dev FP8 | ComfyUI 提交一张 1024×1024 |
| 3 | TripoSplat | ComfyUI 提交一次图生 3D |
| 4 | Hunyuan3D 2.1 | ComfyUI 提交一次 ShapeGen |
| 5 | STL 优化 | 直接跑 mesh_optimizer（CPU 曲线） |
| 6 | MiniMax H3 FastH3 | ComfyUI 提交 4 步 T2V |

> 真实数据填充后，本表替换为实测数值。
