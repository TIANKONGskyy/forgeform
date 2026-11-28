# -*- coding: utf-8 -*-
"""内置 GitHub 加速（2026-11-28）。

用户的要求：不要为了拉 GitHub 再去装 Steam++ / 开全局代理，铸想自己带加速。

做法：所有 GitHub 相关下载都走"镜像前缀"拼接，按顺序回退：
    gh-proxy.com → ghfast.top → 直连
支持：
- `gh_url(url)`：给任意 github.com / raw.githubusercontent.com 链接加前缀
- `clone(url, dest)`：git clone 也走镜像（实测直连会 0 字节卡死 / 连接重置）
- `pip_args()`：pip 用的国内源（与 tools/pip_mirror.py 保持同一份配置）
- `git_config_hint()`：给用户看的"为什么这样配"

配置：config.json → `network.github_mirrors` 可覆盖顺序；`network.github_mirror` 单独指定。
环境变量 `FORGEFORM_GH_MIRROR` 优先级最高。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MIRRORS = ["https://gh-proxy.com/", "https://ghfast.top/"]
PIP_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
]


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def mirrors() -> list[str]:
    env = os.environ.get("FORGEFORM_GH_MIRROR", "").strip()
    if env:
        return [env if env.endswith("/") else env + "/"]
    net = (_cfg().get("network") or {})
    one = str(net.get("github_mirror") or "").strip()
    if one:
        return [one if one.endswith("/") else one + "/"]
    lst = net.get("github_mirrors")
    if isinstance(lst, list) and lst:
        return [str(x) if str(x).endswith("/") else str(x) + "/" for x in lst]
    return list(DEFAULT_MIRRORS)


def gh_url(url: str, mirror: str | None = None) -> str:
    """把 github 链接变成"镜像前缀 + 原链接"。非 github 链接原样返回。"""
    u = str(url or "").strip()
    if not u or not u.startswith("http"):
        return u
    if "github.com" not in u and "githubusercontent.com" not in u:
        return u
    m = mirror or (mirrors() or [""])[0]
    if not m:
        return u
    if u.startswith(m):
        return u
    return m.rstrip("/") + "/" + u


def gh_urls(url: str) -> list[str]:
    """同一链接的多个镜像版本（按优先级），最后附直连。"""
    out = []
    for m in mirrors():
        c = gh_url(url, m)
        if c not in out:
            out.append(c)
    if url not in out:
        out.append(url)
    return out


def clone(url: str, dest: str | Path, *, depth: int = 1, timeout: int = 1800,
          log=print) -> str:
    """git clone，逐个镜像尝试；都失败再试直连。返回实际用的 URL。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = ""
    for candidate in gh_urls(url):
        try:
            log(f"    clone {candidate.split('/')[2]} …")
            p = subprocess.run(
                ["git", "clone", "--depth", str(depth), candidate, str(dest)],
                capture_output=True, timeout=timeout)
            if p.returncode == 0:
                return candidate
            last = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            last = last[0][:160]
            log(f"    ✗ {last}")
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            log(f"    ✗ {last}")
    raise RuntimeError(f"git clone 失败（所有镜像都试过）：{last}")


def git_config_hint() -> str:
    """给用户看的一句话（不要让他去装 Steam++）。"""
    return ("GitHub 下载已内置加速（" + " → ".join(m.split("//")[-1].rstrip("/") for m in mirrors())
            + " → 直连），不需要另外装 Steam++ 或开代理。")


def pip_args(mirror: str | None = None) -> list[str]:
    m = mirror or PIP_MIRRORS[0]
    host = m.split("//")[-1].split("/")[0]
    return ["-i", m, "--trusted-host", host]


def apply_git_proxy_to_env(env: dict | None = None) -> dict:
    """给子进程环境加镜像提示（不会改用户全局 git 配置）。"""
    e = dict(env or os.environ)
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    return e
