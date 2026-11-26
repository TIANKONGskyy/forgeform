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
| Hunyuan3D-2mv（本地） | 权重齐（`hunyuan3d-dit-v2-mv-fast` 4.7 GB + Zero123Plus 5.4 GB）；合成 6 视图路线实测最差（0.3233） |
| **Pixal3D** | TencentARC，[GitHub](https://github.com/TencentARC/Pixal3D)；有 GGUF 版可低显存跑（[指南](https://pixal3d.ai/blog/pixal3d-complete-guide)） |
| MV-Adapter / Era3D / Wonder3D | 均需额外权重 |

**关键实测结论**：真实照片直连 2mv 反而更差（0.267），原因是 5 张照片**仰角不一致**（正视略仰、背视俯拍机尾、侧视平视）。四视图必须同仰角、同尺度。

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

## 七、本轮修掉的两个产品级 bug（2026-11-26 12:20）

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
