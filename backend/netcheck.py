# -*- coding: utf-8 -*-
"""网络与环境自检：**先量一遍再决定用哪个源**，不靠猜。

## 为什么需要"量"

国内网络下这些假设经常是错的：

    "清华源最快"        → 2026-10 pip 清华返回 403，换了阿里云
    "HF 官方能连"       → 实测 reset，得走 hf-mirror
    "GitHub 直连慢"     → 有时直连 3 MB/s，gh-proxy 反而更慢
    "pip 会读我的 -i"   → pip config 里有一条更硬的，`-i` 压不过

所以这里不写死任何源，而是**当场测延迟和可下载性**，取最快能用的那个，
把结果缓存 1 小时（避免每次安装都测一遍）。

## 三类目标

    pypi   pip 装包      阿里云 / 清华 / 中科大 / 腾讯 / 官方
    github 代码与 release 直连 / gh-proxy / hub.fastgit 类
    hf     模型权重       hf-mirror / ModelScope / 官方

## 用法

    from backend.netcheck import pick_pypi, pick_github, pick_hf, probe_all
    url = pick_pypi()           # 'https://mirrors.aliyun.com/pypi/simple'
    print(probe_all())
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "logs" / "net-cache.json"
# 6 小时。原来 1 小时 —— 实测巡检每轮都撞上过期，白花 15-20 秒重测；
# 网络状况不会一小时一变，真要重测用 tools/doctor.py --refresh。
CACHE_TTL = 6 * 3600

PYPI = {
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "ustc": "https://mirrors.ustc.edu.cn/pypi/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
    "pypi": "https://pypi.org/simple",
}

GITHUB = {
    "direct": "https://github.com",
    "gh-proxy": "https://gh-proxy.com/https://github.com",
    "ghproxy-net": "https://ghproxy.net/https://github.com",
}

HF = {
    "hf-mirror": "https://hf-mirror.com",
    "official": "https://huggingface.co",
}
# ModelScope 只用来判断"国内大站通不通"，**不能当 HF 的替代** ——
# 它的 URL 形状（/models/<org>/<name>/resolve/...）和 HF 不同，
# 拿它拼 HF 路径必定 404。真要下 ModelScope 上的模型得单独写适配。
HF_REFERENCE = {"modelscope": "https://www.modelscope.cn"}


# ══════════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════════
def _load_cache() -> dict:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - float(d.get("at") or 0) < CACHE_TTL:
            return d
    except Exception:
        pass
    return {}


def _save_cache(d: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        d = dict(d)
        d["at"] = time.time()
        CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# 探测
# ══════════════════════════════════════════════════════════════════
def _probe(url: str, timeout: float = 6.0, head: int = 2048) -> dict:
    """测一个 URL：能不能连、首字节多久、拿到多少字节。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ForgeForm/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunk = r.read(head)
            first = time.time() - t0
            return {"url": url, "ok": True, "ms": round(first * 1000),
                    "bytes": len(chunk), "status": getattr(r, "status", 200)}
    except urllib.error.HTTPError as e:
        return {"url": url, "ok": e.code < 500, "ms": round((time.time() - t0) * 1000),
                "bytes": 0, "status": e.code,
                "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"url": url, "ok": False, "ms": round((time.time() - t0) * 1000),
                "bytes": 0, "status": 0, "error": f"{type(e).__name__}: {e}"[:80]}


def probe_pypi(timeout: float = 6.0) -> list[dict]:
    """探 PyPI 镜像。用 `simple/pip/` 这种必然存在的小页，不拉大包。"""
    out = []
    for name, base in PYPI.items():
        r = _probe(base.rstrip("/") + "/pip/", timeout)
        r["name"] = name
        r["base"] = base
        out.append(r)
    out.sort(key=lambda r: (not r["ok"], r["ms"]))
    return out


