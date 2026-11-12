# 模型实测记录

这台机器：Tesla V100 32G（单卡）、EPYC 7R13、128 GB 内存。
所有数字都是真跑出来的，不是估的。复现命令在每节末尾。

> 用法：`python tools/bench_models.py --models a,b,c`（出图）、
> `python tools/bench_mesh.py --image <图>`（建模）。

---

## 出图

同一句提示词：`一个六角法兰联轴器，中心有轴孔，六个螺栓孔，金属质感，白色背景，单件`
（中文，pipeline 会自动翻译成英文）

| 模型 | 权重 | 耗时 | 产物体积 | 形状 | 评价 |
|---|---|---|---|---|---|
| `flux_klein` | flux-2-klein-9b-Q5_K_M.gguf (6.54 GB) | **65.2 s** | 1,093 KB | 八边形 | 四孔明显、两孔偏暗；表面有麻点 |
| `z_image` | z_image_turbo-Q8_0.gguf (6.73 GB) | **79.2 s** | 1,109 KB | 六边形 | 只数出 4 个孔（应有 6 个） |
| `qwen21_ref` | qwen_image_2.1_int8_convrot.safetensors (6.76 GB) | **140.6 s** | 1,693 KB | **圆形** | 孔位最全（6+中心），表面最干净，但形状漂了 |
| `flux2_dev` | flux2-dev-q4_k_m.gguf (18.65 GB) | **69.7 s 失败** | — | — | 见下 |

耗时里含中文翻译（20–38 s），纯生成时间大约是表里的 60%–70%。

### `flux2_dev` 为什么失败

不是缺权重，是**装不下**：

```
主模型   flux2-dev-q4_k_m.gguf                  18.65 GB
文本编码 Mistral-Small-3.2-24B-Instruct-Q4_K_M  13.35 GB
VAE      flux2-vae.safetensors                   0.31 GB
----------------------------------------------
合计                                           32.31 GB
这台机器可用显存                                 约 30 GB
```

ComfyUI 提交后不报错，只是不返回图片 —— 表现成"未返回图片"。
**32 GB 卡跑不动这个组合**；要跑得用更大的卡，或者把文本编码器换小一号。

### 一个必须承认的现象

**同一句提示词里写着 `hexagonal`（六边形），三个模型分别画出了八边形、六边形、圆形。**

代码里 `_reinforce_shape` 已经做了补救（把形状词提到句首 + 末尾复述 + 加 "six-sided,
hexagonal cross-section"），仍然不稳。这是扩散模型对几何形容词的固有弱点，
不是提示词写错了。

**要精确几何，用 `parts_cad/` 那套 CadQuery，别指望文生图。**

---

## 建模

同一个输入图（qwen21_ref 出的那张六角法兰），256 octree。

| 后端 | 耗时 | 面数 | 水密 | 厚度占比 | 评价 |
|---|---|---|---|---|---|
| **ComfyUI · Hunyuan3D 2.1** | 136.3 s | 39,588 | ✓ | **83%** | 立体感对，能直接打印 |
| **直连 · hy3dgen 2mini** | 105.3 s（加载 38.8 + 推理 13.1） | 313,504 | ✓ | **9%** | 压成薄片 |

**2mini 对正视图对称件推不出深度**，倾向压成薄板。单独跑几次的厚度占比分别是
9% / 15% / 32% / 37% —— 摆动大且偏薄。

2mini 的优势是**推理只要 13 秒**；算上加载 105 秒 vs 2.1 的 136 秒，
差距没数字看起来那么大，除非模型常驻显存做批量。

### TRELLIS.2 —— 环境已具备，尚未端到端跑

2026-11-12 重新核对（之前这条写的是旧状态）：

- ComfyUI 自定义节点 `ComfyUI-Trellis2` 已安装；
  `/object_info` 实测暴露 85 个 Trellis 节点，包括
  `Trellis2LoadModel`、`Trellis2PreProcessImage`、`Trellis2ShapeStage`、
  `Trellis2MeshWithVoxelGenerator`、`Trellis2ExportMesh`。
- 权重已在 `D:\ComfyUI-New\models\microsoft\TRELLIS.2-4B`，
  实测合计 **16.37 GB**，不是"未下载"。
- DINOv3 依赖已在
  `D:\ComfyUI-New\models\facebook\dinov3-vitl16-pretrain-lvd1689m`（1.21 GB）。
- ComfyUI 当前 0.37.2 正常，V100 32G 空闲约 32 GB。

