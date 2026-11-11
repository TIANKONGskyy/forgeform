# 不用 ComfyUI 跑铸想

铸想原来只有一条路：把工作流 JSON 提交给 ComfyUI。现在有三种：

```jsonc
"backend": { "mode": "comfyui" }   // 默认：只用 ComfyUI，行为与以前完全一致
"backend": { "mode": "direct" }    // 只用直连：diffusers / hy3dgen，不碰 ComfyUI
"backend": { "mode": "auto" }      // 先探测，再按阶段自动路由（推荐新用户）
```

`config.example.json` 里默认给的是 **auto**。

---

## auto 模式在做什么

启动时花不到 1 秒做一次探测（只发 GET、2.5 秒超时、**绝不拉起任何服务**），
然后为每个阶段挑一个后端：

```
出图  → ComfyUI 活着且有出图权重？ 用它（省得重下 20 GB）
        否则本地有 diffusers 模型？ 直连
建模  → ComfyUI + 3D-Pack？        用它
        否则 hy3dgen 有权重？      直连
对话  → Ollama → llama.cpp → vLLM → LM Studio → 谁活着用谁
        都没有                      关掉（纯规则模式）
```

探测项：

| 框架 | 怎么发现 | 判据 |
|---|---|---|
| Ollama | `:11434/api/tags` | 能列出模型 |
| ComfyUI | `:8188/system_stats` | 服务活着 + 扫 `models/` 有没有出图权重、有没有 3D-Pack |
| llama.cpp / vLLM / LM Studio | `:8080` `:8091` `:8000` `:1234` 的 `/v1/models` | 能列出模型 |
| 本地 diffusers | 扫 `model_index.json` | 找到完整 pipeline 目录 |
| hy3dgen | `import hy3dgen` | 装了，且 shapegen/texgen 可导入 |

**ComfyUI 活着但没出图权重**时不会硬塞给它 —— 出图会改用直连。这个判断是从
「扫 `models/checkpoints|diffusion_models|unet` 有没有权重文件」来的。

看这台机器实际探到什么（`python tools/doctor.py`）：

```
[OK]   Ollama              http://127.0.0.1:11434     能: 对话  （10 个模型）
[OK]   ComfyUI             http://127.0.0.1:8188      能: 出图、建模  （Tesla V100，32G · 出图权重 12，VAE 13，3D-Pack 已装）
[OK]   llama.cpp (8091)    http://127.0.0.1:8091/v1   能: 对话  （1 个模型）
[OK]   本地 diffusers 模型                            能: 出图  （2 个）
[OK]   hy3dgen（Hunyuan3D Python 包）                  能: 建模  （已安装（shapegen + texgen））
[OK]   自动路由            出图→comfyui，建模→comfyui，对话→ollama
```

**一个都没有**的时候，`doctor` 会直接说清楚两条路：

```
[FAIL] 自动路由  出图→，建模→
         → 出图、建模 没有可用后端：装 ComfyUI 或 python tools/fetch_model.py 拉一个 diffusers 模型
```

---

## 直连模式（direct）的细节

### 状态

| 阶段 | 直连（direct） | 依赖 |
|---|---|---|
| 出图 | ✅ **端到端已验证**（真模型出真图） | `diffusers`（原生支持 Flux / Qwen-Image / Z-Image，实测 0.39.0 全有） |
| 3D | ✅ **端到端已验证**（比 ComfyUI 快 7.7 倍） | `hy3dgen` 2.0.2 + `tencent/Hunyuan3D-2mini`（3.96 GB） |
| 视频 | ❌ 不支持 | MiniMax H3 只有 ComfyUI 节点实现，没有独立 pipeline |

### 直连 3D 的实测（2026-11-11，V100 32G）

下载 `tencent/Hunyuan3D-2mini`（**只挑 mini 版，3.96 GB** —— 整个仓库 23.5 GB 是因为还有
fast/turbo 变体和 .ckpt 重复副本，用 `--include` 过滤掉）：

```
python tools/fetch_model.py --repo tencent/Hunyuan3D-2mini \
    --dest D:/models/hy3dgen/hunyuan3d-2mini \
    --include '^hunyuan3d-dit-v2-mini/' --include '^hunyuan3d-vae-v2-mini/' \
    --exclude '\.ckpt$' --threads 8
```

跑 `tools/bench_hy3d.py`：

```
[1] 加载 hunyuan3d-2mini/hunyuan3d-dit-v2-mini      42.0s
[2] 推理 steps=30 octree=256                         13.1s
    faces=480024  vertices=240001  watertight=False
[3] 产物 bench_hy3d.glb (8.2 MB)
```

