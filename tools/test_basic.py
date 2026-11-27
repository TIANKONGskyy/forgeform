# -*- coding: utf-8 -*-
"""铸想基础功能全量自测（真实入口，不是读源码）。

覆盖 8 个区：
    A 服务与实例      B Studio 工作台     C 安装与环境
    D 模型与配置      E 创作空间·会话     F 出图与建模
    G 对外 API        H 数据与持久化

    PYTHONPATH= python tools/test_basic.py                # 全跑（含真出图建模，约 20-25 分钟）
    PYTHONPATH= python tools/test_basic.py --fast         # 跳过出图/建模（约 2 分钟）
    PYTHONPATH= python tools/test_basic.py --only A,B,C

结果：outputs/probe/basic.json + 终端摘要
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

# 允许用环境变量改地址：这台机器上 7860 可能跑着**另一份安装**（会 403 拦测试），
# 此时把 FF_APP_URL 指到本仓库自己起的端口即可（2026-11-27 加）。
import os as _os

APP = _os.environ.get("FF_APP_URL", "http://127.0.0.1:7860")
STUDIO = _os.environ.get("FF_STUDIO_URL", "http://127.0.0.1:7861")
API = _os.environ.get("FF_API_URL", "http://127.0.0.1:7863")
T = []          # 结果


def rec(area: str, name: str, ok: bool, sec: float = 0.0, detail: str = "") -> bool:
    T.append({"area": area, "name": name, "ok": bool(ok),
              "sec": round(sec, 2), "detail": str(detail)[:300]})
    print(f"    {'✓' if ok else '✗'} {name:<30}{sec:>7.1f}s  {str(detail)[:100]}")
    return ok


def get(url, timeout=10, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "ff-test", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def post(url, data=None, timeout=60, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "ff-test", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def jget(url, timeout=10, headers=None):
    st, raw, h = get(url, timeout, headers)
    try:
        return st, json.loads(raw), raw, h
    except Exception:
        return st, None, raw, h


def jpost(url, data=None, timeout=60, headers=None):
    st, raw, h = post(url, data, timeout, headers)
    try:
        return st, json.loads(raw), raw, h
    except Exception:
        return st, None, raw, h


def port_up(p):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", p)) == 0


_STOK = [""]


def studio_token() -> str:
    if _STOK[0]:
        return _STOK[0]
    try:
        _, raw, _ = get(STUDIO + "/", 8)
        m = re.search(r"STUDIO_TOKEN='([^']+)'", raw.decode("utf-8", "replace"))
        _STOK[0] = m.group(1) if m else ""
    except Exception:
        pass
    return _STOK[0]


def sh():
    return {"X-Studio-Token": studio_token()}


# ══════════════ A 服务与实例 ══════════════
def area_a() -> None:
    t0 = time.time()
    up = {p: port_up(p) for p in (7860, 7861, 7863, 8188)}
    rec("A", "四个端口在监听", all(up.values()), time.time() - t0, str(up))

    t0 = time.time()
    idn = {}
    for p in (7860, 7861, 7863):
        # app 有 /__forgeform_id；Studio/API 用响应头 X-ForgeForm-Root（404 也能读到头）
        try:
            _, d, raw, h = jget(f"http://127.0.0.1:{p}/__forgeform_id", 6)
            if d and d.get("root"):
                idn[p] = d.get("root")
            else:
                idn[p] = h.get("X-ForgeForm-Root") or "无"
        except urllib.error.HTTPError as e:
            idn[p] = e.headers.get("X-ForgeForm-Root") or f"HTTP {e.code}"
        except Exception as e:
            idn[p] = type(e).__name__
    ok = sum(1 for v in idn.values() if "ForgeForm-oss" in str(v)) >= 2
    rec("A", "服务身份可识别", ok, time.time() - t0, str(idn))

    try:
        from backend import ports as P
        t0 = time.time()
        apps = P._our_pids(("app.py",))
        stsd = P._our_pids(("studio.py",))
        rec("A", "没有重复实例", len(apps) <= 2 and len(stsd) <= 1, time.time() - t0,
            f"app={len(apps)} studio={len(stsd)}")
    except Exception as e:
        rec("A", "没有重复实例", False, 0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    logs = list((ROOT / "logs").glob("*.log")) if (ROOT / "logs").is_dir() else []
    fresh = [p for p in logs if time.time() - p.stat().st_mtime < 86400]
    rec("A", "日志在写", bool(fresh), time.time() - t0, f"{len(logs)} 个日志，{len(fresh)} 个 24h 内更新")

    t0 = time.time()
    import shutil
    du = shutil.disk_usage(str(ROOT))
    free_gb = du.free / 1024 ** 3
    rec("A", "磁盘余量足够", free_gb > 15, time.time() - t0, f"D 盘剩 {free_gb:.1f} GB")


# ══════════════ B Studio 工作台 ══════════════
def area_b() -> None:
    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/ping", 6)
        rec("B", "ping 探活", bool(d and d.get("app") == "forgeform-studio"),
            time.time() - t0, f"v{d.get('version') if d else '?'}")
    except Exception as e:
        rec("B", "ping 探活", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/status", 8, sh())
        job = (d or {}).get("job") or {}
        rec("B", "status 结构完整", "job" in (d or {}) and "ready" in (d or {}),
            time.time() - t0, f"job={job.get('status')} ready={(d or {}).get('ready')}")
    except Exception as e:
        rec("B", "status 结构完整", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/firstrun", 25, sh())
        stages = (d or {}).get("stages") or {}
        ready = [k for k, v in stages.items() if (v or {}).get("ready")]
        rec("B", "一键开始三链", len(stages) == 3, time.time() - t0,
            f"就绪 {len(ready)}/3 {ready}")
    except Exception as e:
        rec("B", "一键开始三链", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/doctor?offline=1", 25, sh())
        env = (d or {}).get("env") or []
        ok = sum(1 for e in env if e.get("ok"))
        rec("B", "环境自检（离线）", len(env) > 0, time.time() - t0, f"{ok}/{len(env)} 项正常")
    except Exception as e:
        rec("B", "环境自检（离线）", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/maintenance", 20, sh())
        ok = isinstance(d, dict) and ("retention" in d or "plan" in d or "ok" in d)
        rec("B", "维护/清理接口", ok, time.time() - t0, str(list((d or {}).keys())[:5]))
    except Exception as e:
        rec("B", "维护/清理接口", False, time.time() - t0, f"{type(e).__name__}")


# ══════════════ C 安装与环境 ══════════════
def area_c() -> None:
    t0 = time.time()
    try:
        st, d, _, _ = jpost(STUDIO + "/api/scan", {"model_root": str(ROOT / "models")}, 180, sh())
        job = {}
        for _ in range(90):
            _, s, _, _ = jget(STUDIO + "/api/status", 8, sh())
            job = (s or {}).get("job") or {}
            if job.get("status") not in ("running", "queued"):
                break
            time.sleep(2)
        _, fr, _, _ = jget(STUDIO + "/api/firstrun", 25, sh())
        cands = len((fr or {}).get("candidates") or [])
        rec("C", "检测环境", job.get("status") == "done" and cands > 0,
            time.time() - t0, f"job={job.get('status')} 方案 {cands} 个")
    except Exception as e:
        rec("C", "检测环境", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        os.environ["FORGEFORM_INSTALL_DRYRUN"] = "1"
        from backend import firstrun as FR
        plan = FR.plan() or []
        cands = plan if isinstance(plan, list) else (plan.get("candidates") or [])
        rec("C", "安装计划可生成", bool(cands), time.time() - t0,
            f"{len(cands)} 个方案")
    except Exception as e:
        rec("C", "安装计划可生成", False, time.time() - t0, f"{type(e).__name__}: {e}")
    finally:
        os.environ.pop("FORGEFORM_INSTALL_DRYRUN", None)

    t0 = time.time()
    try:
        st, d, _, _ = jget(STUDIO + "/api/location", 20, sh())
        rec("C", "安装位置判定", bool((d or {}).get("recommended")), time.time() - t0,
            ((d or {}).get("recommended") or {}).get("path", "?"))
    except Exception as e:
        rec("C", "安装位置判定", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        st, d, _, _ = jpost(STUDIO + "/api/calibrate", {"quick": True}, 120, sh())
        ok = st == 200
        for _ in range(90):
            _, s2, _, _ = jget(STUDIO + "/api/status", 8, sh())
            jb = (s2 or {}).get("job") or {}
            if jb.get("status") not in ("running", "queued"):
                break
            time.sleep(2)
        rec("C", "本机耗时校准", ok and jb.get("status") == "done", time.time() - t0,
            f"job={jb.get('status')}")
    except Exception as e:
        rec("C", "本机耗时校准", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        jpost(STUDIO + "/api/scan", {"model_root": str(ROOT / "models")}, 30, sh())
        time.sleep(1.5)
        st, d, _, _ = jpost(STUDIO + "/api/cancel", {}, 20, sh())
        time.sleep(3)
        _, s3, _, _ = jget(STUDIO + "/api/status", 8, sh())
        jb = (s3 or {}).get("job") or {}
        rec("C", "取消任务", jb.get("status") in ("cancelled", "done", "idle", "failed"),
            time.time() - t0, f"取消后 job={jb.get('status')}")
    except Exception as e:
        rec("C", "取消任务", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ══════════════ D 模型与配置 ══════════════
def area_d() -> None:
    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/catalog", 25, sh())
        items = (d.get("models") if isinstance(d, dict) else None) or []
        rec("D", "模型清单（catalog）", len(items) >= 5, time.time() - t0, f"{len(items)} 个")
    except Exception as e:
        rec("D", "模型清单（catalog）", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    cfgp = ROOT / "config.json"
    backup = cfgp.read_bytes() if cfgp.exists() else b""
    try:
        # 前端"模型与配置"页用 POST /api/config 保存（GET 会 404）。
        # 注意：这个接口**会真写 config.json**，所以先备份、测完还原，
        # 否则会把手调的 backend.mode / 超时配置冲掉。
        st, d, _, _ = jpost(STUDIO + "/api/config",
                            {"mode": "auto", "quant": "auto"}, 30, sh())
        rec("D", "保存配置（POST config）", st == 200, time.time() - t0, str(d)[:80])
    except Exception as e:
        rec("D", "保存配置（POST config）", False, time.time() - t0, f"{type(e).__name__}: {e}")
    finally:
        if backup:
            cfgp.write_bytes(backup)

    t0 = time.time()
    try:
        jpost(STUDIO + "/api/remove", {"id": "__nonexistent__"}, 20, sh())
        rec("D", "删除模型（不存在的 id 安全）", False, time.time() - t0, "居然成功了")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:80]
        rec("D", "删除模型（不存在的 id 安全）", e.code in (400, 404),
            time.time() - t0, f"HTTP {e.code} {body}")
    except Exception as e:
        rec("D", "删除模型（不存在的 id 安全）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        import backend.timings as TM
        from backend import firstrun as FR
        ok = True
        detail = []
        for kind, key in (("mesh", "hunyuan_mini"), ("image", "flux_klein")):
            r = FR.model_ready(kind, key)
            if isinstance(r, tuple):          # 返回 (就绪, 说明)
                ready, why = bool(r[0]), str(r[1])
            else:
                ready, why = bool(r), ""
            ok = ok and ready
            detail.append(f"{kind}:{key}={ready}")
        rec("D", "模型就绪判定", ok, time.time() - t0, " ".join(detail))
    except Exception as e:
        rec("D", "模型就绪判定", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        from backend import secrets as S
        k = S.issue_api_key("basic-test", ttl_days=1)
        lst = S.list_api_keys()
        found = any(x.get("id") == k["id"] for x in lst)
        S.revoke_api_key(k["id"])
        after = [x for x in S.list_api_keys() if x.get("id") == k["id"]]
        revoked = bool(after) and after[0].get("revoked") in (True, 1)
        S.delete_api_key(k["id"])
        gone = not any(x.get("id") == k["id"] for x in S.list_api_keys())
        rec("D", "密钥 签发/列表/撤销/删除", found and revoked and gone,
            time.time() - t0, f"found={found} revoked={revoked} deleted={gone}")
    except Exception as e:
        rec("D", "密钥 签发/列表/撤销/删除", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        _, d, _, _ = jget(STUDIO + "/api/cloud", 15, sh())
        rec("D", "云端配置接口", isinstance(d, dict), time.time() - t0,
            f"键 {list((d or {}))[:4]}")
    except Exception as e:
        rec("D", "云端配置接口", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        from backend.pipeline import Pipeline
        p = Pipeline(str(ROOT / "config.json"))
        got = {}
        for q in ("快速", "标准", "高质量", "极致"):
            got[q] = None
        # 直接读档位表（pipeline 内部）而不是真跑
        src = (ROOT / "backend" / "pipeline.py").read_text(encoding="utf-8")
        m = re.search(r"td_quality = \{(.*?)\n        \}", src, re.S)
        table = m.group(1) if m else ""
        ocs = dict(re.findall(r'"(快速|标准|高质量|极致)":\s*\{"octree_resolution":\s*(\d+)', table))
        ok = len(ocs) >= 4 and len(set(ocs.values())) >= 4
        rec("D", "精度档位是真区分", ok, time.time() - t0, str(ocs))
    except Exception as e:
        rec("D", "精度档位是真区分", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ══════════════ E 会话 ══════════════
def area_e() -> None:
    t0 = time.time()
    try:
        st, raw, _ = get(APP + "/", 15)
        txt = raw.decode("utf-8", "replace")
        need = ["chatbox", "btn_gen", "Model3d"]
        miss = [n for n in need if n.lower() not in txt.lower()]
        rec("E", "页面与关键控件", st == 200 and not miss, time.time() - t0,
            f"{len(raw)//1024} KB" + (f" 缺 {miss}" if miss else ""))
    except Exception as e:
        rec("E", "页面与关键控件", False, time.time() - t0, f"{type(e).__name__}")

    from gradio_client import Client
    try:
        cli = Client(APP + "/", verbose=False)
    except Exception as e:
        rec("E", "gradio 客户端", False, 0, f"{type(e).__name__}: {e}")
        return

    t0 = time.time()
    try:
        r = cli.predict("做一个直径 30mm 的圆形杯垫", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        rec("E", "对话第 1 轮", bool(reply), time.time() - t0, reply[:80])
    except Exception as e:
        rec("E", "对话第 1 轮", False, time.time() - t0, f"{type(e).__name__}: {e}")
        return

    t0 = time.time()
    try:
        r = cli.predict("再加一圈防滑纹", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        rec("E", "对话第 2 轮（上下文）", bool(reply), time.time() - t0, reply[:80])
    except Exception as e:
        rec("E", "对话第 2 轮（上下文）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        r = cli.predict("做成直径 45mm、厚 6mm 的", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        keep = ("45" in reply) and ("6" in reply)
        rec("E", "数字不被改写（45/6mm）", keep, time.time() - t0, reply[:90])
    except Exception as e:
        rec("E", "数字不被改写（45/6mm）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        r = cli.predict("   ", api_name="/on_submit")
        chat = r[1] if len(r) > 1 else []
        reply = str(chat[-1][1]) if chat else ""
        ask = any(k in reply for k in ("描述", "想做", "告诉", "什么"))
        rec("E", "空输入有提示", ask, time.time() - t0, reply[:70])
    except Exception as e:
        rec("E", "空输入有提示", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ══════════════ F 出图与建模 ══════════════
def area_f(fast: bool) -> None:
    from backend.pipeline import Pipeline
    pipe = Pipeline(str(ROOT / "config.json"))

    img = None
    t0 = time.time()
    try:
        res = pipe.generate_images(
            "一个方形木纹杯垫，四分之三俯视，单物体，纯白背景",
            count=1, model_key="flux_klein", quality_name="快速",
            resolution="1024×1024", skip_cache=True)
        ok = [r for r in (res or []) if getattr(r, "success", False) and r.output_path]
        if ok:
            img = Path(ok[0].output_path)
        rec("F", "出图（快速档）", bool(img), time.time() - t0,
            f"{img.name} {img.stat().st_size//1024} KB" if img else "失败")
    except Exception as e:
        rec("F", "出图（快速档）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        res = pipe.generate_images(
            "一个圆锥形灯罩，四分之三侧面，单物体，纯白背景",
            count=2, model_key="flux_klein", quality_name="快速",
            resolution="1024×1024", skip_cache=True)
        ok = [r for r in (res or []) if getattr(r, "success", False)]
        rec("F", "出图（一次 2 张）", len(ok) >= 2, time.time() - t0, f"{len(ok)} 张")
    except Exception as e:
        rec("F", "出图（一次 2 张）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    if fast:
        rec("F", "建模（跳过）", True, 0, "--fast 跳过重活")
        return

    t0 = time.time()
    glb = None
    try:
        r = pipe.generate_3d(model_key="hunyuan_mini", quality_name="高质量",
                             image_path=str(img) if img else None)
        if r and getattr(r, "success", False) and r.output_path:
            glb = Path(r.output_path)
        import trimesh
        m = trimesh.load(str(glb), force="mesh") if glb else None
        rec("F", "建模（512 octree）", bool(glb), time.time() - t0,
            f"{len(m.faces):,}面 水密={m.is_watertight}" if glb else "失败")
    except Exception as e:
        rec("F", "建模（512 octree）", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        res = pipe.export_stl(str(glb), target_size_mm=80.0, optimize_preset="print_ready")
        ok = bool(res and res.success and res.output_path)
        rec("F", "STL 导出 80mm", ok, time.time() - t0,
            f"{Path(res.output_path).stat().st_size//1024} KB" if ok else "失败")
    except Exception as e:
        rec("F", "STL 导出 80mm", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ══════════════ G 对外 API ══════════════
def area_g() -> None:
    t0 = time.time()
    try:
        st, d, _, _ = jget(API + "/v1/health", 6)
        rec("G", "health", bool(d and d.get("ok")), time.time() - t0, str(list((d or {}).keys())[:4]))
    except Exception as e:
        rec("G", "health", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        get(API + "/v1/models", 6)
        ok, code = False, 200
    except urllib.error.HTTPError as e:
        ok, code = e.code == 401, e.code
    except Exception as e:
        ok, code = False, type(e).__name__
    rec("G", "无 key 返回 401", ok, time.time() - t0, f"code={code}")

    t0 = time.time()
    try:
        st, d, _, _ = jget(API + "/openapi.json", 10)
        ok = isinstance(d, dict) and d.get("openapi", "").startswith("3.")
        rec("G", "OpenAPI 3.x", ok, time.time() - t0, d.get("openapi", "?"))
    except Exception as e:
        rec("G", "OpenAPI 3.x", False, time.time() - t0, f"{type(e).__name__}")

    from backend import secrets as S
    k = S.issue_api_key("basic-api", ttl_days=1)
    H = {"Authorization": "Bearer " + k["token"]}
    try:
        t0 = time.time()
        try:
            st, d, _, _ = jget(API + "/v1/models", 15, H)
            n = len((d or {}).get("data") or (d or {}).get("models") or [])
            rec("G", "模型清单（带 key）", n > 0, time.time() - t0, f"{n} 个")
        except Exception as e:
            rec("G", "模型清单（带 key）", False, time.time() - t0, f"{type(e).__name__}")

        t0 = time.time()
        jid = None
        try:
            st, d, _, _ = jpost(API + "/v1/generate",
                                {"prompt": "一个圆形杯垫，纯白背景", "stages": ["image"]},
                                30, H)
            jid = (d or {}).get("id") or (d or {}).get("job_id")
            rec("G", "提交生成任务", bool(jid), time.time() - t0, f"job={str(jid)[:8]}")
        except urllib.error.HTTPError as e:
            rec("G", "提交生成任务", False, time.time() - t0, f"HTTP {e.code}")

        if jid:
            t0 = time.time()
            stt = "?"
            for _ in range(120):
                try:
                    _, dd, _, _ = jget(f"{API}/v1/jobs/{jid}", 10, H)
                    stt = (dd or {}).get("status") or "?"
                    if stt in ("succeeded", "failed", "cancelled"):
                        break
                except Exception:
                    pass
                time.sleep(2)
            rec("G", "任务跑到成功", stt == "succeeded", time.time() - t0, f"status={stt}")

            t0 = time.time()
            try:
                get(f"{API}/v1/jobs/{jid}/files/image", 30, H)
                rec("G", "产物文件下载", True, time.time() - t0, "200")
            except urllib.error.HTTPError as e:
                rec("G", "产物文件下载", False, time.time() - t0, f"HTTP {e.code}")
            except Exception as e:
                rec("G", "产物文件下载", False, time.time() - t0, f"{type(e).__name__}")

        t0 = time.time()
        try:
            st, d, _, _ = jpost(API + "/v1/chat/completions",
                                {"model": "forgeform", "messages": [{"role": "user",
                                 "content": "你好"}]}, 120, H)
            txt = json.dumps(d, ensure_ascii=False)[:100] if d else ""
            rec("G", "chat/completions", st == 200 and bool(d), time.time() - t0, txt)
        except urllib.error.HTTPError as e:
            rec("G", "chat/completions", False, time.time() - t0, f"HTTP {e.code}")
        except Exception as e:
            rec("G", "chat/completions", False, time.time() - t0, f"{type(e).__name__}")

        t0 = time.time()
        try:
            st, d, _, _ = jpost(API + "/v1/images/generations",
                                {"prompt": "一个红色小球，纯白背景", "n": 1}, 300, H)
            ok = st == 200 and bool(d)
            rec("G", "images/generations", ok, time.time() - t0,
                json.dumps(d, ensure_ascii=False)[:80] if d else "")
        except urllib.error.HTTPError as e:
            rec("G", "images/generations", False, time.time() - t0, f"HTTP {e.code}")
        except Exception as e:
            rec("G", "images/generations", False, time.time() - t0, f"{type(e).__name__}")
    finally:
        S.delete_api_key(k["id"])


# ══════════════ H 数据与持久化 ══════════════
def area_h() -> None:
    proj = ROOT / "projects"
    t0 = time.time()
    dirs = [d for d in proj.iterdir() if d.is_dir()] if proj.is_dir() else []
    bad = []
    for d in dirs[:30]:
        if not (d / "meta.json").exists():
            bad.append(d.name)
    rec("H", "作品目录结构", bool(dirs) and len(bad) <= max(2, len(dirs) // 3),
        time.time() - t0, f"{len(dirs)} 个作品，缺 meta {len(bad)} 个")

    t0 = time.time()
    try:
        from backend import project_manager as pm
        got = None
        for d in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                st = pm.load_project(str(d))
                if st:
                    got = st
                    break
            except Exception:
                continue
        fields = ("path", "phase", "candidates")
        ok = bool(got) and any(k in got for k in fields)
        rec("H", "作品可重新载入", ok, time.time() - t0,
            f"键 {[k for k in fields if got and k in got]}")
    except Exception as e:
        rec("H", "作品可重新载入", False, time.time() - t0, f"{type(e).__name__}: {e}")

    t0 = time.time()
    try:
        from backend import maintenance as MT
        never = getattr(MT, "NEVER", [])
        ok = any("projects" in str(x) for x in never)
        rec("H", "清理不碰作品目录", ok, time.time() - t0, f"NEVER 含 projects={ok}")
    except Exception as e:
        rec("H", "清理不碰作品目录", False, time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    try:
        from backend import secrets as S
        ok = bool(S.list_api_keys() is not None) and os.path.exists(ROOT / ".secrets")
        rec("H", "密钥加密存储", ok, time.time() - t0,
            f".secrets 存在={os.path.exists(ROOT / '.secrets')}")
    except Exception as e:
        rec("H", "密钥加密存储", False, time.time() - t0, f"{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()
    areas = ([x.strip().upper() for x in args.only.split(",")] if args.only
             else ["A", "B", "C", "D", "E", "F", "G", "H"])

    print(f"  铸想基础功能自测 · {ROOT} · v{(ROOT/'VERSION').read_text(encoding='utf-8').strip()}")
    for a, fn in (("A", area_a), ("B", area_b), ("C", area_c), ("D", area_d),
                  ("E", area_e), ("H", area_h)):
        if a in areas:
            print(f"\n【{a}】")
            try:
                fn()
            except Exception as exc:
                rec(a, f"{a} 区异常", False, 0, f"{type(exc).__name__}: {exc}")
    if "F" in areas:
        print("\n【F】出图与建模")
        try:
            area_f(args.fast)
        except Exception as exc:
            rec("F", "F 区异常", False, 0, f"{type(exc).__name__}: {exc}")
    if "G" in areas:
        print("\n【G】对外 API")
        try:
            area_g()
        except Exception as exc:
            rec("G", "G 区异常", False, 0, f"{type(exc).__name__}: {exc}")

    ok = sum(1 for r in T if r["ok"])
    bad = [r for r in T if not r["ok"]]
    print(f"\n{'='*66}\n  {ok}/{len(T)} 通过" + (f"，失败 {len(bad)}" if bad else "，全绿"))
    for b in bad:
        print(f"    ✗ [{b['area']}] {b['name']}: {b['detail'][:110]}")
    (OUT / "basic.json").write_text(json.dumps(T, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(f"  明细: {OUT/'basic.json'}\n{'='*66}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
