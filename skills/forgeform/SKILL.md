---
name: forgeform
description: 使用铸想 ForgeForm 通过文字或参考图生成、修改、检查和导出可打印 3D 模型，或配置与排查它的本地模型和运行环境。用户要求调用铸想、ForgeForm、其工作流或 API 时使用。
license: AGPL-3.0
metadata:
  author: ZYKT studio
  homepage: https://github.com/TIANKONGskyy/forgeform
  version: "3.7.3"
---

# 铸想 ForgeForm

铸想由 ZYKT studio 开发，提供对话、出图、图生 3D 和 STL 导出。先确认用户想要的对象、尺寸、现有参考图和所需交付；未知参数可用合理默认值，避免擅自下载大模型、删除数据或调整用户配置。单视图生成不适合要求精确孔位、公差或螺纹的零件，应建议参数化 CAD。网格水密并不保证可打印或尺寸准确，交付前仍须用切片器检查薄壁、悬空和支撑。

## 选择入口与检查环境

- 确定当前安装目录和 `VERSION`，以该目录源码与配置为准，不假定固定路径、模型已安装或服务已启动。Windows 启动入口是 `启动.bat`，配置界面默认 `127.0.0.1:7861`，创作界面默认 `127.0.0.1:7860`；可在启动参数中更改。
- Studio `GET /api/ping`、`GET /api/status`、`GET /api/catalog`、`GET /api/hardware` 可查询状态。扫描是 **POST** `/api/scan`，安装、配置、启动、取消及删除等写操作也用 POST：从本机 Studio 页面取得 `STUDIO_TOKEN`，以 `X-Studio-Token` 发送，并遵守服务的来源校验。异步任务轮询 `/api/status` 至结束，检查错误和日志。不要输出或提交令牌。
- 生成不一定依赖 ComfyUI：`backend.mode` 可为 `auto`、`comfyui` 或 `direct`，实际路由取决于权重和运行环境。确认配置中模型 key、路径和已安装依赖；`tools/doctor.py` 和 Studio 的扫描可帮助诊断。先检查 ComfyUI `/queue` 和 GPU 使用，避开别人的运行任务；不要自行中断其他任务或卸载共享服务。
- 扫描会检查本地模型文件、分片和复用方案节点，但这不等于验证权重可加载或完整推理成功。按实际选择的模型和质量档验证运行；显存及可用内存紧张时优先选低资源配置。低配进程限额实验不能代替真实整机测试。
- 对程序调用，若独立 API 服务可用，先读其 `/openapi.json`：默认 `127.0.0.1:7863`，`GET /v1/health` 无需密钥，`POST /v1/generate` 提交异步任务，`GET /v1/jobs/{id}` 查状态，`GET /v1/jobs/{id}/files/{image|glb|stl}` 获取产物；需已有授权的 `Authorization: Bearer` 密钥。不要为测试随意创建或公开密钥。`tools/api_server.py` 是接口事实来源。

## 本地 Python 工作流

在安装目录、项目使用的 Python 环境执行；模型 key 从当前 `config.json` 和已安装权重确定，输出写到用户允许的位置。以下展示方法，实际参数按用户需求和硬件选择：

```python
from pathlib import Path
from backend.pipeline import Pipeline

pipe = Pipeline()  # 默认读取安装目录的 config.json
images = pipe.generate_images(
    "a single ceramic owl figurine, white background", count=1,
    model_key="<已安装的出图模型 key>", quality_name="快速",
    resolution="512×512 (快速)", skip_cache=True,
)
if not images or not images[0].success:
    raise RuntimeError(images[0].error if images else "没有生成图片")
image_path = images[0].output_path
assert Path(image_path).is_file()
mesh = pipe.generate_3d(model_key="<已安装的建模模型 key>",
                        quality_name="快速", image_path=image_path)
if not mesh.success:
    raise RuntimeError(mesh.error)
assert Path(mesh.output_path).is_file()
stl = pipe.export_stl(mesh.output_path, target_size_mm=80)
if not stl.success:
    raise RuntimeError(stl.error)
assert Path(stl.output_path).is_file()
```

`generate_images` 返回结果列表，`generate_3d` 与 `export_stl` 返回 `StageResult`；检查 `success`、`error`、`output_path`。长任务可能耗时数分钟，不要以短 HTTP 超时或空响应判断成功。参考图优先走用户指定图；按需使用 `reference_image`，并确认所选模型支持该模式。不要猜模型 ID 或生成未请求的内容。

## 验证与交付

- 检查图像可读取且与描述相符；用 `trimesh` 读取 GLB/STL，报告面数、包围盒毫米尺寸、连通性和 `is_watertight`。检查预览外形是否有背景板、碎块或明显变形；记录质量闸、降级、失败及耗时，不能用旧文件冒充本次产物。
- 若失败，保留原始报错。显存不足时先检查占用及队列；只对本次任务的模型做释放或调整，选择更适合的已安装模型或质量档再试。若没有合适模型或资源被占用，说明缺失与下一步，不破坏其他任务。
- 修改铸想源码后，运行相关测试；`python -m pytest` 与 `python tools/verify.py --fast` 分别覆盖行为与项目契约。真实推理和打印效果需要单独实测。修改配置前保留原始文件；不要提交配置、权重、密钥、作品或生成产物。
