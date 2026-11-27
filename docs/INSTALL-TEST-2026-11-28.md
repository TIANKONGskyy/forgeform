# 全新安装实测（2026-11-28，本机真实跑）

环境：Windows / V100 32G / 模型全删（无 ComfyUI、无 Ollama、无本地权重）。
姿势：用户视角 —— 空目录 + `install.ps1` → `doctor.py` → `bootstrap_models.py --only core`。

## 一、跑通的部分

| 步骤 | 结果 |
|---|---|
| 解析最新版本 | ✓ 自动取到 v3.8.8/3.8.9（不再写死） |
| 镜像下载安装包 | ✓ gh-proxy 3.9 MB，30 秒内完成（github.com 直连超时） |
| 解压 / pip 依赖 | ✓ |
| `doctor --fix` | ✓ 自动建 outputs/logs/projects/temp + 生成 config.json |
| 安装后启动 | ✓ 零模型也能起（7865 端口），对话走规则模板 |
| 权重下载 | ✓ Hunyuan3D 2.1 unet 4.7 GB（ModelScope，续传/校验可用） |
| 3D-Pack clone | ✓ |

## 二、实测发现并修掉的问题

1. **install.ps1 死等 GitHub**：第一源 github.com，直连超时 180 s 才回落 → 改镜像优先 + 每源 30 s
2. **install.ps1 版本写死 v3.8.4** → 改为解析 releases/latest
3. **占位符 `${COMFYUI_DIR}` 被报成"目录不存在"** → doctor 识别为"还没配置"并给命令
4. **零模型启动白折腾**（先"卸载 Ollama"再"启动 Strata"）→ `provider_available()` 一行跳过
5. **ComfyUI 便携版根本没进下载队列**（只登记了 URL）→ 并入下载批次
6. **`_extract_archive()` 少传 log** → 修
7. **稀疏预分配导致"没下完也像下完了"** → 去掉 truncate，改按实际写入字节数校验
8. **下载无进度** → 每 10 s 心跳
9. **1.99 GB 便携版镜像限速 0.3–1.3 MB/s**（要 1 小时）→ 新增源码安装模式（clone + pip，默认）
10. **clone 到已存在目录报错** → clone 到临时目录再合并
11. **ComfyUI 新依赖要求 Python ≥3.12** → 自动挑 ≥3.12 解释器
12. **`comfyui-workflow-templates` 0.1.36 在清华镜像缺失** → 过滤模板包（仅示例工作流用）

## 三、仍未通过的（本次实测终点）

ComfyUI 启动失败：

```
ValueError: infer_schema(func): Parameter stride has unsupported type list[int]
```

原因：Python 3.12 环境里的 torch 版本与 ComfyUI（含 3D-Pack 等自定义节点）不匹配。
这条不是铸想代码问题，是 ComfyUI 侧的版本组合问题。

**建议的确定性组合**（需下一轮验证）：

| 组件 | 版本 |
|---|---|
| Python | 3.11（不要 3.12，模板包强依赖 3.12 但 torch 组合更脆） |
| torch | 2.5.1 + cu121（V100 稳定） |
| ComfyUI | 便携版（自带匹配 torch）或 pin 到 2026-10 的 commit |

下一轮要做的：把 `bootstrap_models.py --comfy portable` 跑通（便携版绕开 torch 组合问题），
或增加 `--comfy-commit` 固定 ComfyUI 版本 + `--torch` 固定 torch 版本。

## 四、当前机器状态

- `D:\ComfyUI-New`：源码已 clone，依赖基本装好，**起不来**（torch 不匹配）
- 权重：Hunyuan3D 2.1 unet 4.7 GB 已就位
- `D:\install_test\forgeform-v3.8.8`：安装副本，可启动
- `D:\ForgeForm-oss`：开发仓库，3.8.9 改动本地已提交（GitHub 推送当时被墙，待重试）
