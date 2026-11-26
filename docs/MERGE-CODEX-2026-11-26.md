# 合并记录：Codex 线 × 本仓库线（2026-11-26）

两条线并行做了同一件事——把「图生 3D」做准。Codex 在 `D:\AI-Workshop`（工作副本），
本仓库在 `D:\ForgeForm-oss`（开源仓库）。这次把两边**有用的、验证过的**合并进开源版。

## 一、合并了什么

| 模块 | 来源 | 合并方式 | 状态 |
|---|---|---|---|
| `backend/view_classifier.py` | Codex | 直接并入 + 修一处静默失败 + 模型/端口可配 | ✅ 有测试 |
| `backend/multiview_prompt.py` | Codex | 并入 + 强制 2mv 模式 + 补文档 | ✅ 有测试 |
| `backend/rigging.py` | Codex | **重写后端**（UniRig → 模板/RigAnything），保留其守门逻辑 | ✅ 有测试 |
| `Pipeline.generate_3d_multiview()` | 本仓库 | 新增 | ✅ 有测试 |
| 小壳过滤（`_remove_debris` / `_drop_debris`） | 本仓库 | 新增 | ✅ 有测试 |
| `tools/asset_refine_rig.py` | 本仓库 | 新增（精修 + 绑定封装） | ✅ 手工验证 |

## 二、为什么拒了 Codex 的某些部分

**① `rigging.py` 的 UniRig 后端**

原实现只认 `runtime/UniRig` + `runtime/unirig_env`。实测（读源码 + 本机验证）：

- `src/model/pointcept/models/PTv3Object.py` 硬 `import flash_attn` 且 `assert flash_attn is not None`
- 用的是 FA2 的 `flash_attn_varlen_qkvpacked_func`；**FA2 只支持 sm80+，V100(sm70) 无轮子**
- 另外还要 `torch_scatter`，`validate_cast='bfloat16'`（V100 无原生 bf16）

要跑得改 3 处、约 0.5–1 天，且 SDPA 与 FA 数值不等价、须对拍。
**所以保留 Codex 的守门逻辑（锁 / GPU 检查 / GLB 二进制校验），把执行器换成我们验证过的那两条。**

**② Codex 那版 `multiview_prompt.generate()` 直接吃用户图，没有"白底原图"约束**

实测结论（`docs/RESEARCH-MASTER-2026-11-26.md`）：送**预先抠好的透明底/黑底**进去会被
`Multi Background Remover` 再抠一遍，抠坏 mask，分数从 0.4872 掉到 0.3112。
所以文档里写死规则：**喂白底原图，不预遮罩、不外部归一化**；只有用户本来就有干净 alpha 时，
才用 `JoinImageWithAlpha` 把通道接回去（Codex 这套判断写对了，保留）。

## 三、合并时发现并修掉的两个坑

**① `generation_mode` 是单视图的**

config 里 `models.3d.*.generation_mode` 可能是 `Hunyuan3D-2-Fast`（单视图）。
原样传给 MV 节点 → 加载单视图管线 → 四视图 dict 进去后预处理器对 `None` 调 `cv2.resize`，
报 `src is not a numpy array`，60 秒内必失败。

修法：`generate_3d_multiview` 与 `multiview_prompt._mv_mode` 都强制纠正成 `Hunyuan3D-2mv-*`。

**② 建模前 `/free` 会卸载抠图模型**

`_free_vram()` 打 ComfyUI `/free`，把 birefnet 一起卸了，`Multi_Background_Remover` 返回 None，
下游同样崩。**单图路径需要让显存，多视图路径不需要** → 多视图不再调 `_free_vram()`。

## 四、合并后的实测基线（本机 V100 32G）

| 路径 | 耗时 | 显存 | 结果 |
|---|---|---|---|
| 单图 2.1@512（侧视） | 828 s | 15.1 GB | IoU 0.4709 |
| **多视图 2mv@512（白底原图）** | **84 s** | 32 GB 独占 | **IoU 0.4872**，490,754 面，水密 |
| 精修 Direct3D-V3 512/4 步 | 32.5 s | 5.8 GiB | 水密，1.73 M 面 |
| 贴图 Paint PBR 2.1 | 216 s | 13.7 GB | 色相 σ 6.46° |
| 绑定 模板 | 8–68 s | 0 | 14–21 骨，0 无权重顶点 |
| 绑定 RigAnything | 52 s | 4.7 GB | 52 骨（含手指），撕裂 0.061% |
| 运动 BVH 重定向 | 37 s（CPU） | 0 | 穿地 0.000 mm |

## 五、没合并的（以及原因）

| 东西 | 原因 |
|---|---|
| Codex 的 106 个未提交改动（`backend/agent.py`、`conversation.py`、`describer.py` 等） | 属于他正在迭代的对话/描述逻辑，与本次 3D 合并目标无关；直接搬会带入未验证行为 |
| `auto_bambu.py` / `auto_bambu_slicer.py` | 打印业务脚本，OS 侧已有 `tools/` 里的实现，重复 |
| `finetune/` 下的模型权重与 venv | 体积大（>1 GB），不进仓库 |
| `PartCrafter` 自动分件 | 实测失败：4 个部件输出含 14/67/41/34 个碎块，没能可靠拆出机身/机翼/桨叶 |
| UltraShape 1.0 | 需 BF16 + FlashAttention + cubvh，V100 上不可行 |
| 三视图轮廓相交 | 2D 轮廓分数接近满分（0.98–1.0），3D 却是 19 个连通块的碎片——**这个指标不能用来选模型** |
