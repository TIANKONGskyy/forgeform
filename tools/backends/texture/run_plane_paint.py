"""Submit the Hunyuan3D-2.1 Paint-PBR workflow for plane_21 to the isolated ComfyUI (port 8197).

Usage:
    python run_plane_paint.py --mesh <glb/stl> --image <png> --name plane_pbr_2048 \
        --texture 2048 --view 512 --steps 20 --port 8197
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import requests

WORK = r"D:\ForgeForm-oss\work\research\texture"
OUT = os.path.join(WORK, "out")
LOGS = os.path.join(WORK, "logs")


def log(msg, fh):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def vram():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"], text=True, timeout=10).strip()
        m, t, u = [x.strip() for x in out.split(",")[:3]]
        return int(m), int(t), int(u)
    except Exception:
        return -1, -1, -1


def wait_server(port, fh, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/system_stats", timeout=5)
            if r.status_code == 200:
                log(f"server up after {time.time()-t0:.0f}s", fh)
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def build_wf(mesh, image_name, name, texture, view, steps, seed, ortho):
    return {
        "1": {"class_type": "LocalResearchLoadMesh", "inputs": {"path": mesh}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "3": {"class_type": "Hy3D21CameraConfig",
              "inputs": {"camera_azimuths": "0,90,180,270,0,180",
                         "camera_elevations": "0,0,0,0,90,-90",
                         "view_weights": "1,0.2,0.5,0.2,0.05,0.05",
                         "ortho_scale": ortho}},
        "4": {"class_type": "Hy3DMultiViewsGenerator",
              "inputs": {"trimesh": ["1", 0], "camera_config": ["3", 0],
                         "paint_model": "hunyuan3d-paintpbr-v2-1",
                         "view_size": view, "image": ["2", 0], "steps": steps,
                         "guidance_scale": 3.0, "texture_size": texture,
                         "unwrap_mesh": True, "seed": seed,
                         "diffusion_backend": "pytorch"}},
        "5": {"class_type": "Hy3DBakeMultiViews",
              "inputs": {"pipeline": ["4", 0], "camera_config": ["4", 5],
                         "albedo": ["4", 1], "mr": ["4", 2]}},
        "6": {"class_type": "Hy3DInPaint",
              "inputs": {"pipeline": ["5", 0], "albedo": ["5", 1], "albedo_mask": ["5", 2],
                         "mr": ["5", 3], "mr_mask": ["5", 4],
                         "output_mesh_name": name}},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", required=True)
    ap.add_argument("--image", required=True, help="path; uploaded to ComfyUI input dir")
    ap.add_argument("--name", required=True)
    ap.add_argument("--texture", type=int, default=2048)
    ap.add_argument("--view", type=int, default=512)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ortho", type=float, default=1.1)
    ap.add_argument("--port", type=int, default=8197)
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    fh = open(os.path.join(LOGS, f"paint_{args.name}.log"), "a", encoding="utf-8")

    # reference image must live in the isolated ComfyUI input dir
    iso_input = r"D:\AI-Workshop\work\forgeform_model_eval\paint_research\comfy_isolated\input"
    image_name = os.path.basename(args.image)
    if os.path.abspath(args.image) != os.path.abspath(os.path.join(iso_input, image_name)):
        os.makedirs(iso_input, exist_ok=True)
        shutil.copy2(args.image, os.path.join(iso_input, image_name))

    if not wait_server(args.port, fh):
        log("SERVER NOT UP", fh)
        return 1

    # pre-flight: GPU must be idle (no other agent's job) and the main ComfyUI queue empty
    m, t, u = vram()
    busy = False
    try:
        q = requests.get("http://127.0.0.1:8188/queue", timeout=5).json()
        busy = bool(q.get("queue_running")) or bool(q.get("queue_pending"))
    except Exception:
        pass
    log(f"preflight vram={m}MB temp={t}C util={u}% main_comfy_busy={busy}", fh)
    # the always-on ComfyUI (:8188) legitimately keeps ~7 GB of models resident, so the
    # bar is "no *active* job": low utilisation + empty queue + no other big run
    if m > 12000 or u > 20 or busy:
        log("PREFLIGHT FAIL: GPU not idle, aborting before submit", fh)
        return 2

    wf = build_wf(args.mesh, image_name, args.name, args.texture, args.view,
                  args.steps, args.seed, args.ortho)
    with open(os.path.join(LOGS, f"workflow_{args.name}.json"), "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)

    t0 = time.time()
    r = requests.post(f"http://127.0.0.1:{args.port}/prompt", json={"prompt": wf}, timeout=60)
    log(f"submit {r.status_code} {r.text[:400]}", fh)
    r.raise_for_status()
    pid = r.json()["prompt_id"]

    peak = 0
    while time.time() - t0 < args.timeout:
        time.sleep(10)
        m, t, u = vram()
        peak = max(peak, m)
        log(f"  ... {time.time()-t0:.0f}s vram={m}MB peak={peak}MB temp={t}C util={u}%", fh)
        try:
            h = requests.get(f"http://127.0.0.1:{args.port}/history/{pid}", timeout=15).json()
        except Exception as e:
            log(f"  poll error {e!r}", fh)
            continue
        if pid in h:
            st = h[pid]["status"]
            log(f"DONE {st.get('status_str')} elapsed={time.time()-t0:.1f}s peak_vram={peak}MB", fh)
            with open(os.path.join(LOGS, f"history_{args.name}.json"), "w", encoding="utf-8") as f:
                json.dump(h[pid], f, ensure_ascii=False, indent=2)
            for k, v in st.get("messages", []):
                if k == "execution_error":
                    log(f"ERROR {v.get('node_type')} {v.get('exception_type')}: "
                        f"{str(v.get('exception_message'))[:1500]}", fh)
            if t >= 88:
                log("TEMP GUARD interrupt", fh)
                requests.post(f"http://127.0.0.1:{args.port}/interrupt", timeout=10)
            # copy artefacts
            iso_out = r"D:\AI-Workshop\work\forgeform_model_eval\paint_research\comfy_isolated\output"
            for fn in os.listdir(iso_out):
                if fn.startswith(args.name) or fn.startswith("character_paint"):
                    continue
                if args.name in fn:
                    shutil.copy2(os.path.join(iso_out, fn), os.path.join(OUT, fn))
                    log(f"copied {fn}", fh)
            break
    fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
