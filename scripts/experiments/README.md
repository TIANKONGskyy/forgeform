# 开发期实验脚本（归档）

这里的脚本是开发过程中在**一台特定机器**上跑的一次性工具：跑基准、比对画质、批量验证、改工作流、抓 ComfyUI 的节点源码。

**不做兼容承诺。** 它们普遍写死了当时的路径（`D:\AI-Workshop`、`C:\Users\mac\Desktop\...`）、依赖当时的产物文件、依赖当时装的 ComfyUI 自定义节点。放在仓库里是为了记录「当时是怎么把参数试出来的」，不是为了开箱即用。

想复现某个实验，按脚本头部的注释把路径改成你自己的。

## 哪些是"活的"

`scripts/` 根目录（不在本文件夹里）的那些还能直接用：

| 脚本 | 作用 |
|---|---|
| `set_power_limit.ps1` | 给 GPU 降功耗墙（改卡/散热不佳时防掉卡） |
| `thermal_governor.py` | 按温度渐进降频 + 迟滞回温的守护进程 |
| `setup_thermal_gov.ps1` | 把上面那个注册成登录自启的计划任务 |
| `restart-comfyui.ps1` | 重启 ComfyUI（读锁、释放端口、按单卡/双卡起实例） |
| `restart_aiworkshop.ps1` | 重启整条链路 |
| `start_app.ps1` | 只起 Gradio 应用 |

## 分类

- `bench_*.py` — 参数基准（TeaCache / FBcache / HyperSD / Klein / 多视图 turbo）
- `gen_*.py` — 批量生成数据集或对照组
- `verify_*.py` — 画质/结构验证（锐度、浮雕深度、工作流可用性）
- `judge_*.py` — 人工打分辅助
- `read_*.py` — 读 ComfyUI 蓝图/节点源码，确认输入输出定义
- `_gen_*.py` — 被其它脚本当子进程调用的生成入口
- `submit_rocket_*.py` / `smoke_holder*.py` / `rerun_*.py` — 针对具体零件的复跑
- `auto_evolve.py` / `multi_evolve.py` / `continuous_generate.py` — 参数自动进化与无人值守生成