def probe_github(timeout: float = 6.0) -> list[dict]:
    """探 GitHub：拿一个小 release 的 HEAD / 首页首字节。"""
    out = []
    for name, base in GITHUB.items():
        target = base.rstrip("/") + "/TIANKONGskyy/forgeform/raw/main/VERSION"
        r = _probe(target, timeout)
        r["name"] = name
        r["base"] = base
        out.append(r)
    out.sort(key=lambda r: (not r["ok"], r["ms"]))
    return out


# 探 HF 必须探**真实文件下载**，不能只探 /api ——
# 实测：huggingface.co 的 /api 0.7s 就回 200，但同一个站的 resolve（真下权重）
# 会被 reset。用 API 判断"能用"会把人带到坑里。
_HF_PROBE_FILE = "/bert-base-uncased/resolve/main/config.json"


def probe_hf(timeout: float = 6.0) -> list[dict]:
    """只探**可直接替换**的 HF 源（hf-mirror / 官方），且探的是真文件。"""
    out = []
    for name, base in HF.items():
        target = base.rstrip("/") + _HF_PROBE_FILE
        r = _probe(target, timeout, head=512)
        r["name"] = name
        r["base"] = base
        out.append(r)
    out.sort(key=lambda r: (not r["ok"], r["ms"]))
    return out


def probe_all(timeout: float = 6.0, use_cache: bool = True) -> dict:
    """三类一起测。结果缓存 1 小时。"""
    cached = _load_cache() if use_cache else {}
    if cached.get("pypi") and cached.get("github") and cached.get("hf"):
        cached["cached"] = True
        return cached
    out = {"pypi": probe_pypi(timeout), "github": probe_github(timeout),
           "hf": probe_hf(timeout), "cached": False}
    _save_cache(out)
    return out


def pick(table: str, fallback: str = "") -> str:
    """从缓存（或现测）里取最快可用的 base。全不通就用 fallback。"""
    data = probe_all()
    for r in (data.get(table) or []):
        if r.get("ok"):
            return r["base"]
    return fallback


def pick_pypi() -> str:
    return pick("pypi", PYPI["aliyun"])


def pick_github() -> str:
    return pick("github", GITHUB["gh-proxy"])


def pick_hf() -> str:
    return pick("hf", HF["hf-mirror"])


