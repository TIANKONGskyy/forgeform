# 模型插件

铸想的定位是"一句话建模"。模型会不断换代，所以**模型不能写死在代码里**。

这里的做法：一个模型 = 一个 YAML 文件。加文件即接入，改 `config.json` 即切换。不改 Python。

---

## 一个插件长什么样

`plugins/image/z_image.yaml`：

```yaml
id: z_image
kind: image
label: Z-Image Turbo 6B
pipeline: ZImagePipeline          # diffusers 里的类名
repo: Tongyi-MAI/Z-Image-Turbo    # 从哪下（也可以用 path 指本地目录）
vram_hint: 20                     # 显存提示，只用于展示
notes: 8 步出图；turbo 系 guidance 固定 1.0
defaults:
  steps: 8
  guidance: 1.0
  width: 1024
  height: 1024
  dtype: bfloat16
```

`plugins/mesh/hunyuan21.yaml`：

```yaml
id: hunyuan21
kind: mesh
label: Hunyuan3D 2.1
loader: hy3dgen.shapegen.Hunyuan3DDiTFlowMatchingPipeline
repo: tencent/Hunyuan3D-2.1
vram_hint: 16
defaults:
  steps: 30
  octree: 512
  guidance: 6.0
  dtype: float16
```

---

## 接一个新模型：三步

假设明天出了 `SuperImage-3`，diffusers 加了 `SuperImage3Pipeline`。

**① 丢一个 YAML**

`plugins/image/superimage3.yaml`：
```yaml
id: superimage3
kind: image
label: SuperImage 3
pipeline: SuperImage3Pipeline
repo: someone/superimage-3
defaults:
  steps: 12
  guidance: 2.5
  width: 1024
  height: 1024
```

**② 把权重拉下来**

```powershell
python tools/fetch_model.py --repo someone/superimage-3 --dest D:/models/superimage-3
```

**③ 在 config 里指过去**

```jsonc
{
  "backend": { "mode": "direct" },
  "models": { "image": { "superimage3": { "path": "D:/models/superimage-3" } } },
  "stages": { "image": { "default_model": "superimage3" } }
}
```

完事。**没改一行 Python，没跑一次测试之外的构建。**

---

## 优先级：config 覆盖插件

同一份配置，两个来源：

| 来源 | 作用 |
|---|---|
| 插件 YAML 的 `defaults` | 这个模型的"出厂参数"（步数、guidance、分辨率、dtype） |
| `config.json → models.<stage>.<key>` | 你这台机器上的覆盖值（本地路径、显存不够时降步数…） |

合并规则：**config 里有值就用 config 的，没写就继承插件的**。所以换模型时不用把参数抄一遍。

`config.json` 里最重要的是 `path`（本地权重的实际位置），其余可以留空。

---

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一名，`config.json` 里用它引用 |
| `kind` | ✅ | `image` / `mesh`（`video` 预留，目前没有直连实现） |
| `label` | | 界面上显示的名字 |
| `pipeline` | image 必填 | diffusers 的 pipeline 类名，如 `FluxPipeline` |
| `loader` | mesh 必填 | 完整 import 路径，如 `hy3dgen.shapegen.Hunyuan3DDiTFlowMatchingPipeline` |
| `repo` | 二选一 | HF repo id，`fetch_model.py` 用它下载 |
| `path` | 二选一 | 本地目录；写了就优先用它，忽略 `repo` |
| `vram_hint` | | 显存提示（GB），只用于展示 |
| `notes` | | 给人看的备注 |
| `quant` | | `auto`（默认）/ `none` / `bnb8` / `bnb4` / `gguf`。`auto` 按显存自动选 |
| `offload` | | `model` / `sequential`。留空=自动，显式写了就听你的 |
| `defaults` | | 该模型的出厂参数，见下 |
| `adapter` | | 可选，同目录的 `.py`，用于参数不兼容时的转接 |

### 量化与显存：`quant` + `offload`

同一个插件，在 8G 卡和 32G 卡上要能跑，靠的就是这两个字段。

`quant: auto`（默认）的决策顺序（`backend/backends/quant.py`）：

```
量出「模型要多少」和「这张卡还剩多少」
  → 能整块放下   → quant=none（原精度，最快）
  → 放不下       → quant=none + offload=model（按子模块换入换出，慢但结果对）
```

**故意不自动选 bnb**：实测 bitsandbytes 8bit 量化 `Flux2Transformer2DModel` 之后，
出图是纯噪声 —— 不报错、速度还更快（89s vs 194s），特别坑。
想试可以显式写 `quant: bnb8`，但 `DirectBackend` 会用拉普拉斯方差验图，
一旦发现是噪声就直接报错，不会静默存一张垃圾。

