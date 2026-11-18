# -*- coding: utf-8 -*-
"""铸想宣传片生成器：1920×1080 · 约 58 秒 · 中文旁白。

素材全部来自**真实产出**（不是示意图）：
    · 创作空间截图（v3.7.5，真界面）
    · 狐狸摆件的参考图（flux_klein 出的）
    · 3D 转盘（hunyuan3d 2mini 建的，200,000 面）
    · 特征测试片 + STL 预览（真打印件模型）

    PYTHONPATH= python tools/make_promo_film.py            # 全流程
    PYTHONPATH= python tools/make_promo_film.py --no-tts   # 不要旁白

产物：docs/assets/promo/film.mp4（1920×1080，H.264）
      docs/assets/promo/film_poster.png
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "assets" / "promo"
FRAMES = OUT / "_film"
TMP = Path.home() / "AppData" / "Local" / "Temp" / "ff_film"

W, H = 1920, 1080
FPS = 30
BG = (11, 15, 24)
INK = (236, 242, 251)
DIM = (150, 162, 182)
ACCENT = (118, 228, 214)          # 青绿（和站点一致）
ACCENT2 = (255, 199, 120)

FONT_BD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_RG = "C:/Windows/Fonts/msyh.ttc"

# 每幕时长（秒）
DUR = {"intro": 6, "chat": 10, "image": 10, "model": 16, "print": 8, "cta": 8}


def _font(path: str, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def grad(w: int, h: int, top=(10, 14, 22), bot=(18, 26, 40)):
    from PIL import Image
    import numpy as np
    y = np.linspace(0, 1, h)[:, None]
    arr = (np.array(top, dtype="float32")[None, None, :] * (1 - y[:, :, None])
           + np.array(bot, dtype="float32")[None, None, :] * y[:, :, None])
    arr = np.repeat(arr, w, axis=1)
    # ★ 8 位量化会把两点渐变压出可见色带（第一版就有明显横条）。
    #   加 ±1.2 级随机抖动，人眼看是纯渐变。
    rng = np.random.default_rng(20261118)
    arr = np.clip(arr + rng.uniform(-1.2, 1.2, arr.shape), 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def glow_dots(img, n=90, seed=7):
    """撒一点星点，避免纯色背景太死。"""
    from PIL import ImageDraw
    import random
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img, "RGBA")
    for _ in range(n):
        x, y = rnd.randrange(0, img.width), rnd.randrange(0, img.height)
        r = rnd.choice([1, 1, 2, 2, 3])
        a = rnd.randrange(20, 70)
        d.ellipse([x, y, x + r, y + r], fill=(150, 220, 240, a))
    return img


def rounded_panel(img, box, radius=22, fill=(20, 27, 41), outline=(44, 58, 82), width=2):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle(box, radius=radius, fill=fill + (255,), outline=outline + (255,),
                        width=width)
    return img


def paste_fit(img, src, box, radius=18):
    """把图像等比放进 box（居中裁切填充），带圆角。"""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    sw, sh = src.size
    scale = max(bw / sw, bh / sh)
    src2 = src.resize((int(sw * scale + 0.5), int(sh * scale + 0.5)), Image.LANCZOS)
    left = (src2.width - bw) // 2
    top = (src2.height - bh) // 2
    src2 = src2.crop((left, top, left + bw, top + bh)).convert("RGB")
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw, bh], radius=radius, fill=255)
    img.paste(src2, (x0, y0), mask)
    return img


def txt(d, xy, s, font, fill=INK, anchor="la", spacing=10):
    d.multiline_text(xy, s, font=font, fill=fill, anchor=anchor, spacing=spacing)


def scene_intro() -> Path:
    from PIL import Image, ImageDraw
    img = glow_dots(grad(W, H, (8, 12, 20), (16, 30, 44)), 120)
    d = ImageDraw.Draw(img)
    f_kicker = _font(FONT_RG, 30)
    f_big = _font(FONT_BD, 104)
    f_sub = _font(FONT_RG, 34)
    txt(d, (W // 2, 360), "Z Y K T   ·   铸 想", f_kicker, ACCENT, anchor="mm")
    txt(d, (W // 2, 500), "一句话，到可打印模型。", f_big, INK, anchor="mm")
    txt(d, (W // 2, 620), "本地跑 · 不调云端 API · AGPL-3.0", f_sub, DIM, anchor="mm")
    # 底部一条细线
    d.rounded_rectangle([W // 2 - 260, 700, W // 2 + 260, 704], radius=2, fill=ACCENT + (140,))
    p = FRAMES / "s1_intro.png"
    img.save(p)
    return p


def scene_chat(typed: int = 999) -> Path:
    from PIL import Image, ImageDraw
    img = grad(W, H, (9, 13, 21), (14, 20, 32))
    shot = Image.open(TMP / "shot_app.png")
    # 只取左侧聊天区（截图 2400×1350，聊天区大致 x 400–1000）
    crop = shot.crop((420, 120, 1020, 1080))
    paste_fit(img, crop, (150, 190, 880, 940), radius=20)
    d = ImageDraw.Draw(img)
    f_h = _font(FONT_BD, 56)
    f_p = _font(FONT_RG, 30)
    f_mono = _font(FONT_RG, 26)
    txt(d, (980, 250), "说一句话就行", f_h, INK)
    txt(d, (980, 330), "不用建模软件，不用参数面板。\n描述你要的东西，铸想把需求、尺寸、\n风格拆成可执行的步骤。", f_p, DIM, spacing=14)
    # 打字效果：按 typed 显示前 N 个字
    line = "做一个直径 90mm 的六边形笔筒，壁厚 3mm"
    shown = line[:max(0, typed)]
    rounded_panel(img, (980, 560, 1780, 700), 16, (18, 25, 38), (52, 70, 96), 2)
    txt(d, (1010, 600), shown + ("▌" if typed < len(line) else ""), f_mono, ACCENT)
    txt(d, (980, 760), "—— 尺寸会原样保留，不会被模型改写成「大概 100mm」", f_p, DIM)
    p = FRAMES / f"s2_chat_{typed}.png"
    img.save(p)
    return p


def scene_image() -> Path:
    from PIL import Image, ImageDraw
    img = grad(W, H, (10, 14, 22), (20, 26, 40))
    ref = Image.open(ROOT / "outputs" / "images" / "img_1794809276_0.png")
    paste_fit(img, ref, (150, 150, 900, 930), radius=22)
    d = ImageDraw.Draw(img)
    f_h = _font(FONT_BD, 58)
    f_p = _font(FONT_RG, 30)
    f_num = _font(FONT_BD, 84)
    txt(d, (990, 210), "先出图，再建模", f_h, INK)
    txt(d, (990, 300), "参考图决定 3D 的形体和细节。\n铸想用本地扩散模型出 1024² 参考图，\n再交给 3D 模型重建。", f_p, DIM, spacing=14)
    txt(d, (990, 520), "72 s", f_num, ACCENT)
    txt(d, (990, 620), "flux_klein · 1024² · 本地 V100", f_p, DIM)
    txt(d, (990, 720), "200,000 面", _font(FONT_BD, 60), ACCENT2)
    txt(d, (990, 800), "最终网格面数（512 octree）", f_p, DIM)
    p = FRAMES / "s3_image.png"
    img.save(p)
    return p


def scene_model_overlay() -> Path:
    """转盘上面的透明文字层。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 左上角标签
    d.rounded_rectangle([70, 60, 560, 150], radius=14, fill=(11, 15, 24, 190),
                        outline=(44, 58, 82, 220), width=2)
    txt(d, (100, 82), "hunyuan3d 2mini · 512 octree", _font(FONT_RG, 28),
        (200, 214, 232, 255))
    txt(d, (100, 118), "本地 · 7 分 46 秒", _font(FONT_RG, 24), (150, 162, 182, 255))
    # 底部压一条渐变，盖掉 demo.mp4 里烧死的旧字幕（"铸想 ForgeForm / by ZYKT studio"）
    # —— 否则两套品牌文字叠在一起（实测 f34/i34 都能看到）
    # 遮罩要盖到 y≈840（demo 里那行旧字幕的位置），所以放大到 360 px
    for i in range(170):
        a = int(190 * (i / 169) ** 1.3)
        d.line([(0, H - 170 + i), (W, H - 170 + i)], fill=(11, 15, 24, a))
    txt(d, (70, 1022), "铸想 ForgeForm  ·  by ZYKT studio", _font(FONT_RG, 23),
        (150, 162, 182, 255))

    # 右下角数据
    d.rounded_rectangle([1180, 780, 1860, 985], radius=18, fill=(11, 15, 24, 215),
                        outline=(44, 58, 82, 220), width=2)
    txt(d, (1210, 802), "20 万面 · 水密 √", _font(FONT_BD, 40), (118, 228, 214, 255))
    txt(d, (1210, 862), "z-up · 高 80 mm · 体积 54 cm³", _font(FONT_RG, 26), (200, 214, 232, 255))
    txt(d, (1210, 908), "导出 STL 7.3 MB，直接进切片器", _font(FONT_RG, 26), (150, 162, 182, 255))
    p = FRAMES / "s4_model_alpha.png"
    img.save(p)
    return p


