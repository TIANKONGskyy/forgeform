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

# ── 曲面词典：把"文科"的描述翻成几何参数 ───────────────────────
# 用户说"柔和的褶皱""飘逸的发丝"，这些话对建模没有约束力。
# 但每一句背后其实都对应一组几何量：
#   褶皱 = 周期性起伏（波长 λ、峰谷高差 a、折角 θ、走向 v）
#   花纹 = 曲面上的重复母题（周期 p、对称群、振幅 a）
#   发丝 = 管状细长体（半径 r、分组数 n、弯曲曲率半径 R）
# 词典的输出是**两段**：先给几何参数（模型能验），再给一句文学描述
# （出图模型对自然语言更敏感）。两者缺一不可 —— 只有几何会干瘪，
# 只有文学会飘。
SURFACE_LEXICON: dict[str, str] = {
    "褶皱": "periodic surface folds: alternating ridges and grooves of constant wavelength "
            "(equal distance between crests), constant fold amplitude, fold direction "
            "consistent along the whole garment, depth tapering toward seams — "
            "reads as soft fabric draping, not rigid pleats",
    "折痕": "sharp creases along straight lines: two flat faces meeting at a V-shaped edge, "
            "constant dihedral angle along the crease, crease lines do not curve — "
            "reads as sharp paper/board folds",
    "百褶": "knife pleats: identical flat panels folded at equal angular intervals around "
           "one axis, every pleat same width and same fold angle, edges parallel — "
           "reads as a crisp school-uniform skirt",
    "发丝": "tube-like hair strands: individual strands of constant radius, grouped into tufts, "
            "each tuft bending along one smooth curvature, strand tips taper to a point, "
            "groups separated by visible grooves — "
            "reads as layered hair with clear highlight and shadow bands",
    "卷发": "helical hair curls: each curl is a tube wound along a helix of constant pitch "
            "and constant radius, adjacent curls do not intersect — "
            "reads as bouncy ringlets",
    "花纹": "repeated surface motif: identical unit repeated on a regular grid, constant "
            "period in both directions, motif raised a small constant height above the base "
            "surface, motif edges closed — reads as an embossed ornamental pattern",
    "鳞片": "overlapping scale plates: identical plates tiled in staggered rows, each plate "
            "overlaps the one below by a constant fraction, plates curve along the body — "
            "reads as reptilian/fish scale armour",
    "编织": "interlaced strands: two sets of bands crossing at a constant angle, "
            "over-under alternation, constant band width and constant gap — "
            "reads as woven basketry/knit texture",
    "镂空": "through-cut openings in a plate: identical openings on a regular grid, "
            "every opening fully pierces the wall with clean vertical inner walls, "
            "remaining material forms a continuous connected lattice — "
            "reads as pierced filigree",
    "浮雕": "raised relief on a flat base: motif protrudes a constant depth above the base "
            "plane, all relief edges chamfered, background stays flat — "
            "reads as a carved bas-relief plaque",
    "波纹": "sinusoidal surface waves: one-directional periodic undulation of constant "
            "wavelength and constant amplitude, phase continuous across the surface — "
            "reads as flowing water ripples",
    "扭曲": "twisted prism: cross-section unchanged, rotated progressively about the long axis "
            "at a constant rate from one end to the other — "
            "reads as a spiralling bar",
    "流线": "streamlined surfaces: curvature changes continuously with no abrupt direction "
            "reverse, no flat spots, all transitions tangential — "
            "reads as aerodynamic forms",
}


def _found_surfaces(text: str) -> list[tuple[str, str]]:
    out = []
    for key, desc in SURFACE_LEXICON.items():
        if key in text:
            out.append((key, desc))
    out.sort(key=lambda kv: -len(kv[0]))
    return out


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

    surfaces = _found_surfaces(text)
    if surfaces:
        lines.append("SURFACES — describe these as geometry, then in plain words:")
        for name, desc in surfaces[:3]:
            lines.append(f"  - {name}: {desc}")

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

# ── 深度线索与细节密度检查（2026-11-14，实测得出）─────────────
#
# 三组对照实验（Hunyuan3D 2.1 / 512 octree，同一张出图模型）：
#
#   ① 正面站姿 + 12 道细褶 + 20 根细卷发  → 厚度 40.3%，卷发糊成坨
#   ② 正面站姿 +  6 道深褶 +  6 束粗卷发  → 厚度  0.7%，**整张纸片**
#   ③ 四分之三侧面 + 手前伸 + 6 道深褶 + 6 束粗卷发
#                                          → 厚度 56.3%，水密，细节都在
#
# 两个结论：
#   A. **姿势 > 细节**。正面站姿（左右对称、无前后遮挡）会让模型输出纸片，
#      细节放得再大也救不回来 —— 深度信息一开始就没了。
#   B. 细节尺寸必须大于面片尺寸。200k 面 / 2 单位高 → 平均面片边长约 5-6mm
#      （按 80mm 打印算），细节间距小于这个数就表达不出来。

# 表示"有深度线索"的说法
_DEPTH_CUES = (
    "侧面", "斜侧", "四分之三", "半侧", "侧身", "侧对",
    "three-quarter", "3/4 view", "side view", "profile",
    "向前伸", "前伸", "伸手", "伸出手", "hand extended",
)
# 制造前后遮挡的常见部件
_OCCLUDERS = ("背包", "双肩包", "书包", "挎包", "handbag", "backpack", "斜挎", "手提")

