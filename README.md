# 铸想 ForgeForm

输入一句话，得到可打印的 STL。

本地跑完整链路：LLM 解析需求 → 出图 → 图生 3D → 修水密 → 导出毫米级 STL。不调云端 API。

**官网 / 在线试用**：[zykt.space/foundry](https://zykt.space/foundry) · 作者：[ZYKT studio](https://zykt.space)

**仓库**：[GitHub](https://github.com/TIANKONGskyy/forgeform) · [Gitee 镜像](https://gitee.com/zyktspace/forgeform)（国内访问更快）

![铸想 ForgeForm](docs/assets/promo/poster.png)

<p align="center">
  <img src="docs/assets/promo/demo_small.gif" width="640" alt="模型 360 度预览" />
</p>

<img src="docs/assets/hero.jpg" width="340" align="right" alt="铸想生成的产品图" />

<br clear="right" />

## 能做什么

| 阶段 | 做什么 | 后端 |
|---|---|---|
| 对话 | 需求解析、参数补齐、多轮修改 | 本地 LLM（OpenAI 兼容端点 / Ollama） |
| 出图 | 文生图，1–4 张候选，参考图 i2i、局部重绘 | ComfyUI 或 diffusers 直连 |
| 建模 | 单图生 3D；**多视图（四张真实照片）生 3D** | ComfyUI（Hunyuan3D 2.1 / V2 2mv）或 hy3dgen 直连 |
| 贴图 | PBR 上色（albedo + metallicRoughness），`tools/paint_asset.py` | ComfyUI（Hunyuan3D Paint PBR 2.1） |
| 骨骼 | 自动绑定（模板骨架 / RigAnything） | Blender headless + 可选 RigAnything |
| 动作 | BVH mocap 重定向（走 / 挥 / 鞠 / 待机） | Blender headless（纯 CPU） |
| 精修 | 粗网格 + 参考图 → 更细网格（可选档） | Direct3D-V3 |
| 导出 | GLB → STL：小壳过滤、减面、补洞、水密、毫米、居中贴地 | trimesh / pymeshlab / pymeshfix |
| 打印 | 局域网发现拓竹打印机并直发 | bambu-cli（可选） |
| 管理 | 对话组、项目归档、模型库、显存/温度监控 | 内置 |

逆向入口：上传视频抽帧建模、上传 GLB 直接转 STL。

**能力工作台（v3.8.3）**：界面里「🛠 能力工作台」折叠面板可直接跑骨骼 / 贴图 / 精修 / 多视图 / 动画，
每页都有 3D 预览、下载、耗时日志；对外接口名固定为 `/cap_rig` `/cap_paint` `/cap_refine`
`/cap_multiview` `/cap_motion`（和命令行走同一套产品入口）。

**实测状态（2026-11-28，V100）**：出图 z_image 21 s / klein 28 s / flux 61 s / qwen 464 s；3D 2mini 69 s / 2.1@512 676 s；骨骼模板 53 s。
贴图 PBR、精修、多视图、动画依赖额外扩展与权重，见 `docs/E2E-FULL-REPORT-2026-11-28.md`。

**一键开始**：双击 `一键开始.ps1`（先体检再启动）；只想要环境诊断就跑 `python tools/doctor.py`。
出问题跑 `python tools/report.py` 生成**已脱敏**诊断包，直接贴 issue。

**成品总览**：`python tools/collect_gallery.py` 把所有生成路线的代表模型收进一个文件夹
（默认 `D:\铸想模型总览`），带二进制体检表（面数/水密/贴图/关节/动画）与三视角预览图。

**多视图怎么用**（v3.8.0 新增，实测比单图更准）：

```python
from backend.pipeline import Pipeline
pipe = Pipeline("config.json")
r = pipe.generate_3d_multiview({
    "front": "正面.jpg", "back": "背面.jpg",
    "left":  "左侧.jpg", "right": "右侧.jpg",
}, quality_name="高质量")
```

拍照只有一条硬要求：**四张同仰角、方位严格 90°**。物体在画布里多大无所谓
（管线自己按 bbox 归一化，实测尺度差 2.2 倍反而分数更高）。
**不要预先抠图**——送透明底/黑底进去会被再抠一遍，实测 0.4872 → 0.3112。

## 实测（Tesla V100 32G，单卡，2026-11-28 从零装一遍）

**出图**（1024²，同一句提示词）

| 模型 | 档位 | 耗时 |
|---|---|---|
| Z-Image Turbo 6B int8（5.8 GB） | 4 步 | **18–21 s** |
| FLUX.2 Klein 4B（7.2 GB） | 4 步 | **28 s** |
| FLUX.2 Dev Q4 GGUF（18.7 GB + 编码器 11.4 GB） | 4 步 | **58 s** |
| FLUX.1-dev fp8（16.4 GB） | 14 步 | **61 s** |
| Qwen-Image fp8（21.2 GB + 编码器 8.7 GB） | 20 步 | **464 s** |

**建模**（hy3dgen 直连）

| 档位 | 耗时 | 面数 | 水密 |
|---|---|---|---|
| 2mini 256 快速 | **69 s** | 147,840 | 否 |
| 2.1 256 快速 | **105 s** | 151,488 | 是 |
| 2.1 512 高质量 | **676 s** | 400,000（上限） | 是 |

**全链路**：一句话 → 出图 18 s → 建模 72 s = **90 s 出 GLB**
（实测产物：610 KB PNG + 1.7 MB GLB，质量闸通过）

**后处理**：骨骼模板 28–53 s / 14 关节（纯 CPU）· 动作包交付即时

四个作品的真跑记录（每件含耗时、面数、水密、STL 四视角渲染）：
`docs/showcase/`

## 快速开始（实测路径：三条命令）

```powershell
# 1. 解压发布包（4 MB）
Expand-Archive forgeform-v3.9.7.zip -DestinationPath D:\ff
cd D:\ff\forgeform-v3.9.7

# 2. 环境自检（补目录、生成 config，14 秒）
python tools\doctor.py --fix

# 3. 装权重（按需选，可只装一个）
python tools\bootstrap_models.py --only direct   # 12 GB / 197 s → 能图生 3D
python tools\bootstrap_models.py --only image    # 11 GB / 485 s → 能文生图

# 4. 启动
python app.py            # 浏览器开 127.0.0.1:7860
```

装完直接说「做个白色陶瓷马克杯」，90 秒出可打印模型。

### 套件一览

| `--only` | 内容 | 体积 | 实测耗时 |
|---|---|---|---|
| `direct` | hy3dgen 2mini（直连建模，最稳） | 12 GB | 197 s |
| `image` | Z-Image Turbo int8 + Qwen3-4B + VAE（含 ComfyUI 环境） | 11 GB | 485 s |
| `core` | ComfyUI 源码 + 3D-Pack + Hunyuan3D 2.1 | 5 GB | 86 s |
| `paint` / `mv` / `refine` / `rig` | 贴图 / 多视图 / 精修 / 语义骨骼权重 | 4–10 GB | — |

也可以双击 `启动.bat` 走图形向导（会探硬件、给方案）。

## 能力就绪状态（2026-11-28 实测）

| 能力 | 状态 | 说明 |
|---|---|---|
| 文生图 / 图生图 | ✅ | 5 个模型全部跑通 |
| 图生 3D（单图 / 直连） | ✅ | 2mini 69 s、2.1 105 s |
| 图生 3D（512 高精度） | ✅ | 676 s、40 万面、水密 |
| 骨骼绑定（模板） | ✅ | 28–53 s、14 关节、纯 CPU |
| 动作 / 动画 | ✅ | 5 个预烘焙动作包（52 骨） |
| 贴图 PBR | ⛔ | 光栅化内核需与 torch 版本匹配（需重编） |
| 精修 Direct3D | ⛔ | 需 Direct3D 源码 + third_party CUDA 扩展 |
| 多视图 2mv | ⛔ | 3D-Pack 需要编译 pytorch3d（Windows 无轮子） |

一条命令看自己机器上的状态：

```powershell
python tools\capability_doctor.py
```

## 硬件要求

- Windows 10/11
- NVIDIA GPU，**6 GB 显存**起步（2mini 256oct 峰值 3.9 GB，512oct 5.35 GB）
- 内存 16 GB 也能跑通（低内存机器自动启用逐层搬显存 + fp16 构建，见 docs/MODEL-BENCH.md）
- 内存 32 GB 建议（直连 3D 时模型自身占 10.8 GB）
- Python 3.11–3.13
- 一个本地 LLM 端点（可选，没有则降级成规则模式）

各显存档位能跑什么、实测占用多少：`docs/HARDWARE.md`

## 模型

所有模型按 `config.json` 里写的**文件名**从 ComfyUI 目录找，或用 `path` 指本地 diffusers 目录。
每个权重下哪个、放哪、多大：`docs/MODELS.md`。

换模型 = 改一个 YAML（`plugins/image/*.yaml`），不改代码：`docs/PLUGINS.md`。

## 后端

三种模式，改 `config.json → backend.mode`：

| mode | 行为 |
|---|---|
| `comfyui` | 只用 ComfyUI（默认） |
| `direct` | 只用 diffusers / hy3dgen，不依赖 ComfyUI |
| `auto` | 启动时探测本机有什么，按阶段自动路由 |

细节与实测数据：`docs/BACKENDS.md`

## 文档

| 文件 | 内容 |
|---|---|
| `docs/HARDWARE.md` | 实测显存/内存占用、各档配置对照 |
| `docs/MODELS.md` | 模型下载地址、放哪、多大 |
| `docs/CONFIG.md` | 配置项与按显存分档的方案 |
| `docs/PLUGINS.md` | 模型插件（YAML） |
| `docs/BACKENDS.md` | 三种后端与实测对比 |
| `docs/MODEL-BENCH.md` | 各模型在同一提示词下的实测数据 |
| `docs/EXTENSIBILITY.md` | 扩展点：换模型、给 agent 用、加 stage |
| `docs/ARCHITECTURE.md` | 模块职责与核心机制 |
| `docs/DECISIONS.md` | D1–D10 设计决策记录 |
| [`docs/RESEARCH-MASTER-2026-11-26.md`](docs/RESEARCH-MASTER-2026-11-26.md) | 六方向实测总表（贴图 / 骨骼 / 运动 / 精度 / 多视图 / 低显存） |
| [`docs/RESEARCH-BRIEF-6DIRECTIONS-2026-11-26.md`](docs/RESEARCH-BRIEF-6DIRECTIONS-2026-11-26.md) | 本机环境事实与踩坑清单（V100 没有 OpenGL、SDPA 只有 EFFICIENT 等） |
| [`docs/MERGE-CODEX-2026-11-26.md`](docs/MERGE-CODEX-2026-11-26.md) | v3.8.0 合并了什么、拒了什么、为什么 |
| `skills/forgeform/SKILL.md` | 给 agent 用的技能文件 |
| `CONTRIBUTING.md` / `SECURITY.md` | 贡献约定 / 安全说明 |

## 测试

| 命令 | 内容 |
|---|---|
| `pytest` | 233 个用例，不需要 GPU |
| `verify.py` | 152 条结构性断言 |
| `healthcheck.py` | 12 项全链路自检（含上面两项） |
| `e2e_full.py` | 真跑一遍全流程，约 5 分钟 |

`healthcheck.py` 是最省事的入口 —— 它会检查插件、配置引用、入口文件、文档、
推荐逻辑、后端接口、更新器，最后再跑 pytest 和 verify。

## 许可证

AGPL-3.0。水密修复链入了 `pymeshselab`（GPL-3.0）和 `pymeshfix`（AGPL-3.0），copyleft 会传染。
用它产出的模型和打印件归使用者，不受 AGPL 约束。

第三方组件清单：[NOTICE](NOTICE)

## 三个新入口（v3.5.0）

**本机能力 API** —— 把出图/建模/STL/对话开给别的程序调：

```bash
PYTHONPATH= python tools/api_key.py issue --label 我   # 签发一把 ff_ key
PYTHONPATH= python tools/api_server.py                # 127.0.0.1:7863
# 文档 http://127.0.0.1:7863/docs   规范 docs/API-SPEC.md
```

**云端 API** —— 扩写/出图/建模可以接云端（本地没装权重也能用），key 用
Windows DPAPI 加密只落本机：

```
config.json → cloud.enhancer / cloud.image / cloud.mesh
Studio 的 /api/cloud 读写，或用 GET /api/cloud 看缺什么
```

**运行信息留存** —— GPU/内存/CPU/磁盘每 3 分钟一行，`logs/metrics-*.jsonl`
滚动 14 天；`outputs/` 中间产物 30 天可清，`projects/` 作品永不清理：

```bash
PYTHONPATH= python tools/calibrate.py --summary   # 看本机实测与占用
```

**API 密钥安全** —— 明文只在生成时显示一次，库里只存 sha256，且整个库用本机
DPAPI 加密（拷到别的机器解不开）；每把有独立 id、可选有效期（默认 90 天）、
可撤销可删除，每次调用写 `logs/api-*.jsonl` 审计（只记 key id 与 IP，不记 token）。
界面在「模型与配置 → API 密钥」，命令行 `tools/api_key.py`。

## 装不上？先跑自检

```bash
PYTHONPATH= python tools/doctor.py            # 环境 8 项 + 网络 3 类，逐项给修法
PYTHONPATH= python tools/doctor.py --refresh  # 忽略缓存重测网络
```

它当场测 PyPI / GitHub / HF，各挑一个**实测最快**的源（不写死阿里云/清华），
并把缓存写进 `logs/net-cache.json`（1 小时）。35 条常见问题的逐条解法见
[docs/NETWORK-AND-ENV.md](docs/NETWORK-AND-ENV.md)。

## 下载后怎么最快用上

双击 `启动.bat` → Studio 打开 → 顶部「一键开始」那张卡会显示这台机器
**现在能直接用什么**（对话 / 出图 / 建模 三条链分别标 ✓ 或 ✗），
再点「开始用」，它自己挑最快路径并写好配置：

1. **复用已有 ComfyUI / hy3dgen / diffusers** —— 0 下载（装过 AI 工具的人走这条）
2. **云端 API** —— 0 下载（填一次 key）
3. **下载最小方案** —— 约 10 GB，界面在下载期间已经能用
4. **只开界面** —— 什么都跑不动时也不白屏

出厂配置的默认模型可能指向没下载的权重，`backend/firstrun.py` 会自动把默认
换成这台机器上**真有文件**的那个（例如 2.1 → 2mini），不用人排查。
