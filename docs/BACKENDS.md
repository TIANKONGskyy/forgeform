# 不用 ComfyUI 跑铸想

铸想原来只有一条路：把工作流 JSON 提交给 ComfyUI。现在多了一条 ——
`backend.mode = "direct"`，直接在 Python 里调 `diffusers` / `hy3dgen`，不经过 ComfyUI。

默认仍然是 ComfyUI。**不写这个配置，行为一行都不变。**

---

## 一句话状态

| 阶段 | 直连（direct） | 依赖 |
|---|---|---|
| 出图 | ✅ **端到端已验证** | `diffusers`（原生支持 Flux / Qwen-Image / Z-Image，实测 0.39.0 全有） |
| 3D | ⚠️ 代码就绪，权重待装 | `hy3dgen`（PyPI 有 2.0.2，`pip install hy3dgen` 已通） |
| 视频 | ❌ 不支持 | MiniMax H3 只有 ComfyUI 节点实现，没有独立 pipeline |

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

## 真实成本：先翻一遍本地，别急着下

这是最容易被忽略的一条。**ComfyUI 的权重大多不能直接给 diffusers 用**：

| 现有文件 | 为什么不行 |
|---|---|
| `qwen_image_2.1_int8_convrot.safetensors` | 自定义 int8 量化，`from_single_file` 不认 |
| `flux-2-klein-9b-Q5_K_M.gguf` | GGUF，diffusers 不支持这种布局 |
| `z_image_turbo-Q8_0.gguf` | 同上 |
| `hunyuan3d-dit-v2-1_fp16.ckpt` | 3D-Pack 的私有布局 |

**但有一类例外：你自己做过 LoRA 微调的话，本地多半躺着完整的 diffusers 格式 base 模型。**
作者这台机器上就有 —— `D:\AI-Workshop\finetune\flux2klein-base`，**32.3 GB，完整可用**，
一次都没重下。

先花 10 秒翻一遍，比下 20 GB 划算：

```powershell
# 找所有 diffusers 格式模型（有 model_index.json 的目录）
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

（`fetch_model.py` 自动挑镜像、8 线程分片、断点续传；实测峰值 4.97 MB/s。）

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
