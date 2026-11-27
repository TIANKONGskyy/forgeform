# -*- coding: utf-8 -*-
"""一键体检 + 问题上报（2026-11-27）。

开源项目最常见的两类求助：
1. 「装不上 / 跑不起来」——需要知道缺什么，而不是一堆 traceback
2. 「出错了」——需要环境快照 + 日志尾部，但要**先脱敏**

两个入口：
    python tools/doctor.py            # 体检，缺什么 + 怎么修
    python tools/doctor.py --fix      # 能自动修的自动修（建目录、补配置、装缺失包按提示）
    python tools/report.py            # 生成诊断包（zip，已脱敏），可直接贴 issue

诊断包内容：
    environment.json   系统/Python/GPU/依赖版本/端口/磁盘
    checks.json        doctor 的体检结果
    config.redacted.json   配置（密钥、路径用户名已替换）
    logs/*.log         最近 2 天的日志尾部（每份最多 400 行）
    README.txt         告诉用户这个包能贴到哪
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────── 体检 ───────────────────────

def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def check_python() -> dict:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return {"name": "Python 版本", "ok": ok, "detail": platform.python_version(),
            "fix": "" if ok else "需要 3.10+（推荐 3.12）；conda/uv 建环境后再装"}


def check_deps() -> dict:
    need = {"gradio": "界面", "trimesh": "网格", "torch": "推理",
            "numpy": "数值", "requests": "网络", "PIL": "图像"}
    missing = []
    for mod in need:
        try:
            __import__(mod if mod != "PIL" else "PIL")
        except Exception:
            missing.append(mod)
    return {"name": "Python 依赖", "ok": not missing,
            "detail": "缺 " + ", ".join(missing) if missing else "齐全",
            "fix": "" if not missing else "pip install -r requirements.txt"}


def check_gpu() -> dict:
    code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                      "--format=csv,noheader"])
    if code != 0:
        return {"name": "GPU", "ok": False, "detail": "没检测到 NVIDIA GPU",
                "fix": "没有 GPU 也能用出图/建模的云端后端（config.json 里改 backend）"}
    first = out.strip().splitlines()[0]
    return {"name": "GPU", "ok": True, "detail": first, "fix": ""}


def check_torch_cuda() -> dict:
    try:
        import torch
        ok = torch.cuda.is_available()
        return {"name": "Torch CUDA", "ok": ok,
                "detail": f"torch {torch.__version__} · cuda {ok} · "
                          f"{torch.cuda.get_device_name(0) if ok else '无'}",
                "fix": "" if ok else "装 CUDA 版 torch（不要装 cpu 版）"}
    except Exception as e:
        return {"name": "Torch CUDA", "ok": False, "detail": str(e)[:120],
                "fix": "pip install torch --index-url https://download.pytorch.org/whl/cu121"}


def check_comfy() -> dict:
    try:
        import requests
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        raw = ((cfg.get("paths") or {}).get("comfyui") or "").strip()
        # config.example.json 里写的是 ${COMFYUI_DIR} 这类占位符 —— 不是路径，
        # 直接报"目录不存在"会让新手以为配置错了（2026-11-28 实测装完第一眼就是这个）。
        if "${" in raw or raw.startswith("%"):
            return {"name": "ComfyUI", "ok": False,
                    "detail": "还没配置（config 里是占位符）",
                    "fix": "跑 python tools/bootstrap_models.py --only core 自动装好并写配置"}
        # config 里的 `paths.comfyui` 在本机写的是**安装目录**（D:\ComfyUI-New），
        # 不是 URL —— 直接拼 /system_stats 会报 InvalidSchema（2026-11-27 实测）。
        # 是目录就顺带检查 ComfyUI 本体在不在，地址用默认端口。
        if raw.startswith("http"):
            url = raw.rstrip("/")
        else:
            url = f"http://127.0.0.1:{os.environ.get('FORGEFORM_COMFY_PORT', '8188')}"
            if raw and not Path(raw).expanduser().is_dir():
                return {"name": "ComfyUI", "ok": False,
                        "detail": f"配置的目录不存在：{raw}",
                        "fix": "改 config.json → paths.comfyui 为 ComfyUI 安装目录或 URL"}
        r = requests.get(f"{url}/system_stats", timeout=5)
        ver = ""
        try:
            ver = json.loads(r.content).get("system", {}).get("comfyui_version", "")
        except Exception:
            pass
        return {"name": "ComfyUI", "ok": r.status_code == 200,
                "detail": f"{url}" + (f" · v{ver}" if ver else ""), "fix": ""}
    except Exception as e:
        return {"name": "ComfyUI", "ok": False, "detail": f"{type(e).__name__}: {e}"[:120],
                "fix": "先启动 ComfyUI（仓库不自带，需单独装）"}


def check_config() -> dict:
    p = ROOT / "config.json"
    if not p.is_file():
        return {"name": "配置文件", "ok": False, "detail": "缺 config.json",
                "fix": "复制 config.example.json 为 config.json"}
    try:
        json.loads(p.read_text(encoding="utf-8-sig"))
        return {"name": "配置文件", "ok": True, "detail": "可解析", "fix": ""}
    except Exception as e:
        return {"name": "配置文件", "ok": False, "detail": str(e)[:120],
                "fix": "改坏了就删掉重新复制 config.example.json"}


def check_dirs() -> dict:
    need = ["outputs", "logs", "projects", "temp"]
    missing = [d for d in need if not (ROOT / d).is_dir()]
    return {"name": "运行目录", "ok": not missing,
            "detail": "缺 " + ", ".join(missing) if missing else "齐全",
            "fix": "python tools/doctor.py --fix 自动建"}


def check_disk() -> dict:
    try:
        import shutil as sh
        free = sh.disk_usage(str(ROOT)).free
        ok = free > 2 * 1024 ** 3
        return {"name": "磁盘空间", "ok": ok,
                "detail": f"剩 {free / 1024 ** 3:.1f} GB",
                "fix": "" if ok else "至少留 2 GB（建模临时文件很占地方）"}
    except Exception as e:
        return {"name": "磁盘空间", "ok": False, "detail": str(e)[:80], "fix": ""}


def check_weights() -> dict:
    """模型权重：不是必须（可以只当对话/出图前端用），只提示。"""
    cfg_p = ROOT / "config.json"
    found = []
    try:
        cfg = json.loads(cfg_p.read_text(encoding="utf-8-sig"))
        for key, m in ((cfg.get("models") or {}).get("3d") or {}).items():
            path = (m or {}).get("path") or ""
            if path and not path.startswith(("http", "{")):
                found.append((key, Path(path).is_dir()))
    except Exception:
        pass
    if not found:
        return {"name": "3D 权重", "ok": True, "detail": "未配置本地权重（可用云端后端）", "fix": ""}
    have = [k for k, ok in found if ok]
    return {"name": "3D 权重", "ok": bool(have),
            "detail": f"{len(have)}/{len(found)} 已就位",
            "fix": "" if have else "在 Studio「模型」页下载，或用 hy3dgen 自动下载"}


def check_ports() -> dict:
    ports = {"界面 7860": 7860, "Studio 7861": 7861, "ComfyUI 8188": 8188}
    import socket
    busy = []
    for name, p in ports.items():
        s = socket.socket()
        s.settimeout(0.3)
        try:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                busy.append(name)
        finally:
            s.close()
    return {"name": "端口占用", "ok": True,
            "detail": ("已占用: " + ", ".join(busy)) if busy else "都空闲", "fix": ""}


CHECKS = [check_python, check_deps, check_gpu, check_torch_cuda, check_comfy,
          check_config, check_dirs, check_disk, check_weights, check_ports]


def run_checks() -> list[dict]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:
            out.append({"name": fn.__name__, "ok": False, "detail": f"检查本身出错: {e}",
                        "fix": ""})
    return out


def apply_fixes(rows: list[dict]) -> list[str]:
    """能自动修的：建目录、补配置。其余只打印建议。"""
    done = []
    for d in ("outputs", "logs", "projects", "temp"):
        p = ROOT / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            done.append(f"建目录 {d}/")
    if not (ROOT / "config.json").is_file() and (ROOT / "config.example.json").is_file():
        shutil.copy2(ROOT / "config.example.json", ROOT / "config.json")
        done.append("从 config.example.json 生成 config.json")
    return done


# ─────────────────────── 脱敏 ───────────────────────

_SECRET_KEYS = re.compile(r"(key|token|secret|password|passwd|api[-_]?key)", re.I)


def redact(obj, depth: int = 0):
    """递归脱敏：密钥类字段打码，长 base64/URL 里的 token 打码，家目录换 ~。"""
    home = str(Path.home())
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)) and isinstance(v, str) and v:
                out[k] = f"<redacted:{len(v)}>"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(x, depth + 1) for x in obj]
    if isinstance(obj, str):
        s = obj.replace(home, "~")
        s = re.sub(r"(sk-|zk_live_|ghp_)[A-Za-z0-9_\-]{8,}", r"\1<redacted>", s)
        s = re.sub(r"(token=)[^&\s]+", r"\1<redacted>", s)
        return s
    return obj


def environment() -> dict:
    env = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "project": str(ROOT),
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        if (ROOT / "VERSION").is_file() else "unknown",
    }
    try:
        import torch
        env["torch"] = {"version": torch.__version__, "cuda": torch.cuda.is_available(),
                        "cuda_version": getattr(torch.version, "cuda", None)}
    except Exception:
        env["torch"] = "missing"
    code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,temperature.gpu",
                      "--format=csv,noheader"], timeout=10)
    env["nvidia_smi"] = out.strip() if code == 0 else f"unavailable ({code})"
    try:
        import gradio
        env["gradio"] = gradio.__version__
    except Exception:
        env["gradio"] = "missing"
    return redact(env)


def recent_logs(max_files: int = 8, tail: int = 400) -> dict:
    out = {}
    logdir = ROOT / "logs"
    if not logdir.is_dir():
        return out
    files = sorted(logdir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
            out[f.name] = "\n".join(redact(ln) for ln in lines)
        except Exception as e:
            out[f.name] = f"<读取失败: {e}>"
    return out


def build_report(out_dir: str | Path | None = None) -> Path:
    """生成诊断包 zip，返回路径。"""
    out = Path(out_dir) if out_dir else ROOT / "outputs" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = out / f"_report_{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)

    rows = run_checks()
    (tmp / "environment.json").write_text(
        json.dumps(environment(), ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp / "checks.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = ROOT / "config.json"
    if cfg.is_file():
        try:
            (tmp / "config.redacted.json").write_text(
                json.dumps(redact(json.loads(cfg.read_text(encoding="utf-8-sig"))),
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            (tmp / "config.redacted.json").write_text(f"<解析失败: {e}>", encoding="utf-8")
    logs = recent_logs()
    if logs:
        (tmp / "logs").mkdir(exist_ok=True)
        for name, text in logs.items():
            (tmp / "logs" / name).write_text(text, encoding="utf-8")

    (tmp / "README.txt").write_text(
        "铸想 ForgeGen 诊断包\n"
        "=====================\n"
        "这个包用于报问题。已自动脱敏：\n"
        "  - 配置里的 key/token/secret/password 字段全部打码\n"
        "  - 家目录路径替换成 ~\n"
        "  - 日志里 sk- / ghp_ / zk_live_ 开头的串会被替换\n\n"
        "怎么用：\n"
        "  1. 打开 https://github.com/TIANKONGskyy/forgeform/issues\n"
        "  2. 新建 issue，选「问题上报」模板\n"
        "  3. 把 environment.json 的「平台/Python/GPU」三行贴进去，诊断包作为附件\n\n"
        "仍然不放心的话，先自己打开 zip 看一遍再发。\n",
        encoding="utf-8")

    zip_path = out / f"forgeform-report-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in tmp.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp))
    shutil.rmtree(tmp, ignore_errors=True)
    return zip_path


# ─────────────────────── CLI ───────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="铸想一键体检 / 诊断包")
    ap.add_argument("--fix", action="store_true", help="自动修能修的（建目录、补配置）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--report", action="store_true", help="生成诊断包 zip")
    ns = ap.parse_args(argv)

    if ns.report:
        print(f"诊断包: {build_report()}", flush=True)
        return 0

    rows = run_checks()
    if ns.fix:
        fixed = apply_fixes(rows)
        if fixed:
            print("已自动修复：", flush=True)
            for f in fixed:
                print(f"  ✓ {f}", flush=True)
            rows = run_checks()
    if ns.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    bad = [r for r in rows if not r["ok"]]
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        print(f"  {mark} {r['name']:<10} {r['detail']}")
        if not r["ok"] and r.get("fix"):
            print(f"      → {r['fix']}")
    print(f"\n  {len(rows) - len(bad)}/{len(rows)} 通过", flush=True)
    if bad:
        print("  有问题就跑：python tools/report.py   生成诊断包（已脱敏）", flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
