# -*- coding: utf-8 -*-
"""生成铸想的宣传图。

四张：
  1. poster.png      主视觉
  2. compare.png     艺术品 vs 机械件（厚度对比）
  3. pipeline.png    流程示意 + 实测耗时
  4. models.png      当前支持的模型

输出到 docs/assets/promo/。
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(r"D:\ForgeForm-oss")
OUT = ROOT / "docs" / "assets" / "promo"
OUT.mkdir(parents=True, exist_ok=True)

BG = (12, 16, 25)
PANEL = (20, 26, 39)
LINE = (39, 48, 66)
TEXT = (237, 241, 250)
MUTED = (157, 169, 189)
ACCENT = (156, 228, 218)
WARN = (232, 139, 139)
GOLD = (232, 180, 106)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def font(size: int, bold: bool = False):
    for p in ([r"C:\Windows\Fonts\msyhbd.ttc"] if bold else []) + FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def text(d, xy, s, size, color=TEXT, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=color, anchor=anchor)


def card(d, box, radius=16):
    d.rounded_rectangle(box, radius=radius, fill=PANEL, outline=LINE, width=1)


def load_art(name: str, dark: bool = True) -> Image.Image | None:
    """读渲染图。render3d 出的是白底，贴到深色海报上要先把白底打掉。"""
    p = ROOT / "outputs" / "art" / f"{name}_preview.png"
    if not p.exists():
        p = ROOT / "outputs" / "art" / f"{name}.png"
    if not p.exists():
        return None
    im = Image.open(p).convert("RGB")
    if not dark:
        return im
    # 白底 -> 透明，再把剩余的浅灰也压暗（否则边缘一圈白边）
    px = im.load()
    w, h = im.size
    out = Image.new("RGB", (w, h), BG)
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if r > 235 and g > 235 and b > 235:
                continue                      # 纯白：保留背景
            out.putpixel((x, y), (r, g, b))
    return out


# ══════════════════════════════════════════════════════════
# 1. 主视觉
# ══════════════════════════════════════════════════════════
def poster():
    W, H = 1600, 1000
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # 顶部：品牌
    text(d, (70, 56), "FORGEFORM", 15, MUTED)
    text(d, (70, 78), "by ZYKT studio", 13, (109, 120, 145))

    # 主标题
    text(d, (70, 150), "铸想 ForgeForm", 62, TEXT, bold=True)
    text(d, (70, 240), "一句话，到可打印模型。", 40, ACCENT, bold=True)
    text(d, (70, 306), "本地部署 · 全链路 · 不调云端 API", 20, MUTED)

    # 关键数据
    stats = [
        ("4.9", "分钟", "全流程"),
        ("80", "mm", "STL 尺寸"),
        ("水密", "", "可直接切片"),
        ("本地", "", "零云端"),
    ]
    x = 70
    for val, unit, label in stats:
        text(d, (x, 400), val, 40, TEXT, bold=True)
        if unit:
            text(d, (x + d.textlength(val, font=font(40, True)) + 6, 414), unit, 18, MUTED)
        text(d, (x, 456), label, 15, MUTED)
        x += 200

    # 右侧作品图
    art = load_art("deer")
    if art:
        art = art.resize((560, 560))
        im.paste(art, (1000, 120))
        d.rounded_rectangle([1000, 120, 1560, 680], radius=20, outline=LINE, width=1)

    # 底部：流程
    y = 760
    steps = [
        ("01", "一句话", "描述你要做的东西"),
        ("02", "出图", "本地扩散模型"),
        ("03", "建模", "图生 3D + 补洞"),
        ("04", "STL", "水密、毫米、可切片"),
    ]
    cw = (W - 140 - 3 * 16) // 4
    for i, (num, title, sub) in enumerate(steps):
        bx = 70 + i * (cw + 16)
        card(d, [bx, y, bx + cw, y + 150])
        text(d, (bx + 20, y + 18), num, 13, ACCENT, bold=True)
        text(d, (bx + 20, y + 44), title, 22, TEXT, bold=True)
        text(d, (bx + 20, y + 84), sub, 14, MUTED)

    # 页脚
    text(d, (70, H - 40), "github.com/TIANKONGskyy/forgeform", 13, (109, 120, 145))
    text(d, (W - 70, H - 40), "AGPL-3.0", 13, (109, 120, 145), anchor="ra")

    p = OUT / "poster.png"
    im.save(p)
    print(f"  海报: {p}  {im.size}")
    return p


# ══════════════════════════════════════════════════════════
# 2. 艺术品 vs 机械件
# ══════════════════════════════════════════════════════════
def compare():
    W, H = 1400, 780
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    text(d, (60, 44), "同模型、同参数，只换物体类型", 30, TEXT, bold=True)
    text(d, (60, 90), "2mini · 512 octree · 单张参考图", 16, MUTED)

    # 左：艺术品
    box = [60, 150, 680, 620]
    card(d, box)
    art = load_art("deer")
    if art:
        im.paste(art.resize((420, 420)), (160, 180))
    text(d, (60, 640), "有机形态（动物 / 雕塑 / 摆件）", 22, ACCENT, bold=True)
    text(d, (60, 678), "厚度占比 68–98%  ·  形态准确", 17, TEXT)
    text(d, (60, 712), "1.66 M 面  ·  256 s", 15, MUTED)

    # 右：机械件
    box2 = [720, 150, 1340, 620]
    card(d, box2)
    fl = ROOT / "outputs" / "3d" / "lazy_gpu_test_preview.png"
    if fl.exists():
        im.paste(Image.open(fl).convert("RGB").resize((420, 420)), (820, 180))
    text(d, (720, 640), "回转对称件（法兰 / 垫片）", 22, WARN, bold=True)
    text(d, (720, 678), "厚度占比 11.9%  ·  压成薄片", 17, TEXT)
    text(d, (720, 712), "1.33 M 面  ·  120 s", 15, MUTED)

    text(d, (60, H - 40), "单视图推不出深度：前后长得一样时，模型只能猜成平面。",
         14, (109, 120, 145))

    p = OUT / "compare.png"
    im.save(p)
    print(f"  对比: {p}  {im.size}")
    return p


# ══════════════════════════════════════════════════════════
# 3. 流程 + 实测
# ══════════════════════════════════════════════════════════
def pipeline():
    W, H = 1400, 820
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    text(d, (60, 44), "全流程实测", 32, TEXT, bold=True)
    text(d, (60, 92), "V100 32G · 2026-11-13 · tools/e2e_full.py", 15, MUTED)

    rows = [
        ("LLM 扩写", "33 → 1045 字符", "37.5 s", ACCENT),
        ("出图 z_image 1024²", "748 KB", "49.5 s", TEXT),
        ("建模 2mini 512oct", "1.96 M 面", "191.1 s", TEXT),
        ("导出 STL", "84 K 面 · 水密", "11.5 s", ACCENT),
    ]
    y = 150
    maxsec = max(float(r[2].split()[0]) for r in rows)
    for title, detail, sec, col in rows:
        card(d, [60, y, W - 60, y + 92])
        text(d, (90, y + 24), title, 21, TEXT, bold=True)
        text(d, (90, y + 58), detail, 15, MUTED)
        text(d, (W - 90, y + 26), sec, 26, col, bold=True, anchor="ra")
        # 进度条
        s = float(sec.split()[0])
        bw = int((W - 500) * s / maxsec)
        d.rounded_rectangle([420, y + 60, 420 + bw, y + 70], radius=5,
                            fill=col + (90,) if False else tuple(int(c * .45) for c in col))
        y += 104

    # 合计
    card(d, [60, y + 10, W - 60, y + 130])
    text(d, (90, y + 34), "合计", 22, MUTED, bold=True)
    text(d, (W - 90, y + 24), "4.9 分钟", 46, ACCENT, bold=True, anchor="ra")
    text(d, (W - 90, y + 90), "从一句话到可打印 STL", 15, MUTED, anchor="ra")

    p = OUT / "pipeline.png"
    im.save(p)
    print(f"  流程: {p}  {im.size}")
    return p


# ══════════════════════════════════════════════════════════
# 4. 模型清单
# ══════════════════════════════════════════════════════════
def models():
    W, H = 1400, 860
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    text(d, (60, 44), "模型可替换", 32, TEXT, bold=True)
    text(d, (60, 92), "一个模型一个 YAML 文件，换模型不改代码", 15, MUTED)

    groups = [
        ("出图", [
            ("FLUX.2 Klein 4B", "10 GB 显存 · 2026 新版", True),
            ("FLUX.2 Klein 9B", "32 GB", False),
            ("Z-Image Turbo 6B", "20 GB", False),
            ("Qwen-Image 2.1", "22 GB", False),
            ("FLUX.1-dev", "20 GB", False),
            ("Stable Diffusion 1.5", "7 GB", False),
        ]),
        ("建模", [
            ("Hunyuan3D 2mini", "3.6 GB · 直连可跑", True),
            ("Hunyuan3D 2.1", "9.6 GB · 质量最高", False),
            ("TRELLIS.2 4B", "未接入运行时", None),
            ("TRELLIS.2 GGUF", "未接入运行时", None),
        ]),
    ]

    y = 150
    for gname, items in groups:
        text(d, (60, y), gname, 18, MUTED, bold=True)
        y += 40
        cw = (W - 120 - 2 * 14) // 3
        for i, (name, sub, ok) in enumerate(items):
            col = i % 3
            row = i // 3
            bx = 60 + col * (cw + 14)
            by = y + row * 108
            card(d, [bx, by, bx + cw, by + 92])
            dot = ACCENT if ok is True else (WARN if ok is False else GOLD)
            d.ellipse([bx + 20, by + 20, bx + 30, by + 30], fill=dot)
            text(d, (bx + 40, by + 16), name, 18, TEXT, bold=True)
            text(d, (bx + 20, by + 52), sub, 14, MUTED)
        y += 40 + ((len(items) + 2) // 3) * 108 + 20

    text(d, (60, H - 40), "低配机器：6 GB 显存 / 10 GB 内存也能跑建模（2mini 256 octree，45 秒）",
         14, (109, 120, 145))

    p = OUT / "models.png"
    im.save(p)
    print(f"  模型: {p}  {im.size}")
    return p


if __name__ == "__main__":
    print("生成宣传图：")
    poster()
    compare()
    pipeline()
    models()
    print(f"\n输出目录: {OUT}")