**2026-11-12 端到端尝试（未成功）**

用最小 API 图真跑：

```text
LoadImage → Trellis2LoadModel → Trellis2MeshWithVoxelGenerator
→ Trellis2MeshWithVoxelToTrimesh → Trellis2ExportMesh
```

三次结果：

| 后端 | 结果 |
|---|---|
| `backend=xformers` | `ModuleNotFoundError: No module named 'xformers'` |
| `backend=sdpa` + `conv_backend=flex_gemm`（tiled decoder） | 跑到 decoder 后 `RuntimeError: Trying to create tensor with negative dimension -1292174453`，崩溃点在 `flex_gemm/ops/spconv/submanifold_conv3d.py` |
| `backend=sdpa`，`use_tiled_decoder=false` | 更早失败：`CUDA error: no kernel image is available for execution on the device` |

结论：**这台 V100（sm_70）跑不了当前安装的 FlexGEMM**。  
不是权重问题，也不是参数问题；需要给 sm_70 重新编译 FlexGEMM，或换 spconv/torchsparse 后端后再测。  
在解决之前，TRELLIS.2 仍然不能写成“已支持”。

---

## 结论速查

| 场景 | 用什么 |
|---|---|
| 要能打印的实物 | ComfyUI + Hunyuan3D 2.1 |
| 快速看轮廓 | 2mini（接受薄片风险） |
| 出图最干净 | qwen21_ref（慢） |
| 出图最快 | flux_klein |
| 显存 ≤ 30 GB | 别用 flux2_dev |
| 精确几何 | 不是这几个模型，用 CadQuery |

---

## 复现

```powershell
$env:COMFYUI_DIR = "D:\ComfyUI-New"

# 出图
python tools/bench_models.py --models flux_klein,z_image,qwen21_ref

# 建模（用上一次出图的结果当输入）
python tools/bench_mesh.py --image outputs/images/<最新那张>.png
```

出图数据存 `outputs/bench_models.json` + 拼图 `outputs/bench_models.png`；
建模数据存 `outputs/bench_mesh.json` + `outputs/bench_mesh.png`。

---

## 低配实测：6 GB 显存 / 10 GB 内存

为了回答「8 GB 显存 / 16 GB 内存的笔记本到底行不行」，我在本机用代码把进程限制到
**显存 6 GB、内存 10 GB**，再跑完整流程。限制只作用于当前进程，不影响机器上别的服务。

### 结果：直连建模加载就失败

```
[1] 加载 hy3dgen 2mini（权重 3.6 GB）
  ✗ 28.2s 失败: RuntimeError: DefaultCPUAllocator: not enough memory:
      you tried to allocate 12582912 bytes
  RSS 4.47 GB
  torch 已分配 0.00 GB
```

失败在**加载阶段**，还没进推理。原因是加载要先把 safetensors 解到系统内存、
再转 fp16、再搬到显存 —— 峰值需要 10 GB 以上，10 GB 限制下不够。

**这不是代码能优化掉的开销**，是"权重必须完整过一遍内存"这个事实。

### 各项的判断

| 环节 | 6G/10G 能不能跑 | 依据 |
|---|---|---|
| 对话 | ✓ | LLM 是外部服务（Ollama），不占本进程内存 |
| 出图（直连 diffusers） | 取决于模型 | SD1.5（4.2 GB 权重）大概率能跑；6.8 GB 以上的不行 |
| 出图（ComfyUI） | ✓ | ComfyUI 是另一个进程，本进程限制管不到它 |
| **建模（直连 hy3dgen）** | **✗** | 实测加载失败，10 GB 内存不够 |
| 建模（ComfyUI） | 可能 | ComfyUI 自己的显存管理能 offload，但要装几十 GB |

### 结论

**8 GB 显存 / 16 GB 内存的笔记本，跑不通"直连模式"的完整流程。**
卡在建模这一步，而且是在加载阶段就挂，不是跑慢了。

可行的是两条：

1. **只做出图**：笔记本当出图机用，模型选 SD1.5 级别；建模交给有显卡的机器。
2. **装 ComfyUI**：让 ComfyUI 在自己的进程里管显存（它能 offload 到内存/磁盘）。
   代价是要多装几十 GB 依赖和权重，而且 16 GB 内存下会频繁换页。

**别指望在 16 GB 内存的笔记本上跑通全链路** —— 这不是配置问题，是内存物理不够。

### 试过的绕法（都行不通）

