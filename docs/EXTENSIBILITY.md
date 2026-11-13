# 扩展点

这份文档写给「以后要往铸想里加东西的人」——包括 ZYKT agent。
目标是把**改哪里、不用改哪里**说清楚，避免每次都从头读一遍 1200 行的 `app.py`。

---

## 一句话架构

```
前端（Gradio app.py / Studio ui/）
        ↓ 调方法
Pipeline（backend/pipeline.py）—— 编排：对话 → 出图 → 建模 → STL
        ↓ 调后端
Backend（backend/backends/）—— comfyui / direct / auto 三选一
        ↓ 读插件
Plugins（plugins/{image,mesh}/*.yaml）—— 一个模型一个 YAML
```

**扩展点按"改动成本"从低到高**：

| 想做的事 | 改哪 | 要动代码吗 |
|---|---|---|
| 换模型 | 加/改一个 `plugins/*/*.yaml` | 不用 |
| 调默认参数 | `config.json → models/stages` | 不用 |
| 加生成能力（比如纹理） | 新建一个 stage + backend 方法 | 要 |
| 换整个后端 | 实现 `backend/backends/` 里的接口 | 要 |
| 给 agent 用 | 走 `Studio` 的 HTTP API 或 `Pipeline` 的方法 | 不用 |

---

## 1. 换模型：只写 YAML

`plugins/image/<id>.yaml`：

```yaml
id: my_model
kind: image
label: 显示名
pipeline: SomeDiffusersPipeline    # diffusers 的类名
repo: org/model-repo               # HF repo，下载用
vram_hint: 12                      # 估算显存
quant: auto                        # auto | none | fp8 | bnb8 | bnb4
defaults:
  steps: 20
  guidance: 3.5
  width: 1024
  height: 1024
```

`plugins/mesh/<id>.yaml`：

```yaml
id: my_mesh
kind: mesh
label: 显示名
loader: package.module.ClassName    # 加载入口的完整路径
repo: org/model-repo
vram_hint: 16
defaults:
  subfolder: hunyuan3d-dit-v2-mini  # 权重在 repo 里的子目录
  steps: 30
  octree: 256
```

**规则**（`backend/plugins/spec.py` 会校验）：
- `image` 必须有 `pipeline`，`mesh` 必须有 `loader`
- `kind` 只能是 `image` / `mesh`
- 写完不需要重启 —— `registry(reload=True)` 会重读

**验证**：`python -c "from backend.plugins.spec import registry; print([p.id for p in registry(reload=True).of_kind('mesh')])"`

---

## 2. 给 agent 用：两条路

### A. HTTP API（推荐给 ZYKT agent）

`backend/studio.py` 起一个 stdlib HTTP 服务（默认 :7861），所有写操作要 `X-Studio-Token`。

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/status` | GET | 当前作业状态、进度、日志 |
| `/api/catalog` | GET | 模型目录（含已装/未装、显存需求） |
| `/api/scan` | GET | 扫本机已有的推理框架与模型 |
| `/api/purge-plan` | GET | 删之前先看会删什么、多大 |
| `/api/install` | POST | 安装方案（异步，轮询 `/api/status`） |
| `/api/launch` | POST | 起主服务 |
| `/api/remove` | POST | 删单个模型 |
| `/api/purge` | POST | 删铸想自己的数据 |

**约定**：
- 长任务一律 `POST` 提交 → 返回 job → 轮询 `/api/status`
- 所有路径参数只传 **id**，不接受绝对路径（防越权删除）
- 删除只动 `models/ outputs/ logs/ temp/ projects/ config.json .venv`，
  ComfyUI 等外部资产**永远不动**（`_under_deletable()` 把关）

### B. Python 方法（进程内）

```python
from backend.pipeline import Pipeline
pipe = Pipeline("config.json")

