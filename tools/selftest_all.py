# -*- coding: utf-8 -*-
"""全板块自检：把铸想的每一块都真跑一遍，输出一张结果表。

    python tools/selftest_all.py --home D:/forgeform --quality 快速

覆盖：
  1  环境探测（框架 / 端口 / GPU）
  2  已有模型（ComfyUI 权重 / 本地 diffusers / hy3dgen）
  3  模型推荐（按硬件出方案）
  4  对话（agent 解析需求）
  5  出图（真生成一张）
  6  建模（真生成一个 GLB）
  7  STL（水密 + 毫米尺寸）
  8  历史（项目保存 → 重新加载，字段不丢）
  9  API（运维接口自定义路由已注册）
 10  下载（fetch_model 能列文件、选镜像）
 11  打印（Bambu 打印机探测，没有就记 skip）

设计：每项独立 try/except，一项挂了不影响后面的；最后统一打表。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "做一个六角法兰联轴器，中心有轴孔和六个螺栓孔，金属质感，单件"

RESULTS: list[dict] = []


def rec(key: str, name: str, ok: bool | None, sec: float = 0.0, detail: str = "") -> None:
    RESULTS.append({"key": key, "name": name, "ok": ok, "sec": round(sec, 1), "detail": detail})
    mark = {True: "✓", False: "✗", None: "-"}[ok]
    print(f"  {mark} {name:<22} {sec:>6.1f}s  {detail}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(ROOT))
    ap.add_argument("--quality", default="快速", choices=["快速", "标准", "高质量"])
    ap.add_argument("--size", type=int, default=80)
    ap.add_argument("--skip-gpu", action="store_true", help="跳过出图/建模（只验证静态板块）")
    args = ap.parse_args()

    home = Path(args.home).resolve()
    sys.path.insert(0, str(home))
    os.chdir(home)
    t_all = time.time()

    cfg_path = home / "config.json"
    if not cfg_path.exists():
        print(f"✗ {cfg_path} 不存在 —— 先跑一次 app.py 或 tools/setup.py")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    print("=" * 74)
    print(f"铸想全板块自检   仓库 {home}")
    print("=" * 74)

    # ── 1 环境探测 ────────────────────────────────────────
    print("\n[1] 环境探测")
    t0 = time.time()
    try:
        from backend.backends import detect as D
        fws = D.probe_all(comfy_dir=os.environ.get("COMFYUI_DIR") or None)
        alive = [f.key for f in fws if f.alive]
        rec("detect", "框架探测", bool(alive), time.time() - t0,
            f"{len(alive)} 个可用: {', '.join(alive)}")
    except Exception as exc:
        rec("detect", "框架探测", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 2 已有模型 ────────────────────────────────────────
    print("\n[2] 已有模型")
    t0 = time.time()
    try:
        comfy = next((f for f in fws if f.key.startswith("comfyui") and f.alive), None)
        img_n = len(comfy.models) if comfy else 0
        diff = next((f for f in fws if f.key == "diffusers"), None)
        hy = next((f for f in fws if f.key == "hy3dgen"), None)
        detail = f"ComfyUI 权重 {img_n} · diffusers {len(diff.models) if diff else 0}" \
                 f" · hy3dgen {'有' if (hy and hy.alive) else '无'}"
        rec("models", "已有模型识别", bool(img_n or (diff and diff.alive) or (hy and hy.alive)),
            time.time() - t0, detail)
    except Exception as exc:
        rec("models", "已有模型识别", False, time.time() - t0, str(exc)[:90])

    # ── 3 模型推荐 ────────────────────────────────────────
    print("\n[3] 模型推荐")
    t0 = time.time()
    try:
        from backend.backends.recommend import (build_profiles, pick_default,
                                                probe_hardware, summarize_assets)
        hw = probe_hardware(str(home))
        assets = summarize_assets(fws)
        profiles = build_profiles(hw, assets)
        default = pick_default(profiles)
        usable = [p for p in profiles if p.fits]
        rec("recommend", "按硬件出方案", bool(profiles), time.time() - t0,
            f"{len(profiles)} 个方案 / {len(usable)} 个可用 / 默认 {default.key if default else '无'}")
    except Exception as exc:
        rec("recommend", "按硬件出方案", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 4 对话 ────────────────────────────────────────────
    print("\n[4] 对话")
    t0 = time.time()
    try:
        from backend import agent
        if not agent.is_ready():
            agent.warm_up()
        state = {"phase": "describing", "requirement": "", "auto_params": {},
                 "conversation": [], "candidates": [], "selected": "",
                 "glb_path": "", "stl_path": ""}
        plan = agent.plan(PROMPT, state, timeout=120)
        reply = ((plan or {}).get("reply") or "")[:60].replace("\n", " ")
        acts = [a.get("name") for a in (plan or {}).get("actions", [])]
        rec("chat", "对话解析", bool(plan), time.time() - t0,
            f"动作={acts} 「{reply}…」")
    except Exception as exc:
        rec("chat", "对话解析", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 5/6/7 出图 → 建模 → STL ───────────────────────────
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(cfg_path))
    img_path = glb_path = stl_path = None

    if not args.skip_gpu:
        print("\n[5] 出图")
        t0 = time.time()
        try:
            res = pipe.generate_images(PROMPT, count=1, model_key="qwen21_ref",
                                       quality_name="标准", resolution="1024×1024",
                                       skip_cache=True)
            if res and getattr(res[0], "success", False):
                img_path = Path(res[0].output_path)
                rec("image", "出图", True, time.time() - t0,
                    f"{img_path.name} {img_path.stat().st_size//1024} KB")
            else:
                err = getattr(res[0], "error", "无返回") if res else "无返回"
                rec("image", "出图", False, time.time() - t0, str(err)[:90])
        except Exception as exc:
            rec("image", "出图", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

        if img_path:
            print("\n[6] 建模")
            t0 = time.time()
            try:
                r = pipe.generate_3d(model_key="hunyuan21", quality_name=args.quality,
                                     image_path=str(img_path))
                if getattr(r, "success", False):
                    glb_path = Path(r.output_path)
                    rec("mesh", "建模", True, time.time() - t0,
                        f"{glb_path.name} {glb_path.stat().st_size//1024} KB")
                else:
                    rec("mesh", "建模", False, time.time() - t0,
                        str(getattr(r, "error", "未知"))[:90])
            except Exception as exc:
                rec("mesh", "建模", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])
        else:
            rec("mesh", "建模", None, 0, "跳过（出图没成功）")

        print("\n[7] STL 导出")
        t0 = time.time()
        if glb_path:
            try:
                s = pipe.export_stl(str(glb_path), target_size_mm=args.size)
                if getattr(s, "success", False):
                    stl_path = Path(s.output_path)
                    import trimesh
                    m = trimesh.load(str(stl_path), force="mesh")
                    bb = [round(float(x), 1) for x in m.bounding_box.extents]
                    rec("stl", "STL 导出", bool(m.is_watertight), time.time() - t0,
                        f"水密={bool(m.is_watertight)} bbox={bb} {stl_path.stat().st_size//1024} KB")
                else:
                    rec("stl", "STL 导出", False, time.time() - t0,
                        str(getattr(s, "error", "未知"))[:90])
            except Exception as exc:
                rec("stl", "STL 导出", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])
        else:
            rec("stl", "STL 导出", None, 0, "跳过（建模没成功）")
    else:
        for k, n in (("image", "出图"), ("mesh", "建模"), ("stl", "STL 导出")):
            rec(k, n, None, 0, "跳过（--skip-gpu）")

    # ── 8 历史（项目保存 / 加载）─────────────────────────
    print("\n[8] 历史生成（项目存档）")
    t0 = time.time()
    try:
        from backend import project_manager as PM
        st = PM.create_project()          # 返回完整 state，含 folder
        st["phase"] = "done"
        st["user_prompt"] = PROMPT
        st["auto_params"] = {"size_mm": args.size}
        st["selected"] = str(img_path or "")
        st["glb_path"] = str(glb_path or "")
        st["stl_path"] = str(stl_path or "")
        PM.save_project(st)
        back = PM.load_project(st["folder"])
        # 注意：不要比较 stl_path/glb_path —— load_project 的这两个字段是**从项目目录里
        # 扫文件**得出的（_get_file），而 save_project 只写 meta，不落盘产物文件。
        # 断言它们相等会永远失败，那是测试假设错了，不是产品 bug。
        ok = (back.get("user_prompt") == PROMPT
              and back.get("phase") == "done"
              and back.get("auto_params", {}).get("size_mm") == args.size)
        rec("history", "历史 / 项目存档", ok, time.time() - t0,
            f"存→读 一致={'是' if ok else '否'} folder={st['folder']}")
    except Exception as exc:
        rec("history", "历史 / 项目存档", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 9 API（自定义路由注册）───────────────────────────
    print("\n[9] API 路由")
    t0 = time.time()
    try:
        src = (home / "app.py").read_text(encoding="utf-8", errors="ignore")
        routes = [r for r in ("/ops/rebuild", "/ops/jobs", "/model-library") if r in src]
        rec("api", "运维 / 模型库路由", len(routes) >= 2, time.time() - t0,
            f"已注册: {', '.join(routes) if routes else '无'}")
    except Exception as exc:
        rec("api", "运维 / 模型库路由", False, time.time() - t0, str(exc)[:90])

    # ── 10 下载器 ────────────────────────────────────────
    print("\n[10] 模型下载")
    t0 = time.time()
    try:
        import subprocess
        out = subprocess.run([sys.executable, str(home / "tools" / "fetch_model.py"),
                              "--repo", "tencent/Hunyuan3D-2mini",
                              "--dest", str(home / "_probe"),
                              "--include", "^config\\.json$", "--dry-run"],
                             capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace")
        txt = (out.stdout or "") + (out.stderr or "")
        ok = "待下载" in txt or "使用:" in txt
        mirror = next((l.strip() for l in txt.splitlines() if "使用:" in l), "")
        rec("fetch", "下载器", ok, time.time() - t0,
            mirror or txt[-90:].replace("\n", " "))
    except Exception as exc:
        rec("fetch", "下载器", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 11 打印（Bambu）──────────────────────────────────
    print("\n[11] 打印（Bambu）")
    t0 = time.time()
    try:
        from backend import bambu_print as BP
        printers = []
        for name in ("discover", "list_printers", "find_printers", "scan"):
            fn = getattr(BP, name, None)
            if callable(fn):
                printers = fn() or []
                break
        rec("print", "打印机探测", None, time.time() - t0,
            f"发现 {len(printers)} 台" if printers else "没发现（未配打印机属正常）")
    except Exception as exc:
        rec("print", "打印机探测", None, time.time() - t0, f"跳过: {type(exc).__name__}")

    # ── 汇总 ─────────────────────────────────────────────
    total = time.time() - t_all
    print("\n" + "=" * 74)
    print(f"{'板块':<24} {'结果':<6} {'耗时':>8}")
    print("-" * 74)
    for r in RESULTS:
        mark = {True: "✓ 通过", False: "✗ 失败", None: "- 跳过"}[r["ok"]]
        print(f"{r['name']:<24} {mark:<6} {r['sec']:>7.1f}s")
        if r["detail"]:
            print(f"    {r['detail']}")
    print("-" * 74)
    npass = sum(1 for r in RESULTS if r["ok"] is True)
    nfail = sum(1 for r in RESULTS if r["ok"] is False)
    nskip = sum(1 for r in RESULTS if r["ok"] is None)
    print(f"通过 {npass} · 失败 {nfail} · 跳过 {nskip} · 总耗时 {total/60:.1f} 分钟")
    print("=" * 74)

    out = home / "outputs" / "selftest_all.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已存: {out}")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
