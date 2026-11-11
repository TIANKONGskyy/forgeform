# 设计决策记录

这个文件记"**为什么这么做**"，不是"做了什么"（那个看 [ARCHITECTURE.md](ARCHITECTURE.md)）。
目的是以后要改的时候，先知道当初的约束是什么 —— 避免把一个有原因的设计当成随意写成的东西改掉。

每条都标了时间，因为约束会变。

---

## D1 · 一个对话组 = 一次建模（2026-08）

**决定**：每个对话组绑定一个项目目录，`project.json` 里存 candidates / selected / glb / stl。
切换对话组靠 `gr.State`，不用全局变量。

**为什么**：早期用全局 dict 存"当前选中图"，用户切组后旧图会串到新组里，导出的 STL 是别人的。
Gradio 的 State 是会话隔离的，这是唯一干净的做法。

**代价**：外部（比如运维接口）没法直接切组 —— 必须走"模块级 conv 单例 + 异步任务"。
`_buttons_for_phase` 那种"当前阶段决定按钮"的设计也是同一个约束的产物。

---

## D2 · 字段白名单必须两处同步（2026-09）

**决定**：`project_manager.save_project` 和 `load_project` 各维护一份字段白名单。

**为什么**：不用 `**kwargs` 全存全读，是因为早期用全量序列化时，某个内部字段（`_cache`）
把几 MB 的中间数据写进了 json，重启后加载卡死。

**代价**：新增字段两处都要加，漏一处就是"重启后字段消失"。`candidates` / `selected`
就这么丢过一次，表现为自动建模永远跳过。

---

## D3 · 显存分时，不是共享（2026-08 起，反复强化）

**决定**：对话模型、出图模型、建模模型、视频模型轮流占显存，切换前主动卸载。

**为什么**：V100 32G 上，Strata 125B（29.5 GB）和 ComfyUI 出图（20 GB）同时跑，
两个都掉到 CPU，实测降到 11% CPU 利用率，比单独跑慢 10 倍。

**代价**：生成之后的第一句对话要冷启（约 70 秒）。这是设计，不是卡。

**具体实现**：`gpu_state` 维护占用者 + `pipeline._release_chat_gpu()`（生成前等/停对话）
+ `describer.strata_stop()` + `idle_watchdog`（闲置释放 ComfyUI）。

---

## D4 · 出图提示词必须"重建友好"，不是"好看"（2026-10）

**决定**：提示词层强制加「单体、纯色背景、无植物/道具」，并做否定词清除。

**为什么**：早期生成的图很漂亮（桌面摆拍、有光斑有道具），但喂给 Hunyuan3D 之后
建出来的是一坨带桌子的壳。图生 3D 需要的是"干净的剪影"，不是"好看的摄影"。

**代价**：出的图不如"文艺向"的模型好看，但能喂下游。这个取舍不做，整条链路就是废的。

---

## D5 · 水密闸门要"复检"，不能只信 pymeshlab（2026-10-19）

**决定**：pymeshlab 补完洞后，用 `is_watertight` 再验一遍边界边；不合格再走 pymeshfix。

**为什么**：实测 pymeshlab 报 "holes closed"，结果仍有 **326 条边界边**，
`watertight=False`，切片器当空壳。这个数字是踩出来的。

**代价**：pymeshfix 是 AGPL —— 整个项目的许可证因此变成 AGPL-3.0（见 D9）。

---

## D6 · 用"伪造 ComfyUI 协议"而不是改 pipeline（2026-11）

**决定**：`DirectBackend` 内部起一个 `ThreadingHTTPServer`，返回
`http://127.0.0.1:<port>/view?filename=...`，伪造 ComfyUI 的下载协议。

**为什么**：`pipeline.py` 里只有 9 个方法、25 处调用，全部围绕
`build → submit → wait → get_result → requests.get(url)` 这条链。
改 pipeline 要动 5 处下载逻辑；伪造 URL 一处都不用动。

**代价**：多一个本地端口（只监听 127.0.0.1，随进程退出）。

---

## D7 · 模型是 YAML 插件，不是 if/elif（2026-11）

**决定**：一个模型 = 一个 YAML，`pipeline` 字段写 diffusers 类名，`defaults` 写参数。

**为什么**：模型会一直换代。写死在 `direct.py` 里的后果是每来一个新模型就要改代码、
跑测试、发版。YAML 是数据 —— 加文件即接入，出错只影响那一个模型。

**代价**：参数不兼容的模型需要额外的 `adapter.py`（约定两个函数，还没接）。

---

## D8 · 不自动选 bnb 量化（2026-11）

**决定**：`quant: auto` 只在「原精度放得下 / 放不下走 offload」之间选，**不自动用量化**。
bnb 只保留为显式选项。

**为什么**：实测 bitsandbytes 8bit 量化 `Flux2Transformer2DModel` 之后，出图是**纯噪声** ——
不报错、速度还快 2-3 倍（89 s vs 194 s）。这是最危险的一类 bug。

**代价**：大模型在显存不够时只能 offload（慢），不能靠量化变快。

**配套**：加了**拉普拉斯方差闸门**（正常图 0.002–0.003，噪声 0.041–0.044），
量化坏掉直接报错，不静默存垃圾。

---

## D9 · 许可证 AGPL-3.0，不是 MIT（2026-11）

**决定**：整个项目 AGPL-3.0。

**为什么**：不是偏好。水密修复链入 `pymeshfix`（AGPL-3.0）和 `pymeshlab`（GPL-3.0），
copyleft 会传染。声明 Apache/MIT 在法律上不成立，只是一句没有效力的话。

**代价**：企业用户会绕开。想去掉的话唯一干净做法是删掉这两个依赖（它们是函数内延迟
import + try/except，删了不崩，只是水密率降），然后可以按 MIT 发。

---

## D10 · auto 模式优先复用别人的框架（2026-11）

**决定**：启动时探测 Ollama / ComfyUI / llama.cpp / vLLM / LM Studio / 本地 diffusers，
有就复用，没有才用直连。

**为什么**：重下 20–30 GB 权重是最贵的一步，而用户机器上往往已经有能用的框架。
ComfyUI 那套 Hunyuan3D 权重和 hy3dgen 不兼容（层名、config.yaml 都不一样，见 MODELS.md），
硬要直连就得重下 —— 不如直接用 ComfyUI 跑。

**代价**：启动多花不到 1 秒做探测；判断错的能力（比如 ComfyUI 活着但没权重）要靠
扫目录兜住。

---

## 待办与已知缺口

- **视频直连**：MiniMax H3 只有 ComfyUI 节点实现，没有独立 pipeline。这是硬限制。
- **adapter 机制还没接**：`docs/PLUGINS.md` 里写了约定（`load` / `generate` 两个函数），
  `direct.py` 里还没接。
- **bnb 在别的模型上是否也坏**：只验了 Flux2/Klein。SDXL、Qwen-Image 上可能正常。
- **3D 贴图（texgen）没接**：`hy3dgen` 有 `Hunyuan3DPaintPipeline`，铸想目前只出白模。
- **16 GB 内存机器没实机验证**：`docs/HARDWARE.md` 里的推断基于 32 GB 机器的 RSS 实测，
  16 GB 上是否真的会换页到不可用，需要有人跑一次 `tools/probe_resources.py` 确认。
