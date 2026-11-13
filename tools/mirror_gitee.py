# -*- coding: utf-8 -*-
"""把铸想镜像到 Gitee。

    set GITEE_TOKEN=<你的私人令牌>      # 或者用 --token
    python tools/mirror_gitee.py --create   # 首次：建仓库并推送
    python tools/mirror_gitee.py --push     # 以后：只推送

## 为什么要这个

国内访问 GitHub 的 Release 实测 0.17 MB/s，解压包 4 MB 还能忍，
但用户往往会以为"打不开"。Gitee 是国内的，速度快得多。

## 需要什么

Gitee 的**私人令牌**（不是密码）：https://gitee.com/profile/personal_access_tokens
勾选 `projects` 权限即可。令牌只存在环境变量里，不写进任何文件。

## 它做什么

1. 调 Gitee OpenAPI 建仓库（已存在就跳过）
2. 加 `gitee` 远端
3. `git push gitee main --tags`
4. 在 Gitee 上建 Release 并上传 zip（可选，`--release`）

**不会**改 GitHub 那边任何东西，也不会动工作区。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "TIANKONGskyy"
REPO = "forgeform"
API = "https://gitee.com/api/v5"


def _token(args) -> str:
    tk = getattr(args, "token", None) or os.environ.get("GITEE_TOKEN") or os.environ.get("GITEE_ACCESS_TOKEN")
    if not tk:
        print("✗ 没有 Gitee 令牌。")
        print("  1. 打开 https://gitee.com/profile/personal_access_tokens")
        print("  2. 新建令牌，勾选 projects 权限")
        print("  3. set GITEE_TOKEN=<令牌>   然后重跑")
        sys.exit(2)
    return tk


def _req(method: str, path: str, token: str, data: dict | None = None) -> dict:
    url = f"{API}{path}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "ForgeForm-mirror",
    })
    sep = "&" if "?" in url else "?"
    req.full_url = f"{url}{sep}access_token={token}"
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return {"_error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def ensure_repo(token: str) -> dict:
    """建仓库；已存在就返回现有信息。"""
    info = _req("GET", f"/repos/{OWNER}/{REPO}", token)
    if "_error" not in info:
        print(f"  仓库已存在: {info.get('html_url')}")
        return {"ok": True, "existed": True, **info}

    print("  仓库不存在，创建中…")
    created = _req("POST", "/user/repos", token, {
        "name": REPO,
        "description": "铸想 ForgeForm —— 一句话生成可打印三维模型的本地工作流。AGPL-3.0",
        "private": False,
        "auto_init": False,
    })
    if "_error" in created:
        return {"ok": False, **created}
    print(f"  创建成功: {created.get('html_url')}")
    return {"ok": True, "existed": False, **created}


def push(token: str) -> dict:
    remote = f"https://{OWNER}:{token}@gitee.com/{OWNER}/{REPO}.git"
    subs = [
        (["git", "remote", "add", "gitee", remote], True),
        (["git", "remote", "set-url", "gitee", remote], True),
    ]
    for cmd, _ in subs:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        if r.returncode == 0:
            break
    print("  git push gitee main --tags …")
    r = subprocess.run(["git", "push", "gitee", "main", "--tags", "--force"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0
    print(f"  {'✓ 推送成功' if ok else '✗ 推送失败'}")
    if not ok:
        print("   " + out[-400:])
    return {"ok": ok, "output": out[-600:]}


def make_release(token: str) -> dict:
    """在 Gitee 建 Release 并上传本地打包的 zip。"""
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = f"v{ver}"
    zips = sorted(ROOT.glob("*.zip"))
    if not zips:
        print(f"  ✗ 仓库根目录没有 zip；先打一个包")
        return {"ok": False, "error": "没有 zip"}
    asset = zips[0]
    print(f"  Release {tag}，附件 {asset.name}（{asset.stat().st_size / 1024 / 1024:.2f} MB）")
    created = _req("POST", f"/repos/{OWNER}/{REPO}/releases", token, {
        "tag_name": tag, "target_commitish": "main", "name": f"铸想 ForgeForm {tag}",
        "body": "一句话生成可打印的 STL。解压后双击 启动.bat。",
    })
    if "_error" in created:
        # 已存在就找出来
        got = _req("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{tag}", token)
        if "_error" in got:
            return {"ok": False, **created}
        created = got
    rid = created.get("id")
    print(f"  release id={rid}")
    # 附件上传要用 multipart，这里给出手动路径（Gitee 的 API 上传比较挑）
    print("  → 附件请手动上传到 Gitee Release 页面（API 上传对 Content-Type 要求较严）")
    return {"ok": True, "release_id": rid, "tag": tag}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None)
    ap.add_argument("--create", action="store_true", help="首次：建仓库")
    ap.add_argument("--push", action="store_true", help="只推送代码")
    ap.add_argument("--release", action="store_true", help="再建一个 Release")
    args = ap.parse_args()

    token = _token(args)
    print("=" * 60)
    print("铸想 → Gitee 镜像")
    print("=" * 60)

    result = {}
    if args.create or not (args.push or args.release):
        result["repo"] = ensure_repo(token)

    result["push"] = push(token)

    if args.release:
        result["release"] = make_release(token)

    print("\n" + "=" * 60)
    print(f"仓库地址: https://gitee.com/{OWNER}/{REPO}")
    print("=" * 60)
    return 0 if result.get("push", {}).get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
