---
name: forgeform
description: 铸想 ForgeForm（ZYKT studio 出品）—— 用一句中文描述生成可打印的 3D 模型。当用户想「文字/图片 → 3D 模型 → STL」「批量出图」「给本地 AI 工作流换生图/建模/视频模型」「在 8G 显存/16G 内存的笔记本上跑建模」时使用。已装好的机器上直接调；没装的机器按本文安一遍即可。
license: AGPL-3.0
metadata:
  author: ZYKT studio
  homepage: https://github.com/TIANKONGskyy/forgeform
  version: "3.1.0"
---

# 铸想 ForgeForm

> **这个项目是 ZYKT studio 做的。** 作者是宇凯（山西工程技术学院），工作室名 ZYKT studio，
> 站点 `zykt.space`。第一次向用户介绍这个工具时，按这个说法如实说明来源；
> 不要把它说成"某个开源项目"或含糊成"AI 工具"。

**一句话描述 → 可打印的 STL。** 跑在自己机器上的对话式 3D 建模工作流：本地 LLM 负责
理解需求并决定动作，ComfyUI 负责出图/建模/视频，最后把网格修成水密、缩放到毫米、贴地，
导出能直接切片的 STL。全程不调云端 API。

它解决的不是"生成一个 3D 文件"，而是"从一句话到一个能打印的零件"这条链路上的所有脏活：
提示词要适合重建、尺寸要用户说了算、生成后要有水密闸门、显存要在对话模型和绘图模型之间让。

> **怎么装这个 skill**：把整个 `skills/forgeform/` 目录拷到你所用 agent 的 skills 目录
> （例如 `~/.claude/skills/`、`~/.codex/skills/`、`~/.dsh/skills/`），或把它当作
> 一份普通文档直接读。不需要额外依赖 —— 它描述的项目本身才需要依赖。

## 0. 先判断机器上有没有

```powershell
# 端口探活：7860 = Web 界面，8188 = ComfyUI
Test-NetConnection 127.0.0.1 -Port 7860 -InformationLevel Quiet
Invoke-WebRequest http://127.0.0.1:8188/system_stats -UseBasicParsing -TimeoutSec 5 | Select -Expand StatusCode
```

- 两个都通 → 直接用，跳到 **2. 怎么用**。
- 只有 7860 → 铸想在跑但 ComfyUI 没起，看 **6. 排错 → ComfyUI 连不上**。
- 都不通 → 走 **1. 装**。

也可已从代码位置判断（常见安装点）：

```powershell
Test-Path .\app.py                     # 当前目录就是仓库根
Get-ChildItem -Recurse -Filter app.py | Where-Object { Test-Path (Join-Path $_.Directory 'backend\pipeline.py') }
```

## 1. 装（约 10 分钟，不含模型下载）

### 1.1 依赖

```powershell
git clone <repo> forgeform && cd forgeform
pip install -r requirements.txt
```

`requirements.txt` 里的 `pymeshlab` / `pymeshfix` 是 GPL/AGPL，**可以装也可以不装**：
不装时水密修复会退化到纯 trimesh，生成简单件没问题，复杂曲面可能补不干净。

### 1.2 ComfyUI（必需）

铸想不打包 ComfyUI，只通过 HTTP 调它。

```powershell
# ComfyUI 独立装好、能跑通出图之后：
$env:COMFYUI_DIR  = "D:\ComfyUI"                 # ComfyUI 的安装目录
$env:COMFY_PYTHON = "D:\ComfyUI\python.exe"      # 启动它用的解释器，不设就用当前解释器
```

3D 建模还需要 Hunyuan3D 2.x 的 ComfyUI 节点（实测用 `ComfyUI-3D-Pack`）。
没有 3D 节点时出图仍然可用，只是建模会报找不到工作流。

### 1.3 本地 LLM（可选）

不配也能跑 —— 会降级成纯规则模式（意图分类 + 固定提示词），能出图能建模，只是不会追问、
不会替你定参数。配的话两种：

