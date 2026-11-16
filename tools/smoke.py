# -*- coding: utf-8 -*-
"""冒烟测试：**30-60 秒内把"能不能用"过一遍**。

和另外两个脚本的分工：
    tools/healthcheck.py    本地静态检查（导入、关键函数在不在）
    tools/simulate_user.py  逐条深挖用户流程（10 分钟级，含真建模）
    tools/smoke.py          快速冒烟：服务在不在、页面能不能点、关键接口通不通

冒烟不跑重型生成 —— 它回答的是"装完/改完这次，用户能不能马上开始用"。
真出图/真建模交给 simulate_user.py。

    PYTHONPATH= python tools/smoke.py            # 全跑（含浏览器点击）
    PYTHONPATH= python tools/smoke.py --no-ui    # 不启浏览器（无 playwright 时）
    PYTHONPATH= python tools/smoke.py --json
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STUDIO = "http://127.0.0.1:7861"
APP = "http://127.0.0.1:7860"
API = "http://127.0.0.1:7863"
SITE = "https://zykt.space/foundry"

RESULTS: list[dict] = []


def check(name: str, fn, *, critical: bool = False) -> bool:
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    rec = {"name": name, "ok": bool(ok), "detail": str(detail)[:160],
           "sec": round(time.time() - t0, 2), "critical": critical}
    RESULTS.append(rec)
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name:<28}{rec['sec']:>6.2f}s  {rec['detail']}")
    return rec["ok"]


# CF / 部分反代会按 UA 拦非浏览器请求（实测 curl/urllib 直连站点 403 error 1010）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _get(url: str, timeout: float = 6.0, headers: dict | None = None) -> tuple[int, bytes]:
    h = {"User-Agent": _UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _port_up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── 1. 服务在不在 ─────────────────────────────────────────────
def t_ports():
    up = {p: _port_up(p) for p in (7860, 7861, 8188, 11434)}
    bad = [p for p, v in up.items() if not v]
    return (not bad), ("全在" if not bad else f"没起: {bad}")


def t_no_duplicates():
    """同一个服务不能有两份（会把显存劈开）。"""
    try:
        from backend import ports
        apps = ports._our_pids(("app.py",))
        studios = ports._our_pids(("studio.py",))
        # venv 的 python.exe 会再起一个子进程，父子算一份
        ok = len(apps) <= 2 and len(studios) <= 1
        return ok, f"app={len(apps)} studio={len(studios)}"
    except Exception as exc:
        return True, f"（跳过: {type(exc).__name__}）"


# ── 2. Studio 接口 ────────────────────────────────────────────
def _studio_token() -> str:
    import re
    _, raw = _get(STUDIO + "/", timeout=8)
    m = re.search(r"STUDIO_TOKEN='([^']+)'", raw.decode("utf-8", "replace"))
    return m.group(1) if m else ""


def t_studio_ping():
    st, raw = _get(STUDIO + "/api/ping", timeout=6)
    d = json.loads(raw)
    return st == 200 and d.get("app") == "forgeform-studio", f"v{d.get('version')}"


def t_studio_status():
    tok = _studio_token()
    st, raw = _get(STUDIO + "/api/status", headers={"X-Studio-Token": tok})
    d = json.loads(raw)
    return st == 200 and "job" in d, f'ready={d.get("ready")}'


def t_firstrun():
    tok = _studio_token()
    st, raw = _get(STUDIO + "/api/firstrun", headers={"X-Studio-Token": tok}, timeout=20)
    d = json.loads(raw)
    stages = d.get("stages") or {}
    ready = [k for k, v in stages.items() if v.get("ready")]
    return st == 200 and len(stages) == 3, f"就绪 {len(ready)}/3 {ready}"


def t_location():
    from backend import location
    rep = location.report()
    return bool(rep.get("recommended")), rep["recommended"]["path"]


# ── 3. 页面能不能点（真浏览器）────────────────────────────────
def t_ui_click():
    import asyncio
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            b = await p.chromium.launch(channel="msedge", headless=True)
            page = await b.new_page(viewport={"width": 1400, "height": 1000})
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)[:100]))
            # 带 #setup 进去 —— 这个入口曾经因为深链接 TDZ 报错、按钮全失效
            await page.goto(STUDIO + "/#setup", wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)
            bound = await page.evaluate(
                "typeof document.getElementById('scan').onclick === 'function'"
                " && typeof document.getElementById('launch').onclick === 'function'")
            toast = ""
            if bound:
                await page.evaluate("document.getElementById('scan').click()")
                await page.wait_for_timeout(700)
                toast = await page.inner_text("#toast")
            await b.close()
            return errs, bound, toast

    errs, bound, toast = asyncio.run(run())
    ok = bound and not errs
    return ok, ("按钮已绑定 " + ("有反馈" if toast else "无反馈")) if ok else (f"错误={errs}")


# ── 4. 创作空间 ───────────────────────────────────────────────
def t_app_page():
    st, raw = _get(APP + "/", timeout=10)
    txt = raw.decode("utf-8", "replace")
    need = ["chatbox", "Model3d", "btn_gen"]
    miss = [n for n in need if n.lower() not in txt.lower()]
    return (st == 200 and not miss), (f"{len(raw)} 字节" + (f" 缺 {miss}" if miss else ""))


def t_app_chat():
    from gradio_client import Client
    cli = Client(APP + "/", verbose=False)
    t0 = time.time()
    r = cli.predict("你好", api_name="/on_submit")
    chat = r[1] if len(r) > 1 else []
    reply = str(chat[-1][1])[:60] if chat else ""
    ok = bool(reply) and "DEVICEDEVICE" not in reply and "对话暂不可用" not in reply
    return ok, f"{time.time()-t0:.0f}s · {reply[:40]}"


# ── 5. 对外 API ──────────────────────────────────────────────
def t_api():
    if not _port_up(7863):
        return False, "7863 没起"
    st, raw = _get(API + "/v1/health", timeout=5)
    ok = st == 200 and json.loads(raw).get("ok")
    from backend import secrets
    rec = secrets.issue_api_key("smoke", ttl_days=1)
    try:
        _, raw2 = _get(API + "/v1/models",
                       headers={"Authorization": "Bearer " + rec["token"]})
        has_model = b'"image"' in raw2 or b'"mesh"' in raw2
        try:
            _get(API + "/v1/models")
            noauth = False
        except urllib.error.HTTPError as e:
            noauth = e.code == 401
    finally:
        secrets.delete_api_key(rec["id"])
    return (ok and has_model and noauth), f"health={ok} models={has_model} 401={noauth}"


# ── 6. 数据与产物 ────────────────────────────────────────────
def t_data_dirs():
    proj = ROOT / "projects"
    n = len([d for d in proj.iterdir() if d.is_dir()]) if proj.is_dir() else 0
    with_glb = len([d for d in proj.iterdir()
                    if (d / "model.glb").exists()]) if proj.is_dir() else 0
    sec = ROOT / ".secrets"
    return proj.is_dir() and n >= 1, f"{n} 个作品（{with_glb} 个带 GLB）· .secrets={sec.is_dir()}"


def t_artifact_download():
    """产物下载链接真的能取到字节（不是 404）。"""
    if not _port_up(7860):
        return False, "创作空间没起"
    proj = ROOT / "projects"
    target = None
    for d in sorted(proj.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
        p = d / "candidates" / "cand_0.jpg"
        if p.exists():
            target = p
            break
    if not target:
        return True, "（还没有候选图可测）"
    st, raw = _get(APP + "/gradio_api/file=" + target.as_posix(), timeout=15)
    return st == 200 and len(raw) > 1000, f"{len(raw)//1024} KB"


# ── 7. 发布与站点 ────────────────────────────────────────────
def t_release():
    """查 Release。**先直连再走反代** —— 2026-11-16 实测 gh-proxy 对
    api.github.com 返回 403，而直连是 200。反代只擅长转发 release 下载，
    不保证转发 API，所以两条都试。"""
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    api = f"api.github.com/repos/TIANKONGskyy/forgeform/releases/tags/v{ver}"
    last = ""
    for url in ("https://" + api, "https://gh-proxy.com/https://" + api):
        try:
            st, raw = _get(url, timeout=20)
            d = json.loads(raw)
            assets = d.get("assets") or []
            if st == 200 and assets:
                return True, f'v{ver} · {assets[0]["name"]}（{url.split("/")[2]}）'
            last = f"st={st} assets={len(assets)}"
        except Exception as exc:
            last = f"{type(exc).__name__}"
    return False, f"查不到 v{ver}（{last}）"


def t_site():
    try:
        st, raw = _get(SITE, timeout=20)
        ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        has = f"v{ver}".encode() in raw
        return st == 200 and has, f"{len(raw)//1024} KB · 含 v{ver}={has}"
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def t_zip_contains():
    """发布包里必须有新模块（否则用户下到的是旧代码）。"""
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    gh = f"github.com/TIANKONGskyy/forgeform/releases/download/v{ver}/forgeform-v{ver}.zip"
    tmp = Path.home() / "AppData" / "Local" / "Temp" / f"smoke_{ver}.zip"
    if not tmp.exists() or tmp.stat().st_size < 100_000:
        last = ""
        for url in ("https://gh-proxy.com/https://" + gh, "https://" + gh):
            try:
                st, raw = _get(url, timeout=300)
                if st == 200 and len(raw) > 100_000:
                    tmp.write_bytes(raw)
                    break
                last = f"st={st} {len(raw)}B"
            except Exception as exc:
                last = f"{type(exc).__name__}"
        else:
            return False, f"下载发布包失败（{last}）"
    import zipfile
    with zipfile.ZipFile(tmp) as z:
        names = z.namelist()
    # 必须有的（用户装完就依赖它们）
    must = ["backend/ports.py", "backend/location.py"]
    miss = [n for n in must if not any(x.endswith(n) for x in names)]
    # 本地新增、可能还没发版的：只提示，不算失败
    newer = [n for n in ("tools/smoke.py", "tools/doctor.py", "tools/calibrate.py")
             if not any(x.endswith(n) for x in names)]
    detail = f"{len(names)} 个文件"
    if miss:
        detail += f" 缺必需 {miss}"
    if newer:
        detail += f"（本地新增未发版: {newer}）"
    return (not miss), detail


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想冒烟测试")
    ap.add_argument("--no-ui", action="store_true", help="不启浏览器")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 66)
    print("  铸想 ForgeForm · 冒烟测试")
    print(f"  目录 {ROOT}   版本 {(ROOT / 'VERSION').read_text(encoding='utf-8').strip()}")
    print("=" * 66)

    print("\n【服务】")
    check("端口与服务", t_ports, critical=True)
    check("没有重复实例", t_no_duplicates)

    print("\n【Studio】")
    check("ping 探活", t_studio_ping)
    check("status 状态", t_studio_status)
    check("一键开始三条链", t_firstrun)
    check("安装位置判定", t_location)
    if not args.no_ui:
        check("页面按钮可点（真浏览器）", t_ui_click, critical=True)

    print("\n【创作空间】")
    check("页面与关键元素", t_app_page, critical=True)
    check("一轮对话", t_app_chat)

    print("\n【对外 API】")
    check("鉴权与模型清单", t_api)

    print("\n【数据】")
    check("作品目录", t_data_dirs)
    check("产物下载", t_artifact_download)

    print("\n【发布与站点】")
    check("GitHub Release", t_release)
    check("站点 /foundry", t_site)
    check("发布包含新模块", t_zip_contains)

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["ok"])
    crit_fail = [r["name"] for r in RESULTS if r["critical"] and not r["ok"]]
    print("\n" + "=" * 66)
    print(f"  {passed}/{total} 通过" + (f" · 关键失败: {crit_fail}" if crit_fail else " · 关键项全过"))
    if passed < total:
        print("  失败项:")
        for r in RESULTS:
            if not r["ok"]:
                print(f"    ✗ {r['name']}: {r['detail']}")
    print("=" * 66)
    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    return 0 if not crit_fail and passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
