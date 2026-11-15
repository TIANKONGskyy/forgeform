# -*- coding: utf-8 -*-
"""环境 + 网络一趟查完，每条都给"怎么修"。

    PYTHONPATH= python tools/doctor.py            # 全查（联网约 10 秒）
    PYTHONPATH= python tools/doctor.py --offline  # 只看本地环境
    PYTHONPATH= python tools/doctor.py --json     # 给程序读

为什么要有这个：铸想装不上，九成不是代码问题，而是下面四类之一 ——
    ① 网络层：PyPI / GitHub / HF 在某台机器上不通或极慢
    ② 解释器层：Python 版本、pip 硬配置、venv 半成品
    ③ 系统层：长路径、磁盘、驱动、端口被旧版占着
    ④ 配置层：路径写错、BOM、模型目录对不上
这个脚本把这些一次性摆出来，**不用靠问用户"你那边什么情况"**。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK = "\u2713"
BAD = "\u2717"


def _line(name: str, ok: bool, value: str, fix: str = "", width: int = 16) -> str:
    mark = OK if ok else BAD
    out = f"  {mark} {name:<{width}}{value}"
    if not ok and fix:
        out += f"\n      → {fix}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想 环境与网络自检")
    ap.add_argument("--offline", action="store_true", help="不联网")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新测网络")
    args = ap.parse_args()

    from backend import netcheck as N

    env = N.env_checks()
    net = None
    if not args.offline:
        cache = N.CACHE
        if args.refresh and cache.exists():
            cache.unlink()
        t0 = time.time()
        net = N.probe_all()
        net["_elapsed"] = round(time.time() - t0, 1)

    if args.json:
        print(json.dumps({"env": env, "net": net}, ensure_ascii=False, indent=2))
        return 0 if all(e["ok"] for e in env) else 1

    print("=" * 66)
    print("  铸想 ForgeForm · 环境与网络自检")
    print(f"  目录 {ROOT}")
    print("=" * 66)

    print("\n【本地环境】")
    for e in env:
        print(_line(e["name"], e["ok"], e["value"], e["fix"]))
    bad_env = [e["name"] for e in env if not e["ok"]]

    if net:
        print(f"\n【网络】（实测 {net.get('_elapsed', '?')} 秒"
              f"{'，来自缓存' if net.get('cached') else ''}）")
        for table, label in (("pypi", "PyPI"), ("github", "GitHub"), ("hf", "HuggingFace")):
            rows = net.get(table) or []
            best = next((r for r in rows if r.get("ok")), None)
            if best:
                print(_line(label, True, f'{best["name"]} · {best["ms"]} ms ← 会用这个'))
                for r in rows:
                    if r is best:
                        continue
                    mark = OK if r.get("ok") else BAD
                    print(f"      {mark} {r['name']:<12}{r['ms']:>6} ms  "
                          f"{r.get('error', '') or str(r.get('status', ''))}")
            else:
                print(_line(label, False, "所有镜像都不通",
                            "确认能上网；有代理的话检查 HTTPS_PROXY；"
                            "或让同事用 U 盘拷已下好的模型目录。"))

    print("\n【结论】")
    if bad_env:
        print(f"  {len(bad_env)} 项本地环境要注意：{', '.join(bad_env)}")
    else:
        print("  本地环境没有阻塞项。")
    if net:
        pypi = N.pick_pypi()
        gh = N.pick_github()
        hf = N.pick_hf()
        print(f"  pip 源    {pypi}")
        print(f"  GitHub    {gh}")
        print(f"  HF 权重   {hf}")
        print()
        print("  装的时候直接用这个源：")
        print(f"    set PIP_INDEX_URL={pypi}")
        print(f"    PYTHONPATH= python tools/setup.py --yes")
        print()
        print("  下一步：")
        print("    PYTHONPATH= python tools/setup.py            # 选方案并下载")
        print("    PYTHONPATH= python tools/calibrate.py        # 装完测本机速度")
        print("    PYTHONPATH= python tools/studio.py           # 图形化安装")
    return 0 if not bad_env else 1


if __name__ == "__main__":
    raise SystemExit(main())
