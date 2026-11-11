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

用 `segmind/tiny-sd`（990 MB，最小的真 SD 模型）跑 `tools/bench_direct.py`：

```
[direct] 出图 tiny_sd (StableDiffusionPipeline) 256x256 4步 …
[direct] 出图完成 0.9s → direct_1794381220_b9625a.png
job=72a7c48d  推理 40.5s          ← 含 39s 模型加载，真正采样 0.9s
['direct-bench\images\direct_1794381220_b9625a.png']
http://127.0.0.1:56550/view?filename=images/direct_1794381220_b9625a.png
HTTP 200  64064 字节
✅ 端到端通过
```

这验证的不是画质（tiny-sd 本来就糊），而是**整条链成立**：
插件/配置 → `DirectBackend` → diffusers 加载 → 采样 → 落盘 → 伪 `/view` 协议 → `requests` 拿回。

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

## 真实成本：权重要重下

这是最容易被忽略的一条。**你现有的 ComfyUI 权重，直连模式一个都用不了**：

| 现有文件 | 为什么不行 |
|---|---|
| `qwen_image_2.1_int8_convrot.safetensors` | 自定义 int8 量化，`from_single_file` 不认 |
| `flux-2-klein-9b-Q5_K_M.gguf` | GGUF，diffusers 不支持 |
| `z_image_turbo-Q8_0.gguf` | 同上 |
| `hunyuan3d-dit-v2-1_fp16.ckpt` | 3D-Pack 的私有布局，`hy3dgen.from_pretrained` 要 diffusers 目录结构 |

要重下 diffusers 格式：

```powershell
# 例如 Z-Image Turbo（约 20 GB）
$env:HF_ENDPOINT = "https://hf-mirror.com"
hf download Tongyi-MAI/Z-Image-Turbo --local-dir D:\models\z-image-turbo-diffusers --exclude "assets/*"

# Hunyuan3D-2mini（约 3 GB）
hf download tencent/Hunyuan3D-2mini --local-dir D:\models\hunyuan3d-2mini
```

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
