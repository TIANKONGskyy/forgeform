# -*- coding: utf-8 -*-
"""一条命令把铸想装到"能出模型"（2026-11-28）。

用户反馈里最常见的一句是「我该跑哪条命令」——之前要跑三条，还得知道先跑哪个。
这个脚本把它们串起来，只问一件事：装多大。

    python tools/first_run.py              # 交互：列出方案，默认装最小
    python tools/first_run.py --yes        # 不问，直接装最小（12 GB）
    python tools/first_run.py --with-image # 连出图栈一起装（23 GB）
    python tools/first_run.py --dry-run    # 只体检 + 说会做什么

做的事：
    1. doctor --fix（补目录、生成 config）
    2. 按选择跑 bootstrap_models：direct / image
    3. 复检（capability_doctor）
    4. 告诉你怎么启动
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(args: list[str], timeout: int = 7200) -> int:
    print("  → " + " ".join(args[1:]), flush=True)
    p = subprocess.run([PY] + args, cwd=str(ROOT), timeout=timeout)
    return p.returncode


def _has_mesh() -> bool:
    try:
        sys.path.insert(0, str(ROOT))
        from backend import setup_hint
        return bool(setup_hint.status()["mesh"])
    except Exception:
        return False


def _has_image() -> bool:
    try:
        sys.path.insert(0, str(ROOT))
        from backend import setup_hint
        return bool(setup_hint.status()["image"])
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="一条命令把铸想装到能用")
    ap.add_argument("--yes", action="store_true", help="不问，直接装最小集")
    ap.add_argument("--with-image", action="store_true", help="连出图栈一起装（+11 GB）")
    ap.add_argument("--dry-run", action="store_true", help="只体检并显示计划")
    ns = ap.parse_args()

    print("铸想 ForgeForm 首次安装\n")

    # 1. 环境
    print("[1/3] 环境自检")
    _run(["tools/doctor.py", "--fix"], timeout=900)

    mesh, image = _has_mesh(), _has_image()
    print(f"      建模权重：{'已就位' if mesh else '缺'} · 出图模型：{'已就位' if image else '缺'}")

    plan: list[str] = []
    if not mesh:
        plan.append("direct")
    if not image and ns.with_image:
        plan.append("image")

    if not plan:
        print("\n已经装好了，直接启动：python app.py")
        return 0

    size = {"direct": "12 GB（约 3 分钟）", "image": "11 GB + ComfyUI 环境（约 9 分钟）"}
    print("\n[2/3] 将要安装")
    for p in plan:
        print(f"      · {p:<7} {size.get(p, '')}")

    if ns.dry_run:
        print("\n（dry-run，没有真的下载）")
        return 0

    if not ns.yes:
        try:
            ans = input("\n现在开始？[Y/n] ").strip().lower()
        except EOFError:
            ans = "y"
        if ans not in ("", "y", "yes", "是"):
            print("已取消。之后可以手动跑：python tools/bootstrap_models.py --only " + " / --only ".join(plan))
            return 0

    print("\n[3/3] 安装中（可以关掉这个窗口吗？不行，等它跑完）")
    for suite in plan:
        code = _run(["tools/bootstrap_models.py", "--only", suite])
        if code != 0:
            print(f"\n  ✗ {suite} 没装完（退出码 {code}）。把上面的报错发给我。")
            return code

    print("\n复检：")
    _run(["tools/capability_doctor.py"], timeout=600)

    print("\n装好了。启动：")
    print("    python app.py")
    print("然后浏览器打开 http://127.0.0.1:7860 ，直接说「做个白色陶瓷马克杯」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
