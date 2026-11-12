# -*- coding: utf-8 -*-
"""配置向导：看这台机器有什么，然后告诉你装哪套、好不好装、怎么一键装。

    python tools/setup.py                 # 交互：看方案 → 选一个 → 装
    python tools/setup.py --dry-run       # 只看，不动
    python tools/setup.py --profile reuse --yes      # 直接按某个方案执行
    python tools/setup.py --custom        # 自己选出图/建模各用哪个模型

它做四件事：
  1. 识别环境（显存 / 内存 / 磁盘）和已有框架（ComfyUI / Ollama / llama.cpp / vLLM / 本地 diffusers）
  2. 生成几个方案，标出"能不能装得下"和"为什么"
  3. 你选一个（或自己配）
  4. 需要下载的调 fetch_model.py，最后把 config.json 写好并跑 doctor 验证
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ────────────────────────────────────────────────────────────────
# 展示
# ────────────────────────────────────────────────────────────────
def _line(ch: str = "─", n: int = 72) -> None:
    print(ch * n)


def show(profiles, default_key: str) -> None:
    for i, p in enumerate(profiles, 1):
        mark = "★" if p.key == default_key else " "
        tag = "可用" if p.fits else "装不下"
        tot = sum(d.size_gb for d in p.downloads)
        print(f" {mark} [{i}] {p.name}")
        print(f"      {tag} · 需要下载 {tot:.1f} GB · 后端 {p.backend_mode}")
        print(f"      {p.desc}")
        if p.reason and p.reason != "够用":
            print(f"      注：{p.reason}")
        print()


def show_detail(p) -> None:
    _line()
    print(f"方案：{p.name}")
    print(f"说明：{p.desc}")
    print(f"后端：{p.backend_mode}   出图：{p.image_model or '不变'}   建模：{p.mesh_model or '不变'}")
    _line("·")
    if not p.downloads:
        print("需要下载：无")
    else:
        print(f"需要下载 {sum(d.size_gb for d in p.downloads):.2f} GB：")
        for d in p.downloads:
            kind = {"llm": "对话模型", "framework": "依赖/框架", "image": "出图",
                    "mesh": "建模"}.get(d.kind, d.kind)
            tail = f" → {d.dest}" if d.dest else ""
            print(f"  · [{kind}] {d.repo}  ({d.size_gb:.2f} GB){tail}")
            if d.note:
                print(f"      {d.note}")
            print(f"      {d.command()}")
    _line()


# ────────────────────────────────────────────────────────────────
# config 合并
# ────────────────────────────────────────────────────────────────
def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def write_config(patch: dict, cfg_path: Path) -> Path:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        base = json.loads(cfg_path.read_text(encoding="utf-8"))
        bak = cfg_path.with_suffix(".json.bak")
        shutil.copyfile(cfg_path, bak)
        print(f"  （原配置已备份到 {bak.name}）")
    else:
        example = ROOT / "config.example.json"
        base = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}
    deep_merge(base, patch)
    # 顺手把 paths 补成可用的默认（auto 后端第一次跑要能定位 ComfyUI）
    cfg_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_path


# ────────────────────────────────────────────────────────────────
# 执行
# ────────────────────────────────────────────────────────────────
def run_downloads(p, yes: bool) -> bool:
    if not p.downloads:
        return True
    total = sum(d.size_gb for d in p.downloads)
    print(f"\n要下载 {total:.2f} GB。")
    if not yes:
        ans = input("开始下载？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("跳过下载。")
            return False
    for d in p.downloads:
        # 按 kind 分派：LLM 走 ollama pull，框架走提示，模型走自带下载器
        if d.kind == "llm":
            exe = shutil.which("ollama")
            if not exe:
                print(f"\n  跳过 {d.repo}：没找到 ollama 命令。")
                print("  先装 Ollama（https://ollama.com/download），装完再跑一次。")
                return False
            cmd = [exe, "pull", d.repo]
            print(f"\n$ ollama pull {d.repo}")
            rc = subprocess.call(cmd)
            if rc != 0:
                print(f"  ollama pull 失败（exit {rc}）。可以重跑同一条命令。")
                return False
            continue

        if d.kind == "framework":
            print(f"\n  需要手动装（{d.size_gb:.1f} GB）：{d.repo}")
            print(f"    {d.note}")
            print("请使用图形化安装入口：python tools/studio.py")
            return False

        cmd = [sys.executable, str(ROOT / "tools" / "fetch_model.py"),
               "--repo", d.repo, "--dest", d.dest, "--threads", "8"]
        for pat in d.include:
            cmd += ["--include", pat]
        for pat in d.exclude:
            cmd += ["--exclude", pat]
        print(f"\n$ {' '.join(cmd)}")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"  下载失败（exit {rc}）。可以重跑同一条命令续传。")
            return False
    return True


def run_doctor() -> None:
    doc = ROOT / "tools" / "doctor.py"
    if doc.exists():
        print("\n环境自检：")
        subprocess.call([sys.executable, str(doc)])


# ────────────────────────────────────────────────────────────────
# 自定义
# ────────────────────────────────────────────────────────────────
def custom_flow(plugins, hw) -> dict | None:
    from backend.plugins.spec import registry

    reg = registry(reload=True)
    print("\n可选的出图模型（同显存下越靠前越稳）：")
    images = reg.of_kind("image")
    for i, p in enumerate(images, 1):
        print(f"  [{i}] {p.id:14} {p.label:26} 需要约 {p.vram_hint or '?'} GB 显存")
    print("  [0] 不装（沿用现有配置）")
    try:
        k = int(input("选出图模型: ").strip() or "0")
    except ValueError:
        k = 0
    img = images[k - 1].id if 1 <= k <= len(images) else ""

    print("\n可选的建模模型：")
    meshes = reg.of_kind("mesh")
    for i, p in enumerate(meshes, 1):
        print(f"  [{i}] {p.id:14} {p.label:26} 需要约 {p.vram_hint or '?'} GB 显存")
    print("  [0] 不装（沿用现有配置）")
    try:
        k = int(input("选建模模型: ").strip() or "0")
    except ValueError:
        k = 0
    mesh = meshes[k - 1].id if 1 <= k <= len(meshes) else ""

    if not img and not mesh:
        print("什么都没选，退出。")
        return None

    patch = {"backend": {"mode": "direct"}, "stages": {}, "models": {}}
    if img:
        patch["stages"]["image"] = {"default_model": img}
        patch["models"]["image"] = {img: {}}
    if mesh:
        patch["stages"]["3d"] = {"default_model": mesh}
        patch["models"]["3d"] = {mesh: {"octree": 192 if hw.vram_gb < 10 else 256}}
    print(f"\n自定义：出图={img or '不变'} 建模={mesh or '不变'}")
    print("提示：这些模型如果还没装，用 docs/MODELS.md 里的链接自己下；"
          "或者先选推荐方案让向导帮你下。")
    return patch


# ────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="铸想配置向导")
    ap.add_argument("--profile", default="", help="reuse / minimal / standard / full-comfy / cpu-only")
    ap.add_argument("--custom", action="store_true", help="自己选出图/建模模型")
    ap.add_argument("--yes", action="store_true", help="不确认，直接执行")
    ap.add_argument("--dry-run", action="store_true", help="只看方案，不写配置")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--comfy-dir", default="")
    args = ap.parse_args()

    from backend.backends import detect as D
    from backend.backends.recommend import (build_profiles, explain, pick_default,
                                            probe_hardware, summarize_assets)

    hw = probe_hardware(str(ROOT))
    comfy_dir = args.comfy_dir or os.environ.get("COMFYUI_DIR", "")
    fws = D.probe_all(comfy_dir=comfy_dir or None)
    assets = summarize_assets(fws)

    _line("═")
    print("铸想 ForgeForm · 配置向导")
    _line("═")
    print(explain(hw, assets, []))
    _line("═")
    print("\n选一套：\n")

    profiles = build_profiles(hw, assets)
    default = pick_default(profiles)
    show(profiles, default.key if default else "")

    # 自定义
    if args.custom:
        patch = custom_flow(None, hw)
        if patch is None:
            return 1
        if args.dry_run:
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return 0
        cfg = write_config(patch, Path(args.config))
        print(f"\n配置已写入 {cfg}")
        run_doctor()
        return 0

    # 指定方案
    if args.profile:
        hit = next((p for p in profiles if p.key == args.profile), None)
        if hit is None:
            print(f"没有这个方案：{args.profile}。可选：{', '.join(p.key for p in profiles)}")
            return 1
        picked = hit
    elif args.dry_run:
        # --dry-run 不交互：把每个方案的细节和命令全打出来
        for p in profiles:
            show_detail(p)
        print("（--dry-run，没有改动任何东西）")
        return 0
    else:
        hint = f"（回车默认 {default.name}）" if default else ""
        raw = input(f"选择 [1-{len(profiles)}]{hint}，或输入 c 自己配、q 退出: ").strip().lower()
        if raw in ("q", "quit"):
            return 0
        if raw in ("c", "custom"):
            patch = custom_flow(None, hw)
            if patch is None:
                return 1
            cfg = write_config(patch, Path(args.config))
            print(f"\n配置已写入 {cfg}")
            run_doctor()
            return 0
        if not raw and default:
            picked = default
        else:
            try:
                picked = profiles[int(raw) - 1]
            except (ValueError, IndexError):
                print("输入无效。")
                return 1

    show_detail(picked)
    if not picked.fits:
        print(f"⚠️ 这个方案在你这台机器上跑不动：{picked.reason}")
        if not args.yes:
            if input("还是要继续？[y/N] ").strip().lower() not in ("y", "yes"):
                return 1

    if args.dry_run:
        print("\n（--dry-run，没有改动任何东西）")
        return 0

    ok = run_downloads(picked, yes=args.yes)
    if not ok and picked.downloads:
        print("\n下载未完成，保留原配置。请使用图形化安装入口重试。")
        return 1

    cfg = write_config(picked.config_patch, Path(args.config))
    print(f"\n配置已写入 {cfg}")
    print(f"后端模式：{picked.backend_mode}   出图：{picked.image_model or '不变'}"
          f"   建模：{picked.mesh_model or '不变'}")
    run_doctor()
    print("\n下一步：python app.py  →  http://127.0.0.1:7860")
    return 0


if __name__ == "__main__":
    sys.exit(main())
