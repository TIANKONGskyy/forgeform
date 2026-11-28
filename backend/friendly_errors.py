# -*- coding: utf-8 -*-
"""把底层异常翻译成"用户能照做"的提示（2026-11-28 实测沉淀）。

为什么需要
----------
零模型 / ComfyUI 没起 / 权重没下时，用户看到的是这些：

    HTTPConnectionPool(host='127.0.0.1', port=8188): Max retries exceeded ...
    Repo id must use alphanumeric chars, '-' or '.': 'D:\\models\\hy3dgen\\hunyuan3d-2mini'
    No module named 'cv2'

这些对用户等于"天书"，而真正该做的是三条命令之一。所以统一在这里翻译。
"""
from __future__ import annotations

import re

_HINTS = [
    # (正则, 一句话结论, 该跑什么)
    (r"Max retries exceeded|NewConnectionError|Connection refused|8188",
     "ComfyUI 没在运行（出图/贴图/多视图都要它）",
     "python tools/install_comfy.py   然后重开一次铸想"),
    (r"Repo id must use|is not a valid model identifier|doesn't exist.*models",
     "模型权重不在这台机器上（或路径写错了）",
     "python tools/bootstrap_models.py --only direct   # 直连建模权重（12 GB）"),
    (r"No module named 'cv2'|No module named 'trimesh'|No module named 'diffusers'",
     "缺运行依赖",
     "python tools/install_comfy.py --verify-only   看缺什么，再按提示装"),
    (r"No module named 'pytorch3d'|No module named 'nvdiffrast'|custom_rasterizer_kernel|DLL load failed",
     "这条能力依赖编译型 CUDA 扩展，当前环境装不上",
     "python tools/capability_doctor.py   看这条能力的替代方案"),
    (r"CUDA out of memory|OutOfMemoryError",
     "显存不够",
     "先关掉别的占显存的程序（ComfyUI / 对话模型），或换更小的档位"),
    (r"401|403|Unauthorized|Invalid API key|api key",
     "云端 API key 无效或没配",
     "在设置里填正确的 key，或改用本地后端"),
    (r"timeout|Timeout|timed out",
     "请求超时",
     "网络或服务太慢，重试一次；ComfyUI 首次加载模型会慢"),
]


def friendly(err) -> str:
    """输入异常或错误字符串，返回"原信息 + 该做什么"。"""
    text = str(err) if not isinstance(err, str) else err
    if not text:
        return text
    low = text.lower()
    for pattern, what, todo in _HINTS:
        if re.search(pattern, text, flags=re.I) or pattern.lower() in low:
            # 保留原始信息的前 120 字符，方便排查，但把结论放前面
            brief = text.strip().splitlines()[0][:120]
            return f"{what}。\n→ {todo}\n（原始信息：{brief}）"
    return text
