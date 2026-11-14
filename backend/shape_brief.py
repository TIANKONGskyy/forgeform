# -*- coding: utf-8 -*-
"""把口语化的形状描述翻译成**可验证的几何约束**。

## 为什么需要这个

人建模时会做标注：「六边形」不是"看着像个六边形"，而是
**六条边等长、六个内角 120°、封闭**。3D 模型看到的只是一张图，
它没法验证"像不像"，只能照图推。所以提示词里必须把
**人能验证的性质**写出来，模型才有东西可对。

反例（实测）：用户写「六边形法兰盘」→ 出图经常是五边或七边，
因为"六边形"是个**类别词**，不是约束。写成
「six equal-length edges, six 120° interior angles, closed polygon」
之后出图就是六边。

## 这个模块干什么

1. **词典**：常见几何体 → 精确约束（边的数量/等长、角度、垂直平行、对称轴）
2. **部件拆分**：把物体拆成「主体 / 附属 / 连接」三层，每层单独描述
3. **关系标注**：谁在谁上面、谁穿过谁、谁和谁同轴
4. **尺寸锚定**：用户给了数字就用数字，没给就用**相对比例**（1:2.5）而不是"大/小"

## 怎么用

    brief = shape_brief("一个六边形法兰盘，6 个螺栓孔")
    # → 返回一段可以直接拼进 IMAGE 段的结构化描述

## 边界（老实说）

这个模块**不能保证**出图就精确 —— 扩散模型对"六个孔"这种计数约束仍然
会出错。它的作用是**把约束明确写出来**，让成功的概率显著提高，并且
失败时**一眼能看出错在哪**（"要 6 个孔给了 5 个"）。
"""
from __future__ import annotations

import re

# ── 几何词典 ──────────────────────────────────────────────────
# 键是中文/英文里常见的说法，值是**可验证的约束描述**（英文，直接进提示词）。
# 只收那些"说出来就能验"的：边数、等长、角度、平行/垂直、对称。
SHAPE_LEXICON: dict[str, str] = {
    # 正多边形
    "三角形": "a closed polygon with exactly 3 equal-length straight edges and 3 interior angles of 60 degrees",
    "正三角形": "a closed polygon with exactly 3 equal-length straight edges and 3 interior angles of 60 degrees",
    "正方形": "a closed polygon with exactly 4 equal-length edges and 4 right angles (90 degrees), opposite sides parallel",
    "四边形": "a closed polygon with exactly 4 straight edges, opposite sides parallel",
    "五边形": "a closed regular polygon with exactly 5 equal-length edges and 5 interior angles of 108 degrees",
    "六边形": "a closed regular polygon with exactly 6 equal-length edges and 6 interior angles of 120 degrees, opposite edges parallel",
    "八边形": "a closed regular polygon with exactly 8 equal-length edges and 8 interior angles of 135 degrees",
    "多边形": "a closed polygon with straight edges; count them and make every edge meet at a vertex",
    # 圆 / 旋转体
    "圆形": "a perfect circle: every point on the outline is the same distance from the center",
    "圆柱": "two identical circular faces of equal diameter at both ends, connected by a side surface perpendicular to both, with a single shared axis",
    "圆锥": "one circular base and one apex exactly above its center, with a straight line from apex to every point on the base rim",
    "球形": "a perfect sphere: every point on the surface is the same distance from the center in all three axes",
    "圆环": "a torus: a circular tube of constant thickness bent into a closed ring, with a single circular axis of rotational symmetry",
    "圆盘": "a flat cylinder whose diameter is much larger than its thickness; both faces are parallel perfect circles",
    "法兰": "a flat circular disc with a raised central hub, a central through-bore, and bolt holes arranged on a circle concentric with the bore",
    # 柱体
    "立方体": "a box with exactly 6 identical square faces, 12 equal-length edges and 8 vertices, all faces at right angles",
    "长方体": "a box with 6 rectangular faces in 3 pairs of identical opposite faces, all adjacent faces at right angles",
    "棱柱": "a prism: two identical polygonal end faces parallel to each other, connected by parallelogram side faces of equal height",
    "棱锥": "a pyramid: one polygonal base and one apex; every base vertex connects to the apex by a straight edge",
    # 常见零件
    "螺栓孔": "cylindrical through-holes with a clean vertical wall, identical diameter, evenly spaced",
    "螺纹": "a continuous helical ridge around a cylindrical core, constant pitch and depth",
    "倒角": "a flat chamfer at 45 degrees breaking every sharp edge, constant width",
    "齿轮": "a cylindrical body with teeth of identical profile and equal spacing around the whole circumference",
}

