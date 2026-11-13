# -*- coding: utf-8 -*-
"""启动前的自检：检查更新、桌面快捷方式、网络加速。

    python tools/bootstrap.py            # 交互（启动器调用）
    python tools/bootstrap.py --quiet    # 只检查不改动（CI / 测试）
    python tools/bootstrap.py --no-update

## 流程

1. **查最新版**（3 秒超时，失败就当没有网络）
   - 没网络 / 已经最新 → 直接返回，进应用
   - 有新版本 → 询问用户要不要装（不自动装）
2. **桌面快捷方式**：没有就建一个（只建一次，不覆盖已有的）
3. **网络加速**：下载模型前如果慢，提示可以装 Steam++（**用户点确认才装**）

## 为什么更新不自动装

自动更新会替换正在运行的程序，用户正在用的东西突然变了很怪。
所以只**提示**，用户点了才下。而且回滚成本高 —— 更新器虽然保留用户数据，
但版本不兼容时退不回去。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "TIANKONGskyy/forgeform"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
API_CN = f"https://gh-proxy.com/https://api.github.com/repos/{REPO}/releases/latest"


def _log(msg: str) -> None:
    print(msg, flush=True)


def local_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def check_update(timeout: int = 4) -> dict:
    """查最新版。返回 {ok, has_network, latest, current, newer, url}。

    超时短（4 秒）—— 启动时不能让用户干等。失败就当作没有网络。
    """
    cur = local_version()
    for url in (API, API_CN):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "ForgeForm-launcher", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            latest = (d.get("tag_name") or "").lstrip("v")
            return {
                "ok": True, "has_network": True,
                "current": cur, "latest": latest,
                "newer": _version_tuple(latest) > _version_tuple(cur),
                "url": d.get("html_url") or "",
                "size_mb": round(sum(a.get("size", 0) for a in d.get("assets", [])) / 1024 / 1024, 2),
            }
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:80]}"
    return {"ok": False, "has_network": False, "current": cur,
            "latest": cur, "newer": False, "error": last}


# ── 桌面快捷方式 ────────────────────────────────────────────
SHORTCUT_NAME = "ZYKT铸想.lnk"


def desktop_dir() -> Path | None:
    """拿真实桌面路径（可能被 OneDrive 重定向）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            v, _ = winreg.QueryValueEx(k, "Desktop")
        return Path(os.path.expandvars(v))
    except Exception:
        p = Path.home() / "Desktop"
        return p if p.exists() else None


def shortcut_exists() -> bool:
    d = desktop_dir()
    return bool(d and (d / SHORTCUT_NAME).exists())


def create_shortcut(force: bool = False) -> dict:
    """在桌面建一个指向 启动.bat 的快捷方式。已存在就不动。"""
    if os.name != "nt":
        return {"ok": False, "error": "只支持 Windows"}
    d = desktop_dir()
    if not d:
        return {"ok": False, "error": "找不到桌面目录"}
    target = d / SHORTCUT_NAME
    if target.exists() and not force:
        return {"ok": True, "already": True, "path": str(target)}

    bat = ROOT / "启动.bat"
    if not bat.exists():
        bat = ROOT / "start.ps1"
    if not bat.exists():
        return {"ok": False, "error": "仓库里没有 启动.bat"}

    ps = f'''
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("{target}")
$sc.TargetPath = "{bat}"
$sc.WorkingDirectory = "{ROOT}"
$sc.IconLocation = "{ROOT}\\docs\\assets\\icon.ico,0"
$sc.Description = "ZYKT铸想 — 一句话到可打印模型"
$sc.Save()
'''
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if target.exists():
            return {"ok": True, "created": True, "path": str(target)}
        return {"ok": False, "error": (r.stderr or r.stdout or "未知")[:160]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── 自动更新 ────────────────────────────────────────────────
def auto_update(quiet: bool = False) -> dict:
    """发现新版就下载并合并。用户数据（config/outputs/models/.venv）保留。

    2026-11-13：用户要求"双击就自动更新到最新版"。所以这里不再只是提示，
    而是真的装 —— 但**保留用户数据**（updater.py 的白名单控制），
    并且失败不阻断启动（更新失败照样用旧版跑）。
    """
    upd = check_update()
    if not upd.get("ok"):
        return {"ok": False, "skipped": "no-network"}
    if not upd.get("newer"):
        return {"ok": True, "skipped": "up-to-date", "version": upd.get("current")}

    tag = "v" + upd["latest"]
    if not quiet:
        _log(f"发现新版本 {upd['latest']}（当前 {upd['current']}），正在更新…")

    # 下载 zip：先试 gh-proxy（国内快），再直连
    import tempfile
    url_name = f"forgeform-{tag}.zip"
    base = f"https://github.com/{REPO}/releases/download/{tag}/{url_name}"
    urls = [f"https://gh-proxy.com/{base}", base]
    tmp = Path(tempfile.mkdtemp()) / url_name
    got = False
    last = ""
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "ForgeForm-updater"})
            with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            if tmp.stat().st_size > 1_000_000:
                got = True
                break
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:70]}"
    if not got:
        return {"ok": False, "error": f"下载失败: {last}"}

    # 解压 + 合并（updater.py 里已实现"保留用户数据"）
    import zipfile
    extract = tmp.parent / "pkg"
    try:
        with zipfile.ZipFile(tmp) as z:
            z.extractall(extract)
    except Exception as exc:
        return {"ok": False, "error": f"解压失败: {exc}"}

    root = next((d for d in extract.iterdir() if (d / "app.py").exists()), None)
    if not root:
        return {"ok": False, "error": "包结构异常（没有 app.py）"}

    cmd = [sys.executable, str(ROOT / "tools" / "updater.py"),
           "--from", str(root), "--to", str(ROOT)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    ok = r.returncode == 0
    if not quiet:
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-3:]
        for l in tail:
            _log("  " + l)
    return {"ok": ok, "from": upd["current"], "to": upd["latest"],
            "output": (r.stdout or "")[-500:] if ok else (r.stderr or "")[-500:]}