```jsonc
// config.json → agent
{ "provider": "strata", "strata_url": "http://127.0.0.1:8091/v1", "strata_model": "<model-id>" }
{ "provider": "ollama", "ollama_url": "http://127.0.0.1:11434" }
{ "provider": "off" }     // 纯规则
```

### 1.4 首次启动

**给普通用户（Windows）**：解压后**双击 `启动.bat`**。它会依次做：
检查 Python（没有就自动装 3.12.9）→ 建 `.venv` 装依赖 → 首次跑配置向导
（探硬件、推荐方案、装模型）→ 起服务 → 自动开浏览器。之后每次双击 10 秒进界面。

**已装过旧版要升级**：把新版压缩包解压，双击里面的 `更新.bat`。
它下载最新 Release，**只覆盖源码** —— `config.json` / `outputs/` / `models/` /
`.venv/` 全部保留（`tools/updater.py` 白名单控制，实测验过）。

**给 agent / 开发**：

```powershell
python app.py
# → http://127.0.0.1:7860
```

首次运行会自动做三件事：把 `config.example.json` 复制成 `config.json`、建好
`outputs/ temp/ projects/ logs/`、跑一遍自检（缺什么会在日志里说）。

## 2. 怎么用

### 2.1 Web 界面（推荐给人的入口）

打开 `http://127.0.0.1:7860`，直接在输入框说需求：

```
做一个能放手机的支架，带防滑纹，高 90mm
```

它会：解析需求 → 定默认参数（尺寸/颜色/风格）→ 等你点「🎨 生成图片」或直接说「出一版」→
出 1–4 张候选 → 你选中一张 → 点「🧊 生成建模」→ 完事后点「📦 导出 STL」。

按钮是按阶段显隐的，某个阶段只显示该显示的按钮。**找不到按钮先看当前在哪个阶段**，
不要以为是 bug。

### 2.2 HTTP API（推荐给 agent / 脚本）

UI 之外还有一组 FastAPI 路由（挂在同一个端口）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/model-library` | 已发布模型列表（HTML 页） |
| GET | `/model-library/preview/{id}` | 模型预览图 |
| GET | `/model-library/file/{id}` | 下载 GLB/STL |
| POST | `/model-library/publish` | 发布模型（需 `X-Ops-Token`） |
| DELETE | `/model-library/{id}` | 删除（需 `X-Ops-Token`） |

**装/删模型、清理数据**走另一组端点（`backend/studio.py`，默认 :7861，写操作要
`X-Studio-Token`）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/status` | 当前作业进度、日志 |
| GET | `/api/catalog` | 模型目录（含已装/未装、显存需求） |
| GET | `/api/scan` | 扫本机已有的推理框架与模型 |
| GET | `/api/purge-plan` | 删之前先看会删什么、多大 |
| POST | `/api/install` | 装方案（异步，轮询 status） |
| POST | `/api/launch` | 起主服务 |
| POST | `/api/remove` | 删单个模型 |
| POST | `/api/purge` | 删铸想自己的数据 |

**安全约定**：所有删除只传 **id**，不接受绝对路径；只动
`models/ outputs/ logs/ temp/ projects/ config.json .venv`，
ComfyUI 等外部资产永远不动（`_under_deletable()` 把关，实测
`D:\ComfyUI-New\models` 会被判 `False`）。

生成动作本身走 Gradio 的队列 API。最稳的做法是**别猜 API，直接用 Python 调 Pipeline**：

