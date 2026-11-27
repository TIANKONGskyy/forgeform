# Changelog

本项目在 2026-11 首次开源，之前的开发没有对外发布版本，所以下面只从首个公开版本开始记。

## v3.8.0 — 多视图重建 / 小壳过滤 / 精修与绑定（2026-11-26）

这一版把两条并行研究线合并进来：Codex 那边做的**四视图编排与参考图编辑**，
和本仓库做的**六方向实测**（贴图 / 骨骼 / 运动 / 精度 / 多视图 / 低显存）。
所有数字都来自本机 V100 32G 实测，不是估算。

**新增：多视图重建（真实照片）**

- `Pipeline.generate_3d_multiview(views)`：四张真实照片直传 2mv
  - 实测 **84 s / 490,754 面 / 水密**，优于单图路径（同一架飞机：多视图 0.4872 vs 单图 0.4709）
  - **不做任何预处理**（不抠图、不正交化、不做尺度归一）——实测预遮罩会把 0.4872 打到 0.3112
  - 强制 `Hunyuan3D-2mv-*` 模式；不调用 `/free`（会卸载抠图模型导致必崩）
- `backend/multiview_prompt.py`：文字引导的前视图编辑 + 透明通道重接 + 视图顺序校验
- `backend/view_classifier.py`：本地 VLM 给出 front/back/left/right 建议，**判不准的一律不猜**
  - 修了一处「视图有空洞却返回空且无原因」的静默失败

**新增：小壳过滤**

- `mesh_optimizer.OptConfig.debris_min_faces` / `debris_min_ratio` + `_remove_debris()`
  - 只删「面数低于绝对下限且占比低于阈值」的碎块，保留浮筒/眼镜腿这类有意义的分离件
  - 实测依据：Direct3D-V3 输出 1,290 个连通块，其中 1,275 个是 <100 面碎壳（合计 0.86% 面）
- `Pipeline._drop_debris()` 接在 3D 质量闸前，碎壳云不再被一刀判死

**新增：精修与自动绑定（可选增强档）**

- `tools/asset_refine_rig.py`
  - `refine`：Direct3D-V3 512 档精修（32.5 s / 5.8 GiB / 输出水密）
  - `rig`：自动绑定（模板骨架 8–68 s，零显存；RigAnything 52 s / 4.7 GB / 52 根语义骨）
- `backend/rigging.py`：绑定调度与守门（占位锁、GPU 争用检查、**用二进制校验 GLB 里真有 skin**）
  - 换掉了原实现的 UniRig 后端：实测 UniRig 硬依赖 flash_attn，FA2 无 sm70 内核，V100 上跑不起来

**修复**

- 直连后端忽略 `max_facenum`：2mini@512 一度吐出 327 万面（应为 20 万）
- `generate_3d()` 支持 `seed` 参数（原来写死 `-1`，不可复现）
- `hunyuan21` 插件缺 `path` 导致一直「未就绪」——高质量模型其实从未真正可用
- 3D-Pack Paint PBR 硬编码 `snapshot_download`，本地已有 6.41 GB 权重仍会联网拉 11 GB
  → 补丁优先本地目录（`FORGEFORM_PAINT_PBR_DIR`），重启 ComfyUI 生效

**测试**

- 新增 20 个回归测试：`tests/test_multiview_and_debris.py`、`tests/test_codex_merge.py`
- 全量测试通过（含 172 个流程契约测试）

**文档**

- `docs/RESEARCH-MASTER-2026-11-26.md`：六方向总表（贴图 / 骨骼 / 运动 / 精度 / 多视图 / 低显存）
- `docs/RESEARCH-BRIEF-6DIRECTIONS-2026-11-26.md`：本机环境事实与踩坑清单
- `docs/MERGE-CODEX-2026-11-26.md`：这次合并做了什么、拒了什么、为什么

## v3.8.6 — 并行下载与并行配置（2026-11-27）

装模型时原来是一个个下、单连接：国内到 HF 只有 1–2 MB/s，几个大文件排着队。

