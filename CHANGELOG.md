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

## v3.9.7 — 出图最小集 + 文→图→3D 78 秒（2026-11-28）

新增 `--only image` 套件：Z-Image Turbo int8（5.8 GB）+ Qwen3-4B fp8（5.2 GB）+ VAE（0.3 GB），
外加自动装 ComfyUI 运行环境（venv + torch 2.7 + 依赖）。

干净安装实测：

| 环节 | 耗时 |
|---|---|
| `--only direct`（hy3dgen 12 GB） | 147 s |
| `--only image`（11 GB + ComfyUI 环境） | 472 s |
| z_image 出图 1024²/4 步 | 18 s |
| 2mini 图→3D（10 万面） | 68 s |
| **文→图→3D 一次跑完** | **78 s** |

同时修：`_ensure_comfy_env` 未定义（image 套件必崩）、权重已下完时空下载 job 导致假失败。

## v3.9.6 — 干净安装实测修掉 bootstrap 四个问题（2026-11-28）

在**全新解压的发布包**上真跑 `bootstrap_models.py --only core`，暴露出四个问题：

1. **`PACK_PIP` 未定义** → `NameError`，3D-Pack 依赖整段失败（发布包里真实存在）
2. **干净安装没有 config.json**（只有 config.example.json）→ 写配置必失败
3. **体检用 `doctor.py` 而不是 `--fix`** → 缺 outputs/logs/projects/temp 报错
4. **依赖装错解释器**：bootstrap 用 `sys.executable` 装 3D-Pack 依赖，
   而 ComfyUI 实际跑在 `install_comfy.py` 建的 venv 里 → "依赖装好了 ComfyUI 还是 import 失败"

修完实测（干净解压包）：

```
bootstrap --only core     86s  → ComfyUI 源码 + 3D-Pack + 4.6GB 权重 + 依赖 + 配置 + doctor 通过
bootstrap --only core     13s  （幂等复跑）
bootstrap --only direct  147s  → hy3dgen 2mini 12GB 权重
图→3D（direct 2mini）      75s  → 100,000 面 / 1 连通块 / 质量闸通过 / 1.7MB GLB
骨骼（模板）               28s  → 14 关节
UI（7868）                 起得来，首页 374 KB
```

新增 `--only direct` 套件：只下 hy3dgen 直连权重（不依赖 ComfyUI 节点），
这是本机实测最稳的图生 3D 路径。

## v3.9.5 — install_comfy 收尾与自检（2026-11-28）

- `--verify-only`：不重装，只验证现有 ComfyUI 的节点与能力状态（13 秒出结果）
- `--stubs`：可选装 pytorch3d/nvdiffrast 占位包（排查 import 失败用）
- 收尾打印能力就绪情况（之前被吞掉），并给出下一步命令
- 在**全新解压的发布包**上实测：479 秒装完、ComfyUI 1097 节点、GGUF 可用

## v3.9.4 — ComfyUI 一键安装脚本（2026-11-28）

新增 `tools/install_comfy.py`：把端到端实测里踩过的 ComfyUI 安装步骤固化成一条命令。

```
python tools/install_comfy.py
# venv(D盘) → torch 2.7.1+cu126 → ComfyUI 源码(zip) → requirements(过滤模板包)
# → 3D-Pack/GGUF 节点包 → 39 个实测缺失依赖 → kiui/utils3d → 启动并验证节点
# → 输出五项能力就绪情况
```

实测：479 秒装完，ComfyUI 起在 8188，1097 个节点，GGUF 加载器可用。
关键保护：**覆盖源码时绝不删 `models/`**（之前因此白下 11 GB 权重）。

新增 `tools/make_cuda_stubs.py`：给 `pytorch3d` / `nvdiffrast` 生成占位包，
用于排查"缺编译扩展导致整个节点包 import 失败"的问题。
实测结论：占位包能让部分包 import 通过，但 3D-Pack 在 import 阶段真实调用 pytorch3d，
绕不过去 —— 多视图仍需真正编译 pytorch3d。

## v3.9.3 — 能力就绪检查（2026-11-28）

新增 `tools/capability_doctor.py`：一条命令列出五项能力的状态与补法，
并接进 `tools/doctor.py` 的输出。

