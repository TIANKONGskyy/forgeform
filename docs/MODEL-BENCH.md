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

### TRELLIS.2 —— 没测

`plugins/mesh/trellis2.yaml` 和 `trellis2_gguf.yaml` 里的 `loader`
（`trellis2.pipelines.Trellis2ImageTo3DPipeline`）**是按包结构猜的，没验证**：

- `trellis2` 包未安装（`ModuleNotFoundError`）
- 权重未下载（两个 repo 各约 8.5 GB）

要验证得：装 `trellis2`（可能要编译 CUDA 扩展）→ 下权重 →
`python tools/bench_mesh.py --models trellis2`。

**在验证之前，把它当成"推荐里有、但没跑过"的选项。**

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