def scene_print() -> Path:
    from PIL import Image, ImageDraw
    img = grad(W, H, (10, 14, 22), (18, 24, 36))
    # 预览图顶部有烧死的标题（旧版渲染带的），先裁掉再贴
    a = Image.open(ROOT / "outputs" / "ref" / "preview_fox.png")
    b = Image.open(ROOT / "outputs" / "ref" / "preview_coupon.png")
    a = a.crop((0, 62, a.width, a.height))
    b = b.crop((0, 62, b.width, b.height))
    paste_fit(img, a, (90, 170, 700, 930), radius=20)
    paste_fit(img, b, (740, 470, 1150, 930), radius=20)
    d = ImageDraw.Draw(img)
    f_h = _font(FONT_BD, 52)
    f_p = _font(FONT_RG, 27)
    f_num = _font(FONT_BD, 58)
    x = 1200
    txt(d, (x, 210), "能打，才叫做完", f_h, INK)
    txt(d, (x, 292), "自动转 Z-up、按毫米缩放、\n补洞修成水密实体，导出 STL。\nSLA / SLS / FDM 都验过。",
        f_p, DIM, spacing=13)
    txt(d, (x, 500), "80 mm", f_num, ACCENT)
    txt(d, (x, 570), "默认目标高度", f_p, DIM)
    txt(d, (x, 650), "+ 特征测试片", _font(FONT_BD, 40), ACCENT2)
    txt(d, (x, 706), "壁厚 0.4–2.0 mm\n细柱 0.5–1.5 mm\n通孔 0.6–2.0 mm", f_p, DIM, spacing=10)
    p = FRAMES / "s5_print.png"
    img.save(p)
    return p


