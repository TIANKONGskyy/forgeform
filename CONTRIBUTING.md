# 参与开发

欢迎 issue 和 PR。这个项目是从一台机器上长出来的，有些地方的做法看起来怪，多数是因为在实际硬件上撞过墙 —— 改动之前先看下面的约定，能省你几个小时。

## 起步

```powershell
git clone https://github.com/<you>/forgeform.git
cd forgeform
pip install -r requirements.txt

# 开发用的验证入口（不需要 GPU、不加载模型、不连 ComfyUI）
python -m pytest
python tools/verify.py
```

`tools/verify.py` 是权威入口，包含源码契约断言；`pytest` 是标准入口。两个都要绿。

## 改代码前必读的几条

### 1. 路径只能写在 `backend/env.py`

任何模块要目录，`from backend.env import ROOT, OUTPUTS, COMFYUI_DIR`。
不要在业务代码里出现 `D:\...` 或 `C:\Users\...`。`tests/` 里有一条测试就是在扫这个。

### 2. UI 的按钮、`ui_outputs`、`_update_ui` 返回值必须同步

Gradio 的返回值是按位置对上的。`_update_ui` 返回 N 个、`ui_outputs` 是 M 个、某个 handler `yield` 了 K 个 —— 三者不一致时**不会报错**，只会让按钮长到别的控件上去。

新增一个按钮/输出，四处都要改：

1. `build()` 里创建组件
2. `ui_outputs` 列表
3. `_update_ui()` 的返回元组
4. `_buttons_for_phase()` 的分支（**没列的 phase 会落到最后 `return with_stl(off)`，也就是全部隐藏** —— 用户会以为功能没了）

`tests/test_conversation_flow.py::test_update_ui_arity_matches_ui_outputs` 会在你漏改时红。

### 3. 新增状态字段，两个白名单都要加

`project_manager.save_project` 和 `load_project` 各有一份字段白名单。只加一处的话，重启后字段就丢 —— 表现为「自动建模永远跳过」。`candidates` / `selected` 就这么丢过。

### 4. 显式建模前先 `cancel_autopilot()`

出图后有一个 5 秒后自动建模的 Timer（`arm_autopilot`）。直接调 `_do_generate_3d` 而不取消，会在同一组里建两次模，中间 `glb_path=None`，前端拿不到模型。

### 5. 提示词层的数字不能被模型改写

需求里的 `3mm` 被 LLM 改写成 `100mm` 是有前科的。`_numbers_preserved` 是最后一道闸，动 prompt 的人别把它绕过去。

## 测试怎么写

这些测试**不测 GPU**，测的是流程契约。真机上表现为「点了没反应」的 bug，几乎都只能在源码层面断言：

```python
# 例：断言某个 phase 下按钮必须可见
def test_generating_phase_shows_retry(self):
    ...
```

写测试时的两个坑（都踩过）：

1. **不要 `import app`**。导入会启动后台 bootstrap，可能把对话模型拉进显存，把别人的生成/训练拖慢。
2. **先隔离 `PROJECTS_DIR` 再导入本仓库任何模块**，否则会往真实 `projects/` 里写东西。

外部资源（ComfyUI、CadQuery、用户自己的 STL）用 `skipUnless` + 环境变量，缺了就 skip，不要 fail。

## 提交

- 一个 PR 一件事。重构和功能别混。
- 提交信息用中文可以，格式大致 `fix: ...` / `feat: ...` / `docs: ...`。
- 涉及 UI 的改动，贴一张改动前后的截图。
- 涉及生成质量的改动，贴耗时和 VRAM 数字（`nvidia-smi`）。「感觉更好了」不算。

## 不要做的事

- 不要往仓库里加模型权重、演示视频、`.glb`/`.stl`。`.gitignore` 已经拦了，别用 `-f` 绕过。
- 不要把 `config.json` 提交上来（里面有本机路径和端口）。改 `config.example.json`。
- 不要在 `.ps1` 里写中文注释：PowerShell 5.1 按 GBK 读 UTF-8 无 BOM 脚本会把它们读成乱码然后 ParserError。
- 不要用 `--lowvram` 跑 V100/sm_70（详见 README「已知限制」）。
