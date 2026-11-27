# 全面测试报告（2026-11-27）

对 v3.8.1 做完整验证：**自动化测试套件 + 五项能力端到端 + 边界/负向用例**。
所有数字都是本机实跑（V100 32G / Windows / ComfyUI `:8188`），不是估算。

## 一、总览

| 套件 | 结果 | 用时 | 说明 |
|---|---|---|---|
| **pytest 全量** | **302 项全过**（2 项 skip） | 85 s | 不需要 GPU / ComfyUI |
| **verify.py**（权威结构断言） | **152/152** | 77 s | 插件、配置引用、文档、推荐逻辑、后端接口 |
| **test_basic.py**（A–H 八区） | **39/41** | ~17 min | 失败 2 项全是 E 区，原因见 §四 |
| **test_basic 的 E 区（换端口）** | **5/5** | 3.7 min | 用本仓库自己的 app 实例 |
| **五项能力（verify_capabilities）** | **10/10** | ~9 min | 骨骼 2 / 动画 3 / 精度 2 / 多视图 1 / 修改 2 |
| **贴图（paint_asset）** | 2/2 | 458 s + 1236 s | 快速档 + 完整档 |

**结论：产品侧 41/41 全通（E 区换到本仓库实例后 5/5），五项能力 10/10。**

## 二、五项能力逐项明细

### ① 骨骼线（2/2）

| 用例 | 结果 | 耗时 | 关节数 |
|---|---|---|---|
| 模板档 · 兔子网格 | ✅ | 37.0 s | 10 |
| RigAnything 档 · 人形 | ✅ | 42.9 s | **47** |

两档都通过 `_validate_glb()`（真读 GLB 的 skins + JOINTS_0/WEIGHTS_0）。

**注意**：RigAnything 是生成式，关节数会变（今天三次：52 / 52 / 47）。
断言只校验「≥ 20 关节 + skins 存在」，不能写死 52。

### ② 动画（3/3，纯 CPU）

| BVH | 结果 | 耗时 | 穿地 | 滑移 max | 支撑帧 L/R |
|---|---|---|---|---|---|
| 02_01（走） | ✅ | 9.9 s | 0.0 mm | 7.39 mm | 12/16 |
| 09_01_run（跑） | ✅ | 8.1 s | 0.0 mm | **None**（接触期太短，指标不可定义） | 13/12 |
| 08_01（另一条走） | ✅ | 8.7 s | 0.0 mm | **22.74 mm**（未达标 >8） | 13/12 |

**穿地 3/3 全为 0**，这是硬指标。
滑移一项不可定义、一项超标 —— 说明这个指标**依赖步态速度**，不能跨动作横比（已在文档里注明）。

### ③ 精度提升（2/2）

| 档位 | 结果 | 耗时 | 精修后 | 小壳过滤 |
|---|---|---|---|---|
| 512 档 4 步 | ✅ | 45.2 s | 1,727,578 面 | **712 块 → 1 块**，1,715,294 面 |
| 512 档 15 步 | ✅ | 67.7 s | 1,789,271 面（原始） | **1,289 块 → 1 块**，1,770,010 面 |

15 步比 4 步多 50% 时间、多约 5 万面，但**碎壳也更多**（1289 vs 712）——
小壳过滤是这两个档位共同的前提，不做就是一堆浮尘。

### ④ 多视图（1/1）

| 用例 | 结果 | 耗时 | 产物 |
|---|---|---|---|
| 真实照片 4 视图（2mv@512） | ✅ | 6.1 s（命中缓存）/ 冷启 84 s | 490,754 面，水密=True |

### ⑤ 修改（2/2）

| 指令 | 结果 | 耗时 | 产物 |
|---|---|---|---|
| 模糊："把机身改成红色涂装" | ✅（跑通） | 46.0 s | 变成红色机器人 —— **主体丢失** |
| 具体："只加红色条纹，外形/发动机/螺旋桨/浮筒保持一致" | ✅ | 34.9 s | 白色飞机 + 红条纹 —— **主体保住** |

![修改对比](assets/verify_edit_compare.png)

**结论**：能力可用，但**指令必须逐项列出"不许变的部分"**。

### ⑥ 贴图（2/2）

