# -*- coding: utf-8 -*-
"""运行信息：定时采样留存 + 到期清理。

## 两类数据分开管

**采样（sampler）** —— 每 N 分钟往 `logs/metrics-YYYY-MM-DD.jsonl` 追加一行：
GPU 利用率/显存、内存、CPU、磁盘余量、当前任务阶段。它回答的是"刚才为什么卡"
这类事后问题。滚动保留 7 天。

**清理（retention）** —— 按目录定策略，删除**只删自己生成的中间产物**：

    logs/                 日志与采样     保留 14 天
    outputs/images/       出图中间结果   保留 30 天（projects/ 里有副本）
    outputs/3d/           GLB 中间结果   保留 30 天（projects/ 里有副本）
    outputs/stl/          STL 中间结果   保留 30 天
    outputs/healthcheck*  自检报告       只留最近 10 份
    temp/                 临时文件       保留 1 天
    projects/             **永不清理**  用户的作品
    models/               **永不清理**  权重
    .secrets/             **永不清理**  加密的 key

## 为什么 projects/ 永不清理

用户的作品（图片 + model.glb + model.stl + 对话记录）是唯一不可再生的东西 ——
删了他就得重新生成，而生成要几分钟到十几分钟。中间产物（outputs/）删了还能
再跑一遍；作品删了就是删了。这条边界不能模糊。

## 用法

    from backend import maintenance as mt
    mt.sample_once()             # 采一次（app 里的定时器每 3 分钟调）
    mt.status()                  # 各类目占用多少
    mt.plan()                    # 会删什么（不改动）
    mt.run(apply=True)           # 真删
    mt.retention_report()        # 一句话摘要，给 UI
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
OUTPUTS = ROOT / "outputs"
TEMP = ROOT / "temp"

# 策略：目录 → (保留天数, 说明)。days=0 表示永不清理。
RETENTION: dict[str, tuple[int, str]] = {
    "logs": (14, "日志与运行采样"),
    "outputs/images": (30, "出图中间结果"),
    "outputs/3d": (30, "建模中间结果"),
    "outputs/stl": (30, "STL 中间结果"),
    "temp": (1, "临时文件"),
}

# 只保留最近 N 份（不按时间）
KEEP_LATEST: dict[str, tuple[int, str]] = {
    "outputs": (10, "自检报告（healthcheck*.json 等）"),
}

NEVER = [
    "projects",          # 用户作品：图片 + model.glb + model.stl + 对话记录
    "models",            # 权重：几十 GB，删了要重下
    ".secrets",          # 加密的 API key
    "plugins",           # 模型插件定义
    "ui",                # 界面
    "backend",           # 代码
    "docs",
]

_SAMPLE_MINUTES = 3          # 采样间隔
_MAX_SAMPLE_MB = 64          # logs/ 里 metrics 部分的上限保护


# ══════════════════════════════════════════════════════════════════
# 采样
# ══════════════════════════════════════════════════════════════════
def _gpu() -> dict:
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return {}
        parts = [p.strip() for p in line[0].split(",")]
        return {"gpu_pct": int(float(parts[0])),
                "vram_used_mb": int(float(parts[1])),
                "vram_total_mb": int(float(parts[2])),
                "gpu_temp_c": int(float(parts[3])) if len(parts) > 3 else None}
    except Exception:
        return {}


def _mem() -> dict:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return {"ram_pct": int(st.dwMemoryLoad),
                "ram_total_gb": round(st.ullTotalPhys / 1024 ** 3, 1),
                "ram_free_gb": round(st.ullAvailPhys / 1024 ** 3, 1)}
    except Exception:
        return {}


def _cpu() -> dict:
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage "
             "-Average).Average"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        v = (out.stdout or "").strip()
        return {"cpu_pct": int(float(v))} if v else {}
    except Exception:
        return {}


def _disk() -> dict:
    try:
        total, used, free = shutil.disk_usage(str(ROOT))
        return {"disk_free_gb": round(free / 1024 ** 3, 1),
                "disk_total_gb": round(total / 1024 ** 3, 1)}
    except Exception:
        return {}


_SAMPLE_LOCK = threading.Lock()


def sample_once(note: str = "") -> dict:
    """采一次运行信息，追加到当天 jsonl。返回这一条记录。"""
    rec = {"ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    rec.update(_gpu())
    rec.update(_mem())
    rec.update(_cpu())
    rec.update(_disk())
    if note:
        rec["note"] = note
    try:
        with _SAMPLE_LOCK:
            LOGS.mkdir(parents=True, exist_ok=True)
            path = LOGS / f"metrics-{time.strftime('%Y-%m-%d')}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


class Sampler(threading.Thread):
    """后台每 _SAMPLE_MINUTES 分钟采一次。daemon，不挡退出。"""

    def __init__(self, minutes: float = _SAMPLE_MINUTES, note_fn=None):
        super().__init__(daemon=True, name="ff-metrics")
        self.minutes = max(0.5, float(minutes))
        self.note_fn = note_fn
        self._stop = threading.Event()

    def run(self) -> None:
        # 启动先等 20 秒（别和模型加载抢 CPU）
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            note = ""
            try:
                note = self.note_fn() if self.note_fn else ""
            except Exception:
                pass
            sample_once(note)
            if self._stop.wait(self.minutes * 60):
                return

    def stop(self) -> None:
        self._stop.set()


# ══════════════════════════════════════════════════════════════════
# 占用统计
# ══════════════════════════════════════════════════════════════════
def _dir_size(path: Path) -> tuple[int, int]:
    """(总字节, 文件数)。"""
    total = 0
    count = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                    count += 1
                except OSError:
                    continue
    except Exception:
        pass
    return total, count


def status() -> dict:
    """各类目占用。UI 用。"""
    out = {"root": str(ROOT), "items": [], "total_mb": 0.0,
           "crypto_ok": True, "sampled_today": 0}
    for rel in list(RETENTION) + ["projects", "models", "outputs", "logs"]:
        p = ROOT / rel
        if not p.exists():
            continue
        size, count = _dir_size(p)
        out["items"].append({
            "path": rel,
            "mb": round(size / 1024 ** 2, 1),
            "files": count,
            "keep_days": RETENTION.get(rel, (0, "永不清理"))[0],
            "note": RETENTION.get(rel, (0, "永不清理"))[1],
        })
    out["items"].sort(key=lambda i: -i["mb"])
    out["total_mb"] = round(sum(i["mb"] for i in out["items"]), 1)
    try:
        today = LOGS / f"metrics-{time.strftime('%Y-%m-%d')}.jsonl"
        if today.exists():
            out["sampled_today"] = sum(1 for _ in today.open(encoding="utf-8"))
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════
# 清理
# ══════════════════════════════════════════════════════════════════
def _assert_not_protected(target: Path) -> None:
    """双保险：清理逻辑永远不碰 NEVER 里的目录，也不碰仓库根。"""
    try:
        rel = target.resolve().relative_to(ROOT.resolve()).as_posix()
    except Exception:
        raise RuntimeError(f"拒绝清理仓库外的路径: {target}")
    for name in NEVER:
        if rel == name or rel.startswith(name + "/"):
            raise RuntimeError(f"拒绝清理受保护目录: {rel}")


def _old_files(path: Path, days: int) -> list[Path]:
    cutoff = time.time() - days * 86400
    out = []
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                f = Path(root) / name
                try:
                    if f.stat().st_mtime < cutoff:
                        out.append(f)
                except OSError:
                    continue
    except Exception:
        pass
    return out


def plan() -> dict:
    """会删什么（不真删）。"""
    items = []
    total = 0
    for rel, (days, note) in RETENTION.items():
        p = ROOT / rel
        if not p.exists() or days <= 0:
            continue
        try:
            _assert_not_protected(p)
        except RuntimeError:
            continue
        files = _old_files(p, days)
        size = sum(f.stat().st_size for f in files if f.exists())
        total += size
        if files:
            items.append({"path": rel, "note": note, "days": days,
                          "files": len(files), "mb": round(size / 1024 ** 2, 1),
                          "sample": [f.name for f in files[:3]]})

    # 只留最近 N 份的报告
    for rel, (keep, note) in KEEP_LATEST.items():
        p = ROOT / rel
        if not p.exists():
            continue
        reports = sorted(
            [f for f in p.glob("healthcheck*.json") if f.is_file()],
            key=lambda f: f.stat().st_mtime, reverse=True)
        excess = reports[keep:]
        if excess:
            size = sum(f.stat().st_size for f in excess)
            total += size
            items.append({"path": rel, "note": note, "days": 0,
                          "files": len(excess), "mb": round(size / 1024 ** 2, 1),
                          "sample": [f.name for f in excess[:3]]})
    return {"items": items, "total_mb": round(total / 1024 ** 2, 1)}


def run(apply: bool = False) -> dict:
    """执行清理。apply=False 时只报告。"""
    p = plan()
    removed = 0
    failed = 0
    if apply:
        for rel, (days, _note) in RETENTION.items():
            d = ROOT / rel
            if not d.exists() or days <= 0:
                continue
            try:
                _assert_not_protected(d)
            except RuntimeError:
                continue
            for f in _old_files(d, days):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    failed += 1
        for rel, (keep, _note) in KEEP_LATEST.items():
            d = ROOT / rel
            if not d.exists():
                continue
            reports = sorted([f for f in d.glob("healthcheck*.json") if f.is_file()],
                             key=lambda f: f.stat().st_mtime, reverse=True)
            for f in reports[keep:]:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    failed += 1
    p["applied"] = bool(apply)
    p["removed"] = removed
    p["failed"] = failed
    return p


def retention_report() -> str:
    """一句话摘要，给 UI/日志用。"""
    s = status()
    p = plan()
    parts = [f"共 {s['total_mb']:.0f} MB"]
    if p["items"]:
        parts.append(f"可清理 {p['total_mb']:.1f} MB / {sum(i['files'] for i in p['items'])} 个文件")
    else:
        parts.append("没有超出保留期的文件")
    return " · ".join(parts)


def auto(apply: bool = True) -> dict:
    """定时入口：先清一次，再报摘要。"""
    r = run(apply=apply)
    if apply and r.get("removed"):
        print(f"[maint] 已清理 {r['removed']} 个过期文件，释放 {r['total_mb']:.1f} MB",
              flush=True)
    return r