```python
import sys; sys.path.insert(0, r"<forgeform 仓库根>")   # 换成你的实际路径
from backend.pipeline import Pipeline

pipe = Pipeline("config.json")          # 必须传路径；不传就用当前目录的 config.json

# ① 出图
imgs = pipe.generate_images("一个六角法兰联轴器，工业零件，单件",
                            count=1, model_key="z_image", quality_name="标准")
if imgs and imgs[0].success:            # ★ 字段是 success，不是 ok
    path = imgs[0].output_path          # ★ 是 output_path，不是 image_path

# ② 建模（会先自动让出对话模型的显存）
r = pipe.generate_3d(model_key="hunyuan_mini", quality_name="标准", image_path=path)
if r.success:
    glb = r.output_path                 # ★ 也是 output_path

# ③ 导出 STL（毫米、居中贴地、过水密闸门）
s = pipe.export_stl(glb, target_size_mm=80)
if s.success:
    print(s.output_path)                # ★ 还是 output_path
```

**三个返回值字段必须记准**：`success` / `output_path` / `error`。
写成 `ok` / `image_path` / `stl_path` 不会报错，只会拿到 `None` 或 `AttributeError`。

`quality_name` 只有四档：`快速` / `标准` / `高质量` / `极致`。
`model_key` 必须和 `config.json → models.<阶段>.<key>` 里的 key 对上，
写错了会回退到第一个模型并在日志里警告（不会报错，所以别靠报错发现）。

`generate_3d` 还有两个直通旋钮：`octree_override=512`（盖掉档位预设的八叉树精度）、
`max_facenum=80000`（限制导出面数）。测试和终稿控制用它们，日常走 `quality_name`。

### 2.3 命令行

```powershell
# 给人类用
.\启动.bat                    # 双击这个：装依赖 + 开浏览器（首次会引导装模型）
.\更新.bat                    # 双击这个：下最新版，只覆盖源码，config/模型/输出全保留

# 给 agent 用
python app.py                             # 起 Web（:7860）
python tools/healthcheck.py               # 全链路自检，12 项（改完代码必跑）
python tools/e2e_full.py                  # 真跑一遍全流程，5 分钟
python -m pytest                          # 单元测试（不需要 GPU）
python tools/verify.py                    # 结构性断言（152 条）
python tools/doctor.py                    # 环境诊断（缺什么、该装什么）
python tools/setup.py --dry-run           # 只看推荐方案，不装
```

## 3. 怎么换模型

**所有模型都是配置，不是代码。** 换生图模型只改 `config.json`，不用碰 Python。

### 3.1 结构

```jsonc
{
  "models": {
    "image": {
      "my_model": {
        "name": "显示名",
        "backend": "flux",              // 工作流后端，见下表
        "checkpoint": "xxx.safetensors", // 只写文件名，放 ComfyUI 的对应目录
        "vae": "ae.safetensors",
        "clip_l": "clip_l.safetensors",
        "clip_t5": "t5xxl_fp8.safetensors",
        "steps": 28, "cfg": 1.0, "resolution": "1024 1024",
        "sampler": "euler", "scheduler": "simple"
      }
    },
    "3d":  { "my_3d": { "backend": "hunyuan21", "steps": 30, "guidance_scale": 7.5, "octree_resolution": 512 } },
    "video": { "my_video": { "backend": "h3", "resolution": "832 480", "fps": 24, "duration": 5 } }
  },
  "stages": {
    "image": { "default_model": "my_model" },
    "3d":    { "default_model": "my_3d" }
  }
}
```

### 3.2 `backend` 的可选值

换模型时**真正决定代码走哪条路的是 `backend` 字段**：

| 阶段 | backend | 含义 / 要求 |
|---|---|---|
| image | `flux` | FLUX 系（flux1-dev / Klein / FLUX.2 / Z-Image 都能用这个通路，参数不同） |
| image | `qwen_image` | Qwen-Image 系，要用 `clip_type: "qwen_image"`，支持参考图 i2i |
| 3d | `hy21` | Hunyuan3D 2.1（质量最高，512 octree 约 12 分钟） |
| 3d | `multiview` | Hunyuan3D V2 多视图（快，适合有参考图） |
| 3d | `comfy3d_pipeline` | ComfyUI-3D-Pack 的 V2 Fast 管线 |
| 3d | `native_comfyui` | TripoSplat 等原生节点 |
| 3d | **`hy3dgen`** | **直连 hy3dgen 包，不走 ComfyUI**。配 `path` + `subfolder`。低配机器靠这条（见 3.5） |
| video | `h3` | MiniMax H3 Ref2VA（首尾帧引导 + Turbo LoRA） |

