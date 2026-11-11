# -*- coding: utf-8 -*-
"""生成 AI 创意工坊主题桌面图标（2026-08-11）
设计：深紫渐变圆角方块 + 中心渐变光球 + 三道扩散环（呼应鸿蒙动画）+ 底部"AI"字标
"""
import sys
import math
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

SIZE = 512


def lerp(a, b, t):
    return a + (b - a) * t


def make_icon():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. 圆角背景 + 深紫渐变
    mask = Image.new("L", (SIZE, SIZE), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=96, fill=255)

    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(SIZE):
        t = y / SIZE
        # 深紫 -> 蓝黑渐变
        r = int(lerp(30, 13, t))
        g = int(lerp(27, 15, t))
        b = int(lerp(58, 40, t))
        bd.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))

    # 2. 对角线高光渐变（右上角亮）
    hl = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    for y in range(SIZE):
        for x in range(0, SIZE, 2):
            # 越靠右上越亮
            d = ((x / SIZE) * 0.7 + (1 - y / SIZE) * 0.3)
            alpha = int(28 * d)
            hd.point((x, y), fill=(167, 139, 250, alpha))
    bg = Image.alpha_composite(bg, hl)
    bg = bg.filter(ImageFilter.GaussianBlur(2))

    # 3. 中心光球（径向渐变）
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = SIZE // 2, SIZE // 2 - 10
    for r in range(70, 0, -1):
        t = r / 70
        alpha = int(60 * (1 - t))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(167, 139, 250, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(6))

    # 4. 三道扩散环（鸿蒙元素）
    ring_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring_img)
    for i, rr in enumerate([95, 130, 165]):
        alpha = int(120 - i * 30)
        width = 5 - i
        if width < 2:
            width = 2
        rd.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                   outline=(196, 181, 253, alpha), width=width)

    # 5. 中心核心光点
    core = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for r in range(26, 0, -1):
        t = r / 26
        alpha = int(255 * (1 - t * 0.5))
        cd.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(int(lerp(196, 124, t)), int(lerp(181, 58, t)), int(lerp(253, 237, t)), alpha))

    # 6. 底部 "AI" 文字
    text_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 72)
    except Exception:
        font = ImageFont.load_default()
    text = "AI"
    bbox = td.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx, ty = (SIZE - tw) / 2 - bbox[0], 330 - bbox[1]
    td.text((tx, ty), text, font=font, fill=(226, 226, 238, 255))

    # 合成
    base = Image.alpha_composite(bg, glow)
    base = Image.alpha_composite(base, ring_img)
    base = Image.alpha_composite(base, core)
    base = Image.alpha_composite(base, text_img)
    base = Image.composite(base, Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0)), mask)
    return base


if __name__ == "__main__":
    icon = make_icon()
    out_png = r"D:\AI-Workshop\assets\app_icon.png"
    out_ico = r"D:\AI-Workshop\assets\app_icon.ico"
    import os
    os.makedirs(r"D:\AI-Workshop\assets", exist_ok=True)
    icon.save(out_png, "PNG")
    # 多尺寸 ico
    icon.save(out_ico, "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("saved:", out_png)
    print("saved:", out_ico)
    # 顺便存一个 256 预览
    icon.resize((256, 256), Image.LANCZOS).save(r"D:\AI-Workshop\assets\app_icon_256.png", "PNG")
    print("preview saved")
