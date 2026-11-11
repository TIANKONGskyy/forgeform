# Changelog

本项目在 2026-11 首次开源，之前的开发没有对外发布版本，所以下面只从首个公开版本开始记。

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
