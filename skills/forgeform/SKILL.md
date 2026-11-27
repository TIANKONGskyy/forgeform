---
name: forgeform
description: 用铸想 ForgeForm 把一句话或一张图变成可打印 3D 模型——对话、出图、单图/多视图建模、PBR 贴图、骨骼绑定、BVH 动画、Direct3D 精修、STL 导出与打印。用户要求调用铸想、改模型、绑骨骼、上色、做动画、导出打印文件或排查它的环境时使用。
license: AGPL-3.0
metadata:
  author: ZYKT studio
  homepage: https://github.com/TIANKONGskyy/forgeform
  version: "3.8.4"
---

# 铸想 ForgeForm

## 能力速查（先看这张表）

| 我要什么 | 怎么做 | 耗时 | 需要什么 |
|---|---|---|---|
| 从一句话造模型 | 对话说需求 → 点「生成图片」→「生成 3D」 | 3–12 min | 出图模型 + 3D 模型 |
| 从照片造模型 | 四张白底原图（正/背/左/右，同仰角 90°）→ `Pipeline.generate_3d_multiview()` | ~85 s | Hunyuan3D 2mv |
| 提高精度 | 对话说「精修一下」或「精修 15 步」 | ~1 min | Direct3D-V3 |
| 上色 / 贴图 | 对话说「上个色」（要参考图） | 8–20 min | Paint PBR |
| 绑骨骼 | 对话说「给它绑骨骼」 | ~40 s | 无（模板档纯 CPU） |
| 绑语义骨（含手指） | 说「用 RigAnything 绑骨骼」 | ~50 s | RigAnything 2.2 GB |
| 加动作 | 说「做个走路动画 / 让它跑起来 / 挥手」 | ~15 s | 无（纯 CPU） |
| 导出打印 | 点「导出 STL」，自动水密化+缩放+贴地 | 2–5 s | 无 |
| 看所有产物 | 「🛠 能力工作台 → ⑥ 我的资产」 | — | 无 |
| 环境坏了 | `python tools/doctor.py` / `python tools/report.py` | 秒级 | 无 |

**产品入口只有一处**：对话、界面按钮、CLI 都调用同一套函数，所以任何一条路跑通即代表全部可用。

## 判断用户想要哪条路

- **有明确实物**（笔筒、支架、齿轮）→ 走单图/多视图；要求孔位/公差/螺纹 → 直接建议参数化 CAD（单视图生不出精确孔）
- **要角色/摆件** → 单图建模后再绑骨骼 + 动作
- **已有照片** → 先问是不是四张同仰角白底图：是 → 多视图（更准）；不是 → 单图
- **只想上色/加动作** → 必须有现成 GLB，问清楚是哪个产物（资产编号或路径）

## 环境与入口

- 界面对话 `:7860`，Studio 配置 `:7861`，对外 API `:7863`（`/v1/generate` 异步、`/v1/jobs/{id}` 轮询）
- ComfyUI 默认 `:8188`；`config.json → backend.mode` 可设 `auto|comfyui|direct`
- 配置：`config.json`；密钥走 `.secrets` 或环境变量，**不要写进 config 或仓库**
- 零模型也能启动：出图/建模/贴图/精修/语义骨会明确报缺什么，对话与纯几何功能照常

## 常见动作的代码入口

```python
from backend.pipeline import Pipeline
from backend import rigging, assets

pipe = Pipeline("config.json")
pipe.generate_3d("front.png", quality_name="高质量")                  # 单图
pipe.generate_3d_multiview({"front": "f.png", "back": "b.png",
                            "left": "l.png", "right": "r.png"})       # 多视图
rigging.bind_model("model.glb", "out/", tier="template")             # 骨骼（CPU）
```

```powershell
python tools/paint_asset.py --mesh m.stl --image ref.png --out out.glb --fast
python tools/asset_refine_rig.py refine --image ref.png --mesh m.obj --out r.glb --steps 4
python tools/collect_gallery.py --out D:\模型总览
```

## 交付前必须验证

1. `trimesh.load(...).is_watertight` — 切片软件把空壳当垃圾
2. 单位与尺寸：导出 STL 走「导出 STL」按钮，别自己转
3. 带贴图/骨骼的 GLB **不保证水密**，要打印就重新导出
4. 多视图照片：不预抠图、不归一化，尺度差异无害

## 出错时

- 先 `python tools/doctor.py`（10 项体检，每项给修复命令）
- 再 `python tools/report.py`（脱敏诊断包，可直接贴 issue）
- 失败会记进 `logs/capability-failures.jsonl`，诊断包里带上
