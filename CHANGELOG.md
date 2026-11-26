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
