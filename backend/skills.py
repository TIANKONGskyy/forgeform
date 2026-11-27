# -*- coding: utf-8 -*-
"""技能包加载器（2026-11-27）。

目的
----
把 `skills/<name>/SKILL.md` 里的「能力速查」压成一段很短的话，注入 agent 的
system prompt —— 让模型**不用读整篇文档**就知道自己能干什么、怎么调用、要多久。

为什么不用全量注入
------------------
整篇 SKILL.md 约 3–4 KB（≈1.2k token）。对话模型是本地小模型（3B 级）时，
每轮多 1.2k token 会明显变慢；只注入速查表（约 500 字）就够它选对路。
真正要细节时，模型可以按提示去读文件（agent 有 set_requirement 等动作，
人也可以直接问「技能文档怎么说」）。

约定
----
- 只认 `## 能力速查` 这一节（标题可加粗），取到下一个 `##` 为止
- 没有这一节就退化为 frontmatter 的 description
- 全部失败返回空串（调用方拼 prompt 时无副作用）
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
CACHE: dict[str, object] = {}


def _parse_front(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _section(text: str, title: str = "能力速查") -> str:
    """取 `## 能力速查` 到下一个二级标题之间的内容。"""
    # 标题可能带后缀，如「## 能力速查（先看这张表）」，所以要允许尾部任意文字
    pat = re.compile(rf"^#{{2,3}}\s*{title}[^\n]*$(.*?)(?=^#{{2,3}}\s|\Z)", re.S | re.M)
    m = pat.search(text)
    return (m.group(1).strip() if m else "")


def list_skills() -> list[dict]:
    """扫描 skills/*/SKILL.md，返回 [{name, description, path, quick}]。"""
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for f in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_front(text)
        out.append({
            "name": meta.get("name") or f.parent.name,
            "description": meta.get("description") or "",
            "version": meta.get("version") or "",
            "path": str(f),
            "quick": _section(text),
        })
    return out


def compact_map(max_chars: int = 1400) -> str:
    """给 agent 的能力速查（含缓存；文件改了按 mtime 失效）。"""
    try:
        sig = tuple((str(f), f.stat().st_mtime) for f in sorted(SKILLS_DIR.glob("*/SKILL.md")))
    except Exception:
        sig = ()
    if CACHE.get("sig") == sig and "text" in CACHE:
        return str(CACHE["text"])

    parts = []
    for s in list_skills():
        if s["quick"]:
            parts.append(f"### {s['name']}（v{s['version']}）\n{s['quick']}")
        elif s["description"]:
            parts.append(f"### {s['name']}\n{s['description']}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n…（完整技能文档见 skills/）"
    CACHE["sig"], CACHE["text"] = sig, text
    return text


def prompt_block() -> str:
    """拼进 system prompt 的段落（空技能库时返回空串）。"""
    text = compact_map()
    if not text:
        return ""
    return ("\n\n【你的能力（速查）】\n" + text +
            "\n\n用法：用户要某件具体的事，直接用对应动作/函数；"
            "参数不清就先问一句（尺寸/颜色/用哪张图）。")
