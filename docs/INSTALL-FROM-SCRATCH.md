# 从零安装铸想（本机已清空模型后的实测路径）

> 2026-11-27 本机删除了 ComfyUI、comfy312、Ollama 与全部本地权重（约 477 GB）。
> 这份文档是**按"什么模型都没有"的状态**写的，每步都标了会卡在哪。

## 0. 现在机器上还剩什么

| 还在 | 说明 |
|---|---|
| `D:\ForgeForm-oss`（源码） | 8.7 GB（含 outputs 产物） |
| `D:\forgeform-v3.8.4.zip` | 安装包 3.3 MB |
| Python `D:\miniconda3` | 3.11.15，torch 2.14.1+cu126 已装 |
| V100 32G | 驱动 580.88 |

**没有**：ComfyUI、ComfyUI 的 venv、Ollama、任何 3D/图像/对话权重。

## 1. 冒烟：零模型能不能起来（30 秒）

```powershell
cd D:\ForgeForm-oss
$env:PYTHONPATH=''; $env:PYTHONIOENCODING='utf-8'
python tools/doctor.py
python app.py
```

**预期**：`doctor` 报 8/10，明确列出「ComfyUI 目录不存在」「3D 权重 0/2」；
`app.py` 能开户到 7860，对话可用（走规则/远程模型），出图与建模会**明确报缺什么**，不会崩。

实测（2026-11-27）：

```
✓ Python 3.11.15  ✓ 依赖齐全  ✓ GPU  ✓ torch CUDA
✗ ComfyUI 配置的目录不存在：D:\ComfyUI-New
✗ 3D 权重 0/2 已就位
8/10 通过
→ 界面对话返回："• 尺寸：90.0mm • 模型：qwen21_ref 以上理解正确吗？"
```

## 2. 装对话模型（三种选，按需）

### 2a. 远程 API（最省事，不占显存）

```jsonc
// config.json
"agent": { "provider": "remote" },
"models": { "llm": { "remote": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key_env": "FORGEFORM_LLM_KEY"
}}}
```

```powershell
$env:FORGEFORM_LLM_KEY = "sk-..."     # 不要写进 config / 不要提交
```

**验证**：界面对话说一句，2–5 秒内回话。

### 2b. 本地小模型（实测最快）

```powershell
# 装 Ollama 后
ollama pull qwen2.5:3b
```

```jsonc
"agent": { "provider": "ollama", "model": "qwen2.5:3b" }
```

实测本机：**冷启 12.2 s / 常驻 6.5 s**（27B 级要 60 s 以上，别用）。
显存占用约 2 GB，和建模轮换着用没问题。

### 2c. 不装对话模型

`agent.provider` 留空 → 走规则路径 + 模板回复，能出图建模但不能自然对话。

## 3. 装 ComfyUI（出图/建模/贴图都靠它）

```powershell
# 1) 拿便携版（GitHub 直连不通就用代理）
iwr -useb https://gh-proxy.com/https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z -OutFile ComfyUI.7z
# 2) 解压到 D:\ComfyUI-New（路径别带空格/中文）
# 3) 建一个独立 venv（不要用 portable 自带的，torch 版本可能不匹配 V100）
python -m venv D:\comfy312
D:\comfy312\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu126
D:\comfy312\Scripts\python.exe -m pip install -r D:\ComfyUI-New\requirements.txt
```

**启动参数（V100 必须这么起）**：

```powershell
D:\comfy312\Scripts\python.exe D:\ComfyUI-New\main.py --listen 0.0.0.0 --port 8188 `
  --disable-cuda-malloc --use-pytorch-cross-attention
