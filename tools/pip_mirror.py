# -*- coding: utf-8 -*-
"""pip 安装的统一入口：镜像优先、逐个回退（2026-11-28）。

为什么单独抽出来
----------------
实测踩了两次：在干净 shell 里跑 `bootstrap_models.py`，它内部的 pip 用默认源（pypi.org），
在国内会**静默挂住十几分钟**（CPU 不动、日志不动），用户以为"卡死了"。

这里统一：先看 `PIP_INDEX_URL`（用户自己配的优先），再依次试国内镜像，最后才回官方源。
每个源都带超时，失败就说清楚试过哪些源。
"""
from __future__ import annotations

import os
import subprocess
import sys

MIRRORS = [
    os.environ.get("PIP_INDEX_URL", "").strip(),
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]


def install(args: list[str], log=print, python: str | None = None,
            timeout: int = 1800, quiet: bool = True) -> bool:
    """装包。args 可以是 ['-r','req.txt'] 或 ['pkg1','pkg2']。"""
    py = python or sys.executable
    seen: list[str] = []
    for idx, mirror in enumerate(MIRRORS):
        mirror = (mirror or "").strip()
        if not mirror or mirror in seen:
            continue
        seen.append(mirror)
        cmd = [py, "-m", "pip", "install"]
        if quiet:
            cmd.append("-q")
        cmd += args
        if mirror != "https://pypi.org/simple":
            cmd += ["-i", mirror, "--trusted-host", mirror.split("//")[-1].split("/")[0]]
        log(f"    pip（{mirror.split('//')[-1].split('/')[0]}）")
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    ✗ 超时（{timeout}s），换下一个源")
            continue
        if p.returncode == 0:
            return True
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = err[-1][:140] if err else f"退出码 {p.returncode}"
        log(f"    ✗ {tail}")
    log("    ✗ 所有 pip 源都失败：" + ", ".join(m.split("//")[-1].split("/")[0] for m in seen))
    return False