**注意 `watertight=False`** —— hy3dgen 出的原始网格不闭合（48 万面）。
交给铸想现成的修复链（`mesh_optimizer`，和 ComfyUI 模式完全同一套）：

```
输入: faces=480024 watertight=False
优化耗时 10.8s
输出: faces=199748 watertight=True  bbox=[119.95, 50.46, 107.43]
```

### 和 ComfyUI 模式的对比（同一张输入图，同一个物件）

| 路径 | 加载 | 推理 | 面数 | 水密 | 修复 |
|---|---|---|---|---|---|
| ComfyUI + Hunyuan3D 2.1（512 octree） | — | **505 s** | 200,000 | ✅ 直接水密 | 5.8 s |
| 直连 hy3dgen + 2mini（256 octree） | 42 s | **13.1 s** | 480,024 | ❌ | 10.8 s |

**直连快 7.7 倍**（505+6 vs 42+13+11 ≈ 66 s）。原因是 2mini 比 2.1 小得多、octree 也低一档 ——
质量上 2mini 的面更多但细节略糊，2.1 更干净。要质量用 ComfyUI 的 2.1，要速度用直连连 2mini。

两个路径可以共存：`auto` 模式有 ComfyUI 就走 2.1，没有就走 2mini。

### 3D 这条线：auto 会用本地权重，direct 不会

实测（2026-11-11）：用 `auto` 模式跑完整 3D，`plan = {'image': 'comfyui', 'mesh': 'comfyui', 'text': 'ollama'}`，
走的是 ComfyUI + 本地 Hunyuan3D 2.1 权重，**没有下载任何东西**：

```
[hybrid] 路由: image→comfyui, mesh→comfyui, text→ollama
[3d] success=True
[3d] faces=200000 vertices=99988 watertight=True bbox=[1.99, 1.91, 0.75]
[stl] success=True  → model_1794387020_optimized.stl (80000 faces, 80mm)
[done] 总耗时 412.7s
```

**为什么不让 direct 也用它**：本机那两个 Hunyuan3D 权重是 ComfyUI 重打包格式
（`conditioner.* / model.* / vae.*`，`model` 下是 `blocks.N.*`），而 hy3dgen 要的是
`config.yaml` + `double_blocks.* / single_blocks.*`。层名和结构定义都对不上，
不是改文件名能解决的 —— 详见 [MODELS.md](MODELS.md) 的「3D 权重不能跨框架互换」。

所以 `auto` 的价值在这：**有 ComfyUI 就用它跑 3D，不重复下另一套 3 GB 权重**。
真要走直连 3D，得下 `tencent/Hunyuan3D-2mini`（3 GB，hy3dgen 认的目录结构）。

### 出图这条线的实测（2026-11-11，V100 32G）

**用本机真实的大模型跑的**（`D:\AI-Workshop\finetune\flux2klein-base`，32.3 GB，完整 diffusers 格式）：

```
[direct] 加载 Flux2KleinPipeline ← D:\AI-Workshop\finetune\flux2klein-base  (offload=model)
[direct] 出图 flux2klein (Flux2KleinPipeline) 512x512 8步 …
[direct] 出图完成 166.3s → direct_1794382971_eec39f.png
job=1c74e2d8  推理 193.6s
http://127.0.0.1:60540/view?filename=images/direct_1794382971_eec39f.png
HTTP 200  242670 字节
✅ 端到端通过，总耗时 193.8s
```

逐步耗时（offload 模式）：

```
step 1: 68.95s   ← 第一次把 17 GB transformer 换进显存
step 2: 29.96s
step 3: 17.50s
step 4: 11.64s
step 5~8: 8.4s → 4.4s
```

也用小模型验证过一次更纯粹的链路（`segmind/tiny-sd`，990 MB）：

```
[direct] 出图完成 0.9s → direct_1794381220_b9625a.png
job=72a7c48d  推理 40.5s          ← 39s 是模型加载，真正采样 0.9s
HTTP 200  64064 字节
```

两次验证的都是**整条链**：配置/插件 → `DirectBackend` → diffusers 加载 → 采样 → 落盘 →
伪 `/view` 协议 → `requests` 拿回。ComfyUI 全程没参与。

### 32 GB 模型塞 32 GB 卡：`offload`

`FLUX.2 Klein` 本体 33 GB（transformer 17 + text_encoder 15 + vae 0.2），比显存还大。
插件 YAML 里写 `offload: model`，`DirectBackend` 就会调 `enable_model_cpu_offload()`：
按子模块换入换出，第一步慢（69 s），后面几步就正常了（4-8 s）。