# ── Steam++ ────────────────────────────────────────────────
def steampp_state() -> dict:
    try:
        sys.path.insert(0, str(ROOT))
        from backend import netboost
        return netboost.detect()
    except Exception as exc:
        return {"installed": False, "error": str(exc)}


STEAMPP_URL = "https://steampp.net/download"


def offer_steampp() -> dict:
    """询问用户要不要装 Steam++。返回用户的决定，不在这里装。"""
    st = steampp_state()
    if st.get("installed"):
        return {"installed": True, "ask": False, **st}
    return {"installed": False, "ask": True, "url": STEAMPP_URL, **st}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-update", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--auto-update", action="store_true",
                    help="发现新版就下载并合并（保留 config/outputs/models）")
    args = ap.parse_args()

    if not args.quiet:
        _log(f"当前版本 {local_version()}")

    # 1. 桌面快捷方式（不联网，先做）
    sc = create_shortcut()
    if not args.quiet:
        _log(f"桌面快捷方式: {'已有' if sc.get('already') else ('已创建' if sc.get('created') else '跳过')}")

    # 2. 检查更新（--auto-update 时直接装）
    upd = {"ok": False, "newer": False}
    if args.auto_update:
        res = auto_update(quiet=args.quiet)
        upd = {"ok": res.get("ok", False), "auto": True, **res}
        if not args.quiet:
            if res.get("skipped") == "up-to-date":
                _log(f"已是最新 {res.get('version')}")
            elif res.get("skipped") == "no-network":
                _log("无网络，跳过更新，直接启动")
            elif res.get("ok"):
                _log(f"已更新 {res.get('from')} → {res.get('to')}")
            else:
                _log(f"更新失败（{res.get('error')}），继续用当前版本启动")
    elif not args.no_update:
        t0 = time.time()
        upd = check_update()
        dt = time.time() - t0
        if not args.quiet:
            if not upd.get("ok"):
                _log(f"检查更新: 无网络（{dt:.1f}s），直接启动")
            elif upd.get("newer"):
                _log(f"检查更新: 发现 {upd['latest']}（当前 {upd['current']}）")
            else:
                _log(f"检查更新: 已是最新 {upd['current']}（{dt:.1f}s）")

    # 3. Steam++ 状态（只报状态，安装由界面询问）
    sp = offer_steampp()
    if not args.quiet:
        _log(f"Steam++: {'已装' + sp.get('path', '') if sp.get('installed') else '未装（可用 ' + STEAMPP_URL + '）'}")

    result = {"version": local_version(), "update": upd, "shortcut": sc, "steampp": sp}
    out = ROOT / "logs" / "bootstrap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.quiet or args.check_only:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