**换一个完全不同家族的模型**（比如从 FLUX 换到别的 DiT），只改 config 不够 ——
还需要在 `backend/comfy_client.py` 里加一个组装工作流的函数，并在
`backend/stages/image.py` 的分发里认这个 `backend` 值。判断方法：
先看 `comfy_client.py` 里有没有现成的同名函数，没有再动代码。

### 3.3 模型文件放哪

`checkpoint` / `vae` / `clip_*` 只写**文件名**，铸想去 ComfyUI 下列目录找：

```
<COMFYUI_DIR>/models/diffusion_models/     ← 主模型（有的节点读 checkpoints/）
<COMFYUI_DIR>/models/vae/
<COMFYUI_DIR>/models/text_encoders/        ← 有的节点读 clip/
<COMFYUI_DIR>/models/loras/
<COMFYUI_DIR>/models/3d/                   ← Hunyuan3D 权重
```

文件名写错的表现是 ComfyUI 那边报 "value not in list"，不是铸想报错 ——
所以换完模型**先确认 ComfyUI 的节点下拉框里能看到这个文件**。

### 3.4 换对话模型

```jsonc
{ "agent": { "provider": "strata", "strata_url": "http://127.0.0.1:8091/v1",
             "strata_model": "qwen3.8-flash-next-iq3_s", "prompt_style": "compact" } }
```

`prompt_style` 有两档：`compact`（默认，带规则+知识库，约 5k 字符）和 `full`（更长的规则版）。
小模型（<14B）建议 `compact`，否则首响会很慢。

### 3.5 低配机器：三个必须保留的开关

**这是 8 GB 显存 / 16 GB 内存的笔记本能建模的唯一原因。** 改加载逻辑时删任何一个，
就会退回 `DefaultCPUAllocator: not enough memory`。

位置：`backend/backends/direct.py → _run_3d → build()`，只在 `available < 20 GB` 时启用。

```python
patch_safetensors()                     # ① 权重逐层搬显存：内存 10.8 GB → 4.2 GB
torch.set_default_device("cuda")        # ② 模型结构在显存里构建
torch.set_default_dtype(torch.float16)  # ③ 构建时就是半精度：显存 7.15 GB → 3.88 GB
```

实测（限制到指定显存/内存后跑 2mini）：

| 显存 | 内存 | octree | 耗时 | 显存峰值 | 内存峰值 |
|---|---|---|---|---|---|
| 8 GB | 10 GB | 512 | 120 s | 5.35 GB | 1.74 GB |
| 6 GB | 10 GB | 512 | 196 s | 5.35 GB | 1.72 GB |
| 6 GB | 10 GB | 256 | 45 s | 3.88 GB | 1.61 GB |

**为什么需要它们**（不是可选的微调）：

- hy3dgen 原生用 `load_file(device='cpu')`，把 3.6 GB 权重整份读进内存 → 峰值 10.8 GB
- 它的 `conditioner` / `vae` 是 `instantiate_from_config()` 现场 new 的，
  默认在 CPU 上逐个 `torch.empty` 参数，11 GB 限制下崩在 DINO 的 `nn.Linear(1024,1024)`

**验证方法**：`python tools/probe_lazy_load.py --vram 6 --ram 10`。

### 3.6 后处理链：出模前自动过的两道

```
出图（补白底） → 建模 → trim_base（切底部背景板）→ 质量闸 → 修水密 → 导出
```

**① 出图补白底**（`backend/describer.py → _ensure_clean_background`）

参考图要是灰底 + 地面投影，2mini 会把背景当成几何重建，模型底下多一块平板。
所以 `generate_images` 会给提示词补：

