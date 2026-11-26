# 图生3D 六个方向的现状与可突破点（2026-11-26）

本机：V100 32G（sm70）/ 128G RAM / ComfyUI `:8188` / 铸想 `D:\ForgeForm-oss`（`D:\miniconda3\python.exe`）

## 一、贴图（Texture / PBR）

**本机已有权重**（3D-Pack 节点可直接调）：

| 模型 | 路径 |
|---|---|
| Hunyuan3D Paint v2.0 | `ComfyUI-3D-Pack-main\Checkpoints\Diffusers\tencent\Hunyuan3D-2\hunyuan3d-paint-v2-0` |
| Paint v2.0-turbo | 同目录 `hunyuan3d-paint-v2-0-turbo` |
| **Paint PBR v2.1** | `...\tencent\Hunyuan3D-2_1\hunyuan3d-paintpbr-v2-1`（model.safetensors 1206 MB）|
| Delight v2.0 | `...\Hunyuan3D-2\hunyuan3d-delight-v2-0` |

**已知缺陷**：Codex 上一版贴图在腿部/靴面出现青绿色块 = UV 岛边界溢色（texture bleeding）。修法：UV 岛掩膜 + atlas dilation（扩边 8–16 px），或改用多视角投影烘焙。

**业界路线**（[Hunyuan3D 2.0 论文](https://arxiv.org/html/2501.12202v1)）：2D diffusion 生成多视角着色 → 反投影烘焙 → 接缝优化。开源可用的还有 MVPaint / SyncMVD，但都要额外权重。

## 二、骨骼线（Auto-Rigging）

**本机没有 rig 模型**，hy3dgen 目录下无 skeleton 权重，3D-Pack 无骨骼节点。

**开源候选**：

| 方案 | 出处 | 门槛 |
|---|---|---|
| **UniRig** | [VAST-AI-Research/UniRig](https://relatedrepos.com/gh/VAST-AI-Research/UniRig)（清华+VAST，2025） | 需下载权重（~1 GB），依赖带 flash-attn 的可选路径 |
| **RigAnything** | [Isabella98Liu/RigAnything](https://relatedrepos.com/gh/Isabella98Liu/RigAnything) | 骨骼序列自回归，权重较大 |
| Blender 自动权重 + 模板骨架 | 原生 | 零下载，heat map 权重对薄壳/多件网格不稳 |

**已经验证可行的**：Codex 那套 52 骨人形骨架 + Blender 手动权重（`人物骨骼_四种动作.blend`），腿骨超长 6.4%、步态支撑期只有 3–5 帧（我修成 11–12 帧）。

### 实测补充（2026-11-26，详见 `work/research/rig/RESULT.md`）

**已经做出来的**：`work/research/rig/auto_rig.py` —— 模板骨架（humanoid / quadruped / aircraft / chain 自采样链）+ Blender 热扩散自动权重 + 伪骨清理 + 质量闸门 `rig_gate.py`。
任意水密网格 28–68 s 出「带 skin 的 GLB + 骨骼线 PNG」，**无权重顶点 = 0**，glTF 合规（`JOINTS_0`/`WEIGHTS_0`/`inverseBindMatrices` 全在）。
实测：兔 chain p99 1.05 / 撕裂 0.13%；飞机 p99 1.08 / 0.04%；人形（迈步姿势）模板法 4.38% → 清伪骨后 **0.59%**；同一份人形换 chain **0.00%**。

**天花板不在算法，在姿势**：模板法只对中性姿势成立。坐着的兔子用四足模板 11/19 根骨在体外；迈步的人形用对称腿骨会落在两腿之间的空气里。
根因实测：`upper_arm.R` 在迈步人形上抢到 **45% 的顶点**（手臂骨被拉回体内后，热扩散合理地把它判给了躯干顶点），一摆臂就把半个身子拖走 → 局部拉伸 12.2% 的边。
所以管线里必须有「闸门 + 回退 chain」这条兜底链，而不是迷信模板。

**两个会影响架构的坑（都在 Windows + 本机环境实测）**：

1. **`import bpy` 必须在 trimesh / torch 之前**。顺序反了会 `ImportError: DLL load failed while importing bpy: 找不到指定的程序`。
   实测矩阵：bpy 最先 → OK；`torch,trimesh,bpy`、`tqdm,trimesh,bpy`、`numpy,tqdm,trimesh,bpy` → 全 FAIL。
   另外 miniconda 里那个 `bpy 4.2.0` pip wheel **不带 Cycles 渲染器**（`'CYCLES' not in engine enum`），能建网格但不能渲 → 渲染必须走 `blender.exe -b`。
   RigAnything 的 `inference.py` 恰好第 7 行 import bpy、第 15 行 import trimesh，**只要把它当入口进程跑就安全**，别从已经 import 过 torch 的封装里 import 它。

2. **trimesh 5.0 的 glTF 加载器会丢掉 skin/权重**。`trimesh.load(rigged.glb)` 能读回几何（`ok_read=true`），但 `scene.geometry` 里没有任何 skin/joints/weights 属性，joint 只剩普通 graph 节点 —— 拿不到权重就没法蒙皮。
   **架构结论：前端摆姿势必须用 Three.js `GLTFLoader`（认 `JOINTS_0`/`WEIGHTS_0`，已验证 GLB 里确实有），后端不要用 trimesh 读 rigged GLB。**
   切片器只要网格：从 GLB 导 STL 不崩，导出来是 **rest pose**、`watertight=True`、顶点数回到原始、bbox 与源网格一致（GLB 里顶点按法线拆开约 6 倍冗余，导 STL 时会重新 weld）。

**权重清单实测（hf-mirror）**：UniRig 真实仓库是 `VAST-AI/UniRig`（`VAST-AI-Research/UniRig` 只是 308 重定向），推理必需 6.44 GB；RigAnything 是 `Isabellaliu/RigAnything`（注意 `liu` 不是 `Liu`），单文件 2.34 GB。
**UniRig 在 V100 上不能直接跑**：`PTv3Object.py` 硬 `import flash_attn` 且 `assert flash_attn is not None`，用的是 FA2 的 varlen API（只支持 sm80+），另有 `torch_scatter` 强依赖和 bf16 autocast。
**RigAnything 能跑**：无 sm80 专属算子，模型本体 195,369,478 参数 / 0.73 GB fp32，ckpt 123/123 tensor 全对上（离线自检 PASS）。


## 三、运动（Animation / Retarget）

| 方案 | 说明 |
|---|---|
| **HY-Motion 1.0** | 腾讯 2026 开源，文本→骨骼动画，[官方介绍](https://mp.weixin.qq.com/s?__biz=MzkwODU2OTQyNQ==&mid=2247497140&idx=1&sn=f16fa633d5fe4f6edaaa690c8d3923a2&poc_token=HAu_Amuj-CW4LgCMUqcC59Rg9xbk3EAsSpZV1QynJ) |
| **Motius** | [ZeyuLing/Motius](https://github.com/ZeyuLing/Motius)，统一生成/理解/编辑/控制 |
| Mixamo / CMU BVH + Blender 重定向 | 零模型，只要 BVH 文件，最稳 |
| 小模型直接吐关键帧 | Codex 试过 Qwen2.5-3B，动作幅度只有 20°/5°，走路不完整 |

## 四、精度（Precision / Refinement）

| 方案 | 状态 |
|---|---|
| **Direct3D-V3** | 源码已在 `D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3`；`cudavoxelize`、`fillhole_cpp` **已在本机编译通过**（需 PATH 加 CUDA bin + torch\lib）；**缺 flash_attn**（FA2 不支持 sm70，需改 torch SDPA）。权重 `wushuang98/Direct3D-V3`（[HF](https://huggingface.co/wushuang98/Direct3D-V3)），另需 DINOv3 ViT-H+、BiRefNet |
| **UltraShape 1.0** | 源码已在 `...\UltraShape-1.0`；需 BF16 + FlashAttention + cubvh（[GitHub](https://github.com/PKU-YuanGroup/UltraShape-1.0)） |
| 提 octree | 768 只加面数不加准确度（实测 IoU 0.4635 < 512 的 0.4709） |
| 部件级重建 | 机身生成 + 桨叶/短舱/浮筒参数化建模再布尔合并（结构误差的主因） |

## 五、多视图（Multi-View）

| 方案 | 状态 |
|---|---|
| Hunyuan3D-2mv（本地） | 权重齐（`hunyuan3d-dit-v2-mv-fast` 4.7 GB + Zero123Plus 5.4 GB）；合成 6 视图路线实测最差（0.3233，4 张里 3 张是编的） |
| **Pixal3D** | TencentARC，[GitHub](https://github.com/TencentARC/Pixal3D)；有 GGUF 版可低显存跑（[指南](https://pixal3d.ai/blog/pixal3d-complete-guide)） |
| MV-Adapter / Era3D / Wonder3D | 均需额外权重 |

### 实测（2026-11-26，详见 `work/research/multiview/RESULT.md`）

| 输入 | eval（产品图参照，辅助） | 四视图 IoU（对 `plane_21` 真值口径） | geo 中位 / P90 (mm) |
|---|---|---|---|
| `plane_21` 自己（单图 2.1 基线） | 0.4709 | 0.998 | 0 / 0 |
| 理想输入（自 `plane_21` 渲正交四视图） | 0.4067 | 0.709 | 1.12 / 2.91 |
| 只改尺度（0.62/1.00/1.35/0.80，角度不变） | 0.4279 | 0.744 | 1.14 / 2.64 |
| **真实照片·白底原图直出** | **0.4872** | 0.636 | 2.29 / 6.73 |
| 真实照片·外部正交化后 | 0.4895 | 0.636 | 1.77 / 8.97 |
| 早先那版（预遮罩产物 `feiji_real_512.glb`） | 0.3112 | 0.434 | 6.53 / 29.96 |

**三条必须记住的结论**：

1. **别预遮罩**：`ComfyUI LoadImage` 只输出 RGB（alpha 单独走 MASK），`Multi_Background_Remover` 拿到的永远是 3 通道 → **必然重跑 birefnet**。早先那版 0.267/0.311 不是"照片差"，是送进去的是透明/黑底图，birefnet 在非白底上抠坏了 mask。规则：**喂白底原图，不预遮罩。**
2. **尺度不一致是免费的、角度不一致是贵的**（决定性对照 `scale4`）：把四张理想视图的物体大小拉开 **2.2 倍**（0.62/1.00/1.35/0.80，只改尺度不改角度），输出几乎不变：四视图 IoU 0.709→**0.744**、geo 中位 1.13→**1.14 mm**、eval 0.4067→0.4279。而只把一张改成 22° 仰角（`elev` 组），geo 中位就从 1.13 拉到 **2.48 mm**（P90 2.99→6.68）。
3. **外部预归一化是冗余的**：`MVImageProcessorV2.recenter()` 会按 alpha bbox 裁切 → 按**最大边**缩放到 512×0.85 → 居中。所以「统一高度/留白/居中」管线会重做。实测把同一批照片正交化（1200 px 最大边、88% 占画、白底 alpha）后，四视图 IoU 0.6357→0.6362（噪声内）、geo 中位 2.29→1.77 mm 但 P90 6.73→8.97 mm —— **不构成改进**。想提升只能改**视角一致性**（同仰角、方位严格 90°、无透视）或换更强骨架，改像素预处理没用。
4. **真实照片直连已是正收益**：0.4872 > 单图基线 0.4709 > 理想输入组 0.4067（后者低是因为 `plane_21` 本身比真机瘦，不是 2mv 的错）。

**一句话**：2mv 这条线现在卡在**视角**（仰角/透视/方位），不在像素预处理，也不在尺度归不归一。


## 六、低显存本地部署

| 手段 | 说明 |
|---|---|
| **GGUF 量化** | `ComfyUI-GGUF` 已装；TRELLIS.2 有第三方 GGUF 扩展（[modly](https://raw.githubusercontent.com/lightningpixel/modly-trellis2-gguf-extension/main/README.md)） |
| fp8 | **V100（sm70）不支持** fp8，别试 |
| bf16 | `torch.cuda.is_bf16_supported()` 返回 True，但 V100 无原生 bf16，靠软件模拟（慢） |
| ComfyUI 参数 | `--use-pytorch-cross-attention`；**不要 `--lowvram`**（sm70 会 CUDA error） |
| 分段/串行 | 铸想已有 `_free_comfy_models()`；多阶段任务必须显式让出显存 |

**本机实测占用**：2.1@512 跑 3D 时约 17 GB 显存。

---

## 七、本轮修掉的三个产品级 bug（2026-11-26）

### 1. 直连后端忽略 `max_facenum`（`backend/backends/direct.py`）

低显存账本实测发现：2mini@512 走直连后端时 `max_facenum` 完全没被读到，
直接吐 **327 万面**（ComfyUI 那条路是 20 万）。已补 quadric 减面：

```python
faces_before = int(len(mesh.faces))
max_faces = int(params.get("max_facenum") or 0)
if max_faces > 0 and faces_before > max_faces:
    mesh = mesh.simplify_quadric_decimation(face_count=max_faces)
```

验证：81,920 面球体降到 20,000，水密保持、体积变化 0.02%；全量 pytest 通过。

### 2. Paint PBR 硬编码联网下载（3D-Pack `multiview_utils.py`）

`hunyuan3d-paintpbr-v2-1` 的加载写了无条件 `snapshot_download`，
即使本地 6.41 GB 权重已就位也会联网 → GFW 下 `LocalEntryNotFoundError`，paint 直接失败。

已改为**优先本地目录**（`FORGEFORM_PAINT_PBR_DIR` 环境变量 → 3D-Pack `Checkpoints` 下自动定位），
找不到才回落官方下载。路径解析已验证命中 6.41 GB 权重。
**注意：补丁要重启 ComfyUI 才生效。**

### 3. 采样线程用 `self._stop` 撞了 `Thread._stop()`（精度脚本）

监控线程把停止信号存成 `self._stop`（`threading.Event`），结束时 `join()` 触发
`TypeError: 'Event' object is not callable` —— 因为 `_stop` 是 `threading.Thread` 的内部方法名，
被实例属性覆盖后解释器会把它当方法调用。症状很隐蔽：**15 步跑完、显存正常释放，但产物没落盘**。
统一改名为 `_stop_evt`（或任何非 `_stop` / `_start` / `_target` 的名字）。
凡自己写采样/看门狗线程的都适用。

---

## 八、贴图方向实测结论（2026-11-26 12:30）

### 结果

飞机用 Hunyuan3D Paint-PBR 2.1 上色：**216.5 s / 峰值 13.7 GB / 峰值 83°C**，120k 面、atlas 2048²、1898 岛、覆盖率 63.4%、GLB 7.14 MB。
白机身 + 深蓝垂尾/舷窗条带，涂装方向正确，**无块状串色**；桨叶/起落架有轻度噪点（多视图遮挡）。

同材质区域色相 σ（饱和加权、按 UV 岛）：**atlas 6.46°**，渲染 az0/45/90 = 8.81/7.61/5.22°。
对比 Codex 旧角色贴图：atlas 24.98°，渲染 12.68/19.03/19.73°，青绿占比 26–28%（飞机 9–20%）。

### 三个必须记住的结论

**① Codex 那版青绿块不是 UV 边界溢色**

UV 岛掩膜 + atlas dilation(12 px) 做完指标**零变化**（12.68°→12.66°、青绿 23.96%→23.96%）。
根因是**多视图反投影把外套的青绿抹进了裤子/靴子的 chart 内部**。
能救的是**岛约束中值协调**：σ 12.68°→8.32°（−34%）。事后修补天花板就到这；正路是把跨缝 3D 邻接一致性做进烘焙阶段。

**② trimesh 读 GLB 后 UV 是 OpenGL 约定，取样必须 `row = (1 - v)`**

搞反的后果：整机变"蓝白迷彩"，看起来像模型上色崩了。
判据：3D 相邻面色差均值取小者（飞机 16.16 vs 23.15；角色 7.92 vs 13.88）。

**③ 主实例的 TexGen 节点会白下 11 GB**

3D-Pack 的 `[Comfy3D] Hunyuan3D 21 TexGen` 走 `multiviewDiffusionNet` 里写死的 `snapshot_download`
（下到 hub cache，不认 `Checkpoints/` 本地权重），hf-mirror 下 hf_hub 会 0 字节卡死。
**已在 `Gen_3D_Modules/Hunyuan3D_2_1/hy3dpaint/utils/multiview_utils.py` 打补丁：优先本地目录（`FORGEFORM_PAINT_PBR_DIR` 环境变量 → Checkpoints 自动定位），找不到才回落下载。重启 ComfyUI 生效。**
在补丁生效前，干净可用的路径是**隔离实例**（独立 base-directory + 独立端口），本次就是这么跑的。
