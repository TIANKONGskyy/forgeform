# -*- coding: utf-8 -*-
"""生成「AI 创意工坊生成物统计报告」PPT

内容：生成统计（提示词/耗时/tokens）+ 视频模块看法 + 改进建议
数据源：outputs/continuous/stats.json
"""
import sys, os, json, time
from pathlib import Path
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = Path("D:/AI-Workshop")
STATS = json.loads((WORKSPACE / "outputs" / "continuous" / "stats.json").read_text(encoding="utf-8"))

# 配色（深色科技风）
BG = RGBColor(0x1E, 0x1E, 0x2E)
CARD = RGBColor(0x2A, 0x2A, 0x3E)
ACCENT = RGBColor(0x7C, 0x6F, 0xFF)   # 紫
ACCENT2 = RGBColor(0x4E, 0xCC, 0xB8)  # 青
TEXT = RGBColor(0xE2, 0xE2, 0xE8)
MUTED = RGBColor(0x9A, 0x9A, 0xB0)
WARN = RGBColor(0xFF, 0xB0, 0x54)     # 橙

FONT = "Microsoft YaHei"


def set_bg(slide, color=BG):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = color


def add_rect(slide, x, y, w, h, color, rounded=True):
    from pptx.enum.shapes import MSO_SHAPE
    shp = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
        x, y, w, h)
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def add_text(slide, x, y, w, h, text, size=18, color=TEXT, bold=False, align=PP_ALIGN.LEFT, valign=None):
    from pptx.enum.text import MSO_ANCHOR
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    if valign:
        tf.vertical_anchor = valign
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.color.rgb = color
    r.font.bold = bold
    r.font.name = FONT
    return tb


def add_bullets(slide, x, y, w, h, items, size=16, color=TEXT, gap=6):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    for i, (txt, c) in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        r = p.add_run()
        r.text = "•  " + txt
        r.font.size = Pt(size)
        r.font.color.rgb = c
        r.font.name = FONT


def add_kpi_card(slide, x, y, w, h, label, value, sub, color=ACCENT):
    add_rect(slide, x, y, w, h, CARD)
    add_text(slide, x + Inches(0.2), y + Inches(0.15), w - Inches(0.4), Inches(0.4),
             label, size=13, color=MUTED)
    add_text(slide, x + Inches(0.2), y + Inches(0.5), w - Inches(0.4), Inches(0.6),
             value, size=30, color=color, bold=True)
    if sub:
        add_text(slide, x + Inches(0.2), y + Inches(1.15), w - Inches(0.4), Inches(0.4),
                 sub, size=11, color=MUTED)


def cover(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_rect(s, Inches(0.5), Inches(2.0), Inches(0.08), Inches(2.2), ACCENT, rounded=False)
    add_text(s, Inches(0.9), Inches(2.0), Inches(8.5), Inches(0.8),
             "AI 创意工坊 · 生成物统计报告", size=40, color=TEXT, bold=True)
    add_text(s, Inches(0.9), Inches(2.9), Inches(8.5), Inches(0.6),
             "提示词 · 耗时 · Tokens · 视频模块评估", size=20, color=ACCENT2)
    add_text(s, Inches(0.9), Inches(4.0), Inches(8.5), Inches(0.5),
             f"生成时间：{STATS['generated_at']}    身体：Gooxi 服务器 (RTX 3080 20GB)", size=13, color=MUTED)


def toc(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.5), Inches(6), Inches(0.8), "目录", size=32, color=TEXT, bold=True)
    items = [
        ("01  生成物总览", "图像 / 3D / 视频 三类产出规模"),
        ("02  提示词分析", "8 个提示词轮换分布"),
        ("03  耗时分析", "生成速度与效率"),
        ("04  Tokens 统计", "扩散模型的 token 口径说明"),
        ("05  视频模块评估", "看法 + 改进建议"),
        ("06  改进路线图", "下一步优化方向"),
    ]
    y = Inches(1.6)
    for title, desc in items:
        add_rect(s, Inches(0.6), y, Inches(5.5), Inches(0.75), CARD)
        add_text(s, Inches(0.85), y + Inches(0.12), Inches(3), Inches(0.5), title, size=18, color=ACCENT2, bold=True)
        add_text(s, Inches(3.9), y + Inches(0.18), Inches(2.2), Inches(0.5), desc, size=12, color=MUTED)
        y += Inches(0.85)