**① `backend/downloader.py`（并行下载引擎）**
- **多文件并发**（默认 8 文件）
- **单文件多段**（默认 8 连接，走 Range 分段并发写同一文件）
- 镜像回落（ModelScope 优先 → hf-mirror → gh-proxy）、`.part` 断点续传、大小/哈希校验
- 先写 `.part`，校验过才改名 —— 半成品永远不会冒充成品

**本机实测**

| 场景 | 结果 |
|---|---|
| 6 文件串行单连接（限速服务器） | 24.9 s |
| 6 文件 × 4 连接 | **4.3 s（5.8×）** |
| 单文件 128 MB 单连接 | 33.2 s / 3.9 MB/s |
| 单文件 128 MB × 4 连接 | **8.6 s / 14.8 MB/s（3.8×）** |
| hf-mirror 4 段 | 1.2 MB/s |
| ModelScope 4 段 | **4.2 MB/s** → 设为首选 |

**② `tools/bootstrap_models.py`（并行配置）**
四条互不依赖的线同时推进：下载权重、解压 ComfyUI、clone 3D-Pack、pip 装铸想依赖；
之后才 pip 装 ComfyUI 依赖 → 写 config → 体检。`--only core|paint|mv|refine|rig|all`，
`--dry-run` 先看计划，权重清单的 URL 全部 HEAD 校验过（修掉了 Direct3D/RigAnything 的 401）。

**③ 安全阀**
镜像返回 200 的 HTML 错误页是最常见的坑 —— 大小差太多直接判失败换镜像；
`exact_size` 可开逐字节严格模式。

**测试**：新增 3 个（并行+分段、跳过已存在与大小校验、套件覆盖），
全量 pytest 通过，`verify.py` 152/152。

## v3.8.5 — 清空本地模型后的"零模型可用"与技能包（2026-11-27）

本机删除了 ComfyUI（341.9 GB）、comfy312、铸想 models（23.9 GB）、研究权重（16.7 GB）、
`D:\models`（31.2 GB）、Ollama（模型 42.2 GB + 程序 5.9 GB）、HF 缓存（4.8 GB）——**约 477 GB**。
清单见 `docs/LOCAL-MODELS-INVENTORY.md`。目标：验证"从零安装"的真实体验。

**① 零模型也能启动（实测）**
`doctor` 8/10：明确报「ComfyUI 目录不存在」「3D 权重 0/2」；`app.py` 正常开到 7860，
对话返回规则模板（`尺寸：90.0mm · 模型：qwen21_ref`），出图/建模报缺什么，不崩。

**② 远程对话通道**（`describer._call_remote`）
`agent.provider = remote` → 走 `models.llm.remote`（OpenAI 兼容 `/chat/completions`，
密钥从环境变量或 `.secrets` 读，绝不写进 config）。远程不通自动回落本地/模板。

**③ 技能包**（`skills/forgeform/SKILL.md` v3.8.4 + `backend/skills.py`）
- 技能文档重写：13 行「能力速查」表（做什么/怎么做/耗时/依赖），覆盖出图、单图与多视图建模、
  贴图、骨骼两档、动画、精修、STL 导出、资产、体检、上报
- 加载器只把速查段（703 字）注入 agent prompt（整篇 4 KB 会让 3B 小模型变慢），
  带 mtime 缓存，改文件自动失效
- 实测 prompt 里能看到「绑骨骼/精修/我的资产」等动作提示

**④ 从零安装文档** `docs/INSTALL-FROM-SCRATCH.md`
三种对话模型选型（远程 / qwen2.5:3b 冷启 12.2 s / 不装）、ComfyUI 便携版与 V100 启动参数、
3D-Pack 权重放置、贴图的两个坑、每步自检命令、已知卡点表。

**⑤ 盘点工具** `tools/inventory_models.py`
列出 9 个目标目录的占用与 >200 MB 权重清单、Ollama 模型表，输出 JSON。

**测试**：新增 4 个（技能加载与缓存失效、远程通道未配置时不抛、盘点覆盖范围），
全量 pytest 通过，`verify.py` 152/152。

## v3.8.4 — 对话直达能力 / 资产库 / 一键开始 / 问题上报（2026-11-27）