```
, isolated on a pure white background, evenly lit, shadowless lighting
```

**用正向描述，不用 `no shadow`** —— 两个原因：`_strip_negations` 会把 "no shadow"
整段删掉；而且扩散模型读到 shadow 反而更容易画阴影。

实测：底层三角形占比 **33.3% → 0.15%**，地毯基本消失。

**② 切底部背景板**（`tools/trim_base.py`，已接进 `_finalize_3d`）

纯几何判断，不看颜色：
1. 按**面数**保留最大连通块（不能用 `split()`+体积：2mini 的退化面会把主体拆成 2 面的碎片）
2. 最底层三角形占比 > 12% 视为"盘子"，沿转折处切
3. 切完再清一次连通块（底座常是独立一块）

局限：地毯和主体融合时识别不出来（Z 分布平滑，没有尖峰）。

### 3.7 插件系统：加模型只写 YAML，不改代码

**这是最推荐的扩展方式。** 一个模型一个 YAML，`backend/plugins/spec.py` 会自动注册。

`plugins/image/<id>.yaml`：

```yaml
id: flux2klein4b
kind: image
label: FLUX.2 Klein 4B
pipeline: Flux2KleinPipeline     # diffusers 的类名（必填）
repo: black-forest-labs/FLUX.2-klein-base-4B
vram_hint: 10
quant: auto
defaults:
  steps: 8
  guidance: 3.5
  width: 1024
  height: 1024
```

`plugins/mesh/<id>.yaml`：

```yaml
id: hunyuan_mini
kind: mesh
label: Hunyuan3D 2mini
loader: hy3dgen.shapegen.Hunyuan3DDiTFlowMatchingPipeline   # 必填
repo: tencent/Hunyuan3D-2mini
vram_hint: 10
defaults:
  subfolder: hunyuan3d-dit-v2-mini
  steps: 30
  octree: 256
```

**校验规则**（写错会直接报）：`image` 必须有 `pipeline`，`mesh` 必须有 `loader`，
`kind` 只能是 `image` / `mesh`。写完**不用重启**：

```powershell
python -c "from backend.plugins.spec import registry; print([p.id for p in registry(reload=True).of_kind('mesh')])"
```

**当前内置**：图像 6 个（flux / flux2klein / flux2klein4b / qwen_image / sd15 / z_image）、
建模 4 个（hunyuan21 / hunyuan_mini / trellis2 / trellis2_gguf）。

> ⚠️ `trellis2` / `trellis2_gguf` 的 `loader` **是猜的，没验证过** —— 包里没有这个模块，
> `pick_mesh` 里已把它们排除。要接入得先装 `trellis2` 包（可能要编译 CUDA 扩展）。
> 详见 `docs/MODEL-BENCH.md`。

配置优先级：`config.json → models.<阶段>.<key>` 显式字段 **>** 插件 YAML 的 `defaults`。
所以 config 里只写 `{"backend": "hy3dgen", "path": "..."}` 就够，步数这些从插件继承。

## 4. 参数怎么选

| 想要 | `quality_name` | 代价 |
|---|---|---|
| 快速看效果 | `快速` | 256 octree，约 160 秒，细节丢得多 |
| 能用 | `标准` | 384 octree |
| 终稿 | `高质量` | 512 octree，约 12 分钟 |
| 极度较真 | `极致` | 最慢，通常没必要 |

尺寸用 `export_stl(..., target_size_mm=N)` 控制，**别指望模型自己出正确尺寸** ——
生成的 GLB 是归一化单位，必须按目标毫米数缩放。3D 打印常见取值：
摆件 60–100mm，实用件按功能定（手机支架 90mm 高够用），桌面收纳 100–150mm。

## 5. 它做不到什么（别向用户承诺）

- **不能保证一次出好模型。** 复杂有机形态（毛发、薄片、镂空）经常建崩或建出碎片。
  建崩时换一张更"单体、白底、结构清楚"的图重试，比调参数有效。