显存实测（V100 32G，FLUX.2 Klein 33 GB，512²，8 步）：

| 模式 | 耗时 | 首步 | 稳定性 |
|---|---|---|---|
| `offload=model` + bf16 | **194 s** | 69 s | ✅ 正确 |
| `quant=bnb8` | 89 s | — | ❌ 出噪声（被检测拦下） |
| `quant=bnb8`（只量化 transformer） | 64 s | — | ❌ 仍是噪声 |

### `defaults` 常用键

| key | 阶段 | 含义 |
|---|---|---|
| `steps` | image / mesh | 推理步数 |
| `guidance` | image / mesh | CFG / guidance scale |
| `width` / `height` | image | 输出分辨率 |
| `octree` | mesh | Hunyuan3D 的 octree_resolution |
| `dtype` | 两者 | `bfloat16` / `float16` |

---

## 覆盖内置插件

内置插件在 `plugins/`（跟着仓库走）。想改它们，**不要直接改仓库文件**，放到用户目录：

```
~/.forgeform/plugins/            # 或者用 $env:FORGEFORM_PLUGINS 指定
  image/
    z_image.yaml                 # 同 id 会覆盖内置的
```

加载顺序：仓库 `plugins/` → 用户目录。同 id 后者赢。

这样 `git pull` 不会冲掉你的改动。

---

## 内置清单

| kind | id | 模型 | 说明 |
|---|---|---|---|
| image | `qwen_image` | Qwen-Image-2.1 | 中文提示词最准，铸想默认 |
| image | `z_image` | Z-Image Turbo 6B | 8 步，最省显存 |
| image | `flux` | FLUX.1-dev | 通用性最好 |
| image | `flux2klein` | FLUX.2 Klein 9B | 8 步蒸馏 |
| mesh | `hunyuan21` | Hunyuan3D 2.1 | 质量天花板，512 octree |
| mesh | `hunyuan_mini` | Hunyuan3D 2mini | 3 GB，先跑通再换 2.1 |

---

## 参数不兼容怎么办：`adapter`

绝大多数 diffusers pipeline 都吃 `prompt / num_inference_steps / width / height / guidance_scale`，
所以 `pipeline: XxxPipeline` 就够了。

真碰到不兼容的（比如要额外的控制图、要自己调度器、要特殊的分辨率约束），
在 YAML 同目录放一个 `adapter.py`：

```yaml
id: weird_model
kind: image
adapter: adapter.py
```

`adapter.py` 里提供两个函数，`DirectBackend` 会优先用它们：

```python
def load(ref, dtype):
    from diffusers import SomePipeline
    return SomePipeline.from_pretrained(ref, torch_dtype=dtype)

def generate(pipe, opts: dict, prompt: str):
    """opts 是合并后的参数；返回 PIL.Image"""
    return pipe(prompt=prompt, **_map_my_params(opts)).images[0]
```

> 适配器目前是**约定**（`load` / `generate` 两个函数），`DirectBackend` 还没接。
> 接的时候是 `direct.py` 里 `_run_image` 加一个分支，不影响已有插件。

---

## 和 ComfyUI 模式的关系

两套后端并存：

```jsonc
"backend": { "mode": "comfyui" }   // 默认，走 ComfyUI（能用它的自定义节点、GGUF、视频）
"backend": { "mode": "direct" }    // 直连 diffusers / hy3dgen（不依赖 ComfyUI，但只支持 image/mesh）
```

**插件只作用于 direct 模式。** ComfyUI 模式用的是 `comfy_client.py` 里那套工作流 JSON，
和这里的 YAML 无关 —— 因为 ComfyUI 的模型选择是"工作流里连哪个节点"，不是"加载哪个 pipeline"。

将来要做"插件也能生成 ComfyUI 工作流"是可行的（YAML 里加一段 `comfy_nodes`），但那是下一步。

---

## 为什么插件用 YAML 而不是 Python 类

- **加模型不该需要跑测试**。YAML 是数据，出错只影响那一个模型，不会让整个包 import 失败。
- **用户可以只改一个文件**。Python 插件类要求用户理解继承、注册、import 顺序。
- **能 diff**。YAML 的改动在 PR 里一眼看得出「步数从 30 改成 24」。
- **能远程分发**。以后做"插件市场"，一个 URL 下个 YAML 就能用，不用装 Python 包。