**① 对话直达能力**（`backend/capability_router.py`）
在对话框里说「给它绑骨骼 / 上个色 / 精修一下 / 做个走路动画」直接干活，不走 LLM 绕圈。
- 识别：能力词 + **必须有产物**；「做一个会走路的机器人」「设计一个杯子」不会被抢
- 参数从话里捞：`精修 15 步`、`用 RigAnything 绑骨骼`、`让它跑起来`
- 跑完把产物路径、面数/水密/关节数、资产编号直接回在对话里

**② 资产库**（`backend/assets.py`）
- 统一存放 `outputs/assets/<kind>/`，索引 `index.json`
- **同内容去重**（sha256），删除同时清文件和索引
- 容量上限 8 GB（`FORGEFORM_ASSET_MAX_BYTES` 可改），超了按时间淘汰最旧的
- 界面「🛠 能力工作台 → ⑥ 我的资产」按类型/时间列出，带指标
- 实测：12 件归档 + 1 件对话产物 = 13 件 / 240.7 MB

**③ 一键开始**（`一键开始.ps1`）
找 Python（PATH + 常见安装目录）→ `doctor --fix` → 启动。体检不过会**直接说缺什么**，
不会假装启动成功。`install.ps1` 也补了体检（版本从 v3.0.0 升到当前）。

**④ 问题上报**（`tools/doctor.py` / `tools/report.py`）
- `doctor`：10 项体检（Python/依赖/GPU/torch-CUDA/ComfyUI/配置/目录/磁盘/权重/端口），
  每项失败都给修复命令；`--fix` 自动建目录、补配置
- `report`：生成脱敏诊断包 zip（key/token/密码打码、家目录换 `~`、日志尾部，
  附 `capability-failures.jsonl`），本机实测零明文密钥
- `.github/ISSUE_TEMPLATE/bug_report.yml`：带"先跑 report.py"的引导
- `docs/QUICKSTART.md`：5 分钟上手，每步都有"怎么确认成功"

**测试**：新增 6 个（资产去重/删除、未知类型拒绝、路由命中与不抢、体检结构、
诊断包脱敏），全量 pytest 通过，`verify.py` 152/152。

## v3.8.3 — 五项能力接进界面 + 各路线成品总览（2026-11-27）

**能力工作台（界面）**：`backend/capability_ui.py`，在 app 里挂一个「🛠 能力工作台」折叠面板，
五个 Tab —— 骨骼绑定 / 贴图 PBR / 精修 / 多视图 / 动画。每页都有：输入 → 跑 → 3D 预览 → 下载 + 耗时日志。

- 全部调用**同一套产品入口**（`backend.rigging` / `tools/paint_asset.py` /
  `tools/asset_refine_rig.py` / `Pipeline.generate_3d_multiview` / `retarget_lib`），
  所以 UI 跑通 = CLI 跑通，不存在"两套实现"。
- 对外 API 名固定：`/cap_rig` `/cap_paint` `/cap_refine` `/cap_multiview` `/cap_motion`。

**实测（全部走 gradio 客户端真调 UI 按钮）**

| 能力 | 耗时 | 结果 |
|---|---|---|
| 骨骼·模板 | 45 s | 21 关节 |
| 精修 4 步 | 53 s | 小壳 712 → 1 块，1,715,294 面 |
| 多视图 | 6 s（缓存）/ 84 s 冷启 | 490,754 面，水密 |
| 动画 | 14 s | 穿地 0.000 mm |
| 贴图·快速档 | 471 s | 2 张贴图，峰值 9.65 GB |

**关键修复：UI 里跑 GPU 能力会 OOM**

预热把 Strata 125B（约 28 GB）驻进显存 → 只剩 1.8 GB，精修要 5 GB 直接 OOM。
主对话流程本来会 `_release_chat_gpu()`，能力面板绕过了那条路。
现在每个 GPU 重活入口先 `_free_gpu()`（chat 释放 + strata_stop + 卸载 tuned），模板档（CPU）不释放。

**新工具 `tools/collect_gallery.py`**：把所有生成路线的代表产物收进一个文件夹
（`D:\铸想模型总览`），统一命名 + 二进制体检（面数/水密/贴图/关节/动画）+ 三视角预览图 + `总览.md`。
实测 12/12 收齐。

**测试**：新增 3 个（面板五 Tab 与接口名 / 重活前释放显存 / 总览覆盖每条路线），
全量 pytest 通过，`verify.py` 152/152。

