# -*- coding: utf-8 -*-
"""生成物统计分析（供 PPT 使用）
汇总所有生成物的：提示词、耗时、tokens 估算、分类
输出 stats.json
"""
import sys, os, csv, json, time
from pathlib import Path
from collections import Counter, defaultdict

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import tiktoken

WORKSPACE = Path("D:/AI-Workshop")
CONT = WORKSPACE / "outputs" / "continuous"

# tokenizer：优先 cl100k_base（已缓存则离线可用），失败回退到空格分词估算
_enc = None
try:
    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _enc = None


def text_tokens(s: str) -> int:
    if _enc is not None:
        try:
            return len(_enc.encode(s))
        except Exception:
            pass
    # 回退：英文按空格分词，中文按字符，×1.3 系数近似 BPE token
    import re
    words = len(re.findall(r"[A-Za-z0-9']+", s))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    return int((words + cjk) * 1.3)


def estimate_image_tokens(w: int, h: int) -> int:
    """图像 latent token 估算：FLUX 用 2x2 patch，latent 分辨率 = 图像/8
    512×512 → 64×64 = 4096 latent tokens（这是扩散模型的实际计算单元）
    """
    return (w // 8) * (h // 8)


def estimate_3d_tokens(file_mb: float) -> int:
    """3D 模型内容量估算：按文件大小近似（GLB 含几何+纹理）
    用「三角形面数」更准，但无解析器时按大小粗估
    """
    # 经验值：~100KB ≈ 1k 三角形（含纹理压缩）
    return int(file_mb * 1024 / 100) * 1000


def load_images_csv():
    """合并 generation_log.csv 和 multi_image.csv 的图像记录"""
    rows = []
    for f in [CONT / "generation_log.csv", CONT / "multi_image.csv"]:
        if not f.exists():
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            for r in rd:
                if r.get("success", "") == "True" and r.get("path"):
                    rows.append(r)
    return rows


def main():
    img_rows = load_images_csv()

    # ── 图像统计 ──
    img_count = len(img_rows)
    img_times = [float(r["elapsed_s"]) if "elapsed_s" in r else float(r.get("elapsed", 0))
                 for r in img_rows if r.get("elapsed_s") or r.get("elapsed")]
    img_prompts = [r["prompt"] for r in img_rows if r.get("prompt")]
    img_tokens_text = [text_tokens(p) for p in img_prompts]

    # 提示词去重 + 频次
    prompt_counter = Counter(img_prompts)

    # ── 3D 统计 ──
    td_rows = []
    if (CONT / "multi_3d.csv").exists():
        with open(CONT / "multi_3d.csv", newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for r in rd:
                if r.get("success", "") == "True":
                    td_rows.append(r)
    td_count = len(td_rows)
    td_times = [float(r["elapsed"]) for r in td_rows if r.get("elapsed")]

    # ── 3D GLB 文件统计 ──
    td_files = list((WORKSPACE / "outputs" / "3d").glob("*.glb"))
    td_sizes_mb = [p.stat().st_size / (1024 * 1024) for p in td_files]

    # ── 汇总 ──
    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": {
            "count": img_count,
            "avg_time_s": round(sum(img_times) / len(img_times), 2) if img_times else 0,
            "min_time_s": round(min(img_times), 2) if img_times else 0,
            "max_time_s": round(max(img_times), 2) if img_times else 0,
            "resolution": "512×512",
            "latent_tokens_per_image": estimate_image_tokens(512, 512),
            "total_latent_tokens": img_count * estimate_image_tokens(512, 512),
            "avg_prompt_tokens": round(sum(img_tokens_text) / len(img_tokens_text), 1) if img_tokens_text else 0,
            "total_prompt_tokens": sum(img_tokens_text),
            "unique_prompts": len(prompt_counter),
            "top_prompts": prompt_counter.most_common(8),
        },
        "3d": {
            "count": td_count,
            "avg_time_s": round(sum(td_times) / len(td_times), 2) if td_times else 0,
            "min_time_s": round(min(td_times), 2) if td_times else 0,
            "max_time_s": round(max(td_times), 2) if td_times else 0,
            "file_count": len(td_files),
            "avg_size_mb": round(sum(td_sizes_mb) / len(td_sizes_mb), 2) if td_sizes_mb else 0,
            "total_size_mb": round(sum(td_sizes_mb), 2),
            "estimated_triangles": estimate_3d_tokens(sum(td_sizes_mb) / len(td_sizes_mb)) if td_sizes_mb else 0,
        },
        "video": {
            "count": 0,
            "note": "视频线未启用（320W 功耗墙下温度风险高，宇凯决定暂缓）",
        },
    }

    out = WORKSPACE / "outputs" / "continuous" / "stats.json"
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    # 打印摘要
    print(f"=== 生成物统计 ===")
    print(f"图像: {img_count} 张 | 平均 {stats['image']['avg_time_s']}s/张 | "
          f"总 latent tokens {stats['image']['total_latent_tokens']:,} | "
          f"平均提示词 {stats['image']['avg_prompt_tokens']} tokens")
    print(f"3D:   {td_count} 个 | 平均 {stats['3d']['avg_time_s']}s/个 | "
          f"平均 {stats['3d']['avg_size_mb']}MB | 约 {stats['3d']['estimated_triangles']:,} 三角形")
    print(f"视频: 0 个（未启用）")
    print(f"\nTop 提示词:")
    for p, c in prompt_counter.most_common(8):
        print(f"  {c:3d}x  {p[:60]}")

    return stats


if __name__ == "__main__":
    main()
