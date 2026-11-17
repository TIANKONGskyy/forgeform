# -*- coding: utf-8 -*-
"""铸想功能实测：一步一步跑真实入口，把每步的耗时与异常记下来。

不是单元测试，也不是冒烟 —— 它的目的是**把问题找出来并量化**：
    出图多久、建模多久、STL 能不能下、重启还在不在、API 走不走得通、
    精度档切换有没有真的改 octree、错误输入怎么反应。

    PYTHONPATH= python tools/probe_flows.py --all
    PYTHONPATH= python tools/probe_flows.py --only image,mesh

结果：outputs/probe/flows.json（含每步耗时与错误原文）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 让 `from backend import ...` 可用
OUT = ROOT / "outputs" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

APP = "http://127.0.0.1:7860"
STUDIO = "http://127.0.0.1:7861"
API = "http://127.0.0.1:7863"

R: list[dict] = []


def rec(step: str, ok: bool, sec: float = 0.0, detail: str = "", **extra) -> None:
    R.append({"step": step, "ok": bool(ok), "sec": round(sec, 1),
              "detail": str(detail)[:400], **extra})
    print(f"  {'✓' if ok else '✗'} {step:<26}{sec:>7.1f}s  {detail[:110]}")


def _get(url: str, timeout: float = 10.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "forgeform-probe", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _post(url: str, data: dict, timeout: float = 60.0, headers: dict | None = None):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def studio_token() -> str:
    import re
    _, raw = _get(STUDIO + "/")
    m = re.search(r"STUDIO_TOKEN='([^']+)'", raw.decode("utf-8", "replace"))
    return m.group(1) if m else ""


# ── 1. Studio 流程 ───────────────────────────────────────────
def flow_studio() -> None:
    t0 = time.time()
    try:
        st, raw = _get(STUDIO + "/api/ping", 6)
        rec("studio.ping", st == 200, time.time() - t0, raw[:60].decode("utf-8", "replace"))
    except Exception as e:
        rec("studio.ping", False, time.time() - t0, f"{type(e).__name__}: {e}")

    tok = studio_token()
    h = {"X-Studio-Token": tok}
    t0 = time.time()
    try:
        st, raw = _post(STUDIO + "/api/scan", {"model_root": str(ROOT / "models")}, 120, h)
        ok = st == 200
    except Exception as e:
        ok, raw = False, f"{type(e).__name__}: {e}".encode()
    scan_sec = time.time() - t0
    # 等 job 收尾
    job = {}
    for _ in range(60):
        try:
            _, sraw = _get(STUDIO + "/api/status", 8, h)
            job = json.loads(sraw)
            if job.get("job", {}).get("status") not in ("running", "queued"):
                break
        except Exception:
            pass
        time.sleep(2)
    plans = 0
    try:
        _, fraw = _get(STUDIO + "/api/firstrun", 20, h)
        fr = json.loads(fraw)
        plans = len(fr.get("candidates") or [])
    except Exception:
        pass
    rec("studio.scan", job.get("job", {}).get("status") == "done", scan_sec,
        f"job={job.get('job',{}).get('status')} 方案 {plans} 个")

    # 打开创作空间
    t0 = time.time()
    ok = False
    detail = ""
    try:
        st, raw = _post(STUDIO + "/api/launch", {}, 180, h)
        ok = st == 200
        for _ in range(60):
            try:
                if _get(APP + "/", 4)[0] == 200:
                    break
            except Exception:
                time.sleep(2)
        detail = "创作空间已可访问"
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
    rec("studio.launch", ok and _port_ok(7860), time.time() - t0, detail)


def _port_ok(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── 2. 对话 ──────────────────────────────────────────────────
def flow_chat() -> None:
    t0 = time.time()
    try:
        from gradio_client import Client
        cli = Client(APP + "/", verbose=False)
        r = cli.predict("做一个手机支架", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        ok = bool(reply) and "DEVICEDEVICE" not in reply
        rec("chat.round1", ok, time.time() - t0, reply[:90])
    except Exception as e:
        rec("chat.round1", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        r = cli.predict("第二个问题：能加个底座吗", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        rec("chat.round2", bool(reply), time.time() - t0, reply[:90])
    except Exception as e:
        rec("chat.round2", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ── 3. 出图 ──────────────────────────────────────────────────
def flow_image(quality: str = "高质量") -> Path | None:
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))
    t0 = time.time()
    try:
        res = pipe.generate_images(
            "一个简单的圆柱形笔筒，四分之三侧面，单物体，纯白背景",
            count=1, model_key="flux_klein", quality_name=quality,
            resolution="1024×1024", skip_cache=True)
        ok = [r for r in (res or []) if getattr(r, "success", False) and r.output_path]
        if not ok:
            rec(f"image.{quality}", False, time.time() - t0, "没有成功产物")
            return None
        p = Path(ok[0].output_path)
        from PIL import Image
        with Image.open(p) as im:
            size = im.size
        rec(f"image.{quality}", True, time.time() - t0, f"{p.name} {size[0]}×{size[1]} "
            f"{p.stat().st_size//1024} KB")
        return p
    except Exception as e:
        rec(f"image.{quality}", False, time.time() - t0, f"{type(e).__name__}: {e}")
        return None


# ── 4. 建模 + STL ────────────────────────────────────────────
def flow_mesh(img: Path | None, quality: str = "高质量") -> None:
    if not img:
        rec(f"mesh.{quality}", False, 0, "跳过（没有图）")
        return
    from backend.pipeline import Pipeline
    import trimesh
    pipe = Pipeline(str(ROOT / "config.json"))
    t0 = time.time()
    try:
        r = pipe.generate_3d(model_key="hunyuan_mini", quality_name=quality,
                             image_path=str(img))
        dt = time.time() - t0
        if not (r and getattr(r, "success", False) and r.output_path):
            rec(f"mesh.{quality}", False, dt, f"失败: {getattr(r,'error','')[:120]}")
            return
        glb = Path(r.output_path)
        m = trimesh.load(str(glb), force="mesh")
        rec(f"mesh.{quality}", True, dt,
            f"{glb.name} {len(m.faces):,}面 水密={m.is_watertight} "
            f"{glb.stat().st_size//1024} KB")
    except Exception as e:
        rec(f"mesh.{quality}", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ── 5. STL 导出与下载 ───────────────────────────────────────
def flow_stl() -> None:
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))
    glbs = sorted((ROOT / "outputs" / "3d").glob("*.glb"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not glbs:
        rec("stl.export", False, 0, "没有 glb 可测")
        return
    t0 = time.time()
    try:
        res = pipe.export_stl(str(glbs[0]), target_size_mm=80.0, optimize_preset="print_ready")
        dt = time.time() - t0
        ok = bool(res and res.success and res.output_path)
        rec("stl.export", ok, dt, f"{Path(res.output_path).name if ok else ''} "
            f"{(Path(res.output_path).stat().st_size//1024 if ok else 0)} KB")
    except Exception as e:
        rec("stl.export", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ── 6. 作品持久化 ───────────────────────────────────────────
def flow_persist() -> None:
    proj = ROOT / "projects"
    dirs = [d for d in proj.iterdir() if d.is_dir()] if proj.is_dir() else []
    with_glb = [d for d in dirs if (d / "model.glb").exists()]
    rec("persist.projects", bool(dirs), 0,
        f"{len(dirs)} 个作品，{len(with_glb)} 个有 GLB")
    # 通过 app 的静态文件通道取产物
    cand = None
    for d in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
        for sub in ("candidates/cand_0.jpg", "selected.jpg", "model.stl"):
            if (d / sub).exists():
                cand = d / sub
                break
        if cand:
            break
    if not cand:
        rec("persist.download", False, 0, "没有可下载的产物")
        return
    t0 = time.time()
    try:
        st, raw = _get(APP + "/gradio_api/file=" + cand.as_posix(), 30)
        rec("persist.download", st == 200 and len(raw) > 500, time.time() - t0,
            f"{cand.name} {len(raw)//1024} KB")
    except Exception as e:
        rec("persist.download", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ── 7. 对外 API 端到端 ──────────────────────────────────────
def flow_api() -> None:
    from backend import secrets
    t0 = time.time()
    try:
        k = secrets.issue_api_key("probe", ttl_days=1)
        h = {"Authorization": "Bearer " + k["token"]}
        st, raw = _get(API + "/v1/models", 10, h)
        d = json.loads(raw)
        rec("api.models", st == 200, time.time() - t0,
            f"{len(d.get('data') or d.get('models') or [])} 个模型")
    except Exception as e:
        rec("api.models", False, time.time() - t0, f"{type(e).__name__}: {e}")
        return
    t0 = time.time()
    try:
        st, raw = _post(API + "/v1/generate",
                        {"prompt": "一个简单的方形杯垫，纯白背景", "stages": ["image"]},
                        30, h)
        job = json.loads(raw)
        jid = job.get("id") or job.get("job_id")
        rec("api.generate", st in (200, 202) and bool(jid), time.time() - t0,
            f"job={str(jid)[:8]}")
    except urllib.error.HTTPError as e:
        rec("api.generate", False, time.time() - t0, f"HTTP {e.code} {e.read()[:120]}")
        jid = None
    except Exception as e:
        rec("api.generate", False, time.time() - t0, f"{type(e).__name__}: {e}")
        jid = None
    if jid:
        t0 = time.time()
        status = "?"
        for _ in range(90):
            try:
                st, raw = _get(f"{API}/v1/jobs/{jid}", 10, h)
                d = json.loads(raw)
                status = d.get("status") or "?"
                if status in ("succeeded", "failed", "cancelled"):
                    break
            except Exception:
                pass
            time.sleep(2)
        rec("api.job", status == "succeeded", time.time() - t0, f"status={status}")
    secrets.delete_api_key(k["id"])


# ── 8. 错误输入 ─────────────────────────────────────────────
def flow_errors() -> None:
    from gradio_client import Client
    try:
        cli = Client(APP + "/", verbose=False)
    except Exception as e:
        rec("error.gradio", False, 0, f"{type(e).__name__}: {e}")
        return
    for name, text in (("empty", " "), ("long", "做一个" + "很复杂" * 400 + "的零件")):
        t0 = time.time()
        try:
            r = cli.predict(text, api_name="/on_submit")
            chat = r[1] if len(r) > 1 else []
            reply = str(chat[-1][1]) if chat else ""
            rec(f"error.{name}", bool(reply), time.time() - t0, reply[:80])
        except Exception as e:
            rec(f"error.{name}", False, time.time() - t0, f"{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    ap.add_argument("--quality", default="高质量")
    args = ap.parse_args()
    only = set(x.strip() for x in args.only.split(",")) if args.only != "all" else {
        "studio", "chat", "image", "mesh", "stl", "persist", "api", "errors"}

    print(f"  探测铸想 · {ROOT}")
    img = None
    if "studio" in only:
        print("\n【Studio】")
        flow_studio()
    if "chat" in only:
        print("\n【对话】")
        flow_chat()
    if "image" in only or "mesh" in only:
        print("\n【生成】")
        img = flow_image(args.quality)
    if "mesh" in only:
        flow_mesh(img, args.quality)
    if "stl" in only:
        print("\n【STL】")
        flow_stl()
    if "persist" in only:
        print("\n【持久化】")
        flow_persist()
    if "api" in only:
        print("\n【对外 API】")
        flow_api()
    if "errors" in only:
        print("\n【错误输入】")
        flow_errors()

    bad = [r for r in R if not r["ok"]]
    print(f"\n  完成 {len(R)} 项，失败 {len(bad)} 项")
    for b in bad:
        print(f"    ✗ {b['step']}: {b['detail'][:100]}")
    (OUT / "flows.json").write_text(json.dumps(R, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(f"  明细: {OUT / 'flows.json'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