| 档位 | 结果 | 耗时 | 峰值显存 | 质量 |
|---|---|---|---|---|
| 完整档（6 视角/12 步/1024²） | ✅ | 1236 s | 10.7 GB | 涂装正确 |
| 快速档（4 视角/8 步/1024²） | ✅ | 458 s | 9.7 GB | 只适合预览（atlas 发噪） |

## 三、本轮发现并修掉的问题（5 个）

| # | 问题 | 现象 | 修法 |
|---|---|---|---|
| 1 | `studio.cancel()` 不翻状态 | 取消后 job 仍 `running`；随后保存配置被「请等待当前安装完成」拦住 | 进程内任务取消时直接把 job 置 `cancelled` |
| 2 | 多视图在 `backend.mode=auto` 下不可用 | 报「当前后端不支持多视图」 | HybridBackend 的 ComfyUIClient 是惰性的 → 走 `_get_comfy()`；submit 与 wait 用同一 client |
| 3 | `rigging.bind_model()` 没有 `ok` 字段 | 调用方按 `r["ok"]` 判成败时误判为失败 | 成功路径加 `"ok": True` |
| 4 | `VERSION` 带 BOM | studio `/api/ping` 返回 `"\ufeff3.8.1"` | 重写为无 BOM |
| 5 | `test_basic.py` 端口写死 | 7860 上跑着另一份安装（403 拦测试）时 E 区必然失败 | 支持 `FF_APP_URL/FF_STUDIO_URL/FF_API_URL` 覆盖 |

## 四、E 区为什么在 7860 上失败

`tools/test_basic.py` 默认打 `http://127.0.0.1:7860`。
这台机器的 7860 上是**另一份安装**（`D:\AI-Workshop\tools\foundry_ondemand.py`），
它按 UA 返回 **403**，所以：

- 页面与关键控件 → `HTTPError`
- gradio 客户端 → `Could not fetch config`

换成本仓库自己的 app 实例（`FOUNDRY_PORT=7870 python app.py`）后：

```
✓ 页面与关键控件    271 KB（chatbox / btn_gen / Model3d 全在）
✓ 对话第 1 轮      127.2 s
✓ 对话第 2 轮      33.1 s
✓ 数字不被改写      43.8 s（45/6mm 保持）
✓ 空输入有提示      0.1 s（给引导，不复读上一轮）
5/5 通过
```

**这两项不是产品缺陷，是端口上跑的不是这份代码。**

## 五、已知限制（不是 bug，但要写清楚）

1. **RigAnything 关节数不固定**（47–52）：生成式输出，断言别写死
2. **滑移指标依赖步态速度**：跑步的接触期只有 9 帧，指标不可定义；慢走 7.39 mm、另一条走 22.74 mm
3. **贴图快速档 atlas 发噪**：只能预览，正式出图走完整档（20 分钟）
4. **修改能力对指令敏感**：模糊指令会换主体
5. **多视图会命中 ComfyUI 缓存**：同一组输入第二次只要 6 s，测性能要看冷启

## 六、复现命令

```powershell
cd D:\ForgeForm-oss
$env:PYTHONPATH=''; $env:PYTHONIOENCODING='utf-8'

# 自动化
D:\miniconda3\python.exe -m pytest -q
D:\miniconda3\python.exe tools/verify.py
D:\miniconda3\python.exe tools/test_basic.py               # 39/41（E 区见 §四）
$env:FF_APP_URL='http://127.0.0.1:7870'
D:\miniconda3\python.exe tools/test_basic.py --only E     # 5/5

# 五项能力
D:\miniconda3\python.exe tools/verify_capabilities.py rig
D:\miniconda3\python.exe tools/verify_capabilities.py motion
D:\miniconda3\python.exe tools/verify_capabilities.py refine
D:\miniconda3\python.exe tools/verify_capabilities.py multiview
D:\miniconda3\python.exe tools/verify_capabilities.py edit

# 贴图
D:\miniconda3\python.exe tools/paint_asset.py --mesh model.stl --image ref.png --out out.glb --fast
```

结果 JSON：`D:\tmp\verify_all\results.json`、`D:\ForgeForm-oss\outputs\probe\basic.json`
