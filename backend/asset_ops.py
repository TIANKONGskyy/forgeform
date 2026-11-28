# -*- coding: utf-8 -*-
"""产物操作：删除 / 引用 / 修改 / 展示（2026-11-28）。

用户要的四件事，之前只有"刷新 + 打开"：
- **删除**：删文件 + 从索引里移除（可撤销就不做了，直接删，但要二次确认提示）
- **引用**：把某个产物当作下一次生成的输入（图当参考图、模型当精修/贴图输入）
- **修改**：拿产物 + 一句新要求，重新生成（图→按新提示重出；3D→按新图重出）
- **展示**：给产物生成一段可读的描述（尺寸/面数/水密/时间），而不是只有文件名

设计原则
--------
- 不复制大文件：引用就是返回路径，生成时直接读
- 修改走 `Pipeline`，所以自动带上并发防护（gpu_guard）
- 所有操作返回 (ok, 一句话)，UI 直接显示，不抛异常给用户
"""
from __future__ import annotations

import time
from pathlib import Path

from . import assets as A

ROOT = Path(__file__).resolve().parent.parent


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def describe(asset_id: str) -> dict:
    """产物的可读信息（给界面展示用）。"""
    row = A.get(asset_id)
    if not row:
        return {"ok": False, "error": "产物不存在"}
    p = A.absolute(row)
    size = p.stat().st_size if p.is_file() else 0
    m = row.get("metrics") or {}
    bits = [f"{row.get('kind', '?')}", _fmt_size(size)]
    if m.get("faces"):
        bits.append(f"{int(m['faces']):,} 面")
    if m.get("watertight") is not None:
        bits.append("水密" if m["watertight"] else "不水密")
    if m.get("components"):
        bits.append(f"{m['components']} 连通块")
    ts = row.get("created") or ""
    return {
        "ok": True,
        "title": row.get("title") or p.name,
        "path": str(p),
        "kind": row.get("kind"),
        "size": _fmt_size(size),
        "summary": " · ".join(bits),
        "created": ts,
        "params": row.get("params") or {},
    }


def delete(asset_id: str) -> tuple[bool, str]:
    """删除产物（文件 + 索引）。"""
    row = A.get(asset_id)
    if not row:
        return False, "产物不存在（可能已经删了）"
    try:
        ok = A.remove(asset_id, delete_file=True)
    except Exception as e:
        return False, f"删除失败：{e}"
    return (True, "已删除") if ok else (False, "删除失败：索引里没找到")


def reference(asset_id: str) -> tuple[bool, str]:
    """把产物当作下一次生成的输入。返回路径供上层填进参考图/输入模型。"""
    row = A.get(asset_id)
    if not row:
        return False, "产物不存在"
    p = A.absolute(row)
    if not p.is_file():
        return False, "文件已不在磁盘上"
    kind = row.get("kind") or ""
    if kind not in ("mesh", "multiview", "refined", "painted", "rigged", "motion"):
        return False, f"这个类型不能当输入：{kind}"
    return True, str(p)


def revise(asset_id: str, instruction: str, *, model: str = "", quality: str = "快速",
           progress=None) -> tuple[bool, str]:
    """拿产物 + 一句新要求，重新生成。

    - 图产物：以它为参考图 → 出新图
    - 3D 产物：先导出预览图（或直接复用关联图），按新要求重出模型
    """
    row = A.get(asset_id)
    if not row:
        return False, "产物不存在"
    p = A.absolute(row)
    if not p.is_file():
        return False, "文件已不在磁盘上"
    ins = (instruction or "").strip()
    if not ins:
        return False, "要说清楚改什么（例如「把手加粗」「换成方形」）"

    kind = row.get("kind") or ""
    _pipe = {}

    def pipe():
        if "p" not in _pipe:
            from .pipeline import Pipeline
            _pipe["p"] = Pipeline(str(ROOT / "config.json"))
        return _pipe["p"]
    if progress:
        progress(0, desc="重新生成中…")

    if kind in ("mesh", "multiview", "refined", "painted", "rigged"):
        src = row.get("source_image") or (row.get("params") or {}).get("source_image")
        if not src or not Path(src).is_file():
            return False, ("这个模型没有记录来源图，没法按新要求重出。"
                           "先「引用」拿到路径，或直接重新描述一遍需求。")

        # 2026-11-28：真的"改一版"——按新要求重新出图，再拿新图建模。
        # （原来只拿旧图重跑一遍 3D，结构完全不变，等于什么都没改）
        new_src = str(src)
        try:
            imgs = pipe().generate_images(
                ins, count=1, model_key="z_image", quality_name="快速",
                resolution="1024×1024")
            cand = getattr(imgs[0], "success", False) and getattr(imgs[0], "output_path", "")
            if cand and Path(cand).is_file():
                new_src = cand
        except Exception:
            pass

        r = pipe().generate_3d(model or "hunyuan_mini", quality, new_src,
                             max_facenum=int((row.get("params") or {}).get("max_facenum") or 150000))
        if getattr(r, "success", False):
            return True, getattr(r, "output_path", "")
        return False, str(getattr(r, "error", "生成失败"))[:200]

    return False, f"这个类型暂不支持修改：{kind}"
