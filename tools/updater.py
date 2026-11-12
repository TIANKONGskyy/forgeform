# -*- coding: utf-8 -*-
"""把新版解压出来的内容合并进现有安装，保留用户数据。

    python tools/updater.py --from <解压出来的新版本目录> [--dry-run]

合并规则（这里错了就是灾难，所以写死在白名单里）：
  覆盖：源码、插件、文档、前端、脚本、requirements
  保留：config.json / outputs / models / .venv / logs / projects / temp

也就是说：程序更新，你的数据和配置不动。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些目录**永远不覆盖**：用户数据、下载的模型、虚拟环境
KEEP_DIRS = {".venv", "outputs", "models", "logs", "projects", "temp",
             ".git", "__pycache__", "node_modules", ".pytest_cache"}
# 这些文件**永远不覆盖**：配置与本地状态
KEEP_FILES = {"config.json", "config.json.bak", ".deps-ok"}

# 只认这些扩展名的文件做覆盖（其余一律跳过，避免误伤模型/图片等大文件）
COPY_SUFFIX = {".py", ".yaml", ".yml", ".md", ".txt", ".json", ".html", ".css",
               ".js", ".ps1", ".bat", ".toml", ".cfg", ".ini", ".gitignore",
               ".gitattributes", ".editorconfig"}
# 没有扩展名但必须跟着更新（VERSION 不在的话，更新完版本号还是旧的）
COPY_NAMES = {"VERSION", "LICENSE", "NOTICE", "CHANGELOG", "Makefile", "Dockerfile"}


def _rel_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p, p.relative_to(root)


def merge(src: Path, dst: Path, dry: bool = False) -> dict:
    copied, skipped_keep, skipped_type = [], [], []
    for f, rel in _rel_files(src):
        parts = rel.parts
        if any(part in KEEP_DIRS for part in parts[:-1]):
            skipped_keep.append(str(rel))
            continue
        if rel.name in KEEP_FILES:
            skipped_keep.append(str(rel))
            continue
        if f.suffix.lower() not in COPY_SUFFIX and f.name not in COPY_NAMES:
            skipped_type.append(str(rel))
            continue
        target = dst / rel
        if not f.resolve().is_relative_to(src.resolve()) or not target.resolve().is_relative_to(dst.resolve()):
            raise ValueError(f'更新路径超出安装目录：{rel}')
        if target.exists():
            try:
                if target.read_bytes() == f.read_bytes():
                    continue                      # 内容一样，不算改动
            except Exception:
                pass
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
        copied.append(str(rel))
    return {"copied": copied, "kept": skipped_keep, "skipped": skipped_type}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="新版本解压出来的目录")
    ap.add_argument("--to", dest="dst", default=str(ROOT), help="要合并进去的安装目录")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()

    # 解压出来往往多套一层 forgeform-vX.Y.Z/
    if not (src / "app.py").exists():
        subs = [d for d in src.iterdir() if d.is_dir() and (d / "app.py").exists()]
        if subs:
            src = subs[0]
        else:
            print(f"✗ {src} 里找不到 app.py，不像是铸想的版本目录")
            return 1

    if not (dst / "app.py").exists():
        print(f"✗ {dst} 不是铸想安装目录（没有 app.py）")
        return 1

    print(f"从: {src}")
    print(f"到: {dst}")
    print(f"{'（试运行，不写盘）' if args.dry_run else ''}\n")

    r = merge(src, dst, dry=args.dry_run)
    print(f"覆盖/新增 {len(r['copied'])} 个文件")
    for f in r["copied"][:20]:
        print(f"  + {f}")
    if len(r["copied"]) > 20:
        print(f"  … 还有 {len(r['copied']) - 20} 个")
    print(f"\n保留未动 {len(r['kept'])} 个（用户数据/配置）")
    for f in r["kept"][:8]:
        print(f"  · {f}")
    if len(r["kept"]) > 8:
        print(f"  … 还有 {len(r['kept']) - 8} 个")
    if r["skipped"]:
        print(f"\n跳过 {len(r['skipped'])} 个非源码文件")
    print("\n✓ 合并完成。依赖如果变了，重新跑一次 启动.bat 会自动装。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
