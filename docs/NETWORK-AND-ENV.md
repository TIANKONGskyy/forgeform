# 安装环境与网络 —— 一条问题一条解法

> 装不上铸想，九成不是代码问题。下面每一条都是**实测过的现场**，
> 后面跟一句能直接照抄的做法。不确定自己是哪种，先跑：

```bash
PYTHONPATH= python tools/doctor.py
```

它会把本地环境 8 项 + 网络 3 类全部测一遍，**不通的项直接给命令**。

---

## 一、网络层

1. **PyPI 慢到装不完** —— 国内直连 pypi.org 每个包十几秒，30 个包能拖半小时；用镜像 `-i https://mirrors.aliyun.com/pypi/simple`。
2. **镜像也会坏** —— 清华源 2026-10 返回过 403、11 月又恢复，所以**不要写死**，`tools/doctor.py` 会当场测五个源挑最快的那个。
3. **`pip config` 比 `-i` 更硬** —— `pip config list` 里若有 `global.index-url`，命令行 `-i` 压不过它，先 `pip config unset global.index-url` 或用同一个源。
4. **GitHub 直连被 reset** —— 走 `https://gh-proxy.com/https://github.com/...`；实测直连 0.17 MB/s、gh-proxy 3–5 MB/s。
5. **gh-proxy 也会挂** —— `ghproxy.net` 实测 SSL 握手 6 秒超时，所以代理地址要能切换，`netcheck.pick_github()` 会自动选能用的。
6. **HuggingFace 权重下不动** —— 官方 `resolve` 路径会被 reset；用 `https://hf-mirror.com` 拼同样的路径。
7. **别拿 ModelScope 当 HF 替身** —— 它的 URL 形状不同（`/models/<org>/<name>/resolve/...`），拼 HF 路径必 404；要下 ModelScope 得单独适配。
8. **只看 `/api` 判断"能连"会骗人** —— `huggingface.co/api` 0.7 秒回 200，但真下权重会断；所以探测必须探**真文件**（doctor 探的是 `bert-base-uncased/resolve/main/config.json` 前 512 字节）。
9. **大文件必须支持断点续传** —— `tools/fetch_model.py` 用 `Range` + `read1()` + 30 秒无数据看门狗；不支持 Range 的镜像只重试一次就换源。
10. **下载卡在固定百分比** —— 那是 `read(n)` 在等满 n 字节，慢链路上永远等不满；已改 `read1()` + 每次写入 flush。
11. **有代理反而全不通** —— `HTTPS_PROXY` 指着一个已经关掉的端口，表现就是"什么网站都连不上"；`$env:HTTPS_PROXY=''` 先清掉再试。
12. **Steam++ / Watt Toolkit 能救 GitHub** —— 装它可以给 GitHub 加速到几 MB/s；铸想检测不到会提示，`/api/netboost` 能装。
13. **DNS 污染** —— 表现为"域名解析到奇怪 IP"；把 DNS 换成 `223.5.5.5` 或 `119.29.29.29`。
14. **HF 大文件要走 CDN 域** —— `hf-mirror.com` 的 `resolve` 会 302 到 `cdn-lfs`，如果它被墙就永远卡在 0 字节；换 `hf-mirror` 的备用路径或直接用 ModelScope 的等价模型。

## 二、解释器层

15. **Python 版本太新/太旧** —— 要 3.11–3.13；3.9 及以下缺 3.10+ 语法，3.14 上部分轮子还没出。
16. **`py` 启动器找不到** —— `py -3.12` 不通就用绝对路径 `C:\Users\<你>\AppData\Local\Programs\Python\Python312\python.exe`。
17. **venv 半成品** —— 装到一半断了，`.venv/` 里只有半个 pip；删掉 `.venv` 和 `.venv/.deps-ok` 重来。
18. **`PYTHONPATH` 串进别的环境** —— 跑铸想的所有命令都加 `PYTHONPATH=` 前缀（等号后留空），否则会串进别的 venv，验证在错误环境里"通过"。
19. **pip 缓存不涨就是源 403** —— 不是限速；看 `pip install -v` 的 HTTP 状态码。
20. **`--trusted-host` 要对得上** —— 换了镜像忘了改 host，pip 会报 SSL 证书不匹配；`netcheck` 是按 URL 自动推导 host 的。

