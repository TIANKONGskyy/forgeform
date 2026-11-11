"""Strata VRAM 策略：full（满缓存）/ 24g（24GB，留上下文）/ shared（19GB）/ auto / stop / status。

用法：
  python tools/strata_policy.py status
  python tools/strata_policy.py full      # 满缓存，~32GB，~34 tok/s（上下文余量小）
  python tools/strata_policy.py 24g       # 24GB，~8GB 上下文，~20–29 tok/s（默认）
  python tools/strata_policy.py shared    # 19GB，~28 tok/s，给 ComfyUI 留 ~12GB
  python tools/strata_policy.py auto      # 没跑就按当前显存占用选一个；跑着就只报状态
  python tools/strata_policy.py stop
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

ROOT = Path(r"D:\Strata-data\terminal\0.1.47")
PY = ROOT / "python" / "python.exe"
SRV = ROOT / "serve" / "server.py"
FULL = ROOT / "strata-iq3_s.json"
MID = ROOT / "strata-iq3_s_24g.json"
SHARED = ROOT / "strata-iq3_s_shared.json"
PORT = 8091


def used_mib() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            timeout=10, text=True)
        return int(out.strip().splitlines()[0])
    except Exception:
        return 0


def alive() -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{PORT}/v1/models", timeout=4)
        return r.ok
    except Exception:
        return False


def reserve(cfg: Path) -> int:
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
        a = d["args"]
        return int(a[a.index("--vram-reserve-mib") + 1])
    except Exception:
        return -1


def listener_pids() -> set[int]:
    pids = set()
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == PORT and c.status == "LISTEN" and c.pid:
            pids.add(c.pid)
    return pids


def stop() -> None:
    for pid in listener_pids():
        try:
            p = psutil.Process(pid)
            for ch in p.children(recursive=True):
                try:
                    ch.terminate()
                except Exception:
                    pass
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + 20
    while time.time() < deadline and listener_pids():
        time.sleep(0.5)
    for pid in listener_pids():
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass


def free_comfy() -> None:
    """让 ComfyUI 卸掉常驻模型，否则它占着 10GB+，Strata 19GB 缓存建不起来。"""
    try:
        requests.post("http://127.0.0.1:8188/free",
                      json={"unload_models": True, "free_memory": True}, timeout=15)
        time.sleep(2)
    except Exception:
        pass


def start(cfg: Path, timeout: int = 240) -> bool:
    print(f"starting {cfg.name} (reserve {reserve(cfg)} MiB) ...", flush=True)
    free_comfy()
    log = open(ROOT / "strata-serve.log", "ab")
    flags = 0x00000008 | 0x00000200
    subprocess.Popen([str(PY), str(SRV), "--engine", "strata", "--config", str(cfg),
                      "--port", str(PORT)], cwd=str(ROOT), stdout=log,
                     stderr=subprocess.STDOUT, creationflags=flags)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if alive():
            print(f"ready, gpu used {used_mib()} MiB", flush=True)
            return True
        time.sleep(2)
    print("startup timeout", flush=True)
    return False


def status() -> None:
    up = alive()
    u = used_mib()
    est = "full" if u > 29000 else ("mid" if u > 20000 else ("shared" if u > 16000 else "down/idle"))
    print(json.dumps({
        "running": up,
        "gpu_used_mib": u,
        "estimate": est,
        "full_reserve_mib": reserve(FULL),
        "mid_reserve_mib": reserve(MID),
        "shared_reserve_mib": reserve(SHARED),
        "pids": sorted(listener_pids()),
    }, ensure_ascii=False, indent=2))


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "stop":
        stop()
        print("stopped")
    elif cmd in ("full", "shared", "mid", "24g"):
        cfg = {"full": FULL, "shared": SHARED, "mid": MID, "24g": MID}[cmd]
        if alive():
            stop()
            time.sleep(2)
        return 0 if start(cfg) else 1
    elif cmd == "auto":
        if alive():
            status()
            return 0
        # 当前策略：默认满缓存 32GB
        return 0 if start(FULL) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