```

- **不要加 `--lowvram`**：V100 是 sm_70，采样阶段会报 `operation not supported`
- 必须 `PYTHONIOENCODING=utf-8`（ComfyUI 日志写 emoji，GBK 会在绑端口前崩）

## 4. 装 3D 节点包和权重

```powershell
# 3D-Pack（提供 Hunyuan3D 2.1 / 2mini / 2mv / Paint）
cd D:\ComfyUI-New\custom_nodes
git clone https://gh-proxy.com/https://github.com/MrForExample/ComfyUI-3D-Pack.git
```

权重放 `custom_nodes\ComfyUI-3D-Pack-main\Checkpoints\Diffusers\tencent\Hunyuan3D-2_1\`：

| 权重 | 大小 | 用途 |
|---|---|---|
| `hunyuan3d-v2-1` | 4.7 GB | 单图建模（主力） |
| `hunyuan3d-paintpbr-v2-1` | 6.4 GB | PBR 贴图 |

**贴图权重的两个坑（实测，必踩）**：

1. 必须是**完整 diffusers 目录**（`model_index.json` + `unet/` `vae/` `text_encoder/`），
   平铺的 `unet.bin` 不行 → 报 `no file named model_index.json`
2. `tokenizer/` 里 `vocab.json` 和 `merges.txt` **必须同源**，缺 merges 会报
   `vocab and merges must be both from memory or both filenames`

## 4.5 并行下载（推荐，比逐个下快 4–6 倍）

别一个个下。用引导脚本，下载池、解压、clone、pip 同时推进：

```powershell
python tools/bootstrap_models.py --only core      # 最小可用（约 12 GB）
python tools/bootstrap_models.py --only all       # 全能力（约 16 GB 权重 + ComfyUI）
python tools/bootstrap_models.py --only core --workers 8 --conns 8
python tools/bootstrap_models.py --dry-run        # 只看计划
```

**本机实测（2026-11-27）**

| 场景 | 结果 |
|---|---|
| 本地限速服务器：6 文件串行单连接 | 24.9 s |
| 同上，6 文件 × 4 连接并行 | **4.3 s（5.8×）** |
| 单文件 128 MB：1 连接 | 33.2 s（3.9 MB/s） |
| 同上：4 连接分段 | **8.6 s（14.8 MB/s，3.8×）** |
| hf-mirror 4 段 × 8 MB | 1.2 MB/s |
| **ModelScope 4 段 × 8 MB** | **4.2 MB/s** ← 已设为首选镜像 |

**参数怎么定**：总连接数 ≈ `workers × conns`。镜像限速时加 `conns` 最有效
（单文件多段），文件多时加 `workers`。默认 8 × 8。

**断点续传**：文件先写 `.part`，中断后重跑会继续；大小/哈希不符**不会**改名成正式文件。
`--dry-run` 能先看清要下多少。

**故障保护**：
- 镜像返回 200 的 HTML 错误页 → 大小与清单差太多会直接判失败换镜像
- 权重清单里的 `bytes_hint` 是估算值，默认 ±5% 容差；要逐字节严格传 `exact_size`

## 5. 接上铸想

```powershell
python tools/doctor.py --fix     # 会把 paths.comfyui 指向新目录
```

```jsonc
// config.json
"paths": { "comfyui": "D:\\ComfyUI-New" },
"backend": { "mode": "auto" }
```

再跑 `python tools/doctor.py`，应该 10/10。

## 6. 想要额外能力才装（都不装也能用）

| 能力 | 还要装 | 大小 | 不装的后果 |
|---|---|---|---|
| 多视图重建 | `hunyuan3d-2mv` | ~9 GB | 只能单图建模 |
| 精修 | Direct3D-V3 512 + DINOv3 | ~8 GB | 说「精修」会报缺权重 |
| 语义骨骼（含手指） | RigAnything `riganything_ckpt.pt` | 2.2 GB | 只能用模板档（纯 CPU，够用） |
| 贴图 | Paint PBR 2.1 | 6.4 GB | 不能上色 |
| 动画 | 无 | 0 | 不装也能用 |

## 7. 每步的自检命令

```powershell
python tools/doctor.py                      # 10 项体检
python -c "import requests;print(requests.get('http://127.0.0.1:8188/system_stats').json()['system'])"
python -c "import trimesh;m=trimesh.load('你的.stl');print(m.is_watertight)"
python tools/report.py                      # 出问题生成脱敏诊断包
```

## 8. 已知会卡的地方（按我们踩过的顺序）

| 现象 | 原因 | 处理 |
|---|---|---|
| ComfyUI 起了但建模报「未就绪」 | 权重路径和 config 里的 key 不一致 | `doctor` 的「3D 权重」项会列出缺哪个 |
| 贴图报 `InvalidSchema` / 连不上 | `paths.comfyui` 写成了目录而不是 URL | 已修：目录会自动用 `127.0.0.1:8188` |
| 建模 OOM | 显存被对话模型占着 | 用远程模型，或说「先释放显存」 |
| 对话第一次要 60 s | 27B 级模型冷启动 | 换 `qwen2.5:3b` 或远程 API |
| 生成完 STL 是空壳 | 没走「导出 STL」 | 别自己转，走按钮（会补洞） |

## 9. 安装脚本自己踩过的坑（2026-11-28 实测）

| 现象 | 根因 | 修法 |
|---|---|---|
| 装完是旧版本（v3.8.8） | `install.ps1` 用 `/releases/latest` 解析版本，gh-proxy 把这个响应**缓存住了** | 改用 `releases?per_page=5` 列表 API，取第一条非 draft/pre-release |
| 下载卡 3 分钟 | 第一源是 github.com，直连超时 | 镜像优先，单源 30 s |
| 输出中文花屏 | PS 5.1 按 GBK 读 UTF-8 无 BOM 脚本 | 脚本内只用 ASCII 输出 |
| 解压后目录名对不上 | 发布包内层是 `forgeform-v3.9.1`，脚本按 `forgeform-3.9.1` 找 | 用 fallback 找第一个 `forgeform-*` |

## 10. 发布包实测（v3.9.7，2026-11-28 10:00）

用**线上发布包**（不是开发目录）在空目录里从零装一遍，全程真数字：

| 步骤 | 命令 | 耗时 |
|---|---|---|
| 下载解压 | gh-proxy 拿 v3.9.7（4.0 MB） | 25 s |
| ① 环境自检 | `python tools/doctor.py --fix` | 14 s（9/10） |
| ② 直连建模 | `python tools/bootstrap_models.py --only direct` | 197 s（12 GB） |
| ③ 出图栈 | `python tools/bootstrap_models.py --only image` | 485 s（11 GB + ComfyUI 环境） |
| 实测出图 | z_image 1024²/4 步 | 18 s |
| 实测建模 | 2mini 图→3D（10 万面） | 72 s |
| **文→图→3D** | 一次跑完 | **90 s** |

产物：610 KB PNG + 1.7 MB GLB（5 连通块，质量闸通过）。

结论：发布包可以独立工作，不含开发目录里的任何本地补丁。


## 11. 首次用户验收（v3.10.0，2026-11-28 10:36–11:05）

完全按"用户第一次打开"的顺序走一遍，每一步都是真数字：

| 步骤 | 结果 |
|---|---|
| 解压线上 v3.10.0（4.0 MB） | 24 s |
| 起界面（零模型） | ✅ 聊天区顶部出现提示「还差：建模权重、出图模型」+ 两条命令 |
| `doctor.py --fix` | 9/10 通过 |
| `bootstrap_models.py --only direct` | **174 s**，12 GB 直连权重就位 |
| `bootstrap_models.py --only image` | **564 s**（权重 243 s + ComfyUI 环境 554 s 并行） |
| 重启界面 | ✅ **提示条自动消失**（页面里已无"还差"字样） |
| 文→图（z_image 1024²/4 步） | **25 s** → 567 KB PNG（带把手的马克杯，形状正确） |
| 图→3D（2mini，15 万面） | **72 s** → 2.6 MB GLB（1 连通块，质量闸通过） |
| 导 STL（产品自己的 `export_stl`，90 mm） | **12.1 s** → 3.8 MB / 8 万面 / **水密** / 实测 **90.0 × 90.0 × 69.5 mm** |

**从解压到拿到 90 mm 可打印 STL，全流程约 15 分钟（其中安装 13 分钟，真正生成 97 秒）。**

