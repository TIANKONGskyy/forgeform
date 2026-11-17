# -*- coding: utf-8 -*-
"""装完就跑一遍真实链路，把**这台机器自己的**耗时写成样本。

## 为什么必须在这台机器上跑

`backend/timings.py` 的 `MEASURED` 和 `backend/model_info.py` 的厂商信息，
前者是某台开发机某天的数据，后者是模型本身的元信息。**同一个模型换显卡，
3D 建模能差 4 倍**（V100 32G 上 512oct 约 2 分 14 秒，6G 笔记本实测 3.5 分钟，
再小的卡直接 OOM）。

所以出厂包里**不带任何耗时样本** —— `outputs/` 整个目录都不进 git、不进 zip。
每台机器装完跑一次这个脚本，它把自己的真实数字写进：

    outputs/timings_local.jsonl   每阶段一条 {stage, sec, at, note}
    outputs/llm_bench.jsonl       对话吞吐 {predicted_tok_s, prompt_tok_s, ...}

之后 UI（侧栏「模型详情」、进度环）读的就是本机数据。

## 跑什么

按"装了什么"决定，缺的东西跳过而不是报错：

    llm     对话吞吐（tok/s）—— 只要本地有 OpenAI 兼容服务就能测，2 秒
    image   出图（512 预览）—— 需要出图模型 + ComfyUI/diffusers
    hd      高清重绘 1024   —— 同 image
    3d      建模              —— 需要建模模型；用 image 那张图当输入
    stl     GLB → STL         —— 需要 3d 成功

## 用法

    PYTHONPATH= python tools/calibrate.py            # 全跑（缺的跳过）
    PYTHONPATH= python tools/calibrate.py --quick    # 只测对话，2 秒
    PYTHONPATH= python tools/calibrate.py --only 3d  # 只补某一项
    PYTHONPATH= python tools/calibrate.py --reset    # 先清空旧样本再测

装完自动跑一次：`tools/setup.py` 和 Studio 的安装流程在成功后调 `calibrate --quick`，
完整链路（出图/建模）留给用户第一次真正生成时自然累积 —— 那时模型已经在显存里，
测出来的数字比冷启动校准更接近日常体验。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PY = sys.executable
ENV_HINT = 'PYTHONPATH= PYTHONIOENCODING=utf-8'


def _say(msg: str) -> None:
    print(msg, flush=True)


def _write_stage(stage: str, sec: float, note: str = "") -> None:
    try:
        import backend.timings as T
        T.record(stage, sec, note=note)
        _say(f"  ✓ {stage}: {sec:.1f}s 已记进本机样本")
    except Exception as exc:
        _say(f"  ✗ {stage} 记录失败: {type(exc).__name__}: {exc}")


# ── 各阶段 ───────────────────────────────────────────────────
def calib_llm() -> bool:
    """对话吞吐。找一个活着的 OpenAI 兼容服务测一次。"""
    _say("[1/5] 对话吞吐")
    try:
        import backend.model_info as MI
    except Exception as exc:
        _say(f"  ✗ 无法导入 model_info: {exc}")
        return False

    candidates = []
    try:
        import backend.describer as D
        base, model = D._strata_cfg()
        candidates.append((base, model))
    except Exception:
        pass
    candidates.append(("http://127.0.0.1:8091/v1", "qwen3.8-flash-next-iq3_s"))
    candidates.append(("http://127.0.0.1:11434/v1", "qwen3.5:2b-q4_K_M"))

    seen = set()
    for base, model in candidates:
        if (base, model) in seen:
            continue
        seen.add((base, model))
        try:
            rec = MI.bench_llm(base, model, timeout=180)
        except Exception as exc:
            _say(f"  · {base} 不可用（{type(exc).__name__}），跳过")
            continue
        if float(rec.get("predicted_tok_s") or 0) <= 0:
            _say(f"  · {base} 没返回 timings，跳过")
            continue
        _say(f"  ✓ {model} @ {base}: {rec['predicted_tok_s']} tok/s"
             f"（预填 {rec['prompt_tok_s']}）")
        return True
    _say("  · 没有可用的对话服务 —— 装 Ollama 或让 Strata 起来后重跑")
    return False


def _pipeline():
    """按 config.json 建一个 Pipeline（失败返回 None）。"""
    try:
        from backend.pipeline import Pipeline
        return Pipeline(str(ROOT / "config.json"))
    except Exception as exc:
        _say(f"  ✗ Pipeline 建不起来: {type(exc).__name__}: {exc}")
        return None


def _installed(pipe, stage: str, key: str) -> bool:
    """这个模型文件到底在不在。"""
    try:
        cfg = ((pipe.config.get("models") or {}).get(stage) or {}).get(key) or {}
        p = cfg.get("path") or ""
        if not p:
            return bool(cfg.get("checkpoint"))
        return Path(p).exists()
    except Exception:
        return False


def _default_key(pipe, stage: str, fallback: str) -> str:
    try:
        return ((pipe.config.get("stages") or {}).get(stage) or {}).get(
            "default_model") or fallback
    except Exception:
        return fallback


def calib_image(pipe) -> str:
    """出图（512 预览）。成功返回图片路径，供 3D 当输入。"""
    _say("[2/5] 出图 512 预览")
    key = _default_key(pipe, "image", "z_image")
    if not _installed(pipe, "image", key):
        _say(f"  · 出图模型 {key} 没装，跳过")
        return ""
    t0 = time.time()
    try:
        results = pipe.generate_images(
            "a small red apple on pure white background",
            count=1, resolution="512×512", quality_name="快速",
            model_key=key, skip_cache=True)
    except Exception as exc:
        _say(f"  ✗ 出图异常: {type(exc).__name__}: {exc}")
        return ""
    el = time.time() - t0
    ok = [r for r in (results or []) if getattr(r, "success", False) and r.output_path]
    stage = "image_512" if key != "qwen21_ref" else "qwen21_t2i"
    if not ok:
        _say(f"  ✗ 出图失败（{el:.0f}s）")
        return ""
    _write_stage(stage, el, note=key)
    return ok[0].output_path


def calib_hd(pipe, img: str) -> None:
    _say("[3/5] 高清重绘 1024")
    if not img:
        _say("  · 没有输入图，跳过")
        return
    key = _default_key(pipe, "image", "z_image")
    t0 = time.time()
    try:
        r = pipe.enhance_image(img, "a small red apple, pure white background",
                               model_key=key, quality_name="标准",
                               resolution="1024×1024")
    except Exception as exc:
        _say(f"  ✗ 高清重绘异常: {type(exc).__name__}: {exc}")
        return
    el = time.time() - t0
    if getattr(r, "success", False):
        _write_stage("hd_1024", el, note=key)
    else:
        _say(f"  ✗ 高清重绘失败（{el:.0f}s）")


def calib_3d(pipe, img: str) -> str:
    """建模。返回 GLB 路径供 STL 用。"""
    _say("[4/5] 3D 建模")
    if not img:
        _say("  · 没有输入图，跳过")
        return ""
    key = _default_key(pipe, "3d", "hunyuan_mini")
    if not _installed(pipe, "3d", key):
        _say(f"  · 建模模型 {key} 没装，跳过")
        return ""
    t0 = time.time()
    try:
        r = pipe.generate_3d(model_key=key, quality_name="快速", image_path=img)
    except Exception as exc:
        _say(f"  ✗ 建模异常: {type(exc).__name__}: {exc}")
        return ""
    el = time.time() - t0
    if not (r and getattr(r, "success", False) and r.output_path):
        _say(f"  ✗ 建模失败（{el:.0f}s）")
        return ""
    try:
        import backend.timings as T
        _write_stage(T.stage_for_3d(key, "快速"), el, note=key)
    except Exception:
        _write_stage("3d_fast", el, note=key)
    try:
        import trimesh
        m = trimesh.load(r.output_path, force="mesh")
        _say(f"    面数 {len(m.faces):,}  水密 {m.is_watertight}")
    except Exception:
        pass
    return r.output_path


def calib_stl(pipe, glb: str) -> None:
    _say("[5/5] GLB → STL")
    if not glb:
        _say("  · 没有 GLB，跳过")
        return
    t0 = time.time()
    try:
        # export_stl 返回 StageResult（不是 (ok, path)），按属性读，别解包
        _res = pipe.export_stl(glb, target_size_mm=120.0)
        ok, path = bool(getattr(_res, "success", False)), getattr(_res, "output_path", "")
    except Exception as exc:
        _say(f"  ✗ 导出异常: {type(exc).__name__}: {exc}")
        return
    el = time.time() - t0
    if not ok:
        _say("  ✗ 导出失败")
        return
    try:
        n = Path(path).stat().st_size
    except Exception:
        n = 0
    _write_stage("stl_big" if n > 50 * 1024 * 1024 else "stl", el)


# ── 入口 ─────────────────────────────────────────────────────
def reset() -> None:
    for name in ("timings_local.jsonl", "llm_bench.jsonl"):
        p = ROOT / "outputs" / name
        if p.exists():
            p.unlink()
            _say(f"  已清空 {p.name}")


def summary() -> None:
    _say("")
    _say("本机样本汇总：")
    try:
        import backend.timings as T
        import backend.model_info as MI
        for stage in ("image_512", "qwen21_t2i", "hd_1024",
                      "3d_fast", "3d_std", "3d_multiview", "stl", "stl_big"):
            n = T.samples(stage)
            if n <= 0:
                continue
            med, hi = T.estimate(stage)
            src = "本机" if T.is_local(stage) else "出厂参考"
            _say(f"  {T.label(stage):<22} 中位 {T.human(med):<12}"
                 f" 最长 {T.human(hi):<12} n={n}  {src}")
        b = MI.llm_bench()
        if b.get("n"):
            _say(f"  对话吞吐                 中位 {b['predicted_tok_s']} tok/s"
                 f"（{b['predicted_min']}–{b['predicted_max']}）n={b['n']}")
    except Exception as exc:
        _say(f"  汇总失败: {exc}")
    _say("")
    _say(f"这些数字只存在这台机器上（{ROOT / 'outputs'}），不进 git、不进发布包。")


def main() -> int:
    ap = argparse.ArgumentParser(description="实测本机各阶段耗时，写成样本")
    ap.add_argument("--quick", action="store_true", help="只测对话吞吐（约 2 秒）")
    ap.add_argument("--only", default="", help="只跑一项：llm/image/hd/3d/stl")
    ap.add_argument("--reset", action="store_true", help="先清空旧样本")
    ap.add_argument("--summary", action="store_true", help="只看汇总，不跑")
    args = ap.parse_args()

    _say("=" * 62)
    _say("  铸想 ForgeForm · 本机实测校准")
    _say(f"  目录 {ROOT}")
    _say("=" * 62)

    if args.reset:
        reset()
    if args.summary:
        summary()
        return 0

    only = (args.only or "").strip().lower()
    if args.quick:
        only = "llm"

    if not only or only == "llm":
        calib_llm()
    if args.quick:
        summary()
        return 0

    pipe = None
    img = ""
    glb = ""
    if not only or only in ("image", "hd", "3d", "stl"):
        pipe = _pipeline()
    if pipe is not None:
        if not only or only == "image":
            img = calib_image(pipe)
        if not only or only == "hd":
            calib_hd(pipe, img)
        if not only or only == "3d":
            glb = calib_3d(pipe, img)
        if not only or only == "stl":
            calib_stl(pipe, glb)
    elif only:
        _say("  · 没有 Pipeline，跳过生成类项目")

    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
