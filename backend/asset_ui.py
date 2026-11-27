# -*- coding: utf-8 -*-
"""产物库界面（2026-11-27）。

左侧常驻一列「产物库」：按类型分组，显示大小/指标/时间，点一下就在查看器里打开。
另一份是「📦 产物库」整页视图（网格，信息更全）。

视觉约定（跟项目整体一致）
--------------------------
- 主色 violet（和 Gradio Soft + 现有 CSS 一致），不用花哨配色
- 卡片：深色半透明底 + 1px 边 + 类型色左边条，不加投影（投影会显脏）
- 图标用固定一组字形，不混 emoji 风格
"""
from __future__ import annotations

from pathlib import Path

KIND_META = {
    "mesh": ("模型", "◇", "#8b5cf6"),
    "multiview": ("多视图", "◈", "#06b6d4"),
    "refined": ("精修", "✦", "#f59e0b"),
    "painted": ("贴图", "◐", "#ec4899"),
    "rigged": ("骨骼", "⋔", "#22c55e"),
    "motion": ("动画", "▶", "#3b82f6"),
}


def _size(n: int) -> str:
    n = int(n or 0)
    if n >= 1024 ** 3:
        return f"{n/1024**3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024:.0f} KB"


def _metric(m: dict) -> str:
    m = m or {}
    bits = []
    if m.get("faces"):
        bits.append(f"{int(m['faces']):,}面")
    if m.get("watertight") is not None:
        bits.append("水密" if m["watertight"] else "非水密")
    if m.get("textures"):
        bits.append(f"{m['textures']}贴图")
    if m.get("joints"):
        bits.append(f"{m['joints']}骨")
    if m.get("animations"):
        bits.append(f"{m['animations']}动作")
    return " · ".join(bits)


def _rows(limit: int = 60) -> list[dict]:
    try:
        from . import assets
        return assets.list_assets(limit=limit)
    except Exception:
        return []


def sidebar_html(limit: int = 40) -> str:
    """左列内容：紧凑列表（每条一行，类型色条 + 名称 + 大小）。"""
    rows = _rows(limit)
    try:
        from . import assets
        use = assets.usage()
        head = (f'<div class="ast-head"><span class="ast-title">产物库</span>'
                f'<span class="ast-count">{use["count"]} 件 · {use["mb"]:.0f} MB</span></div>')
    except Exception:
        head = '<div class="ast-head"><span class="ast-title">产物库</span></div>'
    if not rows:
        return head + ('<div class="ast-empty">还没有产物。<br>生成模型后会自动出现在这里。</div>')
    items = []
    for r in rows:
        label, glyph, color = KIND_META.get(r.get("kind"), ("其他", "·", "#64748b"))
        metric = _metric(r.get("metrics") or {})
        items.append(
            f'<div class="ast-item" style="--k:{color}">'
            f'<span class="ast-glyph">{glyph}</span>'
            f'<span class="ast-body"><span class="ast-name">{r.get("title") or "未命名"}</span>'
            f'<span class="ast-meta">{label} · {_size(r.get("bytes"))}'
            f'{" · " + metric if metric else ""}</span></span></div>')
    return head + '<div class="ast-list">' + "".join(items) + "</div>"


def page_html(limit: int = 200) -> str:
    """整页视图：按类型分组卡片。"""
    rows = _rows(limit)
    if not rows:
        return ('<div class="ast-page-empty">产物库还是空的。在对话里生成一个模型，'
                '或者到「🛠 能力台」跑一次骨骼/贴图/精修。</div>')
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("kind", "mesh"), []).append(r)
    out = []
    for kind, items in groups.items():
        label, glyph, color = KIND_META.get(kind, ("其他", "·", "#64748b"))
        cards = []
        for r in items:
            metric = _metric(r.get("metrics") or {})
            cards.append(
                f'<div class="ast-card" style="--k:{color}">'
                f'<div class="ast-card-top"><span class="ast-glyph">{glyph}</span>'
                f'<span class="ast-card-name">{r.get("title") or "未命名"}</span></div>'
                f'<div class="ast-card-meta">{_size(r.get("bytes"))}'
                f'{" · " + metric if metric else ""}</div>'
                f'<div class="ast-card-time">{r.get("created")}</div></div>')
        out.append(f'<section class="ast-group"><h3><span style="--k:{color}">{glyph}</span>'
                   f'{label}<em>{len(items)}</em></h3>'
                   f'<div class="ast-grid">{"".join(cards)}</div></section>')
    return "".join(out)


def choices(limit: int = 40) -> list[tuple[str, str]]:
    """给下拉用：(显示名, 绝对路径)。"""
    out = []
    for r in _rows(limit):
        try:
            from . import assets
            p = assets.absolute(r)
            if p.is_file():
                out.append((f'{r.get("title") or "未命名"} · {KIND_META.get(r.get("kind"), ("",))[0]}', str(p)))
        except Exception:
            continue
    return out