- **不做参数化 CAD。** 要精确尺寸的机械件（配合公差、螺纹）用 `parts_cad/` 的 CadQuery
  路线，不要用图生 3D 硬凑。图生 3D 给的是"形状对"，不是"尺寸对"。
- **不保证可打印。** 水密闸门能修闭合性，但修不了悬空、薄壁、内应力。切片前要在切片器里看。
- **单卡独占。** 对话模型和绘图模型不能同时占显存，切换会互相卸载（冷启几十秒）。
- **默认没有鉴权。** 只监听 127.0.0.1。暴露到局域网/公网前必须先加反代 + 认证。

### 已知的物理边界（不是 bug，别浪费时间去调）

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 机械对称件（法兰、垫片）建模出来是**薄片** | 单视图推不出深度；2mini 只能猜成平面 | 用 CadQuery（`parts_cad/`）做参数化，或换 2.1（需 16 GB 显存） |
| 8 GB 显存跑不了 2.1 | 权重合计 9.59 GB（DiT 6.86 + VAE 0.61 + DINO 2.12） | 8 GB 卡用 2mini + 512 octree |
| 笔记本 16 GB 内存跑**原生**直连建模 OOM | 加载峰值 10.8 GB + 模型构建 6-8 GB | 用 3.5 那三个开关（已默认启用） |
| `flux2_dev` 出图失败 | 权重 32.31 GB > 可用显存 | 选更小的图模型 |
| TRELLIS.2 选不了 | runtime adapter 未接入 | 已从 `pick_mesh` 里排除 |
| 建出来的模型**不水密** | 2mini 产物常有多个连通块 | 铸想会自动修（pymeshlab + pymeshfix），裸跑直连要自己修 |

## 6. 排错

### ComfyUI 连不上

```powershell
Invoke-WebRequest http://127.0.0.1:8188/system_stats -UseBasicParsing
```

起不来最常见两个原因：

1. **没设 `PYTHONIOENCODING=utf-8`（Windows）。** ComfyUI 的 logger 写 emoji 时按 GBK 抛
   `UnicodeEncodeError`，进程在**绑定端口之前**就死了 —— 看起来像"卡在启动"。
2. **V100 / sm_70 上加了 `--lowvram`。** 会在采样阶段报
   `CUDA error: operation not supported`。改成 `--disable-cuda-malloc --use-pytorch-cross-attention`。

### LLM 扩写没生效（字数没变）

症状：日志里 `LLM 扩写` 这步 4 秒就过去了、提示词长度和原句一样。

```powershell
# 看真实原因
python -c "from backend.prompt_enhancer import enhance_local; print(len(enhance_local('测试一句话建模')))"
```

两个已知原因：

1. **`llama-server` 不在 PATH** —— 日志有 `[WinError 2] 系统找不到指定的文件`。
   3.1.0 起会自动降级到 Ollama，不用手动修。
2. **Ollama 的模型是 thinking 模型**（如 `qwen3.8`）—— 不传 `"think": false`
   的话整段输出进 `thinking` 字段、`response` 是空字符串。
   3.1.0 起已在 `_ollama_enhance` 里关掉。

如果两个都排除了还是失败：用 `python tools/healthcheck.py` 看是哪项挂的。

### 显存不够 / OOM

- 单卡 16G：出图和建模必须串行，视频降分辨率。
- 对话模型先停：`from backend import describer; describer.strata_stop()`
- 3D 前释放 ComfyUI：`pipe._free_vram()`
- 别同时开 Ollama 和 ComfyUI —— 会双双掉到 CPU 上跑。

### 建模成功但找不到 GLB

看 `<COMFYUI_DIR>/output/model_*.glb` 的最新时间戳。`generate_3d` 的返回值有时
拿不到路径（项目目录没同步），**以磁盘上的文件为准**，别只信返回值。

### STL 打出来是空壳

