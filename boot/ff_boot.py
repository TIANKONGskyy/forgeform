# -*- coding: utf-8 -*-
"""铸想一键安装器的下载/解压引擎（被 boot/install.ps1 调用）。

为什么要单独一个 Python 文件
---------------------------
Windows 上"双击即装"要同时满足：并行下载（多连接）、边下边解、镜像回退、断点续传。
PowerShell 写这些会很啰嗦，而且路径/编码坑多；用 Python 写清楚得多。

模式
----
    python ff_boot.py plan                 # 只做环境探测，输出 JSON 给 ps1 读
    python ff_boot.py install --tag v3.14.3 --dest D:\ff [--mirror URL ...] [--local FILE]
                                          # 下载 + 边下边解 + 解压到 dest

设计要点
--------
- 优先下载 **tar**（asset 名 forgeform-<tag>.tar）：tar 是顺序格式，可以边下边解
- 只有 zip 时才退化为"下完再解"
- 多连接用 HTTP Range（8 段），每段写自己的偏移，最后校验总字节数
- 镜像顺序：gh-proxy → ghfast → 直连；任一可用即停
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

UA = {"User-Agent": "ForgeForm-Boot/1.0"}
MIRRORS = ["https://gh-proxy.com/", "https://ghfast.top/", ""]
REPO = "TIANKONGskyy/forgeform"
CHUNK = 1 << 20


def log(msg: str) -> None:
    print(msg, flush=True)


try:
    import requests  # 有就用（更好用）
except Exception:  # pragma: no cover
    requests = None


class _Resp:
    """统一 requests / urllib 两种响应。"""

    def __init__(self, status: int, headers: dict, body=None, raw=None):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self._raw = raw

    @property
    def content(self) -> bytes:
        return self._body or b""

    def json(self):
        import json as _j
        return _j.loads(self.content.decode("utf-8"))

    def iter_content(self, size: int = 1 << 20):
        if self._raw is not None:
            while True:
                chunk = self._raw.read(size)
                if not chunk:
                    break
                yield chunk
            return
        body = self._body or b""
        for i in range(0, len(body), size):
            yield body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if self._raw is not None:
            try:
                self._raw.close()
            except Exception:
                pass
        return False


def _req(url: str, method: str = "GET", headers=None, stream: bool = False, timeout=60):
    h = dict(UA)
    if headers:
        h.update(headers)
    if requests is not None:
        return requests.request(method, url, headers=h, stream=stream, timeout=timeout)

    # urllib 兜底（不依赖第三方包）
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout if isinstance(timeout, (int, float)) else 60)
        if stream:
            return _Resp(r.status, dict(r.headers), raw=r)
        body = r.read()
        return _Resp(r.status, dict(r.headers), body=body)
    except urllib.error.HTTPError as e:
        return _Resp(e.code, dict(e.headers or {}), body=e.read() if not stream else b"")
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def mirror_urls(path: str) -> list[str]:
    """把仓库内路径拼成各镜像的完整 URL。

    测试时可用环境变量 FF_GH_BASE 指向本地 HTTP 服务（例如 http://127.0.0.1:8799），
    线上默认是 GitHub 仓库地址。
    """
    override = os.environ.get("FF_GH_BASE", "").strip()
    if override:
        return [f"{override.rstrip('/')}/{path.lstrip('/')}"]
    base = f"https://github.com/{REPO}/{path.lstrip('/')}"
    out = []
    for m in MIRRORS:
        out.append((m + base) if m else base)
    return out


def probe() -> dict:
    """环境探测（给 ps1 决定要不要下 Python）。"""
    info = {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "cuda": False,
        "gpu": "",
        "vram_gb": 0,
        "disk_free_gb": 0,
        "ps": "",
    }
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            text=True, timeout=8).strip().splitlines()
        if out:
            name, mem = [x.strip() for x in out[0].split(",")]
            info["gpu"] = name
            info["vram_gb"] = round(float(mem.split()[0]) / 1024, 1)
            info["cuda"] = True
    except Exception:
        pass
    try:
        info["disk_free_gb"] = round(shutil.disk_usage(os.path.abspath(os.sep)).free / 1024**3, 1)
    except Exception:
        pass
    return info


def resolve_tag(mirror: str = "") -> str:
    """通过镜像拿最新 tag（失败就返回空，由调用方决定用哪个版本）。"""
    api = f"{mirror}https://api.github.com/repos/{REPO}/releases?per_page=5" if mirror else \
          f"https://api.github.com/repos/{REPO}/releases?per_page=5"
    try:
        r = _req(api)
        if r.status_code == 200:
            for rel in r.json():
                if not rel.get("draft") and not rel.get("prerelease") and rel.get("tag_name"):
                    return rel["tag_name"]
    except Exception:
        pass
    return ""


def _head(url: str) -> tuple[int, bool]:
    try:
        r = _req(url, "HEAD", timeout=30)
        if r.status_code >= 400:
            return -1, False
        size = int(r.headers.get("Content-Length") or 0)
        return size, (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
    except Exception:
        return -1, False


def _pick_asset(tag: str, dest_dir: Path, local: str = "") -> Path:
    """下载主包到本地，返回文件路径。优先 tar（可边下边解）。"""
    if local:
        p = Path(local)
        if not p.is_file():
            raise SystemExit(f"本地文件不存在：{p}")
        log(f"  使用本地包：{p.name}")
        return p

    names = [f"forgeform-{tag}.tar", f"forgeform-{tag}.zip"]
    for name in names:
        for url in mirror_urls(f"releases/download/{tag}/{name}"):
            size, ranges = _head(url)
            if size <= 0:
                continue
            host = url.split("/")[2]
            log(f"  源：{host} / {name}（{size/1024/1024:.1f} MB，range={ranges}）")
            out = dest_dir / name
            if out.is_file() and out.stat().st_size == size:
                log("  已存在且大小一致，直接用")
                return out
            _download(url, out, size, ranges, conns=int(os.environ.get("FF_CONNS", "8")))
            return out
    raise SystemExit("所有镜像都拿不到主包（检查网络或换时间重试）")


def _download(url: str, dest: Path, size: int, ranges: bool, conns: int = 8) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists() and tmp.stat().st_size != size:
        tmp.unlink()
    if not tmp.exists():
        tmp.write_bytes(b"")

    done = [0]
    lock = threading.Lock()
    t0 = time.time()

    def worker(start: int, end: int) -> None:
        headers = {"Range": f"bytes={start}-{end}"} if end > 0 else {}
        pos = start
        for attempt in range(3):
            try:
                with _req(url, headers=headers, stream=True, timeout=(20, 120)) as r:
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code}")
                    with open(tmp, "r+b") as f:
                        f.seek(pos)
                        for chunk in r.iter_content(CHUNK):
                            if not chunk:
                                continue
                            f.write(chunk)
                            pos += len(chunk)
                            with lock:
                                done[0] += len(chunk)
                            if end > 0 and pos > end:
                                return
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.5)

    if ranges and conns > 1 and size > 16 * CHUNK:
        step = size // conns
        spans = [(i * step, size - 1 if i == conns - 1 else (i + 1) * step - 1)
                 for i in range(conns)]
        threads = [threading.Thread(target=worker, args=(s, e), daemon=True) for s, e in spans]
    else:
        threads = [threading.Thread(target=worker, args=(0, -1), daemon=True)]

    for t in threads:
        t.start()

    last = 0.0
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
        now = time.time()
        if now - last > 1.0:
            last = now
            mb = done[0] / 1024 / 1024
            speed = mb / max(now - t0, 0.1)
            pct = (done[0] / size * 100) if size else 0
            sys.stdout.write(f"\r  下载 {mb:7.1f} / {size/1024/1024:.1f} MB  {speed:5.1f} MB/s  {pct:5.1f}%")
            sys.stdout.flush()

    for t in threads:
        t.join()
    sys.stdout.write("\n")
    got = tmp.stat().st_size
    if size and got != size:
        raise SystemExit(f"下载字节不符：{got:,} / {size:,}")
    os.replace(tmp, dest)
    log(f"  ✓ 下载完成：{dest.name}（{(time.time()-t0):.0f}s）")


def _stream_tar(url: str, size: int, dest: Path) -> Path:
    """边下边解：下载端写进管道，tar 端顺序读取并解压。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    r_fd, w_fd = os.pipe()
    err: list[BaseException] = []
    done = [0]
    t0 = time.time()

    def pump() -> None:
        try:
            with _req(url, stream=True, timeout=(20, 180)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                with os.fdopen(w_fd, "wb", closefd=True) as w:
                    for chunk in r.iter_content(CHUNK):
                        if not chunk:
                            continue
                        w.write(chunk)
                        done[0] += len(chunk)
                        pct = (done[0] / size * 100) if size else 0
                        mb = done[0] / 1024 / 1024
                        sp = mb / max(time.time() - t0, 0.1)
                        sys.stdout.write(f"\r  下载+解压 {mb:7.1f} / {size/1024/1024:.1f} MB  "
                                         f"{sp:5.1f} MB/s  {pct:5.1f}%")
                        sys.stdout.flush()
        except BaseException as e:  # 传回主线程
            err.append(e)
            try:
                os.close(w_fd)
            except Exception:
                pass

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    try:
        with os.fdopen(r_fd, "rb", closefd=True) as rp:
            with tarfile.open(fileobj=rp, mode="r|*") as tf:
                tf.extractall(dest)
    except BaseException:
        th.join(timeout=1)
        raise
    th.join()
    sys.stdout.write("\n")
    if err:
        raise err[0]
    kids = [q for q in dest.iterdir() if q.is_dir()]
    return next((q for q in kids if (q / "VERSION").is_file()), kids[0] if kids else dest)


def _extract(pkg: Path, dest: Path) -> Path:
    """解压；tar 走流式（边读边写），zip 直接解。返回项目根目录。"""
    dest.mkdir(parents=True, exist_ok=True)
    if pkg.suffix == ".tar":
        log("  解压（tar，顺序读取）…")
        with tarfile.open(pkg, "r:*") as tf:
            tf.extractall(dest)
    else:
        log("  解压（zip）…")
        with zipfile.ZipFile(pkg) as z:
            z.extractall(dest)
    kids = [p for p in dest.iterdir() if p.is_dir()]
    root = next((p for p in kids if (p / "VERSION").is_file()), kids[0] if kids else dest)
    return root


def install(tag: str, dest: Path, local: str = "") -> int:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not tag:
        tag = resolve_tag()
        if not tag:
            raise SystemExit("解析不到最新版本（网络问题），请稍后重试")
    log(f"  目标版本：{tag}")
    if not local:
        # tar 可以边下边解（用户要的就是这个）；失败再退回"下完再解"
        for url in mirror_urls(f"releases/download/{tag}/forgeform-{tag}.tar"):
            size, _ranges = _head(url)
            if size <= 0:
                continue
            log(f"  源：{url.split('/')[2]} / tar {size/1024/1024:.1f} MB → 边下边解")
            try:
                root = _stream_tar(url, size, dest)
                log(f"  ✓ 已解压到：{root}")
                print("RESULT_ROOT=" + str(root), flush=True)
                return 0
            except Exception as e:
                log(f"  ✗ 流式失败（{type(e).__name__}），改用下载后解压")
                break
    pkg = _pick_asset(tag, dest, local)
    root = _extract(pkg, dest)
    log(f"  ✓ 已解压到：{root}")
    print("RESULT_ROOT=" + str(root), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["plan", "install", "tag"])
    ap.add_argument("--tag", default="")
    ap.add_argument("--dest", default=r"D:\\ForgeForm")
    ap.add_argument("--local", default="")
    ns = ap.parse_args()

    if ns.action == "plan":
        print(json.dumps(probe(), ensure_ascii=False))
        return 0
    if ns.action == "tag":
        print(resolve_tag())
        return 0
    return install(ns.tag, Path(ns.dest), ns.local)


if __name__ == "__main__":
    raise SystemExit(main())
