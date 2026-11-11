# -*- coding: utf-8 -*-
"""火箭收尾:等视频+3D完成,复制成品到桌面,写完成标记"""
import json, os, sys, time, urllib.request, shutil
sys.stdout.reconfigure(encoding='utf-8')

BASE = "http://127.0.0.1:8188"
VIDEO_PID = "29be23a6-16aa-4f98-b141-354df381822b"
T3D_PID = "3d16a2b9-2430-488b-848a-b336c82d4b50"
DONE = r"C:\Users\mac\.openclaw\workspace\tmp\rocket_done.log"
OUT_DIR = r"D:\ComfyUI-New\output"
DESKTOP = r"C:\Users\mac\Desktop"
VIDEO_TARGET = os.path.join(DESKTOP, "火箭展示.mp4")
GLB_TARGET = os.path.join(DESKTOP, "火箭_3D.glb")

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(DONE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def history(pid):
    try:
        with urllib.request.urlopen(f"{BASE}/history/{pid}", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None

def wait_one(pid, name, timeout=2400):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = history(pid)
        if h and pid in h:
            st = h[pid].get("status", {})
            s = st.get("status_str")
            if s == "success":
                log(f"{name} SUCCESS")
                return True
            if s == "error":
                for m in st.get("messages", []):
                    if m[0] == "execution_error":
                        log(f"{name} ERROR: {m[1].get('exception_message','')[:300]}")
                return False
        time.sleep(15)
    log(f"{name} TIMEOUT after {timeout}s")
    return False

ok_video = wait_one(VIDEO_PID, "VIDEO", timeout=2400)
if ok_video:
    cands = sorted([f for f in os.listdir(OUT_DIR) if f.startswith("rocket_v1") and f.endswith(".mp4")])
    if cands:
        src = os.path.join(OUT_DIR, cands[-1])
        shutil.copy2(src, VIDEO_TARGET)
        log(f"VIDEO copied -> {VIDEO_TARGET} ({os.path.getsize(VIDEO_TARGET)/1e6:.1f}MB)")
    else:
        log("VIDEO success but no mp4 found in output dir")

ok_3d = wait_one(T3D_PID, "3D", timeout=2400)
if ok_3d:
    if os.path.exists(GLB_TARGET):
        log(f"3D GLB exists -> {GLB_TARGET} ({os.path.getsize(GLB_TARGET)/1e6:.1f}MB)")
    else:
        log("3D success but GLB not found on desktop")

log("ALL DONE")