---

## 怎么开

```jsonc
// config.json
{
  "backend": { "mode": "direct" },

  "models": {
    "image": {
      "z_image": {
        "backend": "z_image",                                  // 决定用哪个 diffusers pipeline
        "path": "D:/models/z-image-turbo-diffusers"            // 本地 diffusers 目录，或 HF repo id
      }
    },
    "3d": {
      "hy_mini": {
        "backend": "hy3dgen",
        "path": "D:/models/hunyuan3d-2mini",
        "octree": 256,
        "steps": 30
      }
    }
  },

  "stages": {
    "image": { "default_model": "z_image" },
    "3d":    { "default_model": "hy_mini" }
  }
}
```

`backend` 字段和 ComfyUI 模式共用同一套取值，只是含义变成"选哪个 diffusers 类"：

| `backend` | diffusers 类 |
|---|---|
| `flux` | `FluxPipeline` |
| `flux2` | `Flux2Pipeline` |
| `flux2klein` | `Flux2KleinPipeline` |
| `qwen_image` | `QwenImagePipeline` |
| `z_image` | `ZImagePipeline` |
| `hy3dgen`（3D） | `hy3dgen.shapegen.Hunyuan3DDiTFlowMatchingPipeline` |

---

## 它是怎么骗过 `pipeline.py` 的

`pipeline.py` 只认 ComfyUI 那套协议：`build_workflow → submit_workflow → wait_for_completion
→ get_result_image → requests.get(url)`。而它只调 9 个方法、25 处，全部集中在这条链上。

`DirectBackend` 的做法是**伪造同一套协议**：

1. `build_workflow()` 把参数打包成一个 dict（不是 ComfyUI 的节点图）
2. `submit_workflow()` **同步**执行推理，产物落到 `outputs/`
3. `wait_for_completion()` 直接把产物吐出去（已经跑完了，没有轮询）
4. `get_result_image()` 返回 `http://127.0.0.1:<随机端口>/view?filename=images%2Fx.png`
   —— 形状和 ComfyUI 的 `/view` 完全一样

第 4 点用了一个内置的 `ThreadingHTTPServer`（只监听 127.0.0.1，随进程退出）。
为什么不用 `file://`：`pipeline` 里是 `requests.get(url)`，requests 不认 file 协议。
为什么要伪造 URL 而不是改 pipeline：改 pipeline 要动 5 处下载逻辑，伪造 URL 一处都不用动。

验证这套协议的是 `tests/test_direct_backend.py` —— 9 个用例，不加载真模型，
把 `_run_image` 换成写个占位 PNG，然后断言「URL 能 200 下载回来」。

---

## 显存怎么管

ComfyUI 帮你换入换出模型，直连就得自己做。`DirectBackend` 里有个 `_ModelKeeper`：
同一时刻只留一个模型，切换时 `del` + `gc.collect()` + `torch.cuda.empty_cache()`。

这比 ComfyUI 粗，但没有额外的显存开销（少一个常驻的 ComfyUI 进程）。

---

## 在不同机器上跑

`quant` 和 `offload` 的目的是：**同一份插件配置，换台机器不用改**。
`quant: auto` 会量当前可用显存，再决定加载方式。

| 显存 | 出图 | 3D | 来源 |
|---|---|---|---|
| **32 GB** | Klein(33G) 走 offload；<26G 的模型整块 | Hunyuan3D 2.1 | **实测**（V100 32G） |
| **24 GB** | Flux/SDXL 整块；Klein 走 offload | 2mini | 按比例推算 |
| **16 GB** | SDXL/小模型整块；大模型 offload 会很慢 | 2mini 勉强 | 推算 |
| **8 GB** | 只能小模型；大模型 offload 到 CPU 内存 | 跑不了 | 推算 |
| **无 GPU** | 跑不了（diffusers 要 CUDA） | 跑不了 | — |

> 只有 32 GB 那一行是真机实测的，其余是按模型大小和 offload 行为推的。
> 你在自己的卡上跑一次 `tools/bench_direct.py` 就知道了，别信表。

CPU offload 的代价是**有上限**的：它把放不下的子模块放到内存，每步换进换出。
32 GB 模型在 32 GB 卡上，第一步 69 秒、之后 4-8 秒/步；如果卡只有 8 GB，
换入换出的频率会高得多，实际会慢到不可用 —— 那时候要么换小模型，要么换卡。

---

## 复用已有的权重

**先翻本地，别急着下。** ComfyUI 那套权重大多不能直接用（GGUF / 私有 int8 / 3D-Pack 布局），
但有两类例外：

