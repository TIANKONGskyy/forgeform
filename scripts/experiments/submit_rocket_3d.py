# -*- coding: utf-8 -*-
"""Hunyuan3D V2:火箭图 → 3D GLB(提交排队,视频完成后自动跑)"""
import urllib.request, json, time, os, uuid, sys
sys.stdout.reconfigure(encoding='utf-8')

BASE = "http://127.0.0.1:8188"

def upload_image(path):
    with open(path, "rb") as f:
        data = f.read()
    fname = os.path.basename(path)
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/upload/image", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    return resp["name"]

def build_workflow(img_name):
    return {
        "4": {"class_type": "LoadImage", "inputs": {"image": img_name}},
        "20": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": "birefnet.safetensors"}},
        "21": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["20", 0], "image": ["4", 0]}},
        "50": {"class_type": "[Comfy3D] Load Hunyuan3D V2 ShapeGen Pipeline", "inputs": {
            "generation_mode": "Hunyuan3D-2-Fast", "weights_format": "safetensors", "flash_vdm": True}},
        "51": {"class_type": "[Comfy3D] Hunyuan3D V2 DiT Flow Matching Model", "inputs": {
            "hunyuan3d_v2_i23d_pipe": ["50", 0],
            "reference_image": ["4", 0],
            "reference_mask": ["21", 0],
            "seed": 2026, "guidance_scale": 5.5,
            "num_inference_steps": 20, "octree_resolution": 256}},
        "70": {"class_type": "[Comfy3D] Save 3D Mesh", "inputs": {
            "mesh": ["51", 0], "save_path": r"C:\Users\mac\Desktop\火箭_3D.glb"}},
    }

def submit(prompt_workflow):
    data = json.dumps({"prompt": prompt_workflow, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(f"{BASE}/prompt", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
        return resp.get("prompt_id")
    except urllib.error.HTTPError as e:
        print("提交失败:", e.read().decode()[:800])
        return None

img = upload_image(r"D:\ComfyUI-New\input\rocket.png")
print("图片已上传:", img)
wf = build_workflow(img)
pid = submit(wf)
print("Hunyuan3D 火箭 3D 任务:", pid)
if pid:
    with open(r"C:\Users\mac\.openclaw\workspace\tmp\rocket_3d_pid.txt", "w") as f:
        f.write(pid)