def slide_overview(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(6), Inches(0.6), "01 · 生成物总览", size=28, color=TEXT, bold=True)

    img = STATS["image"]
    td = STATS["3d"]
    vid = STATS["video"]

    add_kpi_card(s, Inches(0.6), Inches(1.3), Inches(3.1), Inches(1.7),
                 "图像生成", f"{img['count']} 张", f"512×512，平均 {img['avg_time_s']}s/张", ACCENT)
    add_kpi_card(s, Inches(3.9), Inches(1.3), Inches(3.1), Inches(1.7),
                 "3D 生成", f"{td['count']} 个", f"平均 {td['avg_time_s']}s，{td['avg_size_mb']}MB/个", ACCENT2)
    add_kpi_card(s, Inches(7.2), Inches(1.3), Inches(3.1), Inches(1.7),
                 "视频生成", f"{vid['count']} 个", "未启用（功耗墙权衡）", WARN)

    add_bullets(s, Inches(0.6), Inches(3.4), Inches(9.7), Inches(2.5), [
        ("图像是当前主力产出，8 个提示词轮换持续生成，已累计 500+ 张", TEXT),
        ("3D 线自动消费图像产物（每 8 张跑 1 次），形成「图像→3D」流水线", TEXT),
        ("视频模块因 320W 功耗墙下温度风险高，暂缓启用（详见第 05 章）", WARN),
        ("总 latent tokens（图像）约 219 万，是扩散模型的实际计算单元", TEXT),
    ], size=16)


def slide_prompts(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(6), Inches(0.6), "02 · 提示词分析", size=28, color=TEXT, bold=True)
    add_text(s, Inches(0.6), Inches(1.05), Inches(9), Inches(0.4),
             f"共 {STATS['image']['unique_prompts']} 个唯一提示词，平均 {STATS['image']['avg_prompt_tokens']} tokens/条",
             size=14, color=MUTED)

    y = Inches(1.55)
    top = STATS["image"]["top_prompts"]
    maxc = top[0][1] if top else 1
    for prompt, cnt in top:
        add_text(s, Inches(0.6), y, Inches(5.5), Inches(0.35), prompt, size=13, color=TEXT)
        bar_w = Inches(3.5) * (cnt / maxc)
        add_rect(s, Inches(6.3), y + Inches(0.02), bar_w, Inches(0.28), ACCENT)
        add_text(s, Inches(6.3) + bar_w + Inches(0.1), y, Inches(1), Inches(0.35),
                 str(cnt), size=12, color=ACCENT2, bold=True)
        y += Inches(0.42)

    add_text(s, Inches(0.6), y + Inches(0.15), Inches(9), Inches(0.6),
             "提示词覆盖：产品摄影 / 机器人 / 船只模型 / 棋具 / 机械 / 城堡 / 抽象 / 织物 八大类，分布均匀",
             size=14, color=MUTED)


def slide_time(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(6), Inches(0.6), "03 · 耗时分析", size=28, color=TEXT, bold=True)

    img = STATS["image"]
    td = STATS["3d"]
    add_kpi_card(s, Inches(0.6), Inches(1.3), Inches(4.5), Inches(1.7),
                 "图像耗时", f"{img['avg_time_s']}s", f"范围 {img['min_time_s']}~{img['max_time_s']}s", ACCENT)
    add_kpi_card(s, Inches(5.4), Inches(1.3), Inches(4.5), Inches(1.7),
                 "3D 耗时", f"{td['avg_time_s']}s", f"范围 {td['min_time_s']}~{td['max_time_s']}s", ACCENT2)

    add_bullets(s, Inches(0.6), Inches(3.3), Inches(9.7), Inches(2.8), [
        ("图像已启用 TeaCache 加速（1.9x），512 图稳定在 9s 左右", TEXT),
        ("3D 用 TripoSplat，平均 45s，是图像耗时的 5 倍（重负载）", TEXT),
        ("持续生成采用 70% 占空比（生成 + 间隔），避免长时间满载", TEXT),
        ("温度自适应：>74°C 加间隔，>82°C 暂停，>85°C 强制中断", TEXT),
    ], size=16)


def slide_tokens(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(8), Inches(0.6), "04 · Tokens 统计", size=28, color=TEXT, bold=True)

    img = STATS["image"]
    add_kpi_card(s, Inches(0.6), Inches(1.3), Inches(3.1), Inches(1.7),
                 "提示词 tokens", f"{img['total_prompt_tokens']:,}", f"累计（平均 {img['avg_prompt_tokens']}/条）", ACCENT)
    add_kpi_card(s, Inches(3.9), Inches(1.3), Inches(3.1), Inches(1.7),
                 "图像 latent tokens", f"{img['latent_tokens_per_image']:,}/张", "512→64×64 latent", ACCENT2)
    add_kpi_card(s, Inches(7.2), Inches(1.3), Inches(3.1), Inches(1.7),
                 "总 latent tokens", f"{img['total_latent_tokens']:,}", f"{img['count']} 张累计", WARN)

    add_bullets(s, Inches(0.6), Inches(3.4), Inches(9.7), Inches(2.8), [
        ("扩散模型（FLUX/TripoSplat）不产出 LLM 式 token，其计算单元是「latent token」", TEXT),
        ("512×512 图像 = 64×64 = 4096 个 latent token（FLUX 8x 下采样）", TEXT),
        ("提示词 token 用 cl100k_base 估算，平均 11.3/条（简短英文提示）", TEXT),
        ("3D 模型无 token 概念，用三角形面数衡量（约 13.5 万/个）", TEXT),
    ], size=15)