1. **你自己做过 LoRA 微调**：`finetune/xxx-base/` 多半是完整 diffusers 格式。
   作者这台机器上 `D:\AI-Workshop\finetune\flux2klein-base` 就是 32.3 GB 完整模型，一次没重下。
2. **`from_single_file` 支持的 pipeline**：实测 `ZImagePipeline` 和 `FluxPipeline` 有这个方法，
   所以 `z_image_turbo-Q8_0.gguf` 这类 ComfyUI 单文件可以直接喂（配合 `GGUFQuantizationConfig`）。
   `QwenImagePipeline` / `Flux2Pipeline` / `Flux2KleinPipeline` **没有**，只能吃 diffusers 目录。

扫一遍本地有哪些 diffusers 格式模型：

```powershell
Get-ChildItem D:\,C:\Users\$env:USERNAME -Recurse -File -Filter model_index.json -Depth 5 `
  -ErrorAction SilentlyContinue | ForEach-Object {
    $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
    $gb = [math]::Round((Get-ChildItem $_.Directory -Recurse -File -EA SilentlyContinue |
          Measure-Object Length -Sum).Sum/1GB,1)
    "{0,6} GB  {1,-28} {2}" -f $gb, $j._class_name, $_.Directory
  }
```

真没有，再下：

```powershell
python tools/fetch_model.py --repo Tongyi-MAI/Z-Image-Turbo \
    --dest D:/models/z-image-turbo-diffusers --exclude "assets/" --threads 8
```

（自动挑镜像、8 线程分片、断点续传；实测峰值 4.97 MB/s。）

### 作者这台机器上的实测下载速度（2026-11，务必先测你自己的）

| 源 | 单线程 | 8 线程并行 |
|---|---|---|
| huggingface.co | 0.72 MB/s | — |
| hf-mirror.com | 0.40 MB/s | 1.28 MB/s |
| modelscope.cn | 0.78 MB/s | — |

**速度波动非常大**：同一台机器、同一个镜像，下午测 1.6 MB/s，半小时后再测 **4.97 MB/s**（单个 617 MB 文件）。
所以别在第一次测到慢的时候就放弃 —— 换个时段重试。

用自带的下载器（多线程 + 断点续传 + 自动挑镜像）：

```powershell
python tools/fetch_model.py --repo Tongyi-MAI/Z-Image-Turbo \
    --dest D:/models/z-image-turbo-diffusers --exclude "assets/" --threads 8
```

它会先探测 `huggingface.co` 和 `hf-mirror.com` 哪个更快，然后分片下。
断了重跑会从 `.parts/` 续传，不用从头来。

---

## 什么时候该用它

- **要打包成单个可执行文件 / Docker 镜像**，不想再带一个 ComfyUI
- **只做出图**，不需要视频 —— 这是 direct 模式收益最大的场景
- **不想让用户自己装 ComfyUI 和自定义节点**（3D-Pack 的 CUDA 扩展编译是最大的门槛）

不该用它的时候：

- **你要视频** —— 直接放弃这条路
- **你已经在用 ComfyUI 且机器没变** —— 换过去只有成本没有收益
- **你的权重全是 GGUF / 私有量化** —— 重下 20–60 GB，不值

---

## 还没验证的部分（如实写）

- **3D 没跑过真模型**：`hy3dgen` 装好了、API 签名确认了
  （`Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(path, device='cuda', dtype=float16)`），
  但 `tencent/Hunyuan3D-2mini` 的 3 GB 权重没下完。
- **铸想默认的那两个大模型（Qwen-Image-2.1 / Hunyuan3D 2.1）没在直连模式下跑过**：
  它们要 20-30 GB 的 diffusers 格式权重，正在后台拉。出图链路由 tiny-sd 证明成立，
  换上去只剩"加载哪个 repo + 给多少显存"的问题。
- **hy3dgen 2.0.2 是 2.0 的包**：铸想默认的 Hunyuan3D 2.1 需要单独从源码装
  （`Tencent-Hunyuan/Hunyuan3D-2.1`），2.0.2 的 API 大概率兼容但没验。

验证命令（出图已通，3D 等权重到位后同样跑）：

```powershell
# 协议 + 插件（不需要任何权重）
python -m pytest tests/test_direct_backend.py tests/test_plugins.py -q

# 真跑一轮，出耗时和产物路径
python tools/bench_direct.py --model tiny_sd --path D:/models/tiny-sd \
    --pipeline StableDiffusionPipeline --steps 4 --size 256
```