pipe.enhance_prompt(text)                    # LLM 扩写
pipe.generate_images(text, model_key=..., quality_name=...)   # 出图
pipe.generate_3d(model_key=..., image_path=...)               # 建模
pipe.export_stl(glb_path, target_size_mm=80)                  # 导出
```

每一步都返回 `StageResult(success, output_path, error)`，**不抛异常** ——
agent 直接看 `success` 判断，不用 try/except。

---

## 3. 加生成能力（新 stage）

改三处：

1. **`backend/backends/direct.py`** —— 加 `_run_<stage>(params)`，返回产物路径列表
2. **`backend/backends/direct.py: submit_workflow`** —— 在 `stage_name` 分支里接上新方法
3. **`backend/pipeline.py`** —— 加 `generate_<stage>()`，参照 `generate_3d` 的写法

**必须同时改的地方**（漏了会静默失效）：
- `build_workflow` 收到 `params` 后要能拿到 `model_key`（否则落到 `stages.default_model`）
- 传图给直连后端要用 `params["image_path"]`（本地绝对路径），
  传 ComfyUI 用 `params["input_image"]`（上传后的文件名）
- `get_result_<stage>()` 返回什么：**图像要 URL**（`requests.get` 下载），
  **3D 要本地路径**（`shutil.copy2` 复制）

---

## 4. 换后端

`backend/backends/` 里的后端要实现这组方法（`DirectBackend` 是最小参考）：

```python
build_workflow(stage, params) -> dict
submit_workflow(workflow, stage=...) -> job_id
wait_for_completion(job_id, timeout=...) -> {"outputs": {...}}
get_result_image(outputs) -> url
get_result_3d(outputs) -> local_path
upload_image(path, stage=...) -> name
```

`HybridBackend` 负责按阶段路由（`image → comfyui`、`mesh → direct`），
路由结果写在 `self.routes` 里，`/api/status` 能看到。

---

## 5. 低配机器：三个必须保留的开关

这三个是 16 GB 内存笔记本能跑建模的唯一原因，改加载逻辑时**不要删**：

```python
patch_safetensors()           # 权重逐层搬到显存（内存 10.8G → 4.2G）
torch.set_default_device("cuda")   # 模型结构在显存里构建
torch.set_default_dtype(torch.float16)  # 构建时就是半精度（显存 7.15G → 3.88G）
```

位置：`backend/backends/direct.py` 的 `_run_3d → build()`。
**只在 `available < 20 GB` 时启用**，大内存机器走原路径。

实测数据见 `docs/MODEL-BENCH.md`。

---

## 6. 后处理链

3D 产物出厂前依次经过：

```
trim_base（切底部背景板）→ 质量闸（连通块/面数/薄片判定）→ 不合格则兜底重建
```

- `trim_base`：`tools/trim_base.py`，纯几何判断（底部尖峰检测）
- 质量闸：`backend/pipeline.py → _finalize_3d`
- 修水密：`backend/mesh_optimizer._repair`（pymeshlab + pymeshfix 双保险）

**加新的后处理**：接在 `_finalize_3d` 开头、质量闸之前；失败要静默降级（不能阻断出模）。

---

## 7. 实时硬件监控

`backend/monitor.py` 的 `HardwareMonitor` 采集 CPU/GPU/内存/显存/温度/功耗，
**自带滚动历史**（`cpu_hist` / `vram_hist` / `gpu_temp_hist` 等）。

- **API**：`GET /api/hardware`（`backend/studio.py → Studio.hardware()`）
- **UI**：overview 页「本机状态」四张卡片 + sparkline，每 2 秒刷新
- **生命周期**：`page('overview')` 调 `hwStart()`，切走 `hwStop()` —— 别的页面不轮询

采集失败返回 `{ok: false, error}`，界面显示"采集失败：…"，**不会**静默成空白。

**加新指标**：在 `monitor.get_stats()` 里加字段，前端照 `hw-card` 的结构复制一块。
历史序列要自己维护（`_history` 列表 + `maxlen`），否则 sparkline 只有一根竖线。

> ⚠️ 前端踩过的坑：`const HW = {...}` 在文件末尾，而 `hwStart()` 被放在第 7 行调用
> → **TDZ（暂时性死区）**，`ReferenceError: Cannot access 'HW' before initialization`，
> 整行之后的初始化全废。JS 里 `const` / `let` **不提升**（函数声明才提升），
> 把启动调用放在文件最末尾最安全。

## 8. 已知边界（别当 bug 报）

| 现象 | 原因 |
|---|---|
| 机械对称件建模出来是薄片 | 单视图推不出深度，2mini 的固有限制 |
| TRELLIS.2 选不了 | runtime adapter 未接入，`pick_mesh` 里已排除 |
| 8 GB 显存跑不了 2.1 | 权重合计 9.59 GB，物理装不下 |
| `flux2_dev` 出图失败 | 权重 32.31 GB > 可用显存 |
| 笔记本 16 GB 内存跑直连建模会 OOM | 模型构建（VAE+DINO）还要 6-8 GB |

---

## 8. 改完怎么验

```powershell
python tools/healthcheck.py      # 结构 + 逻辑 + 测试，一次pass完
python tools/e2e_full.py         # 真跑一遍全流程（5 分钟）
python -m pytest                 # 单元测试
python tools/verify.py           # 结构性断言
```

**加新功能后至少跑 healthcheck** —— 它会检查接口名、文档存在性、配置引用一致性，
这些都是"改名后忘了同步"的高发区。
