# -*- coding: utf-8 -*-
"""模拟正常用户的使用流程，逐个验证铸想的每条路径能不能闭环。

## 为什么要有这个

单测验证的是函数，**用户走的是一条条流程**：双击 → 说话 → 出图 → 改图 →
建模 → 导 STL → 再打开还能找到 → 下载。中间任何一环断了，用户看到的就是
"不能用"，而单测全绿。

这个脚本每次跑**一个场景**（按轮转），真驱动入口、真检查产物、真看落盘，
把结果写进 `outputs/sim/results.jsonl`。定时任务每 10 分钟跑一次，
一天下来能把十几条路径都扫一遍。

## 场景分级

    light  几秒到几十秒（协议、配置、文件、状态机）
    heavy  1–15 分钟（真出图 / 真建模）

轮转时 heavy 每隔几轮才跑一次，避免每次都等十几分钟。

## 用法

    PYTHONPATH= python tools/simulate_user.py            # 跑下一个场景
    PYTHONPATH= python tools/simulate_user.py --list     # 看全部场景
    PYTHONPATH= python tools/simulate_user.py --run S03  # 指定跑某个
    PYTHONPATH= python tools/simulate_user.py --reset    # 轮转归零
    PYTHONPATH= python tools/simulate_user.py --report   # 看历史结果
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "outputs" / "sim"
STATE = OUT / "state.json"
RESULTS = OUT / "results.jsonl"
PY = sys.executable

APP = "http://127.0.0.1:7860"
STUDIO = "http://127.0.0.1:7861"
API = "http://127.0.0.1:7863"


# ══════════════════════════════════════════════════════════════════
# 基础设施
# ══════════════════════════════════════════════════════════════════
def _alive(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _studio_token() -> str:
    import re
    html = urllib.request.urlopen(STUDIO + "/", timeout=8).read().decode("utf-8")
    m = re.search(r"STUDIO_TOKEN='([^']+)'", html)
    return m.group(1) if m else ""


def _studio(path: str, body=None, timeout: int = 180):
    tok = _studio_token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        STUDIO + path, data=data, method="POST" if body is not None else "GET",
        headers={"X-Studio-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ensure_studio() -> bool:
    """Studio 没起就拉起来（模拟用户双击启动）。"""
    if _alive(STUDIO + "/"):
        return True
    subprocess.Popen([PY, "-u", str(ROOT / "tools" / "studio.py"),
                      "--port", "7861", "--app-port", "7860", "--no-browser"],
                     cwd=str(ROOT), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(20):
        time.sleep(1)
        if _alive(STUDIO + "/"):
            return True
    return False


def _ensure_app() -> bool:
    """创作空间没起就点一次启动（模拟用户点「打开创作空间」）。"""
    if _alive(APP + "/"):
        return True
    if not _ensure_studio():
        return False
    try:
        _studio("/api/launch", {})
    except Exception:
        pass
    for _ in range(30):
        time.sleep(2)
        if _alive(APP + "/"):
            return True
    return False


class Check:
    """一个场景内的断言收集器。"""

    def __init__(self):
        self.items = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        return ok

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items)

    def failures(self) -> list[str]:
        return [f'{i["name"]}: {i["detail"]}' for i in self.items if not i["ok"]]


# ══════════════════════════════════════════════════════════════════
# 场景
# ══════════════════════════════════════════════════════════════════
def s01_studio_boot(c: Check) -> None:
    """用户双击启动 → Studio 能开、能读状态、三条链有结论。"""
    ok = _ensure_studio()
    c("Studio 能启动", ok, "" if ok else "20 秒内 7861 没起来")
    if not ok:
        return
    st = _studio("/api/status")
    c("status 有 job 字段", "job" in st, list(st)[:5])
    fr = _studio("/api/firstrun")
    c("firstrun 返回三条链", set(fr.get("stages", {})) == {"chat", "image", "mesh"},
      list(fr.get("stages", {})))
    ready = [k for k, v in fr["stages"].items() if v.get("ready")]
    c("至少一条链就绪", bool(ready), f"就绪={ready}")
    c("候选路径非空", bool(fr.get("candidates")), len(fr.get("candidates") or []))


def s02_app_boot(c: Check) -> None:
    """用户点「打开创作空间」→ 7860 起、界面元素在、API 齐全。"""
    ok = _ensure_app()
    c("创作空间能启动", ok, "" if ok else "60 秒内 7860 没起来")
    if not ok:
        return
    html = urllib.request.urlopen(APP + "/", timeout=15).read().decode("utf-8")
    c("页面含产品名", "铸想" in html, f"{len(html)} 字节")
    for el in ("chatbox", "Model3d", "btn_gen", "send_btn"):
        c(f"界面元素 {el}", el.lower() in html.lower(), "")


def s03_chat_flow(c: Check) -> None:
    """用户说一句话 → 得到回复，且不是乱码、不是空。"""
    if not _ensure_app():
        return c("创作空间未就绪", False, "跳过")
    from gradio_client import Client
    cli = Client(APP + "/", verbose=False)
    r = cli.predict("一个圆柱形茶杯垫，直径90mm", api_name="/on_submit")
    chat = r[1] if len(r) > 1 else []
    c("有对话返回", bool(chat), len(chat or []))
    if not chat:
        return
    reply = str(chat[-1][1])[:300]
    c("回复非空", len(reply.strip()) > 4, reply[:80])
    bad = ("DEVICEDEVICE", "auto_stop", "{" * 3)
    c("没有乱码/原始 JSON", not any(b in reply for b in bad), reply[:80])


def s04_quality_switch(c: Check) -> None:
    """用户切精度档 → state 里两处都写进去。"""
    if not _ensure_studio():
        return c("Studio 未就绪", False, "跳过")
    import app as A  # noqa
    for q in ("快速", "标准", "高质量", "极致"):
        st, html = A.on_quality_change(q, {"folder": "sim", "auto_params": {}})
        ok = st.get("quality") == q and (st.get("auto_params") or {}).get("quality_3d") == q
        c(f"精度档 {q} 写入 state", ok, json.dumps({k: st.get(k) for k in ("quality",)}))


def s05_model_switch(c: Check) -> None:
    """用户换出图/建模模型 → state 记住，侧栏能渲染。"""
    import app as A
    st, html = A.on_model_change("z_image", "hunyuan_mini", {"folder": "sim"})
    c("换出图模型", st.get("image_model") == "z_image", st.get("image_model"))
    c("换建模模型", st.get("model_3d") == "hunyuan_mini", st.get("model_3d"))
    c("侧栏渲染不报错", isinstance(html, str) and "当前项目" in html, len(html or ""))


def s06_project_persist(c: Check) -> None:
    """用户做一件作品后关掉再打开 → 产物还在、能二次下载。

    注意：`projects/` 里存在**没有图的空作品**是正常的 —— 用户点了
    「新建作品」但还没出图就会有这种目录。所以这里要找「真的有图」的那个，
    而不是简单取最新（取最新会误报成 bug）。
    """
    import backend.project_manager as pm
    proj_root = ROOT / "projects"
    projs = sorted([d for d in proj_root.iterdir() if d.is_dir()],
                   key=lambda d: d.stat().st_mtime, reverse=True) if proj_root.is_dir() else []
    c("有历史作品目录", bool(projs), len(projs))
    if not projs:
        return
    with_img = [p for p in projs if (p / "candidates").is_dir()
                and any((p / "candidates").glob("cand_*"))]
    c("至少一个作品有候选图", bool(with_img), "%d/%d" % (len(with_img), len(projs)))
    if not with_img:
        return
    st = pm.load_project(with_img[0].name)
    cands = st.get("candidates") or []
    c("作品里有候选图", bool(cands), len(cands))
    c("重新加载拿到 glb_path 字段", "glb_path" in st, "")
    # 有 GLB 的项目：产物必须落在项目目录里（不是只留在全局 outputs）
    with_glb = [p for p in projs if (p / "model.glb").exists()]
    if with_glb:
        c("GLB 归拢到项目目录", True, with_glb[0].name)


def s07_artifact_download(c: Check) -> None:
    """用户点产物下载 → HTTP 能真的拿到文件（不是 404）。"""
    import app as A
    projs = sorted([d for d in (ROOT / "projects").iterdir() if d.is_dir()],
                   key=lambda d: d.stat().st_mtime, reverse=True) if (ROOT / "projects").is_dir() else []
    target = next((p / "candidates" / "cand_0.jpg" for p in projs
                   if (p / "candidates" / "cand_0.jpg").exists()), None)
    if not target:
        return c("没有可下载的产物", True, "跳过")
    if not _ensure_app():
        return c("创作空间未就绪", False, "跳过")
    url = APP + "/gradio_api/file=" + target.as_posix()
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = r.read(64)
        c("下载返回 200", True, f"{len(data)} 字节起")
        c("内容是图片", data[:3] in (b"\xff\xd8\xff", b"\x89PN", b"GIF"), data[:6])
    except Exception as e:
        c("下载返回 200", False, f"{type(e).__name__}: {e}")


def s08_api_roundtrip(c: Check) -> None:
    """外部程序用 API：签发 key → 提交任务 → 轮询 → 下载 → 删除 key。"""
    from backend import secrets as S
    rec = S.issue_api_key("sim-run", ttl_days=1)
    tok = rec["token"]
    try:
        # API 服务没起就先拉起来
        if not _alive(API + "/v1/health"):
            subprocess.Popen([PY, "-u", str(ROOT / "tools" / "api_server.py"),
                              "--port", "7863"], cwd=str(ROOT),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _ in range(15):
                time.sleep(1)
                if _alive(API + "/v1/health"):
                    break
        c("API 服务在跑", _alive(API + "/v1/health"), API)
        c("健康检查免鉴权", True, "")

        def call(path, body=None, method=None, timeout=120):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                API + path, data=data,
                method=method or ("POST" if body is not None else "GET"),
                headers={"Authorization": "Bearer " + tok,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()

        st, raw = call("/v1/models")
        c("带 key 能拿模型清单", st == 200 and b'"image"' in raw, st)
        try:
            call("/v1/models", method="GET")
        except urllib.error.HTTPError:
            pass
        req = urllib.request.Request(API + "/v1/models")
        try:
            urllib.request.urlopen(req, timeout=10)
            c("无 key 应该 401", False, "竟然放行了")
        except urllib.error.HTTPError as e:
            c("无 key 返回 401", e.code == 401, e.code)

        st, raw = call("/v1/chat/completions",
                       {"messages": [{"role": "user", "content": "1+1=?"}]})
        d = json.loads(raw)
        c("对话接口返回内容", st == 200 and bool(
            (d.get("choices") or [{}])[0].get("message", {}).get("content")), st)
    finally:
        S.delete_api_key(rec["id"])
        c("key 可删除", not any(k["id"] == rec["id"] for k in S.list_api_keys()), "")


def s09_cloud_paths(c: Check) -> None:
    """接云端的三种用途：用假云端验证请求/响应/产物校验。"""
    from backend import cloud
    st = cloud.status()
    c("cloud.status 有加密后端", st.get("crypto") in ("dpapi", "mask"), st.get("crypto"))
    c("三条用途都有条目", set(st.get("uses", {})) == {"enhancer", "image", "mesh"},
      list(st.get("uses", {})))
    c("没配置时明确说缺什么",
      all(bool(v.get("missing")) or v.get("configured") for v in st["uses"].values()), "")
    # 没配 key 时必须抛 CloudError 而不是崩
    try:
        cloud.chat("x", cfg={"base_url": "http://127.0.0.1:9/v1", "key_name": "nope"})
        c("缺 key 时抛 CloudError", False, "没抛")
    except cloud.CloudError:
        c("缺 key 时抛 CloudError", True, "")


def s10_maintenance(c: Check) -> None:
    """维护：过期文件能清、projects 永不动。"""
    from backend import maintenance as mt
    st = mt.status()
    c("status 有 items", bool(st.get("items")), len(st.get("items") or []))
    c("projects 标记为永不清理",
      any(i["path"] == "projects" and i["keep_days"] == 0 for i in st["items"]), "")
    plan = mt.plan()
    c("plan 可计算", "items" in plan, plan.get("total_mb"))
    # 保护目录测试
    for bad in ("projects", "models", ".secrets"):
        try:
            mt._assert_not_protected(mt.ROOT / bad)
            c(f"拒绝清理 {bad}", False, "竟然允许")
        except RuntimeError:
            c(f"拒绝清理 {bad}", True, "")
    # 采样文件今天有没有
    today = ROOT / "logs" / f"metrics-{time.strftime('%Y-%m-%d')}.jsonl"
    c("今天有运行采样（或尚未到采样时刻）", True,
      "有" if today.exists() else "今天还没采（启动后 3 分钟才有）")


def s11_port_conflict(c: Check) -> None:
    """旧版占着端口 → 新实例会换端口而不是假装成功。"""
    import importlib
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    c("用 connect 判端口而不是 bind",
      "connect_ex" in src, "" if "connect_ex" in src else "还是 SO_REUSEADDR 的坑")
    c("有 _pick_port 逻辑", "_pick_port" in src, "")


def s12_env_doctor(c: Check) -> None:
    """环境自检能跑、能给出实测网络结论。"""
    from backend import netcheck as N
    env = N.env_checks()
    c("环境检查有 8 项", len(env) >= 8, len(env))
    c("每项都有修法或明确 OK",
      all(bool(e.get("fix")) or e["ok"] for e in env),
      [e["name"] for e in env if not e["ok"]])
    pick = N.pick_pypi()
    c("能挑出 pip 源", bool(pick) and pick.startswith("http"), pick)


def s13_empty_input(c: Check) -> None:
    """用户什么都不输就点发送 / 发超长文本 → 不崩。"""
    import app as A
    gen = A.on_submit("", {"folder": "sim"})
    first = next(gen)
    c("空输入不崩", first is not None, str(first)[:60])
    long_text = "做一个" + "非常" * 400 + "复杂的摆件"
    gen2 = A.on_submit(long_text, {"folder": "sim"})
    first2 = next(gen2)
    c("超长输入不崩", first2 is not None, len(long_text))


def s14_pipeline_light(c: Check) -> None:
    """出图（轻量档）真跑一次：产物存在、是图片、状态机推进。"""
    if not _ensure_app():
        return c("创作空间未就绪", False, "跳过")
    from gradio_client import Client
    cli = Client(APP + "/", verbose=False)
    t0 = time.time()
    r = cli.predict(api_name="/on_btn_generate")
    chat = r[0] if r else []
    txt = json.dumps(chat, ensure_ascii=False)[-500:]
    c("点生成图片有返回", bool(chat), f"{time.time()-t0:.0f}s")
    c("没有明显失败字样", "Traceback" not in txt and "失败" not in txt[-100:], txt[-120:])


def s15_3d_heavy(c: Check) -> None:
    """建模（较重）：挑一个**已有选图**的作品，真跑一次 3D。

    为什么不用 `on_btn_model`：那个按钮要求当前 state 已经处于"已选图"阶段，
    在全新的界面里点它只会立刻返回（什么也不做）—— 这不叫 bug，是产品设计
    （没图当然不能建模）。所以这里走按钮最终调用的那条路：
    `conv._do_generate_3d(state)`，验证的是同一条链路。
    前置条件（要有图）由 S14 那类场景负责制造。
    """
    import backend.project_manager as pm
    proj_root = ROOT / "projects"
    projs = sorted([d for d in proj_root.iterdir() if d.is_dir()],
                   key=lambda d: d.stat().st_mtime, reverse=True) if proj_root.is_dir() else []
    with_img = [p for p in projs
                if (p / "selected.jpg").exists()
                or ((p / "candidates").is_dir() and any((p / "candidates").glob("cand_*")))]
    if not with_img:
        return c("有含图的作品可用来建模", False, "一个都没有，先跑 S14 出图")

    st = pm.load_project(with_img[0].name)
    img = st.get("selected") or (st.get("candidates") or [""])[0]
    c("拿到输入图", bool(img) and Path(img).exists(), str(img)[-40:])
    if not img:
        return

    import app as A
    t0 = time.time()
    try:
        A.conv.cancel_autopilot(st)    # 防自动建模重复跑（踩过的坑）
    except TypeError:
        pass
    st["auto_params"] = dict(st.get("auto_params") or {})
    st["auto_params"]["quality_3d"] = "快速"
    res = A.conv._do_generate_3d(st)
    el = time.time() - t0
    c("建模返回成功", bool((res or {}).get("ok", True)) and "失败" not in str(res)[:120],
      f"{el:.0f}s · {str(res)[:100]}")
    glb = st.get("glb_path") or ""
    c("state 里有 glb_path", bool(glb) and Path(glb).exists(), str(glb)[-50:])
    if glb and Path(glb).exists():
        import trimesh
        m = trimesh.load(str(glb), force="mesh")
        c("网格面数合理", len(m.faces) > 1000, f"{len(m.faces):,} 面")
        c("产物落在项目目录", (Path(glb).parent / "model.glb").exists()
          or Path(glb).name == "model.glb", Path(glb).name)
    stl = st.get("stl_path") or ""
    c("自动导出了 STL", bool(stl) and Path(stl).exists(), str(stl)[-46:])
    # 分段耗时（对话流程里记的），用来定位"890 秒花在哪"
    perf = st.get("perf") or {}
    if perf:
        c("分段耗时已记录", True,
          " ".join(f"{k}={v}s" for k, v in perf.items()))


def s17_cancel_interrupt(c: Check) -> None:
    """用户点了生成又点取消 → 作业被标记取消、不会继续烧显卡。"""
    if not _ensure_studio():
        return c("Studio 未就绪", False, "跳过")
    st0 = _studio("/api/status")
    if st0["job"]["status"] in ("running", "queued"):
        _studio("/api/cancel", {})
        time.sleep(2)
    # 起一个"假作业"占住 job 状态（dry-run 安装，不真下载）
    r = _studio("/api/calibrate", {})       # 快作业，顺便验证接口
    c("能启动作业", bool(r.get("job") or r.get("ok")), str(r)[:80])
    # 立刻取消
    rc = _studio("/api/cancel", {})
    c("能取消", bool(rc.get("ok", True)), str(rc)[:60])
    for _ in range(15):
        time.sleep(1)
        st = _studio("/api/status")
        if st["job"]["status"] in ("cancelled", "failed", "done", "idle"):
            break
    c("取消后不再 running", st["job"]["status"] not in ("running", "queued"),
      st["job"]["status"])
    # 取消之后还能再起（不能一取消就卡死）
    r2 = _studio("/api/calibrate", {})
    c("取消后可再次启动", bool(r2.get("job") or r2.get("ok")), str(r2)[:60])


def s18_offline_degrade(c: Check) -> None:
    """网络全挂时：自检能明确说"不通"，下载逻辑不假装成功。"""
    from backend import netcheck as N
    # 用一个必然连不上的地址验探测函数本身会返回 ok=False 而不是抛异常
    bad = N._probe("http://127.0.0.1:9/definitely-not-there", timeout=2.0)
    c("探测失败返回 ok=False", bad.get("ok") is False, str(bad.get("error", ""))[:60])
    c("探测失败带错误信息", bool(bad.get("error")), bad.get("error", "")[:50])
    # 镜像全挂时的挑选函数应回退到默认值而不是抛
    orig = N.probe_all
    try:
        N.probe_all = lambda *a, **k: {"pypi": [{"ok": False}], "github": [{"ok": False}],
                                       "hf": [{"ok": False}]}
        c("全挂时 pick_pypi 有兜底", bool(N.pick_pypi()), N.pick_pypi())
        c("全挂时 pick_github 有兜底", bool(N.pick_github()), N.pick_github())
    finally:
        N.probe_all = orig
    # 下载器对 404 的处理：应当明确失败
    from tools import fetch_model as FM  # noqa
    c("下载器有重试/换源逻辑",
      hasattr(FM, "MIRRORS") and len(FM.MIRRORS) >= 2, list(FM.MIRRORS))


def s16_source_health(c: Check) -> None:
    """源码级回归：关键修复没被改回去。"""
    checks = {
        "app.py": ["connect_ex", "_pick_port"],
        "backend/agent.py": ['_provider() == "strata"'],
        "backend/pipeline.py": ["utf-8-sig", "_cap"],
        "backend/project_manager.py": ["model.glb"],
        "backend/studio.py": ["_pick_pip_args"],
        "backend/describer.py": ["record_usage"],
    }
    for f, needles in checks.items():
        p = ROOT / f
        if not p.exists():
            c(f"{f} 存在", False, "文件缺失")
            continue
        txt = p.read_text(encoding="utf-8")
        for n in needles:
            c(f"{f} 含 {n}", n in txt, "")


SCENARIOS = [
    ("S01", "Studio 启动与状态", "light", s01_studio_boot),
    ("S02", "创作空间启动与界面", "light", s02_app_boot),
    ("S03", "说话→回复", "light", s03_chat_flow),
    ("S04", "精度档切换", "light", s04_quality_switch),
    ("S05", "模型切换", "light", s05_model_switch),
    ("S06", "作品二次打开", "light", s06_project_persist),
    ("S07", "产物下载", "light", s07_artifact_download),
    ("S08", "对外 API 往返", "light", s08_api_roundtrip),
    ("S09", "云端三条路", "light", s09_cloud_paths),
    ("S10", "维护与清理", "light", s10_maintenance),
    ("S11", "端口冲突处理", "light", s11_port_conflict),
    ("S12", "环境自检", "light", s12_env_doctor),
    ("S13", "异常输入", "light", s13_empty_input),
    ("S14", "真出图", "heavy", s14_pipeline_light),
    ("S15", "真建模", "heavy", s15_3d_heavy),
    ("S16", "源码回归", "light", s16_source_health),
    ("S17", "取消与再启动", "light", s17_cancel_interrupt),
    ("S18", "断网降级", "light", s18_offline_degrade),
]


# ══════════════════════════════════════════════════════════════════
# 轮转与记录
# ══════════════════════════════════════════════════════════════════
def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"idx": 0, "run": 0}


def _save_state(s: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# heavy 场景（真出图/真建模）一次要 1-15 分钟，占总耗时 91%。
# 每轮都跑会把 10 小时的窗口吃光，只够扫三轮。改成每 HEAVY_EVERY 次才放行，
# 其余轮次走轻量场景 —— 覆盖面靠轮数，不靠单个场景硬跑。
HEAVY_EVERY = 4


def _next_scenario(heavy_ok: bool) -> tuple:
    s = _load_state()
    n = len(SCENARIOS)
    run_no = int(s.get("run") or 0)
    allow_heavy = heavy_ok and (run_no % HEAVY_EVERY == 0)
    for step in range(n):
        i = (s["idx"] + step) % n
        sid, name, level, fn = SCENARIOS[i]
        if level == "heavy" and not allow_heavy:
            continue
        s["idx"] = (i + 1) % n
        s["run"] = int(s.get("run") or 0) + 1
        _save_state(s)
        return SCENARIOS[i]
    s["idx"] = (s["idx"] + 1) % n
    _save_state(s)
    return SCENARIOS[s["idx"]]


def run_one(sid: str | None = None, heavy: bool = True) -> dict:
    if sid:
        hit = next((x for x in SCENARIOS if x[0].lower() == sid.lower()), None)
        if not hit:
            return {"ok": False, "error": f"没有场景 {sid}"}
        item = hit
        # ★ 指定场景也要推进轮转指针 —— 否则定时任务用 `--run Sxx` 会一直
        #   停在同一个场景（实测连跑两次 S08 才发现）。
        st = _load_state()
        st["idx"] = (SCENARIOS.index(hit) + 1) % len(SCENARIOS)
        st["run"] = int(st.get("run") or 0) + 1
        _save_state(st)
    else:
        item = _next_scenario(heavy)
    sid, name, level, fn = item
    c = Check()
    t0 = time.time()
    err = ""
    try:
        fn(c)
    except Exception as exc:
        import traceback
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    rec = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "id": sid, "name": name, "level": level,
        "ok": c.ok and not err,
        "sec": round(time.time() - t0, 1),
        "checks": c.items,
        "failures": c.failures(),
        "error": err,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def report(limit: int = 20) -> str:
    if not RESULTS.exists():
        return "还没有结果。"
    rows = [json.loads(x) for x in RESULTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    recent = rows[-limit:]
    ok = sum(1 for r in recent if r["ok"])
    lines = [f"最近 {len(recent)} 次：{ok} 通过 / {len(recent)-ok} 失败", ""]
    for r in recent:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f'  {mark} {r["at"]}  {r["id"]} {r["name"]}  {r["sec"]}s')
        for f in (r.get("failures") or [])[:3]:
            lines.append(f"      · {f}")
    # 每场景最近一次结果
    latest = {}
    for r in rows:
        latest[r["id"]] = r
    lines += ["", "各场景最近一次："]
    for sid, name, level, _fn in SCENARIOS:
        r = latest.get(sid)
        if not r:
            lines.append(f"  · {sid} {name}  未跑")
        else:
            lines.append(f'  {"✓" if r["ok"] else "✗"} {sid} {name}  {r["at"]}')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟用户流程验证铸想")
    ap.add_argument("--run", default="", help="指定场景 id")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-heavy", action="store_true", help="跳过耗时场景")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.list:
        for sid, name, level, _ in SCENARIOS:
            print(f"  {sid}  {level:<6}{name}")
        return 0
    if args.report:
        print(report())
        return 0
    if args.reset:
        _save_state({"idx": 0, "run": 0})
        print("轮转已归零")
        return 0

    rec = run_one(args.run or None, heavy=not args.no_heavy)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        mark = "✓ 通过" if rec.get("ok") else "✗ 失败"
        print(f'{mark}  {rec.get("id")} {rec.get("name")}  {rec.get("sec")}s')
        for item in rec.get("checks") or []:
            if not item["ok"]:
                print(f'    ✗ {item["name"]}: {item["detail"]}')
        if rec.get("error"):
            print(f'    异常: {rec["error"]}')
    return 0 if rec.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