def slide_video(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(8), Inches(0.6), "05 · 视频模块评估", size=28, color=TEXT, bold=True)

    add_text(s, Inches(0.6), Inches(1.15), Inches(9.5), Inches(0.5),
             "我的看法", size=20, color=ACCENT2, bold=True)
    add_bullets(s, Inches(0.6), Inches(1.7), Inches(9.7), Inches(2.0), [
        ("H3 视频是当前最重的负载（5-10 分钟/条），也是温度崩溃的头号风险源", WARN),
        ("已接入 turbo lora（8 步→4 步），理论 2x，但受 320W 功耗墙限制未实测", TEXT),
        ("降功耗到 220W 会让视频效率损失过大（更慢），性价比不高，你的判断正确", TEXT),
        ("结论：视频模块应「保留能力、暂缓启用」，先跑通图像+3D 流水线", TEXT),
    ], size=15)

    add_text(s, Inches(0.6), Inches(3.9), Inches(9.5), Inches(0.5),
             "改进建议", size=20, color=ACCENT2, bold=True)
    add_bullets(s, Inches(0.6), Inches(4.45), Inches(9.7), Inches(2.5), [
        ("优先级 1：温度自适应降级——视频任务 >78°C 自动暂停，让 320W 也能安全跑", TEXT),
        ("优先级 2：分段生成——把长视频拆成 2-3 段分别生成再拼接，单段温度可控", TEXT),
        ("优先级 3：等 GPU 散热改造（换硅脂/加风道）后再启用，治本", TEXT),
        ("优先级 4：夜间低负载时段跑视频（室温低，温度余量大）", TEXT),
    ], size=15)


def slide_roadmap(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(0.4), Inches(8), Inches(0.6), "06 · 改进路线图", size=28, color=TEXT, bold=True)

    steps = [
        ("已达成", ACCENT2, ["TeaCache 加速 1.9x", "图片缓存去重（秒回）", "队列防卡死 + 温度看门狗",
                       "仿生持续生成框架", "自动进化闭环 + 进化谱系", "多任务调度器（图像+3D）"]),
        ("下一步", ACCENT, ["视频分段生成（温度可控）", "3D 面数优化（模型精简）",
                      "夜间批量生成调度", "进化记忆接入 MEMORY.md 长期体系"]),
        ("远期", WARN, ["视频线启用（散热改造后）", "WSL2 迁移 ComfyUI（稳定性质变）",
                      "TensorRT 引擎（再快 2-3x）"]),
    ]
    y = Inches(1.3)
    for title, color, items in steps:
        add_rect(s, Inches(0.6), y, Inches(9.7), Inches(1.7), CARD)
        add_text(s, Inches(0.85), y + Inches(0.12), Inches(2), Inches(0.4), title, size=18, color=color, bold=True)
        add_text(s, Inches(3.0), y + Inches(0.15), Inches(7), Inches(1.4),
                 "  ·  ".join(items), size=13, color=TEXT)
        y += Inches(1.85)


def end(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    add_text(s, Inches(0.6), Inches(2.6), Inches(9), Inches(0.8),
             "感谢阅读", size=40, color=TEXT, bold=True, align=PP_ALIGN.CENTER)
    add_text(s, Inches(0.6), Inches(3.5), Inches(9), Inches(0.5),
             "数据实时统计 · 可随时重新生成（gen_stats.py + gen_ppt.py）", size=14, color=MUTED, align=PP_ALIGN.CENTER)


def main():
    prs = Presentation()
    prs.slide_width = Inches(11)
    prs.slide_height = Inches(6.27)  # 16:9 近似

    cover(prs)
    toc(prs)
    slide_overview(prs)
    slide_prompts(prs)
    slide_time(prs)
    slide_tokens(prs)
    slide_video(prs)
    slide_roadmap(prs)
    end(prs)

    out = WORKSPACE / "outputs" / "生成物统计报告.pptx"
    prs.save(str(out))
    print(f"✅ PPT 已生成: {out}")
    print(f"   共 {len(prs.slides._sldIdLst)} 页")


if __name__ == "__main__":
    main()