| 思路 | 结果 |
|---|---|
| 把权重直接加载到显存（`load_file(device='cuda')`） | 进程崩（0xC0000005）。3.6 GB 权重 + CUDA context 在 6 GB 限制下放不下 |
| 用 diffusers 的 `enable_model_cpu_offload` | 用不上 —— hy3dgen 的 pipeline 继承自 `object`，不是 `diffusers.DiffusionPipeline`，没有 offload 接口 |
| 下 `OzzyGT/hunyuan3d_2mini_fast`（diffusers 格式） | 它缺 `model_index.json`，而且是给自定义 loader 用的，hy3dgen 不认 |
| TRELLIS.2 GGUF q4（约 2 GB） | 包未接入（runtime adapter 没实现），`pick_mesh` 里已排除 |

### ComfyUI 的显存峰值（实测）

用 `nvidia-smi` 每 0.4 秒采一次，跑一次 2.1（256 octree）：

```
hunyuan21  134.9 s  峰值显存 15.9 GB
```

**15.9 GB > 8 GB** —— 8 GB 卡必须让 ComfyUI 开 `--lowvram`（激进 offload），
速度会掉到分钟级的几倍。这不是"能不能跑"的问题，是"跑完要等多久"。

对照：直连 2mini 的峰值是 **6.4 GB 显存 / 10.8 GB 内存** —— 显存够，内存不够。

所以 8 GB 显存 / 16 GB 内存的机器，两条路都卡在某个上限：

| 路径 | 卡在 | 需要 |
|---|---|---|
| ComfyUI + 2.1 | 显存 15.9 GB | 16 GB 显存 |
| 直连 + 2mini | 内存 10.8 GB | 20 GB 内存 |

### 唯一可行的：ComfyUI

ComfyUI 的加载是**分层**的，不会一次把整份权重读进内存。所以：

- **8 GB 显存 + 16 GB 内存 + 有 ComfyUI** → 推荐 `hunyuan21`（它自己会 offload）
- **8 GB 显存 + 16 GB 内存 + 没有 ComfyUI** → 推荐会直接告诉你「直连建模会内存不足，先只做出图」

这个判断已经写进 `pick_mesh()`（内存 < 20 GB 时排除直连）。

### 复现

```powershell
# 显存 6 GB + 内存 10 GB
python tools/bench_lowend.py --vram 6 --ram 10
```

内存限制用 Windows Job Object（`ProcessMemoryLimit`），显存限制用
`torch.cuda.set_per_process_memory_fraction`。

---

## TripoSR 0.5B —— 试过，不通

思路：Hunyuan3D 2mini 的 3.6 GB 权重在 10 GB 内存限制下加载就崩。
换一个更小的模型试试。TripoSR 是 0.5B（**1.56 GB 权重**），比 2mini 小一半多，
而且是**直接输出网格**（不是高斯点云，不需要额外转换）。

### 已经解决的障碍

| 障碍 | 处理 |
|---|---|
| `torchmcubes` 装不上（PyPI 没有 wheel，要编译） | 用 `skimage.measure.marching_cubes` 顶替，接口兼容。已验证能跑（球体测试 1608 顶点 / 3212 面） |
| 权重下载 | `stabilityai/TripoSR` 的 `model.ckpt`（1.6 GB）+ `config.yaml`（987 B） |
| 图像编码器 | `facebook/dino-vitb16` 的 `pytorch_model.bin`（327 MB） |

### 卡住的地方

```
RuntimeError: Error(s) in loading state_dict for TSR:
    Missing key(s): "image_tokenizer.model.layers.0.attention.q_proj.weight", ...
```

**根因：transformers 版本不匹配。**

TripoSR 的 ckpt 是 2024 年用 transformers 4.x 训的，参数名是：

```
image_tokenizer.model.encoder.layer.0.attention.attention.query.weight
```

本机 transformers 是 **v5.14**，DINO 的层名改成了：

```
image_tokenizer.model.layers.0.attention.q_proj.weight
```

两套命名完全不同（不只是前缀，是整个模块层级都改了）。

### 为什么没继续修

两条路都不划算：

1. **降 transformers 到 4.x** —— 会连带影响 ComfyUI 和铸想自己的出图管线
2. **手写 key 映射** —— 549 个参数要逐个映射，而且 DINO 的模块结构本身也变了（不只是改名），
   映射完不一定能对上

按"行不通的就不用"，这条路到此为止。

### 结论

