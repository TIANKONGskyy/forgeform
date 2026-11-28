# -*- coding: utf-8 -*-
"""模型管理面板（2026-11-28）。

用户要的三件事：**单独下载某个模型 / 切换默认模型 / 删掉不用的模型**。
之前只能整套 `bootstrap --only <suite>`，删模型得自己进目录删文件。

这个面板挂在右列，很小一块：

    [模型管理]
    模型：Z-Image Turbo 6B   ✓ 已装 5.8 GB      ← 下拉
    [设为默认] [下载] [删除]                   ← 三个按钮
    已装 1/10 · 占 24.3 GB · 剩 460 GB
"""
from __future__ import annotations

import html

from . import model_manager as mm


def choices() -> list[str]:
    """下拉选项：'类型/键 · 名字'（值里带类型，避免 image/3d 撞名）。"""
    out = []
    for r in mm.list_models():
        flag = "✓" if r["installed"] else "·"
        out.append(f"{flag} {r['kind']}/{r['key']} · {r['name'][:20]}")
    return out


def parse_choice(choice: str) -> tuple[str, str]:
    """把下拉文本还原成 (kind, key)。"""
    s = str(choice or "").strip().lstrip("✓·").strip()
    head = s.split("·")[0].strip()
    if "/" not in head:
        raise ValueError(f"选项格式不对：{choice!r}")
    kind, key = head.split("/", 1)
    return kind.strip(), key.strip()


def panel_html(selected: str | None = None) -> str:
    u = mm.usage()
    rows = mm.list_models()
    if selected:
        try:
            kind, key = parse_choice(selected)
            row = next((r for r in rows if r["kind"] == kind and r["key"] == key), None)
        except Exception:
            row = None
    else:
        row = next((r for r in rows if r["installed"]), None)

    if row:
        state = (f"✓ 已装 {row['size_gb']:.1f} GB" if row["installed"] else "· 未安装")
        title = f"{row['name']}（{row['kind']}）"
    else:
        state, title = "· 没有可选模型", "—"

    return (
        '<div class="ff-models">'
        '<div class="ff-models-title">模型管理</div>'
        f'<div class="ff-models-name">{html.escape(title)}</div>'
        f'<div class="ff-models-state">{html.escape(state)}</div>'
        f'<div class="ff-models-usage">已装 {u["installed"]}/{u["total"]} · '
        f'占 {u["disk_gb"]:.1f} GB · 剩 {u["free_gb"]:.0f} GB</div>'
        "</div>"
    )


def do_switch(choice: str) -> str:
    try:
        kind, key = parse_choice(choice)
        mm.switch(kind, key)
        return f"✓ 默认模型已切到 {kind}/{key}"
    except Exception as e:
        return f"✗ 切换失败：{e}"


def do_delete(choice: str) -> str:
    try:
        kind, key = parse_choice(choice)
        r = mm.delete(kind, key)
        if not r.get("ok"):
            return f"✗ 删除失败：{r.get('error')}"
        return f"✓ 已删 {kind}/{key}，释放 {r['freed_gb']:.1f} GB"
    except Exception as e:
        return f"✗ 删除失败：{e}"


def do_download(choice: str, progress=None) -> str:
    """下载单个模型：映射到 bootstrap 套件（image / direct）。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    try:
        kind, key = parse_choice(choice)
    except Exception as e:
        return f"✗ {e}"
    suite = {"image": "image", "3d": "direct"}.get(kind)
    if not suite:
        return f"✗ 不支持的模型类型：{kind}"
    if progress:
        progress(0, desc=f"下载 {key}（{suite} 套件）…")
    p = subprocess.run([sys.executable, str(root / "tools" / "bootstrap_models.py"),
                        "--only", suite], capture_output=True, text=True, timeout=7200)
    tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
    if p.returncode == 0:
        return f"✓ {key} 就位（{suite} 套件完成）"
    return f"✗ 下载失败：{tail[0][:160]}"
