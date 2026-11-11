"""bench_cpu_concurrent.py - CPU 并发 3D 建模实测
用法: python bench_cpu_concurrent.py [并发数]
默认 10 并发，用 hunyuan3d (comfy3d_pipeline) 测
"""
import sys, time, glob, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")

# 用 ComfyUI 的 comfy3d_pipeline 测（GPU）
# CPU 路径直接 import torch + diffusers/hunyuan3d
# 注意：当前 Hunyuan3D 没有独立 CPU 包，实际走 ComfyUI GPU
# 这里用批量提交到 ComfyUI 队列来测并发能力

import requests

API = "http://127.0.0.1:8188"
CONCURRENT = int(sys.argv[1]) if len(sys.argv) > 1 else 10

def get_history(pid):
    try:
        return requests.get(f"{API}/history/{pid}", timeout=5).json()
    except:
        return {}

def wait_done(pid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = get_history(pid)
        if pid in h:
            s = h[pid].get("status", {}).get("status_str")
            if s == "success":
                return "ok"
            if s == "error":
                errs = h[pid].get("status", {}).get("messages", [])
                return "error:" + str(errs[-1][1] if errs else "unknown")[:100]
        time.sleep(5)
    return "timeout"

# 找输入图
imgs = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))
if not imgs:
    print("ERROR: no input images found"); sys.exit(1)
img = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))[-1]
print(f"Input image: {img}")

# 上传图片到 ComfyUI
print(f"\nUploading input image...")
with open(img, "rb") as f:
    r = requests.post(f"{API}/upload/image", files={"image": f}, timeout=30)
r.raise_for_status()
img_name = r.json().get("name", Path(img).name)
print(f"Uploaded as: {img_name}")

# 批量提交 CONCURRENT 个任务到 ComfyUI 队列
print(f"\nSubmitting {CONCURRENT} 3D tasks to ComfyUI queue...")
from backend.pipeline import Pipeline
pipe = Pipeline()
model_cfg = pipe.config.get("models", {}).get("3d", {}).get("hunyuan3d", {})

# 复用 comfy_client 的工作流构建
workflow_data = pipe.comfy._build_hunyuan3d_workflow({
    "model_config": model_cfg,
    "input_image": img_name,
    "seed": 2026,
    "guidance_scale": 5.0,
    "octree_resolution": 256,
    "steps": 20,
    "output_path": f"D:/AI-Workshop/outputs/3d/bench_cpu_{{}}.glb",
})

prompt_ids = []
for i in range(CONCURRENT):
    wf_copy = {
        "prompt": {
            k: dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v
            for k, v in workflow_data["prompt"].items()
        },
        "negative_prompt": ""
    }
    # 每个任务不同 seed 和输出路径
    for node in wf_copy["prompt"].values():
        if isinstance(node.get("inputs", {}).get("seed"), int):
            node["inputs"]["seed"] = 2026 + i
        if "save_path" in node.get("inputs", {}):
            node["inputs"]["save_path"] = f"bench_cpu_{i}.glb"

    r = requests.post(f"{API}/prompt", json={
        "prompt": wf_copy["prompt"],
        "client_id": pipe.comfy.client_id
    }, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    prompt_ids.append(pid)
    print(f"  [{i+1}/{CONCURRENT}] submitted: {pid[:8]}...")
    time.sleep(0.5)

# 监控所有任务
print(f"\nMonitoring {CONCURRENT} tasks...")
t0 = time.time()
completed = 0
errors = 0
while completed + errors < CONCURRENT:
    time.sleep(10)
    elapsed = int(time.time() - t0)
    done_ids = []
    for pid in prompt_ids:
        h = get_history(pid)
        if pid in h:
            s = h[pid].get("status", {}).get("status_str")
            if s == "success":
                done_ids.append(pid)
                completed += 1
            elif s == "error":
                done_ids.append(pid)
                errors += 1
    for pid in done_ids:
        prompt_ids.remove(pid)
    print(f"  [{elapsed}s] done={completed} error={errors} pending={CONCURRENT-completed-errors}")
    if elapsed > 1200:
        break

t_total = time.time() - t0
print(f"\n=== {CONCURRENT} 并发 3D 建模实测 ===")
print(f"总耗时: {t_total:.1f}s")
print(f"完成: {completed}  错误: {errors}")
if completed > 0:
    print(f"单任务平均: {t_total/completed:.1f}s (理论串行: {t_total*CONCURRENT/completed:.1f}s)")
    print(f"并发收益: {t_total*CONCURRENT/completed/t_total:.1f}x")
