# -*- coding: utf-8 -*-
"""文件库：扫描输入/输出目录，生成实时文件列表"""
import os
import time
from pathlib import Path
from datetime import datetime

from backend.env import COMFYUI_DIR, OUTPUTS, TEMP

# 目录配置（跟随仓库根与 COMFYUI_DIR，见 backend/env.py）
INPUT_DIRS = [
    str(COMFYUI_DIR / "input"),    # ComfyUI 输入图片
    str(TEMP),                     # app 临时文件（上传的视频帧等）
]
OUTPUT_DIRS = {
    "🖼 图片": str(OUTPUTS / "images"),
    "🎬 视频": str(OUTPUTS / "videos"),
    "🧊 3D": str(OUTPUTS / "3d"),
    "📦 STL": str(OUTPUTS / "stl"),
}
COMFY_OUTPUT = str(COMFYUI_DIR / "output")

ICONS = {
    ".png": "🖼", ".jpg": "🖼", ".jpeg": "🖼", ".webp": "🖼", ".gif": "🖼",
    ".mp4": "🎬", ".webm": "🎬", ".mov": "🎬", ".avi": "🎬",
    ".glb": "🧊", ".gltf": "🧊", ".obj": "🧊", ".ply": "🧊",
    ".stl": "📦", ".3mf": "📦",
}


def _fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f}GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f}KB"
    return f"{n}B"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _icon_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ICONS.get(ext, "📄")


def _scan_dir(path: str, limit: int = 12) -> list:
    """扫描目录，返回 [(icon, name, size, time, fullpath)]，按时间倒序"""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    try:
        for f in p.iterdir():
            if f.is_file():
                try:
                    st = f.stat()
                    items.append((_icon_for(f.name), f.name, st.st_size, st.st_mtime, str(f)))
                except Exception:
                    pass
    except Exception:
        pass
    items.sort(key=lambda x: x[3], reverse=True)
    return items[:limit]


def _render_list(items: list, empty_text: str = "（空）") -> str:
    if not items:
        return f'<div class="flib-empty">{empty_text}</div>'
    parts = []
    for icon, name, size, ts, full in items:
        parts.append(
            f'<div class="flib-item" title="{full}">'
            f'<span class="flib-ic">{icon}</span>'
            f'<span class="flib-name">{name}</span>'
            f'<span class="flib-meta">{_fmt_size(size)} · {_fmt_time(ts)}</span>'
            f'</div>'
        )
    return "".join(parts)


def get_all_files() -> list:
    """返回所有输入+输出文件 [(name, path, type)]，按时间倒序
    type: image/video/3d/stl/other
    """
    TYPE_MAP = {
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
        ".mp4": "video", ".webm": "video", ".mov": "video", ".avi": "video",
        ".glb": "3d", ".gltf": "3d", ".obj": "3d", ".ply": "3d",
        ".stl": "stl", ".3mf": "stl",
    }
    all_items = []
    for d in INPUT_DIRS:
        for f in _scan_dir(d, limit=500):
            name, path = f[1], f[4]
            ext = Path(name).suffix.lower()
            all_items.append((name, path, TYPE_MAP.get(ext, "other"), f[2], f[3]))
    for d in OUTPUT_DIRS.values():
        for f in _scan_dir(d, limit=500):
            name, path = f[1], f[4]
            ext = Path(name).suffix.lower()
            all_items.append((name, path, TYPE_MAP.get(ext, "other"), f[2], f[3]))
    for f in _scan_dir(COMFY_OUTPUT, limit=500):
        name, path = f[1], f[4]
        ext = Path(name).suffix.lower()
        all_items.append((name, path, TYPE_MAP.get(ext, "other"), f[2], f[3]))
    # 去重（按路径）并按时间倒序
    seen = set()
    uniq = []
    for it in all_items:
        if it[1] not in seen:
            seen.add(it[1])
            uniq.append(it)
    uniq.sort(key=lambda x: x[4], reverse=True)
    return uniq


