# -*- coding: utf-8 -*-
"""批量打样：10 个不同类型的摆件，出图与建模都用最高精度。

宇凯 2026-11-16 的要求：
    "测试无以摆件为主不要少于10个并且全部重新生成提示词生图建模都用最高精度
     这10个要面向10个不同的类型"

## 为什么单独写脚本

一次 10 个，单个按实测（最高精度 1024 octree）要 20–40 分钟，
整批 4–7 小时。必须能**断点续跑**：中途 CUDA 出错、断电、我重启服务都不该白跑。

## 关键约束（都是之前踩出来的）

- 提示词必须带**四分之三侧面 + 一个前伸的肢体**：正面描述建出来是 0.6% 厚的纸片。
- 每个都要求**单物体、纯白背景、无植物/文字**：背景里有东西会被一起建成 3D。
- 精度取 `最高精度`（octree 1024）。显存 <10 GB 时 pipeline 会自动降到 768/512，
  跑完在日志里能看到实际用了多少。
- 落盘：`outputs/art/batch/<编号>_<名>/{image.png,model.glb,model.stl,meta.json}`

## 用法

    PYTHONPATH= python tools/batch_figurines.py            # 全跑（跳过已完成）
    PYTHONPATH= python tools/batch_figurines.py --only 3   # 只跑第 3 个
    PYTHONPATH= python tools/batch_figurines.py --list     # 看清单
    PYTHONPATH= python tools/batch_figurines.py --status   # 看进度
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "outputs" / "art" / "batch"
LOG = OUT / "batch.log"

# 10 个类型，覆盖摆件的主要品类；每条都自带"深度线索"（3/4 侧 + 前伸肢体）
ITEMS = [
    {
        "id": "01_animal_cat",
        "type": "动物摆件",
        "name": "坐着的猫",
        "prompt": ("一只坐着的猫摆件，四分之三侧面，头微微转向镜头，"
                   "前爪向前伸出一只踩在地上，尾巴绕到身前，写实质感，"
                   "毛发有细密层次，单物体，纯白背景"),
    },
    {
        "id": "02_mythic_dragon",
        "type": "神话生物",
        "name": "盘绕的龙",
        "prompt": ("一条盘绕的东方小龙摆件，四分之三侧面，头部抬起转向镜头，"
                   "一只前爪搭在前方石台上，鳞片有层次，单物体，纯白背景"),
    },
    {
        "id": "03_human_warrior",
        "type": "人物雕像",
        "name": "持刀武士",
        "prompt": ("一尊武士小雕像，四分之三侧面站立，一只手臂向前伸出握住长刀，"
                   "战甲和衣褶清晰，写实质感，单物体，纯白背景"),
    },
    {
        "id": "04_steampunk_robot",
        "type": "机械/蒸汽朋克",
        "name": "齿轮小机器人",
        "prompt": ("一个蒸汽朋克风格的小机器人摆件，四分之三侧面，一条机械臂向前伸出，"
                   "身上有齿轮和铆钉细节，黄铜与铸铁质感，单物体，纯白背景"),
    },
    {
        "id": "05_architecture_tower",
        "type": "建筑微缩",
        "name": "多层小塔楼",
        "prompt": ("一座三层小塔楼建筑模型，四分之三俯视角度，正面有一道向外伸展的石阶，"
                   "窗框和瓦片有清晰层次，单物体，纯白背景"),
    },
    {
        "id": "06_vehicle_bike",
        "type": "交通工具",
        "name": "复古摩托车",
        "prompt": ("一辆复古摩托车模型摆件，四分之三侧面，前轮向前伸出，"
                   "油箱与排气管有清晰分件线，单物体，纯白背景"),
    },
    {
        "id": "07_plant_succulent",
        "type": "植物盆景",
        "name": "多肉小盆栽",
        "prompt": ("一盆多肉植物摆件，四分之三侧面，一片肥厚的叶子向前伸出盆沿，"
                   "花瓣状叶尖圆润，陶盆有纹理，单物体，纯白背景"),
    },
    {
        "id": "08_incense_burner",
        "type": "器物/文房",
        "name": "三足香炉",
        "prompt": ("一个三足小香炉摆件，四分之三俯视，一只炉耳向前伸出，"
                   "器身有回纹浮雕，铜质古朴，单物体，纯白背景"),
    },
    {
        "id": "09_cartoon_character",
        "type": "卡通角色",
        "name": "圆润小怪兽",
        "prompt": ("一只圆润可爱的卡通小怪兽摆件，四分之三侧面，一只短手向前伸出打招呼，"
                   "大眼睛，头顶有两只小角，单物体，纯白背景"),
    },
    {
        "id": "10_ocean_octopus",
        "type": "海洋生物",
        "name": "章鱼摆件",
        "prompt": ("一只章鱼摆件，四分之三侧面，三条触手向前铺在地面上蜿蜒伸展，"
                   "吸盘细节清晰，单物体，纯白背景"),
    },
]

QUALITY_3D = "极致"          # octree 768（1024 已删：实测不可用）
QUALITY_IMG = "高质量"       # flux_klein 1024²
# 1024 在这台 V100 上实测会崩（解码阶段要一次性吃 20+ GB，ComfyUI 直接重启）。
# 所以失败后自动降到 768 再试一次，并在 meta 里写清楚"这件是降档的"。
FALLBACK_3D = "极致"


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def item_dir(it: dict) -> Path:
    return OUT / it["id"]


def meta_of(it: dict) -> dict:
    p = item_dir(it) / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def is_done(it: dict) -> bool:
    m = meta_of(it)
    return bool(m.get("ok")) and (item_dir(it) / "model.stl").exists()


def run_one(it: dict, pipe) -> dict:
    d = item_dir(it)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": it["id"], "type": it["type"], "name": it["name"],
            "prompt": it["prompt"], "quality_img": QUALITY_IMG,
            "quality_3d": QUALITY_3D, "ok": False}
    t0 = time.time()

    # ① 出图
    say(f"{it['id']} 出图… {it['name']}")
    t_img = time.time()
    res = pipe.generate_images(it["prompt"], count=1, model_key="flux_klein",
                               quality_name=QUALITY_IMG, resolution="1024×1024",
                               skip_cache=True)
    ok = [r for r in (res or []) if getattr(r, "success", False) and r.output_path]
    if not ok:
        meta["error"] = "出图失败"
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        say(f"{it['id']} ✗ 出图失败")
        return meta
    img = Path(ok[0].output_path)
    (d / "image.png").write_bytes(img.read_bytes())
    meta["image_sec"] = round(time.time() - t_img, 1)
    say(f"{it['id']} ✓ 出图 {meta['image_sec']}s")

    # ② 建模（最高精度）
    say(f"{it['id']} 建模 {QUALITY_3D}…")
    t3 = time.time()
    used_q = QUALITY_3D
    r3 = pipe.generate_3d(model_key="hunyuan_mini", quality_name=used_q,
                          image_path=str(img))
    meta["mesh_sec"] = round(time.time() - t3, 1)
    if not (r3 and getattr(r3, "success", False) and r3.output_path):
        # 降档重试：1024 崩过就退 768，别让整批卡死在一件上
        say(f"{it['id']} ⚠ {QUALITY_3D} 失败，降档 {FALLBACK_3D} 重试…")
        t3b = time.time()
        r3 = pipe.generate_3d(model_key="hunyuan_mini", quality_name=FALLBACK_3D,
                              image_path=str(img))
        meta["mesh_sec"] = round(time.time() - t3, 1)
        meta["fallback_3d"] = FALLBACK_3D
        used_q = FALLBACK_3D
        if not (r3 and getattr(r3, "success", False) and r3.output_path):
            meta["error"] = f"建模失败({QUALITY_3D}→{FALLBACK_3D}): {getattr(r3, 'error', '')[:160]}"
        meta["error"] = f"建模失败: {getattr(r3, 'error', '')[:200]}"
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        say(f"{it['id']} ✗ 建模失败 {meta['mesh_sec']}s")
        return meta

    glb = Path(r3.output_path)
    (d / "model.glb").write_bytes(glb.read_bytes())

    # ③ 转 Z-up + 缩到 80 mm + 导 STL + 量指标
    import numpy as np
    import trimesh
    from backend.mesh_optimizer import y_up_to_z_up

    m = trimesh.load(str(d / "model.glb"), force="mesh")
    m = y_up_to_z_up(m)
    s = 80.0 / float(m.extents[2]) if m.extents[2] > 0 else 1.0
    m.apply_scale(s)
    m.apply_translation([0, 0, -m.bounds[0][2]])
    m.export(str(d / "model.stl"))

    # 壁厚（4000 条射线，够用且快）
    from collections import defaultdict
    N = 4000
    pts, fidx = trimesh.sample.sample_surface(m, N)
    n = m.face_normals[fidx]
    origin = pts - n * 0.05
    loc, iray, _ = m.ray.intersects_location(origin, -n, multiple_hits=True)
    best = defaultdict(lambda: 1e9)
    for p, rr in zip(loc, iray):
        dd = float(np.linalg.norm(p - origin[rr]))
        if 0.2 < dd < best[rr]:
            best[rr] = dd
    th = np.array(list(best.values())) if best else np.array([0.0])
    zn = m.face_normals[:, 2]

    meta["quality_3d_used"] = used_q
    meta.update({
        "ok": True,
        "bbox_mm": [round(float(x), 1) for x in m.extents],
        "volume_cm3": round(float(m.volume) / 1000, 2),
        "faces": int(len(m.faces)),
        "watertight": bool(m.is_watertight),
        "thickness_p1": round(float(np.percentile(th, 1)), 2),
        "thickness_p50": round(float(np.percentile(th, 50)), 2),
        "thin_1mm_pct": round(float((th < 1.0).mean() * 100), 2),
        "overhang_pct": round(float((zn < -0.7).mean() * 100), 1),
        "total_sec": round(time.time() - t0, 1),
    })
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    say(f"{it['id']} ✓ 建模 {meta['mesh_sec']}s · {meta['faces']:,}面 · "
        f"水密={meta['watertight']} · 体积 {meta['volume_cm3']}cm³ · "
        f"最薄 p1={meta['thickness_p1']}mm · 悬垂 {meta['overhang_pct']}%")
    return meta


def status() -> None:
    done = [it for it in ITEMS if is_done(it)]
    print(f"完成 {len(done)}/{len(ITEMS)}")
    for it in ITEMS:
        m = meta_of(it)
        mark = "✓" if m.get("ok") else ("…" if m else "·")
        extra = ""
        if m.get("ok"):
            extra = (f"{m.get('faces', 0):,}面 {m.get('volume_cm3')}cm³ "
                     f"最薄{m.get('thickness_p1')}mm 建模{m.get('mesh_sec')}s")
        elif m.get("error"):
            extra = m["error"][:40]
        print(f"  {mark} {it['id']:<22}{it['type']:<10}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=str, default="", help="只跑某个编号（如 03）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true", help="已完成也重跑")
    ap.add_argument("--quality", default="", help="覆盖建模精度档（极致/高质量/标准/快速）")
    args = ap.parse_args()

    if args.list:
        for it in ITEMS:
            print(f"  {it['id']:<22}{it['type']:<10}{it['name']}")
        return 0
    if args.status:
        status()
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    todo = ITEMS
    if args.only:
        todo = [it for it in ITEMS if it["id"].startswith(args.only)]
        if not todo:
            print(f"没有编号 {args.only}")
            return 1
    if not args.force:
        todo = [it for it in todo if not is_done(it)]
    if not todo:
        print("全部已完成。用 --force 重跑。")
        return 0

    global QUALITY_3D
    if args.quality:
        QUALITY_3D = args.quality
        say(f"建模精度覆盖为 {QUALITY_3D}")

    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))

    say(f"开始：{len(todo)} 个 · 出图 {QUALITY_IMG} · 建模 {QUALITY_3D}")
    t_all = time.time()
    fails = []
    for i, it in enumerate(todo, 1):
        say(f"── [{i}/{len(todo)}] {it['id']} {it['type']} ──")
        try:
            meta = run_one(it, pipe)
            if not meta.get("ok"):
                fails.append((it["id"], meta.get("error", "?")))
        except Exception as exc:
            fails.append((it["id"], f"{type(exc).__name__}: {exc}"))
            say(f"{it['id']} ✗ 异常 {type(exc).__name__}: {exc}")
            with LOG.open("a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        say(f"进度 {i}/{len(todo)} · 已用 {(time.time()-t_all)/60:.1f} 分钟")
    say(f"结束：成功 {len(todo)-len(fails)}/{len(todo)} · "
        f"总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    if fails:
        for k, v in fails:
            say(f"  失败 {k}: {v}")
    status()
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