端到端实测发现，五项能力失败时的报错各不相同（缺脚本/缺权重/缺 CUDA 扩展/缺基础工程），
日志散落在不同位置，用户根本不知道该补什么。现在：

```
能力就绪：
 ✓ 骨骼绑定   模板骨架（纯 CPU，实测 53 s / 14 关节）
 ✓ 动画      5 个预烘焙动作（52 骨）
 ✗ 贴图 PBR  缺节点：…；缺权重：models/diffusers/hunyuan3d-paintpbr（约 6.4 GB）
     → 装 ComfyUI-Hunyuan3D-Paint 节点包 + 权重
 ✗ 精修      缺权重 shape_transformers_512（4.6 GB）
 ✗ 多视图     缺 2mv 节点（3D-Pack）
```

本轮同时确认：**多视图卡在 `nvdiffrast`**（3D-Pack 的编译型 CUDA 依赖），
与之前的 `pytorch3d` 同类问题——Windows + torch 2.7 下这些扩展需要现场编译。

## v3.9.2 — 动画能力可用（2026-11-28）

- 从仓库备份恢复 5 个**预烘焙动作包**（52 骨 + 具名 Action）：walk 1–25 / run 1–19 /
  bow 1–90 / wave 1–65 / idle 1–25，Blender 逐个验证通过
- `capability_ui.do_motion` 改为**优先交付预烘焙动作包**，不再要求现场重定向
  （干净安装没有基础骨架工程，原先会直接报"缺基础骨架工程"）
- 没有动作包时回退「骨架工程 + BVH 现场重定向」
- 补 2 个测试（动作包优先、缺素材提示）

## v3.9.0 — 端到端实测后的安装级修复（2026-11-28）

在本机从"零模型"完整装了一遍、逐个模型跑通、做了一次模型修改，最后删干净。
实测记录见 `docs/E2E-FULL-REPORT-2026-11-28.md`（5 个图像模型 + 3 条 3D 路线 + 骨骼 + 修改测试）。

**最关键的一条：能力后端脚本进仓库**

骨骼 / 动画 / 精修 / 贴图的执行脚本原先只在被 `.gitignore` 排除的 `work/research/`，
干净安装的用户点「绑骨骼」只会看到「缺研究脚本」。现在源码收进 `tools/backends/`
（权重仍不进仓库），`resolve()` 优先用仓库副本、权重路径可配、缺权重给安装命令。

- `tools/backends/rig/auto_rig.py`（模板骨架，实测 53 s / 14 关节）
- `tools/backends/rig/run_riganything.py`（语义骨架）
- `tools/backends/precision/smoke_direct3d.py` + `flash_attn_compat.py`（V100 的 SDPA 兼容层）
- `tools/backends/motion/retarget_lib.py`（BVH 重定向）
- `tools/backends/texture/run_plane_paint.py`

**模型侧的三个坑**

1. Z-Image 工作流写死 GGUF 节点，干净装没有 GGUF 扩展 → 按后缀选 `UNETLoader`/`CLIPLoader`，`clip_type` 可配
2. `_build_flux2_workflow` 的 LoRA 字段指向不存在的文件 → 400；现在缺 LoRA 自动跳过
3. ComfyUI-GGUF 扩展缺 `gguf` 包 → 整个扩展 import 失败（只在启动日志里能看到）

**安装脚本**

- 镜像优先 + 单源 30 s 超时（github.com 直连现在会超时）
- 自动解析 `releases/latest`，不再写死版本
- 输出改 ASCII（PS 5.1 按 GBK 读 UTF-8 会花屏）

**下载器**

- 去掉稀疏预分配（它让"没下完也像下完了"），改按实际写入字节数校验
- 每 10 s 进度心跳
- 清单大小按 ±5% 容差判，HTML 错误页会被识别成失败

实测网络：同一镜像同一小时，21.7 GB 的 Qwen-Image 跑到 81 MB/s，19.2 GB 的 FLUX.2 只有 29 MB/s——
用户报"下不动"先换时间点重试，别急着判死刑。

## v3.8.9 — 全新安装实测修的四处（2026-11-28）