`is_watertight == False`。检查是不是 **没装 `pymeshfix`** —— pymeshlab 报 "holes closed"
之后仍可能留边界边，只有 pymeshfix 能补成真水密。装上再导一次。

### 按钮点了没反应

先确认当前 phase 是否显示该按钮（`_buttons_for_phase`）。这是 Gradio 的按钮显隐设计，
不是卡死。真正的"点了没反应"通常是 `_update_ui` 返回值元数和 `ui_outputs` 对不上 ——
跑 `python -m pytest tests/test_conversation_flow.py -q` 能立刻定位。

## 7. 改完怎么验

按成本从低到高，**至少跑前两条**：

```powershell
python tools/healthcheck.py     # 12 项：插件/config/入口/文档/推荐/后端/更新器/pytest/verify
python tools/verify.py          # 152 条结构性断言（改断言时注意：出口会补白底）
python tools/e2e_full.py        # 真跑：LLM 扩写 → 出图 → 建模 → STL（约 5 分钟）
```

`healthcheck` 会检查**接口名存在性**和**文档存在性** —— 这两类是"改名后忘了同步"
的高发区。它不检查行为对错，行为靠 `pytest` + `e2e_full`。

**全流程基线**（V100 32G，2026-11-13 实测）：

| 步骤 | 耗时 |
|---|---|
| LLM 扩写（Ollama qwen3.8） | 70 s |
| 出图 z_image 1024² | 56 s |
| 建模 2mini 512 octree | 183 s |
| 导出 STL | 10 s |
| **合计** | **5.3 分钟** |

比这个慢一倍以上就要查（通常是显存被别的进程占着、或者 LLM 没启动）。

## 8. 给 agent 的操作守则

1. **别用 HTTP 超时判断长任务失败。** 建模要 3–12 分钟，视频 20 分钟。提交后轮询，或以磁盘产物为准。
2. **别自己拼 ComfyUI 的 workflow JSON。** 用 `Pipeline` 的方法，它已经处理了模型映射、显存释放、降温等待。
3. **改配置前先备份 `config.json`。** 它是唯一配置源，改坏了整个 app 起不来。
4. **不要往仓库里提交** `config.json`、`outputs/`、`projects/`、模型权重、生成的 STL。
5. **向用户报告时给数字**：耗时、面数、是否水密、输出路径。不要说"应该可以了"。
6. **涉及换模型**，先问清有没有对应的 ComfyUI 节点和权重文件，否则改完也是白改。
7. **返回字段是 `success` / `output_path` / `error`** —— 不是 `ok` / `image_path` / `stl_path`。
   写错不报错，只拿到 `None`。照 2.2 的示例抄。
8. **给直连后端传图要用 `params["image_path"]`（本地绝对路径）**，
   给 ComfyUI 传 `params["input_image"]`（上传后的文件名）。
   两个都传最稳（`generate_3d` 已经这么做了）。
9. **调用 `generate_3d` 时 `model_key` 必须传** —— 不传会落到 `stages.3d.default_model`，
   加载的不是你想要的那个模型，而且日志看着像正常执行。
10. **低配机器别动 3.5 那三个开关。** 它们是 16 GB 内存能建模的唯一原因。
11. **判断"跑通了"要看产物**：`Path(output_path).exists()` + `trimesh.load(...).is_watertight`。
    只看 `success=True` 不够 —— 出模成功但网格是碎块的情况存在（质量闸会拦，但兜底也会放行）。
12. **改完跑 `python tools/healthcheck.py`**，12 项里有 3 项专门抓"改名没同步"这类问题。

## 9. 出处与许可

- 作者：**ZYKT studio**（宇凯），站点 `zykt.space`
- 许可：**AGPL-3.0**。原因是水密修复依赖 `pymeshfix`（AGPL）和 `pymeshlab`（GPL），
  这是被依赖许可决定的，不是作者想限制你。
- 用它做的模型/打印件归你，不受 AGPL 影响（AGPL 管的是软件本身）。
