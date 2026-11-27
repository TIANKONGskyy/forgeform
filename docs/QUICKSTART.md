# 5 分钟开始（新手向）

> 这份文档假设你**什么都没装**。按顺序做，每一步都有"怎么确认成功了"。

## 0. 你需要什么

| 项 | 最低 | 说明 |
|---|---|---|
| 系统 | Windows 10/11 64 位 | Linux 也能跑，脚本换成对应的 `.sh` |
| Python | **3.10+**（推荐 3.12） | 装的时候**勾上 "Add python.exe to PATH"** |
| 显卡 | 有更好，没有也能用 | 没显卡时把 `config.json → backend.mode` 改成 `cloud` |
| 磁盘 | 留 20 GB | 建模的临时文件很占地方 |
| ComfyUI | 可选 | 出图/建模/贴图都用它；不装就只能用对话和纯几何功能 |

## 1. 拿到代码

```powershell
git clone https://gh-proxy.com/https://github.com/TIANKONGskyy/forgeform.git
cd forgeform
```

没装 git 就下 zip：<https://github.com/TIANKONGskyy/forgeform/releases/latest>

## 2. 装依赖

```powershell
python -m pip install -r requirements.txt
```

**怎么确认成功**：下面这条不报错就对了。

```powershell
python -c "import gradio, trimesh, torch; print('ok')"
```

> 国内网络：`pip` 走清华/阿里源。`pip install -i` 压不过 pip 配置，要用
> `$env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"`。

## 3. 体检（重要，30 秒）

```powershell
python tools/doctor.py --fix
```

它会逐项告诉你：Python 版本、依赖、显卡、torch CUDA、ComfyUI 连不连得上、
配置能不能解析、目录、磁盘、模型权重、端口占用。

**每项失败都会直接给修复命令**，照着做即可。典型输出：

```
✓ Python 版本  3.12.4
✓ GPU        NVIDIA GeForce RTX 3060, 12288 MiB, 551.23
✗ ComfyUI    ConnectionError
    → 先启动 ComfyUI（仓库不自带，需单独装）
```

## 4. 启动

```powershell
python app.py
```

或者双击 **`一键开始.ps1`**（它会先体检再启动，有问题直接说，不会假装启动成功）。

浏览器打开 <http://127.0.0.1:7860>，看到对话框就能用了。

## 5. 第一次创作（30 秒）

在对话框里输入：

```
做一个直径 90mm 的六边形笔筒，壁厚 3mm
```

然后按提示点「生成图片」→「生成 3D」→「导出 STL」。

**导出后验证水密**（切片软件当空壳是最常见的问题）：

```powershell
python -c "import trimesh;m=trimesh.load(r'你的.stl');print('watertight =',m.is_watertight)"
```

## 6. 五种进阶能力（说完就干，不用点按钮）

有 3D 模型之后，在对话里直接说：

| 你说 | 它做 | 耗时 |
|---|---|---|
| 给它绑骨骼 | 模板骨架（CPU） | ~40 秒 |
| 用 RigAnything 绑骨骼 | 语义骨架，含手指 | ~50 秒 |
| 上个色 / 贴图 | PBR 上色 | 8–20 分钟 |
| 精修一下 / 精修 15 步 | Direct3D-V3 提精度 + 自动去碎壳 | ~1 分钟 |
| 做个走路动画 / 让它跑起来 | BVH 动作重定向 | ~15 秒 |

产物全部自动进 **「🛠 能力工作台 → ⑥ 我的资产」**，带面数/水密/贴图/关节信息，
超容量自动淘汰最旧的（默认 8 GB）。

## 7. 出问题了怎么办

```powershell
python tools/report.py
```

生成 `outputs/reports/forgeform-report-<时间>.zip`（**已脱敏**：key/token/密码打码，
家目录换成 `~`）。把它附在
[issue](https://github.com/TIANKONGskyy/forgeform/issues/new?template=bug_report.yml) 里即可。

## 常见卡点

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面一直"正在处理…" | 首个模型在冷启动（可能几分钟） | 等；日志里 `warm_up` 有进度 |
| 出图报 CUDA 错误 | torch 装成了 CPU 版 | 重装 CUDA 版 torch |
| 建模 OOM | 显存被对话模型占着 | 说「先释放显存」，或重开应用 |
| ComfyUI 连不上 | 没启动 / 端口不对 | `python tools/doctor.py` 看具体地址 |
| STL 切片是空壳 | 没通过水密 | 用「导出 STL」而不是自己转；它会补洞 |
| 输入中文命令行乱码 | GBK 编码 | 设 `$env:PYTHONIOENCODING="utf-8"` |