# 只有人/动物/角色才有"手"。机械件、器皿加一句"一只手向前伸出"是荒谬的
# （实测：给"机械法兰盘"补深度线索时也加上了手，必须挡掉）。
_LIVING = ("角色", "人物", "卡通", "女孩", "男孩", "女生", "男生", "人", "少女",
           "少年", "机器人", "动物", "猫", "狗", "熊", "兔", "吉祥物",
           "character", "girl", "boy", "figure", "mascot", "animal", "robot")

# 细节数量词：数字 + 量词 + 细节名词（"12 道褶"、"20 根卷发"）
_DETAIL_COUNT_RE = re.compile(
    r"(\d+)\s*(道|根|条|束|缕|片)\s*[^\d，。,\.]{0,3}?"
    r"(褶|卷发|发丝|波浪|褶裥|螺纹|棱|齿|孔)")
# 超过这个数就认为密度过高（面片表达不出来）
_MAX_DETAIL_FEATURES = 8

# 细节被模型采到的安全阈值（打印尺寸下），单位 mm
_PATCH_EDGE_MM = 6.0


def ensure_depth_cue(text: str) -> tuple[str, bool]:
    """保证提示词里有深度线索。返回 (新提示词, 是否改动过)。

    为什么必须这一步：用户写「一个卡通女孩，百褶裙」这种正面描述，
    模型对每个像素只能猜深度，结果就是一张 0.6-0.7% 厚的纸片 ——
    出图看着没问题，建模完全没法用。加一句「四分之三侧面站姿、
    一只手向前伸出」成本几乎为零，但把厚度从 0.7% 拉到 56%。
    """
    low = text.lower()
    has_cue = any(c in text or c in low for c in _DEPTH_CUES)
    has_occl = any(c in text or c in low for c in _OCCLUDERS)
    if has_cue and has_occl:
        return text, False

    low2 = text.lower()
    # 先删掉和"侧面"矛盾的词，否则 LLM 会两个都写、然后选正面
    _CONFLICT = ("正面站立", "正面站姿", "正面朝前", "正对镜头", "正面", "正对",
                 "front-facing", "front view", "facing forward")
    for w in _CONFLICT:
        text = re.sub(re.escape(w), "", text)
    text = re.sub(r"[，,]{2,}", "，", text).strip(" ，,")
    is_living = any(k in text or k in low2 for k in _LIVING)
    parts = []
    if not has_cue:
        parts.append("四分之三侧面站姿")
    if not has_occl:
        # 只有活物才加"手"；死物改成"侧前方再放一个部件"这种不荒谬的说法
        parts.append("一只手向前伸出" if is_living
                     else "从侧前方斜视，能看到主体的正侧面和厚度")
    if not parts:
        return text, False
    return text.rstrip("，。 ,.") + "，" + "，".join(parts), True


def density_guard(text: str) -> tuple[str, list[str]]:
    """把过密的细节数量降下来。返回 (新提示词, 改动说明)。

    为什么：模型的面数预算固定（2.1 的 512 octree 是 200k 面），
    平均面片边长约 5-6mm（80mm 打印时）。「12 道细褶」= 6.7mm 间距，
    刚好卡在面片分辨率上，建出来是一片平的；「6 道深褶」= 13mm，能看见。

    规则：超过 _MAX_DETAIL_FEATURES 的，减到一半（12 → 6，20 → 10）。
    只降数量，不动形容词 —— 「细褶」改成「深褶」是另一回事，那需要
    用户自己决定，这里只处理「数量太多」这个机械问题。
    """
    notes: list[str] = []

    def repl(m):
        n = int(m.group(1))
        unit, noun = m.group(2), m.group(3)
        if n <= _MAX_DETAIL_FEATURES:
            return m.group(0)
        new_n = max(3, n // 2)
        # 顺带把"细/薄/小"改成"粗/深/宽"：数量减半但形容词还是"细"的话，
        # 出图仍会画得很密，等于没改。
        noun2 = (noun.replace("细", "深").replace("薄", "厚").replace("小", "大"))
        notes.append(f"{n}{unit}{noun} → {new_n}{unit}{noun2}"
                     f"（面片约 {_PATCH_EDGE_MM:.0f}mm，太密的建不出来）")
        return f"{new_n}{unit}{noun2}"

    out = _DETAIL_COUNT_RE.sub(repl, text)
    return out, notes


def geometry_report(text: str) -> dict:
    """把两项检查跑一遍，返回结论（不改文案）。给日志/UI 用。"""
    low = text.lower()
    return {
        "has_depth_cue": any(c in text or c in low for c in _DEPTH_CUES),
        "has_occluder": any(c in text or c in low for c in _OCCLUDERS),
        "detail_counts": [int(m.group(1)) for m in _DETAIL_COUNT_RE.finditer(text)],
        "over_dense": any(int(m.group(1)) > _MAX_DETAIL_FEATURES
                          for m in _DETAIL_COUNT_RE.finditer(text)),
        "patch_edge_mm": _PATCH_EDGE_MM,
    }
