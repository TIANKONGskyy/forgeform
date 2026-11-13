# -*- coding: utf-8 -*-
"""铸想全链路健康检查 —— 一次跑完所有能自动验的东西。

    python tools/healthcheck.py            # 全跑
    python tools/healthcheck.py --fast     # 跳过需要加载模型的项

不跑真实建模（那个 3 分钟），但会检查：
  · 插件注册与配置引用
  · 推荐系统在各种硬件组合下的输出
  · 三个后端能否构造
  · Studio 的 HTTP 端点
  · 更新器 / 安装器的关键文件
  · 文档里提到的东西是否真实存在
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[dict] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        RESULTS.append({"check": name, "ok": True, "detail": str(detail or "")})
        print(f"  [OK ] {name:<38} {detail or ''}")
    except Exception as exc:
        RESULTS.append({"check": name, "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}"})
        print(f"  [FAIL] {name:<38} {type(exc).__name__}: {exc}")
        if "--trace" in sys.argv:
            traceback.print_exc()


# ── 1. 插件与配置 ────────────────────────────────────────────
def plugins_load():
    from backend.plugins.spec import registry
    r = registry(reload=True)
    imgs = r.of_kind("image")
    meshes = r.of_kind("mesh")
    assert imgs, "没有图像插件"
    assert meshes, "没有建模插件"
    for p in imgs:
        assert p.pipeline, f"{p.id} 缺 pipeline"
    for p in meshes:
        assert p.loader, f"{p.id} 缺 loader"
    return f"图像 {len(imgs)} 个 · 建模 {len(meshes)} 个"


def config_refs():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    from backend.plugins.spec import registry
    r = registry()
    img_ids = {p.id for p in r.of_kind("image")}
    mesh_ids = {p.id for p in r.of_kind("mesh")}
    problems = []
    for key, node in (cfg.get("models") or {}).get("image", {}).items():
        if key not in img_ids:
            problems.append(f"image.{key} 没有对应插件")
    for key, node in (cfg.get("models") or {}).get("3d", {}).items():
        has_path = bool(node.get("path") or node.get("repo") or node.get("checkpoint"))
        if key not in mesh_ids and not has_path:
            problems.append(f"3d.{key} 既没插件也没 path")
    for stage in ("image", "3d"):
        d = (cfg.get("stages") or {}).get(stage, {}).get("default_model")
        table = (cfg.get("models") or {}).get(stage, {})
        if d and d not in table and d not in (img_ids | mesh_ids):
            problems.append(f"stages.{stage}.default_model={d} 不存在")
    return f"{len(problems)} 个问题" + ((": " + "; ".join(problems[:3])) if problems else "")


# ── 2. 推荐系统 ──────────────────────────────────────────────
def recommend_matrix():
    from backend.backends.recommend import Hardware, build_profiles, list_llm_choices, pick_mesh
    cases = [
        ("8G/16G 空机", Hardware(8, 7, 16, 100, "GPU"), {}),
        ("16G/32G diffusers", Hardware(16, 14, 32, 200, "GPU"), {"diffusers": True, "diffusers_models": ["x"]}),
        ("32G/128G ComfyUI", Hardware(32, 30, 128, 500, "GPU"), {"comfyui": True, "comfyui_3d": True, "comfyui_models": ["a"]}),
        ("无 GPU", Hardware(0, 0, 32, 200, "CPU"), {}),
    ]
    lines = []
    for label, hw, assets in cases:
        ps = build_profiles(hw, assets)
        assert ps, f"{label} 没出方案"
        p = ps[0]
        llm = [d for d in p.downloads if d.kind == "llm"]
        lines.append(f"{label}:{p.key}/{p.image_model}/{p.mesh_model}/{llm[0].repo if llm else '-'}")
    return " | ".join(lines)


def mesh_choice_matrix():
    from backend.backends.recommend import pick_mesh
    out = []
    for label, assets, vram, ram in [
        ("8G16G-noComfy", {"comfyui_3d": False}, 8, 16),
        ("8G16G-Comfy", {"comfyui_3d": True}, 8, 16),
        ("16G32G-noComfy", {"comfyui_3d": False}, 16, 32),
    ]:
        mid, why = pick_mesh(assets, vram, ram)
        out.append(f"{label}->{mid}")
    return " | ".join(out)


# ── 3. 后端 ─────────────────────────────────────────────────
def backends_construct():
    from backend.backends import direct, hybrid
    from backend.backends.direct import DirectBackend
    from backend.backends.hybrid import HybridBackend

    assert hasattr(DirectBackend, "_run_3d"), "DirectBackend 缺 _run_3d"
    assert hasattr(DirectBackend, "_run_image"), "DirectBackend 缺 _run_image"
    for m in ("build_workflow", "submit_workflow", "wait_for_completion",
              "get_result_image", "get_result_3d", "upload_image", "pick", "report"):
        assert hasattr(HybridBackend, m), f"HybridBackend 缺 {m}"
    assert hasattr(direct, "low_memory_mode"), "缺 low_memory_mode"
    return "DirectBackend · HybridBackend · ComfyUI 走 hybrid 内的客户端"


def quant_ladder():
    from backend.backends.quant import choose_quant, recommend_quant
    got = []
    for est, free in ((12, 8), (40, 30), (6, 24)):
        r = recommend_quant(est, free, 32, "auto", False, "image")
        got.append(f"{est}/{free}->{r.get('quant')}")
    return " | ".join(got)


# ── 4. Studio ───────────────────────────────────────────────
def studio_import():
    from backend import studio
    for name in ("Studio", "make_server"):
        assert hasattr(studio, name), f"studio 缺 {name}"
    s = studio.Studio
    for m in ("catalog", "install", "purge_plan", "purge", "remove_model"):
        assert hasattr(s, m), f"Studio 缺 {m}"
    return "Studio 接口齐全（含 purge / remove_model）"


# ── 5. 文件与文档 ───────────────────────────────────────────
def entrypoints():
    need = [
        "app.py", "config.example.json", "requirements.txt", "VERSION",
        "启动.bat", "start.ps1", "更新.bat", "update.ps1", "install.ps1",
        "tools/setup.py", "tools/updater.py", "tools/doctor.py",
        "tools/e2e_full.py", "tools/trim_base.py", "tools/probe_lazy_load.py",
        "backend/backends/lazy_gpu_load.py", "backend/backends/triposr_mc_patch.py",
        "ui/studio.html", "ui/studio.js", "ui/studio.css",
        ".github/workflows/ci.yml", ".github/workflows/release.yml",
    ]
    missing = [f for f in need if not (ROOT / f).exists()]
    assert not missing, f"缺 {missing}"
    return f"{len(need)} 个入口文件齐全"


def docs_exist():
    need = ["README.md", "README.en.md", "SECURITY.md", "NOTICE", "CONTRIBUTING.md",
            "docs/HARDWARE.md", "docs/MODELS.md", "docs/CONFIG.md", "docs/PLUGINS.md",
            "docs/BACKENDS.md", "docs/ARCHITECTURE.md", "docs/DECISIONS.md",
            "docs/MODEL-BENCH.md", "docs/EXTENSIBILITY.md",
            "skills/forgeform/SKILL.md"]
    missing = [f for f in need if not (ROOT / f).exists()]
    assert not missing, f"缺 {missing}"
    return f"{len(need)} 份文档齐全"


def updater_dryrun():
    import tempfile
    tmp = tempfile.mkdtemp()
    pkg = Path(tmp) / "forgeform-v9.9.9"
    (pkg / "backend").mkdir(parents=True)
    (pkg / "app.py").write_text("# fake", encoding="utf-8")
    (pkg / "VERSION").write_text("9.9.9", encoding="utf-8")
    inst = Path(tmp) / "inst"
    (inst / "backend").mkdir(parents=True)
    (inst / "app.py").write_text("# old", encoding="utf-8")
    (inst / "config.json").write_text('{"keep":1}', encoding="utf-8")
    # 子进程也要 UTF-8 —— updater.py 会打中文 + ✓，GBK 控制台会 UnicodeEncodeError
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "updater.py"),
                        "--from", str(pkg), "--to", str(inst)],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr[:300]
    assert (inst / "VERSION").read_text(encoding="utf-8").strip() == "9.9.9"
    assert (inst / "config.json").read_text(encoding="utf-8") == '{"keep":1}', "config 被覆盖"
    return "合并正确，config 保留"


# ── 6. pytest / verify ─────────────────────────────────────
def run_pytest():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=900,
                       env={**__import__("os").environ, "PYTHONPATH": ""})
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:]
    assert r.returncode == 0, (tail[0] if tail else "pytest 失败")
    return tail[0].strip()


def run_verify():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "verify.py")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600,
                       env={**__import__("os").environ, "PYTHONPATH": ""})
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:]
    assert r.returncode == 0, (tail[0] if tail else "verify 失败")
    return tail[0].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过 pytest / verify")
    args = ap.parse_args()

    print("=" * 78)
    print("铸想 · 全链路健康检查")
    print("=" * 78)

    print("\n[结构]")
    check("插件注册", plugins_load)
    check("config 引用一致性", config_refs)
    check("入口文件", entrypoints)
    check("文档", docs_exist)
    check("Studio 接口", studio_import)

    print("\n[逻辑]")
    check("推荐系统（4 种硬件）", recommend_matrix)
    check("建模选型（内存门槛）", mesh_choice_matrix)
    check("后端可构造", backends_construct)
    check("量化阶梯", quant_ladder)
    check("更新器合并", updater_dryrun)

    if not args.fast:
        print("\n[测试]")
        check("pytest", run_pytest)
        check("verify.py", run_verify)

    ok = sum(1 for r in RESULTS if r["ok"])
    print("\n" + "=" * 78)
    print(f"{ok}/{len(RESULTS)} 项通过")
    out = ROOT / "outputs" / "healthcheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {out}")
    if ok < len(RESULTS):
        print("\n失败项:")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  · {r['check']}: {r['detail']}")
    print("=" * 78)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