def render_file_library() -> str:
    """渲染输入/输出文件库 HTML"""
    # 输入库
    in_items = []
    for d in INPUT_DIRS:
        in_items.extend(_scan_dir(d, limit=30))
    in_items.sort(key=lambda x: x[3], reverse=True)
    in_items = in_items[:16]

    # 输出库（按类型分组 + ComfyUI 输出）
    out_groups = []
    for label, d in OUTPUT_DIRS.items():
        items = _scan_dir(d, limit=30)
        if items:
            out_groups.append((label, items))

    comfy_items = _scan_dir(COMFY_OUTPUT, limit=30)
    if comfy_items:
        out_groups.append(("⚙️ ComfyUI 输出", comfy_items))

    html = [
        '<div class="flib-wrap">',
        '<div class="flib-col">',
        '<div class="sh">📥 输入库</div>',
        f'<div class="flib-list">{_render_list(in_items, "暂无输入文件")}</div>',
        '</div>',
        '<div class="flib-col">',
        '<div class="sh">📤 输出库</div>',
    ]
    for label, items in out_groups:
        html.append(f'<div class="flib-group">{label}</div>')
        html.append(f'<div class="flib-list">{_render_list(items, "（空）")}</div>')
    html.append('</div></div>')
    return "".join(html)


GROUP_TYPES = ("image", "video", "3d", "stl")
GROUP_ICONS = {"image": "🖼", "video": "🎬", "3d": "🧊", "stl": "📦"}


def _group_name_from_file(name: str) -> str:
    """从文件名提取组名（去掉扩展名和序号后缀）"""
    stem = Path(name).stem
    stem = stem.replace("_optimized", "").replace("_print", "")
    for suf in ("_0", "_1", "_2", "_3", "_4", "_5", "_6", "_7", "_8", "_9"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    return stem


def get_groups() -> list:
    """把文件聚合成项目组
    返回 [{gid, name, cover, files: {type: [paths]}, mtime}]
    分组规则：
      1. batch_<n> 前缀 → 同组（批量队列产物）
      2. 其余按生成时间聚类：相邻文件时间差 < 120s 归一组
         （img_xxx + model_xxx + model_xxx_optimized 同组）
    """
    files = get_all_files()
    groups = {}
    order = []

    def _get_group(gid, name, mtime):
        if gid not in groups:
            groups[gid] = {"gid": gid, "name": name, "cover": "",
                           "files": {t: [] for t in GROUP_TYPES}, "mtime": mtime}
            order.append(gid)
        return groups[gid]

    # batch 组
    batch_files = [f for f in files if f[0].startswith("batch_")]
    other_files = [f for f in files if not f[0].startswith("batch_")]

    for name, path, ftype, size, mtime in batch_files:
        if ftype not in GROUP_TYPES:
            continue
        parts = name.split("_")
        gid = parts[0] + "_" + parts[1] if len(parts) > 1 else name
        g = _get_group(gid, gid, mtime)
        g["files"][ftype].append(path)
        if ftype == "image":
            g["cover"] = path
        elif not g["cover"] and ftype in ("3d", "stl"):
            g["cover"] = path
        g["mtime"] = max(g["mtime"], mtime)

    # 其他文件按时间聚类（升序处理，相邻差 < 120s 同组）
    other_files.sort(key=lambda f: f[4])
    cur_gid = None
    cur_mtime = 0
    for name, path, ftype, size, mtime in other_files:
        if ftype not in GROUP_TYPES:
            continue
        if cur_gid is None or mtime - cur_mtime > 120:
            stem = _group_name_from_file(name)
            cur_gid = "auto_" + stem + "_" + str(int(mtime))
            _get_group(cur_gid, stem, mtime)
        g = groups[cur_gid]
        g["files"][ftype].append(path)
        if ftype == "image":
            g["cover"] = path
        elif not g["cover"] and ftype in ("3d", "stl"):
            g["cover"] = path
        g["mtime"] = max(g["mtime"], mtime)
        cur_mtime = mtime

    result = []
    for gid in order:
        g = groups[gid]
        result.append(g)
    result.sort(key=lambda g: g["mtime"], reverse=True)
    return result


def group_cover_candidates() -> list:
    """返回组封面列表 [(cover_path, gname)] 用于 gr.Gallery"""
    out = []
    for g in get_groups():
        cover = g["cover"] or (g["files"]["3d"] or g["files"]["stl"] or [""])[0]
        if cover and Path(cover).exists():
            out.append((cover, g["name"]))
    return out
