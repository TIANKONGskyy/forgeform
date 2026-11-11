# 铸想 ForgeForm

输入一句话，得到可打印的 STL。

本地跑完整链路：LLM 解析需求 → 出图 → 图生 3D → 修水密 → 导出毫米级 STL。不调云端 API。

<img src="docs/assets/hero.jpg" width="340" align="right" alt="铸想生成的产品图" />

<br clear="right" />

## 能做什么

| 阶段 | 做什么 | 后端 |
|---|---|---|
| 对话 | 需求解析、参数补齐、多轮修改 | 本地 LLM（OpenAI 兼容端点 / Ollama） |
| 出图 | 文生图，1–4 张候选，参考图 i2i、局部重绘 | ComfyUI 或 diffusers 直连 |
| 建模 | 单图生 3D | ComfyUI（Hunyuan3D 2.1）或 hy3dgen 直连 |
| 导出 | GLB → STL：减面、补洞、水密、毫米、居中贴地 | trimesh / pymeshlab / pymeshfix |
| 打印 | 局域网发现拓竹打印机并直发 | bambu-cli（可选） |
| 管理 | 对话组、项目归档、模型库、显存/温度监控 | 内置 |

逆向入口：上传视频抽帧建模、上传 GLB 直接转 STL。

## 实测（Tesla V100 32G，单卡）

| 动作 | 耗时 |
|---|---|
| 出图 1024²，Qwen-Image-2.1，20 步 | ≈ 56 s |
| 出图 1024²，30 步（含中→英翻译） | ≈ 140–170 s |
| 建模 Hunyuan3D 2.1，256 octree | ≈ 160 s |
| 建模 Hunyuan3D 2.1，512 octree | ≈ 8–10 min |
| 建模 Hunyuan3D 2mini 直连，256 octree | 加载 42 s + 推理 13 s |
| 导出 STL（20 万面 → 8 万面） | ≈ 3–6 s |

四个作品的真跑记录（每件含耗时、面数、水密、STL 四视角渲染）：
`docs/showcase/`

## 快速开始

### 方式一：解压后双击（推荐）

1. 下载 [`forgeform-v3.0.0.zip`](https://github.com/TIANKONGskyy/forgeform/releases/latest) 并解压
2. 双击 **`启动.bat`**

它会依次做：检查 Python → 建 `.venv` 并装依赖（3–5 分钟，只一次）→ 首次运行跑配置向导
（探硬件、选方案、装模型）→ 启动服务 → **自动打开浏览器**。

之后每次双击直接进界面（约 10 秒）。窗口就是服务端，关掉窗口即停止。

首次运行向导：

```
硬件：Tesla V100 32G（可用 30） · 内存 128 GB · 磁盘剩 166 GB
已有：ComfyUI(含3D)、本地 diffusers 模型 2 个、hy3dgen、对话服务

 ★ [1] 复用现有 ComfyUI（零下载）        0.0 GB · auto
   [2] 最小可用（SD1.5 + 3D 2mini）       8.2 GB · direct
   [3] 推荐（Z-Image Turbo + 3D 2mini）  30.6 GB · auto
```

### 方式二：命令行

```powershell
git clone https://github.com/TIANKONGskyy/forgeform.git
cd forgeform
pip install -r requirements.txt

python tools/setup.py     # 探测本机硬件与已装框架，推荐方案，回车一键装
python app.py             # → http://127.0.0.1:7860
```

`tools/setup.py` 会先识别显存、内存、磁盘，以及机器上已有的 ComfyUI / Ollama / llama.cpp /
vLLM / 本地 diffusers 模型，然后给出可选方案：

```
硬件：Tesla V100 32G（可用 30） · 内存 128 GB · 磁盘剩 166 GB
已有：ComfyUI(含3D)、本地 diffusers 模型 2 个、hy3dgen、对话服务

 ★ [1] 复用现有 ComfyUI（零下载）        0.0 GB · auto
   [2] 最小可用（SD1.5 + 3D 2mini）       8.2 GB · direct
   [3] 推荐（Z-Image Turbo + 3D 2mini）  30.6 GB · auto
```

也可以 `--dry-run`（只看方案）、`--custom`（自己选模型）、`--profile <名字> --yes`（直接装）。

装完自检：

```powershell
python tools/doctor.py
```

## 硬件要求

- Windows 10/11
- NVIDIA GPU，8 GB 显存起步，16 GB 以上能跑完整链路
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
| `docs/HARDWARE.md` | 实测显存/内存占用、各档硬件推荐 |
| `docs/MODELS.md` | 模型下载地址、放哪、多大 |
| `docs/CONFIG.md` | 配置项与按显存分档的方案 |
| `docs/PLUGINS.md` | 模型插件（YAML） |
| `docs/BACKENDS.md` | 三种后端与实测对比 |
| `docs/ARCHITECTURE.md` | 模块职责与核心机制 |
| `docs/DECISIONS.md` | D1–D10 设计决策记录 |
| `skills/forgeform/SKILL.md` | 给 agent 用的技能文件 |
| `CONTRIBUTING.md` / `SECURITY.md` | 贡献约定 / 安全说明 |

## 测试

```powershell
python -m pytest          # 207 个用例，不需要 GPU
python tools/verify.py    # 151 条结构性断言
```

## 许可证

AGPL-3.0。水密修复链入了 `pymeshselab`（GPL-3.0）和 `pymeshfix`（AGPL-3.0），copyleft 会传染。
用它产出的模型和打印件归使用者，不受 AGPL 约束。

第三方组件清单：[NOTICE](NOTICE)