## v3.8.1 — 贴图产品入口 + 精修自动过滤（2026-11-27）

**新增 `tools/paint_asset.py`**：五项能力里最后缺的那个产品入口。

- `paint(mesh, image, out, texture=2048, view=512, steps=20, fast=False)`
- 提交前自检四个环境坑（节点包 / 权重目录完整性 / tokenizer 同源 / 枚举值），不通过就立刻返回
- 产物用二进制校验（GLB 里必须真有 textures + UV + PBR），不信文件大小
- 实测（plane_21.stl，406k 面）：
  - **完整档 6 视角/12 步/1024²：1236 s，峰值 10.7 GB** —— 涂装正确
  - **快速档 `--fast` 4 视角/8 步：458 s，峰值 9.7 GB** —— 只适合预览（atlas 发噪）

**`refine()` 自动过小壳过滤**：Direct3D-V3 精修产物天然带碎壳
（实测 713 个连通块，712 个 <100 面），原来是验证时手动清，现在 refine 返回前自动清
（713 → 1，面数 1,727,578 → 1,715,294，仍水密）。

**修复**：`refine()` 现在接受 str 路径（CLI 传的就是 str，之前会 `AttributeError`）。

**测试**：新增 3 个（贴图工作流形状 / 参数校验 / refine 容错），全量通过，`verify.py` 152/152。

## v3.0.0 — 首次开源

**流程**

- 对话式工作流：需求 → 出图 → 确认 → 建模 → 导出 STL，全程本地推理
- 多对话组隔离，每组一次建模；项目归档与恢复
- 出图后自动建模（`autopilot`，5 秒延迟，可关可取消）
- 逆向入口：上传视频抽帧建模、上传 GLB 直接转 STL
- 按钮按阶段显隐（phase → `_buttons_for_phase`），不适用阶段也会给重试出口

**模型**

- 出图：Qwen-Image-2.1（默认）、FLUX.1-dev FP8、FLUX.2 Klein/Dev、Z-Image Turbo，可按 `stages.image.default_model` 切换
- 视频：MiniMax H3 Ref2VA（首尾帧引导 + Turbo LoRA）
- 3D：Hunyuan3D 2.1（默认）、Hunyuan3D V2 多视图、TripoSplat，四档后端
- 对话：任意 OpenAI 兼容本地端点（实测 Qwen3.8-Flash-Next 125B）、Ollama，或纯规则模式

**工程**

- `backend/env.py` 统一路径：clone 到任何目录都能跑，外部依赖走环境变量
- 显存仲裁：对话 / 出图 / 建模 / 视频分时占用，切换前主动卸载
- 水密闸门：pymeshlab 修完之后按 `is_watertight` 复检边界边，不合格再走 pymeshfix
- 172 个流程契约测试，不依赖 GPU 和 ComfyUI
- `tools/verify.py` 作为权威验证入口
- 开机自检：目录、端口、依赖、服务状态，能修的自己修

**文档与协作**

- `README.md`：使用边界、按显存分档的推荐配置、实测耗时表
- `docs/CONFIG.md`：字段说明 + 三套开箱配置 + 换模型的完整步骤
- `docs/ARCHITECTURE.md`：模块职责、三个核心机制、测试策略
- `docs/showcase/`：作品展示页，数据来自真实运行记录
- `skills/forgeform/SKILL.md`：符合 Agent Skills 标准的技能文件，教会 agent 安装、使用、换模型、排错
- `CONTRIBUTING.md` / `SECURITY.md` / `NOTICE` / CI workflow

**许可**

- 采用 **AGPL-3.0**。原因不是偏好，是依赖决定的：水密修复用了 `pymeshfix`（AGPL-3.0）
  和 `pymeshlab`（GPL-3.0），链入 copyleft 的程序只能同样 copyleft。
  想闭源商用的做法在 README「许可证」一节写了。
- 用它产出的模型与打印件归使用者，不受 AGPL 约束。

**平台**

- Windows 优先（PowerShell 启动脚本、Bambu 打印）
- 单卡/双卡自适应启动 ComfyUI
- 拓竹打印机局域网发现 + 直发（可选依赖 bambu-cli）
