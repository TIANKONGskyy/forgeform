# AI 创意工坊 真实遥测数据汇总（2026-09-05 实测）

> ⚠️ **历史数据。** 采集时主力卡是 RTX 3080 20GB 改装卡（功耗墙 220W），
> 现在是 V100 32G，耗时会变，模型档位也更新过。保留这份是为了对照
> 「同一套流程换一张卡差多少」，不是当前基准。当前基准见 README「实测耗时」。

> 采集方式：`telemetry_sampler.py` 每秒采样 nvidia-smi + psutil，真实运行每个模型测得。
> 硬件：单卡 RTX 3080 20GB（功耗墙 220W）+ EPYC 7R13 + 128GB RAM。

---

## 一、全链路各模型实测数据

| 步骤 | 模型 | VRAM 载入 | RAM 载入 | GPU 峰值 | 功耗峰值 | 温度峰值 | 单次耗时 |
|---|---|---|---|---|---|---|---|
| ① 提示词扩写 | Qwen3-Coder-30B-A3B (MoE Q4) | 677→18.8GB (+18.1GB) | 21→40GB (+19GB) | 100% | **51W** | 46°C | 载入 ~22s，推理 4.7s/千字 |
| ② 图片生成 | FLUX.1-dev FP8 (28步) | 695→18.8GB (+18.1GB) | 21→40GB (+19GB) | 100% (均值96%) | **38W** | 63°C | 530s (8.8min) |
| ④ 3D 重建 | TripoSplat (fp16) | 857→16.0GB (+15.1GB) | 21→40GB (+19GB) | 100% (均值95%) | 39W | 63°C | 402s (6.7min) |
| ④ 3D 兜底 | Hunyuan3D 2.1 (ShapeGen) | 载入至 13.8GB | — | 100% | 39W | 65°C | **2h+ 未完成** |
| ⑤ STL 优化 | trimesh+pymeshlab (纯CPU) | 0 (不占 GPU) | +2GB | 0% | — | — | 115s |
| ⑥ 视频生成 | MiniMax H3 FastH3 (4步) | 641→20.0GB (+19.4GB) | 19→65GB (**+46GB**) | 100% (均值70%) | 41W | 63°C | 冷启动 24min |

---

## 二、关键洞察（真实瓶颈，视频值得点出）

1. **20GB 显存是最大瓶颈**：FLUX 28 步需 ~27GB 权重（UNET 17.2GB + T5 9.5GB + VAE），触发 ComfyUI「RAM pressure cache」→ GPU util 96% 但**功耗仅 38W**（GPU 空转等显存↔内存换页），530s 里大量时间在搬数据而非计算。

2. **CLIP 32B 编码吃 CPU + RAM**：FastH3 的 qwen3vl_32b 文本编码器在 CPU 上跑，载入吃 25GB RAM，编码期 CPU 100%、GPU 空转 ~5 分钟，之后才进 GPU 采样。

3. **int8_convrot 冷启动成本高**：FastH3 模型首次加载要做 8 分 45 秒权重转换（"Model Initialization"），之后热启动每步仅 ~110-160s。

4. **STL 是纯 CPU 且单核脉冲**：mesh 简化/补洞 CPU 峰值 100% 但呈脉冲（单核 C++ 扩展），414 万面 → 71 万面，115s。

5. **MoE 推理功耗低**：Qwen3-Coder-30B 是 30B MoE（3B 激活），int8 量化下单 batch 推理功耗峰值仅 51W，扩写 4.7s/千字。

---

## 三、曲线图

见 `docs/telemetry_charts/`：

| 图 | 内容 |
|---|---|
| `qwen30b_vram.png` / `qwen30b_compute.png` | Qwen 载入 + 计算 |
| `flux_vram.png` / `flux_compute.png` | FLUX 载入 + 计算 |
| `triposplat_vram.png` / `triposplat_compute.png` | TripoSplat |
| `stl_vram.png` / `stl_compute.png` | STL 纯 CPU |
| `fasth3_vram.png` / `fasth3_compute.png` | FastH3 |

---

## 四、原始数据

CSV 在 `D:/AI-Workshop/temp/`：
`telemetry_qwen30b.csv` · `telemetry_flux.csv` · `telemetry_triposplat.csv` · `telemetry_stl.csv` · `telemetry_fasth3.csv`
