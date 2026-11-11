# 架构

## 一句话

一个 Gradio 应用（`app.py`）把**对话**和**生成**缝在一起，对话由本地 LLM 驱动、生成由 ComfyUI 驱动，`backend/` 负责把「一句话」翻译成「一条可执行流水线」，最后产出可打印的 STL。

## 数据流

```
用户输入
   │
   ├─► intent_classifier / agent ──► 动作（set_requirement / set_param / generate_image / generate_3d / export_stl ...）
   │                                        │
   │                                        ▼
   │                                 conversation（对话组状态机，gr.State 里一份，磁盘上一份）
   │                                        │
   │                                        ▼
   │                                  pipeline（编排）
   │                                   ├─ comfy_client ──► ComfyUI HTTP /prompt + WS 进度
   │                                   ├─ prompt_enhancer ──► 本地 LLM 扩写
   │                                   ├─ mesh_optimizer ──► 减面 / 补洞 / 水密
   │                                   └─ stl_convert ──► GLB→STL（毫米、贴地）
   │                                        │
   ▼                                        ▼
Gradio 组件 ◄── _update_ui ◄── 产物路径 + 进度 + 对话文本
```

## 模块职责

### 入口

| 文件 | 职责 |
|---|---|
| `app.py` | Gradio Blocks 构建、按钮回调、自定义 FastAPI 路由（模型库、运维接口）、launch |
| `tools/verify.py` | 权威验证入口：结构契约 + 行为 + 测试套件 |

### 对话层

| 文件 | 职责 |
|---|---|
| `backend/conversation.py` | 对话组状态机：当前 phase、候选图、选中图、模型路径、STL 路径；按钮显隐策略；自动建模 Timer |
| `backend/agent.py` | 把用户这句话翻译成动作 JSON（本地 LLM + 规则兜底）。决定「该生成还是该追问」 |
| `backend/intent_classifier.py` | 纯规则意图判断，不依赖 LLM，用于降级和快速路径 |
| `backend/describer.py` | 需求解析、提示词生成、对话模型启停、显存让出 |
| `backend/identity_guard.py` | 可识别特征检测（明星/品牌/IP 类主体），避免生成侵权内容 |
| `backend/web_ref.py` | 联网找参考图 → 抠主体 → 白底 |

### 生成层

| 文件 | 职责 |
|---|---|
| `backend/pipeline.py` | 编排器：出图 / 视频 / 建模 / 导出，持有 `state` 与 `config` |
| `backend/comfy_client.py` | ComfyUI HTTP 客户端，各模型的工作流 JSON 组装与提交 |
| `backend/stages/*` | 可插拔的环节实现（base / image / video / threed / stl_export） |
| `backend/prompt_enhancer.py` | 短提示词 → 图像/3D 提示词；直连 llama-server 的 OpenAI 兼容端点 |
| `backend/batch_queue.py` | 批量队列：多个提示词顺序跑完「图→3D→STL」 |

### 产物层

| 文件 | 职责 |
|---|---|
| `backend/mesh_optimizer.py` | 减面、补洞、水密闸门（pymeshlab → `is_watertight` 复检 → pymeshfix） |
| `backend/stl_convert.py` | GLB→STL，毫米单位、居中、贴地 |
| `backend/render3d.py` | GLB 渲染成预览图，贴进对话 |
| `backend/viewer_compat.py` | 提供带稳定 WebGL 后端的 3D 预览页 |
| `backend/model_library.py` | 模型的发布 / 浏览 / 下载，写接口要 token |

### 运行支撑

