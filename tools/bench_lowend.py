# -*- coding: utf-8 -*-
"""在低配限制下跑完整流程，看"8G 显存 / 16G 内存的笔记本"实际要多久。

    python tools/bench_lowend.py --vram 6 --ram 10

两件事：
  · 显存：torch.cuda.set_per_process_memory_fraction(6/32) —— 精确，超了直接 OOM
  · 内存：Windows Job Object 设进程工作集上限 —— 超了分配失败

这两个限制**只作用于当前进程**，不影响机器上别的服务。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "做一个六角法兰联轴器，中心有轴孔和六个螺栓孔"


# ── 显存限制 ────────────────────────────────────────────────
def limit_vram(gb: float, total_gb: float = 32.0) -> float:
    import torch
    frac = max(0.01, min(1.0, gb / total_gb))
    torch.cuda.set_per_process_memory_fraction(frac, 0)
    return frac


# ── 内存限制（Windows Job Object）────────────────────────────
def limit_ram(gb: float) -> bool:
    """给当前进程的工作集设上限。返回是否成功。"""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须显式声明签名 —— 默认 restype 是 c_int，GetCurrentProcess 返回的是
    # pseudo-handle(-1) 这种"大整数"，在不声明时会因为宽度不对报 OverflowError。
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JobObjectExtendedLimitInformation = 9

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = int(gb * 1024 ** 3)
    ok = kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        return False
    # 把自己加进这个 job
    handle = kernel32.GetCurrentProcess()
    return bool(kernel32.AssignProcessToJobObject(job, handle))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vram", type=float, default=6.0)
    ap.add_argument("--ram", type=float, default=10.0)
    ap.add_argument("--total-vram", type=float, default=32.0)
    ap.add_argument("--home", default=str(ROOT))
    ap.add_argument("--image-model", default="z_image")
    ap.add_argument("--mesh-model", default="hunyuan21")
    ap.add_argument("--scale", type=float, default=0.5, help="分辨率缩放（0.5 = 512²）")
    args = ap.parse_args()

    home = Path(args.home).resolve()
    sys.path.insert(0, str(home))
    os.chdir(home)

    print("=" * 70)
    print(f"低配实测：显存 {args.vram} GB / 内存 {args.ram} GB")
    print("=" * 70)

    # 限制必须在 import torch 之后、分配模型之前设好
    import torch
    frac = limit_vram(args.vram, args.total_vram)
    print(f"显存限制: {frac*100:.1f}% of {args.total_vram} GB = {args.vram} GB")
    ok_ram = limit_ram(args.ram)
    print(f"内存限制: {'已设 ' + str(args.ram) + ' GB' if ok_ram else '设置失败（继续，仅靠显存限制）'}")

    import psutil
    me = psutil.Process(os.getpid())

    def rss() -> float:
        return me.memory_info().rss / 1024 ** 3

    cfg = home / "config.json"
    if not cfg.exists():
        print("✗ 没有 config.json，先跑一次 app.py 或 setup.py")
        return 1

    from backend.pipeline import Pipeline
    pipe = Pipeline(str(cfg))

    results = {"limits": {"vram_gb": args.vram, "ram_gb": args.ram},
               "steps": {}}
    t_all = time.time()

    def mark(key: str, ok: bool, sec: float, detail: str = "") -> None:
        results["steps"][key] = {"ok": ok, "sec": round(sec, 1),
                                 "rss_gb": round(rss(), 2), "detail": detail}
        print(f"  {'✓' if ok else '✗'} {key:<10} {sec:>7.1f}s  RSS {rss():.2f} GB  {detail}",
              flush=True)

    # ── 1 对话（不占多少显存，但要能解析需求）──
    print("\n[1] 对话")
    t0 = time.time()
    try:
        from backend import agent
        state = {"phase": "describing", "requirement": "", "auto_params": {},
                 "conversation": [], "candidates": [], "selected": "",
                 "glb_path": "", "stl_path": ""}
        plan = agent.plan(PROMPT, state, timeout=180)
        mark("chat", bool(plan), time.time() - t0,
             f"动作={[a.get('name') for a in (plan or {}).get('actions', [])]}")
    except Exception as exc:
        mark("chat", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:70])

    # ── 2 出图 ──
    print("\n[2] 出图")
    t0 = time.time()
    img = None
    try:
        res = pipe.generate_images(PROMPT, count=1, model_key=args.image_model,
                                   quality_name="标准", resolution="1024×1024",
                                   skip_cache=True)
        if res and getattr(res[0], "success", False):
            img = Path(res[0].output_path)
            mark("image", True, time.time() - t0, f"{img.name} {img.stat().st_size//1024} KB")
        else:
            mark("image", False, time.time() - t0, str(getattr(res[0], "error", "?"))[:70] if res else "无返回")
    except Exception as exc:
        mark("image", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])

    # ── 3 建模 ──
    print("\n[3] 建模")
    t0 = time.time()
    glb = None
    if img:
        try:
            r = pipe.generate_3d(model_key=args.mesh_model, quality_name="快速",
                                 image_path=str(img))
            if getattr(r, "success", False):
                glb = Path(r.output_path)
                mark("mesh", True, time.time() - t0, f"{glb.name} {glb.stat().st_size//1024} KB")
            else:
                mark("mesh", False, time.time() - t0, str(getattr(r, "error", "?"))[:70])
        except Exception as exc:
            mark("mesh", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])
    else:
        mark("mesh", False, 0, "跳过（出图失败）")

    # ── 4 STL ──
    print("\n[4] STL")
    t0 = time.time()
    if glb:
        try:
            s = pipe.export_stl(str(glb), target_size_mm=80)
            if getattr(s, "success", False):
                p = Path(s.output_path)
                import trimesh
                m = trimesh.load(str(p), force="mesh")
                mark("stl", bool(m.is_watertight), time.time() - t0,
                     f"水密={bool(m.is_watertight)} {p.stat().st_size//1024} KB")
            else:
                mark("stl", False, time.time() - t0, str(getattr(s, "error", "?"))[:70])
        except Exception as exc:
            mark("stl", False, time.time() - t0, f"{type(exc).__name__}: {exc}"[:90])
    else:
        mark("stl", False, 0, "跳过（建模失败）")

    total = time.time() - t_all
    print("\n" + "=" * 70)
    ok_n = sum(1 for s in results["steps"].values() if s["ok"])
    print(f"通过 {ok_n}/{len(results['steps'])} · 总耗时 {total/60:.1f} 分钟 · "
          f"峰值 RSS {max(s['rss_gb'] for s in results['steps'].values()):.2f} GB")
    print("=" * 70)

    out = home / "outputs" / "bench_lowend.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    results["total_sec"] = round(total, 1)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据: {out}")
    return 0 if ok_n == len(results["steps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