## 三、系统层

21. **Windows 长路径没开** —— 路径超 260 字符解压失败；管理员执行 `reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f` 后重启。
22. **没有管理员权限** —— 把铸想装在**短路径**下（`D:\FF`），别装进 `C:\Program Files` 或桌面（路径还带中文）。
23. **磁盘不够** —— 完整方案 15–25 GB（含权重）；`outputs/` 会自动清 30 天前的东西，`projects/` 永不自动清。
24. **端口 7860 被旧版占着** —— Windows 的 `SO_REUSEADDR` 允许两个进程绑同一端口，Gradio 会"启动成功"但浏览器连到旧版；铸想现在用 `connect_ex` 检测并自动换端口。
25. **杀毒软件拦 pip 写入** —— 表现是随机 `PermissionError`；把项目目录加白名单。
26. **驱动太旧** —— `nvidia-smi` 报错或版本低于 525 时新版 torch 起不来；装最新 Game Ready / Studio 驱动。
27. **没有 N 卡也能装** —— 会走 CPU 模式，只能看界面不能生成；`tools/doctor.py` 会标出来。
28. **中文路径/空格** —— 模型加载器里有的 C++ 库不接受非 ASCII 路径；装到纯英文短路径。

## 四、配置层

29. **`config.json` 被记事本改出 BOM** —— 用 `utf-8-sig` 读，已修；你自己改配置也建议用 VS Code。
30. **`${COMFYUI_DIR}` 没展开** —— 配置里的占位符要能被环境变量替换，否则路径变成字面量；`normalize_config_paths()` 会自动展开并补全默认值。
31. **已有 ComfyUI 没被识别** —— `guess_comfy_dir()` 会扫 C–G 盘 + 桌面/文档找 `main.py` + `models/`，找到就能选"复用现有（零下载）"。
32. **模型目录结构不对** —— 例如 Hunyuan3D 2.1 的权重必须在 `models/checkpoints/tencent/Hunyuan3D_2_1/hunyuan3d-dit-v2-1/model.fp16.ckpt`；`tools/setup.py` 会按方案的 `dest` 放对位置。
33. **下载中断后配置被改坏** —— 铸想的策略是"全部步骤成功才写配置"；失败时保留原配置，不会留下"配置指向不存在的文件"。

## 五、装完之后

34. **先测本机速度** —— `PYTHONPATH= python tools/calibrate.py`；样本只存在本机 `outputs/`，不进发布包。
35. **看是否真的能跑** —— `PYTHONPATH= python tools/calibrate.py --only 3d` 真跑一次建模，比任何"环境检查通过"都可靠。

---

## 一条命令看全部

```bash
PYTHONPATH= python tools/doctor.py            # 环境 + 网络，逐项给修法
PYTHONPATH= python tools/doctor.py --refresh  # 忽略缓存重测网络
PYTHONPATH= python tools/doctor.py --json     # 给程序读
```

输出示例（这台机器的真实结果）：

```
【本地环境】
  ✓ Python 版本       3.11.15  (D:\miniconda3\python.exe)
  ✓ pip 有效源        https://pypi.tuna.tsinghua.edu.cn/simple · 232 ms 可用
  ✓ 代理环境变量       （无）
  ✗ Windows 长路径     未启用（>260 字符的路径会失败）
      → 管理员 PowerShell：reg add HKLM\...\FileSystem /v LongPathsEnabled ...
  ✓ 磁盘余量          70 GB 可用
  ✓ 已有 ComfyUI      D:\ComfyUI-New
  ✓ NVIDIA 驱动       Tesla PG500-216, 32768 MiB, 580.88

【网络】（实测 0.0 秒，来自缓存）
  ✓ PyPI            aliyun · 143 ms ← 会用这个
  ✓ GitHub          gh-proxy · 909 ms ← 会用这个
  ✓ HuggingFace     official · 725 ms ← 会用这个
```
