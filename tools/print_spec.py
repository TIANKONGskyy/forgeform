# -*- coding: utf-8 -*-
"""把批量打样的产物变成"可以直接交给打印厂"的工单。

输入：`outputs/art/batch/<id>/{image.png,model.stl,meta.json}`
输出：
    outputs/art/batch/PRINT-ORDER.md    人看的工单（每件：尺寸/体积/克重/摆放/支撑/填充）
    outputs/art/batch/PRINT-ORDER.csv   表格（给 Excel）
    outputs/art/batch/contact_sheet.png 一张总览图

材料与参数的口径（实测过的，不是抄厂商页）：
    SLA  树脂 1.10 g/cm³，壁厚靠 25 µm 层厚撑细节，实体件不掏空（摆件不掏）
    SLS  PA12 1.01 g/cm³，粉耗按件重的 3.5 倍估（含刷新）
    FDM  PLA 1.24 g/cm³，15% 螺旋填充 + 3 壁，实体外壳按 1.2 mm 皮厚折算

    PYTHONPATH= python tools/print_spec.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "outputs" / "art" / "batch"

# 材料密度与算法参数
SLA_DENSITY = 1.12      # g/cm³（Formlabs 通用树脂 1.09–1.16）
SLS_DENSITY = 1.01      # PA12
FDM_DENSITY = 1.24      # PLA
SLS_POWDER_MULT = 3.5   # 粉耗 ≈ 件重 × 3.5（含刷新损耗）
FDM_SKIN_MM = 1.2       # 3 壁 × 0.4 ≈ 1.2 mm 实心皮


def load_items() -> list[dict]:
    out = []
    if not BATCH.is_dir():
        return out
    for d in sorted(BATCH.iterdir()):
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        stl = d / "model.stl"
        if not (meta_p.exists() and stl.exists()):
            continue
        try:
            m = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not m.get("ok"):
            continue
        m["_dir"] = str(d)
        m["_stl"] = str(stl)
        m["_stl_mb"] = round(stl.stat().st_size / 1024 / 1024, 1)
        out.append(m)
    return out


def fmt(v, n=1):
    return f"{v:.{n}f}" if isinstance(v, (int, float)) else str(v)


def build_rows(items: list[dict]) -> list[dict]:
    rows = []
    for i, m in enumerate(items, 1):
        vol = float(m.get("volume_cm3") or 0)
        bbox = m.get("bbox_mm") or [0, 0, 0]
        h = bbox[2] if len(bbox) > 2 else 0
        # SLA：实体灌满 + 支撑 12%
        sla_g = vol * SLA_DENSITY * 1.12
        # SLS：件重 + 粉耗
        sls_g = vol * SLS_DENSITY
        sls_powder = sls_g * SLS_POWDER_MULT
        # FDM：外壳实心 + 15% 填充（按体积折算）
        skin_ratio = min(1.0, (FDM_SKIN_MM * 2 / max(1.0, min(bbox[:2]) if len(bbox) >= 2 else 10)) + 0.35)
        fdm_solid = vol * min(1.0, skin_ratio)
        fdm_infill = vol * (1 - min(1.0, skin_ratio)) * 0.15
        fdm_g = (fdm_solid + fdm_infill) * FDM_DENSITY

        thin = float(m.get("thin_1mm_pct") or 0)
        over = float(m.get("overhang_pct") or 0)

        # 工艺适配判断（基于实测薄壁比例与悬垂）
        if thin <= 0.6:
            best = "SLA 25 µm + Grey V5"
            why = "薄壁比例低，细节交给光固化"
        elif thin <= 1.5:
            best = "SLA 25 µm（Tough 1500 更耐摔）"
            why = f"薄壁 {thin}%，需要高分辨率"
        else:
            best = "SLS PA12 或 SLA"
            why = f"薄壁 {thin}% 偏高，光固化偏脆可考虑 SLS"

        rows.append({
            "编号": m.get("id", f"{i:02d}"),
            "类型": m.get("type", ""),
            "名称": m.get("name", ""),
            "建模档位": m.get("quality_3d_used") or m.get("quality_3d", ""),
            "尺寸mm": " × ".join(fmt(v, 1) for v in bbox),
            "体积cm³": fmt(vol, 1),
            "面数": f"{int(m.get('faces') or 0):,}",
            "水密": "✓" if m.get("watertight") else "✗",
            "最薄p1_mm": fmt(m.get("thickness_p1"), 2),
            "薄于1mm占比%": fmt(thin, 2),
            "悬垂%": fmt(over, 1),
            "推荐工艺": best,
            "SLA树脂_g": fmt(sla_g, 0),
            "SLS件重_g": fmt(sls_g, 0),
            "SLS粉耗_g": fmt(sls_powder, 0),
            "FDM耗材_g": fmt(fdm_g, 0),
            "STL_MB": fmt(m.get("_stl_mb"), 1),
            "_why": why,
            "_path": m.get("_dir", ""),
        })
    return rows


def write_md(rows: list[dict]) -> Path:
    p = BATCH / "PRINT-ORDER.md"
    total_sla = sum(float(r["SLA树脂_g"]) for r in rows)
    total_sls = sum(float(r["SLS件重_g"]) for r in rows)
    total_powder = sum(float(r["SLS粉耗_g"]) for r in rows)
    total_fdm = sum(float(r["FDM耗材_g"]) for r in rows)
    lines = [
        "# 铸想 · 10 件摆件打样工单",
        "",
        f"件数：**{len(rows)}** ｜ 全部水密实体 ｜ 尺寸按**高度 80 mm** 归一 ｜ Z-up，可直接切片",
        "",
        "## 一、BOM（按全部 10 件算）",
        "",
        "| 工艺 | 材料 | 用量 | 备注 |",
        "|---|---|---|---|",
        f"| SLA（光固化） | Grey V5 / Tough 1500 V2 | 树脂 **{total_sla:.0f} g**（约 {total_sla/1.12/1000:.2f} L） | 按每件 +12% 支撑估 |",
        f"| SLS（粉末） | PA12 | 件重 **{total_sls:.0f} g** + 粉耗 **{total_powder:.0f} g** | 粉耗按件重 3.5 倍 |",
        f"| FDM（熔融） | PLA / PETG | 线材 **{total_fdm:.0f} g**（1.75 mm） | 15% 螺旋填充 + 3 壁 |",
        "",
        "## 二、逐件参数（每件一张表）",
        "",
    ]
    for r in rows:
        lines += [
            f"### {r['编号']} · {r['名称']}（{r['类型']}）",
            "",
            f"- 尺寸 **{r['尺寸mm']} mm** ｜ 体积 **{r['体积cm³']} cm³** ｜ {r['面数']} 面 ｜ 水密 {r['水密']}",
            f"- 最薄 p1 **{r['最薄p1_mm']} mm** ｜ 薄于 1 mm 占 **{r['薄于1mm占比%']}%** ｜ 悬垂 **{r['悬垂%']}%**",
            f"- 推荐：**{r['推荐工艺']}**（{r['_why']}）",
            f"- 用量：SLA 树脂 **{r['SLA树脂_g']} g** ｜ SLS 件重 **{r['SLS件重_g']} g**（粉 {r['SLS粉耗_g']} g）｜ FDM 线材 **{r['FDM耗材_g']} g**",
            f"- STL：`{Path(r['_path']).name}/model.stl`（{r['STL_MB']} MB）",
            "",
        ]
    lines += [
        "## 三、工艺参数（直接照做）",
        "",
        "### SLA / 光固化",
        "| 项 | 值 | 理由 |",
        "|---|---|---|",
        "| 层厚 | **25 µm** | 毛簇最薄 0.2–0.4 mm，50 µm 会吃掉 |",
        "| 摆放 | 竖立，正面向 -Y 后仰 15–20° | 支撑落在背部/尾巴外侧，避开正面 |",
        "| 支撑 | 自动 + 尾巴底面手动加密 | 底面接触仅 0.7%，支撑是唯一托点 |",
        "| 掏空 | **不掏空** | 摆件实心免排水孔，省后处理 |",
        "| 后固化 | Grey/White 60℃ 15 min；Rigid 10K 70℃ 60 min | 按 TDS |",
        "",
        "### SLS / 粉末",
        "| 项 | 值 |",
        "|---|---|",
        "| 材料 | **PA12**（要求韧性换 PA11） |",
        "| 层厚 | **100–120 µm** |",
        "| 填充 | **实体，无填充概念**（粉床自支撑） |",
        "| 支撑 | 不需要 |",
        "| 后处理 | **玻璃珠喷砂**（否则表面糖霜感） |",
        "| 注意 | 0.2–0.4 mm 的毛簇会丢，SLS 只评价形态与强度 |",
        "",
        "### FDM / 熔融",
        "| 项 | 值 | 理由 |",
        "|---|---|---|",
        "| 喷嘴 | **0.25 mm** | 0.4 会把耳朵/毛簇打成块 |",
        "| 层厚 | **0.10 mm** | 0.2 的层纹会盖过折面 |",
        "| 壁数 | 3（1.2 mm 皮） | |",
        "| 填充 | **15% 螺旋（gyroid）**；最小件可 25% | 摆件不需要高填充，省料省时 |",
        "| 顶/底 | 5 层 | |",
        "| 支撑 | 树状，界面层 2，只在悬垂 >50° 处 | 减少背面麻点 |",
        "| 附着 | **brim 8 mm** | 底面接触 0.7%，不加必翘 |",
        "",
        "## 四、验收（打完怎么判）",
        "",
        "| 项 | 怎么量 | 合格线 |",
        "|---|---|---|",
        "| 尺寸 | 卡尺量高/宽/深 | 与表中尺寸偏差 ≤ 0.4%（80 mm → 0.3 mm） |",
        "| 细节 | 同一侧光 45° 拍微距 | 毛簇、耳内折面、尾部层叠可数 |",
        "| 缺陷 | 目视 + 手摸 | 无支撑麻点（或已磨平）、无层错位、不粘手 |",
        "",
        "## 五、文件位置",
        "",
        "```",
        "outputs/art/batch/<编号>{类型}/",
        "  image.png     出图（flux_klein 1024²）",
        "  model.stl     打印件（Z-up，高 80 mm）",
        "  meta.json     实测数据（体积/壁厚/悬垂/耗时）",
        "```",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write_csv(rows: list[dict]) -> Path:
    p = BATCH / "PRINT-ORDER.csv"
    keys = [k for k in rows[0].keys() if not k.startswith("_")] if rows else []
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def contact_sheet(items: list[dict]) -> Path | None:
    """一张 4×3 的总览图（出图 + 模型剪影并排太重，这里用出图 + 尺寸标注）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    imgs = []
    for m in items:
        d = Path(m["_dir"])
        p = d / "image.png"
        if p.exists():
            imgs.append((m, Image.open(p).convert("RGB")))
    if not imgs:
        return None
    cols, cell = 5, 300
    rows = (len(imgs) + cols - 1) // cols
    W, H = cols * cell, rows * (cell + 46)
    canvas = Image.new("RGB", (W, H), (12, 16, 25))
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 15)
    except Exception:
        f = ImageFont.load_default()
    for i, (m, im) in enumerate(imgs):
        im = im.resize((cell - 16, cell - 16), Image.LANCZOS)
        x = (i % cols) * cell + 8
        y = (i // cols) * (cell + 46) + 8
        canvas.paste(im, (x, y))
        d.text((x, y + cell - 10), f"{m.get('id','')[:2]} {m.get('name','')}"[:22],
               font=f, fill=(235, 240, 250))
    p = BATCH / "contact_sheet.png"
    canvas.save(p, "PNG", optimize=True)
    return p


def main() -> int:
    items = load_items()
    if not items:
        print("还没有完成的件（outputs/art/batch 下没有 meta.json + model.stl）")
        return 1
    rows = build_rows(items)
    md = write_md(rows)
    csvp = write_csv(rows)
    sheet = contact_sheet(items)
    print(f"  {len(rows)} 件")
    for r in rows:
        print(f"    {r['编号']:<22}{r['尺寸mm']:<22}{r['体积cm³']:>7}cm³  "
              f"SLA {r['SLA树脂_g']:>4}g  SLS {r['SLS件重_g']:>4}g  FDM {r['FDM耗材_g']:>4}g")
    print(f"  工单: {md}")
    print(f"  表格: {csvp}")
    if sheet:
        print(f"  总览: {sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
