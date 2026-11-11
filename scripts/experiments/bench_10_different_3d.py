"""bench_10_different_3d.py - 10个不同物品: 图→3D→STL，结果输出到桌面"""
import sys, time, glob, os, shutil, requests, json
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")

API = "http://127.0.0.1:8188"
OUT_DIR = Path("C:/Users/mac/Desktop/并发3D测试")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 10个不同的提示词
PROMPTS = [
    ("robot",        "a cute robot character, low poly, toy figurine, standing pose, colorful"),
    ("dragon",       "a dragon statue, fantasy, detailed scales, spread wings, dark purple"),
    ("watch",        "a vintage pocket watch, steampunk, brass gears visible, chain attached"),
    ("sushi",        "a colorful sushi platter, Japanese food, salmon Nigiri and maki rolls, ceramic plate"),
    ("gem",          "a large crystal gem, fantasy, glowing blue, faceted cut, floating"),
    ("gear",         "a mechanical gear assembly, steampunk, brass and copper pipes, intricate"),
    ("cat",          "a cute sitting cat, anime style, fluffy, round face, pink nose"),
    ("castle",       "a medieval castle, tiny scale model, grey stone walls, towers with flags"),
    ("flowerpot",    "a ceramic flower pot, colorful flowers, daisies and tulips, garden style"),
    ("spaceship",    "a spaceship, sci-fi, silver metallic, detailed cockpit windows, rocket engines"),
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
        time.sleep(3)
    return "timeout"

def wait_all(pids, label=""):
    t0 = time.time()
    results = {}
    while len(results) < len(pids):
        elapsed = int(time.time() - t0)
        for pid in list(pids):
            if pid in results:
                continue
            h = get_history(pid)
            if pid in h:
                s = h[pid].get("status", {}).get("status_str", "")
                if s == "success":
                    results[pid] = "ok"
                    print(f"  [{elapsed}s] DONE {pid[:8]}... ({len(results)}/{len(pids)}) {label}")
                elif s == "error":
                    results[pid] = "error"
                    msgs = h[pid].get("status", {}).get("messages", [])
                    err = msgs[-1][1] if msgs else {}
                    print(f"  [{elapsed}s] FAIL {pid[:8]}... | {str(err.get('exception_message', err))[:60]}")
        if elapsed > 900:
            break
        time.sleep(3)
    return time.time() - t0, results

# 用 pipeline
import warnings
warnings.filterwarnings("ignore")
from backend.pipeline import Pipeline
pipe = Pipeline()

print("=== Step 1: 生成 10 张不同图片 ===\n")
img_pids = []
img_names = []
for i, (name, prompt) in enumerate(PROMPTS):
    print(f"[{i+1}/10] Generating: {name} | {prompt[:50]}...")
    results = pipe.generate_images(
        prompt=prompt, count=1,
        model_key="flux",
        quality_name="标准",
        resolution="1024×1024 (正方形)",
        seed=1000 + i,
        negative="",
        skip_cache=False,
    )
    if results and results[0].success:
        img_path = results[0].output_path
        # upload
        with open(img_path, "rb") as f:
            r = requests.post(f"{API}/upload/image", files={"image": f}, timeout=30)
        r.raise_for_status()
        img_name = r.json().get("name", Path(img_path).name)
        img_names.append((name, img_name, img_path))
        print(f"  -> {Path(img_path).name}")
    else:
        print(f"  -> FAIL: {results[0].error if results else 'no result'}")
        img_names.append((name, None, None))
    time.sleep(1)

valid = [(n, n2, p) for n, n2, p in img_names if n2]
print(f"\n图片生成完成: {len(valid)}/10 成功\n")

# 获取3D模型配置
td_models = pipe.config.get("models", {}).get("3d", {})
model_cfg = td_models.get("hunyuan3d", {})

print("=== Step 2: 10个不同3D建模（并发提交） ===\n")
model_pids = []
for i, (name, img_name, img_path) in enumerate(valid):
    wf_data = pipe.comfy._build_hunyuan3d_v2_workflow({
        "model_config": model_cfg,
        "input_image": img_name,
        "seed": 2000 + i,
        "guidance_scale": 5.0,
        "octree_resolution": 256,
        "steps": 20,
        "output_path": str(OUT_DIR / f"{name}.glb"),
    })
    wf = {"prompt": wf_data["prompt"], "negative_prompt": ""}
    for node in wf["prompt"].values():
        if isinstance(node, dict) and "inputs" in node:
            if "save_path" in node["inputs"]:
                node["inputs"]["save_path"] = f"并发3D测试/{name}.glb"

    r = requests.post(f"{API}/prompt", json={
        "prompt": wf["prompt"],
        "client_id": pipe.comfy.client_id
    }, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    model_pids.append((name, pid))
    print(f"[{i+1}/{len(valid)}] submitted: {name} -> {pid[:8]}...")
    time.sleep(0.3)

# 等所有3D完成
pids_dict = {pid: name for name, pid in model_pids}
_, results_3d = wait_all(list(pids_dict.keys()), "3D")
t_3d = time.time()

# 找GLB文件
print("\n=== Step 3: 导出STL ===\n")
import shutil
found_glbs = []
for root, dirs, files in os.walk("/d/ComfyUI-New/output/并发3D测试"):
    for f in files:
        if f.endswith(".glb"):
            found_glbs.append(os.path.join(root, f))

print(f"找到 {len(found_glbs)} 个GLB文件")

stl_ok = 0
for glb in found_glbs:
    name = Path(glb).stem
    dest_glb = OUT_DIR / f"{name}.glb"
    dest_stl = OUT_DIR / f"{name}.stl"
    shutil.copy2(glb, dest_glb)

    # 转STL
    try:
        stl_result = pipe.export_stl(str(dest_glb), target_size_mm=80, optimize_preset="standard")
        if stl_result and stl_result.success:
            stl_dest = OUT_DIR / f"{name}.stl"
            shutil.copy2(stl_result.output_path, stl_dest)
            print(f"  {name}: GLB={dest_glb.stat().st_size//1024}KB  STL={stl_dest.stat().st_size//1024}KB  OK")
            stl_ok += 1
        else:
            print(f"  {name}: STL FAIL -> {stl_result.error if stl_result else '?'}")
    except Exception as e:
        print(f"  {name}: STL EXCEPTION -> {e}")

ok_3d = sum(1 for v in results_3d.values() if v == "ok")
print(f"\n=== 结果汇总 ===")
print(f"3D成功: {ok_3d}/{len(valid)}  STL成功: {stl_ok}/{ok_3d}")
print(f"输出目录: {OUT_DIR}")
