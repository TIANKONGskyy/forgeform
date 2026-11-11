# -*- coding: utf-8 -*-
"""FLUX 文生图:小型火箭摆件(白底产品图)"""
import urllib.request, json, time, os, uuid, sys
sys.stdout.reconfigure(encoding='utf-8')

BASE = "http://127.0.0.1:8188"

PROMPT = ("a small rocket model as a desktop ornament, "
          "detailed miniature space rocket standing upright, "
          "polished silver metallic body with sleek panel lines, "
          "red nose cone and red fins, tiny engine nozzles at the base, "
          "on a perfectly clean pure white background, "
          "bright even studio lighting, product photography, "
          "centered composition, sharp details, "
          "no shadow, no dark areas, no text no watermark")

NEGATIVE = "text, watermark, logo, blurry, low quality, distorted, people, hand"

def build_workflow():
    return {
        "3": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
            "clip_name2": "clip_l.safetensors", "type": "flux"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 0], "text": PROMPT}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 0], "text": NEGATIVE}},
        "10": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux1-dev-fp8.safetensors", "weight_dtype": "default"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "13": {"class_type": "EmptyLatentImage", "inputs": {"width": 1344, "height": 768, "batch_size": 1}},
        "14": {"class_type": "KSampler", "inputs": {
            "model": ["10", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["13", 0], "seed": 2026, "steps": 24, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["12", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {"images": ["15", 0], "filename_prefix": "rocket"}},
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

def wait_result(prompt_id, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/history/{prompt_id}") as r:
                h = json.loads(r.read())
        except Exception:
            time.sleep(5); continue
        if prompt_id in h:
            st = h[prompt_id]["status"]
            if st.get("status_str") == "success":
                return time.time() - t0
            if st.get("status_str") == "error":
                for msg in st.get("messages", []):
                    if msg[0] == "execution_error":
                        print("错误:", msg[1].get("exception_message"))
                return None
        time.sleep(5)
    return None

wf = build_workflow()
pid = submit(wf)
if pid:
    print("Flux 火箭任务:", pid)
    t = wait_result(pid)
    print(f"完成，耗时 {t:.0f}s" if t else "失败/超时")
else:
    print("提交失败")
