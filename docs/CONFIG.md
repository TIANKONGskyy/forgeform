# 配置与选型

所有可调的东西都在 `config.json`（首次运行从 `config.example.json` 复制）。这份文档解释每个
字段干什么、怎么按你的卡选模型、以及换模型到底要改哪几行。

---

## 1. 按显存选配置

先用这条命令看你的卡：

```powershell
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

### 32 GB（实测：Tesla V100 32G）

全档位可用。这是作者的机器，下面的数字都在这个配置上测出来的。

```jsonc
{
  "stages": {
    "image": { "default_model": "qwen21_ref" },
    "3d":    { "default_model": "hunyuan21" },
    "video": { "default_model": "h3" }
  },
  "models": { "video": { "h3": { "resolution": "1344 768" } } },
  "comfyui_idle": { "free_after_minutes": 10 }
}
```

- 出图 1024²：约 56 s
- 建模 512 octree（`高质量`）：约 12 min
- 视频 1344×768：能跑，但 96 帧要 20 分钟以上

### 24 GB（RTX 4090 / 3090）

可以全档位，但**必须串行**，并且关掉视频的高分辨率档。

```jsonc
{
  "models": { "video": { "h3": { "resolution": "832 480", "duration": 5 } } },
  "comfyui_idle": { "free_after_minutes": 5 }
}
```

### 16 GB（4080 / 4070 Ti / 3080）

能跑，但要降低期望：

- 出图：`qwen21_ref` 可能刚好卡边，用 `flux` 系更稳
- 建模：只用 `快速` / `标准`，`高质量` 大概率 OOM
- 视频：不要开

```jsonc
{
  "stages": {
    "image": { "default_model": "flux" },
    "3d":    { "default_model": "hunyuan21" }
  },
  "generation": { "default_batch_size": 1, "max_batch_size": 1 },
  "comfyui_idle": { "free_after_minutes": 3 }
}
```

### 8 GB 及以下

只跑出图（`flux` 或 `z_image`），3D 会 OOM。把 `agent.enabled` 也关掉，
省下几 GB 给绘图模型：

```jsonc
{ "agent": { "enabled": false } }
```

### 没有 GPU

界面起得来、能翻历史、能下载已有模型，**所有生成动作不可用**。这不是降级模式，
是"能看不能用"。

---

## 2. 显存是怎么分配的

这是最容易翻车的地方。同一张卡上要轮流住四个模型，而它们互斥：

| 占用者 | 典型显存 | 什么时候在 |
|---|---|---|
| 对话模型（Strata / Ollama） | 20–30 GB | 你说话的时候 |
| 出图模型 | 18–20 GB | 点生成图片之后 |
| 建模模型 | 14–16 GB | 点生成建模之后 |
| 视频模型 | 20 GB+ | 点生成视频之后 |

代码里的处理（`backend/gpu_state.py` + `pipeline._release_chat_gpu()`）：

1. 生成前先看对话在不在忙：忙就等最多 30 秒，还忙就**拒绝这次生成**（不打断回答也不硬抢）
2. 不忙就把对话模型卸载（`describer.strata_stop()`）
3. 生成完，下一次对话时再冷启对话模型（约 70 秒）

**所以生成之后的第一句话会慢一拍，这是设计，不是卡了。**

`comfyui_idle.free_after_minutes` 控制闲置多久把 ComfyUI 的显存还回去。卡越小，这个值应该越小。

---

## 3. 字段说明

### `app`

```jsonc
"app": { "name": "铸想 ForgeForm", "host": "127.0.0.1", "port": 7860, "share": false }
```

- `host`：默认只监听本机。改成 `0.0.0.0` 前先读 [SECURITY.md](../SECURITY.md) —— 这应用**没有鉴权**。
- `share`：Gradio 的公网分享。保持 `false`，它会绕过你所有的网络隔离。

### `paths`

```jsonc
"paths": {
  "comfyui":   "${COMFYUI_DIR}",
  "models":    "${COMFYUI_DIR}/models",
  "output":    "./outputs",
  "temp":      "./temp",
  "meshy_api": "./meshy_api"
}
```

`${VAR}` 按环境变量展开，相对路径按**仓库根**解析（不是当前工作目录）。
所以你可以随便 `cd` 到哪，路径都不会跑偏。

### `agent`

```jsonc
"agent": {
  "enabled": true,
  "provider": "strata",                      // strata | ollama | off
  "strata_url": "http://127.0.0.1:8091/v1",  // OpenAI 兼容端点
  "strata_model": "qwen3.8-flash-next-iq3_s",
  "prompt_style": "compact",                 // compact | full
  "parallel": 2
}
```

- `provider: "off"` → 纯规则模式（意图分类 + 固定模板）。能用，但不会追问、不会替你定参数。
- `prompt_style: "compact"` 是默认 —— 大约 5k 字符，带规则和站点知识库。
  `full` 版规则更长，**只有大模型才值得用**，小模型会被长 prompt 拖慢首响。

### `models`

每个阶段下是一张"模型表"，key 随便起名，但 `backend` 字段决定代码走哪条路：

```jsonc
"models": {
  "image": {
    "qwen21_ref": {
      "name": "Qwen-Image-2.1 参考图",
      "backend": "qwen_image",        // ← 决定用哪个工作流组装函数
      "checkpoint": "qwen_image_xxx.safetensors",
      "clip_l": "qwen3vl_8b_xxx.gguf",
      "vae": "qwen_image_vae.safetensors",
      "clip_type": "qwen_image",
      "steps": 20, "cfg": 4.0,
      "sampler": "euler", "scheduler": "simple",
      "resolution": "1024 1024"
    }
  }
}
```

`backend` 可选值：

| 阶段 | backend | 要求 |
|---|---|---|
| image | `flux` | FLUX 系通用通路（flux1-dev / Klein / FLUX.2 / Z-Image 都走它，参数各自配） |
| image | `qwen_image` | 需要 `clip_type: "qwen_image"`，支持参考图 i2i |
| 3d | `hy21` | Hunyuan3D 2.1，质量最高 |
| 3d | `multiview` | Hunyuan3D V2 多视图，快 |
| 3d | `comfy3d_pipeline` | ComfyUI-3D-Pack 的 V2 Fast 管线 |
| 3d | `native_comfyui` | TripoSplat 等原生节点 |
| video | `h3` | MiniMax H3 Ref2VA |

**想知道某个 `backend` 在代码里对应什么**：搜 `backend/comfy_client.py` 里同名的组装函数，
或看 `backend/stages/{image,threed,video}.py` 的分发逻辑。

### `stages`

```jsonc
"stages": {
  "image": { "default_model": "qwen21_ref", "preview_candidates": 1, "fallback_chain": "flux" },
  "3d":    { "default_model": "hunyuan21", "fallback_chain": "multiview" },
  "video": { "default_model": "h3" }
}
```

- `default_model` 必须是 `models.<阶段>` 里存在的 key。写错不会报错，会**静默回退到第一个模型**并在日志里警告 —— 所以改完看日志。
- `fallback_chain`：主模型失败时依次尝试。

### `comfyui_idle`

```jsonc
"comfyui_idle": { "free_after_minutes": 10 }
```

闲置多少分钟把 ComfyUI 的模型卸掉。小显存建议 3–5。

### `autopilot`

```jsonc
"autopilot": { "enabled": true, "delay_sec": 5, "model": "hunyuan21", "quality": "快速" }
```

出图后自动接着建模，省一次点击。想手动控制就设 `enabled: false` ——
它会和你的手动建模抢，导致同一组建两次模。

### `generation`

```jsonc
"generation": { "default_batch_size": 4, "max_batch_size": 4 }
```

一次出几张候选。显存小的卡把这两个都设成 1。

---

## 4. 换模型的完整步骤

以"把生图模型从 Qwen-Image 换成 FLUX.2 Klein"为例：

**① 把权重放进 ComfyUI**

```
<COMFYUI_DIR>/models/diffusion_models/flux-2-klein-xxx.safetensors
<COMFYUI_DIR>/models/vae/flux2-vae.safetensors
<COMFYUI_DIR>/models/text_encoders/flux2-klein-xxx.safetensors
```

**② 确认 ComfyUI 能看见它** —— 打开 ComfyUI 的后台，在节点下拉框里找。找不到就是路径或文件名不对，
这时候改铸想的配置也没用。

**③ 在 `config.json` 里加一段**

```jsonc
"models": {
  "image": {
    "flux2klein": {
      "name": "FLUX.2 Klein 9B",
      "backend": "flux",
      "checkpoint": "flux-2-klein-xxx.safetensors",
      "vae": "flux2-vae.safetensors",
      "clip_l": "flux2-klein-xxx.safetensors",
      "steps": 8, "cfg": 1.0,
      "resolution": "1024 1024"
    }
  }
}
```

**④ 切默认**

```jsonc
"stages": { "image": { "default_model": "flux2klein" } }
```

**⑤ 重启 app，看日志** —— 有没有 `⚠️ 模型 key 不存在` 的警告。

换**完全不同家族**的模型（比如从扩散模型换成别的东西），只改配置不够 —— 需要在
`comfy_client.py` 里加组装函数、在 `stages/image.py` 里认新的 `backend` 值。
判断方法：先搜 `comfy_client.py` 有没有现成的同名函数。

---

## 5. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `COMFYUI_DIR` | ComfyUI 安装目录（**必需**，除非同级目录叫 ComfyUI） | `<仓库同级>/ComfyUI` |
| `COMFY_PYTHON` | 启动 ComfyUI 用的解释器 | 当前解释器 |
| `BAMBU_EXE` | `bambu` CLI 路径（打印功能） | 从 `PATH` 找 |
| `LLAMA_SERVER` | 提示词扩写服务可执行文件 | 只连不拉 |
| `LLAMA_MODEL` | 扩写服务用的 GGUF | 同上 |
| `FOUNDRY_PORT` | Web 端口 | 7860 |
| `CAD_PYTHON` | 装了 CadQuery 的解释器（跑 `parts_cad` 和对应测试） | 不设则跳过 |

---

## 6. 三套开箱配置

不想逐项调，直接把下面某一段整个替换进 `config.json` 的对应位置。

### A. 单机自用、32G 卡、只要 3D 打印件

```jsonc
{
  "stages": {
    "image": { "default_model": "qwen21_ref" },
    "3d":    { "default_model": "hunyuan21" }
  },
  "autopilot": { "enabled": true, "delay_sec": 5, "model": "hunyuan21", "quality": "高质量" },
  "comfyui_idle": { "free_after_minutes": 10 },
  "models": { "video": {} }
}
```

### B. 显存紧张、优先能跑

```jsonc
{
  "agent": { "enabled": true, "provider": "ollama", "prompt_style": "compact" },
  "stages": {
    "image": { "default_model": "flux" },
    "3d":    { "default_model": "hunyuan21" }
  },
  "autopilot": { "enabled": false },
  "generation": { "default_batch_size": 1, "max_batch_size": 1 },
  "comfyui_idle": { "free_after_minutes": 3 }
}
```

### C. 只用来批量出图（不建模）

```jsonc
{
  "agent": { "enabled": false },
  "stages": { "image": { "default_model": "z_image", "preview_candidates": 1 } },
  "autopilot": { "enabled": false },
  "generation": { "default_batch_size": 4, "max_batch_size": 8 }
}
```
