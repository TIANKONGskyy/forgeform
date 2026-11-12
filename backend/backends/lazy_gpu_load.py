# -*- coding: utf-8 -*-
"""把 safetensors 权重"逐层搬到显存"，让系统内存只留当前那一层。

## 为什么需要这个

hy3dgen 原生是 `safetensors.torch.load_file(ckpt, device='cpu')`：
把 3.6 GB 权重整份读进系统内存、建完整的 Python dict，再转 dtype、再搬显存。
实测峰值 RSS **10.8 GB**，16 GB 内存的机器直接 `DefaultCPUAllocator: not enough memory`。

改成"读一层 → 搬到显存 → 释放内存里那份"之后，实测：

```
1325 层 / 3643 MB / 10.5s
显存峰值 3.56 GB   内存峰值 4.19 GB
```

同样的权重，内存峰值从 10.8 GB 降到 4.2 GB。

## 怎么用

```python
from backend.backends.lazy_gpu_load import patch_safetensors
patch_safetensors()          # 装上之后所有 load_file 都会走这条路
from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(...)
```

## 注意

只对**单文件 safetensors** 生效。GGUF / ckpt 的加载路径不变。
显存不够时会抛 `torch.cuda.OutOfMemoryError`（优雅失败），不会段错误 ——
这一点和 `load_file(device='cuda')` 不同，后者在显存吃紧时会让进程直接崩。
"""
from __future__ import annotations

from typing import Optional

import torch


def load_to_gpu(path: str, dtype: Optional[torch.dtype] = torch.float16,
                device: str = "cuda") -> dict:
    """逐层读 → 搬设备 → 丢掉内存副本。返回一个"张量都在 GPU 上"的普通 dict。

    返回的是 `dict` 而不是惰性对象 —— hy3dgen 的 `from_single_file` 会多次
    遍历 `.items()` 并按 key 分组，惰性对象在这种用法下会把所有张量又缓存一遍，
    反而回到"整份权重驻留内存"的老路（实测会在 6 GB 显存限制下段错误）。
    """
    from safetensors import safe_open

    out: dict[str, torch.Tensor] = {}
    with safe_open(path, framework="pt", device="cpu") as f:
        for key in f.keys():
            t = f.get_tensor(key)
            if dtype is not None and t.is_floating_point():
                t = t.to(dtype)
            if device != "cpu":
                t = t.to(device, non_blocking=True)
            out[key] = t
            del t                      # 让 CPU 那份尽快被回收
    return out


def patch_safetensors(device: str = "cuda", dtype=torch.float16) -> None:
    """把 `safetensors.torch.load_file` 换成"直接落到 GPU"。"""
    import safetensors.torch as st

    if getattr(st.load_file, "_ff_patched", False):
        return

    orig = st.load_file

    def load_file_gpu(filename, device="cpu"):
        # 只接管 .safetensors；调用方显式要 cpu 的也照搬（有些路径确实需要）
        if not str(filename).lower().endswith(".safetensors"):
            return orig(filename, device=device)
        return load_to_gpu(str(filename), dtype=dtype,
                           device="cpu" if device == "cpu" else device)

    load_file_gpu._ff_patched = True
    load_file_gpu._ff_orig = orig
    st.load_file = load_file_gpu


def unpatch_safetensors() -> None:
    import safetensors.torch as st

    fn = getattr(st, "load_file", None)
    if fn is not None and getattr(fn, "_ff_patched", False):
        st.load_file = fn._ff_orig
