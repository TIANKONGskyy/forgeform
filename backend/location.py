# -*- coding: utf-8 -*-
"""安装位置自动选择：**挑一个能装、装得下、路径不出幺蛾子的地方**。

## 为什么不能"就装在你解压的地方"

实测遇到的四种翻车：

1. **路径带中文** —— 某些 C++ 扩展（torch 的 DLL、mesh 处理库）按 ANSI 打开文件，
   中文目录直接失败，报错还特别难看懂。
2. **装在 `C:\\Program Files`** —— 没有写权限，pip 装到一半 `PermissionError`。
3. **磁盘不够** —— 完整方案 15–25 GB，C 盘经常只剩 20 GB，装到 80% 炸。
4. **路径太深** —— 超 260 字符解压失败（Windows 长路径默认关闭）。

所以装之前先打分，挑一个合适的根目录；模型单独挑（模型是大头，20+ GB）。

## 打分规则

    + 剩余空间（越大越好，封顶 200 分）
    - 非纯 ASCII 路径                -100
    - C:\\Windows / Program Files    -100 / -60
    - 路径层级 > 2                   -5
    - 剩余 < 30 GB                   -200（基本等于否决）

## 用法

    from backend import location as L
    L.report()            # 当前在哪、推荐在哪、为什么
    L.models_root()       # 模型放哪（自动挑剩余空间最大的盘）
    L.apply()             # 把推荐位置写进 config.json 的 paths
"""
from __future__ import annotations

import json
import os
import shutil
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 模型要留的余量：完整方案 14.4 GB + 下载临时文件 + 余量
NEED_GB = 30.0
# 目录名固定英文，避免中文路径问题
APP_DIR_NAME = "ForgeForm"


def drives() -> list[Path]:
    """所有存在的盘根。Windows 用 A–Z 探测，其他平台只给 /。"""
    out: list[Path] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            p = Path(f"{letter}:/")
            try:
                if p.exists():
                    out.append(p)
            except OSError:
                continue
    else:
        out.append(Path("/"))
    return out


def free_gb(path: Path) -> float:
    """剩余空间。**目录还不存在时沿父目录往上找** —— 否则给一个"准备创建"的
    路径算空间会永远返回 0（第一版就踩了这个，推荐里写"剩 0 GB"）。"""
    cur = Path(path)
    for _ in range(6):
        try:
            if cur.exists():
                return round(shutil.disk_usage(str(cur)).free / 1024 ** 3, 1)
        except Exception:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    try:
        return round(shutil.disk_usage(str(path.anchor or "/")).free / 1024 ** 3, 1)
    except Exception:
        return 0.0


def _is_ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def score(path: Path) -> tuple[float, list[str]]:
    """给一个候选根目录打分，并给出理由（理由要能直接念给用户听）。"""
    s = 0.0
    why: list[str] = []
    gb = free_gb(path)
    s += min(200.0, gb * 2)
    why.append(f"剩 {gb:.0f} GB")
    low = str(path).lower().replace("\\", "/")
    if not _is_ascii(path):
        s -= 100
        why.append("路径含中文（会让部分 C++ 库打不开文件）")
    if "windows" in low:
        s -= 100
        why.append("系统目录")
    if "program files" in low:
        s -= 60
        why.append("Program Files 无写权限")
    depth = len([p for p in Path(path).parts if p not in ("\\", "/")])
    if depth > 2:
        s -= 5 * (depth - 2)
        why.append("路径偏深")
    if gb < NEED_GB:
        s -= 200
        why.append(f"不足 {NEED_GB:.0f} GB，装不下完整方案")
    # 当前所在盘优先（省一次跨盘拷贝）
    try:
        if Path(path).drive and Path(path).drive == ROOT.drive:
            s += 15
            why.append("和当前目录同盘")
    except Exception:
        pass
    return s, why


def candidates() -> list[dict]:
    """所有候选（盘根下的 ForgeForm 目录）。"""
    out = []
    for d in drives():
        cand = d / APP_DIR_NAME
        s, why = score(cand)
        out.append({"path": str(cand), "drive": str(d), "score": round(s, 1),
                    "free_gb": free_gb(d), "why": why})
    out.sort(key=lambda x: -x["score"])
    return out


def report() -> dict:
    cur = ROOT
    rows = candidates()
    best = rows[0] if rows else None
    cur_state = {
        "path": str(cur),
        "free_gb": free_gb(cur),
        "ascii": _is_ascii(cur),
        "depth": len([p for p in cur.parts if p not in ("\\", "/")]),
    }
    problems = []
    if not cur_state["ascii"]:
        problems.append("当前路径含中文")
    if cur_state["free_gb"] < NEED_GB:
        problems.append(f"当前盘只剩 {cur_state['free_gb']:.0f} GB")
    return {"current": cur_state, "recommended": best, "candidates": rows[:5],
            "problems": problems,
            "advice": (f"建议装到 {best['path']}（{', '.join(best['why'][:3])}）"
                       if best else "找不到合适的盘")}


def models_root() -> str:
    """模型放哪。优先当前盘（够用就不折腾），不够就挑剩余空间最大的。"""
    cur_models = os.environ.get("FORGEFORM_MODELS_DIR", "")
    if cur_models:
        return cur_models
    if free_gb(ROOT) >= NEED_GB:
        return str(ROOT / "models")
    rows = candidates()
    for r in rows:
        if r["score"] > 0 and r["free_gb"] >= NEED_GB:
            return str(Path(r["path"]) / "models")
    return str(ROOT / "models")


def apply(config_path: str | None = None) -> dict:
    """把自动选出的模型目录写进 config.json（只在确定更好时才改）。"""
    p = Path(config_path) if config_path else (ROOT / "config.json")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    except Exception:
        cfg = {}
    paths = cfg.setdefault("paths", {})
    want = models_root()
    old = str(paths.get("models") or "")
    changed = False
    if want and want != old:
        # 只有"新的确实更空"才改，避免每次启动都抖动
        if not old or free_gb(Path(old).parent if Path(old).exists() else ROOT) < NEED_GB:
            paths["models"] = want
            changed = True
    if changed:
        try:
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            changed = False
    return {"changed": changed, "models": str(paths.get("models") or ""),
            "old": old, "why": report()["advice"]}
