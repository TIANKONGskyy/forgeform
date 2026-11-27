# -*- coding: utf-8 -*-
"""问题上报：一行命令生成脱敏诊断包。

    python tools/report.py

等价于 `python tools/doctor.py --report`，单独留一个入口是因为
「出问题了」的人不会先去看 doctor 有哪些参数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from tools.doctor import build_report, environment

    env = environment()
    print("  环境快照：", flush=True)
    for k in ("platform", "python", "version", "nvidia_smi"):
        v = str(env.get(k, ""))
        print(f"    {k}: {v.splitlines()[0] if v else '-'}"[:160], flush=True)
    path = build_report()
    print(f"\n  ✓ 诊断包：{path}", flush=True)
    print("    已脱敏（key/token/密码打码，家目录换 ~）。发之前可以先自己解压看一眼。", flush=True)
    print("    报问题：https://github.com/TIANKONGskyy/forgeform/issues", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
