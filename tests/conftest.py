# -*- coding: utf-8 -*-
"""pytest 前置：仓库里只提交 ``config.example.json``，第一次运行时得先落一份 ``config.json``。

为什么放在 conftest 里：它在**收集阶段之前**执行。如果放到某个测试里，
就会出现「前面几个用例因为没有配置而红、后面因为配置被别的用例写出来而绿」的
顺序依赖 —— 在开发机上永远看不出来（本地早就有 config.json 了），
在干净的 CI / 别人的机器上第一次跑就是一片红。
"""
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CFG = _ROOT / "config.json"
_EXAMPLE = _ROOT / "config.example.json"

if not _CFG.exists() and _EXAMPLE.exists():
    shutil.copyfile(_EXAMPLE, _CFG)