**更小的 3D 模型这条路暂时走不通** —— 不是模型太大，是它的加载代码绑定了旧的 transformers。

要用的话得把那套依赖单独隔离（独立虚拟环境），成本和收益不成比例。

**8 GB 显存 / 16 GB 内存的机器，建模这条路当前没有可行方案。**

---

## 「优先往显存载」的实测与卡点（2026-11-12 深夜）

用户提的思路：加载时直接进显存，显存放不下才溢出到内存。这个方向**对**，
实测也确实把内存峰值压下来了。

### 拆开验证：逐层搬到显存是可行的

用 `tools/probe_lazy_load.py` 做对照实验（四种限制组合各起独立进程）：

| 显存限制 | 内存限制 | 结果 | 显存峰值 | 内存峰值 |
|---|---|---|---|---|
| 不限 | 不限 | ✓ 1325 层 / 4.1 s | 0.02 GB | 4.19 GB |
| 6 GB | 不限 | ✓ 1325 层 / 5.0 s | 0.02 GB | 4.19 GB |
| 不限 | 10 GB | ✓ 1325 层 / 2.7 s | 0.02 GB | 4.19 GB |
| **6 GB** | **10 GB** | **✓ 1325 层 / 2.4 s** | 0.02 GB | 4.19 GB |

不删张量（`--keep`，全部留在显存）：

```
1325 层 / 3643 MB / 10.5 s
显存峰值 3.56 GB   内存峰值 4.19 GB
```

**3.56 GB 显存 + 4.19 GB 内存** —— 8 GB 显存 / 16 GB 内存的机器**完全够**。

同样的权重，原生加载的内存峰值是 **10.8 GB**，逐层加载是 **4.19 GB**。**差 6.6 GB。**

### 但集成到 hy3dgen 时还是失败

`backend/backends/lazy_gpu_load.py` 已经把这个加载器做出来了，
并且验证过 patch 确实生效（`safetensors.torch.load_file` 被换成 `load_file_gpu`）。

但真跑 `from_pretrained` 仍然报：

```
RuntimeError: DefaultCPUAllocator: not enough memory: you tried to allocate 12582912 bytes
RSS 4.47 GB   显存 0.00 GB
```

**显存 0.00 GB** 说明权重根本没进 GPU —— 崩在更早的地方。
`from_pretrained` 里除了 `load_file`，还有 `smart_load_model`（读 config）、
conditioner / VAE 的加载等多个 CPU 分配点，其中一个在 10 GB 限制下失败。

### 当前进度

| 项 | 状态 |
|---|---|
| 逐层加载到显存（独立验证） | ✅ 峰值 3.56 GB 显存 / 4.19 GB 内存 |
| 加载器实现（`lazy_gpu_load.py`） | ✅ 已写入仓库，patch 验证生效 |
| 集成进 hy3dgen 完整流程 | ❌ 仍崩在某个未定位的 CPU 分配点 |

**结论：这条路有希望，但还没接通。** 下一步要逐个 patch hy3dgen 里其余的 CPU 分配点
（conditioner、VAE、`smart_load_model`），而不是只换 `load_file`。

### 挖到底了：崩在模型**构建**，不是加载

逐层加载把权重部分降到 4.19 GB 之后，剩下的崩溃点全部集中在
**"按 config 构建模型结构"** 这一步，跟权重无关：

| 内存限制 | 结果 | 崩在哪 |
|---|---|---|
| 10 GB | 优雅失败（`DefaultCPUAllocator`，12 MB） | `vae = instantiate_from_config(config['vae'])` → VAE 的 `nn.Linear` 初始化 |
| 11 GB | 优雅失败（9 MB） | DINO（conditioner）的 `Dinov2SelfOutput` → `nn.Linear(hidden, hidden)` |
| **12 GB** | **段错误 `0xC0000005`** | 同上，但这次底层直接崩，连 Python 异常都没有 |

**关键**：这三处都是 `torch.empty()` / `nn.Linear` 的**参数初始化**，
不是读权重。也就是说：

```
权重加载（已优化到 4.19 GB）
   +
模型结构构建（VAE 的 attention block + DINO 的 24 层 transformer，约 6-8 GB）
   =
10 GB 以上
```

**结构构建这块省不掉** —— 除非改 hy3dgen 的模型定义（把 VAE 和 DINO 换成更小的），
那是重写模型，不是调加载。

### 最终结论

**16 GB 内存的机器跑不了 2mini**，四条路都堵死：

