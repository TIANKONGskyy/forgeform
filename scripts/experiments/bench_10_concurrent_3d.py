"""bench_10_concurrent_3d.py - 并发跑10个不同3D建模，结果输出到桌面文件夹"""
import sys, time, glob, os, shutil, requests, json
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")

API = "http://127.0.0.1:8188"
OUT_DIR = Path("C:/Users/mac/Desktop/并发3D测试")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PROMPTS = [
    "a cute robot character, low poly style, toy figurine",
    "a dragon statue, fantasy, detailed scales",
    "a vintage pocket watch, steampunk style",
    "a sushi plate, colorful, Japanese food",
    "a crystal gem, fantasy, glowing effect",
    "a mechanical gear assembly, steampunk, brass",
    "a cute cat sitting, anime style figurine",
    "a medieval castle, tiny scale model",
    "a flower pot, ceramic, colorful flowers",
    "a spaceship, sci-fi, detailed cockpit",
]

def get_queue():
    try:
        return json.loads(requests.get(f"{API}/queue", timeout=5).text)
    except:
        return {}

def get_history(pid):
    try:
        return json.loads(requests.get(f"{API}/history/{pid}", timeout=5).text)
    except:
        return {}

def wait_done(pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = get_history(pid)
        if pid in h:
            s = h[pid].get("status", {}).get("status_str", "")
            if s == "success":
                return "ok"
            if s == "error":
                msgs = h[pid].get("status", {}).get("messages", [])
                err = msgs[-1][1] if msgs else {}
                return f"error:" + str(err.get("exception_message", err))[:100]
        time.sleep(5)
    return "timeout"

# 找一张输入图
imgs = sorted(glob.glob("D:/AI-Workshop/outputs/images/*.png"))
if not imgs:
    print("ERROR: no input image"); sys.exit(1)
img = imgs[-1]
print(f"Input: {img}")

# 上传图片
print("Uploading image...")
with open(img, "rb") as f:
    r = requests.post(f"{API}/upload/image", files={"image": f}, timeout=30)
r.raise_for_status()
img_name = r.json().get("name", Path(img).name)
print(f"Uploaded: {img_name}")

# 用 pipeline 构建正确的工作流
import warnings
warnings.filterwarnings("ignore")
from backend.pipeline import Pipeline
pipe = Pipeline()

# 获取模型配置（hunyuan3d = v2 ShapeGen）
td_models = pipe.config.get("models", {}).get("3d", {})
model_cfg = td_models.get("hunyuan3d", {})

print(f"\nSubmitting 10 tasks...")
prompt_ids = []
for i, prompt in enumerate(PROMPTS):
    wf_data = pipe.comfy._build_hunyuan3d_v2_workflow({
        "model_config": model_cfg,
        "input_image": img_name,
        "seed": 1000 + i,
        "guidance_scale": 5.0,
        "octree_resolution": 256,
        "steps": 20,
        "output_path": str(OUT_DIR / f"task_{i+1}.glb"),
    })
    wf = {"prompt": wf_data["prompt"], "negative_prompt": ""}
    # 修改输出路径
    for node in wf["prompt"].values():
        if isinstance(node, dict) and "inputs" in node:
            if "save_path" in node["inputs"]:
                node["inputs"]["save_path"] = f"并发3D测试/task_{i+1}.glb"

    r = requests.post(f"{API}/prompt", json={
        "prompt": wf["prompt"],
        "client_id": pipe.comfy.client_id
    }, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    prompt_ids.append(pid)
    print(f"  [{i+1}/10] submitted: {pid[:8]}... | {prompt[:40]}...")
    time.sleep(0.3)

print(f"\nAll submitted. Monitoring...")

t0 = time.time()
results = {}
while len(results) < 10:
    elapsed = int(time.time() - t0)
    queue = get_queue()
    running = len(queue.get("queue_running", []))
    pending = len(queue.get("queue_pending", []))
    
    for pid in list(prompt_ids):
        if pid in results:
            continue
        h = get_history(pid)
        if pid in h:
            s = h[pid].get("status", {}).get("status_str", "")
            if s == "success":
                results[pid] = "ok"
                print(f"  [{elapsed}s] DONE: {pid[:8]}... ({len(results)}/10)")
            elif s == "error":
                results[pid] = "error"
                msgs = h[pid].get("status", {}).get("messages", [])
                err = msgs[-1][1] if msgs else {}
                print(f"  [{elapsed}s] FAIL: {pid[:8]}... | {err.get('exception_message', str(err))[:60]}")
    
    print(f"  [{elapsed}s] running={running} pending={pending} done={len(results)}/10")
    if elapsed > 900:
        break
    time.sleep(10)

t_total = time.time() - t0
ok = sum(1 for v in results.values() if v == "ok")
fail = sum(1 for v in results.values() if v != "ok")

print(f"\n=== 10并发3D建模实测 ===")
print(f"总耗时: {t_total:.1f}s")
print(f"成功: {ok}/10  失败: {fail}/10")

# 列出输出文件
glbs = list(OUT_DIR.glob("task_*.glb"))
print(f"\n输出文件 ({len(glbs)}):")
for g in sorted(glbs):
    sz = g.stat().st_size / 1024
    print(f"  {g.name}  {sz:.0f}KB")

# 复制一个代表性截图说明
print(f"\n输出目录: {OUT_DIR}")