在本机（模型全删、无 ComfyUI/Ollama）按用户姿势真跑了一遍 `install.ps1`：

**跑通的**：解析到 v3.8.8 → 镜像下载 3.96 MB → 解压 → pip 依赖 → doctor 自动建目录/补配置 → 9/10

**修掉的**

1. **install.ps1 死等 GitHub**：第一源是 github.com，直连超时要等 180 s 才回落。
   现在镜像优先（gh-proxy → ghfast → github），每个源 30 s 超时。
2. **install.ps1 版本写死 v3.8.4**：装完是旧版。现在先解析 `releases/latest` 的 tag，
   解析不到才用 pin。
3. **占位符被报成"目录不存在"**：`config.example.json` 里写 `${COMFYUI_DIR}`，
   doctor 直接判它不存在，新手第一眼以为配错了。现在识别为"还没配置"并给出
   `bootstrap_models.py --only core` 的具体命令。
4. **零模型启动时白折腾**：会先"卸载 Ollama"再"启动 Strata"，两条报错吓人。
   新增 `agent.provider_available()`，provider 用不了就一行日志跳过预热。

另外 `config.example.json` 的默认 provider 从 `strata` 改成 `remote`
（远程 OpenAI 兼容，没配就自动降级规则模板），不再假设装的人有本地大模型。

**测试**：新增 2 个（占位符识别、provider 不可用快速返回），全量 pytest 通过。

## v3.8.8 — 视觉系统 v2 整体重建（2026-11-27）

不再打补丁，重建一套设计系统 `ui/forgeform.css`（creative.css 只留结构基座）：

**token 化**：颜色/间距/圆角/阴影/字阶全部收进 `:root`，深色默认 + 浅色换值；
全站只引用 token，组件不再各写一套颜色。

**"贴图"用 CSS 生成，不加载图片**：
- 双层径向光晕（violet / cyan，饱和度压低）
- 24px 细网格 + 顶部径向 mask（本项目的"工程感"材质）
- 顶部 2px 渐变品牌线

**组件统一**：顶栏毛玻璃、对话气泡（用户侧渐变）、输入框聚焦环、发送/生成按钮
渐变+轻投影、精度档 pill、3D 查看器内发光、右栏卡片、产物库条目（悬停右移 2px）、
滚动条细化；动效只动 `transform/opacity`，不碰 `background-position`。

**图标统一**：侧栏链路进度从 emoji 换成几何字形（◐ 图片 / ◈ 选图 / ◇ 3D / ▤ STL），
与产物库同一组（◇◈✦◐⋔▶）。

**响应式**：>1200 三列 / 1200–860 两列（产物库折到下方）/ <860 单列。

截图：`docs/assets/ui-v2-wide.png`、`ui-v2-mid.png`。

## v3.8.7 — 三列布局 + 左侧产物库 + 界面精简（2026-11-27）

**① 左侧常驻「产物库」**（`backend/asset_ui.py`）
- 紧凑列表：类型色条 + 名称 + 类型/大小/指标（面数·水密·关节·贴图·动作）
- 「选择产物 → 打开」直接进 3D 查看器；「刷新」与接口 `/asset_refresh` `/asset_open`
- 1200px 以下自动折到中间下方，避免三列挤压

**② 界面精简**
- 去掉「南航文创工作台」——首屏不再堆三个折叠面板（模块保留给 CLI/脚本）
- 五项能力工作台从**对话上方**挪到**主区下方**（产物已经进左列，不需要两处入口）
- 右栏「模型详情」收进 `<details>`，默认折叠（原来一大段厂商/量化/许可把侧栏拉到很长）
- 「修改图片」按钮不再竖排换行（原按钮文字被挤成两行）

**③ 装饰统一**
- 产物库图标换成同一组几何字形（◇ 模型 / ◈ 多视图 / ✦ 精修 / ◐ 贴图 / ⋔ 骨骼 / ▶ 动画），
  不再混用 emoji；颜色沿用项目主色 violet，类型色只做细边条，不加投影
- 卡片/分组样式与现有 `--ff-*` 变量、深浅色主题一致

**测试**：新增 2 个（产物库三种渲染、布局含左列且无文创面板），
全量 pytest 通过，`verify.py` 152/152。

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