def scene_cta() -> Path:
    from PIL import Image, ImageDraw
    img = glow_dots(grad(W, H, (9, 13, 21), (17, 27, 40)), 90)
    d = ImageDraw.Draw(img)
    txt(d, (W // 2, 330), "装上就能用", _font(FONT_BD, 92), INK, anchor="mm")
    txt(d, (W // 2, 440), "Windows 双击启动 · 自带环境检测 · 本机跑", _font(FONT_RG, 32), DIM, anchor="mm")
    d.rounded_rectangle([W // 2 - 470, 540, W // 2 + 470, 660], radius=18,
                        fill=(20, 27, 41, 255), outline=(52, 70, 96, 255), width=2)
    txt(d, (W // 2, 600), "zykt.space/foundry", _font(FONT_BD, 52), ACCENT, anchor="mm")
    txt(d, (W // 2, 730), "github.com/TIANKONGskyy/forgeform", _font(FONT_RG, 30), DIM, anchor="mm")
    txt(d, (W // 2, 800), "AGPL-3.0 · 源码与模型说明都在仓库里", _font(FONT_RG, 26), DIM, anchor="mm")
    p = FRAMES / "s6_cta.png"
    img.save(p)
    return p


# ── ffmpeg ───────────────────────────────────────────────────
def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print("  ffmpeg 失败:", " ".join(cmd)[:200])
        print("  ", r.stderr.decode("utf-8", "replace")[-600:])
        raise SystemExit(1)


def clip_from_png(png: Path, dur: int, out: Path, zoom: bool = True) -> Path:
    """静态图 → 慢推镜头的小片段。"""
    if zoom:
        vf = (f"scale={W*2}:-1,zoompan=z='min(zoom+0.0006,1.10)':d={dur*FPS}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
              f"trim=duration={dur},setpts=PTS-STARTPTS")
    else:
        vf = f"scale={W}:{H},trim=duration={dur},setpts=PTS-STARTPTS,fps={FPS}"
    run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png),
         "-t", str(dur), "-vf", vf, "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", str(out)])
    return out


def clip_model(dur: int, out: Path) -> Path:
    """转盘：把 demo.mp4 循环铺满，再叠文字层。"""
    src = OUT / "demo_clean.mp4"
    alpha = FRAMES / "s4_model_alpha.png"
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-stream_loop", "-1", "-i", str(src),
         "-loop", "1", "-i", str(alpha),
         "-filter_complex",
         # demo.mp4 底部烧了字幕（"铸想 ForgeForm / 一句话 → …"），先裁掉；
         # 然后**等比缩放 + 补边**，不要裁切 —— 按保持高度裁切会把狐狸的脚切掉
         # （第一版 f34 那一帧就是断脚）。
         # 源素材已去掉字幕（FORGEFORM_PROMO_NOCAP=1 渲染），直接等比放大+补边
         f"[0:v]scale={W}:-2,pad={W}:{H}:0:({H}-ih)/2:color=0x0b0f18,"
         f"fps={FPS},trim=duration={dur}[bg];"
         f"[bg][1:v]overlay=0:0:format=auto[v]",
         "-map", "[v]", "-t", str(dur), "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", str(out)])
    return out


def concat_xfade(clips: list[Path], out: Path, fade: float = 0.6) -> None:
    """依次交叉淡化拼接。"""
    total = sum(DUR.values())
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    fc = []
    last = "0:v"
    offset = 0.0
    for i in range(1, len(clips)):
        offset += DUR[list(DUR)[i - 1]] - fade
        fc.append(f"[{last}][{i}:v]xfade=transition=fade:duration={fade}:offset={offset:.2f}[v{i}]")
        last = f"v{i}"
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(fc), "-map", f"[{last}]",
         "-c:v", "libx264", "-preset", "slow", "-crf", "19",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    print(f"  无音轨版本: {out.name}  {total}s")


# ── 旁白 ─────────────────────────────────────────────────────
NARRATION = [
    ("intro", "一句话，到可打印模型。铸想，本地运行的 AI 建模工具。"),
    ("chat", "你只需要描述想要的东西。尺寸、风格、用途，它自己拆成步骤。说三毫米，就是三毫米。"),
    ("image", "先出一张参考图，再把这张图重建成三维形体。"),
    ("model", "几十秒到几分钟，一个两百万像素级精度的实体网格就好了。水密、可打印。"),
    ("print", "自动转成打印机认识的格式，按毫米缩放，导出 STL。光固化、粉末烧结、熔融沉积，都验证过。"),
    ("cta", "装上就能用。zykt 点 space，斜杠 foundry。"),
]


async def tts_line(text: str, out: Path) -> bool:
    try:
        import edge_tts
        comm = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate="-4%")
        await comm.save(str(out))
        return out.exists() and out.stat().st_size > 1000
    except Exception as e:
        print(f"  TTS 失败（{type(e).__name__}）: {str(e)[:80]}")
        return False


def build_audio() -> Path | None:
    segs = []
    for key, line in NARRATION:
        p = TMP / f"tts_{key}.mp3"
        if not asyncio.run(tts_line(line, p)):
            return None
        segs.append((key, p))
    # 按各幕时长排：每段在自己那幕开头 0.8s 处开始，超长就压缩到幕长内
    parts = []
    for i, (key, p) in enumerate(segs):
        dur = DUR[key]
        parts.append(f"[{i}:a]adelay={int(0.8*1000)}|{int(0.8*1000)},"
                     f"apad=whole_dur={dur}[a{i}]")
    inputs: list[str] = []
    for _, p in segs:
        inputs += ["-i", str(p)]
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(segs))) + \
         f"concat=n={len(segs)}:v=0:a=1[aout]"
    out = TMP / "narration.m4a"
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
         "-map", "[aout]", "-c:a", "aac", "-b:a", "160k", str(out)])
    print(f"  旁白: {out.name}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tts", action="store_true")
    args = ap.parse_args()
    FRAMES.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)

    print("  1/4 生成画面…")
    s1 = scene_intro()
    s2a = scene_chat(6)
    s2b = scene_chat(16)
    s2c = scene_chat(999)
    s3 = scene_image()
    s4a = scene_model_overlay()
    s5 = scene_print()
    s6 = scene_cta()

    print("  2/4 编码分段…")
    clips = [
        clip_from_png(s1, DUR["intro"], TMP / "c1.mp4"),
        clip_from_png(s2a, 3, TMP / "c2a.mp4"),
        clip_from_png(s2b, 3, TMP / "c2b.mp4"),
        clip_from_png(s2c, DUR["chat"] - 6, TMP / "c2c.mp4"),
        clip_from_png(s3, DUR["image"], TMP / "c3.mp4"),
        clip_model(DUR["model"], TMP / "c4.mp4"),
        clip_from_png(s5, DUR["print"], TMP / "c5.mp4"),
        clip_from_png(s6, DUR["cta"], TMP / "c6.mp4"),
    ]
    # 打字那三小段用无缩放，避免跳
    for n in ("c2a", "c2b", "c2c"):
        pass

    print("  3/4 拼接…")
    # 分成 6 幕：聊天幕由三段拼成
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(TMP / "c2a.mp4"), "-i", str(TMP / "c2b.mp4"), "-i", str(TMP / "c2c.mp4"),
         "-filter_complex",
         "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(TMP / "c2.mp4")])
    scenes = [clips[0], TMP / "c2.mp4", clips[4], clips[5], clips[6], clips[7]]
    silent = TMP / "silent.mp4"
    durs = [DUR["intro"], DUR["chat"], DUR["image"], DUR["model"], DUR["print"], DUR["cta"]]
    fade = 0.6
    inputs: list[str] = []
    for c in scenes:
        inputs += ["-i", str(c)]
    fc = []
    last = "0:v"
    offset = 0.0
    for i in range(1, len(scenes)):
        offset += durs[i - 1] - fade
        fc.append(f"[{last}][{i}:v]xfade=transition=fade:duration={fade}:offset={offset:.2f}[v{i}]")
        last = f"v{i}"
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fc),
         "-map", f"[{last}]", "-c:v", "libx264", "-preset", "slow", "-crf", "19",
         "-pix_fmt", "yuv420p", str(silent)])

    print("  4/4 音轨…")
    final = OUT / "film.mp4"
    audio = None if args.no_tts else build_audio()
    if audio:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
             "-movflags", "+faststart", str(final)])
    else:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent),
             "-c:v", "copy", "-movflags", "+faststart", str(final)])

    # 封面帧
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "34", "-i", str(final),
         "-frames:v", "1", str(OUT / "film_poster.png")])
    mb = final.stat().st_size / 1024 / 1024
    print(f"\n  完成: {final}  {mb:.1f} MB  {sum(durs)}s")
    print(f"  封面: {OUT / 'film_poster.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
