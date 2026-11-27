# 铸想端到端实测总报告（2026-11-28）

> 在本机（Gooxi / V100 32G / Windows）从"零模型"重新装一遍，逐个模型跑通，做一次模型修改，
> 收集全部数据，最后删干净。目的：找出开源用户真实会踩的坑。
> 全程网络：前段 1–8 MB/s（镜像限速），后段恢复到 80–110 MB/s。

## 一、跑通的矩阵

### 图像模型（5/5）

| 模型 | 权重构成 | 档位 | 耗时 |
|---|---|---|---|
| z_image（Z-Image Turbo 6B int8） | 5.8 GB | 1024² / 4 步 | **21 s** |
| flux_klein（FLUX.2 Klein 4B fp16） | 7.2 GB | 1024² / 4 步 | **28 s** |
| flux2_dev（FLUX.2 Dev Q4_K_M + Mistral fp4） | 18.7 + 11.4 GB | 1024² / 4 步 | **58 s** |
| flux（FLUX.1-dev fp8） | 16.4 GB | 1024² / 14 步 | **61 s** |
| qwen21_ref（Qwen-Image fp8 + qwen_vl fp8） | 21.2 + 8.7 GB | 1024² / 20 步 | **464 s** |

### 3D 模型（3 条路径）

| 路线 | 档位 | 耗时 | 面数 | 水密 | 碎块 |
|---|---|---|---|---|---|
| 2mini（direct / hy3dgen） | 256 快速 | **69 s** | 147,840 | 否 | 2 |
| 2.1（direct / hy3dgen） | 256 快速 | **105 s** | 151,488 | 是 | 2 |
| 2.1（direct / hy3dgen） | 512 高质量 | **676 s** | 400,000（上限） | 是 | 1 |

文→图→3D 全链路：z_image 21 s → 2mini 72 s → 3.4 MB GLB，质量闸通过。

### 骨骼（模板档，纯 CPU）

```
53 s / 14 关节 / GLB 正常输出
```

### 用模型修改产物（256 → 512 提精度）

| 指标 | 结果 |
|---|---|
| 面数 | 151,488 → 400,000（×2.64） |
| 体积变化 | −8.06 % |
| 表面平均偏移 | 64 mm（包围盒归一） |
| 碎块 | 2 → 1 |

## 二、装不起来的四条（附根因）

| 能力 | 卡点 | 根因 | 可修性 |
|---|---|---|---|
| **贴图 PBR** | `DLL load failed: custom_rasterizer_kernel` | 节点包里的 CUDA 扩展是按旧 torch 编译的，与 venv 的 torch 2.7 不匹配 | 需重新编译扩展（要 CUDA 工具链），或用配套 torch 版本 |
| **精修 Direct3D-V3** | 缺 `model_index.json` 之外的 DINOv3 与 `third_party` CUDA 扩展 | 原脚本指向已删除的研究目录 | 需搬源码 + 编 CUDA 扩展 |
| **多视图** | 3D-Pack 依赖 `pytorch3d` | Windows 无预编译轮子，31 处 import | 可用占位包绕过导入，但 2mv 节点要用到真实实现 |
| **动画** | 缺基础骨架工程 `.blend` 与 BVH 素材 | 研究产物被清理 | 需重建骨架工程（可重新生成） |

## 三、过程中修掉的产品问题（共 15 条）

### 安装脚本
1. `install.ps1` 第一源是 github.com，直连超时白等 180 s → 镜像优先 + 单源 30 s
2. 版本写死 v3.8.4 → 自动解析 `releases/latest`
3. 末尾中文输出在 PS 5.1 下花屏 → 改 ASCII

### 环境诊断
4. `${COMFYUI_DIR}` 占位符被报成"目录不存在" → 识别为未配置
5. 零模型启动时先"卸载 Ollama"再"启动 Strata" → `provider_available()` 一行跳过

### 下载器
6. ComfyUI 便携版根本没进下载队列（只登记 URL）
7. `_extract_archive()` 少传 `log` 参数 → TypeError
8. 稀疏预分配导致"没下完也像下完了" → 去掉 truncate，按实际写入字节校验
9. 下载无进度 → 每 10 s 心跳
10. 1.99 GB 便携版镜像限速 0.3–1.3 MB/s → 新增源码安装模式（clone + pip）

### 3D-Pack
11. `git clone` 走 gh-proxy 会 0 字节卡死 → 改 zip（93.9 MB / 10 s）
12. 假成功判断（只看目录名）→ 必须有 `__init__.py` + `nodes.py`
13. 依赖不分层（编译型 CUDA 包混在必装里）→ 拆成纯 Python 必装 + 编译包提示

### 工作流
14. Z-Image 工作流写死 GGUF 节点（干净装没有 GGUF 扩展）→ 按后缀选 UNETLoader/CLIPLoader
15. `_build_flux2_workflow` 的 LoRA 指向不存在的文件 → 缺 LoRA 自动跳过

### 依赖/环境
16. 最新 ComfyUI 需要 torch ≥2.7（2.6 报 `list[int]` stride 不支持）
17. `gguf` 包没装 → ComfyUI-GGUF 扩展整个 import 失败
18. ComfyUI 升级包带 `models/` 骨架，覆盖式复制会清空 11 GB 权重
19. 后端脚本（骨骼/动画/精修/贴图）只在被 gitignore 的 `work/research` 下 → 已收进 `tools/backends/`

## 四、网络实测

| 文件 | 大小 | 耗时 | 平均速度 |
|---|---|---|---|
| Qwen-Image fp8 | 21.7 GB | 267 s | 81 MB/s |
| FLUX.2 Dev Q4 | 19.2 GB | 651 s | 29 MB/s |
| FLUX.1-dev fp8 | 16.4 GB | 206 s | 80 MB/s |
| Mistral fp4 | 11.7 GB | 538 s | 22 MB/s |
| z_image int8 + 编码器 | 11.6 GB | 236 s | 49 MB/s |

**结论：同一台机器同一镜像，速度能差 20 倍以上** —— 用户报"下不动"时要先确认是不是镜像端限速，不要直接下"网络不行"的结论。

## 五、最终状态

- 模型与 ComfyUI 已按目标删除，只留安装包
- 源码、后端脚本、实测数据保留在 `D:\ForgeForm-oss`
- 数据文件：`D:\e2e_2026-11-28\`（mesh_stats.json / report.md / files/）

## 补充：动画能力（2026-11-28 06:5x）

从仓库备份里恢复了 5 个**预烘焙动作包**（52 骨 + 具名 Action），用 Blender 逐个验证：

| 动作 | 骨骼 | Action 名 | 帧范围 | 大小 |
|---|---|---|---|---|
| walk | 52 | 05_BVH_Walk_重定向 | 1–25 | 30.3 MB |
| run | 52 | 06_Run_跑步_mocap | 1–19 | 30.3 MB |
| bow | 52 | 03_Bow_鞠躬_mocap | 1–90 | 30.7 MB |
| wave | 52 | 02_Wave_挥手_mocap | 1–65 | 30.6 MB |
| idle | 52 | 04_Idle_待机_mocap | 1–25 | 30.3 MB |

产品侧改成**优先交付预烘焙动作包**（不再要求现场重定向，干净安装也能用），
没有动作包时才回退到"基础骨架工程 + BVH 现场重定向"。

实测：`do_motion("02_01.bvh")` → 直接交付 `motion_walk_*.blend`，52 骨具名动作。