# ══════════════════════════════════════════════════════════════════
# 环境检查（不联网的部分）
# ══════════════════════════════════════════════════════════════════
def _run(argv: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def env_checks() -> list[dict]:
    """本地环境：每条都给 ok / 现状 / 怎么修。"""
    out = []

    v = sys.version_info
    out.append({
        "name": "Python 版本",
        "ok": (3, 10) <= (v.major, v.minor) <= (3, 14),
        "value": f"{v.major}.{v.minor}.{v.micro}  ({sys.executable})",
        "fix": "装 Python 3.11–3.13（winget install Python.Python.3.12）。"
               "3.9 及以下缺 3.10+ 语法，3.14 上部分轮子还没出。",
    })

    # pip 的有效 index（pip config 的优先级高于命令行 -i，这是最容易踩的坑）
    # 注意：**不要用"是不是白名单里的域名"来判断**，要真去测那个源。
    # 实测这台机器上清华源 2026-10 报过 403，11 月又恢复了 —— 域名判断会误报。
    code, txt = _run([sys.executable, "-m", "pip", "config", "list"])
    index_url = ""
    for line in txt.splitlines():
        low = line.lower()
        if "index-url" in low or "index_url" in low:
            index_url = line.split("=", 1)[-1].strip().strip("'\"")
    if index_url:
        probe = _probe(index_url.rstrip("/") + "/six/", timeout=6.0)
        pip_ok = bool(probe.get("ok"))
        pip_val = (f'{index_url} · {probe["ms"]} ms '
                   f'{"可用" if pip_ok else "不通（" + probe.get("error", str(probe.get("status"))) + "）"}')
        pip_fix = (f"pip config 里的 index-url 优先于 -i。这个源不通就改："
                   f"`pip config set global.index-url {PYPI['aliyun']}`"
                   f"（会自动按实测挑一个能用的）。")
    else:
        pip_ok, pip_val = True, "（无硬配置，命令行 -i 生效）"
        pip_fix = ""
    out.append({
        "name": "pip 有效源",
        "ok": pip_ok,
        "value": pip_val,
        "fix": pip_fix,
    })

    # 代理环境变量（经常是"为什么连不上"的真凶）
    prox = {k: v for k, v in __import__("os").environ.items()
            if k.lower() in ("http_proxy", "https_proxy", "all_proxy") and v}
    out.append({
        "name": "代理环境变量",
        "ok": not prox,
        "value": json.dumps(prox, ensure_ascii=False) if prox else "（无）",
        "fix": "有代理但代理挂了会表现为「什么都连不上」。临时清掉："
               "`set HTTPS_PROXY=` / `$env:HTTPS_PROXY=''`，或确认代理端口在跑。",
    })

    # 长路径支持（Windows 上路径 >260 会让某些包解压失败）
    long_ok = True
    long_val = "非 Windows，不适用"
    if sys.platform == "win32":
        code, txt = _run(["reg", "query",
                          r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
                          "/v", "LongPathsEnabled"])
        long_ok = "0x1" in txt
        long_val = "已启用" if long_ok else "未启用（>260 字符的路径会失败）"
    out.append({
        "name": "Windows 长路径",
        "ok": long_ok,
        "value": long_val,
        "fix": "管理员 PowerShell：`reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
               "/v LongPathsEnabled /t REG_DWORD /d 1 /f`，然后重启。",
    })

    # 磁盘
    try:
        import shutil as _sh
        free_gb = _sh.disk_usage(str(ROOT)).free / 1024 ** 3
    except Exception:
        free_gb = -1
    out.append({
        "name": "磁盘余量",
        "ok": free_gb >= 30,
        "value": f"{free_gb:.0f} GB 可用" if free_gb >= 0 else "读不到",
        "fix": "完整方案要 15–25 GB（含权重）。少于 30 GB 建议先清 outputs/ 或换盘。",
    })

    # 端口
    taken = []
    for port in (7860, 7861, 7863, 8188):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                taken.append(port)
    out.append({
        "name": "端口占用",
        "ok": True,
        "value": ("占用中: " + ", ".join(map(str, taken))) if taken else "7860/7861/7863/8188 都空",
        "fix": "铸想会自动换端口；若浏览器打开的是旧版界面，先关掉占 7860 的旧进程。",
    })

    # ComfyUI（装了就能复用，省 23 GB 下载）
    try:
        from backend.backends.detect import guess_comfy_dir
        cdir = guess_comfy_dir()
    except Exception:
        cdir = ""
    out.append({
        "name": "已有 ComfyUI",
        "ok": bool(cdir),
        "value": cdir or "没找到",
        "fix": "装了 ComfyUI 就能选「复用现有 ComfyUI（零下载）」。"
               "没装也不影响：直连模式（diffusers / hy3dgen）一样能跑。",
    })

    # GPU
    code, txt = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                      "--format=csv,noheader"])
    gpu_ok = code == 0 and txt.strip() != ""
    out.append({
        "name": "NVIDIA 驱动",
        "ok": gpu_ok,
        "value": txt.strip().splitlines()[0] if gpu_ok else "nvidia-smi 不可用",
        "fix": "装最新 Game Ready / Studio 驱动。没有 N 卡也能装，但只能看界面。",
    })
    return out


def report() -> dict:
    """一次性拿到网络 + 环境全貌。"""
    net = probe_all()
    env = env_checks()

    def _best(rows: list[dict]) -> str:
        for r in rows:
            if r.get("ok"):
                return f'{r["name"]} ({r["ms"]} ms)'
        return "全部不通"

    return {
        "net": {
            "pypi": _best(net.get("pypi") or []),
            "github": _best(net.get("github") or []),
            "hf": _best(net.get("hf") or []),
            "detail": {k: net.get(k) for k in ("pypi", "github", "hf")},
        },
        "env": env,
        "bad": [e["name"] for e in env if not e["ok"]],
    }