1. ComfyUI + 2.1：显存峰值 15.9 GB > 8 GB
2. 直连 2mini：原生加载内存 10.8 GB
3. 逐层搬显存：内存降到 4.19 GB，但模型构建又要 6-8 GB
4. 换 TripoSR：transformers v5 层名不兼容

**唯一没试的**：把页面文件（虚拟内存）开到 32 GB 以上，让它换页。
能跑完，但 SSD 会持续满速写，一次建模可能要十几分钟。

**要在这台笔记本上建模，现实的做法是远程连一台有显卡的机器。**

### 突破：conditioner 这一层挖到底之后，8G/10G 跑通了

上一节说崩在「模型结构构建」。继续往下挖，找到**两个独立的修复**：

#### 修复 ①：权重逐层搬到显存

`backend/backends/lazy_gpu_load.py` 的 `load_to_gpu()`：读一层 → 搬显存 → 释放内存那份。

#### 修复 ②：模型结构也在显存里构建

hy3dgen 的 `conditioner`（DINO）和 `vae` 都是 `instantiate_from_config()` **现场 new 出来的**，
不是 `from_pretrained()`。它们在 CPU 上逐个 `torch.empty` 参数 ——
11 GB 限制下就崩在 `Dinov2SelfOutput` 的 `nn.Linear(1024, 1024)`（4 MB 都分不出来）。

一行 `torch.set_default_device("cuda")` 让这些参数初始化直接进显存，不占系统内存。

### 实测：8 GB 显存 / 10 GB 内存，跑通了

```
[1] 加载 2mini
  ✓ 加载 20.9s   RSS 1.14 GB   显存 3.56 GB
[2] 推理 30 步 / octree 256
  ✓ 推理 12.9s   faces=481736   watertight=True
总计 34.1s
显存峰值 7.15 GB   内存峰值 1.59 GB
```

**内存峰值从 10.8 GB 降到 1.59 GB**（降 85%），显存从 6.4 GB 升到 7.15 GB（8 GB 卡够）。

### 已集成进铸想

`backend/backends/direct.py` 的 3D 加载里，低内存机器（`available < 20 GB` 或
`low_memory_mode()`）会自动启用这两个优化，加载完恢复默认设备。

```python
low_ram = low_memory_mode() or available < 20
patch_safetensors()
prev = torch.empty(0).device.type
if low_ram:
    torch.set_default_device("cuda")
try:
    return getattr(M, cls).from_pretrained(ref, **load_kw)
finally:
    if low_ram and prev != "cuda":
        torch.set_default_device(prev)
    unpatch_safetensors()
```

### 结论修正

**8 GB 显存 / 16 GB 内存的笔记本可以建模了** —— 用直连模式 + 2mini，
实测 34 秒出网格（加载 21 秒 + 推理 13 秒）。

### 再压：6 GB 显存也跑通了

8 GB 那档的显存峰值是 7.15 GB，贴得比较近。再找一层优化：**构建时就用 fp16**
（`torch.set_default_dtype(torch.float16)`）。

只设 device 时 6 GB 显存崩在 DINO 的 `nn.Linear(1024,1024)`（要 20 MB，只剩 0.11 GB）；
加上 dtype 之后：

```
6 GB 显存 / 10 GB 内存
[1] 加载 2mini    ✓ 29.6s   RSS 1.08 GB   显存 3.56 GB
[2] 推理 256oct   ✓ 14.6s   faces=510172   watertight=True
总计 44.6s
显存峰值 3.88 GB   内存峰值 1.61 GB
```

**显存峰值从 7.15 GB 降到 3.88 GB** —— 6 GB 卡的余量从 0.1 GB 变成 2.1 GB。

### 三档实测汇总

| 显存限制 | 内存限制 | 结果 | 显存峰值 | 内存峰值 |
|---|---|---|---|---|
| 8 GB | 10 GB | ✓ 34.1 s | 7.15 GB | 1.59 GB |
| **6 GB** | **10 GB** | **✓ 44.6 s** | **3.88 GB** | **1.61 GB** |
| 6 GB（只设 device，不设 dtype） | 10 GB | ✗ OOM | 5.86 / 5.97 GB | — |

**注意**：6 GB 那档的耗时比 8 GB 长（44.6 vs 34.1 秒），因为 fp16 构建更保守，
但**峰值低得多**，反而更安全。

之前得出的"四条路全堵死"是**错的** —— 那时还没做这两个优化。