| 文件 | 职责 |
|---|---|
| `backend/env.py` | **全仓库唯一**允许出现安装路径的地方；`config.json` 的 `paths.*` 在这里展开 |
| `backend/gpu_state.py` | GPU 互斥状态：对话模型与 ComfyUI 不能同时占显存 |
| `backend/idle_watchdog.py` | 闲置若干分钟后把 ComfyUI 的显存还回去 |
| `backend/startup.py` | 自检与自愈：目录、端口、服务、依赖、GPU、文件库 |
| `backend/repair.py` | 一键修复：诊断 + 试图自己修 |
| `backend/monitor.py` | 显存 / 温度 / 功耗 / 利用率采集 |
| `backend/timings.py` | 各阶段耗时估算（来自实测，用于进度条） |
| `backend/progress_tracker.py` | 通过 ComfyUI 的 WebSocket 拿逐步进度 |
| `backend/project_manager.py` | 对话组文件夹的创建 / 读取 / 归档 |
| `backend/file_library.py` | 输入/输出目录扫描，实时文件列表 |
| `backend/logger.py` | 结构化日志（文件 + 控制台） |
| `backend/bambu_print.py` | 拓竹打印机局域网发现 + 直发（可选依赖） |

## 三个必须理解的机制

### 1. 对话组 = 一次建模

一个对话组就是一个项目目录（`projects/<folder>/`），里面存 `project.json`、候选图、选中的图、GLB、STL。

切换对话组靠 `gr.State`。这意味着**外部无法直接切组** —— 从外面调接口切组必然串号。所以运维接口（重出图 / 重建模）走的是「异步任务 + 模块级 conv 单例」，不是直接改 state。

`project_manager.save_project` / `load_project` 各有一份**字段白名单**。新增状态字段两处都要加，否则重启就丢。

### 2. 按钮显隐 = phase 的函数

`_buttons_for_phase(phase)` 决定这一阶段哪些按钮可见。关键坑：函数末尾是 `return with_stl(off)`（全部关闭），新增 phase 如果没补分支，用户看到的就是「核心功能全没了」。

`generating` 和 `3d` 这两个阶段补了「没有产物也要给重试按钮」的分支。

### 3. 显存是分时的，不是共享的

同一张卡上要轮流住：对话模型（十几到几十 GB）、出图模型、建模模型、视频模型。

- `gpu_state` 维护当前占用者，`is_chatting()` / `wait_chat_idle()` 用来避免在对话回答中途抢显存
- `pipeline._release_chat_gpu()`：生成前先停对话大脑；如果对话正在回答，等最多 30 秒，还忙就拒绝这次生成（不打断回答）
- `describer.strata_stop()`：真的把对话模型卸载掉，下次说话再冷启（冷启约 70 秒，所以生成后的第一句话会慢一拍）
- `idle_watchdog`：闲置若干分钟主动释放 ComfyUI

这个设计不是「优雅」，是**必须**。V100 32G 装不下两个大模型，硬撑的结果是两个都掉到 CPU 上跑。

## 测试策略

`tests/` 里 172 个用例不碰 GPU、不连 ComfyUI、不加载模型。它们守的是**流程契约**：

- 按钮显隐与 phase 同步
- `_update_ui` 返回值元数 = `ui_outputs` 长度
- 自动建模不会重复触发
- 跨对话组不串号（STL、参考图、候选图）
- 提示词里的数字不被 LLM 改写
- 水密闸门的判定顺序
- 路径不出现本机绝对路径

**为什么不做端到端 GPU 测试**：一次真跑要几十秒到十几分钟，CI 跑不起；而且这些 bug 的表现是「点了没反应」，端到端也只能看到「没反应」，看不到根因。

验证要驱动真实入口（`app.build()`、`app.on_submit`）而不是只断言源码字符串 —— 字符串能过，`NameError` 还是会炸。

## 扩展点

- **加一个生图模型**：`config.json → models.image.<key>` 加一段，`comfy_client` 里加工作流组装函数，`stages/image.py` 里按 `backend` 分发。
- **加一个 3D 后端**：`models.3d.<key>` + `stages/threed.py` 的分发 + 产物落盘约定（GLB 路径要能被 `stl_convert` 吃掉）。
- **加一个阶段**：`stages/base.py` 子类 + `_buttons_for_phase` 补分支 + `ui_outputs` 扩一个 + 测试。