# 数值归一：用户写"3mm"就用 3mm，没写就用比例
_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米|米)\b", re.I)
_COUNT_RE = re.compile(r"(\d+)\s*(个|条|根|片|块|颗|只|孔|边|面)")

# 在建模语境里没有信息量、会稀释约束的词
_EMPTY_WORDS = (
    "beautiful", "amazing", "stunning", "gorgeous", "high quality",
    "best quality", "masterpiece", "ultra detailed", "8k", "4k",
    "photorealistic", "cinematic", "award winning", "trending on artstation",
    "漂亮", "精美", "高级感", "质感好", "大片感",
)


def _found_shapes(text: str) -> list[tuple[str, str]]:
    """找出文本里提到、且词典里有精确约束的形状。"""
    out = []
    for key, constraint in SHAPE_LEXICON.items():
        if key in text:
            out.append((key, constraint))
    # 长词优先（"正三角形" 命中后不该再命中 "三角形"）
    out.sort(key=lambda kv: -len(kv[0]))
    seen, kept = set(), []
    for k, v in out:
        if v in seen:
            continue
        seen.add(v)
        kept.append((k, v))
    return kept


def _numbers(text: str) -> list[str]:
    """抽出手数性信息：尺寸和数量。这些必须原样保留。"""
    got = [f"{m.group(1)}{m.group(2)}" for m in _UNIT_RE.finditer(text)]
    got += [f"{m.group(1)}{m.group(2)}" for m in _COUNT_RE.finditer(text)]
    return got


def shape_brief(text: str, max_len: int = 900) -> str:
    """生成结构化几何简报。返回空串表示"这段话里没有可约束的几何信息"。

    输出格式（英文，直接可以拼进 IMAGE 段）：

        GEOMETRY: <形状词典给的精确约束，按出现顺序>
        PARTS: <数量/尺寸锚点，原样保留>
        RELATIONS: <部件之间的关系约束>
        RULES: <通用建模约束：封闭、无浮空、尺寸一致>
    """
    if not text or not text.strip():
        return ""

    shapes = _found_shapes(text)
    nums = _numbers(text)

    lines = []

    if shapes:
        lines.append("GEOMETRY — count and verify these, do not approximate:")
        for name, constraint in shapes[:4]:
            lines.append(f"  - {name}: {constraint}")

    if nums:
        lines.append("EXACT VALUES — keep these numbers unchanged, do not round:")
        lines.append("  - " + ", ".join(dict.fromkeys(nums)))

    # 关系约束：不管什么物体都成立，但扩散模型经常忘
    lines.append("RELATIONS — every pair of parts must satisfy these:")
    lines.append("  - symmetric parts are mirror images about one axis, same size")
    lines.append("  - repeated features are evenly spaced on a shared circle or line")
    lines.append("  - attached parts intersect the body, never float above it")
    lines.append("  - coaxial parts share one center line")

    lines.append("RULES — non-negotiable:")
    lines.append("  - the whole object is one closed solid, no open shells, no floating pieces")
    lines.append("  - identical features have identical size (do not vary hole diameter)")
    lines.append("  - view is orthographic front-ish (three-quarter) with the full silhouette visible")

    brief = "\n".join(lines)
    return brief[:max_len]


def strip_empty_words(text: str) -> str:
    """去掉没有信息量的形容词。保留原句结构，只删词。"""
    out = text
    for w in _EMPTY_WORDS:
        out = re.sub(re.escape(w), "", out, flags=re.I)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s*[,，]\s*(?=[,，])", "", out)
    return out.strip(" ,，")


def count_check(text: str, rendered: str) -> list[str]:
    """便宜的自检：提示词里说要 N 个，出图描述里是不是也说了 N 个。

    这不是像素级验证，只是抓"扩写时把 6 个孔写成 several holes"这种**丢数字**的错。
    """
    problems = []
    want = _numbers(text)
    for n in want:
        if n not in rendered:
            problems.append(f"数字 {n} 在扩写结果里丢了")
    for w in _EMPTY_WORDS:
        if w.lower() in rendered.lower():
            problems.append(f"扩写里出现了空话词 {w}")
    return problems
